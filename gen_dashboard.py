#!/usr/bin/env python3
"""生成微信桥接实时监控面板，每分钟由 cron 调用"""
import json, sqlite3
from datetime import datetime, timezone, timedelta
import subprocess

# 只读连接：面板只做 SELECT，避开 get_db() 的 DDL 迁移（DROP INDEX/CREATE TABLE
# 需要排他写锁，常驻写者持有长事务时会报 "database is locked"（2026-10-11 14:06
# 起连撞两次）。只读连接不受写锁影响，且在 WAL 下不会阻塞写者。

# 面板面向用户展示，用北京时间（UTC+8），与消息时间戳的 +8 hours 一致
CST = timezone(timedelta(hours=8))

def get_procs():
    result = {}
    for name, pattern in [('wal_reader', 'wal_reader_daemon.py'),
                          ('sendd_v2', 'sendd_v2.py'),
                          ('weixin', 'weixin')]:
        try:
            out = subprocess.run(['pgrep', '-f', pattern],
                               capture_output=True, text=True, timeout=5)
            count = len([l for l in out.stdout.strip().split('\n') if l])
            result[name] = count
        except:
            result[name] = 0
    return result

def main():
    db = sqlite3.connect(
        'file:~/workspace/wechat-bridge/queue.db?mode=ro',
        uri=True, timeout=30)
    now = datetime.now(timezone.utc).astimezone(CST).strftime('%Y-%m-%d %H:%M:%S')

    # 队列统计
    stats = dict(db.execute(
        "SELECT status, COUNT(*) FROM messages WHERE chat='YOUR_MAIN_GROUP@chatroom' GROUP BY status"
    ).fetchall())

    # 最近20条
    rows = db.execute("""
        SELECT id, sender_name, direction, should_reply, status,
               datetime(msg_time,'unixepoch','+8 hours'),
               substr(COALESCE(content,''),1,120),
               substr(COALESCE(reply_content,''),1,120)
        FROM messages WHERE chat='YOUR_MAIN_GROUP@chatroom'
        ORDER BY msg_time DESC LIMIT 20
    """).fetchall()

    msgs_html = ""
    for r in rows:
        id, name, dir, sr, st, ts, content, reply = r
        if dir == 'INBOUND':
            text = content
            cls = 'msg-in'
        else:
            text = reply or content
            if text.startswith('{'):
                try:
                    j = json.loads(text)
                    if 'messages' in j:
                        text = ' | '.join(j['messages'])
                except: pass
            cls = 'msg-out'
        if st == 'SKIPPED':
            cls = 'msg-skip'
        time_short = ts[11:16] if len(ts) > 10 else ts
        msgs_html += f'<tr><td>{time_short}</td><td>{id}</td><td class="{cls}">{name}</td><td>{dir}</td><td class="status-{st}">{st}</td><td>{text[:100]}</td></tr>\n'

    # 健康状态
    try:
        with open('~/workspace/wechat-bridge/state/health.json') as f:
            health = json.load(f)
        h_state = health.get('state', 'UNKNOWN')
        h_signals = health.get('signals', {})
        login = h_signals.get('login', '?')
        wal_age = h_signals.get('wal_age_s', '?')
    except:
        h_state, login, wal_age = 'UNKNOWN', '?', '?'

    procs = get_procs()

    # 日志
    try:
        with open('~/workspace/wechat-bridge/wal_reader.log') as f:
            log_lines = f.readlines()[-12:]
        log_html = ''.join(log_lines).replace('<', '&lt;').replace('>', '&gt;')
    except:
        log_html = '(无日志)'

    # 状态徽章
    def badge(text, ok=True):
        cls = 'ok' if ok else 'bad'
        return f'<span class="badge {cls}">{text}</span>'

    h_badge = badge(h_state, h_state == 'HEALTHY')
    login_badge = badge(login, login == 'LOGGED_IN')
    wr_warn = procs['wal_reader'] != 1
    sd_warn = procs['sendd_v2'] != 1

    stats_html = ""
    for k in ['SENT', 'NEW', 'READY', 'SKIPPED', 'DEAD', 'GENERATING', 'SEND_UNCERTAIN']:
        v = stats.get(k, 0)
        color = {'SENT': '#52c41a', 'NEW': '#fa8c16', 'GENERATING': '#722ed1'}.get(k, '')
        style = f' style="color:{color}"' if color else ''
        stats_html += f'<div class="stat"><span>{k}</span><span class="v"{style}>{v}</span></div>\n'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>微信桥接监控</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f0f2f5;color:#333;font-size:14px;line-height:1.6;padding:16px}}
h1{{font-size:20px}}.subtitle{{color:#888;font-size:12px;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:16px}}
.card{{background:#fff;border-radius:8px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.card h2{{font-size:15px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #eee}}
.badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600}}
.badge.ok{{background:#e6f7e6;color:#1a7f1a}}.badge.bad{{background:#fde8e8;color:#c0392b}}
.stat{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #f5f5f5;font-size:13px}}
.stat .v{{font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;padding:6px 8px;background:#f8f9fa;color:#666;position:sticky;top:0}}
td{{padding:6px 8px;border-top:1px solid #f0f0f0;vertical-align:top}}
.msg-in{{color:#1890ff}}.msg-out{{color:#52c41a}}.msg-skip{{color:#999}}
.status-NEW{{color:#fa8c16;font-weight:600}}.status-SENT{{color:#52c41a}}
.status-READY{{color:#1890ff;font-weight:600}}.status-SKIPPED{{color:#999}}
.status-GENERATING{{color:#722ed1;font-weight:600}}
.log{{background:#1e1e1e;color:#d4d4d4;border-radius:6px;padding:12px;font-family:monospace;font-size:11px;max-height:250px;overflow-y:auto;white-space:pre-wrap}}
.full{{grid-column:1/-1}}
.refresh{{color:#1890ff;font-size:12px}}
</style></head><body>
<h1>🤖 微信桥接监控 <span class="refresh">（60秒自动刷新）</span></h1>
<div class="subtitle">更新于 {now} · [主群]群</div>
<div class="grid">
<div class="card"><h2>📊 系统状态</h2>
<div class="stat"><span>整体健康</span>{h_badge}</div>
<div class="stat"><span>微信登录</span>{login_badge}</div>
<div class="stat"><span>wal_reader</span><span class="v">{procs['wal_reader']} 个{' ⚠️' if wr_warn else ' ✓'}</span></div>
<div class="stat"><span>sendd_v2</span><span class="v">{procs['sendd_v2']} 个{' ⚠️' if sd_warn else ' ✓'}</span></div>
<div class="stat"><span>微信进程</span><span class="v">{procs['weixin']} 个</span></div>
<div class="stat"><span>WAL 延迟</span><span class="v">{wal_age} 秒</span></div>
</div>
<div class="card"><h2>📬 队列统计</h2>{stats_html}</div>
<div class="card"><h2>⚠️ 待处理</h2>
<div style="font-size:13px;color:#666">
{"✅ 进程均为单实例" if not (wr_warn or sd_warn) else "⚠️ 有重复进程，keepalive 会自动清理"}<br><br>
NEW 消息会被 worker 每2分钟处理。<br>
GENERATING 卡住超过10分钟需人工看。
</div></div>
</div>
<div class="grid"><div class="card full"><h2>💬 最近20条</h2>
<div style="max-height:400px;overflow-y:auto"><table>
<tr><th>时间</th><th>ID</th><th>发送者</th><th>方向</th><th>状态</th><th>内容</th></tr>
{msgs_html}</table></div></div></div>
<div class="grid"><div class="card full"><h2>📝 wal_reader 日志</h2>
<div class="log">{log_html}</div></div></div>
</body></html>"""

    with open('~/workspace/wechat-bridge/dashboard.html', 'w') as f:
        f.write(html)
    print(f"面板已更新 {now}")

if __name__ == '__main__':
    main()
