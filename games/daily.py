#!/usr/bin/env python3
"""
每日签到 + 今日运势（算命）。

- 签到：每人每天一次，连续签到加分（进积分榜）。
- 算命：按 (群, 人, 日期) 确定性生成运势骨架（星级/分项/幸运物/宜忌），
  结合本人近期发言做针对性解读（worker 负责语言表现）。

只在 GAMES_ALLOWED_CHATS 的群里可用。由 router 在 /game 命令之前识别。
"""

import hashlib
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import store as gstore
from .config import GAMES_ALLOWED_CHATS, GAME_SYSTEM_SENDER

BJ = ZoneInfo("Asia/Shanghai")

CHECKIN_WORDS = {"签到", "/签到", "每日签到"}
FORTUNE_WORDS = {"算命", "/算命", "算一卦", "今日运势", "今日运程",
                 "帮我算一卦", "给我算一卦", "帮我算算"}

_AT_PREFIX_RE = re.compile(r"^@\S+\s*")


def ensure_schema_daily(db):
    db.execute("""CREATE TABLE IF NOT EXISTS daily_checkin (
        chat_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        date TEXT NOT NULL,
        streak INTEGER DEFAULT 1,
        points INTEGER DEFAULT 0,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (chat_id, user_id, date))""")
    db.commit()


def _today():
    return datetime.now(BJ).strftime("%Y-%m-%d")


def parse_daily(content):
    """返回 'checkin' / 'fortune' / None。"""
    from .base import Game
    text = Game.normalize(content or "").strip()
    text = _AT_PREFIX_RE.sub("", text).strip()
    if text in CHECKIN_WORDS:
        return "checkin"
    if text in FORTUNE_WORDS:
        return "fortune"
    return None


# ---------------- 签到 ----------------

def do_checkin(db, chat, sender, sender_name):
    """执行签到，返回 facts（含 fixed_text，直接发送）。"""
    ensure_schema_daily(db)
    today = _today()
    name = sender_name or sender
    row = db.execute(
        "SELECT streak, points FROM daily_checkin"
        " WHERE chat_id=? AND user_id=? AND date=?",
        (chat, sender, today)).fetchone()
    if row:
        total = gstore.get_stats(db, chat, sender)["points"]
        return {"kind": "checkin", "player": name, "already": True,
                "streak": row[0], "points_today": row[1],
                "total_points": total,
                "fixed_text": f"{name} 今天已经签过到了 ✅ 连续 {row[0]} 天"}

    yesterday = (datetime.now(BJ) - timedelta(days=1)).strftime("%Y-%m-%d")
    prev = db.execute(
        "SELECT streak FROM daily_checkin"
        " WHERE chat_id=? AND user_id=? AND date=?",
        (chat, sender, yesterday)).fetchone()
    streak = (prev[0] + 1) if prev else 1

    # 2026-10-10 用户要求：签到改抽奖制，1-10 随机，1 概率最高指数递减
    # 权重 2^(10-n)：1→512, 2→256, ..., 10→1，总 1023
    import random
    weights = [2 ** (10 - n) for n in range(1, 11)]
    draw = random.choices(range(1, 11), weights=weights, k=1)[0]
    points = draw
    bonus = ""
    if streak % 30 == 0:
        points += 10
        bonus = "，30 天里程碑 +10"
    elif streak % 7 == 0:
        points += 3
        bonus = "，满 7 天 +3"

    db.execute(
        "INSERT INTO daily_checkin (chat_id, user_id, date, streak, points,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (chat, sender, today, streak, points, int(time.time())))
    gstore.add_points_by_chat(db, chat, sender, name, points)
    total = gstore.get_stats(db, chat, sender)["points"]
    db.commit()
    return {"kind": "checkin", "player": name, "already": False,
            "streak": streak, "points_today": points,
            "total_points": total,
            "fixed_text": (f"✅ {name} 签到成功！连续 {streak} 天，"
                           f"抽中 {draw} 分{bonus}（总 {total} 分）")}


# ---------------- 算命 ----------------

LUCKY_COLORS = ["曜石黑", "奶茶棕", "薄荷绿", "落日橙", "雾霾蓝", "樱花粉",
                "墨绿", "珍珠白", "番茄红", "克莱因蓝"]
YI_POOL = ["摸鱼", "喝奶茶", "在群里冒泡", "早睡", "给群友点赞",
           "立个小目标", "整理桌面", "听老歌", "出门走走", "夸一下同事",
           "学个新梗", "给阳台植物浇水", "复盘昨天", "吃顿好的"]
JI_POOL = ["立 flag", "熬夜", "冲动消费", "已读乱回", "空腹喝咖啡",
           "深夜 emo", "拖延", "跟人抬杠", "暴饮暴食", "久坐不动"]
CHARMS = ["一杯冰美式", "耳机", "润唇膏", "创可贴", "一枚硬币",
          "便利贴", "雨伞", "薄荷糖", "手账本", "保温杯"]


def _seeded(chat, sender, date):
    h = hashlib.md5(f"{chat}|{sender}|{date}".encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _recent_msgs(db, chat, sender, limit=5, days=7):
    """本人近期发言样本（去命令词），供 worker 做针对性解读。"""
    since = int(time.time()) - days * 86400
    rows = db.execute(
        "SELECT substr(content,1,40) FROM messages"
        " WHERE chat=? AND sender=? AND direction='INBOUND'"
        " AND msg_time>=? AND length(content)<=60"
        " ORDER BY msg_time DESC LIMIT ?",
        (chat, sender, since, limit * 3)).fetchall()
    out = []
    for (c,) in rows:
        c = (c or "").strip()
        if (c and not c.startswith("/") and c not in CHECKIN_WORDS
                and c not in FORTUNE_WORDS and "[图片]" not in c
                and "[应用消息]" not in c and c not in out):
            out.append(c)
        if len(out) >= limit:
            break
    return out


def _pick_guide(db, chat, sender, rng):
    """今日贵人：从群里最近发言的人里挑一个（排除自己和 bot）。"""
    since = int(time.time()) - 7 * 86400
    rows = db.execute(
        "SELECT DISTINCT sender_name FROM messages"
        " WHERE chat=? AND msg_time>=? AND sender NOT IN (?, ?)"
        " AND sender_name NOT IN ('', '游戏系统')",
        (chat, since, sender, GAME_SYSTEM_SENDER)).fetchall()
    names = [r[0] for r in rows if r[0]]
    return rng.choice(names) if names else ""


def get_fortune(db, chat, sender, sender_name):
    """确定性运势骨架 + 个人发言上下文。同一人同一天结果不变。"""
    date = _today()
    rng = _seeded(chat, sender, date)
    name = sender_name or sender
    overall = rng.choices([3, 4, 5, 2], weights=[45, 35, 12, 8])[0]
    scores = {
        "事业": rng.randint(62, 98),
        "财运": rng.randint(60, 97),
        "桃花": rng.randint(55, 99),
        "健康": rng.randint(65, 98),
    }
    recent = _recent_msgs(db, chat, sender)
    msg_count = db.execute(
        "SELECT COUNT(*) FROM messages WHERE chat=? AND sender=?"
        " AND direction='INBOUND' AND msg_time>=?",
        (chat, sender, int(time.time()) - 7 * 86400)).fetchone()[0]
    return {
        "kind": "fortune", "player": name, "date": date,
        "overall": overall, "scores": scores,
        "lucky_number": rng.randint(1, 99),
        "lucky_color": rng.choice(LUCKY_COLORS),
        "yi": rng.sample(YI_POOL, 2),
        "ji": rng.sample(JI_POOL, 2),
        "charm": rng.choice(CHARMS),
        "guide": _pick_guide(db, chat, sender, rng),
        "recent_msgs": recent,
        "msg_count_7d": msg_count,
    }


# ---------------- 路由入口 ----------------

def handle_daily(db, chat, sender, sender_name, kind, msg_id,
                 msg_time, should_reply=0):
    """处理签到/算命。返回 router 风格的 result dict，或 None。"""
    from .manager import enqueue_game_message

    if chat not in GAMES_ALLOWED_CHATS:
        return None
    ensure_schema_daily(db)
    suppress = should_reply in (1, 2)
    base = {"game_id": 0, "suppress_original": suppress,
            "reply_needed": True, "event_id": 0, "synth_msg_id": None}

    if kind == "checkin":
        facts = do_checkin(db, chat, sender, sender_name)
    elif kind == "fortune":
        facts = get_fortune(db, chat, sender, sender_name)
    else:
        return None

    eid = gstore.add_event(db, 0, "DAILY_" + kind.upper(), facts,
                           msg_id=msg_id, user_id=sender, commit=False)
    mid = enqueue_game_message(db, chat, 0, eid, "GAME_REPLY", commit=False)
    db.commit()
    return {**base, "event_type": "DAILY_" + kind.upper(),
            "facts": facts, "event_id": eid, "synth_msg_id": mid}
