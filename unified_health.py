#!/usr/bin/env python3
"""步骤5 统一 Health State（2026-10-06）。

合并：微信登录态、WAL新鲜度、sendd心跳、worker心跳、代理出口、
MemAvailable、research状态、mute状态。
三级：OK / WARN / CRIT。只做可视化和告警，不自动恢复。
注意避免「假活」：进程在但已不同步。
"""

import os
import sys
import time
import json
import socket

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db, is_muted
from rest_window import is_active as rest_window_active, append_digest

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
UNIFIED_FILE = os.path.join(STATE_DIR, "unified_health.json")

OK, WARN, CRIT = "OK", "WARN", "CRIT"


def _read_json(name):
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return {}


def _check_login():
    """微信登录态。"""
    s = _read_json("login_state.json")
    state = s.get("state", "UNKNOWN")
    if state == "LOGGED_IN":
        return OK, "已登录"
    elif state == "LOGIN_REQUIRED":
        return CRIT, "需要扫码登录"
    return WARN, f"未知状态: {state}"


def _check_wal():
    """WAL新鲜度：源库最新消息与入队的差距。
    2026-10-06 修：深夜 23:00–08:00 放宽阈值（用户批准）。
    教训：深夜群闲置时 wal_age 自然变大，300s 就 CRIT 纯属误报。
    2026-10-11 修（Idea：分清闲置和故障）：仅当有入站消息活动、
    WAL 却不推进时才告警；纯闲置（无近期入站消息）只记日志不告警。
    2026-10-11 修2（用户要求）：新鲜度判据从 WAL mtime 切换为入库时间——
    曾出现"用户消息正常入库、WAL mtime 却 22 分钟不动"的指标失真；
    入库时间直接证明读库→入队链路活着。同时修 SYSTEM 发送者排除 bug：
    合成消息的 sender 可能是 SYSTEM/MUSE 等多种，不再只排除两个。"""
    import datetime
    h = _read_json("health.json")
    wal_age = h.get("signals", {}).get("wal_age_s", -1)
    if wal_age < 0:
        return WARN, "无WAL数据"
    # 深夜时段阈值放宽 3 倍（保持不变）
    bj_hour = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))).hour
    is_night = bj_hour >= 23 or bj_hour < 8
    crit_th = 900 if is_night else 300   # 15分钟 / 5分钟
    warn_th = 360 if is_night else 120   # 6分钟 / 2分钟
    ACTIVITY_WINDOW = 600  # 10 分钟无用户入站即视为闲置
    # 合成/系统类 sender 全集：bot 自己入队的各类合成消息都不算"用户活动"
    SYNTHETIC_SENDERS = ("GAME_SYSTEM", "SYSTEM", "SYSTEM_ANNOUNCE", "MUSE")
    try:
        db = get_db()
        # ① 任何入站入库（含合成）都证明读库链路活着 → 新鲜度按入库时间算
        last_any = db.execute(
            "SELECT MAX(created_at) FROM messages WHERE direction='INBOUND'"
        ).fetchone()[0] or 0
        # ② 真实用户入站（排除全部合成 sender）→ 区分闲置/故障
        last_user = db.execute(
            "SELECT MAX(created_at) FROM messages WHERE direction='INBOUND' "
            f"AND sender NOT IN {SYNTHETIC_SENDERS}"
        ).fetchone()[0] or 0
        db.close()
        now = int(time.time())
        ingest_age = now - last_any if last_any else 999999
        idle_for = now - last_user if last_user else 999999
        if ingest_age <= max(crit_th, ACTIVITY_WINDOW):
            return OK, f"入库{ingest_age}秒前（读库链路活；WAL mtime {wal_age}s 仅供参考）"
        if idle_for > ACTIVITY_WINDOW:
            # 纯闲置：不告警，只记录
            return OK, f"WAL {wal_age}秒无更新（闲置{idle_for}秒，无近期用户入站，不告警）"
    except Exception:
        # 查不到时保守起见走原阈值逻辑（不静默可能的真故障）
        pass
    # 兜底：入库停滞且非纯闲置 → 按原 WAL mtime 阈值告警
    if wal_age > crit_th:
        return CRIT, f"WAL {wal_age}秒无更新（可能掉线）"
    if wal_age > warn_th:
        return WARN, f"WAL {wal_age}秒无更新"
    return OK, f"WAL {wal_age}秒前更新"


def _check_process(name, pid_file=None):
    """检查进程是否存活（防假活：同时检查最近活动）。"""
    import subprocess
    try:
        out = subprocess.run(
            ["pgrep", "-f", name], capture_output=True, text=True,
            timeout=5)
        pids = [p for p in out.stdout.strip().split("\n") if p]
        # 排除 pgrep 自身
        if not pids:
            return CRIT, f"{name} 进程不存在"
        return OK, f"{name} 运行中 ({len(pids)}个)"
    except Exception as e:
        return WARN, f"检查失败: {e}"


def _check_proxy():
    """代理出口：chain_proxy可达，出口IP为YOUR_PROXY_IP。"""
    # 检查 chain_proxy 本地端口
    try:
        sock = socket.create_connection(("127.0.0.1", 18890), timeout=3)
        sock.close()
    except Exception:
        return CRIT, "chain_proxy 127.0.0.1:18890 不可达"
    return OK, "chain_proxy 可达（出口IP需AWS验证）"


def _check_memory():
    """MemAvailable + Balloon 趋势，区分压力来源。

    2026-10-07（用户定调）：Balloon = 动态外生变量，不可控、不可依赖、只能观测。
    保守预算按有效可用约 3.2G、无 swap。阈值不变（300/400），只增加来源分类：
    - MemAvailable ↓ + Balloon 明显 ↑ → 宿主收紧资源
    - MemAvailable ↓ + Balloon 基本不变 → 自身进程/缓存增长
    - MemAvailable ↑ + Balloon 明显 ↓ → 宿主归还内存
    """
    MEM_TREND_FILE = os.path.join(STATE_DIR, "memory_trend.json")
    SIG_MB = 50  # 变化显著性阈值（MB）
    try:
        avail_mb = balloon_mb = None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_mb = int(line.split()[1]) // 1024
                elif line.startswith("Balloon:"):
                    balloon_mb = int(line.split()[1]) // 1024
                if avail_mb is not None and balloon_mb is not None:
                    break
        if avail_mb is None:
            return WARN, "无法读取内存"

        # 读上次快照，做趋势分类
        trend_note = ""
        prev = None
        try:
            if os.path.exists(MEM_TREND_FILE):
                with open(MEM_TREND_FILE) as f:
                    prev = json.load(f)
        except Exception:
            prev = None
        if prev and isinstance(prev, dict):
            pa = prev.get("avail_mb")
            pb = prev.get("balloon_mb")
            if isinstance(pa, int) and isinstance(pb, int) and balloon_mb is not None:
                d_avail = avail_mb - pa
                d_balloon = balloon_mb - pb
                if d_avail < -SIG_MB and d_balloon > SIG_MB:
                    trend_note = "（宿主收紧资源：Balloon +%dMB）" % d_balloon
                elif d_avail < -SIG_MB and abs(d_balloon) <= SIG_MB:
                    trend_note = "（自身增长：Balloon %s%dMB）" % (
                        "+" if d_balloon >= 0 else "", d_balloon)
                elif d_avail > SIG_MB and d_balloon < -SIG_MB:
                    trend_note = "（宿主归还内存：Balloon %dMB）" % d_balloon
        # 写本次快照
        try:
            with open(MEM_TREND_FILE, "w") as f:
                json.dump({"avail_mb": avail_mb, "balloon_mb": balloon_mb,
                           "ts": int(time.time())}, f)
        except Exception:
            pass

        b_str = "Balloon %dMB" % balloon_mb if balloon_mb is not None else "Balloon 未知"
        if avail_mb < 300:
            return CRIT, f"内存 {avail_mb}MB（<300，需停机），{b_str}{trend_note}"
        if avail_mb < 400:
            return WARN, f"内存 {avail_mb}MB（<400，research暂停），{b_str}{trend_note}"
        return OK, f"内存 {avail_mb}MB，{b_str}{trend_note}"
    except Exception:
        pass
    return WARN, "无法读取内存"


def _check_research():
    """research状态：并发、今日用量。"""
    try:
        db = get_db()
        running = db.execute(
            "SELECT COUNT(*) FROM research_jobs WHERE status='RUNNING'"
        ).fetchone()[0]
        now = int(time.time())
        day_start = now - (now % 86400)
        today = db.execute(
            "SELECT COUNT(*) FROM research_jobs WHERE created_at >= ?",
            (day_start,)).fetchone()[0]
        db.close()
        detail = f"并发{running}/1，今日{today}/10"
        if running > 1:
            return WARN, detail + "（并发超限）"
        return OK, detail
    except Exception as e:
        return WARN, f"查询失败: {e}"


def _check_pipeline():
    """流水线卡住检测（2026-10-07 新增）。
    抓"上游做完了、下游没人取"的静默故障：
    - WAITING_RESEARCH + job DONE 超过 5 分钟没人重写回复 → CRIT
      （2026-10-07 实证：cron worker 因平台 DEFCON 限流连续失败，
       DONE 的 job 3 分钟无人取，用户问了才发现）
    - NEW 且 should_reply IN (1,2) 超过 5 分钟没人领 → WARN
    - PENDING research job 超过 5 分钟没派发 → WARN
    """
    try:
        db = get_db()
        now = int(time.time())
        problems = []
        # 1. DONE 但没人取的 research
        rows = db.execute("""
            SELECT m.id, j.id, j.finished_at FROM messages m
            JOIN research_jobs j ON j.msg_id = m.id
            WHERE m.status='WAITING_RESEARCH' AND j.status='DONE'
              AND j.finished_at < ?
        """, (now - 300,)).fetchall()
        for mid, jid, _ in rows:
            problems.append((CRIT, f"msg#{mid} research job#{jid} DONE 超5分钟无人取"))
        # 2. 该回没人领的消息
        rows = db.execute("""
            SELECT id FROM messages
            WHERE status='NEW' AND should_reply IN (1,2)
              AND created_at < ?
        """, (now - 300,)).fetchall()
        if rows:
            problems.append((WARN, f"{len(rows)}条可回复消息超5分钟无人领"))
        # 3. 一直 PENDING 的 research
        rows = db.execute("""
            SELECT id FROM research_jobs
            WHERE status='PENDING' AND created_at < ?
        """, (now - 300,)).fetchall()
        if rows:
            problems.append((WARN, f"{len(rows)}个research job PENDING超5分钟未派发"))
        db.close()
        if not problems:
            return OK, "流水线无卡住"
        # 取最严重的
        problems.sort(key=lambda x: x[0])
        worst = problems[0][0]
        detail = "；".join(p[1] for p in problems)
        return worst, detail
    except Exception as e:
        return WARN, f"查询失败: {e}"


def _check_mute():
    """mute状态。"""
    try:
        db = get_db()
        muted, until = is_muted(db)
        db.close()
        if muted:
            left = (until - int(time.time())) // 60
            return WARN, f"静默中（剩余{left}分钟）"
        return OK, "未静默"
    except Exception as e:
        return WARN, f"查询失败: {e}"


def _check_worker():
    """worker心跳：event worker是否活跃。"""
    s = _read_json("event_worker.json")
    if s.get("active"):
        # 检查最近更新
        age = time.time() - s.get("updated_at", 0)
        if age > 900:  # 15分钟无更新
            return WARN, f"event worker 标记活跃但 {int(age)}秒无更新（假活？）"
        return OK, "event worker 活跃"
    return WARN, "event worker 未运行（2分钟cron兜底）"


def get_unified_health():
    """生成统一健康状态。"""
    checks = {
        "login": _check_login(),
        "wal": _check_wal(),
        "sendd": _check_process("sendd_v2.py"),
        "reader": _check_process("wal_reader_daemon.py"),
        "proxy": _check_proxy(),
        "memory": _check_memory(),
        "research": _check_research(),
        "mute": _check_mute(),
        "worker": _check_worker(),
        "pipeline": _check_pipeline(),
    }
    # 总体取最严重的
    overall = OK
    for _, (level, _) in checks.items():
        if level == CRIT:
            overall = CRIT
            break
        if level == WARN:
            overall = WARN

    result = {
        "overall": overall,
        "checked_at": int(time.time()),
        "checks": {
            k: {"level": lv, "detail": detail}
            for k, (lv, detail) in checks.items()
        },
    }
    # 写文件
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(UNIFIED_FILE, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    # 2026-10-09 22:13 用户要求：WAL 红超 1 小时，自动跑 OCR 核实
    # （区分真掉线 vs 群里没人说话的误报）
    try:
        wal_lv, wal_detail = checks.get("wal", (OK, ""))
        if wal_lv == CRIT:
            import re as _re
            m = _re.search(r"WAL (\d+)秒", wal_detail)
            if m and int(m.group(1)) > 3600:
                import subprocess as _sp
                _bridge = os.path.dirname(STATE_DIR)
                _sp.Popen(
                    ["python3", os.path.join(_bridge, "wal_ocr_verify.py")],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                    start_new_session=True)
    except Exception:
        pass
    return result


def print_health():
    h = get_unified_health()
    icons = {OK: "✅", WARN: "⚠️", CRIT: "🔴"}
    print(f"总体: {icons[h['overall']]} {h['overall']}")
    print()
    for name, c in h["checks"].items():
        print(f"  {icons[c['level']]} {name}: {c['detail']}")
    # 2026-10-08 用户睡眠边界规则落地：休息窗口激活时，CRIT 只记摘要、不上报。
    # （此前口头承诺过但未进代码，导致 03:47 CRIT 在睡眠边界后打扰用户。）
    if h["overall"] == CRIT:
        active, info = rest_window_active()
        if active:
            crits = [f"{k}: {c['detail']}"
                     for k, c in h["checks"].items() if c["level"] == CRIT]
            append_digest("健康 CRIT（休息窗口中已压住上报）\n\n"
                          + "\n".join(f"- {c}" for c in crits)
                          + f"\n\n窗口备注：{info.get('note', '')}")
            print()
            print("🛌 休息窗口激活中：CRIT 已记入 state/health_digest.md，"
                  "本次不上报用户（REST_SUPPRESSED）。")


if __name__ == "__main__":
    print_health()
