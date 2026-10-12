#!/usr/bin/env python3
"""
fingerprint_snapshot.py - 系统指纹快照

VM 重置后对比用，找出微信掉线的真正原因。
保存到 ~/workspace/wechat-bridge/state/fingerprint.json
"""
import json
import os
import subprocess
import time

STATE_FILE = '~/workspace/wechat-bridge/state/fingerprint.json'


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ''


def get_fingerprint():
    fp = {'captured_at': int(time.time())}

    # 基础标识
    fp['hostname'] = run(['hostname'])
    try:
        fp['machine_id'] = open('/etc/machine-id').read().strip()
    except Exception:
        fp['machine_id'] = ''

    # 网络
    fp['mac'] = run(['bash', '-c',
                     'ip link show | grep -m1 "link/ether" | grep -oP "(?<=link/ether )[^ ]+\"'])
    # 去掉引号问题，用更简单的方式
    mac_raw = run(['bash', '-c', 'cat /sys/class/net/*/address 2>/dev/null | head -5'])
    fp['mac_all'] = mac_raw.replace('\n', ',')

    # 出口 IP（经代理）
    try:
        r = subprocess.run(
            ['curl', '-s', '--max-time', '10', '-x', 'http://YOUR_SANDBOX_PROXY_IP:3128',
             'https://api.ipify.org'],
            capture_output=True, text=True, timeout=15)
        fp['egress_ipv4'] = r.stdout.strip()
    except Exception:
        fp['egress_ipv4'] = ''

    # CPU / 内存（微信可能用这些做指纹）
    fp['cpuinfo'] = run(['bash', '-c',
                         'grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | xargs'])
    fp['mem_total'] = run(['bash', '-c',
                           'grep MemTotal /proc/meminfo | awk "{print $2}"'])

    # 时区 / locale
    fp['timezone'] = run(['bash', '-c', 'timedatectl show --property=Timezone 2>/dev/null || cat /etc/timezone 2>/dev/null || echo ""'])

    # 微信相关文件 mtime（判断哪些被重置了）
    wx_base = os.path.expanduser('~/xwechat_files/wxid_YOUR_BOT_ACCOUNT')
    fp['wechat_files'] = {}
    for sub in ['config/login_config', 'config/login_configv2']:
        p = os.path.join(wx_base, sub)
        try:
            fp['wechat_files'][sub] = int(os.path.getmtime(p))
        except OSError:
            fp['wechat_files'][sub] = 0

    return fp


def main():
    fp = get_fingerprint()

    # 读取旧的对比
    old = {}
    try:
        with open(STATE_FILE) as f:
            old = json.load(f)
    except Exception:
        pass

    # 保存新的
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(fp, f, indent=2)

    # 对比输出
    print(f"[{time.strftime('%H:%M:%S')}] fingerprint saved")
    if old:
        print("\n=== 变化对比 (VM 重置检测) ===")
        changed = False
        for k in sorted(set(list(old.keys()) + list(fp.keys()))):
            if k in ('captured_at', 'wechat_files'):
                continue
            ov, nv = old.get(k, ''), fp.get(k, '')
            if ov != nv:
                changed = True
                print(f"  {k}:")
                print(f"    旧: {ov}")
                print(f"    新: {nv}")
        if not changed:
            print("  无变化")
    else:
        print("  (首次快照，无对比基线)")

    return 0


if __name__ == '__main__':
    exit(main())
