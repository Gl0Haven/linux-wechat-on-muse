#!/usr/bin/env python3
"""微信桥接运行状态快照（2026-10-11 E5）。

只读诊断，不读取聊天内容。聚焦真实关心的链路：
  WAL 入库 → Hook 唤醒 → LLM 生成 → READY → sendd 发送 → WAL 确认

输出各阶段：最后成功时间、当前积压、失败数、卡住位置。
区分"任务处理了"/"按下发送键了"/"获得客户端确认了"。

用法：python3 health_snapshot.py [--json]
"""
import json
import os
import sqlite3
import sys
import time

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
DB_PATH = os.path.join(BRIDGE, "queue.db")


def _ts(ts):
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def snapshot():
    now = int(time.time())
    db = sqlite3.connect(DB_PATH, timeout=10)
    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))}

    # 1. WAL 入库：最近入库时间 + 待处理积压
    try:
        r = db.execute("SELECT MAX(created_at) FROM messages").fetchone()
        out["ingest"] = {
            "last_msg_at": _ts(r[0] if r else 0),
            "age_sec": now - (r[0] if r and r[0] else now),
        }
    except Exception as e:
        out["ingest"] = {"error": str(e)[:100]}

    # 2. 待回复积压（NEW + should_reply）
    try:
        r = db.execute(
            "SELECT COUNT(*) FROM messages WHERE status='NEW'"
            " AND should_reply IN (1,2)").fetchone()
        out["pending_reply"] = r[0] if r else 0
        # 最老的积压
        r2 = db.execute(
            "SELECT MIN(created_at) FROM messages WHERE status='NEW'"
            " AND should_reply IN (1,2)").fetchone()
        out["oldest_pending_at"] = _ts(r2[0] if r2 and r2[0] else 0)
    except Exception as e:
        out["pending_reply"] = f"error: {e}"[:100]

    # 3. LLM 生成：GENERATING 卡住的（lease 过期）
    try:
        r = db.execute(
            "SELECT COUNT(*) FROM messages WHERE status='GENERATING'"
            " AND lease_until < ?", (now,)).fetchone()
        out["stuck_generating"] = r[0] if r else 0
    except Exception:
        out["stuck_generating"] = -1

    # 4. READY 待发送
    try:
        r = db.execute(
            "SELECT COUNT(*) FROM messages WHERE status='READY'").fetchone()
        out["ready_to_send"] = r[0] if r else 0
    except Exception:
        out["ready_to_send"] = -1

    # 5. 发送状态分布（最近1小时）
    try:
        hour_ago = now - 3600
        rows = db.execute(
            "SELECT status, COUNT(*) FROM messages"
            " WHERE created_at > ? AND action='REPLY'"
            " GROUP BY status", (hour_ago,)).fetchall()
        out["send_1h"] = {s: n for s, n in rows}
        # 区分 SENT / SEND_UNCERTAIN / FAILED
        r = db.execute(
            "SELECT MAX(sent_at) FROM messages WHERE sent_at > 0").fetchone()
        out["last_sent_at"] = _ts(r[0] if r and r[0] else 0)
    except Exception as e:
        out["send_1h"] = {"error": str(e)[:100]}

    # 6. 图片链路：索引/解密状态
    try:
        r = db.execute(
            "SELECT state, COUNT(*) FROM image_index_log GROUP BY state").fetchone()
        # 全部状态
        rows = db.execute(
            "SELECT state, COUNT(*) FROM image_index_log GROUP BY state").fetchall()
        out["image_index"] = {s: n for s, n in rows}
        # 密钥状态（不暴露明文）
        import dat_key
        aes, _ = dat_key.get_key()
        out["dat_key"] = "ready" if aes else "MISSING"
    except Exception as e:
        out["image_index"] = {"error": str(e)[:100]}

    # 7. 进程心跳（sendd/wal_reader）
    out["procs"] = {}
    for name, hbfile in (("wal_reader", "wal_reader.heartbeat"),
                         ("sendd", "sendd.heartbeat")):
        p = os.path.join(BRIDGE, "state", hbfile)
        try:
            if os.path.exists(p):
                age = now - int(os.path.getmtime(p))
                out["procs"][name] = f"alive({age}s ago)" if age < 300 else f"STALE({age}s)"
            else:
                out["procs"][name] = "no heartbeat file"
        except Exception:
            out["procs"][name] = "check failed"

    db.close()
    return out


def main():
    snap = snapshot()
    if "--json" in sys.argv:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        print("=== 微信桥接状态快照 ===")
        print(f"时间: {snap['at']}")
        print(f"\n[入库] 最后消息: {snap['ingest'].get('last_msg_at')} "
              f"({snap['ingest'].get('age_sec')}s前)")
        print(f"[待回复] 积压: {snap['pending_reply']} 条 "
              f"(最老: {snap.get('oldest_pending_at')})")
        print(f"[生成] 卡住: {snap['stuck_generating']} | "
              f"[待发送] READY: {snap['ready_to_send']}")
        print(f"[发送1h] {snap['send_1h']} | 最后发送: {snap.get('last_sent_at')}")
        print(f"[图片] 索引状态: {snap['image_index']} | 密钥: {snap.get('dat_key')}")
        print(f"[进程] {snap['procs']}")


if __name__ == "__main__":
    main()
