#!/usr/bin/env python3
"""常驻回复 worker 轮询器：每 10 秒检查 at_queue.jsonl，消费新条目并打印。
由上层 agent 负责生成正文并调用 send-msg.sh 发送。循环 500 次后退出。"""
import json
import time
import fcntl
from pathlib import Path

BRIDGE = Path.home() / "workspace" / "wechat-bridge"
QUEUE = BRIDGE / "at_queue.jsonl"
QUEUE.touch(exist_ok=True)

def drain():
    entries = []
    with open(QUEUE, "r+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
            f.seek(0)
            f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return entries

for i in range(500):
    try:
        for e in drain():
            print("NEW_AT_JSON:" + json.dumps(e, ensure_ascii=False), flush=True)
    except Exception as ex:
        print(f"POLL_ERROR: {ex}", flush=True)
    time.sleep(10)

print("WORKER_LOOP_DONE", flush=True)
