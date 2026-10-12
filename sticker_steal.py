#!/usr/bin/env python3
"""微信表情包偷取（2026-10-11 F5）。

当群里有人发表情包（type 47）时，自动下载原图存到 stickers/stolen/，
用户后续可分类到各个情绪文件夹。

表情包 XML 格式：
  <msg><emoji md5="..." cdnurl="..." thumburl="..." ... /></msg>

下载优先级：
  1. cdnurl（原图，可能是 GIF）
  2. thumburl（缩略图）
"""
import os
import time
import urllib.request

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
STOLEN_DIR = os.path.join(BRIDGE, "stickers", "stolen")

os.makedirs(STOLEN_DIR, exist_ok=True)


def steal(md5, cdnurl=None, thumburl=None, sender=None, chat=None):
    """下载表情包。返回保存路径或 None。"""
    if not md5:
        return None

    # 已存在则跳过
    for ext in (".gif", ".png", ".jpg"):
        p = os.path.join(STOLEN_DIR, f"{md5}{ext}")
        if os.path.exists(p):
            return p

    # 按优先级尝试下载（2026-10-12：加重试，避免并发冲垮）
    for url in (cdnurl, thumburl):
        if not url:
            continue
        # 每个 URL 重试 3 次
        for attempt in range(3):
            try:
                # 解码 HTML 实体（微信 XML 里 & 写成 &amp;）
                import html as _html
                url = _html.unescape(url)
                # 微信 CDN URL 可能需要补全协议
                if url.startswith("//"):
                    url = "https:" + url
                elif not url.startswith("http"):
                    continue

                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                    if len(data) < 100:  # 太小，可能是错误页面
                        continue

                # 判断格式
                if data[:6] in (b"GIF87a", b"GIF89a"):
                    ext = ".gif"
                elif data[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                elif data[:2] == b"\xff\xd8":
                    ext = ".jpg"
                else:
                    ext = ".dat"  # 未知格式，先存下来

                path = os.path.join(STOLEN_DIR, f"{md5}{ext}")
                with open(path, "wb") as f:
                    f.write(data)

                # 记录元信息
                meta = os.path.join(STOLEN_DIR, f"{md5}.txt")
                with open(meta, "w") as f:
                    f.write(f"md5={md5}\n")
                    f.write(f"sender={sender}\n")
                    f.write(f"chat={chat}\n")
                    f.write(f"url={url}\n")
                    f.write(f"time={int(time.time())}\n")

                print(f"[sticker_steal] saved {path} ({len(data)} bytes)")
                # 2026-10-12: 入库 sticker_library，标记待审核
                try:
                    import sqlite3
                    _db = sqlite3.connect(os.path.join(BRIDGE, "queue.db"), timeout=10)
                    _db.execute("""
                        INSERT OR IGNORE INTO sticker_library
                        (md5, first_seen_at, source_chat, file_path, pending_review, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?)
                    """, (md5, int(time.time()), chat, path, int(time.time())))
                    _db.commit()
                    _db.close()
                except Exception as _e:
                    print(f"[sticker_steal] db insert failed: {_e}")
                return path
        except Exception as e:
            print(f"[sticker_steal] download failed {url} (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(2)  # 重试前等待
            continue

    print(f"[sticker_steal] all urls failed for {md5}")
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        # 测试：python3 sticker_steal.py <md5> <url>
        md5 = sys.argv[1]
        url = sys.argv[2] if len(sys.argv) > 2 else None
        print(steal(md5, cdnurl=url))
    else:
        # 列出已偷的
        files = [f for f in os.listdir(STOLEN_DIR)
                 if not f.endswith(".txt")]
        print(f"stolen: {len(files)}")
        for f in sorted(files)[:10]:
            print(f"  {f}")
