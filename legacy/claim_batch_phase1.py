#!/usr/bin/env python3
"""Phase 1: claim a batch and dump it as JSON for the LLM to decide."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db, is_muted
import blocking_worker
from blocking_worker import process_batch

db = get_db()
muted, mute_until = is_muted(db)
if muted:
    print(json.dumps({"muted": True}))
    sys.exit(0)

batch = process_batch(db, max_batch=10)

def slim(m):
    return {
        "id": m.get("id"),
        "chat": m.get("chat"),
        "chat_name": m.get("chat_name"),
        "sender": m.get("sender"),
        "sender_name": m.get("sender_name"),
        "content": m.get("content"),
        "msg_time": m.get("msg_time"),
        "should_reply": m.get("should_reply"),
        "claim_version": m.get("claim_version"),
        "worker_id": m.get("worker_id"),
        "game": m.get("game"),
        "recent_5min": m.get("recent_5min"),
        "sender_recent": m.get("sender_recent"),
        "session_ctx": m.get("session_ctx"),
        "session_ids": m.get("session_ids"),
        "research": m.get("research"),
    }

out = {
    "muted": False,
    "explicit": [slim(m) for m in batch["explicit"]],
    "sessions": {chat: [slim(m) for m in msgs]
                 for chat, msgs in batch["sessions"].items()},
}
with open(os.path.expanduser("~/workspace/wechat-bridge/state/batch_current.json"), "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False)[:4000])
print(f"\n[summary] explicit={len(out['explicit'])} sessions={ {k: len(v) for k, v in out['sessions'].items()} }")
