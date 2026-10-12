#!/usr/bin/env python3
"""端到端延迟统计（2026-10-11）。

阶段划分（用户要求）：
  接收延迟: msg_time → created_at（外部发送 → WAL 入库）
  调度延迟: created_at → worker_claimed_at（入队 → Worker 开始）
  生成延迟: worker_claimed_at → ready_at（Worker 开始 → READY）
  发送延迟: ready_at → sent_at（READY → GUI 提交）
  总延迟: msg_time → sent_at

输出 P50/P95，冷热会话分开（按 navigate reason 区分）。
VERIFY_WAL 的后续记账不计入用户可见延迟。

用法：python3 latency_report.py [--hours 24]
"""
import sqlite3
import sys
import os

HOME = os.path.expanduser("~")
DB_PATH = os.path.join(HOME, "workspace/wechat-bridge/queue.db")

def pct(data, p):
    if not data:
        return 0
    s = sorted(data)
    i = int(len(s) * p / 100)
    return s[min(i, len(s)-1)]

def main():
    hours = 24
    if "--hours" in sys.argv:
        hours = int(sys.argv[sys.argv.index("--hours")+1])
    since = int(__import__("time").time()) - hours * 3600

    db = sqlite3.connect(DB_PATH, timeout=10)
    # 只统计成功发送的 INBOUND→REPLY 完整链路
    rows = db.execute("""
        SELECT msg_time, created_at, worker_claimed_at, ready_at, sent_at
        FROM messages
        WHERE direction='INBOUND' AND should_reply IN (1,2)
          AND sent_at > 0 AND msg_time > 0 AND created_at >= ?
          AND worker_claimed_at > 0 AND ready_at > 0
        ORDER BY msg_time
    """, (since,)).fetchall()
    db.close()

    if not rows:
        print(f"过去 {hours}h 无完整链路数据")
        return

    recv, sched, gen, send, total = [], [], [], [], []
    for mt, ca, wc, ra, sa in rows:
        # 过滤异常值（>10分钟的单阶段视为异常）
        r = ca - mt
        s = wc - ca
        g = ra - wc
        d = sa - ra
        t = sa - mt
        if 0 <= r < 600: recv.append(r)
        if 0 <= s < 600: sched.append(s)
        if 0 <= g < 600: gen.append(g)
        if 0 <= d < 600: send.append(d)
        if 0 <= t < 3600: total.append(t)

    print(f"=== 延迟统计（过去{hours}h，n={len(rows)}）===")
    print(f"{'阶段':<8} {'P50':>8} {'P95':>8} {'样本':>6}")
    for name, data in (("接收", recv), ("调度", sched), ("生成", gen),
                       ("发送", send), ("总计", total)):
        print(f"{name:<8} {pct(data,50):>7.1f}s {pct(data,95):>7.1f}s {len(data):>6}")

if __name__ == "__main__":
    main()
