#!/usr/bin/env python3
"""群聊名场面/日报素材（2026-10-11 F4）。

按需生成，不自动推送。用户说"昨日名场面"/"今天有啥乐子"时，
worker 调用此模块取素材，再由 LLM 写成有趣的回顾。

素材包括：
- 高互动消息（被引用/回复多的）
- 图片消息（已解密的）
- 事件记忆（F3）
- 高频词/梗候选

用法：
  python3 highlights.py <chat> [hours=24] [--json]
"""
import json
import os
import sqlite3
import sys
import time
from collections import Counter

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
sys.path.insert(0, BRIDGE)

ALLOWED_CHATS = {"YOUR_MAIN_GROUP@chatroom": "[主群]",
                 "YOUR_TEST_GROUP@chatroom": "[测试群]"}


def get_highlights(chat, hours=24):
    """取素材。返回 dict。"""
    if chat not in ALLOWED_CHATS:
        return {"error": "chat not allowed"}
    since = int(time.time()) - hours * 3600
    db = sqlite3.connect(os.path.join(BRIDGE, "queue.db"), timeout=10)

    # 1. 消息统计
    rows = db.execute("""
        SELECT sender_name, COUNT(*) FROM messages
        WHERE chat=? AND created_at>=? AND direction='INBOUND'
        GROUP BY sender_name ORDER BY 2 DESC""", (chat, since)).fetchall()
    top_speakers = [(r[0] or "?", r[1]) for r in rows[:5]]

    # 2. 图片消息（有解密图的）
    img_rows = db.execute("""
        SELECT m.sender_name, m.id, ir.decrypted_path
        FROM messages m JOIN image_resources ir ON m.id=ir.msg_id
        WHERE m.chat=? AND m.created_at>=? AND ir.decrypted_path != ''
        GROUP BY m.id LIMIT 5""", (chat, since)).fetchall()
    images = [{"sender": r[0], "msg_id": r[1], "path": r[2]} for r in img_rows]

    # 3. 事件记忆
    try:
        import event_memory
        event_memory.ensure_schema(db)
        events = event_memory.get_events(db, chat, limit=5)
    except Exception:
        events = []

    # 4. 高频词（简单分词，找梗候选）
    text_rows = db.execute("""
        SELECT content FROM messages
        WHERE chat=? AND created_at>=? AND direction='INBOUND'
          AND length(content) < 100""", (chat, since)).fetchall()
    words = Counter()
    for (c,) in text_rows:
        if not c:
            continue
        # 简单切分：按标点切，取2-6字词
        import re
        for w in re.findall(r"[\u4e00-\u9fff]{2,6}", c):
            words[w] += 1
    hot_words = [w for w, n in words.most_common(10) if n >= 3]

    db.close()
    return {
        "chat_name": ALLOWED_CHATS[chat],
        "hours": hours,
        "top_speakers": top_speakers,
        "images": images,
        "events": events,
        "hot_words": hot_words,
        "total_msgs": sum(n for _, n in rows),
    }


if __name__ == "__main__":
    chat = sys.argv[1] if len(sys.argv) > 1 else "YOUR_MAIN_GROUP@chatroom"
    hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    h = get_highlights(chat, hours)
    if "--json" in sys.argv:
        print(json.dumps(h, ensure_ascii=False, indent=2))
    else:
        print(f"=== {h['chat_name']} 过去{h['hours']}h ===")
        print(f"总消息: {h['total_msgs']}")
        print(f"话痨榜: {h['top_speakers'][:3]}")
        print(f"图片: {len(h['images'])} 张")
        print(f"事件: {[e['text'][:30] for e in h['events']]}")
        print(f"热词: {h['hot_words'][:5]}")
