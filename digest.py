#!/usr/bin/env python3
"""群聊日报 + 聊天记录问答（功能 A，2026-10-10）。

**开关位置**：`DIGEST_CHATS` 白名单。只在测试群
`YOUR_TEST_GROUP@chatroom`（[测试群]）启用。
要切到[主群]：把 `"YOUR_MAIN_GROUP@chatroom"` 加进 DIGEST_CHATS，
并在主群实测后再正式开（群里人多，日报措辞要更收敛）。

触发方式：
- A1 每日日报：`scheduled.tick()`（由 `~/hooks/scripts/wechat-game.sh`
  每 5 分钟驱动）→ 北京时间 22:00–22:05 取到第一个 tick 即生成一次
  （once-per-day 守卫在 `digest_log` 表）。
- A2 问答：`wal_reader_daemon.py` 旁路识别自然语言提问，入队合成回复。
  原消息不压制（不 suppress），只在命中时多发一条合成回复。

bot 的实际回复内容由 worker(LLM) 按 facts.worker_instruction 生成，
本模块只提供 facts 和指示，不手写群发内容。
"""

import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BJ = ZoneInfo("Asia/Shanghai")
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- 开关 ----------
# 想开主群：在集合里加 "YOUR_MAIN_GROUP@chatroom"
DIGEST_CHATS = {"YOUR_TEST_GROUP@chatroom",  # [测试群]
                  "YOUR_MAIN_GROUP@chatroom"}  # [主群]（2026-10-10 切入）

# 日报时间窗口（北京时间）：hook 每 5 分钟跑，22:00–22:05 的第一个 tick 触发
DIGEST_WINDOW_START = 22.0
DIGEST_WINDOW_END = 22.0 + 5.0 / 60.0
# 当日消息少于此数则静默跳过（不发空日报）
MIN_DIGEST_MSGS = 5


def ensure_schema_digest(db):
    db.execute("""CREATE TABLE IF NOT EXISTS digest_log (
        push_type TEXT NOT NULL,
        period TEXT NOT NULL,
        chat TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE (push_type, period, chat))""")
    db.commit()


def _beijing_now(now=None):
    return datetime.fromtimestamp(now or time.time(), tz=BJ)


def _in_window(now):
    bj = _beijing_now(now)
    h = bj.hour + bj.minute / 60.0
    return DIGEST_WINDOW_START <= h < DIGEST_WINDOW_END


# ================= A1 每日日报 =================

def check_daily_digest(db, now=None):
    """日报检查。返回 {"chat","msg_id","msg_count","date"} 或 {"skipped": 原因}。

    链路：北京时间 22:00–22:05 触发 → 取当日 00:00–22:00 消息 →
    入队合成消息（event_type DAILY_DIGEST），worker 按 worker_instruction
    用 history.py 取全量消息后生成日报正文（分 2~3 条发出）。
    """
    from games import store as gstore
    from games.manager import enqueue_game_message
    import history

    now = int(now or time.time())
    bj = _beijing_now(now)
    date = bj.strftime("%Y-%m-%d")
    ensure_schema_digest(db)

    for chat in sorted(DIGEST_CHATS):
        if not _in_window(now):
            return {"skipped": f"不在日报窗口内(北京时间 22:00–22:05), 当前 {bj.strftime('%H:%M')}"}

        # once-per-day 守卫
        row = db.execute(
            "SELECT 1 FROM digest_log WHERE push_type='daily_digest'"
            " AND period=? AND chat=?", (date, chat)).fetchone()
        if row:
            return {"skipped": f"{chat} {date} 日报已推送"}

        # 静默检查
        try:
            from msgqueue import is_muted
            muted, _ = is_muted(db, now)
            if muted:
                print(f"[digest] {chat} 静默期，跳过日报", flush=True)
                return {"skipped": "静默期"}
        except Exception as e:
            print(f"[digest] 静默检查失败(继续): {e}", flush=True)

        # 系统健康检查（微信登录态 + sendd 存活）
        try:
            from games.scheduler import system_healthy_for_game
            ok, reason = system_healthy_for_game(BRIDGE_DIR)
            if not ok:
                print(f"[digest] {chat} 系统不健康({reason})，跳过日报",
                      flush=True)
                return {"skipped": f"系统不健康: {reason}"}
        except Exception as e:
            print(f"[digest] 健康检查失败(继续): {e}", flush=True)

        # 取当日 00:00–22:00 消息
        day_start, day_end = history.day_bounds(date)
        msgs = history.fetch_range(db, chat, day_start,
                                   min(now, day_end), limit=500)
        n = len(msgs)
        if n < MIN_DIGEST_MSGS:
            print(f"[digest] {chat} {date} 仅 {n} 条消息(<{MIN_DIGEST_MSGS})，"
                  "不发空日报", flush=True)
            return {"skipped": f"消息太少({n} 条)"}

        facts = {
            "kind": "daily_digest",
            "date": date,
            "chat": chat,
            "msg_count": n,
            "worker_instruction": (
                f"生成 {date} 的群聊日报。先在 shell 里取当日全量消息：\n"
                f"cd ~/workspace/wechat-bridge && "
                f"python3 history.py --chat {chat} --date {date} --limit 500\n"
                "然后按这个结构写日报：\n"
                "① 今日话题 Top3（每条一句话讲清在聊什么、谁的观点最有意思）\n"
                "② 关键结论/约定（群里达成的共识、说好的事、待办）\n"
                "③ 金句摘录（2~3 条原话，注明是谁说的）\n"
                "④ 一段「AI 锐评」：熟人互损风格，阴阳怪气但不过火，"
                "只调侃发言内容不人身攻击，不造没发生过的梗\n"
                "分 2~3 条消息发出，手机阅读友好（短句分段），"
                "开头第一条注明「今日日报」。引用原话必须真实存在于查到的记录里，"
                "查不到的内容不要编。"
            ),
        }
        gstore.ensure_schema(db)
        eid = gstore.add_event(db, 0, "DAILY_DIGEST", facts, commit=False)
        mid = enqueue_game_message(db, chat, 0, eid, "GAME_REPLY",
                                   commit=False)
        db.execute(
            "INSERT OR IGNORE INTO digest_log"
            " (push_type, period, chat, created_at) VALUES (?,?,?,?)",
            ("daily_digest", date, chat, now))
        db.commit()
        print(f"[digest] {chat} {date} 日报已入队 mid={mid} ({n} 条消息)",
              flush=True)
        return {"chat": chat, "msg_id": mid, "msg_count": n, "date": date}

    return {"skipped": "无启用群聊"}


# ================= A2 聊天记录问答 =================

# 去掉开头的 @提及（如 "@Muse "），避免干扰意图识别
_AT_PREFIX_RE = re.compile(r"^@\S+\s*")

# "昨天/今天/前天…聊了啥/说了啥/在聊什么" —— 问号可选
_TIME_QA_RE = re.compile(
    r"^(昨天|今天|前天|大前天|上周|这周|本周|最近三天|最近一周)"
    r"(群里|大家|咱们)?(都)?"
    r"(聊了啥|说了啥|在聊什么|聊了什么|聊些什么|讨论了什么|在说什么)"
    r"\s*[?？]?$")

_TIME_HINT_DAYS = {
    "今天": ("today", 1),
    "昨天": ("yesterday", 1),
    "前天": ("day_before_yesterday", 2),
    "大前天": ("3_days_ago", 3),
    "最近三天": ("last_3_days", 3),
    "这周": ("this_week", 7),
    "本周": ("this_week", 7),
    "最近一周": ("last_7_days", 7),
    "上周": ("last_week", 14),
}

# "谁说…请客" / "谁请客"
_WHO_TREAT_RE = re.compile(
    r"^谁(说(要|过|了|请)?|请|提到|提过|说过|讲过)?.*"
    r"(请客|请吃饭|请喝奶茶|请喝咖啡|请吃火锅)\s*[?？]?$")

# "XXX 说了啥"（人名 1~12 字，中英数字下划线·. 组成，避免误伤长句；
#  名字和动词之间允许空格，如 "[friend1] 说了啥"）
_WHO_SAID_WHAT_RE = re.compile(
    r"^([\u4e00-\u9fa5A-Za-z0-9_·.\-]{1,12})\s*"
    r"(说|讲|提到|聊)(了|过)?(什么|啥|些什么)\s*[?？]?$")

# 名字里不能含时间词（防"我昨天聊了啥"被误判为问"我昨天"说了啥）
_NAME_TIME_WORDS = ("昨天", "今天", "前天", "大前天", "上周", "这周",
                    "本周", "最近")


def _clean(content):
    text = _AT_PREFIX_RE.sub("", (content or "").strip()).strip()
    return text


def parse_history_qa(content):
    """保守的意图识别。返回 (time_hint, keywords, question)，不命中返回 None。

    time_hint: today / yesterday / day_before_yesterday / 3_days_ago /
               last_3_days / this_week / last_7_days / last_week / None
    keywords: 供 history.search 用的关键词列表
    question: 清理后的原始问题
    宁可漏识别不误触发：整句必须完全匹配模式。
    """
    text = _clean(content)
    if not text or len(text) > 40:
        return None

    m = _TIME_QA_RE.match(text)
    if m:
        hint, _days = _TIME_HINT_DAYS[m.group(1)]
        return (hint, [], text)

    m = _WHO_TREAT_RE.match(text)
    if m:
        return (None, [m.group(3)], text)

    m = _WHO_SAID_WHAT_RE.match(text)
    if m:
        who = m.group(1)
        if who == "谁":
            return (None, [], text)
        if any(t in who for t in _NAME_TIME_WORDS):
            return None  # "我昨天聊了啥"之类：名字里混了时间词，不触发
        return (None, [who], text)

    return None


def handle_history_qa(db, chat, sender, sender_name, content, msg_id,
                      msg_time):
    """处理聊天记录问答。命中意图则入队合成回复，返回 result dict，否则 None。

    旁路设计：原消息不 suppress，只在命中时加一条合成回复
    （event_type HISTORY_QA → worker 查证后回答）。
    """
    from games import store as gstore
    from games.manager import enqueue_game_message

    if chat not in DIGEST_CHATS:
        return None

    parsed = parse_history_qa(content)
    if not parsed:
        return None
    time_hint, keywords, question = parsed

    try:
        from msgqueue import is_muted
        muted, _ = is_muted(db, int(time.time()))
        if muted:
            return None
    except Exception:
        pass

    asker = sender_name or sender
    if time_hint in ("today", "yesterday", "day_before_yesterday",
                     "3_days_ago"):
        fetch_hint = (f"python3 history.py --chat {chat} "
                      f"--date <对应日期> --limit 500")
    elif keywords:
        fetch_hint = (f"python3 history.py --chat {chat} "
                      f"--search \"{' '.join(keywords)}\" --days 30")
    else:
        fetch_hint = (f"python3 history.py --chat {chat} --days 7 "
                      f"--limit 500")
    facts = {
        "kind": "history_qa",
        "question": question,
        "asker": asker,
        "time_hint": time_hint,
        "keywords": keywords,
        "worker_instruction": (
            f"群友 {asker} 问了一句关于聊天记录的问题：「{question}」。\n"
            f"时间提示：{time_hint or '无（按近 7 天查）'}；"
            f"关键词：{' '.join(keywords) if keywords else '无'}。\n"
            "先在 shell 里查证（cd ~/workspace/wechat-bridge）：\n"
            f"{fetch_hint}\n"
            "（time_hint=yesterday 时对应日期是昨天（北京时间），"
            "其余 hint 按字面换算；查不到就加 --days 放大范围再试一次。）\n"
            "回答要求：\n"
            "1. 引用原话必须真实存在于查到的记录里，注明是谁说的、哪天说的；\n"
            "2. 查不到就直说「没查到」，不编、不脑补；\n"
            "3. 简短直接，1~2 条消息，不要长篇大论；\n"
            "4. 不要回答与聊天记录无关的后续追问（那是正常聊天，走正常回复）。"
        ),
    }
    gstore.ensure_schema(db)
    eid = gstore.add_event(db, 0, "HISTORY_QA", facts,
                           msg_id=msg_id, user_id=sender, commit=False)
    mid = enqueue_game_message(db, chat, 0, eid, "GAME_REPLY", commit=False)
    db.commit()
    print(f"[digest] 聊天记录问答已入队 mid={mid} chat={chat} 问={question!r}",
          flush=True)
    return {"synth_msg_id": mid, "event_id": eid, "question": question,
            "time_hint": time_hint, "keywords": keywords}
