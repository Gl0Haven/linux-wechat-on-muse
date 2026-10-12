#!/usr/bin/env python3
"""聊天记录查询 helper（只读）：供日报 / 问答 / 统计等功能使用。

数据源：~/workspace/wechat-bridge/queue.db 的 messages 表（经 msgqueue.get_db()）。
- 时间字段统一用 msg_time（微信消息时间戳）；时区换算一律 Asia/Shanghai。
- 默认只查 INBOUND（群友发的）；GAME_SYSTEM 合成消息默认排除（调用方可改）。
- 图片/表情在 content 里记为 "[图片]" / "[表情]"，调用方自行过滤或标注。

CLI（供 worker 按 facts 里的指示取数）：
    python3 history.py --chat <chat_id> --date 2026-10-10 [--limit 500]
    python3 history.py --chat <chat_id> --days 7 [--limit 2000]
    python3 history.py --chat <chat_id> --search "请客 奶茶" --days 30
输出 JSON（ensure_ascii=False）。
"""

import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BJ = ZoneInfo("Asia/Shanghai")

# 默认排除的"系统"发送者（合成消息，不算群友发言）
DEFAULT_EXCLUDE = {"GAME_SYSTEM"}


def day_bounds(date_str):
    """'2026-10-10'（北京时间）→ (day_start_ts, day_end_ts)。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=BJ)
    start = int(d.timestamp())
    return start, start + 86400


def today_str(now=None):
    return datetime.fromtimestamp(now or time.time(), tz=BJ).strftime("%Y-%m-%d")


def fetch_range(db, chat, start_ts, end_ts, limit=500,
                exclude_senders=DEFAULT_EXCLUDE):
    """取某群某时间段的消息（INBOUND），按时间升序。返回
    [{sender, sender_name, content, msg_time}, ...]。"""
    skip = set(exclude_senders or ())
    ph = ",".join("?" for _ in skip) or "''"
    rows = db.execute(
        f"""SELECT sender, sender_name, content, msg_time FROM messages
            WHERE chat=? AND direction='INBOUND'
              AND msg_time>=? AND msg_time<?
              AND sender NOT IN ({ph})
            ORDER BY msg_time ASC LIMIT ?""",
        (chat, start_ts, end_ts, *skip, limit)).fetchall()
    return [{"sender": r[0], "sender_name": r[1] or r[0],
             "content": r[2] or "", "msg_time": r[3]} for r in rows]


def search(db, chat, keywords, days=30, limit=50,
           exclude_senders=DEFAULT_EXCLUDE):
    """关键词搜索（OR），按时间降序。keywords: str 或 list[str]。"""
    if isinstance(keywords, str):
        keywords = [k for k in keywords.split() if k]
    keywords = [k for k in keywords if k]
    if not keywords:
        return []
    since = int(time.time()) - days * 86400
    skip = set(exclude_senders or ())
    ph = ",".join("?" for _ in skip) or "''"
    cond = " OR ".join("content LIKE ?" for _ in keywords)
    rows = db.execute(
        f"""SELECT sender, sender_name, content, msg_time FROM messages
            WHERE chat=? AND direction='INBOUND' AND msg_time>=?
              AND sender NOT IN ({ph}) AND ({cond})
            ORDER BY msg_time DESC LIMIT ?""",
        (chat, since, *skip, *[f"%{k}%" for k in keywords],
         limit)).fetchall()
    return [{"sender": r[0], "sender_name": r[1] or r[0],
             "content": r[2] or "", "msg_time": r[3]} for r in rows]


def last_n_days(db, chat, days, limit=2000,
                exclude_senders=DEFAULT_EXCLUDE):
    """最近 N 天的消息，按时间升序（统计用）。"""
    now = int(time.time())
    return fetch_range(db, chat, now - days * 86400, now + 60,
                       limit=limit, exclude_senders=exclude_senders)


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="聊天记录查询（只读）")
    ap.add_argument("--chat", required=True)
    ap.add_argument("--date", default="",
                    help="查某天（北京时间），如 2026-10-10")
    ap.add_argument("--days", type=int, default=0,
                    help="查最近 N 天（与 --date 二选一）")
    ap.add_argument("--search", default="", help="关键词（空格分隔，OR）")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args(argv)

    import msgqueue
    db = msgqueue.get_db()
    if args.search:
        rows = search(db, args.chat, args.search.split(),
                      days=args.days or 30, limit=args.limit)
    elif args.date:
        s, e = day_bounds(args.date)
        rows = fetch_range(db, args.chat, s, e, limit=args.limit)
    else:
        rows = last_n_days(db, args.chat, args.days or 1,
                           limit=args.limit)
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
