"""
文件/文件夹同步工具（基于 watchdog）。
从 .env 读取多组同步对，监听源目录的变化，
将所有变更（创建/修改/删除/移动）实时镜像到目标目录。

每个同步对支持:
  - MODE:   copy = 复制（默认，源文件保留）；move = 移动（同步后删除源文件）
  - DELAY_SECONDS: 检测到变化后延迟多少秒再上传（0 = 立即，默认）
                   支持 60*3 这类简单算式
  - DEPTH:  限制同步的目录层数（0 = 无限制）
  - IGNORE: 独立的忽略规则（与全局 SYNC_IGNORE 合并生效）
"""

import os
import re
import ast
import sys
import time
import shutil
import fnmatch
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 从脚本所在目录加载 .env 配置（不使用 dotenv 的转义，避免反斜杠路径被误解析）
def _load_env_raw(env_path: Path):
    """原样加载 .env，不对反斜杠做转义处理。"""
    if not env_path.exists():
        return
    seen = set()
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # 同一个键写了两遍（常见于复制同步对忘记改编号），后面的会覆盖前面的
            if key in seen:
                logging.warning(".env 中 %s 出现多次，后一次会覆盖前一次，"
                                "复制同步对时请记得改编号", key)
            seen.add(key)
            # 去掉值两端的引号（单引号或双引号），但不做转义
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ[key] = value

_load_env_raw(Path(__file__).parent / ".env")


# --------------- 配置 ---------------

VALID_MODES = ("copy", "move")


@dataclass
class SyncPair:
    source: str
    dest: str
    depth: int = 0  # 0 = 无限制
    ignore: list[str] = field(default_factory=list)
    mode: str = "copy"  # copy = 复制并镜像删除; move = 移动（源文件被搬走）
    delay_seconds: float = 0.0  # 变化发生后延迟多少秒再上传，0 = 立即


def load_sync_pairs() -> list[SyncPair]:
    """从环境变量解析 SYNC_PAIR_<N>_SOURCE/DEST/MODE/DELAY_SECONDS/DEPTH/IGNORE。"""
    global_ignore = _parse_patterns(os.getenv("SYNC_IGNORE", ""))
    global_mode = _parse_mode(os.getenv("SYNC_MODE", "copy"), "全局 SYNC_MODE")
    global_delay = _parse_delay(os.getenv("SYNC_DELAY_SECONDS", ""), "全局 SYNC_DELAY_SECONDS")

    raw: dict[str, dict[str, str]] = {}
    for key, value in os.environ.items():
        m = re.match(r"SYNC_PAIR_(\d+)_(SOURCE|DEST|MODE|DELAY_SECONDS|DELAY|DEPTH|IGNORE)$", key)
        if m:
            idx, role = m.group(1), m.group(2).lower()
            raw.setdefault(idx, {})[role] = value.strip()

    pairs = []
    for idx in sorted(raw, key=int):
        p = raw[idx]
        src, dst = p.get("source"), p.get("dest")
        if not src or not dst:
            logging.warning("同步对 %s 配置不完整，已跳过", idx)
            continue

        depth = int(p.get("depth", "0"))
        pair_ignore = _parse_patterns(p.get("ignore", ""))
        merged_ignore = list(set(global_ignore + pair_ignore))
        mode = _parse_mode(p.get("mode", ""), "同步对 %s" % idx, default=global_mode)
        # DELAY_SECONDS 为标准写法，DELAY 作为简写别名
        raw_delay = p.get("delay_seconds") or p.get("delay") or ""
        delay = _parse_delay(raw_delay, "同步对 %s" % idx, default=global_delay)

        pairs.append(SyncPair(
            source=os.path.normpath(src),
            dest=os.path.normpath(dst),
            depth=depth,
            ignore=merged_ignore,
            mode=mode,
            delay_seconds=delay,
        ))
    return pairs


def _parse_patterns(raw: str) -> list[str]:
    """解析逗号分隔的忽略规则字符串。"""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_mode(raw: str, who: str, default: str = "copy") -> str:
    """解析同步方式：copy（复制）或 move（移动）。留空时取默认值。"""
    mode = (raw or "").strip().lower()
    if not mode:
        return default
    if mode not in VALID_MODES:
        logging.warning("%s 的 MODE=%s 无效（仅支持 copy/move），已按 %s 处理", who, raw, default)
        return default
    return mode


_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)


def _eval_number(node) -> float:
    """只对数字与 + - * / ( ) 组成的算式求值，不执行任何其它语法。"""
    if isinstance(node, ast.Expression):
        return _eval_number(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_number(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        left, right = _eval_number(node.left), _eval_number(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise ValueError("除数为 0")
        return left / right
    raise ValueError("不支持的表达式")


def parse_seconds(text: str) -> float:
    """把秒数文本解析成数值，支持 30、1.5 以及 60*3、2*60+30 这类简单算式。"""
    return _eval_number(ast.parse(text, mode="eval"))


def _parse_delay(raw: str, who: str, default: float = 0.0) -> float:
    """解析延迟秒数（支持 60*3 这类算式）。留空取默认值；非法或负数按默认值处理。"""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = parse_seconds(text)
    except (ValueError, SyntaxError, TypeError, ZeroDivisionError, RecursionError, MemoryError):
        logging.warning("%s 的 DELAY_SECONDS=%s 无法解析（支持 30、1.5、60*3 这类写法），"
                        "已按 %s 秒处理", who, raw, default)
        return default
    if value < 0:
        logging.warning("%s 的 DELAY_SECONDS=%s 为负数，已按 %s 秒处理", who, raw, default)
        return default
    return value


# --------------- 工具函数 ---------------

def should_ignore(path: str, patterns: list[str]) -> bool:
    """判断路径是否匹配忽略规则。"""
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def get_depth(path: str, base: str) -> int:
    """计算 path 相对于 base 的目录层级深度，base 本身为 0。"""
    rel = os.path.relpath(path, base)
    if rel == ".":
        return 0
    return len(Path(rel).parts)


def exceeds_depth(path: str, base: str, max_depth: int) -> bool:
    """检查路径是否超过允许的最大深度。max_depth=0 表示无限制。"""
    if max_depth == 0:
        return False
    return get_depth(path, base) >= max_depth


def wait_until_stable(path: str, interval: float = 0.3, max_wait: float = 5.0) -> bool:
    """等待文件大小连续两次相同，避免搬走/复制正在写入中的文件。"""
    last = -1
    waited = 0.0
    while waited < max_wait:
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size == last:
            return True
        last = size
        time.sleep(interval)
        waited += interval
    return True


def move_file(src: str, dst: str) -> bool:
    """把单个文件移动到目标位置（目标已存在则覆盖）。"""
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            os.remove(dst)
        shutil.move(src, dst)
        logging.info("[移动文件] %s -> %s", src, dst)
        return True
    except Exception as e:
        logging.error("移动文件失败 %s: %s", src, e)
        return False


def _fmt_delay(delay: float) -> str:
    """把延迟秒数格式化成简洁的日志文本。"""
    return str(int(delay)) if float(delay).is_integer() else str(delay)


def full_move(pair: SyncPair):
    """一次性全量搬运: 把源目录中的文件移动到目标目录（源文件不再保留）。

    与 full_sync 不同，move 模式下目标目录是"收件箱"：
    只搬入，不会因为源目录缺少某个文件就删除目标中的内容。
    """
    src, dst = pair.source, pair.dest

    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not should_ignore(d, pair.ignore)]

        # 深度检查：与 full_sync 保持一致
        current_depth = get_depth(root, src)
        if pair.depth > 0 and current_depth + 1 >= pair.depth:
            dirs.clear()
        if exceeds_depth(root, src, pair.depth):
            continue

        rel = os.path.relpath(root, src)
        dst_root = os.path.normpath(os.path.join(dst, rel))

        for f in files:
            if should_ignore(f, pair.ignore):
                continue
            move_file(os.path.join(root, f), os.path.join(dst_root, f))


def full_sync(pair: SyncPair):
    """一次性全量镜像同步: 源目录 -> 目标目录（复制新增/更新文件，删除多余文件）。"""
    src, dst = pair.source, pair.dest

    # 复制 / 更新
    for root, dirs, files in os.walk(src):
        # 过滤掉需要忽略的目录
        dirs[:] = [d for d in dirs if not should_ignore(d, pair.ignore)]

        # 深度检查：如果再深入就超过限制，不再继续深入
        current_depth = get_depth(root, src)
        if pair.depth > 0 and current_depth + 1 >= pair.depth:
            dirs.clear()

        # 如果当前目录本身已超过允许深度，跳过
        if exceeds_depth(root, src, pair.depth):
            continue

        rel = os.path.relpath(root, src)
        dst_root = os.path.join(dst, rel)
        os.makedirs(dst_root, exist_ok=True)

        for f in files:
            if should_ignore(f, pair.ignore):
                continue
            s = os.path.join(root, f)
            d = os.path.join(dst_root, f)
            if not os.path.exists(d) or os.stat(s).st_mtime > os.stat(d).st_mtime:
                shutil.copy2(s, d)
                logging.debug("已复制: %s -> %s", s, d)

    # 删除目标目录中源目录已不存在的文件/文件夹，或超出深度限制的内容
    for root, dirs, files in os.walk(dst, topdown=False):
        rel = os.path.relpath(root, dst)
        src_root = os.path.join(src, rel)

        # 如果目标目录中此路径超出深度限制，整个删除
        if exceeds_depth(root, dst, pair.depth):
            shutil.rmtree(root, ignore_errors=True)
            logging.debug("已删除（超出深度）: %s", root)
            continue

        for f in files:
            src_file = os.path.join(src_root, f)
            if not os.path.exists(src_file):
                target = os.path.join(root, f)
                os.remove(target)
                logging.debug("已删除文件: %s", target)

        for d in dirs:
            src_dir = os.path.join(src_root, d)
            if not os.path.exists(src_dir):
                target = os.path.join(root, d)
                shutil.rmtree(target, ignore_errors=True)
                logging.debug("已删除目录: %s", target)


# --------------- 延迟执行 ---------------

class DelayedRunner:
    """按路径合并的延迟执行器。

    delay = 0 时立即执行（与未启用延迟时行为完全一致）；
    delay > 0 时，从同一路径的第一次事件开始计时，delay 秒后执行；
    这段窗口内该路径再次发生变化只更新最终要做的动作，不会重新计时，
    因此频繁改动的文件也能保证在 delay 秒内被上传一次。
    """

    def __init__(self, delay: float):
        self.delay = delay
        self._pending: dict = {}
        self._timers: dict = {}
        self._lock = threading.Lock()

    def run(self, key: str, action):
        if self.delay <= 0:
            action()
            return
        with self._lock:
            self._pending[key] = action
            if key in self._timers:
                return  # 已在等待中，只替换动作，不重新计时
            timer = threading.Timer(self.delay, self._fire, args=(key,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _fire(self, key: str):
        with self._lock:
            self._timers.pop(key, None)
            action = self._pending.pop(key, None)
        if action is None:
            return
        try:
            action()
        except Exception as e:
            logging.error("延迟任务执行失败 %s: %s", key, e)

    def flush(self):
        """立即执行所有还在等待中的任务（退出前调用，避免丢改动）。"""
        with self._lock:
            timers, self._timers = self._timers, {}
            pending, self._pending = self._pending, {}
        for timer in timers.values():
            timer.cancel()
        if pending:
            logging.info("正在完成 %d 个延迟中的同步任务...", len(pending))
        for key, action in pending.items():
            try:
                action()
            except Exception as e:
                logging.error("延迟任务执行失败 %s: %s", key, e)


# --------------- 事件处理器 ---------------

class SyncHandler(FileSystemEventHandler):
    def __init__(self, pair: SyncPair):
        self.src = pair.source
        self.dst = pair.dest
        self.depth = pair.depth
        self.patterns = pair.ignore
        self.mode = pair.mode
        self.delay = pair.delay_seconds
        self.runner = DelayedRunner(pair.delay_seconds)

    def _dst_path(self, src_path: str) -> str:
        """将源路径转换为对应的目标路径。"""
        rel = os.path.relpath(src_path, self.src)
        return os.path.join(self.dst, rel)

    def flush(self):
        """把还在延迟窗口里的改动立刻同步掉。"""
        self.runner.flush()

    def _move_in(self, src_path: str):
        """move 模式：把源文件搬到目标目录（等待写入完成后再搬）。"""
        # 移动到监听范围之外的事件，dest_path 不在源目录内，直接忽略
        rel = os.path.relpath(src_path, self.src)
        if rel.startswith(".."):
            return
        if not os.path.isfile(src_path):
            return
        wait_until_stable(src_path)
        if os.path.isfile(src_path):
            move_file(src_path, self._dst_path(src_path))

    def _should_skip(self, path: str, is_directory: bool = False) -> bool:
        """判断是否应该跳过此路径（匹配忽略规则或超出深度）。"""
        if should_ignore(path, self.patterns):
            return True
        if self.depth > 0:
            depth = get_depth(path, self.src)
            # 目录：depth >= max_depth 时跳过（与 full_sync 一致）
            # 文件：depth > max_depth 时跳过（文件比所在目录深一级）
            if is_directory and depth >= self.depth:
                return True
            if not is_directory and depth > self.depth:
                return True
        return False

    # --- 事件回调 ---

    def on_created(self, event):
        """文件或目录被创建时触发。"""
        if self._should_skip(event.src_path, event.is_directory):
            return
        src_path, is_dir = event.src_path, event.is_directory
        self.runner.run(src_path, lambda: self._apply_created(src_path, is_dir))

    def _apply_created(self, src_path: str, is_directory: bool):
        if self.mode == "move":
            # 目录本身留在源端，只搬运其中的文件
            if not is_directory:
                self._move_in(src_path)
            return
        dst = self._dst_path(src_path)
        try:
            if is_directory:
                os.makedirs(dst, exist_ok=True)
                logging.info("[创建目录] %s -> %s", src_path, dst)
            else:
                if not os.path.exists(src_path):
                    return  # 延迟期间源文件已消失
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src_path, dst)
                logging.info("[创建文件] %s -> %s", src_path, dst)
        except Exception as e:
            logging.error("创建同步失败: %s", e)

    def on_modified(self, event):
        """文件被修改时触发。"""
        if event.is_directory or self._should_skip(event.src_path, False):
            return
        src_path = event.src_path
        self.runner.run(src_path, lambda: self._apply_modified(src_path))

    def _apply_modified(self, src_path: str):
        if self.mode == "move":
            self._move_in(src_path)
            return
        if not os.path.exists(src_path):
            return  # 延迟期间源文件已消失
        dst = self._dst_path(src_path)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src_path, dst)
            logging.info("[修改文件] %s -> %s", src_path, dst)
        except Exception as e:
            logging.error("修改同步失败: %s", e)

    def on_deleted(self, event):
        """文件或目录被删除时触发。"""
        if self._should_skip(event.src_path, event.is_directory):
            return
        if self.mode == "move":
            # 源文件被搬走（或用户自行删除）都不影响已入库的目标文件
            return
        src_path, is_dir = event.src_path, event.is_directory
        self.runner.run(src_path, lambda: self._apply_deleted(src_path, is_dir))

    def _apply_deleted(self, src_path: str, is_directory: bool):
        # 延迟期间源路径又出现（编辑器"删除+重建"式保存），就不该再删目标
        if os.path.exists(src_path):
            return
        dst = self._dst_path(src_path)
        try:
            if is_directory:
                shutil.rmtree(dst, ignore_errors=True)
                logging.info("[删除目录] %s", dst)
            else:
                if os.path.exists(dst):
                    os.remove(dst)
                    logging.info("[删除文件] %s", dst)
        except Exception as e:
            logging.error("删除同步失败: %s", e)

    def on_moved(self, event):
        """文件或目录被移动/重命名时触发。"""
        if self._should_skip(event.src_path, event.is_directory) and self._should_skip(event.dest_path, event.is_directory):
            return
        if self.mode == "move":
            # 源目录内的改名 = 出现了一个新文件，按新名字搬运
            if not event.is_directory and not self._should_skip(event.dest_path, False):
                dest_path = event.dest_path
                self.runner.run(dest_path, lambda: self._move_in(dest_path))
            return
        src_path, dest_path = event.src_path, event.dest_path
        self.runner.run(src_path, lambda: self._apply_moved(src_path, dest_path))

    def _apply_moved(self, src_path: str, dest_path: str):
        src_dst = self._dst_path(src_path)
        dest_dst = self._dst_path(dest_path)

        # 如果移动目标超出深度限制，仅执行删除操作
        if exceeds_depth(dest_path, self.src, self.depth):
            if os.path.exists(src_dst):
                if os.path.isdir(src_dst):
                    shutil.rmtree(src_dst, ignore_errors=True)
                else:
                    os.remove(src_dst)
                logging.info("[移出范围] 已删除 %s（目标超出深度限制）", src_dst)
            return

        try:
            if os.path.exists(src_dst):
                os.makedirs(os.path.dirname(dest_dst), exist_ok=True)
                shutil.move(src_dst, dest_dst)
                logging.info("[移动]     %s -> %s", src_dst, dest_dst)
            elif os.path.exists(dest_path):
                # 延迟期间目标端还没有旧副本（窗口内新建后又改名），直接补传
                os.makedirs(os.path.dirname(dest_dst), exist_ok=True)
                shutil.copy2(dest_path, dest_dst)
                logging.info("[移动]     %s -> %s", dest_path, dest_dst)
        except Exception as e:
            logging.error("移动同步失败: %s", e)


class SingleFileHandler(FileSystemEventHandler):
    """监听单个文件的变化，将其同步到目标目录。

    针对"被程序频繁重写"的文件（如输入法词典）做了三点加固：
      1. 处理 on_moved —— 编辑器/输入法常用"写临时文件 + 改名覆盖"保存，
         这会触发 moved 而非 modified，旧实现会漏掉。
      2. 防抖 —— 连续事件合并，延迟后再复制，避开写入中途。
      3. 大小校验 + 重试 —— 避免把写到一半的残缺/空文件复制到目标。
    """
    _DEBOUNCE_SEC = 0.4
    _RETRY = 5
    _RETRY_WAIT = 0.3

    def __init__(self, src_file: str, dst_dir: str, mode: str = "copy", delay: float = 0.0):
        self.src_file = os.path.normpath(src_file)
        self.dst_dir = dst_dir
        self.mode = mode
        self.delay = delay
        self.dst_file = os.path.join(dst_dir, os.path.basename(src_file))
        self._timer: threading.Timer | None = None
        self._pending = None
        self._lock = threading.Lock()

    def _is_target(self, path: str) -> bool:
        return os.path.normpath(path) == self.src_file

    def _schedule(self, action):
        """合并连续事件，延迟后只执行最后一个动作。

        delay = 0：沿用原有的 0.4 秒防抖（连续事件重新计时，避开写入中途）；
        delay > 0：从第一次事件起算固定延迟，窗口内的后续事件只替换动作。
        """
        with self._lock:
            self._pending = action
            if self.delay > 0:
                if self._timer is not None:
                    return
                wait = self.delay
            else:
                if self._timer is not None:
                    self._timer.cancel()
                wait = self._DEBOUNCE_SEC
            self._timer = threading.Timer(wait, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            self._timer = None
            action, self._pending = self._pending, None
        if action is not None:
            action()

    def _schedule_copy(self):
        self._schedule(self._do_copy)

    def _schedule_delete(self):
        self._schedule(self._do_delete)

    def flush(self):
        """把还在延迟窗口里的改动立刻同步掉。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            action, self._pending = self._pending, None
        if action is not None:
            logging.info("正在完成延迟中的同步任务: %s", self.src_file)
            action()

    def _do_copy(self):
        # 源文件可能正被占用或写入中途，重试若干次并校验大小一致
        for attempt in range(1, self._RETRY + 1):
            try:
                if not os.path.exists(self.src_file):
                    return
                os.makedirs(self.dst_dir, exist_ok=True)
                shutil.copy2(self.src_file, self.dst_file)
                if os.path.getsize(self.src_file) == os.path.getsize(self.dst_file):
                    logging.info("[同步文件] %s -> %s", self.src_file, self.dst_file)
                    if self.mode == "move":
                        try:
                            os.remove(self.src_file)
                            logging.info("[已移走源文件] %s", self.src_file)
                        except OSError as e:
                            logging.error("删除源文件失败: %s", e)
                    return
                logging.warning("文件大小不一致（可能写入中途），重试: %s", self.src_file)
            except Exception as e:
                logging.warning("同步文件失败（第 %d 次）: %s", attempt, e)
            time.sleep(self._RETRY_WAIT)
        logging.error("同步文件最终失败: %s", self.src_file)

    def _do_delete(self):
        # move 模式下源文件本就会消失，目标文件应当保留
        if self.mode == "move":
            return
        # 延迟期间源文件又出现（原子保存），不再删除目标
        if os.path.exists(self.src_file):
            return
        try:
            if os.path.exists(self.dst_file):
                os.remove(self.dst_file)
                logging.info("[删除文件] %s", self.dst_file)
        except Exception as e:
            logging.error("删除同步失败: %s", e)

    def on_modified(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        self._schedule_copy()

    def on_created(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        self._schedule_copy()

    def on_moved(self, event):
        if event.is_directory:
            return
        # 原子保存：临时文件被改名覆盖到源文件路径
        if self._is_target(event.dest_path):
            self._schedule_copy()
        # 源文件被改名移走，视为删除
        elif self._is_target(event.src_path):
            self._schedule_delete()

    def on_deleted(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        self._schedule_delete()


# --------------- .env 热重载 ---------------

def _clear_sync_env_vars():
    """清除当前进程中所有 SYNC_PAIR_* 及全局 SYNC_* 配置，便于重新加载。"""
    for k in list(os.environ.keys()):
        if k.startswith("SYNC_PAIR_") or k in ("SYNC_IGNORE", "SYNC_MODE", "SYNC_DELAY_SECONDS"):
            del os.environ[k]


class EnvReloadHandler(FileSystemEventHandler):
    """监听 .env 文件本身的变化，触发重载回调。"""
    def __init__(self, env_path: Path, callback):
        self.env_path = os.path.normpath(str(env_path))
        self.callback = callback

    def _is_env(self, path: str) -> bool:
        return os.path.normpath(path) == self.env_path

    def on_modified(self, event):
        if not event.is_directory and self._is_env(event.src_path):
            self.callback()

    def on_created(self, event):
        if not event.is_directory and self._is_env(event.src_path):
            self.callback()

    def on_moved(self, event):
        if event.is_directory:
            return
        # 编辑器有时通过 "写临时文件 + 重命名" 完成保存
        if self._is_env(event.src_path) or self._is_env(event.dest_path):
            self.callback()


class SyncManager:
    """管理所有同步对的生命周期，支持 .env 修改后动态增删同步对。"""

    def __init__(self, env_path: Path):
        self.env_path = env_path
        self.observer = Observer()
        # pair_key -> (handler, ObservedWatch)
        self.watches: dict[tuple, tuple] = {}
        self._reload_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _pair_key(pair: SyncPair) -> tuple:
        """同步对的唯一标识：source/dest/mode/delay/depth/ignore 任一变动即视为新对。"""
        return (pair.source, pair.dest, pair.mode, pair.delay_seconds,
                pair.depth, tuple(sorted(pair.ignore)))

    def _initial_sync(self, pair: SyncPair) -> bool:
        if os.path.isdir(pair.source):
            os.makedirs(pair.dest, exist_ok=True)
            depth_desc = "无限制" if pair.depth == 0 else str(pair.depth)
            logging.info("同步对[%s]: %s -> %s [延迟=%s秒, 深度=%s, 忽略=%s]",
                         pair.mode, pair.source, pair.dest,
                         _fmt_delay(pair.delay_seconds), depth_desc, pair.ignore)
            if pair.mode == "move":
                full_move(pair)
            else:
                full_sync(pair)
            return True
        if os.path.isfile(pair.source):
            os.makedirs(pair.dest, exist_ok=True)
            dst_file = os.path.join(pair.dest, os.path.basename(pair.source))
            logging.info("同步文件[%s]: %s -> %s [延迟=%s秒]",
                         pair.mode, pair.source, dst_file, _fmt_delay(pair.delay_seconds))
            if pair.mode == "move":
                move_file(pair.source, dst_file)
            elif not os.path.exists(dst_file) or os.stat(pair.source).st_mtime > os.stat(dst_file).st_mtime:
                shutil.copy2(pair.source, dst_file)
            return True
        # move 模式的单文件对：源文件可能已被搬走，只要父目录还在就继续监听
        if pair.mode == "move" and os.path.isdir(os.path.dirname(pair.source)):
            os.makedirs(pair.dest, exist_ok=True)
            logging.info("同步文件[move]: %s 当前不存在，等待其出现", pair.source)
            return True
        logging.error("源路径不存在: %s，已跳过", pair.source)
        return False

    def _schedule_pair(self, pair: SyncPair):
        if os.path.isdir(pair.source):
            handler = SyncHandler(pair)
            watch = self.observer.schedule(handler, pair.source, recursive=True)
        else:
            handler = SingleFileHandler(pair.source, pair.dest, pair.mode, pair.delay_seconds)
            watch = self.observer.schedule(handler, os.path.dirname(pair.source), recursive=False)
        return handler, watch

    def add_pair(self, pair: SyncPair):
        key = self._pair_key(pair)
        if key in self.watches:
            return
        if not self._initial_sync(pair):
            return
        try:
            handler, watch = self._schedule_pair(pair)
            self.watches[key] = (handler, watch)
            logging.info("[已添加] [%s, 延迟%s秒] %s -> %s",
                         pair.mode, _fmt_delay(pair.delay_seconds), pair.source, pair.dest)
        except Exception as e:
            logging.error("添加同步对失败 %s: %s", pair.source, e)

    def remove_pair(self, key: tuple):
        entry = self.watches.pop(key, None)
        if entry is None:
            return
        handler, watch = entry
        try:
            # 先把还在延迟窗口里的改动落地，再摘掉 handler
            handler.flush()
            # 仅移除该 handler，不影响共享同一 watch 路径的其他 handler
            self.observer.remove_handler_for_watch(handler, watch)
            logging.info("[已移除] [%s] %s -> %s", key[2], key[0], key[1])
        except Exception as e:
            logging.error("移除同步对失败: %s", e)

    def _reload(self):
        with self._lock:
            self._reload_timer = None
            _clear_sync_env_vars()
            try:
                _load_env_raw(self.env_path)
            except Exception as e:
                logging.error("重新加载 .env 失败: %s", e)
                return

            new_pairs = load_sync_pairs()
            new_map = {self._pair_key(p): p for p in new_pairs}
            old_keys = set(self.watches.keys())
            new_keys = set(new_map.keys())

            # 同步日志级别（如果 .env 改了 SYNC_LOG_LEVEL）
            log_level = os.getenv("SYNC_LOG_LEVEL", "INFO").upper()
            logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))

            if new_keys == old_keys:
                logging.info("[.env 重载] 配置无变化")
                return

            logging.info("[.env 重载] 应用新配置...")
            for key in old_keys - new_keys:
                self.remove_pair(key)
            for key in new_keys - old_keys:
                self.add_pair(new_map[key])
            logging.info("[.env 重载完成] 当前同步对数量: %d", len(self.watches))

    def schedule_reload(self):
        """防抖：1 秒内多次 .env 修改事件合并为一次重载。"""
        with self._lock:
            if self._reload_timer is not None:
                self._reload_timer.cancel()
            self._reload_timer = threading.Timer(1.0, self._reload)
            self._reload_timer.daemon = True
            self._reload_timer.start()

    def start(self):
        pairs = load_sync_pairs()
        if not pairs:
            logging.error("未在 .env 中找到任何同步对，程序退出。")
            sys.exit(1)

        logging.info("正在执行初始全量同步...")
        for pair in pairs:
            self.add_pair(pair)

        # 监听 .env 自身（监听其所在目录，handler 内做文件名过滤）
        env_handler = EnvReloadHandler(self.env_path, self.schedule_reload)
        self.observer.schedule(env_handler, str(self.env_path.parent), recursive=False)

        self.observer.start()
        logging.info("正在监听 %d 个同步对，并监听 .env 变化，按 Ctrl+C 停止。", len(self.watches))

    def stop(self):
        with self._lock:
            if self._reload_timer is not None:
                self._reload_timer.cancel()
                self._reload_timer = None
        self.observer.stop()
        self.observer.join()
        # 退出前把延迟窗口里未执行的同步补完，避免丢改动
        for handler, _ in list(self.watches.values()):
            handler.flush()


# --------------- 主程序 ---------------

def main():
    log_level = os.getenv("SYNC_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    env_path = Path(__file__).parent / ".env"
    manager = SyncManager(env_path)
    manager.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("正在停止...")
        manager.stop()


if __name__ == "__main__":
    main()
