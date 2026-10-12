#!/usr/bin/env python3
"""
bridge_status.py - 微信桥接状态一览 (vNext §11)

轻量 CLI，无 Web UI。输出：
- 健康状态
- 队列统计
- sendd / reader 存活
- 最近发送 / NCC / 主动搭话
- 异常计数
"""
import sys
import os
import time
import json
import subprocess

sys.path.insert(0, '~/workspace/wechat-bridge')
from msgqueue import get_db


def pgrep_alive(pattern):
    safe = f"[{pattern[0]}]{pattern[1:]}" if pattern else pattern
    try:
        r = subprocess.run(['pgrep', '-f', safe],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def main():
    db = get_db()
    now = int(time.time())
    as_json = '--json' in sys.argv

    # 健康状态
    health = {}
    try:
        with open('~/workspace/wechat-bridge/state/health.json') as f:
            health = json.load(f)
    except Exception:
        pass
    hstate = health.get('state', 'UNKNOWN')
    wal_age = (health.get('signals', {}) or {}).get('wal_age_s', '?')

    # 队列
    q = {}
    for s in ['NEW', 'GENERATING', 'READY', 'SENDING', 'SENT', 'SKIPPED',
              'RETRY', 'DEAD', 'SEND_UNCERTAIN']:
        c = db.execute(
            "SELECT COUNT(*) FROM messages WHERE status=?", (s,)).fetchone()[0]
        if c:
            q[s] = c

    # 最近发送
    last_sent = db.execute(
        "SELECT id, sent_at FROM messages WHERE status='SENT' "
        "ORDER BY sent_at DESC LIMIT 1").fetchone()
    last_sent_str = (
        f"#{last_sent[0]} "
        f"{time.strftime('%H:%M', time.localtime(last_sent[1]))}"
        if last_sent and last_sent[1] else '-')

    # 退避中
    backoff = db.execute(
        "SELECT COUNT(*) FROM messages "
        "WHERE status='READY' AND not_before > ?", (now,)).fetchone()[0]

    # 主动搭话统计（24h）
    proactive_24h = db.execute(
        "SELECT COUNT(*) FROM messages WHERE action='PROACTIVE' "
        "AND created_at > ?", (now - 86400,)).fetchone()[0]

    # 异常计数
    uncertain = q.get('SEND_UNCERTAIN', 0)
    retry = q.get('RETRY', 0)
    dead = q.get('DEAD', 0)

    # research 状态
    research_active = db.execute(
        "SELECT COUNT(*) FROM messages "
        "WHERE research_status IN ('REQUESTED','RUNNING')").fetchone()[0]

    data = {
        'health': hstate,
        'wal_age_s': wal_age,
        'wechat_alive': pgrep_alive('wechat/wechat'),
        'reader_alive': pgrep_alive('wal_reader_daemon.py'),
        'sendd_alive': pgrep_alive('sendd_v2.py'),
        'queue': q,
        'backoff_pending': backoff,
        'last_sent': last_sent_str,
        'proactive_24h': proactive_24h,
        'send_uncertain': uncertain,
        'retry': retry,
        'dead': dead,
        'research_active': research_active,
    }

    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    # 人类可读输出
    icon = {'HEALTHY': '✅', 'STALE': '⚠️', 'LOGIN_REQUIRED': '🔴',
            'WECHAT_DOWN': '🔴', 'READER_DOWN': '🔴', 'SENDD_DOWN': '🔴',
            'UNKNOWN': '❓'}.get(hstate, '❓')
    print(f"{icon} Health: {hstate}  (WAL {wal_age}s 前)")
    print(f"   微信: {'活' if data['wechat_alive'] else '死'}  "
          f"Reader: {'活' if data['reader_alive'] else '死'}  "
          f"Sendd: {'活' if data['sendd_alive'] else '死'}")
    qs = ' '.join(f"{k}={v}" for k, v in sorted(q.items()))
    print(f"   队列: {qs or '(空)'}")
    print(f"   退避中: {backoff}  最近发送: {last_sent_str}  "
          f"主动(24h): {proactive_24h}")
    if uncertain or retry:
        print(f"   ⚠️ 异常: SEND_UNCERTAIN={uncertain} RETRY={retry} "
              f"DEAD={dead}")
    if research_active:
        print(f"   🔍 Research 进行中: {research_active}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
