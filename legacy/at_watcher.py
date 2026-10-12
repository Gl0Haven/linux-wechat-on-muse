#!/usr/bin/env python3
"""
@ 热队列写入器：每 10 秒扫 DB，发现 @GloHaven/@muse 就写入热队列。
不发送任何回复，只负责快速发现。真正的回复由 30 秒的 atflash cron 完成。
"""
# ⚠️ 已退役（2026-10-06 D方案）：直读微信源 DB，违反"唯一 reader"原则。
# 生产流程不再调用此文件，保留仅供手动调试。
import json
import subprocess
import sys
import time
from pathlib import Path

BRIDGE = Path.home() / "workspace" / "wechat-bridge"
HOT_QUEUE = BRIDGE / "state" / "at_hot.jsonl"

AT_PATTERNS = ["@glohaven", "@muse"]

def is_at(content):
    return any(p in content.lower() for p in AT_PATTERNS)

def poll_once():
    result = subprocess.run(
        ["python3", str(BRIDGE / "db_poller.py"), "--json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return
    try:
        msgs = json.loads(result.stdout) if result.stdout.strip() else []
    except Exception:
        return
    # 去重：只写入热队列里没有的
    existing = set()
    if HOT_QUEUE.exists():
        for line in HOT_QUEUE.read_text().splitlines():
            try:
                m = json.loads(line)
                existing.add((m.get("time"), m.get("sender"), m.get("content")))
            except Exception:
                pass
    HOT_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with open(HOT_QUEUE, "a") as f:
        for m in msgs:
            key = (m.get("time"), m.get("sender"), m.get("content"))
            if key in existing:
                continue
            if is_at(m.get("content", "")):
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
                print(f"HOT: {m.get('sender')}: {m.get('content','')[:40]}", flush=True)

def main():
    print("at_watcher started, polling every 10s", flush=True)
    while True:
        try:
            poll_once()
        except Exception as e:
            print(f"ERR: {e}", flush=True)
        time.sleep(10)

if __name__ == "__main__":
    main()
