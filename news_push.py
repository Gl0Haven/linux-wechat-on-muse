#!/usr/bin/env python3
"""
每日定时推送：互联网大事情（约 10 条，中外混搭）。

触发链路（**不许用 cron**）：
    ~/hooks/scripts/wechat-game.sh 每 5 分钟 tick
      → scheduled.tick(db, now)（功能 A，try/except ImportError 调本模块）
      → check_news_push(db, now)

窗口：北京时间 08:30–08:35（tick 每 5 分钟，窗口内通常命中一次；
once-per-day 守卫防重发）。

开关 NEWS_CHATS：2026-10-12 起只发主群，测试群不再收（用户要求）。
切换方法：把群 chatroom id 加进 NEWS_CHATS 集合即开；从集合里删掉即关。

内容：news_sources.py 拉 RSS（中文组 + 全球组，含正文摘要），Python 里按标题去重、
每源取最新 5 条（约 70 条候选）；由 worker 按主群口味精选 8-12 条，
每条写实质内容（2-4 句）+ 群梗风点评，不只做一句话概括
（facts.worker_instruction）。

失败回退：全部源失败 → 跳过当天、不入队、不发空消息，记日志。
"""

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")

# ---------------- 开关 ----------------
# 2026-10-12 起只发主群，测试群不再收（用户要求）。
NEWS_CHATS = {"YOUR_MAIN_GROUP@chatroom"}  # [主群]
LAOBEIJING_CHAT = "YOUR_MAIN_GROUP@chatroom"  # [主群]

PUSH_TYPE = "NEWS_PUSH"
WINDOW_START = (8, 30)   # 北京时间 08:30（含）
WINDOW_END = (8, 35)     # 北京时间 08:35（不含）
ITEMS_PER_SOURCE = 5     # 2026-10-11 改：每个源取 5 条（14 源约 70 条候选），
                         # 由 worker 按主群口味精选后推送
FRESH_HOURS = 48         # 只取 48 小时内的新闻（无时间的条目排最后，不丢弃）

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)

from games import store as gstore  # noqa: E402
from games.manager import enqueue_game_message  # noqa: E402
from games.scheduler import system_healthy_for_game  # noqa: E402
from msgqueue import is_muted  # noqa: E402
from news_sources import fetch_all_news, dedup_news  # noqa: E402


def _beijing_now(now=None):
    return datetime.fromtimestamp(now or time.time(), tz=BJ)


def _today_str(now=None):
    return _beijing_now(now).strftime("%Y-%m-%d")


def in_window(now=None):
    t = _beijing_now(now)
    cur = (t.hour, t.minute)
    return WINDOW_START <= cur < WINDOW_END


def ensure_schema_news(db):
    db.execute("""CREATE TABLE IF NOT EXISTS news_push_log (
        push_type TEXT NOT NULL,
        period TEXT NOT NULL,
        chat TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        outcome TEXT DEFAULT '',
        UNIQUE(push_type, period, chat))""")
    db.commit()


def already_pushed(db, chat, date):
    row = db.execute(
        "SELECT 1 FROM news_push_log WHERE push_type=? AND period=? AND chat=?",
        (PUSH_TYPE, date, chat)).fetchone()
    return bool(row)


def mark_pushed(db, chat, date, outcome, commit=True):
    db.execute(
        "INSERT OR IGNORE INTO news_push_log"
        " (push_type, period, chat, created_at, outcome) VALUES (?,?,?,?,?)",
        (PUSH_TYPE, date, chat, int(time.time()), outcome))
    if commit:
        db.commit()


def _log(msg):
    try:
        path = os.path.join(BRIDGE_DIR, "state", "news_push.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{_beijing_now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def select_items(items, now=None, per_source=ITEMS_PER_SOURCE,
                 fresh_hours=FRESH_HOURS):
    """去重 → 每个源取最新 per_source 条 → 中外穿插。

    2026-10-11 改：从"每组 5 条"改为"每源 5 条"，给 worker 充足候选
    由其按群口味精选。返回 [{title, url, source, summary}]。

    返回的 summary 为纯文本摘要（最长 800 字），供 worker 写实质内容，
    不要只看标题概括。
    """
    now = now or int(time.time())
    items = dedup_news(items)
    cutoff = now - fresh_hours * 3600

    def sort_key(it):
        pub = it.get("published") or 0
        fresh = 0 if (pub and pub >= cutoff) else 1  # 48h 内的排前面
        return (fresh, -pub)

    # 按源分组，每源取最新 per_source 条
    by_source = {}
    for it in items:
        by_source.setdefault(it.get("source", "未知"), []).append(it)
    picked = []
    for source in sorted(by_source):
        grp = sorted(by_source[source], key=sort_key)
        picked.extend(grp[:per_source])

    # 中外穿插：先按组分开，再交错合并，保证中外混合
    cn = [it for it in picked if it.get("group") == "cn"]
    gl = [it for it in picked if it.get("group") == "global"]
    # 组内按时间倒序
    cn = sorted(cn, key=sort_key)
    gl = sorted(gl, key=sort_key)
    merged, i, j = [], 0, 0
    while i < len(cn) or j < len(gl):
        if i < len(cn):
            merged.append(cn[i]); i += 1
        if j < len(gl):
            merged.append(gl[j]); j += 1
    return [{"title": it["title"], "url": it["url"], "source": it["source"],
             "summary": it.get("summary", "")}
            for it in merged]


WORKER_INSTRUCTION = (
    "你拿到的是约 70 条候选新闻（14 个源 × 每源 5 条，中外混合），"
    "每条都有 title、source 和 summary（正文摘要）。"
    "请你做主群的新闻编辑，按以下标准精选 8-12 条最值得推的：\n"
    "精选标准：① 群友会感兴趣的（AI/科技大事件、互联网大新闻、有梗可玩的）；"
    "② 去重（同一事件只留一条最好的）；③ 中外兼顾，不要只推一类。\n"
    "输出要求：每条新闻单独成段，格式为「标题 + 实质内容 + 点评」：\n"
    "- 标题：原标题（可微调更口语化）\n"
    "- 实质内容：用 summary 里的关键信息写 2-4 句话，把这条新闻讲清楚"
    "（发生了什么、关键数字、影响），不要只概括一句话了事；"
    "summary 为空的可以跳过该条\n"
    "- 点评：一句话群梗风锐评（活泼、玩梗，AI 方向可稍微专业），"
    "只调侃内容不人身攻击\n"
    "分 3-5 条消息发出（每条消息 2-3 条新闻），手机阅读友好，短句分段。"
    "开头第一条注明「今日新闻」。url 一般不用贴。"
)


def check_news_push(db, now=None):
    """每日新闻推送检查。返回 {chat: report}。

    scheduled.tick() 每 5 分钟调一次；窗口外/已推送/静默/不健康/源全失败
    都记 {"skipped": 原因}，不入队。

    2026-10-12 修：原来 for 循环里直接 return，只处理了集合里第一个群
    （NEWS_CHATS 是 set，顺序随机），每天随机只有一个群能收到新闻。
    现在逐个群独立处理，RSS 只抓一次、两个群共享同一批 items。
    """
    now = now or int(time.time())
    date = _today_str(now)
    ensure_schema_news(db)
    results = {}

    if not in_window(now):
        for chat in sorted(NEWS_CHATS):
            results[chat] = {"skipped": "out_of_window", "chat": chat,
                             "date": date}
        return results

    muted, _ = is_muted(db, now)
    if muted:
        for chat in sorted(NEWS_CHATS):
            _log(f"[{date}] {chat} skipped: muted")
            results[chat] = {"skipped": "muted", "chat": chat, "date": date}
        return results

    ok, reason = system_healthy_for_game(BRIDGE_DIR, now)
    if not ok:
        for chat in sorted(NEWS_CHATS):
            _log(f"[{date}] {chat} skipped: unhealthy: {reason}")
            results[chat] = {"skipped": f"unhealthy: {reason}", "chat": chat,
                             "date": date}
        return results

    pending = [c for c in sorted(NEWS_CHATS)
               if not already_pushed(db, c, date)]
    for chat in sorted(NEWS_CHATS):
        if chat not in pending:
            results[chat] = {"skipped": "already_pushed", "chat": chat,
                             "date": date}
    if not pending:
        return results

    fetched = fetch_all_news()
    picked = select_items(fetched["items"], now=now)
    if not picked:
        for chat in pending:
            mark_pushed(db, chat, date,
                        f"all_sources_failed: {fetched['failed']}")
            _log(f"[{date}] {chat} skipped: all_sources_failed: "
                 f"{fetched['failed']}")
            results[chat] = {"skipped": "all_sources_failed", "chat": chat,
                             "date": date, "failed": fetched["failed"]}
        return results

    facts = {"kind": "news_push", "date": date, "items": picked,
             "worker_instruction": WORKER_INSTRUCTION,
             "sources_ok": fetched["ok"]}
    for chat in pending:
        eid = gstore.add_event(db, 0, "NEWS_PUSH", facts, commit=False)
        mid = enqueue_game_message(db, chat, 0, eid, "GAME_REPLY",
                                   commit=False)
        if not mid:
            db.rollback()
            _log(f"[{date}] {chat} skipped: enqueue_failed")
            results[chat] = {"skipped": "enqueue_failed", "chat": chat,
                             "date": date}
            continue
        mark_pushed(db, chat, date, f"ok:{len(picked)}", commit=False)
        db.commit()

        _log(f"[{date}] {chat} pushed {len(picked)} items "
             f"(event={eid}, msg={mid}), sources_ok={len(fetched['ok'])}, "
             f"sources_failed={list(fetched['failed'])}")
        results[chat] = {"ok": True, "chat": chat, "date": date,
                         "items": len(picked), "event_id": eid,
                         "synth_msg_id": mid,
                         "sources_ok": fetched["ok"],
                         "sources_failed": fetched["failed"]}
    return results


if __name__ == "__main__":
    # 手工冒烟：python3 news_push.py（只在窗口内会真跑，默认只读检查）
    import sqlite3
    print("in_window:", in_window(), "| today:", _today_str())
