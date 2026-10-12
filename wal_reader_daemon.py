#!/usr/bin/env python3
"""
单一 WAL 读取进程 - 整个系统唯一读取微信数据库的进程

- 用 inotify 监听 WAL 文件变化（fallback 到轮询）
- 维护游标，只推进不回退
- 白名单过滤、@ 匹配、指纹去重都在这一层
- 写入 SQLite 队列
- 更新群上下文
"""

import os
import re
import sys
import time
import json
import fcntl
import sqlite3
import hashlib
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "frida"))
from msgqueue import (get_db, enqueue, get_cursor, set_cursor, set_cursor_nocommit,
                      get_recent_context, is_muted, set_mute, clear_mute,
                      detect_mute_command, MUTE_DEFAULT_SECONDS)
from wal_reader import read_live_db, WALChangedError, get_group_table, _cleanup_stale_temp, _unlink_db_files
from wxextract.keys import load_keys
from wxextract.messages import decompress_field

# 启动时清理上次崩溃残留的临时文件（tmpfs 占内存）
_cleanup_stale_temp()

# 白名单
WHITELIST_GROUPS = {
    "YOUR_MAIN_GROUP@chatroom",  # [主群]
    "YOUR_TEST_GROUP@chatroom",  # [测试群]
}
BIG_WXID = "wxid_YOUR_MAIN_ACCOUNT"  # 大号
SELF_WXID = "wxid_YOUR_BOT_ACCOUNT"  # 小号
TEST_GROUP = "YOUR_TEST_GROUP@chatroom"  # [测试群]（2026-10-07 用户放宽：大号在测试群说话不用@也回，可主动）

# @ 匹配
AT_KEYWORDS = ["@yourbot", "@muse"]

# 群上下文保留条数
CONTEXT_KEEP = 20

HEARTBEAT_FILE = "/tmp/wechat-bridge/wal_reader.heartbeat"

# D方案：rowid 高水位 + 重叠回读。create_time 实测乱序，不再参与游标判断。
# 每轮从 last_rowid - OVERLAP 开始读，靠源唯一键去重。游标只存 rowid HWM。
OVERLAP = 256

# 微信源库位置
DB_BASE = Path.home() / "xwechat_files/wxid_YOUR_BOT_ACCOUNT/db_storage"
WXEXTRACT_WS = Path.home() / ".local/share/wxextract"

# 会话 username -> 显示名（从 db_poller 迁移）
CHAT_MAP = {
    "YOUR_MAIN_GROUP@chatroom": "[主群]",
    "YOUR_TEST_GROUP@chatroom": "[测试群]",
    "wxid_YOUR_MAIN_ACCOUNT": "大号",
}
NAME_MAP = {
    "wxid_YOUR_MAIN_ACCOUNT": "大号",
    "wxid_REDACTED": "Ran",
    "wxid_REDACTED": "GH",
    "wxid_YOUR_BOT_ACCOUNT": "GloHaven",
    "wxid_REDACTED": "&",
    "YOUR_MAIN_GROUP@chatroom": "[主群]",
}

# flock 单例锁文件
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "state", "wal_reader.lock")
_lock_fd = None

def acquire_lock():
    """D方案：flock 保证唯一 reader。抢不到立即退出，不靠 keepalive 杀进程。"""
    global _lock_fd
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    _lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except (IOError, OSError):
        return False

def is_at_me(content):
    if not content:
        return False
    lower = content.lower()
    return any(k in lower for k in AT_KEYWORDS)

def should_process(chat, sender, content):
    """白名单过滤（旧接口，保留兼容）。新代码用 classify_message。"""
    # 服务号排除
    if "tencent" in chat.lower() or "news" in chat.lower():
        return False, False, False
    # 群聊白名单
    if chat in WHITELIST_GROUPS:
        return True, is_at_me(content), False
    # 大号单聊
    if chat == BIG_WXID or sender == BIG_WXID:
        return True, False, True
    # 其他人单聊：记录但不自动回复（转告逻辑在 worker 层）
    if "@chatroom" not in chat:
        return True, False, True
    return False, False, False


def _classify_image_need(db, chat, sender_wxid, msg_time):
    """图片是否需要理解。返回 (should_reply_level, needs_media)。
    2026-10-09 图片理解（截图兜底）：
    - 大号发的图 → EXPLICIT(1) + needs_media=1（大号的图永远看）
    - [主群]：任何人发的图 → EXPLICIT(1) + needs_media=1
      （2026-10-09 21:43 用户确认：取消@前提，有图就回；群里没人斗图）
    - 其他群：有人 @bot 后 5 分钟内发的图 → EXPLICIT(1) + needs_media=1
    - 其余 → (0, 0)，只做上下文占位
    2026-10-11 19:30 修复：排除 bot 自己发的贴纸（GUI 发送缺 <msgsource> 会被误判为 INBOUND）。
    检查 queue.db 最近 60 秒内是否有 STICKER 消息（status SENT/READY），有则跳过。
    """
    try:
        # 2026-10-11 19:30：先排除 bot 自己的贴纸
        # GUI 发的贴纸回读时可能缺 <msgsource>，sender_wxid 为空，会被误判为用户发的图
        if chat == "YOUR_MAIN_GROUP@chatroom":
            # 查 60 秒内是否有 bot 发的 STICKER
            recent_sticker = db.execute("""
                SELECT id FROM messages
                WHERE chat=? AND action='STICKER' AND direction='OUTBOUND'
                  AND created_at > ? AND status IN ('SENT', 'READY', 'SENDING')
                LIMIT 1
            """, (chat, msg_time - 60)).fetchone()
            if recent_sticker:
                # 这是 bot 刚发的贴纸，不是用户发的图，不触发读图
                return 0, 0
        
        if sender_wxid == BIG_WXID:
            return 1, 1
        # [主群]：无@前提，有图就看
        if chat == "YOUR_MAIN_GROUP@chatroom":
            return 1, 1
        if "@chatroom" in chat:
            rows = db.execute(
                """SELECT content FROM messages
                   WHERE chat=? AND sender=? AND msg_time > ?
                   ORDER BY msg_time DESC LIMIT 5""",
                (chat, sender_wxid, msg_time - 300)).fetchall()
            for (c,) in rows:
                if c and is_at_me(c):
                    return 1, 1
    except Exception:
        pass
    return 0, 0


# 2026-10-09 22:29 图片指代问题：消息明确指向之前发的图
# （如"刚才那张图右下角是谁"），且30分钟内有图，直接判 EXPLICIT。
# 用户要求：这种有上下文指向性的内容，允许回复图片。
IMAGE_REF_KEYWORDS = [
    "图片", "照片", "那张图", "这张图", "刚才的图", "最新的图",
    "右下角", "左下角", "右上角", "左上角", "中间那个",
    "皮套",  # VTuber 虚拟形象
]

def _is_image_question(db, chat, content, msg_time):
    """是否在问之前发的图。返回 True/False。"""
    if not content or "@chatroom" not in chat:
        return False
    # 1. 有没有提图
    has_ref = any(k in content for k in IMAGE_REF_KEYWORDS)
    if not has_ref:
        return False
    # 2. 10分钟内有没有图
    try:
        r = db.execute(
            """SELECT COUNT(*) FROM messages
               WHERE chat=? AND content='[图片]'
               AND msg_time > ? AND msg_time <= ?""",
            (chat, msg_time - 1800, msg_time)).fetchone()
        if r and r[0] > 0:
            return True
    except Exception:
        pass
    return False


# should_reply 三态：0=IGNORE（普通群聊），1=EXPLICIT（明确在对 Muse 说话），
# 2=CONTEXTUAL（疑似在和 Muse 说话，worker 让 Muse 自己判断，SKIP 或回复）
REPLY_IGNORE = 0
REPLY_EXPLICIT = 1
REPLY_CONTEXTUAL = 2

# 对话亲和 / 追问窗口（秒）
AFFINITY_WINDOW = 180   # Muse 回复某人后 3 分钟内，此人继续说话视为对话延续候选
FOLLOWUP_WINDOW = 300   # Muse 在群里说过话 5 分钟内，追问式内容视为候选

# 追问/补充式内容（弱规则，需配合“ Muse 最近在群里说过话”）
# 2026-10-08 23:59 优化：大幅扩充。“真的吗”这类对 bot 发言的直接反应之前漏掉
FOLLOWUP_HINTS = [
    "为什么", "为啥", "怎么", "如何", "怎么办",
    "那这个呢", "那呢", "还有呢", "然后呢", "接着", "继续",
    "能不能再", "再讲讲", "详细", "展开", "举个例子", "比如",
    "刚才那个", "刚才说的", "你刚说的", "上面说的", "之前说的",
    # 2026-10-08 新增：对 bot 发言的直接反应/质疑/追问
    "真的吗", "真的假的", "是吗", "真有", "有这么", "这么厉害",
    "什么意思", "啥意思", "细说", "展开说说", "具体说说",
    "然后", "后来呢", "最后呢", "结果呢",
    "我去", "卧槽", "牛啊", "牛逼",  # 惊叹式接话（弱信号，配合时间窗口）
]

# bot 刚说完话的强窗口（秒）：60 秒内任何人的短消息都视为可能的接话
# worker 会再做 LLM 判断，误判成本只是一次 worker 空转
FOLLOWUP_STRONG_WINDOW = 60


def _looks_like_question(content):
    """像是在提问/追问：以问号结尾，或含疑问语气词"""
    c = (content or "").strip()
    if not c:
        return False
    if c.endswith("?") or c.endswith("？"):
        return True
    # 短句 + 疑问词，大概率是在接话茬
    if len(c) <= 30 and any(w in c for w in ("吗", "呢", "吧", "嘛", "啊？", "？")):
        return True
    return False


def _last_explicit(db, chat_uname):
    """本群最近一条 EXPLICIT 消息的 (sender, msg_time)，没有返回 None"""
    return db.execute(
        "SELECT sender, msg_time FROM messages WHERE chat=? AND should_reply=1 "
        "AND typeof(msg_time)='integer' "
        "ORDER BY msg_time DESC LIMIT 1", (chat_uname,)).fetchone()


def _last_bot_time(db, chat_uname):
    """Muse 在本群最近发言时间（0 表示没有/解析不到）"""
    row = db.execute(
        "SELECT MAX(msg_time) FROM messages WHERE chat=? AND sender=? "
        "AND typeof(msg_time)='integer'",
        (chat_uname, SELF_WXID)).fetchone()
    return row[0] if row and row[0] else 0


def _bot_snippets(db, chat_uname, n=3, maxlen=40, minlen=15):
    """Muse 在本群最近 n 条发言的前缀片段（用于识别引用回复），best effort"""
    rows = db.execute(
        "SELECT content FROM messages WHERE chat=? AND sender=? "
        "ORDER BY msg_time DESC LIMIT ?", (chat_uname, SELF_WXID, n)).fetchall()
    return [r[0][:maxlen] for r in rows if r[0] and len(r[0]) >= minlen]


def _is_followup(content):
    low = (content or "").lower()
    return any(h in low for h in FOLLOWUP_HINTS)


def classify_reply(db, chat_uname, sender_wxid, content, msg_time):
    """
    读取层判定：这条消息是不是在对 Muse 说话。
    返回 0=IGNORE / 1=EXPLICIT / 2=CONTEXTUAL。只用原始 username/wxid。
    """
    low = (content or "").lower()

    # ---- 硬规则 → EXPLICIT ----
    if any(k in low for k in AT_KEYWORDS):      # 明确 @Muse / @GloHaven
        return REPLY_EXPLICIT
    # 消息里直接出现 muse（词边界，不匹配 museum/amuse 这类普通单词；
    # 中文语境下“问muse一下”能匹配，因为汉字不是 ASCII 字母数字）
    if re.search(r'(?<![a-z0-9])muse(?![a-z0-9])', low):
        return REPLY_EXPLICIT
    # 大号私聊直接回复（注意：只是 chat 为大号时；大号在群里说话走正常群规则，
    # 不要用 sender==BIG_WXID 误判）
    if chat_uname == BIG_WXID:
        return REPLY_EXPLICIT
    # 2026-10-07 用户放宽：测试群里大号说话不用 @ 也直接回（仅测试群，别处不行）
    if chat_uname == TEST_GROUP and sender_wxid == BIG_WXID:
        return REPLY_EXPLICIT
    # 回复/引用 Muse 的消息：内容含 Muse 近期发言片段（best effort，命中会打日志）
    for snip in _bot_snippets(db, chat_uname):
        if snip and snip in content:
            print(f"[wal_reader] reply-to-bot hit in {chat_uname}: {snip[:20]}",
                  flush=True)
            return REPLY_EXPLICIT

    # ---- 弱规则 → CONTEXTUAL（仅白名单群）----
    if chat_uname in WHITELIST_GROUPS:
        bot_t = _last_bot_time(db, chat_uname)
        exp = _last_explicit(db, chat_uname)
        # 对话亲和：Muse 回复过，且是回复在最后一条 EXPLICIT 之后，
        # 3 分钟内同一人继续说话 → 视为对话延续候选
        if (exp and bot_t and sender_wxid == exp[0]
                and bot_t > exp[1] and (msg_time - bot_t) <= AFFINITY_WINDOW):
            return REPLY_CONTEXTUAL
        # 追问：内容像追问/补充，且 Muse 5 分钟内在群里说过话
        if bot_t and (msg_time - bot_t) <= FOLLOWUP_WINDOW and _is_followup(content):
            return REPLY_CONTEXTUAL
        # 2026-10-08 新增：bot 刚说完（60 秒内），短疑问句直接视为接话候选
        # （worker 会再做 LLM 判断，误判成本低）
        if bot_t and (msg_time - bot_t) <= FOLLOWUP_STRONG_WINDOW \
                and _looks_like_question(content):
            return REPLY_CONTEXTUAL

    # 2026-10-09 00:02 用户决定：把回复权交给 LLM。
    # 白名单群的消息默认都给 CONTEXTUAL（由 worker 的 LLM 判断回不回），
    # 不再用关键词规则提前判死刑。worker 会批量 SKIP 过期消息防积压。
    if chat_uname in WHITELIST_GROUPS:
        return REPLY_CONTEXTUAL

    return REPLY_IGNORE


def classify_message(db, chat_uname, sender_wxid, content, msg_time):
    """
    完整分类，返回 (ok, level, is_dm)：
      ok: 是否在记录范围（False 直接丢弃）
      level: 0=IGNORE / 1=EXPLICIT / 2=CONTEXTUAL
      is_dm: 是否单聊（非大号私聊只记录不回复）
    """
    # 服务号排除
    if "tencent" in chat_uname.lower() or "news" in chat_uname.lower():
        return False, REPLY_IGNORE, False
    is_group = "@chatroom" in chat_uname
    is_big_dm = (chat_uname == BIG_WXID or sender_wxid == BIG_WXID)
    if is_group and chat_uname not in WHITELIST_GROUPS:
        return False, REPLY_IGNORE, False
    if not is_group and not is_big_dm:
        # 其他人私聊：记录但不自动回复
        return True, REPLY_IGNORE, True
    # 白名单群 或 大号私聊 → 三态判定
    level = classify_reply(db, chat_uname, sender_wxid, content, msg_time)
    # 2026-10-09 22:29 图片指代问题：如果在问之前发的图，直接升 EXPLICIT
    # （用户要求：有上下文指向性的图片问题，允许回复）
    if level != REPLY_EXPLICIT and _is_image_question(
            db, chat_uname, content, msg_time):
        level = REPLY_EXPLICIT
    return True, level, (not is_group)

def update_group_context(db, chat, sender_name, content, msg_time, commit=True):
    """更新群最近消息上下文。commit=False 时由调用方统一提交（D方案原子事务）。"""
    try:
        row = db.execute(
            "SELECT messages_json FROM group_context WHERE chat=?",
            (chat,)).fetchone()
        msgs = json.loads(row[0]) if row else []
        msgs.append({
            "sender": sender_name,
            "content": content[:200],
            "time": msg_time,
        })
        msgs = msgs[-CONTEXT_KEEP:]
        db.execute("""
            INSERT OR REPLACE INTO group_context (chat, messages_json, updated_at)
            VALUES (?, ?, ?)
        """, (chat, json.dumps(msgs, ensure_ascii=False), int(time.time())))
        if commit:
            db.commit()
    except Exception as e:
        print(f"[wal_reader] context update error: {e}", flush=True)

def heartbeat():
    os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
    with open(HEARTBEAT_FILE, "w") as f:
        f.write(str(int(time.time())))

def _table_to_chat(table):
    """Msg_<md5> -> username"""
    h = table[4:]
    for uname in CHAT_MAP:
        if hashlib.md5(uname.encode()).hexdigest() == h:
            return uname
    return None


def _parse_appmsg_title(text):
    """从 appmsg XML 提取 <title>，失败返回 None。"""
    import re
    m = re.search(r"<title>(.*?)</title>", text, re.DOTALL)
    if not m:
        return None
    title = m.group(1).strip()
    # XML 反转义
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        title = title.replace(a, b)
    return title or None


def _parse_row(source_db, table, row):
    """解析微信源行 -> dict。row: (rowid, local_id, server_id, sort_seq,
    create_time, local_type, message_content, ct_flag, source)。
    文字(type=1)正常解析；非文字转可读摘要（is_event=True，只做上下文）。
    完全无法解析返回 None（但游标照常推进）。"""
    (rowid, local_id, server_id, sort_seq, ct, ltype,
     content_blob, ct_flag, src_field) = row
    msg_type = (ltype or 0) & 0xFFFFFFFF
    # 发送人识别：source 含 <msgsource> XML 即本号发出
    is_own = isinstance(src_field, str) and '<msgsource>' in src_field
    sender_wxid = SELF_WXID if is_own else ""

    def _base(content, sender_wxid=sender_wxid, is_event=False):
        return {
            "rowid": rowid,
            "local_id": local_id,
            "server_id": server_id,
            "sort_seq": sort_seq,
            "msg_time": ct,
            "sender_wxid": sender_wxid,
            "sender_name": NAME_MAP.get(sender_wxid, sender_wxid),
            "content": content,
            "is_own": is_own,
            "is_event": is_event,  # True=非文字事件，只做上下文不触发回复
            "msg_type": msg_type,  # 2026-10-09 图片理解：透出原始类型供分类
            "source": {
                "source_db": source_db,
                "source_table": table,
                "local_id": local_id,
                "server_id": server_id,
                "sort_seq": sort_seq,
                "rowid": rowid,
            },
        }

    # --- 非文字消息 ---
    # 注意：appmsg 的 <msgsource> 不可靠（如拍一拍通知也带该标签），
    # 不能用 is_own 判定发送人。优先从内容解析，解析不出则标 unknown。
    if msg_type != 1:
        try:
            text = decompress_field(content_blob, ct_flag or 0)
        except Exception:
            return None
        # 尝试从 "wxid:\n" 前缀取发送人
        _sw, sep, _body = text.partition(":\n")
        if not sep:
            _sw, sep, _body = text.partition(":")
        _sw = _sw.strip()
        if _sw.startswith("wxid_"):
            sender_wxid = _sw
        else:
            # 无前缀：拍一拍从 title 取拍人，其他标 unknown
            sender_wxid = ""
        if msg_type == 49:
            # appmsg：拍一拍(type 62)、引用回复(type 57)、链接、小程序等
            title = _parse_appmsg_title(text)
            if title:
                if "拍了拍" in title:
                    # 从 "\"X\" 拍了拍 \"Y\"" 取 X
                    import re as _re
                    _m = _re.match(r'"([^"]+)"\s*拍了拍', title)
                    patter = _m.group(1) if _m else ""
                    # 反查 wxid（备注名 -> wxid）
                    _rev = {v: k for k, v in NAME_MAP.items()}
                    if patter and patter in _rev:
                        sender_wxid = _rev[patter]
                    return _base(f"[拍一拍] {title}", sender_wxid or patter or "unknown",
                                 is_event=True)
                return _base(f"[应用消息] {title}", sender_wxid or "unknown",
                             is_event=True)
            return _base("[应用消息]", sender_wxid or "unknown", is_event=True)
        elif msg_type == 3:
            return _base("[图片]", sender_wxid or "unknown", is_event=True)
        elif msg_type == 47:
            # 2026-10-11 F5: 解析表情包 XML，提取下载信息（偷表情包用）
            # XML 格式: <msg><emoji md5="..." cdnurl="..." ... /></msg>
            # 2026-10-11 T1: 额外记录 sticker_events（最小改动，不阻塞主流程）
            _se_md5 = None
            _se_product = None
            _se_len = None
            _se_missing = None
            try:
                import re as _re47
                import threading as _th47
                import time as _t47
                _m = _re47.search(r'<emoji[^>]*>', text)
                if _m:
                    _tag = _m.group(0)
                    _md5 = _re47.search(r'md5="([^"]+)"', _tag)
                    _cdn = _re47.search(r'cdnurl="([^"]+)"', _tag)
                    _thumb = _re47.search(r'thumburl="([^"]+)"', _tag)
                    _prod = _re47.search(r'productid="([^"]+)"', _tag)
                    _lenm = _re47.search(r'len="(\d+)"', _tag)
                    if _md5:
                        _se_md5 = _md5.group(1)
                        if _prod:
                            _se_product = _prod.group(1)
                        if _lenm:
                            _se_len = int(_lenm.group(1))
                        # 异步下载，不阻塞主流程
                        def _steal():
                            try:
                                import sticker_steal
                                sticker_steal.steal(
                                    md5=_md5.group(1),
                                    cdnurl=_cdn.group(1) if _cdn else None,
                                    thumburl=_thumb.group(1) if _thumb else None,
                                    sender=sender_wxid,
                                    chat=source_db,  # 传入 source 标识
                                )
                            except Exception:
                                pass
                        _th47.Thread(target=_steal, daemon=True).start()
                    else:
                        _se_missing = "no_md5_in_xml"
                else:
                    _se_missing = "no_emoji_tag"
                # 写 sticker_events（独立表，不影响队列）
                try:
                    import sqlite3 as _sq47
                    _db47 = _sq47.connect('~/workspace/wechat-bridge/queue.db', timeout=5)
                    _msg_id = str(server_id) if server_id else str(local_id)
                    _dir = "OUTBOUND" if is_own else "INBOUND"
                    # 2026-10-12: 存 cdnurl，失败可重试
                    _cdn_url = _cdn.group(1) if _cdn else None
                    _db47.execute("""
                        INSERT INTO sticker_events 
                        (message_id, direction, md5, product_id, file_length, timestamp, missing_reason, cdnurl, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (_msg_id, _dir, _se_md5, _se_product, _se_len, ct, _se_missing, _cdn_url, int(_t47.time())))
                    _db47.commit()
                    _db47.close()
                except Exception:
                    pass
            except Exception:
                pass
            return _base("[表情]", sender_wxid or "unknown", is_event=True)
        elif msg_type == 10000:
            # 系统消息：去掉 XML 标签，只留文本
            import re as _re2
            body = _re2.sub(r"<[^>]+>", "", text).strip()
            # 去掉多余空白
            body = _re2.sub(r"\s+", " ", body)
            if body:
                return _base(f"[系统] {body[:120]}", "system", is_event=True)
            return None
        else:
            # 其他未知类型：占位，保证游标推进但不污染上下文
            return _base(f"[未知类型{msg_type}]", sender_wxid or "unknown",
                         is_event=True)

    # --- 文字消息（原逻辑） ---
    try:
        text = decompress_field(content_blob, ct_flag or 0)
    except Exception:
        return None
    if is_own:
        sender_wxid = SELF_WXID
        body = text
    else:
        sender_wxid, sep, body = text.partition(":\n")
        if not sep:
            sender_wxid, sep, body = text.partition(":")
        sender_wxid = sender_wxid.strip()
        # 2026-10-10 修：切分失败（无冒号）或切出来的"发送人"不是 wxid 格式时，
        # 不要把整段正文塞进 sender_wxid（曾造出 #2542195 这类垃圾行：
        # sender=整段文字、content 为空）。GUI 发出的消息回读时可能缺
        # <msgsource> 且格式错位，此时保留正文、发送人标 unknown。
        if not sep or not sender_wxid.startswith("wxid_"):
            body = text
            sender_wxid = ""
    return {
        "rowid": rowid,
        "local_id": local_id,
        "server_id": server_id,
        "sort_seq": sort_seq,
        "msg_time": ct,
        "sender_wxid": sender_wxid,
        "sender_name": NAME_MAP.get(sender_wxid, sender_wxid),
        "content": body.strip(),
        "is_own": is_own,
        "is_event": False,
        "source": {
            "source_db": source_db,
            "source_table": table,
            "local_id": local_id,
            "server_id": server_id,
            "sort_seq": sort_seq,
            "rowid": rowid,
        },
    }


def _fetch_source_rows():
    """慢 I/O（事务外）：读微信源库，返回 [(chat_uname, parsed), ...] 和
    {cursor_key: max_rowid}。WAL 世代变化时整轮丢弃重试一次。
    2026-10-11 R3（借鉴 wechat-ai）：SQLite 可恢复错误（locked/corrupt/
    not a database）时重开连接重试一次；HWM 不重置，靠去重保证不漏不重。
    """
    try:
        keys = load_keys(WXEXTRACT_WS / "all_keys.json")
    except Exception as e:
        print(f"[wal_reader] load_keys failed: {e}", flush=True)
        return [], {}
    msg_dbs = [(rel, k) for rel, k in keys.items()
               if rel.startswith("message/message_") and rel.endswith(".db")
               and "fts" not in rel and "resource" not in rel and "biz_" not in rel]

    # 读游标（只读连接，不持有写锁）
    db = get_db()
    hwm = {}
    try:
        for rel, _ in sorted(msg_dbs):
            for uname in CHAT_MAP:
                table = get_group_table(uname)
                key = f"ingest:{rel}:{table}"
                hwm[key] = int(get_cursor(db, key, "0"))
    finally:
        db.close()

    pending = []   # (chat_uname, parsed)
    max_rowids = {}
    for rel, _ in sorted(msg_dbs):
        src = DB_BASE / rel
        if not src.exists():
            continue
        # WAL 世代变化 → 整轮丢弃重试一次
        # R3：SQLite 可恢复错误也重试（不重置 HWM）
        # 2026-10-11 修：malformed 可能是持久损坏，只重试一次，持续出现则记异常
        _RECOVERABLE_ONCE = ("database is locked", "database schema has changed")
        _RECOVERABLE_NEVER = ("database disk image is malformed",)
        for attempt in range(2):
            try:
                merged = read_live_db(src, rel)
                break
            except WALChangedError as e:
                print(f"[wal_reader] {e}, retrying", flush=True)
                if attempt == 1:
                    merged = None
                time.sleep(1)
            except Exception as e:
                _msg = str(e).lower()
                if any(s in _msg for s in _RECOVERABLE_NEVER):
                    print(f"[wal_reader] DB损坏(不重试): {e}", flush=True)
                    merged = None
                    break
                if any(s in _msg for s in _RECOVERABLE_ONCE) and attempt == 0:
                    print(f"[wal_reader] 可恢复DB错误，重试: {e}", flush=True)
                    time.sleep(1)
                    continue
                print(f"[wal_reader] 源库读取失败: {e}", flush=True)
                merged = None
                break
        if not merged:
            continue
        try:
            con = sqlite3.connect(f"file:{merged}?mode=ro", uri=True)
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'Msg_%'")]
            for t in tables:
                chat_uname = _table_to_chat(t)
                if not chat_uname or chat_uname not in CHAT_MAP:
                    continue
                key = f"ingest:{rel}:{t}"
                last_rowid = hwm.get(key, 0)
                try:
                    rows = con.execute(
                        f"SELECT rowid, local_id, server_id, sort_seq, "
                        f"create_time, local_type, message_content, "
                        f"WCDB_CT_message_content, source FROM [{t}] "
                        f"WHERE rowid > ? ORDER BY rowid",
                        (last_rowid - OVERLAP,)).fetchall()
                except Exception:
                    continue
                cur_max = last_rowid
                for r in rows:
                    if r[0] > cur_max:
                        cur_max = r[0]
                    parsed = _parse_row(rel, t, r)
                    if parsed:
                        pending.append((chat_uname, parsed))
                # 只推进不回退（本轮事务提交时才真正写游标）
                if cur_max > max_rowids.get(key, last_rowid):
                    max_rowids[key] = cur_max
            con.close()
        finally:
            _unlink_db_files(merged)
    return pending, max_rowids


def _migrate_old_cursor(db):
    """一次性迁移：旧 JSON 游标 (create_time, rowid) -> 新 SQLite rowid HWM。
    只取 rowid 部分，create_time 丢弃（乱序不可信）。"""
    old_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "state", "db_cursor.wal.json")
    if not os.path.exists(old_path):
        return
    try:
        old = json.loads(open(old_path).read())
    except Exception:
        return
    migrated = 0
    for key, val in old.items():
        # key 格式: "message/message_0.db:Msg_<md5>"
        if ":" not in key:
            continue
        new_key = f"ingest:{key}"
        if get_cursor(db, new_key, None) is not None:
            continue  # 已迁移过
        try:
            _ct, rowid = val
            set_cursor_nocommit(db, new_key, str(int(rowid)))
            migrated += 1
        except Exception:
            pass
    if migrated:
        db.commit()
        print(f"[wal_reader] migrated {migrated} cursors from JSON", flush=True)
    # 迁移完改名，防止重复处理
    try:
        os.rename(old_path, old_path + ".migrated")
    except Exception:
        pass


def poll_once():
    """
    D方案单次轮询：
    1. 慢 I/O 在事务外：读源库 -> pending 列表（不持有 queue.db 写锁）
    2. 快事务：BEGIN IMMEDIATE -> classify+enqueue(INSERT OR IGNORE)
       -> 推进游标 -> COMMIT
    崩溃在任何时刻：游标没推进，下轮从 overlap 处重读，靠源唯一键去重。
    消息落库和游标推进是同一个原子提交单元。
    """
    pending, max_rowids = _fetch_source_rows()
    if not pending and not max_rowids:
        return 0

    db = get_db()
    # 注意：旧游标迁移已移到 main() 启动时一次性执行，
    # 不能在这里做——否则首轮 fetch 会先看到 HWM=0 全量读历史。

    count = 0
    db.execute("BEGIN IMMEDIATE")
    try:
        for chat_uname, p in pending:
            chat = chat_uname
            sender = p["sender_wxid"]
            content = p["content"]
            msg_time = p["msg_time"]
            sender_name = p["sender_name"]
            chat_name = CHAT_MAP.get(chat_uname, chat_uname)
            rowid = p["rowid"]
            key = f"ingest:{p['source']['source_db']}:{p['source']['source_table']}"
            last_rowid = int(get_cursor(db, key, "0"))

            if rowid <= last_rowid:
                # overlap 区：三级检查
                # 1. 源唯一键存在 → 已入库，跳过
                exists = db.execute(
                    """SELECT 1 FROM messages WHERE source_db=? AND source_table=?
                       AND source_local_id=? LIMIT 1""",
                    (p["source"]["source_db"], p["source"]["source_table"],
                     p["source"]["local_id"])).fetchone()
                if exists:
                    continue
                # 2. heal：找同聊天同发送者同内容的老行（无 source key 的），
                #    把源信息补上去，不插新行。处理手动补回时时间戳估错
                #    导致 msg_hash 对不上的情况。
                old = db.execute(
                    """SELECT id FROM messages
                       WHERE chat=? AND sender=? AND content=?
                         AND source_local_id IS NULL
                       ORDER BY ABS(msg_time - ?) LIMIT 1""",
                    (chat, sender, content, msg_time)).fetchone()
                if old:
                    db.execute(
                        """UPDATE messages SET source_db=?, source_table=?,
                           source_local_id=?, source_server_id=?, sort_seq=?,
                           source_rowid=?, updated_at=? WHERE id=?""",
                        (p["source"]["source_db"], p["source"]["source_table"],
                         p["source"]["local_id"], p["source"]["server_id"],
                         p["source"]["sort_seq"], p["source"]["rowid"],
                         int(time.time()), old[0]))
                    continue
                # 3. 真没见过 → 掉到下面走完整 classify+insert（理论上不该发生）

            # 本号发出的消息：OUTBOUND，供 sendd 验证，不进入回复队列
            if p["is_own"]:
                # D方案（2026-10-06）：OUTBOUND 去重。bot 发消息会产生两行：
                # 1. 逻辑行（worker 写的 READY -> sendd 标 SENT，reply_content 有正文）
                # 2. WAL 回读行（daemon 从微信源库读到的真实发出消息）
                # 先尝试关联到逻辑行：同群、10分钟内、reply_content 包含本消息
                # 前30字。找到则回填 source 字段，不插新行，避免上下文重复展示。
                linked = db.execute(
                    """SELECT id FROM messages
                       WHERE chat=? AND status IN ('SENT','READY','SENDING')
                         AND reply_content != ''
                         AND msg_time >= ?
                         AND (reply_content LIKE ? OR ? LIKE '%' || substr(reply_content,1,30) || '%')
                       ORDER BY msg_time DESC LIMIT 1""",
                    (chat, msg_time - 600, f"%{content[:30]}%", content)
                ).fetchone()
                if linked:
                    db.execute(
                        """UPDATE messages SET source_db=?, source_table=?,
                           source_local_id=?, source_server_id=?, sort_seq=?,
                           source_rowid=?, updated_at=? WHERE id=?""",
                        (p["source"]["source_db"], p["source"]["source_table"],
                         p["source"]["local_id"], p["source"]["server_id"],
                         p["source"]["sort_seq"], p["source"]["rowid"],
                         int(time.time()), linked[0]))
                    continue
                mid = enqueue(db, chat, chat_name, sender, sender_name,
                              content, msg_time, is_at=False,
                              is_dm=False, should_reply=0, direction='OUTBOUND',
                              source=p["source"], commit=False,
                              # 2026-10-09：OUTBOUND 验证记录直接 SENT，
                              # 不标 NEW（NEW 的 OUTBOUND 是无主僵尸）
                              status='SENT')
                if mid:
                    count += 1
                continue

            # 非文字事件（拍一拍/图片/表情/系统消息）：只做上下文，不分类不回复
            # 2026-10-09 图片理解：msg_type=3 的图片若需理解（大号发的 / 主群有图就看 / @bot），
            # 则判 EXPLICIT + needs_media=1，走截图兜底链路；其余图片仍只做上下文。
            # 2026-10-09 21:43 表情包自动看图已取消（用户要求）：msg_type=47 只做上下文。
            if p.get("is_event"):
                img_level, img_media = 0, 0
                if p.get("msg_type") == 3 and "@chatroom" in chat and sender:
                    img_level, img_media = _classify_image_need(
                        db, chat, sender, msg_time)
                if "@chatroom" in chat and sender:
                    mid = enqueue(db, chat, chat_name, sender, sender_name,
                                  content, msg_time, is_at=False,
                                  is_dm=False, should_reply=img_level,
                                  source=p["source"], commit=False,
                                  needs_media=img_media)
                    if mid:
                        count += 1
                        if img_media:
                            print(f"[wal_reader] image needs_media=1 "
                                  f"#{mid} from {sender_name}", flush=True)
                            # 2026-10-09 图片理解：入库即后台触发截图，
                            # worker 来处理时 media_path 已就绪。不阻塞入库循环。
                            try:
                                import subprocess
                                subprocess.Popen(
                                    ["python3", "capture_image.py",
                                     chat, str(mid)],
                                    cwd="~/workspace/wechat-bridge",
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True
                                )
                            except Exception as e:
                                print(f"[wal_reader] 图片截图触发失败: {e}",
                                      flush=True)
                            # 2026-10-11 Step 1：资源索引（message_resource.db →
                            # DAT 文件映射）。只做定位，不解密；失败不影响截图兜底。
                            # 后台 detached，避免阻塞入库循环。
                            try:
                                import subprocess
                                subprocess.Popen(
                                    ["python3", "image_index.py",
                                     "index", str(mid)],
                                    cwd="~/workspace/wechat-bridge",
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True
                                )
                            except Exception as e:
                                print(f"[wal_reader] 图片索引触发失败: {e}",
                                      flush=True)
                continue

            # 步骤0 静默开关（2026-10-06）：入队环节用规则识别禁言/恢复指令，
            # 不靠 LLM，不受 5 分钟上下文窗口限制。只有大号的指令有效。
            mute_cmd = detect_mute_command(sender, content)
            if mute_cmd == "mute":
                set_mute(db, int(time.time()) + MUTE_DEFAULT_SECONDS,
                         sender, f"关键词: {content[:20]}", commit=False)
                print(f"[wal_reader] MUTE set by {sender_name} "
                      f"({content[:20]}), 60min", flush=True)
                # 指令消息本身只做上下文，不触发回复
                if "@chatroom" in chat and sender:
                    mid = enqueue(db, chat, chat_name, sender, sender_name,
                                  content, msg_time, is_at=False,
                                  is_dm=False, should_reply=0,
                                  source=p["source"], commit=False)
                    if mid:
                        count += 1
                continue
            elif mute_cmd == "unmute":
                clear_mute(db, commit=False)
                print(f"[wal_reader] MUTE cleared by {sender_name} "
                      f"({content[:20]})", flush=True)
                if "@chatroom" in chat and sender:
                    mid = enqueue(db, chat, chat_name, sender, sender_name,
                                  content, msg_time, is_at=False,
                                  is_dm=False, should_reply=0,
                                  source=p["source"], commit=False)
                    if mid:
                        count += 1
                continue

            ok, level, is_dm = classify_message(db, chat, sender, content, msg_time)
            if not ok:
                continue

            if "@chatroom" in chat:
                update_group_context(db, chat, sender_name, content, msg_time,
                                     commit=False)

            # 白名单群所有消息都入队（level=0 只做上下文，不触发回复）
            if level >= 1 or is_dm or ("@chatroom" in chat and ok):
                mid = enqueue(db, chat, chat_name, sender, sender_name,
                              content, msg_time, is_at=is_at_me(content),
                              is_dm=is_dm, should_reply=level,
                              source=p["source"], commit=False)
                if mid:
                    count += 1
                    if level >= 1:
                        print(f"[wal_reader] enqueued #{mid} level={level} "
                              f"from {sender_name}: {content[:30]}", flush=True)
                    # 2026-10-09 21:35 @bot 补图：收到 @ 消息时，往前找发送人
                    # 2 分钟内发的图/表情，补标 needs_media=1 并触发截图。
                    # 覆盖"先发图、后@ "的顺序（之前只处理"先@、后发图"）。
                    if is_at_me(content) and "@chatroom" in chat:
                        try:
                            img = db.execute(
                                """SELECT id FROM messages
                                   WHERE chat=? AND sender=? AND content IN ('[图片]','[表情]')
                                   AND msg_time > ? AND msg_time <= ?
                                   AND needs_media=0
                                   ORDER BY msg_time DESC LIMIT 1""",
                                (chat, sender, msg_time - 120, msg_time)).fetchone()
                            if img:
                                img_id = img[0]
                                db.execute(
                                    "UPDATE messages SET should_reply=1, needs_media=1, "
                                    "updated_at=? WHERE id=?",
                                    (int(time.time()), img_id))
                                print(f"[wal_reader] @补图 #{img_id} needs_media=1",
                                      flush=True)
                                import subprocess
                                subprocess.Popen(
                                    ["python3", "capture_image.py",
                                     chat, str(img_id)],
                                    cwd="~/workspace/wechat-bridge",
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True)
                        except Exception as e:
                            print(f"[wal_reader] @补图失败: {e}", flush=True)
                    # 2026-10-07 小游戏路由：不碰 should_reply，只识别游戏输入。
                    # 游戏输入 → 落 game_event；需要回复时 manager 已写好
                    # 合成 GAME_REPLY；suppress_original 时原消息标 SKIPPED
                    #（游戏回复替代普通回复，避免双回）。
                    if "@chatroom" in chat:
                        try:
                            from games import store as _gstore
                            from games import router as _grouter
                            _gstore.ensure_schema(db)
                            _muted, _ = is_muted(db, int(time.time()))
                            if not _muted:
                                _groute = _grouter.route_message(
                                    db, chat, sender, sender_name,
                                    content, mid, msg_time,
                                    should_reply=level)
                                if _groute and _groute.get(
                                        "suppress_original"):
                                    db.execute(
                                        "UPDATE messages SET status='SKIPPED',"
                                        " hold_reason='game' WHERE id=?",
                                        (mid,))
                        except Exception as _ge:
                            print(f"[wal_reader] game_router 跳过: {_ge}",
                                  flush=True)
                    # 2026-10-10 积分生图：@muse 生成图片 <prompt>
                    # 确定性流程：查积分→不够直接回@提示；够则扣费建 IMAGE_GEN 任务。
                    # 接管后原消息标 SKIPPED，避免 worker 再回一条。
                    if "@chatroom" in chat and level in (1, 2):
                        try:
                            from games import image_gen as _igen
                            _prompt = _igen.parse_request(content)
                            if _prompt is not None:
                                _ires = _igen.handle_request(
                                    db, chat, chat_name, sender,
                                    sender_name, _prompt, mid, msg_time,
                                    should_reply=level)
                                if _ires.get("suppress_original"):
                                    db.execute(
                                        "UPDATE messages SET status='SKIPPED',"
                                        " hold_reason='image_gen' WHERE id=?",
                                        (mid,))
                        except Exception as _ie:
                            print(f"[wal_reader] image_gen 跳过: {_ie}",
                                  flush=True)
                    # 2026-10-10 群梗彩蛋（功能 B）：关键词触发（旁路，不压原消息）
                    if "@chatroom" in chat:
                        try:
                            import meme as _meme
                            _meme.check_meme(db, chat, chat_name, sender,
                                             sender_name, content, mid,
                                             msg_time)
                        except Exception as _me:
                            print(f"[wal_reader] meme 跳过: {_me}",
                                  flush=True)
                    # 2026-10-10 群统计：按需命令（旁路，不压原消息）
                    if "@chatroom" in chat:
                        try:
                            import stats as _stats
                            _stats.handle_stats_cmd(
                                db, chat, sender, sender_name,
                                content, mid, msg_time)
                        except Exception as _se:
                            print(f"[wal_reader] stats 跳过: {_se}",
                                  flush=True)
                    # 2026-10-10 聊天记录问答：自然语言查群史（旁路，不压原消息）
                    if "@chatroom" in chat:
                        try:
                            import digest as _digest
                            _digest.handle_history_qa(
                                db, chat, sender, sender_name,
                                content, mid, msg_time)
                        except Exception as _de:
                            print(f"[wal_reader] digest_qa 跳过: {_de}",
                                  flush=True)

        # 推进游标：只推进不回退，和上面的入库是同一个事务
        for key, max_rid in max_rowids.items():
            cur = int(get_cursor(db, key, "0"))
            if max_rid > cur:
                set_cursor_nocommit(db, key, str(max_rid))

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return count

def main():
    # D方案：flock 单例。抢不到锁说明已有实例在跑，直接退出。
    # 进程死亡锁自动释放，无 stale-lock 问题。不再靠 keepalive 杀多实例。
    if not acquire_lock():
        print("[wal_reader] another instance holds the lock, exiting",
              flush=True)
        sys.exit(1)
    print("[wal_reader] lock acquired, single reader process", flush=True)
    # D方案：启动时先迁移旧游标，再开始 fetch。顺序不能反，
    # 否则首轮 fetch 看到 HWM=0 会全量读历史（2026-10-06 曾因此插入 489 条）。
    try:
        _db = get_db()
        _migrate_old_cursor(_db)
        _db.close()
    except Exception as e:
        print(f"[wal_reader] cursor migration failed: {e}", flush=True)
    # 尝试 inotify（2026-10-09 用户批准：WAL inotify 事件驱动替代纯轮询）
    # 监控 message/ 目录的 WAL 写入，触发即时 poll；2s 轮询保留为兜底
    use_inotify = False
    inotify_watcher = None
    msg_dir = os.path.expanduser(
        "~/xwechat_files/wxid_YOUR_BOT_ACCOUNT/db_storage/message")
    try:
        import inotify.adapters
        if os.path.isdir(msg_dir):
            inotify_watcher = inotify.adapters.Inotify()
            # 只关心 WAL/DB 文件的写入关闭（消息落盘）
            inotify_watcher.add_watch(
                msg_dir,
                mask=(inotify.constants.IN_CLOSE_WRITE |
                      inotify.constants.IN_MODIFY))
            use_inotify = True
            print("[wal_reader] inotify watching message/ dir", flush=True)
        else:
            print(f"[wal_reader] msg_dir not found: {msg_dir}", flush=True)
    except ImportError:
        print("[wal_reader] inotify not available, using polling",
              flush=True)
    except Exception as e:
        print(f"[wal_reader] inotify setup failed: {e}", flush=True)

    # 找到 WAL 文件路径（旧注释保留）
    account_dir = os.path.expanduser(
        "~/xwechat_files/wxid_YOUR_BOT_ACCOUNT")
    poll_interval = 2  # 兜底轮询间隔（inotify 漏事件时）

    last_poll = 0
    while True:
        try:
            heartbeat()
            now = time.time()
            triggered = False
            # inotify 事件检查（非阻塞，100ms 超时）
            if use_inotify and inotify_watcher:
                try:
                    for event in inotify_watcher.event_gen(
                            yield_nones=False, timeout_s=0.1):
                        (_, type_names, path, filename) = event
                        fn = filename.decode() if isinstance(
                            filename, bytes) else str(filename)
                        # 只关心 message_*.db 的 WAL/DB 写入
                        if fn.startswith("message_") and (
                                fn.endswith(".db-wal") or
                                fn.endswith(".db")):
                            triggered = True
                            break
                except Exception as e:
                    print(f"[wal_reader] inotify read error: {e}",
                          flush=True)
            if triggered or (now - last_poll >= poll_interval):
                n = poll_once()
                last_poll = now
                if n > 0:
                    print(f"[wal_reader] {n} new messages enqueued"
                          f"{' (inotify)' if triggered else ''}",
                          flush=True)
            if not use_inotify:
                time.sleep(1)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[wal_reader] error: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
