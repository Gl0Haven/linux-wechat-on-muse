#!/usr/bin/env python3
"""常驻回复 worker 轮询脚本（供 subagent 驱动）:
- 每轮: 阻塞式领取 queue.db 的新 @ 和单聊 (最多 ~10s) + 消费 at_queue.jsonl
- 发现新消息打印 NEW_AT <json> 供上层 agent 生成正文并发送
- 循环 500 次后退出; 每 50 轮打印心跳
注意: queue.py 已改名为 msgqueue.py (2026-10-06, 修复遮蔽 stdlib queue 导致 db_poller 崩溃)
"""
import json
import subprocess
import sys
import time

BRIDGE = "~/workspace/wechat-bridge"
sys.path.insert(0, BRIDGE)
from msgqueue import get_db

ROUNDS = 500
INTERVAL = 10
WORKER_ID = "worker-subagent"


def claim_any(db, worker_id, lease_seconds=600):
    """领取新 @ (is_at=1) 或待处理单聊 (is_dm=1), 按 msg_time 最早优先"""
    now = int(time.time())
    lease_until = now + lease_seconds
    cur = db.execute("""
        UPDATE messages
        SET status='GENERATING', lease_until=?, lease_owner=?
        WHERE id = (
            SELECT id FROM messages
            WHERE (is_at=1 OR is_dm=1)
              AND status IN ('NEW', 'GENERATING', 'RETRY')
              AND (status != 'GENERATING' OR lease_until < ?)
            ORDER BY msg_time ASC
            LIMIT 1
        )
        RETURNING id, chat, chat_name, sender, sender_name, content, msg_time, is_at, is_dm
    """, (lease_until, worker_id, now))
    row = cur.fetchone()
    db.commit()
    return row


def pull_file_queue():
    try:
        r = subprocess.run(
            [sys.executable, BRIDGE + "/worker_queue.py", "pull"],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return []
        return json.loads(r.stdout) if r.stdout.strip() else []
    except Exception:
        return []


def main():
    print(f"WORKER_START rounds={ROUNDS} interval={INTERVAL}s", flush=True)
    db = get_db()
    for i in range(1, ROUNDS + 1):
        try:
            row = None
            start = time.time()
            while time.time() - start < INTERVAL:
                row = claim_any(db, WORKER_ID)
                if row:
                    break
                time.sleep(2)
            if row:
                print("NEW_AT " + json.dumps({
                    "src": "db", "id": row[0], "chat": row[1],
                    "chat_name": row[2], "sender": row[3],
                    "sender_name": row[4], "content": row[5],
                    "msg_time": row[6], "is_at": row[7], "is_dm": row[8],
                }, ensure_ascii=False), flush=True)
        except Exception as e:
            print(f"DB_POLL_ERROR: {e}", flush=True)
        try:
            for m in pull_file_queue():
                m2 = dict(m)
                m2["src"] = "file"
                print("NEW_AT " + json.dumps(m2, ensure_ascii=False), flush=True)
        except Exception as e:
            print(f"FILE_POLL_ERROR: {e}", flush=True)
        if i % 50 == 0:
            print(f"HEARTBEAT round={i}", flush=True)
    print("WORKER_DONE 500/500", flush=True)


if __name__ == "__main__":
    main()
