#!/usr/bin/env python3
"""
微信登录态监测器（含自动恢复）

三态：
  LOGGED_IN      - 主聊天窗口存在（大尺寸 Weixin 窗口）
  LOGIN_REQUIRED - 只检测到小尺寸窗口（登录框），疑似掉线
  UNKNOWN        - 无法判定（进程不在、无窗口、尺寸模糊等），不触发任何动作

自动恢复（2026-10-08 用户批准，打破 10-06"只观测不自动重登"决定）：
  LOGIN_REQUIRED 时自动点 Enter Weixin 按钮（640,509），最多 3 轮，
  每轮等 20s。自动登录已 4 次验证无需手机确认（21:17/22:28/22:35/22:43）。

每次运行记录：Xvfb/微信进程存活、窗口列表（含尺寸）、
微信 DB/WAL 最近写入时间。状态变化时自动截图并记时间，
便于和 Sentinel 网络审批、GUI 操作时间对齐定位根因。

用法: python3 login_watch.py [--shot-dir DIR]
退出码永远 0。单行结果输出到 stdout：LOGIN_STATE=<STATE>
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

BRIDGE = Path.home() / "workspace" / "wechat-bridge"
STATE_FILE = BRIDGE / "state" / "login_state.json"
LOG_FILE = BRIDGE / "login_watch.log"
SHOT_DIR = BRIDGE / "state" / "login_shots"
XW_DIR = Path.home() / "xwechat_files" / "wxid_YOUR_BOT_ACCOUNT"

# 窗口尺寸阈值（1280x800 虚拟屏实测）
#   主窗口约 913x667；扫码登录框约 300x430
LOGGED_IN_MIN_W = 700   # 宽度 >=700 视为主窗口
LOGIN_MAX_W = 500       # 宽度 <500 视为登录框

TZ = timezone(timedelta(hours=8))


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()[:200]
    except Exception as e:
        return -1, "", str(e)[:200]


def pgrep_alive(pattern):
    rc, out, _ = run(["pgrep", "-f", pattern])
    return rc == 0 and bool(out)


def wechat_windows():
    """返回 [(wid, name, w, h, x, y)]，失败返回 []
    2026-10-06: 改用 python-xlib，不再依赖 xdotool（VM 重置后 libxdo.so.3 丢失导致误报）"""
    wins = []
    try:
        from Xlib import display
        d = display.Display(':99')
        root = d.screen().root
        for w in root.query_tree().children:
            try:
                name = w.get_wm_name() or ''
                if 'Weixin' not in name and 'wechat' not in name.lower():
                    continue
                g = w.get_geometry()
                # 取绝对坐标
                try:
                    ap = w.translate_coords(root, 0, 0)
                    x, y = ap.x, ap.y
                except Exception:
                    x, y = g.x, g.y
                wins.append({"wid": str(w.id), "name": name,
                             "w": g.width, "h": g.height, "x": x, "y": y})
            except Exception:
                pass
        d.close()
    except Exception:
        pass
    return wins


def db_mtimes():
    """微信关键文件的最近写入时间（用于判断是否还在同步）"""
    out = {}
    targets = {
        "message_db": XW_DIR / "db_storage" / "message" / "message_0.db",
        "session_db": XW_DIR / "db_storage" / "session" / "session.db",
        "login_cfg": XW_DIR / "config" / "login_configv2",
    }
    for k, p in targets.items():
        try:
            if p.exists():
                ts = p.stat().st_mtime
                out[k] = datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")
            else:
                out[k] = None
        except Exception:
            out[k] = None
    return out


def take_shot(reason):
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    safe = reason.replace(">", "to").replace("/", "_")
    ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    path = SHOT_DIR / f"{ts}-{safe}.png"
    env = dict(os.environ, DISPLAY=":99")
    try:
        subprocess.run(
            [str(Path.home() / "bin" / "scrot"), "-z", str(path)],
            timeout=15, env=env, capture_output=True)
    except Exception:
        pass
    return str(path) if path.exists() else None


def load_prev():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def detect_state():
    """返回 (state, detail, wins, xvfb_alive, wechat_alive)"""
    xvfb_alive = pgrep_alive("Xvfb :99")
    wechat_alive = pgrep_alive("~/wechat/wechat")
    wins = wechat_windows() if xvfb_alive else []
    if not wechat_alive or not xvfb_alive:
        return "UNKNOWN", "process missing", wins, xvfb_alive, wechat_alive
    if not wins:
        return "UNKNOWN", "no Weixin window", wins, xvfb_alive, wechat_alive
    max_w = max(w["w"] for w in wins)
    if max_w >= LOGGED_IN_MIN_W:
        return ("LOGGED_IN",
                f"main window {max_w}x{[w['h'] for w in wins if w['w']==max_w][0]}",
                wins, xvfb_alive, wechat_alive)
    elif max_w <= LOGIN_MAX_W and max_w > 0:
        return ("LOGIN_REQUIRED",
                f"small window {max_w}x{[w['h'] for w in wins if w['w']==max_w][0]}, likely login dialog",
                wins, xvfb_alive, wechat_alive)
    else:
        return "UNKNOWN", f"ambiguous size {max_w}", wins, xvfb_alive, wechat_alive


def click_enter_weixin():
    """点 Enter Weixin 按钮（640,509，1280x800 实测）。返回 True/False。
    在二维码屏上点是无害空点；在账号选择屏上即登录。"""
    try:
        from Xlib import X, display
        from Xlib.ext import xtest
        d = display.Display(':99')
        root = d.screen().root
        root.warp_pointer(640, 509)
        d.sync()
        time.sleep(0.3)
        xtest.fake_input(d, X.ButtonPress, 1)
        d.sync()
        time.sleep(0.15)
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync()
        d.close()
        return True
    except Exception:
        return False


def main():
    ts = now_iso()
    state, detail, wins, xvfb_alive, wechat_alive = detect_state()
    mtimes = db_mtimes()

    prev = load_prev()
    prev_state = prev.get("state")
    changed = prev_state != state

    shot = None
    forensics_dir = None
    forensics_error = None
    if changed and state in ("LOGIN_REQUIRED", "LOGGED_IN"):
        # 状态变化（尤其掉线/恢复）时截图留证
        shot = take_shot(f"{prev_state or 'NONE'}-{state}")
        # 掉线瞬间自动取证：LOGGED_IN → LOGIN_REQUIRED 时收集现场
        # （必须在任何重启/恢复动作之前，此时证据最新鲜）
        if state == "LOGIN_REQUIRED" and prev_state == "LOGGED_IN":
            try:
                out = run(
                    "python3 ~/workspace/wechat-bridge/forensics_snapshot.py dropout",
                    timeout=45,
                )
                # 取最后一行 "完成: <路径>"
                for line in (out or "").splitlines():
                    if "完成:" in line:
                        forensics_dir = line.split("完成:")[-1].strip()
            except Exception as e:
                forensics_error = str(e)

    # 自动恢复（2026-10-08 用户批准）：LOGIN_REQUIRED 时点 Enter Weixin。
    # 只在仅小窗口时尝试（主窗口存在时不动，避免干扰用户手动操作）；
    # 最多 3 轮，每轮等 20s；二维码屏上是无害空点。
    auto_recovered = False
    if state == "LOGIN_REQUIRED":
        for attempt in range(1, 4):
            with open(LOG_FILE, "a") as f:
                f.write(f"[{now_iso()}] auto-recover attempt {attempt}: clicking Enter Weixin\n")
            click_enter_weixin()
            time.sleep(20)
            state2, detail2, wins2, _, _ = detect_state()
            with open(LOG_FILE, "a") as f:
                f.write(f"[{now_iso()}] auto-recover attempt {attempt}: state={state2} detail={detail2}\n")
            if state2 == "LOGGED_IN":
                state, detail, wins = state2, detail2, wins2
                auto_recovered = True
                changed = True
                break
        if not auto_recovered:
            with open(LOG_FILE, "a") as f:
                f.write(f"[{now_iso()}] auto-recover FAILED after 3 attempts, need user\n")

    # 持久化（rec 在恢复动作之后构建，记录最终状态）
    rec = {
        "ts": ts,
        "state": state,
        "prev_state": prev_state,
        "detail": detail,
        "xvfb_alive": xvfb_alive,
        "wechat_alive": wechat_alive,
        "windows": wins,
        "db_mtimes": mtimes,
        "auto_recovered": auto_recovered,
    }
    if shot:
        rec["screenshot"] = shot
    if forensics_dir:
        rec["forensics"] = forensics_dir
    if forensics_error:
        rec["forensics_error"] = forensics_error
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(rec, ensure_ascii=False, indent=1))

    # 日志：变化必记；未变化则只记心跳（避免刷屏，每次一行即可）
    with open(LOG_FILE, "a") as f:
        flag = "CHANGED" if changed else "heartbeat"
        f.write(f"[{ts}] {flag} state={state} prev={prev_state} "
                f"detail={detail} xvfb={xvfb_alive} wechat={wechat_alive} "
                f"db={json.dumps(mtimes, ensure_ascii=False)}"
                + (f" shot={shot}" if shot else "") + "\n")

    # 单行结果给 keepalive.sh / cron 用
    extra = f" changed {prev_state}->{state}" if changed else ""
    extra += f" shot={shot}" if shot else ""
    print(f"LOGIN_STATE={state}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
