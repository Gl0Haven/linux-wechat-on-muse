#!/usr/bin/env python3
"""worker 队列消费辅助：去重指纹 + CAS 安全移除，避免双看门狗重复入队导致重复回复。"""
import json, os, sys
from pathlib import Path

BRIDGE = Path.home() / "workspace" / "wechat-bridge"
QUEUE = BRIDGE / "at_queue.jsonl"
SEEN = BRIDGE / "state" / "llm_worker_seen.json"

def fp(m):
    return f"{m.get('time')}|{m.get('chat')}|{m.get('sender')}|{m.get('content')}"

def load_seen():
    if SEEN.exists():
        try:
            return set(json.loads(SEEN.read_text()))
        except Exception:
            return set()
    return set()

def save_seen(s):
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(sorted(s)[-2000:], ensure_ascii=False))

def read_lines():
    if not QUEUE.exists():
        return []
    out = []
    with open(QUEUE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out

def cmd_pull():
    """输出未处理过的队列项（JSON 数组），顺手把已见过的行从队列 CAS 移除。"""
    seen = load_seen()
    # CAS: 只有文件在读取期间没被追加过才重写
    size1 = QUEUE.stat().st_size if QUEUE.exists() else 0
    lines = read_lines()
    fresh = [m for m in lines if fp(m) not in seen]
    if not fresh and lines:
        size2 = QUEUE.stat().st_size if QUEUE.exists() else 0
        if size1 == size2:
            QUEUE.write_text("")
    print(json.dumps(fresh, ensure_ascii=False))

def cmd_done():
    """用法: worker_queue.py done <fp1> [fp2 ...] — 记入 seen 并 CAS 从队列移除。"""
    fps = set(sys.argv[2:])
    seen = load_seen() | fps
    save_seen(seen)
    for _ in range(3):
        size1 = QUEUE.stat().st_size if QUEUE.exists() else 0
        lines = read_lines()
        keep = [m for m in lines if fp(m) not in seen]
        if len(keep) == len(lines):
            break  # 没啥可删的
        size2 = QUEUE.stat().st_size if QUEUE.exists() else 0
        if size1 == size2:
            QUEUE.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in keep))
            break

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "done":
        cmd_done()
    else:
        cmd_pull()
