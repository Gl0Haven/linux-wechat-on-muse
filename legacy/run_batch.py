#!/usr/bin/env python3
"""单轮 worker 批处理驱动：claim → JSON 打印，供外层 LLM 生成后 commit。"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blocking_worker
from blocking_worker import process_batch, WORKER_ID
from msgqueue import get_db, is_muted

db = get_db()

muted, until = is_muted(db)
if muted:
    print(json.dumps({"muted": True, "mute_until": until}))
    sys.exit(0)

batch = process_batch(db, max_batch=10)

def clean(m):
    m = dict(m)
    m.pop("session_ctx", None)  # 单独输出
    return m

out = {"muted": False, "worker_id": WORKER_ID,
       "explicit": [clean(m) for m in batch["explicit"]],
       "sessions": {}}
for chat, msgs in batch["sessions"].items():
    ctx = msgs[0].get("session_ctx", []) if msgs else []
    out["sessions"][chat] = {
        "msgs": [clean(m) for m in msgs],
        "session_ctx": ctx,
    }
print(json.dumps(out, ensure_ascii=False))
