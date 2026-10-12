#!/usr/bin/env python3
"""LLM worker 的队列等待器：每 interval 秒检查 at_queue.jsonl 一次，
发现超出 offset 的新行就解析并输出，推进 offset，然后退出。
timeout 秒内无新消息则输出空结果退出。单次调用 = timeout/interval 轮。"""
import argparse
import json
import sys
import time
from pathlib import Path

BRIDGE = Path.home() / "workspace" / "wechat-bridge"
QUEUE = BRIDGE / "at_queue.jsonl"
STATE = BRIDGE / "state"
OFFSET_FILE = STATE / "llm_queue_offset.txt"


def read_offset():
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def write_offset(n):
    OFFSET_FILE.write_text(str(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=500)
    ap.add_argument("--interval", type=int, default=10)
    args = ap.parse_args()

    deadline = time.time() + args.timeout
    rounds = 0
    new_entries = []

    while time.time() < deadline:
        rounds += 1
        offset = read_offset()
        try:
            with open(QUEUE) as f:
                lines = [l.rstrip("\n") for l in f if l.strip()]
        except FileNotFoundError:
            lines = []

        # 文件被截断/轮转时重置
        if len(lines) < offset:
            offset = 0
            write_offset(0)

        if len(lines) > offset:
            for raw in lines[offset:]:
                try:
                    new_entries.append(json.loads(raw))
                except Exception:
                    pass  # 解析失败的行直接跳过
            write_offset(len(lines))
            break

        remaining = deadline - time.time()
        if remaining > 0:
            time.sleep(min(args.interval, remaining))

    print(json.dumps({"rounds": rounds, "new": new_entries}, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
