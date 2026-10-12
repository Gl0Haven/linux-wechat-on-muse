#!/usr/bin/env python3
"""
轻量调度器（2026-10-09 Batch3）

职责（确定性 Python，不做 AI 思考）：
1. 租约/过期维护：回收过期 GENERATING，清理僵尸 OUTBOUND
2. 任务优先级排序：P0 显式 > P1 research > P2 上下文 > P3 主动搭话
3. 会话聚合：为 worker 预聚合待决策的会话批次
4. 指标采集：新消息→LLM、LLM→READY 等延迟

不做的事：
- 不做 LLM 裁决（那是 Decision Worker 的事）
- 不直接触发 Agent（平台不支持按需唤醒，cron 仍是触发器）
- 不发送消息（那是 sendd 的事）

运行：python3 scheduler.py（常驻，由 keepalive 拉起）
"""

import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msgqueue import get_db

SCHEDULER_ID = "scheduler"
HEARTBEAT_FILE = os.path.expanduser(
    "~/workspace/wechat-bridge/state/scheduler_heartbeat")
METRICS_FILE = os.path.expanduser(
    "~/workspace/wechat-bridge/state/scheduler_metrics.jsonl")

# 优先级：P0 显式 > P1 research 完成 > P2 上下文 > P3 主动搭话
PRIORITY_ORDER = {
    1: 0,  # EXPLICIT 最优先
    2: 2,  # CONTEXTUAL
}


def heartbeat():
    with open(HEARTBEAT_FILE, "w") as f:
        f.write(str(int(time.time())))


def log_metric(name, value):
    """记录指标（JSONL，供后续分析 P50/P95）"""
    try:
        with open(METRICS_FILE, "a") as f:
            f.write(json.dumps({
                "ts": int(time.time()),
                "metric": name,
                "value": value,
            }) + "\n")
    except Exception:
        pass


def maintain_leases(db):
    """回收过期租约：GENERATING 超过 lease_until 未完成的，打回 NEW"""
    now = int(time.time())
    n = db.execute("""
        UPDATE messages
        SET status='NEW', lease_owner='', lease_until=0, updated_at=?
        WHERE status='GENERATING' AND lease_until < ? AND lease_until > 0
    """, (now, now)).rowcount
    if n:
        print(f"[scheduler] 回收 {n} 个过期 GENERATING", flush=True)
        log_metric("lease_recycled", n)
    db.commit()
    return n


def clean_zombies(db):
    """清理僵尸：10 分钟前的 OUTBOUND NEW"""
    now = int(time.time())
    n = db.execute("""
        UPDATE messages
        SET status='SKIPPED', hold_reason='zombie', updated_at=?
        WHERE direction='OUTBOUND' AND status='NEW' AND created_at < ?
    """, (now, now - 600)).rowcount
    if n:
        print(f"[scheduler] 清理 {n} 个僵尸 OUTBOUND", flush=True)
        log_metric("zombie_cleaned", n)
    db.commit()
    return n


def expire_old_contextual(db):
    """5 分钟前的 should_reply=2 → SKIPPED"""
    now = int(time.time())
    n = db.execute("""
        UPDATE messages
        SET status='SKIPPED', hold_reason='expired', updated_at=?
        WHERE should_reply=2 AND status='NEW' AND msg_time < ?
    """, (now, now - 300)).rowcount
    if n:
        log_metric("contextual_expired", n)
    db.commit()
    return n


def get_pending_summary(db):
    """待处理任务摘要（供 worker 快速决策）"""
    rows = db.execute("""
        SELECT should_reply, COUNT(*), MIN(msg_time)
        FROM messages
        WHERE status='NEW' AND should_reply IN (1, 2)
        GROUP BY should_reply
    """).fetchall()
    return {r[0]: {"count": r[1], "oldest": r[2]} for r in rows}


def collect_latency_metrics(db):
    """采集延迟指标：新消息→LLM（NEW 停留时间）、LLM→READY"""
    now = int(time.time())
    # NEW 消息的平均等待时间（待处理）
    row = db.execute("""
        SELECT AVG(? - created_at), MAX(? - created_at), COUNT(*)
        FROM messages
        WHERE status='NEW' AND should_reply IN (1, 2)
    """, (now, now)).fetchone()
    if row and row[2]:
        log_metric("new_wait_avg", row[0])
        log_metric("new_wait_max", row[1])
    # READY 的生成延迟（GENERATING→READY 的时间，近似）
    row = db.execute("""
        SELECT AVG(updated_at - created_at), COUNT(*)
        FROM messages
        WHERE status='READY' AND updated_at > ?
    """, (now - 3600,)).fetchone()
    if row and row[1]:
        log_metric("gen_latency_avg", row[0])


def main():
    print("[scheduler] 启动轻量调度器", flush=True)
    # 单例锁
    lockfile = os.path.expanduser(
        "~/workspace/wechat-bridge/state/scheduler.lock")
    try:
        import fcntl
        lf = open(lockfile, "w")
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        print("[scheduler] 已有实例在运行，退出", flush=True)
        sys.exit(1)

    while True:
        try:
            heartbeat()
            db = get_db()
            try:
                maintain_leases(db)
                clean_zombies(db)
                expire_old_contextual(db)
                collect_latency_metrics(db)
                # 每 5 分钟输出一次摘要
                if int(time.time()) % 300 < 30:
                    summary = get_pending_summary(db)
                    if summary:
                        print(f"[scheduler] 待处理: {summary}", flush=True)
            finally:
                db.close()
            time.sleep(30)  # 30s 一轮维护
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[scheduler] 错误: {e}", flush=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
