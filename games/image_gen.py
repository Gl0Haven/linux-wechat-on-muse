#!/usr/bin/env python3
"""
积分生图（2026-10-10 用户需求）：
- 触发：@muse 生成图片 <prompt>（群里 @ bot）
- 费用：20 积分/张，先检查积分，够才扣费生成
- 不够：@对方 "当前积分不够，还差X分"
- 够：扣 20 分，建 IMAGE_GEN 任务，由图片 cron 生成并发送

设计：复用游戏积分体系（games.store player_stats），走确定性流程，
不依赖 worker LLM 裁决。调用方（wal_reader）按 suppress_original 标原消息。
"""
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COST = 20

# 匹配：@muse 生成图片 <prompt> 或 消息以"生成图片"开头
# level in (1,2) 已保证是冲 bot 来的（@ 或上下文），这里再要求明确指令形态，
# 避免"这个生成图片功能不错"这类闲聊误触发。
_REQ_RE = re.compile(
    r'(?:^|\s)@muse\s+生成图片\s*(.*)$'
    r'|(?:^|\s)@MUSE\s+生成图片\s*(.*)$'
    r'|^生成图片\s+(.+)$',
    re.IGNORECASE | re.DOTALL)


def parse_request(content):
    """返回 prompt（str）或 None。"""
    if not content:
        return None
    text = content.strip()
    m = _REQ_RE.search(text)
    if not m:
        return None
    prompt = (m.group(1) or m.group(2) or m.group(3) or "").strip()
    return prompt


def get_points(db, chat, sender):
    """当前积分。"""
    from games import store as gstore
    try:
        return gstore.get_stats(db, chat, sender)["points"]
    except Exception:
        return 0


def handle_request(db, chat, chat_name, sender, sender_name, prompt,
                   msg_id, msg_time, should_reply=0):
    """
    处理一条生图请求。返回 dict：
    {
      "handled": bool,          # 是否接管
      "suppress_original": bool,# 是否压住原消息的普通回复
      "reply_needed": bool,     # 是否已生成文本回复（积分不够时）
      "job_id": int|None,      # IMAGE_GEN 任务 id（积分够时）
    }
    """
    from games import store as gstore
    gstore.ensure_schema(db)

    points = get_points(db, chat, sender)
    now = int(time.time())
    display = sender_name or sender

    if points < COST:
        need = COST - points
        # 积分不够：直接插一条 OUTBOUND READY 文本回复（带真 @，sendd 会转）
        text = (f"@{display} 当前积分不够，还差{need}分哦～"
                f"生成一张图片需要{COST}积分，你现在有{points}分，"
                f"每天签到可以攒积分！")
        db.execute("""INSERT INTO messages
            (msg_hash, chat, chat_name, sender, sender_name, content,
             reply_content, msg_time, created_at, should_reply, status,
             direction, action, is_at)
            VALUES (?, ?, ?, 'MUSE', 'MUSE', ?, ?, ?, ?, 0,
                    'READY', 'OUTBOUND', 'REPLY', 1)""",
            (f"imgdeny-{now}-{uuid.uuid4().hex[:8]}", chat, chat_name,
             text, text, now, now))
        db.commit()
        return {"handled": True, "suppress_original": should_reply in (1, 2),
                "reply_needed": False, "job_id": None}

    # 积分够：扣费，建 IMAGE_GEN 任务
    # 2026-10-10 改：不走单独 cron，should_reply=1 让 triage hook 直接领到，
    # worker 按 JSON 里的指示生成图片（走正常消息流水线）
    gstore.add_points_by_chat(db, chat, sender, display, -COST)
    new_points = get_points(db, chat, sender)
    prompt = prompt or "一张可爱的画"
    db.execute("""INSERT INTO messages
        (msg_hash, chat, chat_name, sender, sender_name, content,
         reply_content, msg_time, created_at, should_reply, status,
         direction, action, is_at)
        VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, 1,
                'NEW', 'OUTBOUND', 'IMAGE_GEN', 0)""",
        (f"imggen-{now}-{uuid.uuid4().hex[:8]}", chat, chat_name,
         sender, display, prompt, now, now))
    db.commit()
    job_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    print(f"[image_gen] 扣费 {COST}（{display} 剩 {new_points}），"
          f"任务 #{job_id}：{prompt[:40]}", flush=True)
    return {"handled": True, "suppress_original": should_reply in (1, 2),
            "reply_needed": False, "job_id": job_id}


if __name__ == "__main__":
    # CLI：python3 games/image_gen.py parse "<content>"
    #      python3 games/image_gen.py points <chat> <sender>
    import msgqueue
    if len(sys.argv) < 2:
        print("usage: image_gen.py parse|points <args>")
        sys.exit(1)
    if sys.argv[1] == "parse":
        print(repr(parse_request(sys.argv[2] if len(sys.argv) > 2 else "")))
    elif sys.argv[1] == "points":
        db = msgqueue.get_db()
        print(get_points(db, sys.argv[2], sys.argv[3]))
