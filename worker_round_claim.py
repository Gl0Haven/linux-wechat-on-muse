#!/usr/bin/env python3
"""单轮 worker：claim 批次并转储 JSON，供 LLM 裁决后再 commit。
claim_version/worker_id 随批次持久化，commit 脚本显式传入即可跨进程。
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msgqueue import get_db, is_muted
import blocking_worker
from blocking_worker import process_batch

OUT = os.path.expanduser("~/workspace/wechat-bridge/state/worker_batch.json")

def main():
    db = get_db()
    muted, until = is_muted(db)
    if muted:
        print(json.dumps({"muted": True, "until": until}))
        open(OUT, "w").write(json.dumps({"muted": True}))
        return
    batch = process_batch(db, max_batch=10)
    # 附加游戏 render
    try:
        from games import store as _gstore
        _gstore.ensure_schema(db)
        for m in batch["explicit"]:
            r = _gstore.get_render(db, m["id"])
            if r and (r.get("link", {}).get("kind") or "").startswith("GAME_"):
                m["game"] = r
        for chat, msgs in batch["sessions"].items():
            for m in msgs:
                r = _gstore.get_render(db, m["id"])
                if r and (r.get("link", {}).get("kind") or "").startswith("GAME_"):
                    m["game"] = r
    except Exception as e:
        print(f"[claim] game attach 跳过: {e}", file=sys.stderr)
    out = {
        "muted": False,
        "worker_id": blocking_worker.WORKER_ID,
        "claimed_at": int(time.time()),
        "explicit": batch["explicit"],
        "sessions": batch["sessions"],
    }
    open(OUT, "w").write(json.dumps(out, ensure_ascii=False, default=str))
    n_exp = len(batch["explicit"])
    n_ses = sum(len(v) for v in batch["sessions"].values())
    print(json.dumps({"muted": False, "explicit": n_exp,
                      "session_msgs": n_ses,
                      "session_chats": list(batch["sessions"].keys())}))

if __name__ == "__main__":
    main()
