#!/usr/bin/env python3
"""event worker 跨进程提交助手：读取 lease_owner → 设置 WORKER_ID → commit。

用法：
  python3 event_commit.py <msg_id> reply <reply_file>
  python3 event_commit.py <msg_id> skip "<reason>"
跨进程提交是唯一安全做法（blocking_worker.WORKER_ID 每进程随机）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import blocking_worker
from msgqueue import get_db

def main():
    msg_id = int(sys.argv[1])
    mode = sys.argv[2]
    db = get_db()
    row = db.execute(
        "SELECT lease_owner FROM messages WHERE id=?", (msg_id,)
    ).fetchone()
    if not row:
        print(f"FAIL: msg #{msg_id} 不存在")
        return 1
    # 跨进程提交：把 WORKER_ID 设成 claim 时的 owner（见 AGENTS.md 教训）
    blocking_worker.WORKER_ID = row[0]
    if mode == "reply":
        text = open(sys.argv[3], encoding="utf-8").read()
        ok = blocking_worker.commit_reply(db, msg_id, text)
    elif mode == "skip":
        ok = blocking_worker.commit_skip(db, msg_id, sys.argv[3])
    else:
        print(f"FAIL: 未知 mode {mode}")
        return 1
    print("OK" if ok else "FAIL: lease 丢失")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
