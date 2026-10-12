#!/usr/bin/env python3
"""balloon_watch.py — Balloon / MemAvailable 只读观测记录器。

设计原则（2026-10-07 用户定调）：
- 只读 /proc/meminfo，不修改 balloon、不卸载驱动、不制造内存压力
- Balloon = 动态外生变量，只观测不依赖
- 用途：区分"宿主收紧资源" vs "自身进程/缓存增长"

用法：
    python3 balloon_watch.py record    # 采样一条，追加到 state/balloon_history.jsonl（供 cron 调用）
    python3 balloon_watch.py summary   # 打印最近 24h 统计和显著波动
    python3 balloon_watch.py summary --hours 72

记录格式（JSONL，每行一条）：
    {"ts": 1791303000, "iso": "2026-10-07T00:20:00+08:00",
     "balloon_mb": 4600, "avail_mb": 779, "total_mb": 7935}

保留策略：每次 record 时若超过 MAX_LINES 行，只保留最近的（约 10 天 @5分钟采样）。
"""

import json
import os
import sys
import time
import datetime

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
HISTORY_FILE = os.path.join(STATE_DIR, "balloon_history.jsonl")
MAX_LINES = 3000          # 约 10 天（5 分钟采样）
SIG_SWING_MB = 100        # summary 显著波动阈值（MB）


def _read_meminfo():
    """只读 /proc/meminfo，返回 (total_mb, avail_mb, balloon_mb)。"""
    total = avail = balloon = None
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) // 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) // 1024
            elif line.startswith("Balloon:"):
                balloon = int(line.split()[1]) // 1024
            if total is not None and avail is not None and balloon is not None:
                break
    return total, avail, balloon


def record():
    total, avail, balloon = _read_meminfo()
    if avail is None:
        print("无法读取 /proc/meminfo", file=sys.stderr)
        return 1
    now = time.time()
    iso = datetime.datetime.fromtimestamp(
        now, datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")
    entry = {"ts": int(now), "iso": iso, "balloon_mb": balloon,
             "avail_mb": avail, "total_mb": total}
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    # 裁剪：只保留最近 MAX_LINES 行
    try:
        with open(HISTORY_FILE) as f:
            lines = f.readlines()
        if len(lines) > MAX_LINES:
            with open(HISTORY_FILE, "w") as f:
                f.writelines(lines[-MAX_LINES:])
    except Exception:
        pass
    print(json.dumps(entry))
    return 0


def _load(since_ts):
    rows = []
    if not os.path.exists(HISTORY_FILE):
        return rows
    with open(HISTORY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ts", 0) >= since_ts:
                rows.append(r)
    return rows


def summary(hours=24):
    rows = _load(time.time() - hours * 3600)
    if not rows:
        print(f"过去 {hours}h 无采样记录（{HISTORY_FILE} 不存在或为空）")
        return 0
    balloons = [r["balloon_mb"] for r in rows if r.get("balloon_mb") is not None]
    avails = [r["avail_mb"] for r in rows if r.get("avail_mb") is not None]
    print(f"采样数：{len(rows)}（过去 {hours}h，{rows[0]['iso']} → {rows[-1]['iso']}）")
    if balloons:
        print(f"Balloon    : 最小 {min(balloons)}MB / 最大 {max(balloons)}MB / "
              f"当前 {balloons[-1]}MB / 波动幅度 {max(balloons)-min(balloons)}MB")
    if avails:
        print(f"MemAvailable: 最小 {min(avails)}MB / 最大 {max(avails)}MB / "
              f"当前 {avails[-1]}MB / 波动幅度 {max(avails)-min(avails)}MB")
    # 显著波动：相邻采样 Balloon 变化超过阈值
    print(f"\n显著波动（Balloon 相邻采样变化 > {SIG_SWING_MB}MB）：")
    found = False
    for prev, cur in zip(rows, rows[1:]):
        pb, cb = prev.get("balloon_mb"), cur.get("balloon_mb")
        pa, ca = prev.get("avail_mb"), cur.get("avail_mb")
        if pb is None or cb is None:
            continue
        d = cb - pb
        if abs(d) > SIG_SWING_MB:
            found = True
            direction = "充气↑（宿主拿走）" if d > 0 else "放气↓（宿主归还）"
            avail_note = ""
            if pa is not None and ca is not None:
                avail_note = f"，Avail {pa}→{ca}MB"
            print(f"  {cur['iso']}: Balloon {pb}→{cb}MB（{d:+d}MB）{direction}{avail_note}")
    if not found:
        print("  无")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "record"
    if cmd == "record":
        sys.exit(record())
    elif cmd == "summary":
        hours = 24
        if "--hours" in sys.argv:
            try:
                hours = int(sys.argv[sys.argv.index("--hours") + 1])
            except (ValueError, IndexError):
                pass
        sys.exit(summary(hours))
    else:
        print(__doc__)
        sys.exit(1)
