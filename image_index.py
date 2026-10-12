#!/usr/bin/env python3
"""图片资源索引（2026-10-11 Step 1：只做资源定位，不解密）。

建立确定性关联：
  队列 messages.source_server_id
  → message_resource.db :: MessageResourceInfo.message_svr_id
  → MessageResourceDetail（三档：original/mid/thumb）
  → packed_info protobuf 中的文件 MD5
  → msg/attach/*/{YYYY-MM}/Img/{md5}[_h|_t].dat

设计约束（用户 2026-10-11 拍板）：
- 只读访问微信数据库，不修改微信原始数据；
- 图片元数据写我们自己的 queue.db；
- 幂等：同一 msg_id 重复索引不产生重复行；
- 资源暂时未出现 → 记为待补全，不判永久失败；
- 禁止按文件修改时间猜测归属；
- GUI 截图链路原样保留，本模块只是新增索引能力。

用法：
  python3 image_index.py index <msg_id>   # 为指定队列消息建索引
  python3 image_index.py show <msg_id>    # 查看索引结果
  python3 image_index.py retry            # 重试所有未完成的索引
"""
import glob
import os
import re
import sqlite3
import sys
import time

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
WXID_DIR = os.path.join(HOME, "xwechat_files/wxid_YOUR_BOT_ACCOUNT")
RESOURCE_DB = os.path.join(WXID_DIR, "db_storage/message/message_resource.db")
ATTACH_BASE = os.path.join(WXID_DIR, "msg/attach")

sys.path.insert(0, BRIDGE)
sys.path.insert(0, os.path.join(BRIDGE, "frida"))

# detail.type → (tier 名, 文件后缀)
TIER_MAP = {
    0x10001: ("original", "_h.dat"),   # 实测最大（4.7MB），最高质量
    0x20001: ("mid", ".dat"),
    0x40001: ("thumb", "_t.dat"),
}


def ensure_schema(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS image_resources (
            msg_id INTEGER NOT NULL,
            svr_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            resource_id INTEGER DEFAULT 0,
            size INTEGER DEFAULT 0,
            status INTEGER DEFAULT 0,
            dat_path TEXT DEFAULT '',
            file_exists INTEGER DEFAULT 0,
            file_size INTEGER DEFAULT 0,
            decrypted_path TEXT DEFAULT '',
            indexed_at INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0,
            PRIMARY KEY (msg_id, tier)
        )""")
    # 老表补列（幂等）
    try:
        db.execute("ALTER TABLE image_resources ADD COLUMN decrypted_path TEXT DEFAULT ''")
    except Exception:
        pass
    db.execute("""
        CREATE TABLE IF NOT EXISTS image_index_log (
            msg_id INTEGER PRIMARY KEY,
            svr_id TEXT DEFAULT '',
            state TEXT DEFAULT '',   -- indexed / partial / pending / no_resource
            note TEXT DEFAULT '',
            updated_at INTEGER DEFAULT 0
        )""")
    db.commit()


def _parse_md5(packed_info):
    """从 MessageResourceInfo.packed_info（protobuf）提取文件 MD5。

    结构：field 2 (0x12) → 内嵌 field 1 (0x0a) → 32 字节 hex MD5。
    """
    if not packed_info or len(packed_info) < 36:
        return ""
    try:
        data = bytes(packed_info)
        if data[0] != 0x12:
            return ""
        inner_len = data[1]
        inner = data[2:2 + inner_len]
        if len(inner) < 2 or inner[0] != 0x0A:
            return ""
        md5_len = inner[1]
        md5 = inner[2:2 + md5_len].decode("ascii", errors="ignore")
        if re.fullmatch(r"[0-9a-f]{32}", md5):
            return md5
    except Exception:
        pass
    return ""


def _resolve_dat_files(md5):
    """按 MD5 找到三档 DAT 文件。返回 {tier: path}。"""
    found = {}
    # attach 下按 chat md5 分目录，数量少，直接 glob
    pattern = os.path.join(ATTACH_BASE, "*", "*", "Img", md5 + "*.dat")
    for path in glob.glob(pattern):
        base = os.path.basename(path)
        if base == md5 + "_h.dat":
            found["original"] = path
        elif base == md5 + "_t.dat":
            found["thumb"] = path
        elif base == md5 + ".dat":
            found["mid"] = path
    return found


def _open_resource_db():
    """只读打开 message_resource.db（走现有 WAL 合并解密链路）。"""
    from wal_reader import read_live_db
    from pathlib import Path
    out = read_live_db(Path(RESOURCE_DB), "message/message_resource.db")
    db = sqlite3.connect(f"file:{out}?mode=ro", uri=True, timeout=10)
    return db, out


# worker 视觉模型可读的格式（hevc/wxgf 等不行）；decrypt_v2 返回的 ext 不带点
VIEWABLE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}

def _validate_image(data, ext):
    """解密后完整性校验（借鉴 wechat-ai 的思路）。
    PNG 查 IEND 结束块，JPEG 查 FFD9 结束标记，GIF 查 003B。
    返回 (ok: bool, reason: str)。reason 用于区分截断 vs 解密失败。
    2026-10-11 修：半截下载导致解密出无效图，需明确标记为 truncated。
    """
    if not data or len(data) < 100:
        return False, "too_short"
    ext = (ext or "").lower().lstrip(".")
    if ext == "png":
        if data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"):
            return True, ""
        # PNG 头对但尾不对 = 截断
        if data.startswith(b"\x89PNG"):
            return False, "truncated"
        return False, "decrypt_failed"
    if ext in ("jpg", "jpeg"):
        if data.endswith(b"\xff\xd9"):
            return True, ""
        if data.startswith(b"\xff\xd8"):
            return False, "truncated"
        return False, "decrypt_failed"
    if ext == "gif":
        if data.endswith(b"\x00\x3b"):
            return True, ""
        return False, "truncated"
    return True, ""


def cleanup_truncated(db, msg_id=None):
    """清理截断的 DAT 文件，触发微信重下。
    只删 _log 记为 truncated 的 tier 对应的文件。
    返回清理的文件列表。用户 2026-10-11 授权：允许删半截文件触发重下。
    """
    import os
    cleaned = []
    q = "SELECT msg_id, tier, dat_path FROM image_resources WHERE file_exists=1"
    params = ()
    if msg_id:
        q += " AND msg_id=?"
        params = (msg_id,)
    rows = db.execute(q, params).fetchall()
    for mid, tier, dat_path in rows:
        # 查 log 确认是 truncated
        log = db.execute(
            "SELECT 1 FROM image_index_log WHERE msg_id=? AND state='truncated' "
            "AND detail LIKE ? ORDER BY created_at DESC LIMIT 1",
            (mid, f"%{tier}%")).fetchone()
        if not log:
            continue
        try:
            # 备份到 /tmp 再删
            import shutil
            bak = f"/tmp/truncated_{mid}_{tier}.dat.bak"
            shutil.copy2(dat_path, bak)
            os.remove(dat_path)
            db.execute(
                "UPDATE image_resources SET file_exists=0, file_size=0, dat_path='', "
                "decrypted_path='', updated_at=? WHERE msg_id=? AND tier=?",
                (int(__import__('time').time()), mid, tier))
            cleaned.append(dat_path)
            print(f"[image_index] 清理截断文件 #{mid} {tier}: {dat_path}", flush=True)
        except Exception as e:
            print(f"[image_index] 清理失败 #{mid} {tier}: {e}", flush=True)
    db.commit()
    return cleaned


def _decrypt_best_tier(db, msg_id, now):
    """解密最优档位，存 media/ 下，回写 messages.media_path。

    档位策略（2026-10-11 E1）：默认中图（~67KB，视觉理解够用）；
    中图缺失/损坏时升原图；最后才用缩略图。
    返回解密出的图片路径，失败返回 ''。密钥失效时自动清缓存重派生一次。
    解密失败不触发 GUI 截图，由调用方记不可用状态。
    """
    try:
        import dat_key
        from pathlib import Path as _P
        from wxextract.images import decrypt_v2, is_v2
    except ImportError as e:
        print(f"[image_index] 解密依赖缺失: {e}", flush=True)
        return ""

    # E1：中图优先（效率），原图按需（细节），缩略图兜底
    for tier in ("mid", "original", "thumb"):
        row = db.execute(
            "SELECT dat_path, file_exists FROM image_resources"
            " WHERE msg_id=? AND tier=?", (msg_id, tier)).fetchone()
        if not row or not row[1]:
            continue
        dat_path = _P(row[0])
        if not dat_path.exists() or not is_v2(dat_path):
            continue

        for attempt in (0, 1):  # 第2次是密钥失效重派生后重试
            aes_key, xor_key = dat_key.get_key()
            if not aes_key:
                break
            try:
                result = decrypt_v2(dat_path, aes_key, xor_key)
            except Exception:
                result = None
            if result:
                ok, reason = _validate_image(result[0], result[1])
            else:
                ok, reason = False, "decrypt_failed"
            if ok:
                data, ext = result
                # E1：跳过 worker 看不了的格式（如 hevc/wxgf），继续试下一档
                if ext.lower() not in VIEWABLE_EXTS:
                    print(f"[image_index] msg #{msg_id} {tier} 格式 {ext} 不可读，试下一档",
                          flush=True)
                    continue
                # ext 不带点，补上
                out_path = os.path.join(
                    BRIDGE, "media", f"{msg_id}_dec.{ext}")
                with open(out_path, "wb") as f:
                    f.write(data)
                os.chmod(out_path, 0o600)
                # 回写：让 worker 优先用解密图
                db.execute(
                    "UPDATE messages SET media_path=?, updated_at=?"
                    " WHERE id=?", (out_path, now, msg_id))
                db.execute(
                    "UPDATE image_resources SET decrypted_path=?, updated_at=?"
                    " WHERE msg_id=? AND tier=?",
                    (out_path, now, msg_id, tier))
                db.commit()
                print(f"[image_index] msg #{msg_id} {tier} 解密成功", flush=True)
                return out_path
            # 2026-10-11: 区分截断 vs 解密失败，截断记 log 供 cleanup
            if reason == "truncated":
                _log(db, msg_id, "", "truncated", f"{tier} 文件截断，需重下", now)
                print(f"[image_index] msg #{msg_id} {tier} 文件截断，跳过", flush=True)
                continue  # 截断不重试密钥，直接试下一档
            # 解密失败 → 清缓存重派生后重试一次
            dat_key.invalidate()
            print(f"[image_index] msg #{msg_id} {tier} 解密/校验失败，密钥重派生重试",
                  flush=True)
    # 三档都失败 → 记不可用（不回退 GUI，由 worker 侧判断）
    _log(db, msg_id, "", "decrypt_failed", "三档均解密失败或校验不通过", now)
    return ""


def index_message(db, msg_id):
    """为指定队列消息建资源索引。幂等。返回 (state, detail)。"""
    ensure_schema(db)
    now = int(time.time())

    row = db.execute(
        "SELECT source_server_id, chat, sender_name FROM messages WHERE id=?",
        (msg_id,)).fetchone()
    if not row or not row[0]:
        _log(db, msg_id, "", "no_resource", "队列消息无 source_server_id", now)
        return "no_resource", "no source_server_id"

    svr_id = str(row[0])
    # 已索引过 → 直接返回（幂等）
    existing = db.execute(
        "SELECT state FROM image_index_log WHERE msg_id=?", (msg_id,)).fetchone()
    if existing and existing[0] == "indexed":
        return "indexed", "already indexed"

    try:
        rdb, tmppath = _open_resource_db()
    except Exception as e:
        _log(db, msg_id, svr_id, "pending", f"资源库打开失败: {e}", now)
        return "pending", f"resource db open failed: {e}"

    try:
        info = rdb.execute(
            "SELECT message_id, packed_info FROM MessageResourceInfo"
            " WHERE message_svr_id=?", (int(svr_id),)).fetchone()
        if not info:
            _log(db, msg_id, svr_id, "pending",
                 "资源库暂无该 svr_id（可能延迟），待补全", now)
            return "pending", "svr_id not in resource db yet"

        res_msg_id, packed = info
        md5 = _parse_md5(packed)
        details = rdb.execute(
            "SELECT resource_id, type, size, status FROM MessageResourceDetail"
            " WHERE message_id=?", (res_msg_id,)).fetchall()
    finally:
        rdb.close()
        try:
            os.unlink(tmppath)
        except OSError:
            pass

    if not md5:
        _log(db, msg_id, svr_id, "no_resource", "packed_info 无有效 MD5", now)
        return "no_resource", "no md5 in packed_info"

    dat_files = _resolve_dat_files(md5)
    indexed_tiers = []
    for resource_id, rtype, size, status in details:
        tier_info = TIER_MAP.get(rtype)
        if not tier_info:
            continue
        tier, _suffix = tier_info
        path = dat_files.get(tier, "")
        fexists, fsize = 0, 0
        if path and os.path.exists(path):
            fexists, fsize = 1, os.path.getsize(path)
        db.execute("""
            INSERT OR REPLACE INTO image_resources
            (msg_id, svr_id, tier, resource_id, size, status,
             dat_path, file_exists, file_size, indexed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, svr_id, tier, resource_id, size, status,
             path, fexists, fsize, now, now))
        indexed_tiers.append(tier)

    # 状态判定
    tiers_with_file = [t for t in indexed_tiers
                       if db.execute("SELECT file_exists FROM image_resources"
                                     " WHERE msg_id=? AND tier=?",
                                     (msg_id, t)).fetchone()[0]]
    if len(tiers_with_file) >= 3:
        state, note = "indexed", f"三档齐全 md5={md5}"
    elif tiers_with_file:
        state, note = "partial", f"部分档位缺文件 md5={md5} tiers={tiers_with_file}"
    elif indexed_tiers:
        state, note = "partial", f"有资源记录但文件未找到 md5={md5}"
    else:
        state, note = "no_resource", "无已知 tier 的资源记录"

    _log(db, msg_id, svr_id, state, note, now)
    db.commit()

    # 2026-10-11 E步：索引成功后自动解密最优档位（离线，不碰 GUI）
    if state in ("indexed", "partial"):
        try:
            _decrypt_best_tier(db, msg_id, now)
        except Exception as e:
            print(f"[image_index] 解密异常: {e}", flush=True)

    return state, note


def _log(db, msg_id, svr_id, state, note, now):
    db.execute("""
        INSERT OR REPLACE INTO image_index_log
        (msg_id, svr_id, state, note, updated_at)
        VALUES (?, ?, ?, ?, ?)""", (msg_id, svr_id, state, note[:500], now))
    db.commit()


def get_indexed(db, msg_id):
    """取某消息的索引结果。返回 {tier: {...}}，空 dict 表示未索引。"""
    ensure_schema(db)
    rows = db.execute(
        "SELECT tier, resource_id, size, status, dat_path, file_exists,"
        " file_size FROM image_resources WHERE msg_id=?", (msg_id,)).fetchall()
    return {r[0]: {"resource_id": r[1], "size": r[2], "status": r[3],
                   "dat_path": r[4], "file_exists": bool(r[5]),
                   "file_size": r[6]} for r in rows}


def retry_pending(db, limit=20):
    """重试 pending 状态的消息。返回 [(msg_id, state, note)]。"""
    ensure_schema(db)
    rows = db.execute(
        "SELECT msg_id FROM image_index_log WHERE state='pending'"
        " ORDER BY updated_at ASC LIMIT ?", (limit,)).fetchall()
    out = []
    for (mid,) in rows:
        state, note = index_message(db, mid)
        out.append((mid, state, note))
    return out


def main(argv):
    sys.path.insert(0, BRIDGE)
    from msgqueue import get_db
    db = get_db()
    if len(argv) < 2:
        print(__doc__)
        return
    cmd = argv[1]
    if cmd == "index" and len(argv) > 2:
        state, note = index_message(db, int(argv[2]))
        print(f"{state}: {note}")
        for tier, info in get_indexed(db, int(argv[2])).items():
            print(f"  {tier}: exists={info['file_exists']} size={info['file_size']}"
                  f" path={info['dat_path']}")
    elif cmd == "show" and len(argv) > 2:
        mid = int(argv[2])
        log = db.execute("SELECT state, note FROM image_index_log WHERE msg_id=?",
                         (mid,)).fetchone()
        print(f"log: {log}")
        for tier, info in get_indexed(db, mid).items():
            print(f"  {tier}: {info}")
    elif cmd == "retry":
        for mid, state, note in retry_pending(db):
            print(f"{mid}: {state}: {note}")
    else:
        print(__doc__)
    db.close()


if __name__ == "__main__":
    main(sys.argv)
