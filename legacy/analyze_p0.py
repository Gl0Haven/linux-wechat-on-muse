#!/usr/bin/env python3
"""
P0 实验分析：47s 入库延迟到底是网络还是数据库？
用法：python3 analyze_p0.py [start_ts] [end_ts]   (默认最近30分钟)
输出每条测试消息的 A→B、B→D 分段耗时统计。

A = msg_time（微信服务器时间戳）
B = chain_proxy 观测到的下行流量脉冲（server→client，认为是客户端收到时刻）
D = queue.db created_at（wal_reader 入队时刻）
(C→D 已知 ~2s inotify，故 B→D ≈ B→C + 2s)
"""
import sqlite3, sys, time, datetime, statistics, os

EXP_LOG = "/tmp/chain_proxy_exp.log"
DB = os.path.expanduser("~/workspace/wechat-bridge/queue.db")

now = time.time()
start = float(sys.argv[1]) if len(sys.argv) > 1 else now - 1800
end = float(sys.argv[2]) if len(sys.argv) > 2 else now

# 读 BURST 事件
bursts = []
try:
    with open(EXP_LOG) as f:
        for line in f:
            p = line.strip().split()
            if len(p) >= 2 and p[1] == "BURST":
                bursts.append(float(p[0]))
except FileNotFoundError:
    pass
bursts.sort()

# 读测试消息（只看测试群，避免主群噪音）
con = sqlite3.connect(DB)
rows = con.execute(
    "SELECT msg_time, created_at, sender_name, substr(content,1,30) FROM messages "
    "WHERE chat='YOUR_TEST_GROUP@chatroom' AND direction='INBOUND' "
    "AND created_at >= ? AND created_at <= ? ORDER BY msg_time", (start, end)).fetchall()

print(f"窗口 {datetime.datetime.fromtimestamp(start).strftime('%H:%M:%S')}~"
      f"{datetime.datetime.fromtimestamp(end).strftime('%H:%M:%S')}，测试群消息 {len(rows)} 条，"
      f"BURST 事件 {len(bursts)} 个\n")
print(f"{'发送时刻':8} {'A→B(网络)':>10} {'B→D(客户端+入库)':>14} {'A→D(总)':>8}  内容")
abs_ = []
net = []
cli = []
for mt, ca, sn, c in rows:
    # B = D 之前最后一个 BURST（若无，则记为 None）
    cand = [b for b in bursts if mt - 5 <= b <= ca]
    b = max(cand) if cand else None
    a_b = (b - mt) if b else None
    b_d = (ca - b) if b else None
    a_d = ca - mt
    abs_.append(a_d)
    if a_b is not None and a_b >= -5:
        net.append(a_b)
    if b_d is not None and b_d >= 0:
        cli.append(b_d)
    ts = datetime.datetime.fromtimestamp(mt).strftime('%H:%M:%S')
    amb = "（B点共享/模糊）" if b is None else ""
    print(f"{ts:8} {('%6.0fs' % a_b) if a_b is not None else '   共享B':>10} "
          f"{('%6.0fs' % b_d) if b_d is not None else '   共享B':>14} "
          f"{a_d:6.0f}s  {sn}: {c}{amb}")

def stat(v, name):
    if not v:
        print(f"{name}: 无数据"); return
    print(f"{name}: n={len(v)} 中位数={statistics.median(v):.0f}s "
          f"P95={sorted(v)[max(0,int(len(v)*0.95)-1)]:.0f}s 最大={max(v):.0f}s")

print()
stat(abs_, "A→D 总延迟")
stat(net, "A→B 网络段 ")
stat(cli, "B→D 客户端段")
print("\n判定指引：")
print("  A→B 大、B→D 小（<10s）→ 网络同步是主瓶颈，Frida 难提速，重心转链路")
print("  A→B 小、B→D 大          → 客户端/WAL 是瓶颈，Frida 接收 Hook 值得推进")
print("  两段都波动大            → 分别优化")
