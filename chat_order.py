#!/usr/bin/env python3
"""动态聊天位置管理（2026-10-06）。

微信聊天列表按最近活动排序（置顶除外）。
我们维护自己的顺序预期：
- 大号置顶，固定位置 1
- 最近发送/接收消息的群排在前面
- 每次发送或收到新消息时，更新顺序

坐标：x=150 固定，y 根据顺序计算（100 + index*60）。
"""

import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "state", "chat_order.json")

# 基础 Y 坐标和行高（2026-10-09 实测标定：visual grounding + winpos）
BASE_Y = 119
ROW_HEIGHT = 68
X_POS = 150

# 置顶的聊天（固定位置，不参与排序；顺序 = 屏幕从上到下）
PINNED = [
    "testroom@chatroom",        # [测试群]
    "YOUR_MAIN_GROUP@chatroom",     # [主群]
    "wxid_YOUR_MAIN_ACCOUNT",      # 大号
]


def _load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"order": [], "updated_at": 0}


def _save(data):
    data["updated_at"] = int(time.time())
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.rename(tmp, STATE_FILE)


def get_order():
    """返回当前顺序列表（置顶在前）。"""
    data = _load()
    order = data.get("order", [])
    # 置顶的放最前，去重
    result = []
    for c in PINNED:
        if c not in result:
            result.append(c)
    for c in order:
        if c not in result:
            result.append(c)
    return result


def touch_chat(chat_id):
    """
    某群有新活动（发送或收到消息）→ 移到置顶后的第一位。
    """
    data = _load()
    order = [c for c in data.get("order", []) if c != chat_id]
    # 置顶的不进 order
    if chat_id not in PINNED:
        order.insert(0, chat_id)
    data["order"] = order
    _save(data)
    print(f"[chat_order] {chat_id} → 位置 2", flush=True)


def get_position(chat_id):
    """
    返回 (x, y) 坐标。
    置顶：y=100；其余按顺序：y=100 + index*60（跳过置顶）。
    """
    order = get_order()
    try:
        idx = order.index(chat_id)
    except ValueError:
        # 未知聊天，默认放最后
        idx = len(order)
    # 置顶的 idx=0 → y=100；非置顶的 idx 从 1 开始
    y = BASE_Y + idx * ROW_HEIGHT
    return {"x": X_POS, "y": y}


def get_all_positions():
    """返回所有已知聊天的坐标（给 sendd 用）。"""
    order = get_order()
    return {chat_id: get_position(chat_id) for chat_id in order}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["get", "touch", "list"],
                   nargs="?", default="list")
    p.add_argument("--chat", default="")
    args = p.parse_args()
    if args.cmd == "get" and args.chat:
        pos = get_position(args.chat)
        print(f"{args.chat}: ({pos['x']}, {pos['y']})")
    elif args.cmd == "touch" and args.chat:
        touch_chat(args.chat)
    else:
        for c in get_order():
            pos = get_position(c)
            print(f"{c}: ({pos['x']}, {pos['y']})")
