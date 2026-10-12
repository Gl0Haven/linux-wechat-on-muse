#!/usr/bin/env python3
"""表情包选择（2026-10-11 F5）。

根据聊天语境选择表情包。v1 用关键词匹配，v2 可上语义向量。
使用限制：每群每 10 分钟最多发 3 张，防刷屏。

用法：
  python3 sticker.py pick <chat> "<recent_text>"
  python3 sticker.py list
  python3 sticker.py send <chat> <category>  # 入队 STICKER 消息
"""
import os
import random
import sqlite3
import sys
import time

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
STICKER_DIR = os.path.join(BRIDGE, "stickers")
DB_PATH = os.path.join(BRIDGE, "queue.db")

# 关键词 → 表情类别
KEYWORDS = {
    "happy": ["哈哈", "笑死", "好笑", "开心", "快乐", "嘿嘿", "嘻嘻"],
    "sad": ["呜呜", "难过", "伤心", "哭", "唉", "叹气"],
    "angry": ["气死", "哼", "怒", "烦", "讨厌"],
    "surprise": ["哇", "震惊", "居然", "天哪", "我去"],
    "agree": ["对对", "同意", "没错", "是的", "嗯嗯", "好"],
    "disagree": ["不对", "不是", "错了", "别", "不"],
    "hello": ["你好", "嗨", "hi", "hello", "来了"],
    "bye": ["拜拜", "走了", "晚安", "再见", "bye"],
}

# 频率限制：每群 10 分钟最多 3 张
RATE_LIMIT_WINDOW = 600
RATE_LIMIT_MAX = 3


def _db():
    return sqlite3.connect(DB_PATH, timeout=10)


def check_rate_limit(chat):
    """检查是否超频。返回 True=允许发送。
    2026-10-11：测试群豁免频率限制（用户要求）。
    2026-10-12 修：把排队中未发的 STICKER 也算进去，堵并发 worker 的竞态
    （检查→入队→记录之间有延迟，快速连发时检查会失效）。"""
    # 测试群豁免
    if chat == "YOUR_TEST_GROUP@chatroom":
        return True
    db = _db()
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS sticker_log (
                chat TEXT, ts INTEGER
            )""")
        now = int(time.time())
        n = db.execute(
            "SELECT COUNT(*) FROM sticker_log WHERE chat=? AND ts>?",
            (chat, now - RATE_LIMIT_WINDOW)).fetchone()[0]
        # 排队中还没发的也算（堵竞态）
        try:
            pending = db.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE chat=? AND action='STICKER'
                   AND status IN ('READY','GENERATING','SENDING')
                   AND created_at>?""",
                (chat, now - RATE_LIMIT_WINDOW)).fetchone()[0]
        except Exception:
            pending = 0
        return (n + pending) < RATE_LIMIT_MAX
    finally:
        db.close()


def record_send(chat):
    db = _db()
    try:
        db.execute("INSERT INTO sticker_log (chat, ts) VALUES (?, ?)",
                   (chat, int(time.time())))
        db.commit()
    finally:
        db.close()


def pick_sticker(text):
    """根据文本选表情类别。返回 (category, path) 或 (None, None)。"""
    if not text:
        return None, None
    scores = {}
    for cat, kws in KEYWORDS.items():
        for kw in kws:
            if kw in text:
                scores[cat] = scores.get(cat, 0) + 1
    if not scores:
        return None, None
    # 取最高分
    best = max(scores, key=scores.get)
    # 从该类别随机选一张
    cat_dir = os.path.join(STICKER_DIR, best)
    if not os.path.isdir(cat_dir):
        return None, None
    files = [f for f in os.listdir(cat_dir)
             if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))]
    if not files:
        return None, None
    return best, os.path.join(cat_dir, random.choice(files))


def list_stickers():
    result = {}
    for cat in os.listdir(STICKER_DIR):
        d = os.path.join(STICKER_DIR, cat)
        if os.path.isdir(d):
            files = [f for f in os.listdir(d)
                     if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))]
            result[cat] = len(files)
    return result


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "pick" and len(sys.argv) >= 4:
        cat, path = pick_sticker(sys.argv[3])
        print(f"{cat}: {path}" if cat else "no match")
    elif cmd == "list":
        for cat, n in list_stickers().items():
            print(f"{cat}: {n}")
    elif cmd == "send" and len(sys.argv) >= 4:
        chat, cat = sys.argv[2], sys.argv[3]
        if not check_rate_limit(chat):
            print("rate limited")
        else:
            cat_dir = os.path.join(STICKER_DIR, cat)
            files = [f for f in os.listdir(cat_dir)
                     if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))]
            if files:
                path = os.path.join(cat_dir, random.choice(files))
                # 入队 STICKER 消息
                sys.path.insert(0, BRIDGE)
                import msgqueue
                import hashlib
                db = msgqueue.get_db()
                # 用 enqueue_game_message 类似机制，这里简化：直接插 messages
                now = int(time.time())
                content = f"[表情包:{cat}]"
                msg_hash = hashlib.md5(f"{chat}:{now}:{content}".encode()).hexdigest()
                db.execute("""
                    INSERT INTO messages
                    (chat, sender, sender_name, content, msg_time, created_at,
                     should_reply, status, direction, action, media_path, msg_hash)
                    VALUES (?, 'GAME_SYSTEM', 'Muse', ?, ?, ?, 1, 'READY', 'OUTBOUND', 'STICKER', ?, ?)
                """, (chat, content, now, now, path, msg_hash))
                db.commit()
                record_send(chat)
                print(f"queued sticker {cat}: {path}")
            else:
                print("no files")
    elif cmd == "send_md5" and len(sys.argv) >= 4:
        # 2026-10-11: 从表情库按 MD5 发送（静态单帧降级版）
        chat, md5 = sys.argv[2], sys.argv[3]
        if not check_rate_limit(chat):
            print("rate limited")
        else:
            # 查库找文件
            db = _db()
            r = db.execute("SELECT file_path FROM sticker_library WHERE md5=?", (md5,)).fetchone()
            if r and r[0] and os.path.exists(r[0]):
                path = r[0]
            else:
                # 尝试 stolen 目录
                path = os.path.join(STICKER_DIR, "stolen", f"{md5}.gif")
                if not os.path.exists(path):
                    print(f"not found: {md5}")
                    sys.exit(1)
            sys.path.insert(0, BRIDGE)
            import msgqueue
            import hashlib
            db = msgqueue.get_db()
            now = int(time.time())
            content = f"[表情包:library]"
            msg_hash = hashlib.md5(f"{chat}:{now}:{md5}".encode()).hexdigest()
            db.execute("""
                INSERT INTO messages
                (chat, sender, sender_name, content, msg_time, created_at,
                 should_reply, status, direction, action, media_path, msg_hash)
                VALUES (?, 'GAME_SYSTEM', 'Muse', ?, ?, ?, 1, 'READY', 'OUTBOUND', 'STICKER', ?, ?)
            """, (chat, content, now, now, path, msg_hash))
            db.commit()
            record_send(chat)
            print(f"queued library sticker {md5[:8]}: {path}")
    else:
        print(__doc__)
