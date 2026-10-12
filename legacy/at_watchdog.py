#!/usr/bin/env python3
"""
微信 @ 检测常驻守护进程：每 5 秒轮询一次，发现 @ 写入队列文件。
不发送任何消息，只负责检测。由 keepalive.sh 拉起。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

BRIDGE = Path.home() / "workspace" / "wechat-bridge"
QUEUE = BRIDGE / "at_queue.jsonl"
AT_PATTERNS = ["@glohaven", "@muse"]

def is_at_message(content):
    c = content.lower()
    return any(p in c for p in AT_PATTERNS)

def run_poller():
    result = subprocess.run(
        ["python3", str(BRIDGE / "db_poller.py"), "--json"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout) if result.stdout.strip() else []
    except:
        return []

def main():
    print("at_watchdog started, polling every 5s", flush=True)
    seen = set()  # 已入队的消息指纹，避免重复
    while True:
        try:
            msgs = run_poller()
            for m in msgs:
                # 跳过小号自己发的消息（_own 由 db_poller 按 source 字段可靠标记）
                if m.get("_own"):
                    continue
                if not is_at_message(m.get("content", "")):
                    continue
                # 指纹：时间+发送者+内容
                fp = f"{m.get('time')}|{m.get('sender')}|{m.get('content')}"
                if fp in seen:
                    continue
                seen.add(fp)
                # 写入队列
                with open(QUEUE, "a") as f:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
                print(f"QUEUED: {m.get('sender')}: {m.get('content', '')[:30]}", flush=True)
            # 控制 seen 集合大小，避免无限增长
            if len(seen) > 1000:
                seen.clear()
        except Exception as e:
            print(f"ERROR: {e}", flush=True)
        time.sleep(5)

if __name__ == "__main__":
    main()
