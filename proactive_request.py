#!/usr/bin/env python3
"""用户手动触发 proactive 的标志文件。event worker 每轮循环检查此标志。
用法:
  python3 proactive_request.py set     # 用户手动触发，设置标志
  python3 proactive_request.py check   # 检查是否有待处理的请求（返回 True/False）
  python3 proactive_request.py clear   # 清除标志（event worker 处理后调用）
标志 10 分钟过期，避免僵尸标志一直残留。
"""
import json
import os
import sys
import time

FLAG_PATH = os.path.expanduser("~/workspace/wechat-bridge/state/proactive_request.json")
EXPIRE_SECONDS = 600  # 10 分钟过期

def set_flag():
    os.makedirs(os.path.dirname(FLAG_PATH), exist_ok=True)
    with open(FLAG_PATH, "w") as f:
        json.dump({"requested_at": int(time.time()), "by": "user"}, f)
    print("flag set")

def check_flag():
    try:
        with open(FLAG_PATH) as f:
            d = json.load(f)
        age = int(time.time()) - d.get("requested_at", 0)
        if age < EXPIRE_SECONDS:
            print("True")
            return True
        else:
            # 过期，自动清理
            os.remove(FLAG_PATH)
            print("False")
            return False
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        print("False")
        return False

def clear_flag():
    try:
        os.remove(FLAG_PATH)
        print("flag cleared")
    except FileNotFoundError:
        print("no flag")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "set":
        set_flag()
    elif cmd == "check":
        check_flag()
    elif cmd == "clear":
        clear_flag()
    else:
        print(f"unknown cmd: {cmd}", file=sys.stderr)
        sys.exit(1)
