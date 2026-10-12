#!/usr/bin/env python3
"""表情包每日 AI 评审（2026-10-12）。
每天 08:00 运行，评审昨天偷到的待审核表情，决定留/删。
"""
import os
import sys
import sqlite3
import time

BRIDGE = "~/workspace/wechat-bridge"
DB_PATH = os.path.join(BRIDGE, "queue.db")

def get_pending():
    db = sqlite3.connect(DB_PATH, timeout=10)
    rows = db.execute("""
        SELECT md5, file_path, source_chat, first_seen_at
        FROM sticker_library
        WHERE pending_review=1
        ORDER BY first_seen_at DESC
    """).fetchall()
    db.close()
    return rows

def main():
    pending = get_pending()
    if not pending:
        print("No pending stickers to review")
        return 0
    
    print(f"Found {len(pending)} pending stickers")
    # TODO: 集成 worker AI 评审
    # 当前：只列出，待 worker 集成后自动评审
    # 临时方案：标记为已评审（避免重复），人工可手动删
    db = sqlite3.connect(DB_PATH, timeout=10)
    for md5, path, chat, ts in pending:
        print(f"  {md5[:8]}... {path} from {chat}")
        # 暂时标记为已评审，待 AI 集成后改为真正的评审
        # db.execute("UPDATE sticker_library SET pending_review=0 WHERE md5=?", (md5,))
    # db.commit()
    db.close()
    print("Review placeholder - AI integration pending")
    return 0

if __name__ == "__main__":
    sys.exit(main())
