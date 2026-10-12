#!/usr/bin/env python3
"""常驻 LLM worker 轮询脚本：每 10 秒拉一次 at_queue，共 500 轮。
发现新 @ 时打印 NEW_AT <json> 供上层 agent 生成回复并发送。
无新消息时静默；每 50 轮打印一次心跳。"""
import json, subprocess, sys, time
from pathlib import Path

BRIDGE = Path.home() / "workspace" / "wechat-bridge"
ROUNDS = 500
INTERVAL = 10

def pull():
    try:
        r = subprocess.run(
            ["python3", str(BRIDGE / "worker_queue.py"), "pull"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return []
        return json.loads(r.stdout) if r.stdout.strip() else []
    except Exception as e:
        print(f"PULL_ERROR: {e}", flush=True)
        return []

def main():
    print(f"WORKER_START rounds={ROUNDS} interval={INTERVAL}s", flush=True)
    for i in range(1, ROUNDS + 1):
        items = pull()
        for m in items:
            print("NEW_AT " + json.dumps(m, ensure_ascii=False), flush=True)
        if i % 50 == 0:
            print(f"HEARTBEAT round={i}", flush=True)
        time.sleep(INTERVAL)
    print("WORKER_DONE 500/500", flush=True)

if __name__ == "__main__":
    main()
