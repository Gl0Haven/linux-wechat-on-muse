#!/usr/bin/env python3
"""步骤6 群聊记忆（2026-10-06）。

chat_state（每群一行）：current_topic、open_questions、
muse_recent_actions、updated_at。
- 话题 2 小时无活动自动清空
- worker 处理一批消息时顺带更新，不额外增加回合
- 不做长期人物画像，不存个人信息
"""

import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db

TOPIC_TIMEOUT = 2 * 3600  # 2 小时无活动清空
MAX_ACTIONS = 10  # 保留最近 10 次


def get_chat_state(db, chat):
    """取群聊状态，不存在则创建空。"""
    row = db.execute(
        "SELECT current_topic, open_questions, muse_recent_actions,"
        " updated_at FROM chat_state WHERE chat=?",
        (chat,)).fetchone()
    if not row:
        now = int(time.time())
        db.execute(
            "INSERT INTO chat_state"
            " (chat, current_topic, open_questions,"
            " muse_recent_actions, updated_at)"
            " VALUES (?, '', '[]', '[]', ?)",
            (chat, now))
        db.commit()
        return {"current_topic": "", "open_questions": [],
                "muse_recent_actions": [], "updated_at": now}
    # 检查话题超时
    now = int(time.time())
    topic, questions, actions, updated = row
    if topic and now - updated > TOPIC_TIMEOUT:
        # 2 小时无活动，清空话题
        db.execute(
            "UPDATE chat_state SET current_topic='',"
            " open_questions='[]', updated_at=? WHERE chat=?",
            (now, chat))
        db.commit()
        topic, questions = "", "[]"
    return {
        "current_topic": topic,
        "open_questions": json.loads(questions or "[]"),
        "muse_recent_actions": json.loads(actions or "[]"),
        "updated_at": updated,
    }


def update_topic(db, chat, topic, commit=True):
    """更新当前话题。"""
    now = int(time.time())
    db.execute(
        "UPDATE chat_state SET current_topic=?, updated_at=?"
        " WHERE chat=?",
        (topic[:200], now, chat))
    if commit:
        db.commit()


def add_open_question(db, chat, question, commit=True):
    """添加未解决的问题。"""
    state = get_chat_state(db, chat)
    questions = state["open_questions"]
    if question not in questions:
        questions.append(question[:200])
        # 最多保留 5 个
        questions = questions[-5:]
    now = int(time.time())
    db.execute(
        "UPDATE chat_state SET open_questions=?, updated_at=?"
        " WHERE chat=?",
        (json.dumps(questions, ensure_ascii=False), now, chat))
    if commit:
        db.commit()


def resolve_question(db, chat, question, commit=True):
    """标记问题已解决（移除）。"""
    state = get_chat_state(db, chat)
    questions = [q for q in state["open_questions"] if q != question]
    now = int(time.time())
    db.execute(
        "UPDATE chat_state SET open_questions=?, updated_at=?"
        " WHERE chat=?",
        (json.dumps(questions, ensure_ascii=False), now, chat))
    if commit:
        db.commit()


def log_muse_action(db, chat, action_type, summary, commit=True):
    """记录 Muse 的参与（类型 + 摘要）。"""
    state = get_chat_state(db, chat)
    actions = state["muse_recent_actions"]
    now = int(time.time())
    actions.append({
        "type": action_type,  # REPLY/CONTEXTUAL/FOLLOWUP/PROACTIVE
        "summary": summary[:100],
        "at": now,
    })
    actions = actions[-MAX_ACTIONS:]
    db.execute(
        "UPDATE chat_state SET muse_recent_actions=?, updated_at=?"
        " WHERE chat=?",
        (json.dumps(actions, ensure_ascii=False), now, chat))
    if commit:
        db.commit()


def touch(db, chat, commit=True):
    """更新活动时间（有新消息时调用，防止话题超时）。"""
    now = int(time.time())
    db.execute(
        "UPDATE chat_state SET updated_at=? WHERE chat=?",
        (now, chat))
    if commit:
        db.commit()
