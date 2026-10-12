#!/usr/bin/env python3
"""
微信掉线取证快照 - forensics_snapshot.py

在检测到掉线（LOGGED_IN → LOGIN_REQUIRED）时自动运行，
收集诊断掉线原因所需的全部现场数据。只读，不干扰客户端。

触发方式：
  1. login_watch.py 在状态变为 LOGIN_REQUIRED 时自动调用
  2. 手动：python3 forensics_snapshot.py [reason]

输出：~/workspace/wechat-bridge/state/forensics/<timestamp>_<reason>/
  - snapshot.json  结构化数据
  - wechat.log.tail 微信客户端日志尾部（掉线前最后现场）
  - screenshot.png  当时屏幕截图
  - ss.txt          网络连接快照

设计原则：
  - 30 秒内完成，不阻塞调用方
  - 所有采集都是只读的
  - 单个采集失败不影响其他项（try/except 包裹）
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path("~/workspace/wechat-bridge")
STATE_DIR = BASE / "state" / "forensics"
XWECHAT = Path("~/xwechat_files/wxid_YOUR_BOT_ACCOUNT")
DISPLAY = ":99"

CST = timezone(timedelta(hours=8))


def now_cst():
    return datetime.now(CST)


def run(cmd, timeout=8):
    """运行命令，失败返回 None"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def get_wechat_pid():
    out = run("pgrep -x wechat")
    return int(out.split()[0]) if out else None


def process_forensics(pid):
    """进程级取证"""
    d = {"pid": pid}
    try:
        # 启动时间、CPU、内存
        st = run(f"ps -o lstart,etime,%cpu,%mem,rss --no-headers -p {pid}")
        d["ps"] = st
        # 线程数和状态
        task_dir = Path(f"/proc/{pid}/task")
        if task_dir.exists():
            threads = []
            for t in task_dir.iterdir():
                try:
                    wchan = (t / "wchan").read_text().strip()
                    state = (t / "status").read_text().split("State:")[1].split()[0]
                    threads.append({"tid": t.name, "wchan": wchan, "state": state})
                except Exception:
                    pass
            d["threads"] = threads
            d["thread_count"] = len(threads)
        # 打开的文件描述符数
        fd_count = run(f"ls /proc/{pid}/fd 2>/dev/null | wc -l")
        d["fd_count"] = fd_count
    except Exception as e:
        d["error"] = str(e)
    return d


def network_forensics(pid):
    """网络级取证"""
    d = {}
    try:
        # wechat 进程的 TCP 连接
        ss_out = run(f"ss -tnp 2>/dev/null | grep 'pid={pid},' | head -20")
        d["wechat_conns"] = ss_out
        # 所有到 18890（chain_proxy）的连接
        proxy_conns = run("ss -tn 2>/dev/null | grep ':18890' | head -20")
        d["proxy_conns"] = proxy_conns
        # chain_proxy 进程状态
        cp_pid = run("pgrep -f 'chain_proxy.py'")
        d["chain_proxy_pid"] = cp_pid
        if cp_pid:
            d["chain_proxy_start"] = run(f"ps -o lstart --no-headers -p {cp_pid.split()[0]}")
        # chain_proxy 日志尾
        try:
            log = Path("/tmp/chain_proxy.log")
            if log.exists():
                d["chain_proxy_log_tail"] = log.read_text()[-2000:]
        except Exception:
            pass
    except Exception as e:
        d["error"] = str(e)
    return d


def aws_health_check():
    """AWS 出口快速健康检查（只做 TCP+握手，不中转数据）"""
    d = {"target": "YOUR_PROXY_IP:443"}
    try:
        # 必须经 3128 CONNECT（沙箱劫持直连）
        s = socket.socket()
        s.settimeout(10)
        s.connect(("YOUR_SANDBOX_PROXY_IP", 3128))
        s.sendall(b"CONNECT YOUR_PROXY_IP:443 HTTP/1.1\r\nHost: YOUR_PROXY_IP:443\r\n\r\n")
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(1024)
            if not chunk:
                break
            resp += chunk
        d["connect_3128"] = "200" in resp.split(b"\r\n")[0].decode(errors="ignore")
        if d["connect_3128"]:
            # SOCKS5 握手（不认证，只看服务是否响应）
            s.sendall(b"\x05\x01\x02")
            r = s.recv(2)
            d["socks5_responds"] = r == b"\x05\x02"
        s.close()
    except Exception as e:
        d["error"] = f"{type(e).__name__}: {e}"
    return d


def x11_forensics():
    """X11 窗口取证"""
    d = {"display": DISPLAY}
    try:
        out = run(
            f"DISPLAY={DISPLAY} python3 -c \""
            "from Xlib import display; "
            f"d = display.Display('{DISPLAY}'); "
            "wins = []; "
            "root = d.screen().root; "
            "[wins.append({'name': str(w.get_wm_name()), 'w': w.get_geometry().width, 'h': w.get_geometry().height}) "
            "for w in root.query_tree().children]; "
            "print(wins); d.close()\"",
            timeout=10,
        )
        d["windows"] = out
    except Exception as e:
        d["error"] = str(e)
    return d


def file_forensics():
    """文件级取证：会话文件、日志、DB 时间戳"""
    d = {}
    try:
        # 会话关键文件 mtime
        for rel in [
            "db_storage/session/session.db",
            "db_storage/session/session.db-wal",
            "config/login_config",
            "config/login_configv2",
        ]:
            p = XWECHAT / rel
            if p.exists():
                st = p.stat()
                d[rel] = {
                    "mtime": datetime.fromtimestamp(st.st_mtime, CST).isoformat(),
                    "size": st.st_size,
                }
        # all_users 登录密钥
        kp = Path("~/xwechat_files/all_users/login/wxid_YOUR_BOT_ACCOUNT/key_info.db")
        if kp.exists():
            st = kp.stat()
            d["key_info.db"] = {
                "mtime": datetime.fromtimestamp(st.st_mtime, CST).isoformat(),
                "size": st.st_size,
            }
        # 微信客户端日志尾部（关键！重启前保存现场）
        wlog = Path("/tmp/wechat.log")
        if wlog.exists():
            d["wechat_log_size"] = wlog.stat().st_size
            d["wechat_log_mtime"] = datetime.fromtimestamp(
                wlog.stat().st_mtime, CST
            ).isoformat()
    except Exception as e:
        d["error"] = str(e)
    return d


def session_age():
    """计算本次会话时长（用于验证 4 小时理论）"""
    d = {}
    try:
        # 从 login_watch 的历史找上次 LOGGED_IN 的时间
        log_file = BASE / "login_watch.log"
        if log_file.exists():
            lines = log_file.read_text().splitlines()
            # 找最近的 LOGGED_IN
            for line in reversed(lines[-500:]):
                if "state=LOGGED_IN" in line and "CHANGED" in line:
                    # 格式 [2026-10-07T01:12:xx+08:00]
                    ts_str = line.split("]")[0][1:]
                    ts = datetime.fromisoformat(ts_str)
                    age = now_cst() - ts
                    d["last_login"] = ts_str
                    d["session_age_seconds"] = int(age.total_seconds())
                    d["session_age_hours"] = round(age.total_seconds() / 3600, 2)
                    break
    except Exception as e:
        d["error"] = str(e)
    return d


def system_forensics():
    """系统级取证"""
    d = {}
    try:
        d["uptime"] = run("uptime")
        d["load"] = run("cat /proc/loadavg")
        # 内存
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(("MemTotal", "MemAvailable", "MemFree")):
                    k, v = line.split(":")
                    mem[k.strip()] = v.strip()
        d["memory"] = mem
    except Exception as e:
        d["error"] = str(e)
    return d


def main():
    reason = sys.argv[1] if len(sys.argv) > 1 else "manual"
    ts = now_cst()
    dirname = ts.strftime("%Y%m%d-%H%M%S") + f"_{reason}"
    outdir = STATE_DIR / dirname
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[forensics] 采集掉线现场: {dirname}", flush=True)

    snapshot = {
        "timestamp": ts.isoformat(),
        "reason": reason,
        "session": session_age(),
        "process": {},
        "network": {},
        "aws": {},
        "x11": {},
        "files": {},
        "system": {},
    }

    pid = get_wechat_pid()
    if pid:
        snapshot["process"] = process_forensics(pid)
        snapshot["network"] = network_forensics(pid)
    else:
        snapshot["process"] = {"error": "wechat process not found"}

    # AWS 检查独立于 wechat 进程
    snapshot["aws"] = aws_health_check()
    snapshot["x11"] = x11_forensics()
    snapshot["files"] = file_forensics()
    snapshot["system"] = system_forensics()

    # 保存结构化数据
    with open(outdir / "snapshot.json", "w") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)

    # 保存 wechat.log 尾部（单独文件，方便直接看）
    try:
        wlog = Path("/tmp/wechat.log")
        if wlog.exists():
            content = wlog.read_text(errors="ignore")
            (outdir / "wechat.log.tail").write_text(content[-20000:])
    except Exception:
        pass

    # 截图
    try:
        shot = outdir / "screenshot.png"
        run(f"DISPLAY={DISPLAY} ~/bin/scrot -z {shot}", timeout=10)
    except Exception:
        pass

    # 网络连接快照（单独文件）
    try:
        ss_all = run("ss -tnp 2>/dev/null | head -40", timeout=10)
        if ss_all:
            (outdir / "ss.txt").write_text(ss_all)
    except Exception:
        pass

    print(f"[forensics] 完成: {outdir}", flush=True)
    print(f"[forensics] 会话时长: {snapshot['session'].get('session_age_hours', '?')} 小时", flush=True)
    return str(outdir)


if __name__ == "__main__":
    main()
