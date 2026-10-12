#!/usr/bin/env python3
"""微信消息语义解析（2026-10-11 E2+E3）。

E2 引用消息：
  解析 <refermsg><svrid>，回查原消息。原图 → 走 image_resources 找解密图；
  原文字 → 取文本；其他类型 → 结构化描述。

E3 丰富语义（不含语音/视频，用户 2026-10-11 明确暂不做）：
  - 表情包 (47)：MD5、描述
  - 链接/小程序 (49)：标题、URL、摘要、应用名
  - 引用 (49)：见 E2

所有解析只读，不修改微信数据。失败返回 None，不抛异常。
"""
import re
import sqlite3
import sys
import os
import xml.etree.ElementTree as ET

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
sys.path.insert(0, BRIDGE)
sys.path.insert(0, os.path.join(BRIDGE, "frida"))

# 微信引用消息 XML 结构（标准格式）
# <msg><appmsg><title>..</title><refermsg><svrid>..</svrid><type>..</type>
#   <displayname>..</displayname><content>..</content></refermsg></appmsg></msg>


def parse_quote(content):
    """解析引用消息 XML。返回 dict 或 None。
    {'quoted_svrid': str, 'quoted_type': int, 'from_user': str,
     'display_name': str, 'preview': str}
    """
    if not content or "refermsg" not in content:
        return None
    try:
        # 微信 XML 可能不规范，先正则提 svrid（最关键）
        m = re.search(r"<svrid>(\d+)</svrid>", content)
        if not m:
            return None
        quoted_svrid = m.group(1)
        # 尽量解析完整结构
        result = {"quoted_svrid": quoted_svrid, "quoted_type": 0,
                  "from_user": "", "display_name": "", "preview": ""}
        try:
            root = ET.fromstring(content)
            refer = root.find(".//refermsg")
            if refer is not None:
                for tag in ("type", "fromusr", "displayname", "content"):
                    el = refer.find(tag)
                    if el is not None and el.text:
                        key = {"type": "quoted_type", "fromusr": "from_user",
                               "displayname": "display_name",
                               "content": "preview"}[tag]
                        result[key] = el.text.strip()[:500]
                try:
                    result["quoted_type"] = int(result["quoted_type"] or 0)
                except ValueError:
                    result["quoted_type"] = 0
        except ET.ParseError:
            pass  # 正则已拿到 svrid，细节缺失可接受
        return result
    except Exception:
        return None


def _get_wechat_db():
    """只读打开 message_0.db（WAL 合并）。调用方负责 close 和删临时文件。"""
    from pathlib import Path
    from wal_reader import read_live_db
    src = Path(HOME) / "xwechat_files/wxid_YOUR_BOT_ACCOUNT/db_storage/message/message_0.db"
    out = read_live_db(src, "message/message_0.db")
    db = sqlite3.connect(f"file:{out}?mode=ro", uri=True, timeout=10)
    return db, out


def _find_msg_table(db):
    """找主群的消息表（按 chat md5）。"""
    import hashlib
    target = hashlib.md5("YOUR_MAIN_GROUP@chatroom".encode()).hexdigest()
    tables = [r[0] for r in db.execute(
        "select name from sqlite_master where type='table' and name like 'Msg_%'")]
    for t in tables:
        if target in t:
            return t
    return None


def resolve_quote(quoted_svrid):
    """回查被引用的原消息。返回结构化 dict。
    {'found': bool, 'type': 'text'|'image'|'emoji'|'link'|'unknown',
     'text': str, 'image_path': str, 'sender': str}
    图片走 image_resources 找解密图（E1 已建）。
    """
    result = {"found": False, "type": "unknown", "text": "",
              "image_path": "", "sender": ""}
    if not quoted_svrid:
        return result
    tmppath = None
    try:
        from wxextract.messages import decompress_field
        db, tmppath = _get_wechat_db()
        table = _find_msg_table(db)
        if not table:
            return result
        row = db.execute(
            f"SELECT local_type, real_sender_id, message_content, compress_content"
            f" FROM {table} WHERE server_id=?", (int(quoted_svrid),)).fetchone()
        if not row:
            return result
        ltype, sender, mc, cc = row
        msg_type = (ltype or 0) & 0xFFFFFFFF
        result["sender"] = sender or ""
        result["found"] = True

        if msg_type == 1:
            content = decompress_field(cc) if cc else (mc or b"")
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="ignore")
            result["type"] = "text"
            result["text"] = (content or "")[:1000]
        elif msg_type == 3:
            result["type"] = "image"
            # 走 E1 的 image_resources 找解密图
            qdb = sqlite3.connect(os.path.join(BRIDGE, "queue.db"), timeout=10)
            try:
                # 先找 queue 消息 id（按 svrid）
                qrow = qdb.execute(
                    "SELECT id FROM messages WHERE source_server_id=?",
                    (str(quoted_svrid),)).fetchone()
                if qrow:
                    import image_index
                    image_index.ensure_schema(qdb)
                    irow = qdb.execute(
                        "SELECT decrypted_path FROM image_resources"
                        " WHERE msg_id=? AND decrypted_path != '' LIMIT 1",
                        (qrow[0],)).fetchone()
                    if irow and irow[0] and os.path.exists(irow[0]):
                        result["image_path"] = irow[0]
                    else:
                        # 未解密 → 触发索引+解密（后台已有，这里同步补一次）
                        image_index.index_message(qdb, qrow[0])
                        irow2 = qdb.execute(
                            "SELECT decrypted_path FROM image_resources"
                            " WHERE msg_id=? AND decrypted_path != '' LIMIT 1",
                            (qrow[0],)).fetchone()
                        if irow2 and irow2[0]:
                            result["image_path"] = irow2[0]
            finally:
                qdb.close()
            if not result["image_path"]:
                result["text"] = "[图片：解密不可用]"
        elif msg_type == 47:
            result["type"] = "emoji"
            result["text"] = "[表情包]"
        elif msg_type == 49:
            result["type"] = "link"
            content = decompress_field(cc) if cc else (mc or b"")
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="ignore")
            info = parse_link(content or "")
            result["text"] = info.get("title", "[链接]") if info else "[链接]"
        else:
            result["text"] = f"[未知类型{msg_type}]"
    except Exception:
        pass
    finally:
        try:
            if tmppath and os.path.exists(tmppath):
                os.unlink(tmppath)
        except OSError:
            pass
    return result


def parse_emoji(content):
    """解析表情包消息 (47)。返回 {'md5': str, 'desc': str} 或 None。"""
    if not content:
        return None
    try:
        # 微信表情 XML：<msg><emoji fromusername=.. md5=".." .../>
        m = re.search(r'<emoji[^>]*md5="([0-9a-fA-F]{32})"', content)
        md5 = m.group(1).lower() if m else ""
        # 描述可能在 <emoji ... >xxx</emoji> 或 desc 属性
        d = re.search(r'desc="([^"]{0,100})"', content)
        desc = d.group(1) if d else ""
        if not md5 and not desc:
            # 纯文本表情（如 [微笑]）
            if re.fullmatch(r"\[[^\[\]]{1,10}\]", (content or "").strip()):
                return {"md5": "", "desc": (content or "").strip()}
            return None
        return {"md5": md5, "desc": desc}
    except Exception:
        return None


def parse_link(content):
    """解析链接/小程序消息 (49)。返回 dict 或 None。
    {'title': str, 'url': str, 'desc': str, 'app': str, 'type': str}
    type: 'link'|'miniapp'|'unknown'
    """
    if not content or "<appmsg" not in content:
        return None
    try:
        result = {"title": "", "url": "", "desc": "", "app": "", "type": "unknown"}
        # 引用消息不走这里
        if "refermsg" in content:
            return None
        for tag, key in (("title", "title"), ("url", "url"), ("des", "desc"),
                         ("sourcedisplayname", "app")):
            m = re.search(f"<{tag}>([^<]{{0,200}})</{tag}>", content)
            if m:
                result[key] = m.group(1).strip()
        # 小程序特征
        if "appid" in content and ("miniapp" in content.lower() or "wxa" in content.lower()):
            result["type"] = "miniapp"
        elif result["url"]:
            result["type"] = "link"
        if not result["title"] and not result["url"]:
            return None
        return result
    except Exception:
        return None


def enrich_message(msg_type, content):
    """统一入口：按类型解析，返回结构化语义 dict。
    供 worker 组装上下文用。
    {'kind': 'text'|'image'|'emoji'|'link'|'miniapp'|'quote'|'unknown',
     'quote': {...}|None, 'emoji': {...}|None, 'link': {...}|None,
     'summary': str}  # 给 LLM 的一句话摘要
    """
    out = {"kind": "unknown", "quote": None, "emoji": None,
           "link": None, "summary": ""}
    try:
        if msg_type == 1:
            out["kind"] = "text"
            out["summary"] = (content or "")[:200]
        elif msg_type == 3:
            out["kind"] = "image"
            out["summary"] = "[图片]"
        elif msg_type == 47:
            e = parse_emoji(content)
            out["kind"] = "emoji"
            out["emoji"] = e
            out["summary"] = f"[表情包{e['desc'] and '：'+e['desc'] or ''}]" if e else "[表情包]"
        elif msg_type == 49:
            q = parse_quote(content)
            if q:
                out["kind"] = "quote"
                out["quote"] = q
                # 回查原消息（轻量，只拿摘要）
                orig = resolve_quote(q["quoted_svrid"])
                out["quote"]["original"] = orig
                ot = orig["type"]
                osum = orig["text"][:80] if orig["text"] else (
                    "[图片]" if ot == "image" else f"[{ot}]")
                out["summary"] = f"[引用{osum}] {(content or '')[:100]}"
            else:
                l = parse_link(content)
                out["kind"] = l["type"] if l else "unknown"
                out["link"] = l
                out["summary"] = f"[{l['type']}: {l['title']}]" if l else "[链接]"
        else:
            out["summary"] = f"[类型{msg_type}]"
    except Exception:
        pass
    return out


if __name__ == "__main__":
    # 自测
    qxml = ('<msg><appmsg><title>test</title><refermsg><svrid>12345</svrid>'
            '<type>1</type><displayname>Ran</displayname><content>hello</content>'
            '</refermsg></appmsg></msg>')
    print("quote:", parse_quote(qxml))
    print("emoji:", parse_emoji('<msg><emoji md5="abc123def456abc123def456abc12345" /></msg>'))
    print("link:", parse_link('<msg><appmsg><title>新闻</title><url>https://x.com</url>'
                              '<des>摘要</des></appmsg></msg>'))
