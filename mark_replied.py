#!/usr/bin/env python3
"""把 messages.jsonl 中指定消息标为已回复（按 time+sender+content 匹配）。
用法: mark_replied.py '<json: {"time":..., "sender":..., "content":...}>'
打印标记条数。"""
import json
import sys
from pathlib import Path

BRIDGE = Path.home() / "workspace" / "wechat-bridge"
MESSAGES = BRIDGE / "messages.jsonl"


def main():
    target = json.loads(sys.argv[1])
    if not MESSAGES.exists():
        print(0)
        return
    out = []
    marked = 0
    with open(MESSAGES) as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                m = json.loads(raw)
            except Exception:
                out.append(raw)
                continue
            if (not m.get("replied")
                    and m.get("time") == target.get("time")
                    and m.get("sender") == target.get("sender")
                    and m.get("content") == target.get("content")):
                m["replied"] = True
                marked += 1
                raw = json.dumps(m, ensure_ascii=False)
            out.append(raw)
    with open(MESSAGES, "w") as f:
        f.write("\n".join(out) + "\n")
    print(marked)


if __name__ == "__main__":
    main()
