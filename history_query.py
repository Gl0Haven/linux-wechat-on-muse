#!/usr/bin/env python3
"""微信历史只读查询（2026-10-11 E4）。

给 worker 按需回查用的受控接口，不是完整 MCP Server。
约束：
- 只读，不修改微信数据；
- 按群限制范围（当前只支持[主群] + 测试群）；
- 不跨群检索，不暴露私聊；
- 返回摘要而非全文，避免 prompt 膨胀。

接口：
  get_recent(chat, limit=20, before_ts=None) -> [msg]
  search(chat, keyword, hours=24, limit=10) -> [msg]
  get_by_svrid(chat, svrid) -> msg | None
每条 msg: {'svrid', 'sender', 'sender_name', 'type', 'text', 'time'}
"""
import hashlib
import os
import sqlite3
import sys
import time

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
sys.path.insert(0, BRIDGE)
sys.path.insert(0, os.path.join(BRIDGE, "frida"))

# 允许查询的群（白名单）
ALLOWED_CHATS = {
    "YOUR_MAIN_GROUP@chatroom",  # [主群]
    "YOUR_TEST_GROUP@chatroom",  # [测试群]
}

NAME_MAP = {
    "wxid_YOUR_MAIN_ACCOUNT": "大号",
    "wxid_REDACTED": "Ran",
    "wxid_REDACTED": "GH",
    "wxid_REDACTED": "&",
}


def _check_chat(chat):
    if chat not in ALLOWED_CHATS:
        raise ValueError(f"chat not allowed: {chat}")


def _open_db():
    from pathlib import Path
    from wal_reader import read_live_db
    src = Path(HOME) / "xwechat_files/wxid_YOUR_BOT_ACCOUNT/db_storage/message/message_0.db"
    out = read_live_db(src, "message/message_0.db")
    db = sqlite3.connect(f"file:{out}?mode=ro", uri=True, timeout=10)
    return db, out


def _table_for(db, chat):
    target = hashlib.md5(chat.encode()).hexdigest()
    tables = [r[0] for r in db.execute(
        "select name from sqlite_master where type='table' and name like 'Msg_%'")]
    for t in tables:
        if target in t:
            return t
    return None


def _row_to_msg(row, table):
    """(local_id, server_id, local_type, real_sender_id, create_time, content) -> dict"""
    lid, svr, ltype, sender, ct, content = row
    msg_type = (ltype or 0) & 0xFFFFFFFF
    text = ""
    if msg_type == 1:
        text = (content or "")[:200]
    elif msg_type == 3:
        text = "[图片]"
    elif msg_type == 47:
        text = "[表情包]"
    elif msg_type == 49:
        text = "[引用/链接]"
    elif msg_type == 34:
        text = "[语音]"
    else:
        text = f"[类型{msg_type}]"
    return {
        "svrid": str(svr),
        "sender": sender or "",
        "sender_name": NAME_MAP.get(sender or "", sender or ""),
        "type": msg_type,
        "text": text,
        "time": ct,
    }


def _get_content(db, table, row):
    from wxextract.messages import decompress_field
    lid, svr, ltype, sender, ct, mc, cc = row
    content = decompress_field(cc) if cc else (mc or b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")
    return (lid, svr, ltype, sender, ct, content)


def get_recent(chat, limit=20, before_ts=None):
    """取某群最近消息。"""
    _check_chat(chat)
    tmppath = None
    try:
        db, tmppath = _open_db()
        table = _table_for(db, chat)
        if not table:
            return []
        q = (f"SELECT local_id, server_id, local_type, real_sender_id,"
             f" create_time, message_content, compress_content FROM {table}")
        if before_ts:
            q += f" WHERE create_time < {int(before_ts)}"
        q += f" ORDER BY local_id DESC LIMIT {int(limit)}"
        rows = db.execute(q).fetchall()
        return [_row_to_msg(_get_content(db, table, r), table) for r in rows]
    finally:
        try:
            if tmppath and os.path.exists(tmppath):
                os.unlink(tmppath)
        except OSError:
            pass


def search(chat, keyword, hours=24, limit=10):
    """按关键词搜某群历史（只搜文字消息）。"""
    _check_chat(chat)
    if not keyword or len(keyword) > 50:
        return []
    tmppath = None
    try:
        db, tmppath = _open_db()
        table = _table_for(db, chat)
        if not table:
            return []
        since = int(time.time()) - int(hours) * 3600
        # 先按时间+类型过滤，再在 Python 侧做关键词匹配（微信 content 是压缩的）
        rows = db.execute(
            f"SELECT local_id, server_id, local_type, real_sender_id,"
            f" create_time, message_content, compress_content FROM {table}"
            f" WHERE create_time > {since} AND (local_type & 4294967295) = 1"
            f" ORDER BY local_id DESC LIMIT 500").fetchall()
        out = []
        for r in rows:
            lid, svr, ltype, sender, ct, content = _get_content(db, table, r)
            if keyword in (content or ""):
                out.append(_row_to_msg((lid, svr, ltype, sender, ct, content), table))
                if len(out) >= limit:
                    break
        return out
    finally:
        try:
            if tmppath and os.path.exists(tmppath):
                os.unlink(tmppath)
        except OSError:
            pass


def get_by_svrid(chat, svrid):
    """按 server_id 取单条消息。"""
    _check_chat(chat)
    tmppath = None
    try:
        db, tmppath = _open_db()
        table = _table_for(db, chat)
        if not table:
            return None
        r = db.execute(
            f"SELECT local_id, server_id, local_type, real_sender_id,"
            f" create_time, message_content, compress_content FROM {table}"
            f" WHERE server_id=?", (int(svrid),)).fetchone()
        if not r:
            return None
        return _row_to_msg(_get_content(db, table, r), table)
    finally:
        try:
            if tmppath and os.path.exists(tmppath):
                os.unlink(tmppath)
        except OSError:
            pass


if __name__ == "__main__":
    # 自测：取主群最近 3 条
    for m in get_recent("YOUR_MAIN_GROUP@chatroom", limit=3):
        print(m["sender_name"], m["text"][:40])
