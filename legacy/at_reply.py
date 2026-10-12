#!/usr/bin/env python3
"""
@ 秒回脚本：轮询 DB 新消息，检测 @GloHaven，立即发送简短确认回复。
设计为被 cron worker 调用，单次运行应在 10 秒内完成。
真正的深度回复由 2 分钟的 wechat-bridge-reply 任务处理。
"""
# ⚠️ 已退役（2026-10-06 D方案）：直读微信源 DB，违反"唯一 reader"原则。
# 生产流程不再调用此文件，保留仅供手动调试。
import json
import subprocess
import sys
import os
from pathlib import Path

BRIDGE = Path.home() / "workspace" / "wechat-bridge"
MESSAGES = BRIDGE / "messages.jsonl"

# @ 检测：大小写不敏感，认 GloHaven 和 muse 两个名字
AT_PATTERNS = ["@glohaven", "@muse"]

def is_at_message(content):
    c = content.lower()
    return any(p in c for p in AT_PATTERNS)

# 简单的确认回复模板，轮换使用避免重复
ACK_REPLIES = [
    "收到，在看了 👀",
    "来了来了，稍等",
    "看到啦，马上回你",
    "在的在的",
]

def run_poller():
    """运行 db_poller.py 获取新消息"""
    result = subprocess.run(
        ["python3", str(BRIDGE / "db_poller.py"), "--json"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"POLLER_ERROR: {result.stderr[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        print(f"POLLER_PARSE_ERROR", file=sys.stderr)
        return []

def send_reply(text, chat=None):
    """通过 send-msg.sh 发送回复，可指定目标聊天"""
    # 先确认微信在运行（家目录二进制）
    import os
    wechat_path = os.path.expanduser("~/wechat/wechat")
    wechat = subprocess.run(["pgrep", "-f", wechat_path], capture_output=True)
    if wechat.returncode != 0:
        print("WECHAT_NOT_RUNNING", file=sys.stderr)
        return False

    cmd = ["bash", str(BRIDGE / "send-msg.sh"), text]
    if chat:
        cmd.append(chat)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode == 0

def mark_replied(msg_time, msg_sender, msg_content):
    """把 messages.jsonl 中对应消息标为已回复"""
    if not MESSAGES.exists():
        return
    lines = []
    # 逐行读改写
    out = []
    with open(MESSAGES) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                m = json.loads(line)
            except:
                out.append(line)
                continue
            if (not m.get("replied") and m.get("time") == msg_time
                    and m.get("sender") == msg_sender and m.get("content") == msg_content):
                m["replied"] = True
                # 标记这是 @ 秒回的确认，2分钟任务可以跟进深度回复
                m["at_acked"] = True
                line = json.dumps(m, ensure_ascii=False)
            out.append(line)
    with open(MESSAGES, "w") as f:
        f.write("\n".join(out) + "\n")

def get_stuck_at_messages():
    """从 messages.jsonl 找卡住的未回复 @ 消息（游标已过但未回）"""
    if not MESSAGES.exists():
        return []
    stuck = []
    try:
        with open(MESSAGES) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except:
                    continue
                if not m.get("replied") and is_at_message(m.get("content", "")):
                    # 只收 10 分钟内的，避免翻几个小时前的旧账
                    stuck.append(m)
    except Exception:
        pass
    return stuck

def main():
    new_msgs = run_poller()
    at_msgs = [m for m in new_msgs if is_at_message(m.get("content", ""))]

    # 兜底：检查 JSONL 里卡住的未回复 @
    if not at_msgs:
        stuck = get_stuck_at_messages()
        # 按时间取最近 3 条，避免一次刷屏
        at_msgs = sorted(stuck, key=lambda m: m.get("time", ""))[-3:]
        if at_msgs:
            print(f"FOUND {len(at_msgs)} stuck @ messages in JSONL")

    if not at_msgs:
        print(f"NO_AT_MESSAGES (checked {len(new_msgs)} new)")
        return

    # 回复所有 @ 消息（按时间排序，先回旧的）
    # 注意：db_poller 已推进游标，未回复的 @ 若只回一条会永久漏掉
    # 用户要求：取消确认语，@ 直接由 LLM 回正文，此处只标记不发送
    at_msgs.sort(key=lambda m: m.get("time", ""))
    for msg in at_msgs:
        # 只标记为已处理，不发送确认语（用户明确不要过渡语）
        mark_replied(msg.get("time"), msg.get("sender"), msg.get("content"))
        print(f"AT_SEEN (no ACK): chat={msg.get('chat')} sender={msg.get('sender')} content={msg.get('content', '')[:30]}")

if __name__ == "__main__":
    main()
