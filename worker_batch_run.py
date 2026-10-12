#!/usr/bin/env python3
"""Worker batch driver, step 1: claim batch + dump JSON for LLM judgment.

Writes /tmp/worker_batch.json with:
  {"worker_id": ..., "batch": {"explicit": [...], "sessions": {...}},
   "research_done": [...], "research_pending": [...]}
"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msgqueue import get_db, is_muted
import blocking_worker
from blocking_worker import process_batch

db = get_db()
muted, _ = is_muted(db)
if muted:
    print(json.dumps({"muted": True}))
    sys.exit(3)

batch = process_batch(db, max_batch=10)

# research 检查
research_done, research_pending = [], []
try:
    from research_helper import get_done_jobs
    research_done = [dict(zip(
        ["id","msg_id","chat_id","asker_wxid","question","result_json",
         "status","sender_name","content"], r)) for r in get_done_jobs(db)]
    research_pending = db.execute(
        "SELECT id, msg_id, question, created_at FROM research_jobs "
        "WHERE status='PENDING' ORDER BY created_at ASC").fetchall()
    research_pending = [dict(zip(["id","msg_id","question","created_at"], r))
                        for r in research_pending]
except Exception as e:
    print(f"[driver] research check error: {e}", flush=True)

out = {
    "worker_id": blocking_worker.WORKER_ID,
    "muted": False,
    "explicit": batch["explicit"],
    "sessions": batch["sessions"],
    "research_done": research_done,
    "research_pending": research_pending,
}
with open("/tmp/worker_batch.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)
print(f"[driver] claimed explicit={len(batch['explicit'])} "
      f"sessions={len(batch['sessions'])} "
      f"research_done={len(research_done)} research_pending={len(research_pending)}",
      flush=True)
