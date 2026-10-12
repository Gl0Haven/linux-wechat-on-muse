#!/usr/bin/env python3
"""
health_monitor.py - 微信桥接统一健康状态

只做状态汇总，不直接操作微信（vNext §6）。

输入信号：
- login_watch.py: LOGGED_IN / LOGIN_REQUIRED / UNKNOWN
- WAL/DB 最近写入时间
- wal_reader_daemon / sendd_v2 进程存活
- 微信进程 / Xvfb 存活
- sync_heartbeat 触发计数

输出 state/health.json:
{
  "state": "HEALTHY|STALE|LOGIN_REQUIRED|GUI_BLOCKED|READER_DOWN|SENDD_DOWN|WECHAT_DOWN|UNKNOWN",
  "since": <unix timestamp>,
  "signals": {...}
}

优先级（严重→普通）:
WECHAT_DOWN > LOGIN_REQUIRED > READER_DOWN > SENDD_DOWN > STALE > HEALTHY
"""
import sys
import os
import time
import json
import subprocess
import glob

sys.path.insert(0, '~/workspace/wechat-bridge')
from msgqueue import DB_PATH

STATE_DIR = '~/workspace/wechat-bridge/state'
HEALTH_JSON = os.path.join(STATE_DIR, 'health.json')
WXID = 'wxid_YOUR_BOT_ACCOUNT'

# 阈值（秒）
# 2026-10-06 修：深夜 23:00–08:00 放宽（用户批准），与 unified_health.py 保持一致
def _stale_threshold():
    import datetime
    bj_hour = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))).hour
    if bj_hour >= 23 or bj_hour < 8:
        return 900  # 深夜 15 分钟
    return 300      # 白天 5 分钟

STALE_THRESHOLD = 300      # 默认值（白天）；实际用 _stale_threshold() 动态取
SYNC_KICK_THRESHOLD = 3    # sync_heartbeat 连续触发3次 → 值得注意


def run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip()
    except Exception:
        return False, ''


def pgrep_alive(pattern):
    # 用 [x] 技巧避免 pgrep -f 匹配到自身
    # e.g. "sendd_v2.py" → "[s]endd_v2.py"
    safe = f"[{pattern[0]}]{pattern[1:]}" if pattern else pattern
    ok, _ = run(['pgrep', '-f', safe])
    return ok


def get_login_state():
    """从 login_watch.log 读最近状态"""
    log_path = '~/workspace/wechat-bridge/login_watch.log'
    try:
        with open(log_path) as f:
            lines = f.readlines()
        for line in reversed(lines[-20:]):
            if 'state=LOGGED_IN' in line:
                return 'LOGGED_IN'
            if 'state=LOGIN_REQUIRED' in line:
                return 'LOGIN_REQUIRED'
            if 'state=UNKNOWN' in line:
                return 'UNKNOWN'
    except Exception:
        pass
    return 'UNKNOWN'


def get_db_age():
    """message DB 距上次写入秒数"""
    pattern = os.path.expanduser(
        f'~/xwechat_files/{WXID}_1a29/db_storage/message/message_0.db*')
    now = time.time()
    newest = 0
    for f in glob.glob(pattern):
        try:
            mtime = os.path.getmtime(f)
            if mtime > newest:
                newest = mtime
        except OSError:
            pass
    if newest == 0:
        return None
    return int(now - newest)


def get_queue_stats():
    # 2026-10-12 修复：只读连接，避免 DDL 与写进程抢锁导致 health_monitor 崩溃
    # （get_db() 会跑 SCHEMA DDL，曾因 database is locked 崩掉整轮检查）
    from msgqueue import DB_PATH
    import sqlite3
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    try:
        stats = {}
        for s in ['NEW', 'GENERATING', 'READY', 'SENDING', 'SENT', 'SKIPPED',
                  'RETRY', 'DEAD', 'SEND_UNCERTAIN']:
            c = db.execute(
                "SELECT COUNT(*) FROM messages WHERE status=?", (s,)).fetchone()[0]
            if c:
                stats[s] = c
        return stats
    finally:
        db.close()


def get_sync_kick_count():
    """最近1小时 sync_heartbeat 实际点击次数（从日志数）"""
    # 简单实现：读脚本输出日志（如果有）
    # 当前版本只在 cron 输出里，暂返回 0，由 bridge_status 扩展
    return 0


def determine_state(signals):
    """按优先级判定"""
    if not signals['wechat_alive']:
        return 'WECHAT_DOWN'
    if signals['login'] == 'LOGIN_REQUIRED':
        return 'LOGIN_REQUIRED'
    if not signals['reader_alive']:
        return 'READER_DOWN'
    if not signals['sendd_alive']:
        return 'SENDD_DOWN'
    wal_age = signals.get('wal_age_s')
    if wal_age is not None and wal_age > _stale_threshold():
        return 'STALE'
    if signals['login'] == 'UNKNOWN':
        return 'UNKNOWN'
    return 'HEALTHY'


def main():
    now = int(time.time())
    signals = {
        'login': get_login_state(),
        'wal_age_s': get_db_age(),
        'wechat_alive': pgrep_alive('wechat/wechat'),
        'xvfb_alive': pgrep_alive('Xvfb :99'),
        'reader_alive': pgrep_alive('wal_reader_daemon.py'),
        'sendd_alive': pgrep_alive('sendd_v2.py'),
        'queue': get_queue_stats(),
        'sync_kick_count_1h': get_sync_kick_count(),
    }

    state = determine_state(signals)

    # 读取上次状态，计算 since
    prev = {}
    try:
        with open(HEALTH_JSON) as f:
            prev = json.load(f)
    except Exception:
        pass

    if prev.get('state') == state:
        since = prev.get('since', now)
    else:
        since = now

    output = {
        'state': state,
        'since': since,
        'checked_at': now,
        'signals': signals,
    }

    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = HEALTH_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    os.rename(tmp, HEALTH_JSON)

    print(f"[{time.strftime('%H:%M:%S')}] health={state} "
          f"wal_age={signals['wal_age_s']}s login={signals['login']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
