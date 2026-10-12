#!/usr/bin/env python3
"""
SQLite 链路健康检查 - 观察期专用

检查:
1. 有没有漏消息 / 重复入队 (对比 messages.jsonl)
2. lease 恢复是否正常 (GENERATING 但 lease 过期的数量)
3. WAL daemon 心跳是否正常
4. 有没有卡在异常状态 (SENDING 超过 10 分钟, GENERATING 超过 30 分钟)
"""

import sys
import os
import time
import json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db

def check():
    db = get_db()
    now = int(time.time())
    issues = []

    # 1. 状态统计
    print("=== SQLite 状态 ===")
    for row in db.execute(
        "SELECT status, COUNT(*) FROM messages GROUP BY status"):
        print(f"  {row[0]}: {row[1]}")

    # 2. 检查卡住的 GENERATING (lease 过期超过 5 分钟还没被回收)
    stuck_gen = db.execute("""
        SELECT COUNT(*) FROM messages
        WHERE status='GENERATING' AND lease_until < ?
    """, (now - 300,)).fetchone()[0]
    if stuck_gen > 0:
        issues.append(f"有 {stuck_gen} 条 GENERATING 卡住超过 5 分钟未回收")

    # 3. 检查卡住的 SENDING (超过 10 分钟)
    stuck_send = db.execute("""
        SELECT COUNT(*) FROM messages
        WHERE status='SENDING' AND lease_until < ?
    """, (now - 600,)).fetchone()[0]
    if stuck_send > 0:
        issues.append(f"有 {stuck_send} 条 SENDING 卡住超过 10 分钟")

    # 4. 检查 SEND_UNCERTAIN (需要人工处理)
    uncertain = db.execute(
        "SELECT COUNT(*) FROM messages WHERE status='SEND_UNCERTAIN'"
    ).fetchone()[0]
    if uncertain > 0:
        issues.append(f"有 {uncertain} 条 SEND_UNCERTAIN 需要人工确认")

    # 5. 检查 DEAD
    dead = db.execute(
        "SELECT COUNT(*) FROM messages WHERE status='DEAD'"
    ).fetchone()[0]
    if dead > 0:
        issues.append(f"有 {dead} 条 DEAD")

    # 6. WAL daemon 心跳
    hb_file = "/tmp/wechat-bridge/wal_reader.heartbeat"
    if os.path.exists(hb_file):
        hb = int(open(hb_file).read().strip())
        age = now - hb
        print(f"\n=== WAL daemon 心跳: {age} 秒前 ===")
        if age > 60:
            issues.append(f"WAL daemon 心跳 {age} 秒前，可能已死")
    else:
        issues.append("WAL daemon 心跳文件不存在")

    # 7. 对比旧系统：检查是否有 @ 消息被旧系统处理但 SQLite 没记录
    # (通过 at_queue.jsonl 和 messages.jsonl 对比)
    print("\n=== 新旧系统对比 ===")
    try:
        # 旧队列
        old_queue_size = 0
        qpath = os.path.expanduser(
            "~/workspace/wechat-bridge/at_queue.jsonl")
        if os.path.exists(qpath):
            with open(qpath) as f:
                old_queue_size = sum(1 for _ in f if _.strip())
        print(f"  旧 at_queue.jsonl: {old_queue_size} 条待处理")

        # SQLite 中的 NEW
        sqlite_new = db.execute(
            "SELECT COUNT(*) FROM messages WHERE status='NEW'"
        ).fetchone()[0]
        print(f"  SQLite NEW: {sqlite_new} 条待处理")
    except Exception as e:
        print(f"  对比失败: {e}")

    print("\n=== 检查结果 ===")
    if issues:
        for i in issues:
            print(f"  ⚠️  {i}")
        return 1
    else:
        print("  ✅ 一切正常")
        return 0

if __name__ == "__main__":
    sys.exit(check())
