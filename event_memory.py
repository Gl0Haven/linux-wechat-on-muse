#!/usr/bin/env python3
"""群聊事件记忆（2026-10-11 F3）。

存储群里发生过的事件：谁提到下周考试、前天讨论的游戏、约好的开黑时间等。
每条附来源消息 ID、时间、过期时间，避免把玩笑当长期事实。

设计（借鉴 koishi-plugin-memorytable）：
- 按群隔离，不跨群
- 短期事件（7天）+ 长期事件（30天，需人工确认）
- 只存事实性事件，不存情绪/评价

安全约束（观察期内）：
- 只实现存储+检索，不自动提取（提取需 LLM，等观察期后）
- 不接入回复链路，worker 按需调用 get_events()

用法：
  python3 event_memory.py add <chat> <text> <source_msg_id> [ttl_days]
  python3 event_memory.py list <chat> [limit]
  python3 event_memory.py search <chat> <keyword>
"""
import os
import sqlite3
import sys
import time

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
DB_PATH = os.path.join(BRIDGE, "queue.db")

ALLOWED_CHATS = {"YOUR_MAIN_GROUP@chatroom", "YOUR_TEST_GROUP@chatroom"}


def ensure_schema(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS group_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat TEXT NOT NULL,
            event_text TEXT NOT NULL,
            source_msg_id INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            confirmed INTEGER DEFAULT 0,
            UNIQUE(chat, event_text)
        )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_chat ON group_events(chat, expires_at)")
    db.commit()


def add_event(db, chat, text, source_msg_id=0, ttl_days=7):
    """存事件。返回 True/False。"""
    if chat not in ALLOWED_CHATS or not text or len(text) > 500:
        return False
    ensure_schema(db)
    now = int(time.time())
    try:
        db.execute("""
            INSERT OR IGNORE INTO group_events
            (chat, event_text, source_msg_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)""",
            (chat, text.strip(), source_msg_id, now, now + ttl_days * 86400))
        db.commit()
        return True
    except Exception:
        return False


def get_events(db, chat, limit=10):
    """取某群未过期事件（按时间倒序）。"""
    if chat not in ALLOWED_CHATS:
        return []
    ensure_schema(db)
    now = int(time.time())
    rows = db.execute("""
        SELECT event_text, source_msg_id, created_at, confirmed
        FROM group_events
        WHERE chat=? AND expires_at > ?
        ORDER BY created_at DESC LIMIT ?""", (chat, now, limit)).fetchall()
    return [{"text": r[0], "source_msg_id": r[1],
             "created_at": r[2], "confirmed": bool(r[3])} for r in rows]


def search_events(db, chat, keyword, limit=5):
    """按关键词搜事件。"""
    if chat not in ALLOWED_CHATS or not keyword:
        return []
    ensure_schema(db)
    now = int(time.time())
    rows = db.execute("""
        SELECT event_text, source_msg_id, created_at
        FROM group_events
        WHERE chat=? AND expires_at > ? AND event_text LIKE ?
        ORDER BY created_at DESC LIMIT ?""",
        (chat, now, f"%{keyword}%", limit)).fetchall()
    return [{"text": r[0], "source_msg_id": r[1], "created_at": r[2]} for r in rows]


def cleanup_expired(db):
    """清过期事件。返回清理数。"""
    ensure_schema(db)
    now = int(time.time())
    cur = db.execute("DELETE FROM group_events WHERE expires_at <= ?", (now,))
    db.commit()
    return cur.rowcount


if __name__ == "__main__":
    db = sqlite3.connect(DB_PATH, timeout=10)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "add" and len(sys.argv) >= 5:
        ttl = int(sys.argv[5]) if len(sys.argv) > 5 else 7
        ok = add_event(db, sys.argv[2], sys.argv[3], int(sys.argv[4]), ttl)
        print("ok" if ok else "failed")
    elif cmd == "list" and len(sys.argv) >= 3:
        lim = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        for e in get_events(db, sys.argv[2], lim):
            print(f"- {e['text']} (src:{e['source_msg_id']})")
    elif cmd == "search" and len(sys.argv) >= 4:
        for e in search_events(db, sys.argv[2], sys.argv[3]):
            print(f"- {e['text']}")
    elif cmd == "cleanup":
        print(f"cleaned {cleanup_expired(db)}")
    else:
        print(__doc__)
    db.close()
