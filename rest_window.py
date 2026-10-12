#!/usr/bin/env python3
"""休息窗口标记（2026-10-08 用户睡眠边界规则的落地实现）。

背景：用户明确说过"休息期间尽量不发消息征求意见"，并在一次健康 CRIT
半夜打扰后要求：睡眠/休息边界内健康告警只记录、早上批量报。
本脚本提供一个持久标记，让 unified_health.py 在窗口激活时压住 CRIT 上报、
只记入 state/health_digest.md。

用法：
    python3 rest_window.py set [note]     # 用户宣布休息/睡觉时调用
    python3 rest_window.py clear          # 用户回来/早上汇总后调用
    python3 rest_window.py status         # 查看当前状态

约定文件：state/rest_window.json
    {"active": true, "since": <epoch>, "note": "..."}  -> 窗口激活
    文件不存在或 {"active": false}                     -> 正常
摘要文件：state/health_digest.md（窗口内被压住的告警，按时间追加）
"""

import json
import os
import sys
import time

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
FLAG_FILE = os.path.join(STATE_DIR, "rest_window.json")
DIGEST_FILE = os.path.join(STATE_DIR, "health_digest.md")


def _load():
    try:
        with open(FLAG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def is_active():
    """返回 (active: bool, info: dict)。供其他脚本调用。"""
    d = _load()
    return bool(d.get("active")), d


def cmd_set(note=""):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(FLAG_FILE, "w") as f:
        json.dump(
            {"active": True, "since": int(time.time()), "note": note},
            f, ensure_ascii=False, indent=2,
        )
    print(f"[rest_window] 已设置休息窗口: {note or '(无备注)'}")


def cmd_clear():
    d = _load()
    d["active"] = False
    d["cleared_at"] = int(time.time())
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(FLAG_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print("[rest_window] 休息窗口已解除")


def cmd_status():
    active, d = is_active()
    if active:
        since = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(d.get("since", 0)))
        print(f"[rest_window] 激活中（自 {since}，备注：{d.get('note', '')}）")
    else:
        print("[rest_window] 未激活")
    # 摘要是否有未读
    if os.path.exists(DIGEST_FILE):
        n = sum(1 for line in open(DIGEST_FILE) if line.startswith("## "))
        print(f"[rest_window] 摘要文件有 {n} 条记录: {DIGEST_FILE}")


def append_digest(text):
    """窗口内被压住的告警记入摘要。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    bj = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time()))
    with open(DIGEST_FILE, "a") as f:
        f.write(f"\n## {bj}\n\n{text}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        cmd_status()
    elif sys.argv[1] == "set":
        cmd_set(" ".join(sys.argv[2:]))
    elif sys.argv[1] == "clear":
        cmd_clear()
    elif sys.argv[1] == "status":
        cmd_status()
    else:
        print("用法: rest_window.py [set [note]|clear|status]")
        sys.exit(1)
