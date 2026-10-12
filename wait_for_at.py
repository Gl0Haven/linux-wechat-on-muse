#!/usr/bin/env python3
"""
阻塞式等待 - 队列里出现新 @ 才返回

用法: python3 wait_for_at.py [timeout_seconds] [worker_id]
返回: 有新消息时打印 JSON 并退出 0，超时退出 1
"""

import sys
import time
import json
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db, claim_next_reply

def wait_for_at(timeout=110, worker_id=None):
    """
    阻塞等待新 @ 消息（只做门铃，不 claim）。
    每 2 秒检查一次，最长等 timeout 秒。
    返回 True（有新 NEW 消息）或 False（超时）。
    实际 claim 由调用方的 process_one() 完成，避免 owner 不一致。
    """
    # worker_id 参数保留兼容，但不再使用（不 claim）
    db = get_db()
    start = time.time()
    while time.time() - start < timeout:
        row = db.execute("""
            SELECT id FROM messages
            WHERE status='NEW' AND should_reply IN (1,2)
            LIMIT 1
        """).fetchone()
        if row:
            return True
        time.sleep(2)
    return False

if __name__ == "__main__":
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 110
    # 第 2 个参数保留兼容（旧调用传 worker_id），现在忽略
    has_new = wait_for_at(timeout)
    if has_new:
        print("NEW_MESSAGE")
        sys.exit(0)
    else:
        sys.exit(1)
