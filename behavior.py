#!/usr/bin/env python3
"""步骤4 行为层（2026-10-06）。

类型：REPLY / CONTEXTUAL / FOLLOWUP / PROACTIVE / SKIP
每条处理写入 behavior_log，记录 behavior + reason，方便审计。

限制（文档默认值，若现有限制更严格则保持现有）：
- 每群每小时 PROACTIVE ≤ 2 次
- 两次 PROACTIVE 间隔 ≥ 15 分钟
- 北京时间 23:00–08:00 不主动搭话
- 静默期间所有类型一律禁止
- 只有 REPLY 和 FOLLOWUP 可触发 Research
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db, is_muted

# 行为类型
REPLY = "REPLY"           # 被 @ 或被点名，直接回应
CONTEXTUAL = "CONTEXTUAL" # 没被 @，但话题与 Muse 刚参与的内容强相关
FOLLOWUP = "FOLLOWUP"     # 对之前回答的追问 / 遗留问题跟进
PROACTIVE = "PROACTIVE"   # 主动搭话
SKIP = "SKIP"             # 不发言

# PROACTIVE 限制（2026-10-08 23:14 用户调整）
# - 每小时限制：已取消（2026-10-07 起）
# - 间隔限制：已取消（2026-10-07 起）
# - 安静时段：01:00–08:00（2026-10-08 从 23:00–08:00 收窄）
PROACTIVE_MAX_PER_HOUR = 999  # 保留常量兼容，实际不限制
PROACTIVE_MIN_INTERVAL = 0  # 保留常量兼容，实际不限制
PROACTIVE_QUIET_HOURS = (1, 2, 3, 4, 5, 6, 7)  # 01:00–08:00

# 3连限制触发后，暂停主动发言1小时（2026-10-07 18:18 用户策略）
PROACTIVE_PAUSE_AFTER_LIMIT = 60 * 60  # 1 小时
CONSECUTIVE_LIMIT_FLAG = os.path.expanduser(
    "~/workspace/wechat-bridge/state/consecutive_limit_hit.json")


def log_behavior(db, chat, behavior, reason="", msg_id=0, detail="",
                 commit=True):
    """写入行为审计日志。"""
    now = int(time.time())
    cur = db.execute(
        "INSERT INTO behavior_log"
        " (created_at, chat, msg_id, behavior, reason, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now, chat, msg_id, behavior, reason[:500], detail[:1000]))
    if commit:
        db.commit()
    return cur.lastrowid


def record_consecutive_limit_hit():
    """记录3连限制触发时间（sendd 调用）。"""
    import json
    try:
        os.makedirs(os.path.dirname(CONSECUTIVE_LIMIT_FLAG), exist_ok=True)
        with open(CONSECUTIVE_LIMIT_FLAG, "w") as f:
            json.dump({"hit_at": int(time.time())}, f)
    except Exception:
        pass


def is_proactive_paused_by_limit(now=None):
    """检查是否因3连限制触发而处于暂停期（1小时）。
    返回 (paused: bool, reason: str)。"""
    import json
    now = now or int(time.time())
    try:
        with open(CONSECUTIVE_LIMIT_FLAG) as f:
            d = json.load(f)
        age = now - d.get("hit_at", 0)
        if age < PROACTIVE_PAUSE_AFTER_LIMIT:
            remain = (PROACTIVE_PAUSE_AFTER_LIMIT - age) // 60
            return True, f"3连限制触发后暂停中（还剩约 {remain} 分钟）"
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return False, "ok"


def check_proactive_allowed(db, chat, now=None):
    """
    检查是否允许主动搭话。
    返回 (allowed: bool, reason: str)。
    """
    import datetime
    now = now or int(time.time())

    # 2026-10-07 用户放宽：测试群可主动（含深夜），别处不行
    TEST_GROUP = "YOUR_TEST_GROUP@chatroom"
    is_test = (chat == TEST_GROUP)

    # 1. 静默检查（测试群也不豁免）
    muted, _ = is_muted(db, now)
    if muted:
        return False, "静默期间"

    # 1b. 3连限制触发后暂停1小时（2026-10-07 18:18 用户策略）
    paused, pause_reason = is_proactive_paused_by_limit(now)
    if paused:
        return False, pause_reason

    # 2. 时间检查（北京时间；测试群豁免深夜限制）
    bj = datetime.datetime.fromtimestamp(
        now, tz=datetime.timezone(datetime.timedelta(hours=8)))
    if not is_test and bj.hour in PROACTIVE_QUIET_HOURS:
        return False, f"深夜时段（{bj.hour}点）"

    # 3&4. 每小时次数和间隔限制已取消（2026-10-07 18:20 用户要求：
    #    正常主动不再限每小时2条/15分钟间隔，只靠3连限制+1小时暂停来刹车）
    #    保留静默、深夜、3连暂停作为底线。

    # F2 2026-10-11: 按群状态机（未接话计数/冷却/自动暂停）
    try:
        import proactive_state
        allowed, reason = proactive_state.check_allowed(chat)
        if not allowed:
            return False, reason
    except Exception:
        pass

    return True, "ok"


def get_last_activity_time(db, chat):
    """返回该群最后一次消息活动时间（收或发），无记录返回 None。

    查 messages 表的 msg_time（INBOUND 和 OUTBOUND 都算）。
    chat 参数支持群名（如"[主群]"）或 chatroom ID（自动映射）。
    """
    # chatroom ID 到群名的映射
    CHAT_ID_TO_NAME = {
        "YOUR_MAIN_GROUP@chatroom": "[主群]",
        "YOUR_TEST_GROUP@chatroom": "[测试群]",
    }
    name = CHAT_ID_TO_NAME.get(chat, chat)
    row = db.execute(
        "SELECT MAX(msg_time) FROM messages WHERE chat_name=?",
        (name,)).fetchone()
    return row[0] if row and row[0] else None


def can_trigger_research(behavior):
    """只有 REPLY 和 FOLLOWUP 可以触发 Research。"""
    return behavior in (REPLY, FOLLOWUP)


def get_audit(db, chat=None, limit=20):
    """查询审计日志：回答「为什么发/为什么没发」。"""
    sql = ("SELECT created_at, chat, msg_id, behavior, reason"
           " FROM behavior_log")
    params = []
    if chat:
        sql += " WHERE chat=?"
        params.append(chat)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(sql, params).fetchall()
    return [
        {
            "time": time.strftime("%m-%d %H:%M",
                                  time.localtime(r[0])),
            "chat": r[1],
            "msg_id": r[2],
            "behavior": r[3],
            "reason": r[4],
        }
        for r in rows
    ]
