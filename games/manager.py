#!/usr/bin/env python3
"""
微信群每日小游戏 - GameManager

职责：
- start_game：开局（建行 → plugin.start → GAME_START 合成消息 → 链路）
- handle_input：处理一条玩家输入（router 调用）
- finish_game_flow：结算（GAME_END 合成消息 + 积分落库）
- tick：定时驱动（提示/超时/过期/每日提议），由 game-tick cron 每 5 分钟调用
- 合成消息统一走 messages 表（should_reply=1），worker 认领后渲染，
  sendd 仍是唯一 GUI 发送者。

合成消息 sender = GAME_SYSTEM，worker 靠 message_game_link 找到 facts。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgqueue import enqueue, get_db, is_muted  # noqa: E402
from games import store as gstore  # noqa: E402
from games import get_plugin  # noqa: E402
from games.config import (  # noqa: E402
    GAME_SYSTEM_SENDER, GAME_SYSTEM_NAME, GAMES_ALLOWED_CHATS,
)

BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 合成消息优先级（EXPLICIT=100；游戏互动 80；tick 60；提议 40）
PRIORITY = {
    "GAME_REPLY": 80,
    "GAME_START": 70,
    "GAME_END": 70,
    "GAME_TICK": 60,
    "GAME_PROPOSE": 40,
}


def _chat_name(db, chat_id):
    row = db.execute(
        "SELECT chat_name FROM messages WHERE chat=? AND chat_name IS NOT NULL"
        " ORDER BY msg_time DESC LIMIT 1", (chat_id,)).fetchone()
    return row[0] if row and row[0] else chat_id


def enqueue_game_message(db, chat_id, game_id, event_id, kind,
                         commit=True):
    """写一条合成游戏消息，返回 messages.id。"""
    now = int(time.time())
    content = f"[GAME:{kind}] #{event_id}"
    mid = enqueue(db, chat_id, _chat_name(db, chat_id),
                  GAME_SYSTEM_SENDER, GAME_SYSTEM_NAME,
                  content, now, is_at=False, is_dm=False,
                  should_reply=1, direction="INBOUND", commit=False)
    if not mid:
        return None
    db.execute("UPDATE messages SET action=?, priority=? WHERE id=?",
               (kind, PRIORITY.get(kind, 50), mid))
    gstore.link_message(db, mid, game_id, event_id, kind, commit=False)
    if commit:
        db.commit()
    return mid


def _apply_rewards(db, game_id, points, wins):
    for user_id, name, pts in (points or []):
        if pts:
            gstore.add_points(db, game_id, user_id, name, pts,
                              commit=False)
            # 本局记分板也同步
            db.execute(
                "UPDATE game_players SET score=score+? "
                "WHERE game_id=? AND user_id=?", (pts, game_id, user_id))
    for user_id, name in (wins or []):
        gstore.add_win(db, game_id, user_id, name, commit=False)


def start_game(db, chat_id, game_type, chat_name="", config=None):
    """开一局。config: 可选，如 {"category": "成语"}（猜词按范围选词）。
    返回 (game_id, synthetic_msg_id)；失败返回 (None, error)。"""
    from games.scheduler import today_str
    if chat_id not in GAMES_ALLOWED_CHATS:
        return None, "chat not allowed"
    if gstore.get_active_game(db, chat_id):
        return None, "already has active game"
    plugin = get_plugin(game_type)
    if not plugin:
        return None, f"unknown game {game_type}"

    now = int(time.time())
    game_id = gstore.create_game(db, chat_id, game_type,
                                 duration_sec=plugin.duration_sec,
                                 config=config, commit=False)
    ctx = {"db": db, "game_id": game_id, "chat_id": chat_id,
           "chat_name": chat_name or _chat_name(db, chat_id), "now": now,
           "config": config or {}}
    try:
        state, facts = plugin.start(ctx)
    except Exception as e:
        gstore.finish_game(db, game_id, "CANCELLED",
                           {"error": str(e)}, commit=False)
        db.commit()
        return None, f"start failed: {e}"
    if state is None:
        # 开局条件不足（如人不够）
        gstore.finish_game(db, game_id, "CANCELLED", facts, commit=False)
        db.commit()
        return None, f"start declined: {facts}"

    gstore.save_game_state(db, game_id, state, commit=False)
    # 玩家占位：猜词/探险按行动加入；NG 词在 start 里已加
    eid = gstore.add_event(db, game_id, "GAME_START", facts, commit=False)
    mid = enqueue_game_message(db, chat_id, game_id, eid, "GAME_START",
                               commit=False)
    db.execute("UPDATE games SET host_message_id=? WHERE id=?",
               (mid or 0, game_id))
    gstore.mark_daily_started(db, chat_id, today_str(now), game_type,
                              commit=False)
    db.commit()
    return game_id, mid


def handle_input(db, game, msg):
    """处理一条玩家输入。返回 (result, event_id, synthetic_msg_id|None)。"""
    plugin = get_plugin(game["game_type"])
    now = int(time.time())
    ctx = {"db": db, "game_id": game["id"], "chat_id": game["chat_id"],
           "now": now}
    result = plugin.handle_message(msg, game, ctx)
    if result.get("new_state") is not None:
        gstore.save_game_state(db, game["id"], result["new_state"],
                               commit=False)
    _apply_rewards(db, game["id"], result.get("points"),
                   result.get("wins"))

    eid = gstore.add_event(db, game["id"], result["event_type"],
                           result.get("facts", {}),
                           msg_id=msg.get("msg_id", 0),
                           user_id=msg.get("sender", ""), commit=False)
    # 原消息链上（审计用）
    if msg.get("msg_id"):
        gstore.link_message(db, msg["msg_id"], game["id"], eid,
                            "GAME_INPUT", commit=False)

    synth_mid = None
    if result.get("reply_needed"):
        synth_mid = enqueue_game_message(db, game["chat_id"], game["id"],
                                         eid, "GAME_REPLY", commit=False)
    if result.get("finish_now"):
        # 先结算，GAME_END 排在 REPLY 后面
        finish_game_flow(db, gstore.get_game(db, game["id"]),
                         _finish_reason(result), commit=False)
    db.commit()
    return result, eid, synth_mid


def handle_invalid_input(db, game, msg):
    """无效游戏输入：提示有效动作。不改状态、不扣步数、不加分。
    返回 (result, event_id, synthetic_msg_id|None)；
    无需提示时返回 (None, None, None)。"""
    plugin = get_plugin(game["game_type"])
    now = int(time.time())
    ctx = {"db": db, "game_id": game["id"], "chat_id": game["chat_id"],
           "now": now}
    result = plugin.invalid_action_hint(msg, game, ctx)
    if not result:
        return None, None, None

    eid = gstore.add_event(db, game["id"], result["event_type"],
                           result.get("facts", {}),
                           msg_id=msg.get("msg_id", 0),
                           user_id=msg.get("sender", ""), commit=False)
    # 原消息链上（审计用）
    if msg.get("msg_id"):
        gstore.link_message(db, msg["msg_id"], game["id"], eid,
                            "GAME_INPUT", commit=False)

    synth_mid = None
    if result.get("reply_needed"):
        synth_mid = enqueue_game_message(db, game["chat_id"], game["id"],
                                         eid, "GAME_REPLY", commit=False)
    db.commit()
    return result, eid, synth_mid


def _finish_reason(result):
    et = result.get("event_type", "")
    if et == "WIN":
        return "win"
    if et == "HIT":
        return "last_survivor"
    return "all_done"


def finish_game_flow(db, game, reason, commit=True):
    """结算一局：facts + 积分 + GAME_END 合成消息。"""
    from games.scheduler import today_str
    plugin = get_plugin(game["game_type"])
    now = int(time.time())
    ctx = {"db": db, "game_id": game["id"], "chat_id": game["chat_id"],
           "now": now}
    out = plugin.finish(game, reason, ctx) or {}
    _apply_rewards(db, game["id"], out.get("points"), out.get("wins"))
    facts = dict(out.get("facts") or {})
    facts.setdefault("reason", reason)
    facts["game_name"] = plugin.name
    eid = gstore.add_event(db, game["id"], "GAME_END", facts, commit=False)
    mid = enqueue_game_message(db, game["chat_id"], game["id"], eid,
                               "GAME_END", commit=False)
    gstore.finish_game(db, game["id"], "FINISHED",
                       facts.get("winner"), commit=False)
    gstore.mark_daily_finished(db, game["chat_id"], today_str(now),
                               commit=False)
    if commit:
        db.commit()
    return mid


def tick(db, now=None):
    """定时驱动：过期回收 → ACTIVE 游戏 tick → 每日提议。
    返回 {"ticked": [...], "expired": [...], "proposed": [...]}。"""
    from games import scheduler as sched
    now = now or int(time.time())
    report = {"ticked": [], "expired": [], "proposed": []}

    # 1. ACTIVE 游戏 tick（提示 / 自然超时结算）。
    # 自然超时（ends_at 刚过）走 plugin.finish 发体面结束语；
    # 只有超过宽限期（VM 重置/长时间停机）才走 EXPIRED 回收。
    EXPIRE_GRACE_SEC = 30 * 60
    overdue_ids = []
    for game in gstore.list_active_games(db):
        if now >= game["ends_at"] + EXPIRE_GRACE_SEC:
            overdue_ids.append(game["id"])
            continue
        plugin = get_plugin(game["game_type"])
        if not plugin:
            overdue_ids.append(game["id"])
            continue
        ctx = {"db": db, "game_id": game["id"],
               "chat_id": game["chat_id"], "now": now}
        try:
            out = plugin.tick(game, ctx)
        except Exception as e:
            print(f"[game tick] {game['game_type']}#{game['id']} error: {e}",
                  flush=True)
            continue
        if not out:
            continue
        if out.get("new_state") is not None:
            gstore.save_game_state(db, game["id"], out["new_state"],
                                   commit=False)
        _apply_rewards(db, game["id"], out.get("points"), out.get("wins"))
        if out["kind"] == "END":
            finish_game_flow(db, gstore.get_game(db, game["id"]),
                             out.get("facts", {}).get("reason", "timeout"),
                             commit=False)
            report["ticked"].append((game["id"], "END"))
        else:
            eid = gstore.add_event(db, game["id"], "GAME_TICK",
                                   out.get("facts", {}), commit=False)
            enqueue_game_message(db, game["chat_id"], game["id"], eid,
                                 "GAME_TICK", commit=False)
            report["ticked"].append((game["id"], "TICK"))
        db.commit()

    # 2. 过期回收（超过宽限期的 ACTIVE 游戏）
    for gid in overdue_ids:
        game = gstore.get_game(db, gid)
        if not game or game["status"] != "ACTIVE":
            continue
        gstore.finish_game(db, gid, "EXPIRED", commit=False)
        # 有过玩家参与才发一条结束语，否则静默过期
        n = db.execute(
            "SELECT COUNT(*) FROM game_events WHERE game_id=? "
            "AND user_id != ''", (gid,)).fetchone()[0]
        if n:
            finish_game_flow(db, game, "expired", commit=False)
        report["expired"].append(gid)
    db.commit()

    # 3. 每日提议（每个允许的群）
    muted, _ = is_muted(db, now)
    if not muted:
        for chat_id in GAMES_ALLOWED_CHATS:
            cand = sched.check_daily_propose(db, chat_id, BRIDGE_DIR, now)
            if not cand:
                continue
            facts = {"candidate_game": cand["game_type"],
                     "candidate_name": cand["game_name"],
                     "reason": cand["reason"]}
            eid = gstore.add_event(db, 0, "GAME_PROPOSE", facts,
                                   commit=False)
            mid = enqueue_game_message(db, chat_id, 0, eid,
                                       "GAME_PROPOSE", commit=False)
            # 记录提议时间（冷却用）；vibe SKIP 后再记 declined
            db.execute(
                """INSERT INTO daily_game_state (chat_id, date, last_propose_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id, date) DO UPDATE SET
                     last_propose_at=excluded.last_propose_at""",
                (chat_id, sched.today_str(now), now))
            db.commit()
            report["proposed"].append((chat_id, cand["game_type"], mid))

    return report


# ---------- CLI ----------

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="微信群小游戏管理")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("tick", help="定时驱动（提示/超时/提议）")
    p = sub.add_parser("start", help="手动开一局")
    p.add_argument("--chat", required=True)
    p.add_argument("--game", required=True,
                   choices=["guess_word", "ng_word", "adventure",
                            "number_bomb"])
    p.add_argument("--category", default="",
                   help="猜词按范围选词，如 成语/电脑/生活")
    p = sub.add_parser("decline", help="vibe check 拒绝今日提议")
    p.add_argument("--chat", required=True)
    p = sub.add_parser("cancel", help="取消当前进行中的游戏")
    p.add_argument("--chat", required=True)
    p = sub.add_parser("status", help="查看游戏状态")
    p.add_argument("--chat", required=True)
    sub.add_parser("schema", help="建表（幂等）")

    args = ap.parse_args(argv)
    db = get_db()
    gstore.ensure_schema(db)

    if args.cmd == "schema":
        print("game schema ok")
    elif args.cmd == "tick":
        import json as _j
        print(_j.dumps(tick(db), ensure_ascii=False))
    elif args.cmd == "start":
        cfg = {"category": args.category} if args.category else None
        gid, mid = start_game(db, args.chat, args.game, config=cfg)
        print(f"game_id={gid} msg_id={mid}")
    elif args.cmd == "decline":
        from games.scheduler import today_str, PROPOSE_COOLDOWN_SEC
        gstore.mark_daily_declined(db, args.chat, today_str(), commit=True)
        print(f"declined, cooldown {PROPOSE_COOLDOWN_SEC}s")
    elif args.cmd == "cancel":
        game = gstore.get_active_game(db, args.chat)
        if game:
            finish_game_flow(db, game, "cancelled")
            print(f"cancelled game #{game['id']}")
        else:
            print("no active game")
    elif args.cmd == "status":
        import json as _j
        game = gstore.get_active_game(db, args.chat)
        print(_j.dumps(game, ensure_ascii=False, indent=1) if game
              else "no active game")
    else:
        ap.print_help()


if __name__ == "__main__":
    main(sys.argv[1:])
