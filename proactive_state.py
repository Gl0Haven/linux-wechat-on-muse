#!/usr/bin/env python3
"""主动搭话状态机（2026-10-11 F2）。

借鉴 proactive_chat：按群独立维护状态。
- last_activity: 最后群活动时间
- unreplied: 连续主动未被接话计数
- cooldown_until: 冷却到何时
- paused: 是否自动暂停（连续3次未接话）

新消息到来 → 重置 unreplied，更新 activity
主动发送 → unreplied+1，设冷却
unreplied>=3 → 自动暂停，需人工或新话题恢复
"""
import os
import sqlite3
import time

HOME = os.path.expanduser("~")
DB_PATH = os.path.join(HOME, "workspace/wechat-bridge/queue.db")

# 连续3次主动未接话 → 自动暂停
MAX_UNREPLIED = 3
# 主动后冷却 5 分钟（现有规则保留）
COOLDOWN_SEC = 300


def _db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("""
        CREATE TABLE IF NOT EXISTS proactive_state (
            chat TEXT PRIMARY KEY,
            last_activity INTEGER DEFAULT 0,
            unreplied INTEGER DEFAULT 0,
            cooldown_until INTEGER DEFAULT 0,
            paused INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0
        )""")
    return db


def on_group_activity(chat):
    """群有新消息（任何人）：更新活动时间，重置未接话计数。"""
    db = _db()
    try:
        now = int(time.time())
        db.execute("""
            INSERT INTO proactive_state (chat, last_activity, unreplied, updated_at)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(chat) DO UPDATE SET
                last_activity=excluded.last_activity,
                unreplied=0,
                paused=0,
                updated_at=excluded.updated_at""",
            (chat, now, now))
        db.commit()
    finally:
        db.close()


def on_proactive_sent(chat):
    """主动消息已发送：计数+1，设冷却；3次未接话则暂停。"""
    db = _db()
    try:
        now = int(time.time())
        row = db.execute(
            "SELECT unreplied FROM proactive_state WHERE chat=?", (chat,)).fetchone()
        unreplied = (row[0] if row else 0) + 1
        paused = 1 if unreplied >= MAX_UNREPLIED else 0
        db.execute("""
            INSERT INTO proactive_state (chat, unreplied, cooldown_until, paused, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat) DO UPDATE SET
                unreplied=excluded.unreplied,
                cooldown_until=excluded.cooldown_until,
                paused=excluded.paused,
                updated_at=excluded.updated_at""",
            (chat, unreplied, now + COOLDOWN_SEC, paused, now))
        db.commit()
        return paused == 1
    finally:
        db.close()


def check_allowed(chat):
    """检查是否允许主动。返回 (allowed, reason)。"""
    db = _db()
    try:
        row = db.execute(
            "SELECT unreplied, cooldown_until, paused, last_activity"
            " FROM proactive_state WHERE chat=?", (chat,)).fetchone()
        if not row:
            return True, "ok"
        unreplied, cooldown_until, paused, _ = row
        now = int(time.time())
        if paused:
            return False, f"连续{unreplied}次未接话，已自动暂停"
        if now < cooldown_until:
            return False, f"冷却中（{cooldown_until - now}s）"
        return True, "ok"
    finally:
        db.close()


def get_state(chat):
    """取状态（调试用）。"""
    db = _db()
    try:
        row = db.execute(
            "SELECT last_activity, unreplied, cooldown_until, paused"
            " FROM proactive_state WHERE chat=?", (chat,)).fetchone()
        if not row:
            return None
        return {"last_activity": row[0], "unreplied": row[1],
                "cooldown_until": row[2], "paused": bool(row[3])}
    finally:
        db.close()
