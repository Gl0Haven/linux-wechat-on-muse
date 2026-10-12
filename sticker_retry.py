#!/usr/bin/env python3
"""重试下载失败的表情包（2026-10-12）。
从 sticker_events 取 CDN URL，串行下载。
"""
import os
import sys
import time
import sqlite3
import urllib.request
import html

BRIDGE = "~/workspace/wechat-bridge"
STOLEN_DIR = os.path.join(BRIDGE, "stickers", "stolen")
DB_PATH = os.path.join(BRIDGE, "queue.db")

os.makedirs(STOLEN_DIR, exist_ok=True)

def download_one(md5, cdnurl, chat=None):
    """下载单个，带重试。返回路径或 None。"""
    # 已存在跳过
    for ext in (".gif", ".png", ".jpg"):
        p = os.path.join(STOLEN_DIR, f"{md5}{ext}")
        if os.path.exists(p):
            return p
    
    url = html.unescape(cdnurl)
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return None
    
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                if len(data) < 100:
                    continue
                if data[:6] in (b"GIF87a", b"GIF89a"):
                    ext = ".gif"
                elif data[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                elif data[:2] == b"\xff\xd8":
                    ext = ".jpg"
                else:
                    ext = ".dat"
                path = os.path.join(STOLEN_DIR, f"{md5}{ext}")
                with open(path, "wb") as f:
                    f.write(data)
                print(f"  Saved {path} ({len(data)} bytes)")
                # 入库
                try:
                    db = sqlite3.connect(DB_PATH, timeout=10)
                    db.execute("""
                        INSERT OR IGNORE INTO sticker_library
                        (md5, first_seen_at, source_chat, file_path, pending_review, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?)
                    """, (md5, int(time.time()), chat, path, int(time.time())))
                    db.commit()
                    db.close()
                except Exception as e:
                    print(f"  DB insert failed: {e}")
                return path
        except Exception as e:
            print(f"  Attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(3)
    return None

def main():
    db = sqlite3.connect(DB_PATH, timeout=10)
    # 取最近 1 小时有 CDN URL 但未入库的
    rs = db.execute("""
        SELECT DISTINCT md5, cdnurl FROM sticker_events
        WHERE cdnurl IS NOT NULL AND timestamp > ?
        ORDER BY timestamp DESC
    """, (int(time.time()) - 3600,)).fetchall()
    db.close()
    
    print(f"Found {len(rs)} candidates")
    success = 0
    for md5, cdnurl in rs:
        # 检查是否已在库中
        db = sqlite3.connect(DB_PATH, timeout=10)
        exists = db.execute("SELECT 1 FROM sticker_library WHERE md5=?", (md5,)).fetchone()
        db.close()
        if exists:
            continue
        print(f"Downloading {md5[:8]}...")
        if download_one(md5, cdnurl, chat="YOUR_TEST_GROUP@chatroom"):
            success += 1
        time.sleep(1)  # 避免并发
    
    print(f"Done: {success} downloaded")
    return 0

if __name__ == "__main__":
    sys.exit(main())
