#!/usr/bin/env python3
"""GUI 独占锁（2026-10-11 R2 TOCTOU 修复）。

保证 verify_chat → 输入框聚焦 → 粘贴 → 发送 处于同一独占区间，
防止其他 GUI 任务（截图等）在检查后、发送前切换会话。

用法：
    from gui_lock import gui_locked
    with gui_locked(timeout=60):
        ... GUI 操作 ...
"""
import fcntl
import os
import time

HOME = os.path.expanduser("~")
LOCK_FILE = os.path.join(HOME, "workspace/wechat-bridge/state/gui.lock")


class gui_locked:
    def __init__(self, timeout=60):
        self.timeout = timeout
        self.fp = None

    def __enter__(self):
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        self.fp = open(LOCK_FILE, "w")
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fp.write(str(os.getpid()))
                self.fp.flush()
                return self
            except (OSError, IOError):
                if time.time() >= deadline:
                    self.fp.close()
                    raise TimeoutError(f"GUI 锁等待超时 ({self.timeout}s)")
                time.sleep(0.1)

    def __exit__(self, *args):
        try:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            self.fp.close()
        except Exception:
            pass
