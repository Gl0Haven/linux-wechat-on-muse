#!/usr/bin/env python3
"""回复密度限制（2026-10-11 F1）。

借鉴 group_chat_plus 的 Phase 7.1：
- 硬限制：窗口内超过最大回复数 → 直接跳过，不调用 AI
- 软限制：达到 60% 时提示 AI"已回复较多"

按群独立统计。
"""
import os
import sqlite3
import time

HOME = os.path.expanduser("~")
DB_PATH = os.path.join(HOME, "workspace/wechat-bridge/queue.db")

# 窗口 5 分钟，硬限制 5 条，软限制 60%
WINDOW_SEC = 300
HARD_LIMIT = 5
SOFT_RATIO = 0.6


def _db():
    return sqlite3.connect(DB_PATH, timeout=10)


def record_reply(chat):
    """记录一次 bot 回复（发送成功时调用）。"""
    db = _db()
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reply_density (
                chat TEXT NOT NULL, ts INTEGER NOT NULL
            )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_density ON reply_density(chat, ts)")
        db.execute("INSERT INTO reply_density (chat, ts) VALUES (?, ?)",
                   (chat, int(time.time())))
        # 清理旧记录（1小时前）
        db.execute("DELETE FROM reply_density WHERE ts < ?",
                   (int(time.time()) - 3600,))
        db.commit()
    finally:
        db.close()


def check_density(chat):
    """检查密度。返回 (allowed: bool, hint: str)。
    allowed=False → 硬限制，直接跳过
    hint 非空 → 软限制，提示 AI
    """
    db = _db()
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reply_density (
                chat TEXT NOT NULL, ts INTEGER NOT NULL
            )""")
        now = int(time.time())
        n = db.execute(
            "SELECT COUNT(*) FROM reply_density WHERE chat=? AND ts > ?",
            (chat, now - WINDOW_SEC)).fetchone()[0]
        if n >= HARD_LIMIT:
            return False, ""
        if n >= HARD_LIMIT * SOFT_RATIO:
            return True, f"你在该群 5 分钟内已回复 {n} 次，适当减少回复，保持克制。"
        return True, ""
    finally:
        db.close()
