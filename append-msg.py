#!/usr/bin/env python3
"""追加一条消息到 messages.jsonl（自动处理转义和去重）。
用法: append-msg.py <time> <chat> <sender> <content>
去重键: time + chat + sender + content 完全相同则跳过。
"""
import json
import sys
from pathlib import Path

LOG = Path("~/workspace/wechat-bridge/messages.jsonl")

def main():
    if len(sys.argv) != 5:
        print("usage: append-msg.py <time> <chat> <sender> <content>", file=sys.stderr)
        sys.exit(1)
    time, chat, sender, content = sys.argv[1:5]
    entry = {"time": time, "chat": chat, "sender": sender,
             "content": content, "replied": False}
    key = (time, chat, sender, content)
    if LOG.exists():
        with open(LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (e.get("time"), e.get("chat"), e.get("sender"), e.get("content")) == key:
                    print("duplicate, skipped")
                    return
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print("appended")

if __name__ == "__main__":
    main()
