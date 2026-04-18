"""
文件/文件夹同步工具（基于 watchdog）。
从 .env 读取多组同步对，监听源目录的变化，
将所有变更（创建/修改/删除/移动）实时镜像到目标目录。

每个同步对支持:
  - DEPTH: 限制同步的目录层数（0 = 无限制）
  - IGNORE: 独立的忽略规则（与全局 SYNC_IGNORE 合并生效）
"""

import os
import re
import sys
import time
import shutil
import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 从脚本所在目录加载 .env 配置（不使用 dotenv 的转义，避免反斜杠路径被误解析）
def _load_env_raw(env_path: Path):
    """原样加载 .env，不对反斜杠做转义处理。"""
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # 去掉值两端的引号（单引号或双引号），但不做转义
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ[key] = value

_load_env_raw(Path(__file__).parent / ".env")


# --------------- 配置 ---------------

@dataclass
class SyncPair:
    source: str
    dest: str
    depth: int = 0  # 0 = 无限制
    ignore: list[str] = field(default_factory=list)


def load_sync_pairs() -> list[SyncPair]:
    """从环境变量解析 SYNC_PAIR_<N>_SOURCE/DEST/DEPTH/IGNORE。"""
    global_ignore = _parse_patterns(os.getenv("SYNC_IGNORE", ""))

    raw: dict[str, dict[str, str]] = {}
    for key, value in os.environ.items():
        m = re.match(r"SYNC_PAIR_(\d+)_(SOURCE|DEST|DEPTH|IGNORE)$", key)
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

        pairs.append(SyncPair(
            source=os.path.normpath(src),
            dest=os.path.normpath(dst),
            depth=depth,
            ignore=merged_ignore,
        ))
    return pairs


def _parse_patterns(raw: str) -> list[str]:
    """解析逗号分隔的忽略规则字符串。"""
    return [p.strip() for p in raw.split(",") if p.strip()]


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


# --------------- 事件处理器 ---------------

class SyncHandler(FileSystemEventHandler):
    def __init__(self, pair: SyncPair):
        self.src = pair.source
        self.dst = pair.dest
        self.depth = pair.depth
        self.patterns = pair.ignore

    def _dst_path(self, src_path: str) -> str:
        """将源路径转换为对应的目标路径。"""
        rel = os.path.relpath(src_path, self.src)
        return os.path.join(self.dst, rel)

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
        dst = self._dst_path(event.src_path)
        try:
            if event.is_directory:
                os.makedirs(dst, exist_ok=True)
                logging.info("[创建目录] %s -> %s", event.src_path, dst)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(event.src_path, dst)
                logging.info("[创建文件] %s -> %s", event.src_path, dst)
        except Exception as e:
            logging.error("创建同步失败: %s", e)

    def on_modified(self, event):
        """文件被修改时触发。"""
        if event.is_directory or self._should_skip(event.src_path, False):
            return
        dst = self._dst_path(event.src_path)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(event.src_path, dst)
            logging.info("[修改文件] %s -> %s", event.src_path, dst)
        except Exception as e:
            logging.error("修改同步失败: %s", e)

    def on_deleted(self, event):
        """文件或目录被删除时触发。"""
        if self._should_skip(event.src_path, event.is_directory):
            return
        dst = self._dst_path(event.src_path)
        try:
            if event.is_directory:
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
        src_dst = self._dst_path(event.src_path)
        dest_dst = self._dst_path(event.dest_path)

        # 如果移动目标超出深度限制，仅执行删除操作
        if exceeds_depth(event.dest_path, self.src, self.depth):
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
        except Exception as e:
            logging.error("移动同步失败: %s", e)


class SingleFileHandler(FileSystemEventHandler):
    """监听单个文件的变化，将其同步到目标目录。"""
    def __init__(self, src_file: str, dst_dir: str):
        self.src_file = os.path.normpath(src_file)
        self.dst_dir = dst_dir
        self.dst_file = os.path.join(dst_dir, os.path.basename(src_file))

    def _is_target(self, path: str) -> bool:
        return os.path.normpath(path) == self.src_file

    def on_modified(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        try:
            shutil.copy2(self.src_file, self.dst_file)
            logging.info("[修改文件] %s -> %s", self.src_file, self.dst_file)
        except Exception as e:
            logging.error("修改同步失败: %s", e)

    def on_created(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        try:
            shutil.copy2(self.src_file, self.dst_file)
            logging.info("[创建文件] %s -> %s", self.src_file, self.dst_file)
        except Exception as e:
            logging.error("创建同步失败: %s", e)

    def on_deleted(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        try:
            if os.path.exists(self.dst_file):
                os.remove(self.dst_file)
                logging.info("[删除文件] %s", self.dst_file)
        except Exception as e:
            logging.error("删除同步失败: %s", e)


# --------------- 主程序 ---------------

def main():
    log_level = os.getenv("SYNC_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    pairs = load_sync_pairs()
    if not pairs:
        logging.error("未在 .env 中找到任何同步对，程序退出。")
        sys.exit(1)

    observer = Observer()

    for pair in pairs:
        is_file_source = os.path.isfile(pair.source)

        if is_file_source:
            # 源是单个文件：直接复制到目标目录
            os.makedirs(pair.dest, exist_ok=True)
            dst_file = os.path.join(pair.dest, os.path.basename(pair.source))
            logging.info("同步文件: %s -> %s", pair.source, dst_file)
            logging.info("正在执行初始同步...")
            if not os.path.exists(dst_file) or os.stat(pair.source).st_mtime > os.stat(dst_file).st_mtime:
                shutil.copy2(pair.source, dst_file)
                logging.debug("已复制: %s -> %s", pair.source, dst_file)
        elif os.path.isdir(pair.source):
            os.makedirs(pair.dest, exist_ok=True)
            depth_desc = "无限制" if pair.depth == 0 else str(pair.depth)
            logging.info("同步对: %s -> %s [深度=%s, 忽略=%s]",
                         pair.source, pair.dest, depth_desc, pair.ignore)
            logging.info("正在执行初始全量同步...")
            full_sync(pair)
        else:
            logging.error("源路径不存在: %s，已跳过", pair.source)
            continue

    for pair in pairs:
        is_file_source = os.path.isfile(pair.source)
        if is_file_source:
            # 监听文件所在的目录，通过 handler 过滤只关注该文件
            watch_dir = os.path.dirname(pair.source)
            handler = SingleFileHandler(pair.source, pair.dest)
            observer.schedule(handler, watch_dir, recursive=False)
        else:
            handler = SyncHandler(pair)
            observer.schedule(handler, pair.source, recursive=True)

    observer.start()
    logging.info("正在监听 %d 个同步对，按 Ctrl+C 停止。", len(pairs))

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("正在停止...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
