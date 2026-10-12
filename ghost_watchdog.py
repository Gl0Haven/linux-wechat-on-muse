#!/usr/bin/env python3
"""
幽灵看门狗（2026-10-09）

每分钟检查一次：
1. cron.status 看 in_flight 的 run
2. 如果 in_flight 超过 5 分钟，检查是否有对应进程
3. 无进程 = 疑似幽灵，记录告警到文件

注意：看门狗清不掉幽灵，只能早发现、早告警。
真正的清理要靠 timeout 或平台。
"""

import json
import subprocess
import time
import os

ALERT_FILE = os.path.expanduser(
    "~/workspace/wechat-bridge/state/ghost_alerts.jsonl")
CHECK_INTERVAL = 60  # 每分钟
GHOST_THRESHOLD = 300  # 5 分钟无进程 = 疑似幽灵


def log_alert(job_id, run_id, detail):
    entry = {
        "ts": int(time.time()),
        "job_id": job_id,
        "run_id": run_id,
        "detail": detail,
    }
    with open(ALERT_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[watchdog] GHOST ALERT: {job_id} {run_id} {detail}", flush=True)


def check_job(job_id):
    """用 cron.status 检查（通过 hatch CLI 或 API）"""
    # 这里用 Python 直接调 cron 工具不方便，改用子进程调 hatch
    # 简化：只检查 wechat-bridge-reply 和 wechat-event-worker
    pass


def main():
    print("[watchdog] 幽灵看门狗启动", flush=True)
    # 单例
    lockfile = os.path.expanduser(
        "~/workspace/wechat-bridge/state/watchdog.lock")
    try:
        import fcntl
        lf = open(lockfile, "w")
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        print("[watchdog] 已有实例，退出", flush=True)
        return

    # TODO: 需要 Muse 平台的 cron.status API
    # 目前只能靠主 agent 定期检查
    # 先做本地进程健康检查
    while True:
        try:
            # 检查关键进程
            for proc, name in [
                ("wal_reader_daemon", "wal_reader"),
                ("sendd_v2", "sendd"),
                ("scheduler.py", "scheduler"),
            ]:
                r = subprocess.run(
                    ["pgrep", "-f", f"[{proc[0]}]{proc[1:]}"],
                    capture_output=True, timeout=5)
                if r.returncode != 0:
                    log_alert("local", name, "进程丢失")
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[watchdog] 错误: {e}", flush=True)
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
