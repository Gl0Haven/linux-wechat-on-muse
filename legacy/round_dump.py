#!/usr/bin/env python3
"""Decision worker round: claim batch + dump context for LLM judgment.

Single process: mute check, research maintenance, process_batch,
game attach, auto-research trigger for explicit, then dump JSON.
The agent (LLM) reads the dump, decides replies, writes decisions;
a second script with the same WECHAT_WORKER_ID commits.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msgqueue import get_db, is_muted
import blocking_worker

OUT_DIR = os.path.expanduser(
    "~/workspace/goals/wechat-bridge-experiment/hidden_files")

def main():
    ts = time.strftime("%Y%m%d_%H%M%S")
    db = get_db()
    wid = blocking_worker.WORKER_ID

    muted, until = is_muted(db)
    if muted:
        print(json.dumps({"status": "MUTED", "until": until, "worker_id": wid}))
        return 0

    # ---- research maintenance ----
    research_info = {"done_jobs": [], "pending_jobs": []}
    try:
        from research_helper import (check_timeouts, reclaim_stuck_jobs,
                                     get_done_jobs)
        timeouts = check_timeouts(db)
        reclaimed = reclaim_stuck_jobs(db)
        for job in get_done_jobs(db):
            (jid, msg_id, chat_id, asker_wxid, question, result_json,
             status, sender_name, content) = job
            research_info["done_jobs"].append({
                "job_id": jid, "msg_id": msg_id, "chat_id": chat_id,
                "asker_wxid": asker_wxid, "question": question,
                "result_json": result_json, "status": status,
                "sender_name": sender_name, "content": content,
            })
        rows = db.execute(
            "SELECT id, msg_id, chat_id, question FROM research_jobs "
            "WHERE status='PENDING'").fetchall()
        for jid, msg_id, chat_id, question in rows:
            research_info["pending_jobs"].append(
                {"job_id": jid, "msg_id": msg_id, "chat_id": chat_id,
                 "question": question})
        research_info["timeouts"] = [r[0] for r in (timeouts or [])]
        research_info["reclaimed"] = list(reclaimed or [])
    except Exception as e:
        research_info["error"] = str(e)

    # ---- claim batch ----
    batch = blocking_worker.process_batch(db, max_batch=10)

    # ---- game attach + auto-research trigger for explicit ----
    try:
        from games import store as _gstore
        _gstore.ensure_schema(db)
    except Exception as e:
        print(f"[round] game schema skip: {e}", flush=True)

    for m in batch["explicit"]:
        try:
            from games import store as _gstore2
            _render = _gstore2.get_render(db, m["id"])
            if _render and (_render["link"]["kind"] or "").startswith("GAME_"):
                m["game"] = _render
        except Exception as e:
            print(f"[round] game attach skip: {e}", flush=True)
        # auto-research trigger (rule-based, no network)
        m["research_triggered"] = False
        m["research_job_id"] = None
        try:
            from research_helper import (classify_research_need,
                                         check_cache_first, check_guardrails,
                                         create_research_job)
            from behavior import can_trigger_research, REPLY, log_behavior
            need = classify_research_need(m["content"])
            if need == "RESEARCH" and can_trigger_research(REPLY):
                hit, _ = check_cache_first(db, m["content"])
                if not hit:
                    ok, _ = check_guardrails(db)
                    if ok:
                        jid = create_research_job(
                            db, m["id"], m["chat"], m["sender"], m["content"])
                        if jid:
                            m["research_triggered"] = True
                            m["research_job_id"] = jid
                            try:
                                log_behavior(db, m["chat"], REPLY,
                                             f"auto-research job #{jid}",
                                             m["id"])
                            except Exception:
                                pass
        except Exception as e:
            print(f"[round] auto-research skip: {e}", flush=True)

    for chat, msgs in batch["sessions"].items():
        for m in msgs:
            try:
                from games import store as _gstore3
                _render = _gstore3.get_render(db, m["id"])
                if _render and (_render["link"]["kind"] or "").startswith("GAME_"):
                    m["game"] = _render
            except Exception:
                pass

    # game/daily router states etc are not needed here; game facts come via render

    def clean(m):
        d = dict(m)
        for k in ("worker_id",):
            d.pop(k, None)
        return d

    dump = {
        "status": "OK",
        "worker_id": wid,
        "ts": ts,
        "explicit": [clean(m) for m in batch["explicit"]],
        "sessions": {c: [clean(m) for m in msgs]
                     for c, msgs in batch["sessions"].items()},
        "research": research_info,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"worker_round_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=1)
    n_e = len(dump["explicit"])
    n_s = sum(len(v) for v in dump["sessions"].values())
    print(json.dumps({"status": "OK", "worker_id": wid, "dump": path,
                      "explicit": n_e, "sessions": n_s,
                      "done_jobs": len(research_info["done_jobs"]),
                      "pending_jobs": len(research_info["pending_jobs"])}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
