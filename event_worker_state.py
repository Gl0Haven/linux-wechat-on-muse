#!/usr/bin/env python3
"""
事件驱动 worker 的状态协调。

状态文件：state/event_worker.json
  {
    "active": true/false,       # 是否有 event worker 在运行
    "worker_id": "...",         # worker 标识
    "waiter_pid": 12345,        # 当前 waiter 的 PID（0=无）
    "started_at": 1234567890,   # worker 启动时间
    "msg_count": 5,             # 已处理消息数
    "updated_at": 1234567890
  }

规则：
- 2 分钟 cron（兜底）：如果 active=true 且 waiter 存活，则安静退出；
  否则按原逻辑处理（防止 event worker 挂掉后无人处理）。
- event worker：启动时设 active=true，退出时设 active=false。
- supervisor：检查 active，若 false 则拉起新的 event worker。
"""

import os
import sys
import json
import time

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "state", "event_worker.json")

MAX_MSGS = 20       # 处理 20 条后退出
# 2026-10-07 修：调度器 3600 秒硬超时恰好等于 60 分钟，worker 每次都在
# 满 60 分钟时被直接掐死，跑不到 stop，留下 active=true 僵尸。
# 改为 50 分钟主动退出，留 10 分钟余量保证 stop 能跑完。
MAX_RUNTIME = 3000  # 运行 50 分钟后退出

def _load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save(state):
    state["updated_at"] = int(time.time())
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.rename(tmp, STATE_FILE)

def is_waiter_alive(pid):
    """检查 waiter 进程是否存活。"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def is_event_worker_active():
    """event worker 是否在运行（active 且 waiter 存活，或最近有心跳）。"""
    s = _load()
    if not s.get("active"):
        return False
    # waiter 存活 → 肯定在运行
    if is_waiter_alive(s.get("waiter_pid", 0)):
        return True
    # waiter 不在，但 5 分钟内有更新 → 可能在处理消息，算活跃
    if time.time() - s.get("updated_at", 0) < 300:
        return True
    return False

def worker_start(worker_id):
    """event worker 启动时调用。"""
    _save({
        "active": True,
        "worker_id": worker_id,
        "waiter_pid": 0,
        "started_at": int(time.time()),
        "msg_count": 0,
        "last_work_at": 0,  # 上次真正处理消息的时间（0=还没干过活）
    })
    print(f"[event_worker] started: {worker_id}", flush=True)

def worker_stop():
    """event worker 退出时调用。"""
    s = _load()
    s["active"] = False
    s["waiter_pid"] = 0
    _save(s)
    print("[event_worker] stopped", flush=True)

def set_waiter_pid(pid):
    """更新当前 waiter PID。"""
    s = _load()
    s["waiter_pid"] = pid or 0
    _save(s)

def increment_msg_count(n=1):
    """增加已处理消息计数，返回当前计数。同时更新 last_work_at。"""
    s = _load()
    s["msg_count"] = s.get("msg_count", 0) + n
    s["last_work_at"] = int(time.time())
    _save(s)
    return s["msg_count"]

def mark_work_done():
    """标记一次有效工作（处理了消息），更新 last_work_at。"""
    s = _load()
    s["last_work_at"] = int(time.time())
    _save(s)

def is_stuck(pending_new=0, stuck_after=180):
    """event worker 是否卡住：active 但有待处理消息却长时间没干活。
    pending_new: 队列里 NEW 状态的消息数（由调用方传入）。
    stuck_after: 多少秒没干活算卡住（默认 180 秒）。"""
    s = _load()
    if not s.get("active"):
        return False, "not active"
    if pending_new <= 0:
        return False, "no pending messages"
    last_work = s.get("last_work_at", 0)
    # 从没干过活：看启动时间
    since = last_work if last_work else s.get("started_at", 0)
    idle = time.time() - since
    if idle > stuck_after:
        return True, f"idle {int(idle)}s with {pending_new} pending"
    return False, f"idle {int(idle)}s, ok"

def should_exit():
    """是否达到退出条件（20 条或 60 分钟）。"""
    s = _load()
    if s.get("msg_count", 0) >= MAX_MSGS:
        return True, f"msg_count={s['msg_count']} >= {MAX_MSGS}"
    if time.time() - s.get("started_at", time.time()) >= MAX_RUNTIME:
        return True, "runtime >= 50min"
    return False, ""

def heartbeat():
    """心跳：只更新 updated_at，证明 worker 还活着。"""
    s = _load()
    _save(s)
    return s.get("updated_at", 0)


def get_state():
    return _load()


def reclaim_stale():
    """回收僵尸 worker：如果 active 但 waiter 已死且超过 5 分钟无心跳，
    强制设为 inactive。返回 True 表示回收了一个僵尸。"""
    s = _load()
    if not s.get("active"):
        return False
    if is_waiter_alive(s.get("waiter_pid", 0)):
        return False
    if time.time() - s.get("updated_at", 0) < 300:
        return False
    # 僵尸：waiter 死了 + 5 分钟无心跳
    s["active"] = False
    s["waiter_pid"] = 0
    _save(s)
    print(f"[event_worker] reclaimed stale worker {s.get('worker_id')}",
          flush=True)
    return True

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=[
        "status", "start", "stop", "should_exit",
        "set_waiter_pid", "increment", "heartbeat", "reclaim",
        "mark_work", "is_stuck",
    ])
    p.add_argument("--worker-id", default="")
    p.add_argument("--pid", type=int, default=0)
    p.add_argument("--n", type=int, default=1)
    p.add_argument("--pending", type=int, default=0)
    args = p.parse_args()
    if args.cmd == "status":
        print(json.dumps({
            "active": is_event_worker_active(),
            "state": get_state(),
        }, indent=2, ensure_ascii=False))
    elif args.cmd == "start":
        worker_start(args.worker_id or f"event-{int(time.time())}")
    elif args.cmd == "stop":
        worker_stop()
    elif args.cmd == "should_exit":
        yes, reason = should_exit()
        print(f"{'YES' if yes else 'NO'}: {reason}")
    elif args.cmd == "set_waiter_pid":
        set_waiter_pid(args.pid)
        print(f"waiter_pid={args.pid}")
    elif args.cmd == "increment":
        n = increment_msg_count(args.n)
        print(f"msg_count={n}")
    elif args.cmd == "heartbeat":
        ts = heartbeat()
        print(f"heartbeat updated_at={ts}")
    elif args.cmd == "reclaim":
        reclaimed = reclaim_stale()
        print(f"reclaimed={reclaimed}")
    elif args.cmd == "mark_work":
        mark_work_done()
        print("last_work_at updated")
    elif args.cmd == "is_stuck":
        stuck, reason = is_stuck(pending_new=args.pending)
        print(json.dumps({"stuck": stuck, "reason": reason}, ensure_ascii=False))
