#!/usr/bin/env python3
"""
sendd - 微信 GUI 唯一写者（独立发送进程）

职责：READY → SENDING → 调 send-verified.sh → SENT / SEND_UNCERTAIN
worker 只到 READY，绝不碰 xdotool / GUI。

发送前校验：
  - VERIFY_CHAT: focused_chat 状态文件中的聚焦群 == 目标群（对不上不发）
  - 3 条连发限制：目标群最后 3 条若全是小号自己发的 → 暂缓（@ 回复不受限）
  - VERIFY_DRAFT / VERIFY_WAL: 由 send-verified.sh 执行（粘贴校验 + WAL 回读）

多消息：reply_content 若为 {"messages": [...]} JSON，按 1-4 秒随机间隔逐条发送。

SENDING 遗留恢复（启动时）：lease 过期仍 SENDING → 查 WAL，
  能证明发出 → SENT；无法证明 → SEND_UNCERTAIN（绝不自动重发）。

纯观测期：没有 READY 消息时空转，不改变任何状态。
"""

import json
import os
import random
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db

BRIDGE = os.path.dirname(os.path.abspath(__file__))
FOCUSED_CHAT_FILE = os.path.join(BRIDGE, "state", "focused_chat.txt")
SEND_SCRIPT = os.path.join(BRIDGE, "send-verified.sh")
SELF_WXID = "wxid_YOUR_BOT_ACCOUNT"

SENDER_ID = f"sendd-{uuid.uuid4().hex[:8]}"
LEASE_SECONDS = 180
POLL_INTERVAL = 5

# 3 条连发限制：最后 N 条全是自己 → 暂缓非 @ 消息
CONSECUTIVE_LIMIT = 3


def log(msg):
    print(f"[sendd] {msg}", flush=True)


def get_focused_chat():
    try:
        with open(FOCUSED_CHAT_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def claim_next_send(db):
    """原子领取一条 READY。EXPLICIT 优先，同级按时间。"""
    now = int(time.time())
    lease_until = now + LEASE_SECONDS
    attempt_id = uuid.uuid4().hex[:12]
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute("""
            UPDATE messages
            SET status='SENDING', lease_until=?, lease_owner=?,
                send_attempt_id=?, send_started_at=?, updated_at=?
            WHERE id = (
                SELECT id FROM messages
                WHERE status='READY'
                ORDER BY should_reply DESC, msg_time ASC
                LIMIT 1
            )
            RETURNING id, chat, chat_name, sender_name, content,
                      reply_content, is_at, should_reply
        """, (lease_until, SENDER_ID, attempt_id, now, now))
        row = cur.fetchone()
        db.commit()
        return row, attempt_id
    except Exception:
        db.rollback()
        raise


def check_consecutive_limit(db, chat_uname, is_at):
    """
    3 条连发限制。@ 回复不受限。
    返回 (allowed: bool, reason: str)
    """
    if is_at:
        return True, "at-exempt"
    rows = db.execute(
        "SELECT sender FROM messages WHERE chat=? AND typeof(msg_time)='integer' "
        "ORDER BY msg_time DESC LIMIT ?",
        (chat_uname, CONSECUTIVE_LIMIT)).fetchall()
    if len(rows) < CONSECUTIVE_LIMIT:
        return True, "not-enough-history"
    if all(r[0] == SELF_WXID for r in rows):
        return False, f"last {CONSECUTIVE_LIMIT} are all self"
    return True, "ok"


def parse_messages(reply_content):
    """解析待发送内容：支持 {"messages": [...]} 或纯文本"""
    if not reply_content:
        return []
    rc = reply_content.strip()
    if rc.startswith("{"):
        try:
            data = json.loads(rc)
            msgs = data.get("messages")
            if isinstance(msgs, list) and msgs:
                return [str(m) for m in msgs if str(m).strip()]
        except Exception:
            pass
    return [reply_content] if reply_content.strip() else []


def run_send_verified(chat_name, content):
    """调 send-verified.sh，返回退出码。0=成功 2=弹窗 3=发送失败 4=WAL不确定"""
    try:
        r = subprocess.run(
            ["bash", SEND_SCRIPT, chat_name, content],
            capture_output=True, text=True, timeout=120)
        return r.returncode
    except subprocess.TimeoutExpired:
        return 3
    except Exception as e:
        log(f"send-verified exception: {e}")
        return 3


def mark_sent(db, msg_id, attempt_id):
    now = int(time.time())
    db.execute(
        "UPDATE messages SET status='SENT', sent_at=?, updated_at=? "
        "WHERE id=? AND lease_owner=?",
        (now, now, msg_id, SENDER_ID))
    db.commit()


def mark_uncertain(db, msg_id, attempt_id, reason):
    now = int(time.time())
    db.execute(
        "UPDATE messages SET status='SEND_UNCERTAIN', error=?, updated_at=? "
        "WHERE id=? AND lease_owner=?",
        (f"attempt {attempt_id}: {reason}", now, msg_id, SENDER_ID))
    db.commit()


def mark_retry(db, msg_id, attempt_id, reason):
    now = int(time.time())
    db.execute(
        "UPDATE messages SET status='RETRY', retry_count=retry_count+1, "
        "error=?, lease_owner='', lease_until=0, updated_at=? "
        "WHERE id=? AND lease_owner=?",
        (f"attempt {attempt_id}: {reason}", now, msg_id, SENDER_ID))
    db.commit()


def release_hold(db, msg_id, attempt_id, reason):
    """暂缓发送：放回 READY（3 条限制等真人说话后下次再试），清 lease"""
    now = int(time.time())
    db.execute(
        "UPDATE messages SET status='READY', lease_owner='', lease_until=0, "
        "error=?, updated_at=? WHERE id=? AND lease_owner=?",
        (f"held: {reason}", now, msg_id, SENDER_ID))
    db.commit()


def process_one(db):
    row, attempt_id = claim_next_send(db)
    if not row:
        return False
    msg_id, chat, chat_name, sender_name, content, reply_content, is_at, level = row
    log(f"claimed #{msg_id} level={level} chat={chat_name} attempt={attempt_id}")

    # VERIFY_CHAT：聚焦群必须 == 目标群
    focused = get_focused_chat()
    if focused and focused != chat:
        log(f"VERIFY_CHAT fail: focused={focused} target={chat}, release")
        release_hold(db, msg_id, attempt_id, f"focus mismatch ({focused})")
        return True

    # 3 条连发限制（@ 不受限）
    allowed, reason = check_consecutive_limit(db, chat, bool(is_at))
    if not allowed:
        log(f"consecutive limit: hold #{msg_id} ({reason})")
        release_hold(db, msg_id, attempt_id, reason)
        return True

    # 解析多消息
    parts = parse_messages(reply_content)
    if not parts:
        log(f"empty reply_content #{msg_id}, mark SENT (nothing to send)")
        mark_sent(db, msg_id, attempt_id)
        return True

    # 逐条发送，多条之间 1-4 秒随机间隔（发送策略控制，不让 Muse sleep）
    target_name = chat_name or chat
    for i, part in enumerate(parts):
        if i > 0:
            gap = random.uniform(1, 4)
            log(f"gap {gap:.1f}s before part {i+1}/{len(parts)}")
            time.sleep(gap)
        rc = run_send_verified(target_name, part)
        if rc == 0:
            log(f"part {i+1}/{len(parts)} sent")
            continue
        if rc == 2:
            log(f"popup detected, release #{msg_id} for retry later")
            release_hold(db, msg_id, attempt_id, "popup detected")
            return True
        if rc == 4:
            log(f"WAL uncertain for #{msg_id}, mark SEND_UNCERTAIN (no auto-resend)")
            mark_uncertain(db, msg_id, attempt_id, "wal not confirmed")
            return True
        log(f"send failed rc={rc} for #{msg_id}, mark RETRY")
        mark_retry(db, msg_id, attempt_id, f"send-verified rc={rc}")
        return True

    mark_sent(db, msg_id, attempt_id)
    log(f"#{msg_id} SENT")
    return True


def wal_contains(db, chat, content_snippet, since_ts):
    """WAL 回读：检查指定时间后目标群是否出现过该内容片段（用于 SENDING 恢复）"""
    try:
        row = db.execute(
            "SELECT 1 FROM messages WHERE chat=? AND msg_time>=? "
            "AND instr(content, ?) > 0 LIMIT 1",
            (chat, since_ts, content_snippet[:20])).fetchone()
        return bool(row)
    except Exception:
        return False


def reconcile_stale_sending(db):
    """启动时恢复：lease 过期的 SENDING → WAL 能证明则 SENT，否则 SEND_UNCERTAIN"""
    now = int(time.time())
    rows = db.execute(
        "SELECT id, chat, reply_content, send_attempt_id, send_started_at "
        "FROM messages WHERE status='SENDING' AND lease_until < ?",
        (now,)).fetchall()
    for msg_id, chat, reply_content, attempt_id, started_at in rows:
        parts = parse_messages(reply_content)
        snippet = (parts[0] if parts else "")[:20]
        if snippet and wal_contains(db, chat, snippet, (started_at or 0) - 60):
            db.execute(
                "UPDATE messages SET status='SENT', sent_at=?, updated_at=? "
                "WHERE id=?", (now, now, msg_id))
            log(f"reconcile #{msg_id}: WAL proved sent → SENT")
        else:
            db.execute(
                "UPDATE messages SET status='SEND_UNCERTAIN', "
                "error='reconcile: cannot prove sent, no auto-resend', "
                "updated_at=? WHERE id=?", (now, msg_id))
            log(f"reconcile #{msg_id}: cannot prove → SEND_UNCERTAIN")
    if rows:
        db.commit()
    return len(rows)


def main():
    log(f"starting, sender_id={SENDER_ID}")
    db = get_db()
    n = reconcile_stale_sending(db)
    if n:
        log(f"reconciled {n} stale SENDING")
    while True:
        try:
            worked = process_one(db)
            if not worked:
                time.sleep(POLL_INTERVAL)
        except Exception as e:
            log(f"loop error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
