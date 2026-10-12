#!/usr/bin/env python3
"""轻量微信数据库轮询器 —— 替代截图 worker。

每次运行：
1. 拷贝 message_*.db (+WAL) 到临时目录（不锁 live DB）
2. 用缓存的 key 解密
3. 查出游标之后的新消息，追加到 messages.jsonl
4. 输出新消息的 JSON（供调用方判断 @）

用法: python3 db_poller.py [--json]
游标文件: ~/workspace/wechat-bridge/state/db_cursor.json
"""
# ⚠️ 已退役（2026-10-06 D方案）：直读微信源 DB，违反"唯一 reader"原则。
# 生产流程不再调用此文件，保留仅供手动调试。
import json
import shutil
import sqlite3
import sys
import tempfile
import hashlib
from pathlib import Path

from wxextract.decrypt import decrypt_file
from wxextract.keys import load_keys
from wxextract.messages import decompress_field

HOME = Path.home()
DB_BASE = HOME / "xwechat_files/wxid_YOUR_BOT_ACCOUNT/db_storage"
WS = HOME / ".local/share/wxextract"
BRIDGE = HOME / "workspace/wechat-bridge"
# WAL-aware reader: merges WAL frames for real-time data (0.6s vs 30-60s delay)
import sys as _sys
_sys.path.insert(0, str(BRIDGE / "frida"))
from wal_reader import read_live_db
CURSOR_FILE = BRIDGE / "state" / "db_cursor.json"
# 允许通过 --cursor-file 指定独立游标，避免多个读取者互相抢游标
for _i, _a in enumerate(sys.argv):
    if _a == "--cursor-file" and _i + 1 < len(sys.argv):
        CURSOR_FILE = Path(sys.argv[_i + 1])
        break
JSONL = BRIDGE / "messages.jsonl"

# wxid -> 显示名
NAME_MAP = {
    "wxid_YOUR_MAIN_ACCOUNT": "大号",
    "wxid_REDACTED": "Ran",
    "wxid_REDACTED": "GH",
    "wxid_YOUR_BOT_ACCOUNT": "GloHaven",
    "wxid_REDACTED": "&",
    "YOUR_MAIN_GROUP@chatroom": "[主群]",
}
# 会话 username -> 显示名
CHAT_MAP = {
    "YOUR_MAIN_GROUP@chatroom": "[主群]",
    "YOUR_TEST_GROUP@chatroom": "[测试群]",
    "wxid_YOUR_MAIN_ACCOUNT": "大号",
}
BOT_WXID = "wxid_YOUR_BOT_ACCOUNT"

def load_cursor():
    if CURSOR_FILE.exists():
        return json.loads(CURSOR_FILE.read_text())
    return {}

def save_cursor(c):
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(json.dumps(c, ensure_ascii=False))

def table_to_chat(table):
    """Msg_<md5> -> username"""
    h = table[4:]
    for uname in CHAT_MAP:
        if hashlib.md5(uname.encode()).hexdigest() == h:
            return uname
    return None

def main():
    keys = load_keys(WS / "all_keys.json")
    cursor = load_cursor()
    new_messages = []

    # 找到所有 message_*.db 的 key
    msg_dbs = [(rel, k) for rel, k in keys.items()
               if rel.startswith("message/message_") and rel.endswith(".db")
               and "fts" not in rel and "resource" not in rel and "biz_" not in rel]

    with tempfile.TemporaryDirectory() as tmpd:
        tmpd = Path(tmpd)
        for rel, key_hex in sorted(msg_dbs):
            src = DB_BASE / rel
            if not src.exists():
                continue
            # 用 WAL-aware reader：拷贝 DB+WAL，解密并合并，拿到实时数据
            try:
                dst = read_live_db(src, rel)
            except Exception:
                continue
            con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for t in tables:
                chat_uname = table_to_chat(t)
                if not chat_uname or chat_uname not in CHAT_MAP:
                    continue
                chat_name = CHAT_MAP[chat_uname]
                cur_key = f"{rel}:{t}"
                last_time, last_rowid = cursor.get(cur_key, (0, 0))
                try:
                    rows = con.execute(
                        f"SELECT rowid, create_time, local_type, message_content, "
                        f"WCDB_CT_message_content, source FROM [{t}] "
                        f"WHERE create_time > ? OR (create_time = ? AND rowid > ?) "
                        f"ORDER BY create_time, rowid",
                        (last_time, last_time, last_rowid)).fetchall()
                except Exception:
                    continue
                for rowid, ct, ltype, content_blob, ct_flag, src_field in rows:
                    # 只处理文本消息 (local_type=1, subtype in low 32 bits)
                    msg_type = (ltype or 0) & 0xFFFFFFFF
                    if msg_type != 1:
                        # 更新游标但跳过非文本
                        cursor[cur_key] = (ct, rowid)
                        continue
                    try:
                        text = decompress_field(content_blob, ct_flag or 0)
                    except Exception:
                        cursor[cur_key] = (ct, rowid)
                        continue
                    # 解析发送人。可靠识别自己发的消息：source 为可读 <msgsource>
                    # XML 即本号发出（群聊里自己发的消息没有 "wxid:\n" 前缀，
                    # 私聊表里双方消息都没有前缀，只能靠 source 区分）。
                    # 自己发的消息不再跳过，而是正确标记，供 3 条连发限制检查用；
                    # at_watchdog / wal_reader 会按 _own / wxid 跳过，不会误触发回复。
                    is_own = isinstance(src_field, str) and '<msgsource>' in src_field
                    if is_own:
                        sender_wxid = BOT_WXID
                        body = text
                    else:
                        # 群聊别人发的: "wxid_REDACTED:\n内容"
                        sender_wxid, _, body = text.partition(":\n")
                        if not body:
                            sender_wxid, _, body = text.partition(":")
                    sender = NAME_MAP.get(sender_wxid.strip(), sender_wxid.strip())
                    # 时间戳转可读
                    import datetime
                    dt = datetime.datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M")
                    msg = {
                        "time": dt,
                        "chat": chat_name,
                        "sender": sender,
                        "content": body.strip(),
                        "replied": False,
                        "_wxid": sender_wxid.strip(),
                        "_own": is_own,
                        "_ts": ct,
                        "_chat_uname": chat_uname,
                    }
                    new_messages.append(msg)
                    cursor[cur_key] = (ct, rowid)
            con.close()

    save_cursor(cursor)

    # 追加到 JSONL (去重)：文件写入永远去重，避免重复行。
    # --json 输出则返回未过滤的全部新消息（调用方自己负责去重）：
    # 否则 at_watchdog / cron 的 db_poller --json 看不到已在 JSONL 里的未回复消息，
    # 导致 @ 消息卡死无人处理（2026-10-06 [friend1] @MUSE 签到事故）。
    # --no-dedup 保留兼容：等同于默认行为（--json 不过滤）。
    to_append = new_messages
    if new_messages and JSONL.exists():
        existing = set()
        for line in JSONL.read_text().splitlines():
            try:
                m = json.loads(line)
                existing.add((m.get("time"), m.get("chat"), m.get("sender"), m.get("content")))
            except Exception:
                pass
        to_append = [m for m in new_messages
                     if (m["time"], m["chat"], m["sender"], m["content"]) not in existing]

    if to_append:
        with open(JSONL, "a") as f:
            for m in to_append:
                out = {k: v for k, v in m.items() if not k.startswith("_")}
                f.write(json.dumps(out, ensure_ascii=False) + "\n")

    if "--json" in sys.argv:
        # --json 输出保留 _ 开头的原始字段（供 wal_reader 做精确判定），
        # 写入 JSONL 文件时仍去掉，保持旧格式兼容。
        # 注意：这里输出未过滤的 new_messages，调用方自行去重。
        print(json.dumps(new_messages, ensure_ascii=False))
    else:
        print(f"new: {len(new_messages)}")
        for m in new_messages:
            print(f"  [{m['time']}] {m['chat']} <{m['sender']}>: {m['content'][:50]}")

if __name__ == "__main__":
    main()
