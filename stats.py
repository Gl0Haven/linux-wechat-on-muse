#!/usr/bin/env python3
"""群聊统计 / 水群排行（功能 D）。

触发方式（两种，互不干扰）：
1. 每周五 18:00（北京时间）自动播报：功能 A 的 `scheduled.tick(db, now)`
   每 5 分钟确定性调用 `check_weekly_stats(db, now)`，窗口内未推送过则入队一条
   WEEKLY_STATS 合成消息。不走 cron。
2. 按需命令：wal_reader_daemon 的旁路块调 `handle_stats_cmd(...)`，
   命中「水群排行/群统计/本周统计/上周统计」时入队 STATS_ONDEMAND（不压原消息）。

开关位置：STATS_CHATS 白名单。默认只开测试群；要给[主群]开，
把 "YOUR_MAIN_GROUP@chatroom" 加进 STATS_CHATS 即可（默认关闭）。

数字一律由 Python 确定性算好放 facts 里，worker 只做语言表现（点评），
不许 worker 编数字。

已知限制：
- @ 统计只计 @bot 的 is_at=1（wal_reader 判定 @bot 才标），@别人的不计。
- 图片统计依赖 content=="[图片]" 或 needs_media=1；部分老图片行可能只有截图。
- 「上周统计」目前和「本周统计」一样取最近 7 天（真实自然周切片待加）。
"""

import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import msgqueue

BJ = ZoneInfo("Asia/Shanghai")

# ---------------- 开关 ----------------
# 白名单群：只在这里启用。[主群]默认关闭；
# 要打开，把 "YOUR_MAIN_GROUP@chatroom" 加进来。
TEST_CHAT = "YOUR_TEST_GROUP@chatroom"          # [测试群]（开）
LAOBEIJING_CHAT = "YOUR_MAIN_GROUP@chatroom"    # [主群]（默认关闭）
STATS_CHATS = {TEST_CHAT, "YOUR_MAIN_GROUP@chatroom"}  # 2026-10-10 切入主群

# bot 自己的发送者（排除统计）：小号 wxid / 合成消息 / MUSE
BOT_WXID = "wxid_YOUR_BOT_ACCOUNT"
EXCLUDE_SENDERS = {BOT_WXID, "GAME_SYSTEM", "MUSE"}

# 每周播报窗口：北京时间周五 18:00–18:05（weekday()==4）
WEEKLY_WEEKDAY = 4
WEEKLY_START_MIN = 18 * 60
WEEKLY_END_MIN = 18 * 60 + 5


# ---------------- schema ----------------

def ensure_schema_stats(db):
    """once-per-week 守卫表。"""
    db.execute("""CREATE TABLE IF NOT EXISTS stats_push_log (
        push_type TEXT NOT NULL,     -- 'weekly' / 'ondemand'
        period TEXT NOT NULL,        -- ISO 周，如 2026-W41（北京时间）
        chat TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE (push_type, period, chat))""")
    db.commit()


def already_pushed(db, push_type, period, chat):
    row = db.execute(
        "SELECT 1 FROM stats_push_log WHERE push_type=? AND period=? AND chat=?",
        (push_type, period, chat)).fetchone()
    return row is not None


# ---------------- 时间窗口 ----------------

def _bj(dt_ts=None):
    return datetime.fromtimestamp(dt_ts if dt_ts is not None else time.time(),
                                  tz=BJ)


def in_weekly_window(now=None):
    """是否在播报窗口内：北京时间周五 18:00–18:05。"""
    b = _bj(now)
    mins = b.hour * 60 + b.minute
    return b.weekday() == WEEKLY_WEEKDAY and WEEKLY_START_MIN <= mins < WEEKLY_END_MIN


def period_str(now=None):
    """ISO 周（北京时间），如 2026-W41。"""
    return _bj(now).strftime("%Y-W%V")


# ---------------- 统计 ----------------

def _fetch_stats_msgs(db, chat, since_ts):
    """取某群 since_ts 之后的消息（含 is_at/needs_media），按时间升序。
    history.last_n_days 不返回 is_at/needs_media，这里自己查（同模式）。"""
    cols = [r[1] for r in db.execute("PRAGMA table_info(messages)")]
    extra = ", is_at" if "is_at" in cols else ", 0"
    extra += ", needs_media" if "needs_media" in cols else ", 0"
    ph = ",".join("?" for _ in EXCLUDE_SENDERS)
    rows = db.execute(
        f"""SELECT sender, sender_name, content, msg_time{extra} FROM messages
            WHERE chat=? AND direction='INBOUND' AND msg_time>=?
              AND sender NOT IN ({ph})
            ORDER BY msg_time ASC LIMIT 5000""",
        (chat, since_ts, *EXCLUDE_SENDERS)).fetchall()
    return [{"sender": r[0], "sender_name": r[1] or r[0],
             "content": r[2] or "", "msg_time": r[3],
             "is_at": r[4] or 0, "needs_media": r[5] or 0} for r in rows]


def compute_stats(db, chat, days=7):
    """最近 days 天的群聊统计。返回确定性数字 dict。"""
    since = int(time.time()) - days * 86400
    msgs = _fetch_stats_msgs(db, chat, since)
    counts = {}
    names = {}
    images = emojis = at_bot = 0
    for m in msgs:
        s = m["sender"]
        counts[s] = counts.get(s, 0) + 1
        names.setdefault(s, m["sender_name"])
        c = m["content"]
        if c == "[图片]" or m["needs_media"]:
            images += 1
        if c == "[表情]":
            emojis += 1
        if m["is_at"]:
            at_bot += 1
    top5 = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {
        "chat": chat, "days": days,
        "total": len(msgs),
        "speakers": len(counts),
        "top5": [{"sender": s,
                  "name": names[s] if names[s] != s else s[-4:],
                  "count": c} for s, c in top5],
        "images": images, "emojis": emojis, "at_bot": at_bot,
    }


def render_fixed_text(stats, label):
    """Python 渲染固定文本（worker 直接发送，不许改数字）。"""
    lines = [f"📊 {label}"]
    for i, t in enumerate(stats["top5"], 1):
        lines.append(f"{i}. {t['name']} {t['count']} 条")
    if not stats["top5"]:
        lines.append("（这段时间没人说话）")
    lines.append("")
    lines.append(f"💬 共 {stats['total']} 条 · 👥 {stats['speakers']} 人发言")
    lines.append(f"🖼 图片 {stats['images']} 张 · 😀 表情 {stats['emojis']} 个"
                f" · @bot {stats['at_bot']} 次")
    return "\n".join(lines)


def enqueue_stats_message(db, chat, event_type, kind, facts,
                          msg_id=0, user_id="", commit=True):
    """入队一条统计合成消息（kind GAME_REPLY，走游戏插队快速通道）。"""
    from games import store as gstore
    from games.manager import enqueue_game_message
    eid = gstore.add_event(db, 0, event_type, facts,
                           msg_id=msg_id, user_id=user_id, commit=False)
    mid = enqueue_game_message(db, chat, 0, eid, "GAME_REPLY", commit=False)
    if commit:
        db.commit()
    return eid, mid


def _build_facts(kind, stats, label, extra=None):
    fixed = render_fixed_text(stats, label)
    facts = {
        "kind": kind,
        "period": stats.get("period", ""),
        "stats": stats,
        "fixed_text": fixed,
        "worker_instruction":
            "直接发送 fixed_text，再加一句毒舌但不过火的水群点评（不许改数字）",
    }
    if extra:
        facts.update(extra)
    return facts


# ---------------- 健康检查 ----------------

def system_healthy(bridge_dir=None):
    """播报前健康检查：微信在线 + sendd 存活（复用 games.scheduler）。"""
    try:
        from games.scheduler import system_healthy_for_game
        return system_healthy_for_game(
            bridge_dir or os.path.dirname(os.path.abspath(__file__)))
    except Exception as e:
        return False, f"健康检查不可用: {e}"


# ---------------- D1：每周五 18:00 自动播报 ----------------

def check_weekly_stats(db, now=None):
    """功能 A 的 scheduled.tick() 每 5 分钟调用。

    窗口内（北京时间周五 18:00–18:05）且本周未推送 → 每白名单群入队一条
    WEEKLY_STATS，返回 report dict；否则返回 None。
    """
    ensure_schema_stats(db)
    if not in_weekly_window(now):
        return None
    period = period_str(now)

    muted, _ = msgqueue.is_muted(db, now)
    reports = []
    for chat in sorted(STATS_CHATS):
        if already_pushed(db, "weekly", period, chat):
            reports.append({"chat": chat, "status": "skipped",
                            "reason": "already pushed"})
            continue
        if muted:
            reports.append({"chat": chat, "status": "skipped",
                            "reason": "muted"})
            continue
        ok, reason = system_healthy()
        if not ok:
            reports.append({"chat": chat, "status": "skipped",
                            "reason": f"unhealthy: {reason}"})
            continue
        stats = compute_stats(db, chat, days=7)
        stats["period"] = period
        if stats["total"] == 0:
            reports.append({"chat": chat, "status": "skipped",
                            "reason": "no messages"})
            continue
        facts = _build_facts("weekly_stats", stats,
                             f"本周水群排行（{period} · 近7天）")
        eid, mid = enqueue_stats_message(
            db, chat, "WEEKLY_STATS", "GAME_REPLY", facts, commit=False)
        db.execute(
            "INSERT INTO stats_push_log (push_type, period, chat, created_at)"
            " VALUES (?,?,?,?)",
            ("weekly", period, chat, int(time.time())))
        db.commit()
        reports.append({"chat": chat, "status": "pushed",
                        "event_id": eid, "synth_msg_id": mid,
                        "stats": stats})
    return {"period": period, "window": "fri 18:00-18:05 BJ",
            "reports": reports}


# ---------------- D2：按需命令 ----------------

_AT_PREFIX_RE = re.compile(r"^@\S+\s*")
_STATS_WORDS = {"水群排行", "群统计", "本周统计", "上周统计"}


def parse_stats(content):
    """命中统计命令返回 days（7），否则 None。保守：只认 4 个词的 substring。"""
    t = (content or "").strip().replace("　", " ")
    t = _AT_PREFIX_RE.sub("", t).strip()
    if "上周统计" in t:
        return 7  # 目前取最近 7 天（自然周切片待加）
    if any(w in t for w in ("水群排行", "群统计", "本周统计")):
        return 7
    return None


def handle_stats_cmd(db, chat, sender, sender_name, content,
                     msg_id, msg_time):
    """按需命令入口（wal_reader 旁路调用）。不压原消息。
    返回 None 或 result dict（含 facts/event_id/synth_msg_id）。"""
    if chat not in STATS_CHATS:
        return None
    days = parse_stats(content)
    if days is None:
        return None
    ensure_schema_stats(db)
    muted, _ = msgqueue.is_muted(db, int(time.time()))
    if muted:
        return None
    stats = compute_stats(db, chat, days=days)
    facts = _build_facts("stats_ondemand", stats, "水群排行（近7天）",
                         extra={"requested_by": sender_name or sender})
    eid, mid = enqueue_stats_message(
        db, chat, "STATS_ONDEMAND", "GAME_REPLY", facts,
        msg_id=msg_id, user_id=sender, commit=False)
    db.commit()
    return {"event_type": "STATS_ONDEMAND", "facts": facts,
            "event_id": eid, "synth_msg_id": mid}
