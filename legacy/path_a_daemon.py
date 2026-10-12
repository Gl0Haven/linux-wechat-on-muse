#!/usr/bin/env python3
"""
Path A 发信 daemon（M1/M2）：外部 CLI → fifo → BUILD 文本注入 → 当前聚焦会话。

原理（已验证 Batch 4-D）：
  1. 经 gadget-cmd.json 下发 arm_build_patch（from=占位符, to=目标文本，等长，ASCII，1..22 字符）
  2. 用 send-ui.py 把占位符粘贴进输入框 + 回车，触发 Qt slot → 官方链路
  3. BUILD onLeave 时 gadget 原地替换 [q0+0x5c8] 的 SSO 文本（等长）
  4. 以 build_patch_done + POST_A CASE0 为双重 ack

约束：
  - v1 仅 ASCII（arm_build_patch 的长度校验按 JS 字符串长度，CJK 会 mismatch）
  - 占位符与目标文本必须等长
  - 一次只处理一条，arm 是一次性的

用法：
  path_a_daemon.py daemon            # 常驻：读 /tmp/wx_send.fifo
  path_a_daemon.py send "<文本>"     # 单发测试（自动聚焦测试群，发完恢复焦点）
"""
import json
import os
import sys
import time
import uuid
import subprocess

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
STATE = os.path.join(BRIDGE, "state")
CMD_PATH = os.path.join(STATE, "gadget-cmd.json")
RESP_PATH = os.path.join(STATE, "gadget-resp.jsonl")
GADGET_LOG = os.path.join(BRIDGE, "frida-re/gadget.log")
FIFO_PATH = "/tmp/wx_send.fifo"
SEND_UI = os.path.join(BRIDGE, "send-ui.py")

TESTROOM_POS = (150, 220)      # testroom@chatroom（[测试群]）
MAINROOM_POS = (150, 160)      # YOUR_MAIN_GROUP@chatroom（[主群]）

PLACEHOLDER_PREFIX = "WXA"


def placeholder_for(msg):
    n = len(msg)
    if n >= 4:
        return PLACEHOLDER_PREFIX + "P" * (n - 3)
    return "P" * n


def gadget_cmd(cmd, timeout=10):
    """下发 gadget 命令并等回执，返回 (ok, result_dict_or_error)."""
    cid = "pa-" + uuid.uuid4().hex[:8]
    cmd = dict(cmd)
    cmd["id"] = cid
    with open(CMD_PATH, "w") as f:
        json.dump(cmd, f)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.2)
        try:
            with open(RESP_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("id") == cid:
                        return r.get("ok", False), r
        except FileNotFoundError:
            pass
    return False, {"error": "timeout waiting resp"}


def log_tail_after(marker_line):
    """返回 gadget.log 当前行数（用作基线）。"""
    with open(GADGET_LOG, "rb") as f:
        f.seek(0, 2)
        return f.tell()


def wait_log_event(pred, timeout, since_pos, poll=0.3):
    """等 gadget.log 出现满足 pred(dict) 的事件行。"""
    deadline = time.time() + timeout
    pos = since_pos
    while time.time() < deadline:
        time.sleep(poll)
        with open(GADGET_LOG, "rb") as f:
            f.seek(pos)
            data = f.read()
            pos = f.tell()
        for line in data.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if pred(ev):
                return ev
    return None


def ui(cmd_args):
    r = subprocess.run([sys.executable, SEND_UI] + cmd_args,
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def ensure_login():
    r = subprocess.run([sys.executable, os.path.join(BRIDGE, "login_watch.py")],
                       capture_output=True, text=True, timeout=30)
    return "LOGGED_IN" in r.stdout


def path_a_send(msg, verify_db=True):
    """单发。返回 (ok, detail)。"""
    # 校验
    if not msg or len(msg) > 22:
        return False, "msg length must be 1..22"
    try:
        msg.encode("ascii")
    except UnicodeEncodeError:
        return False, "v1 ASCII only"
    ph = placeholder_for(msg)
    assert len(ph) == len(msg)

    if not ensure_login():
        return False, "not LOGGED_IN"

    # 1. 武装
    ok, res = gadget_cmd({"cmd": "arm_build_patch", "from": ph, "to": msg})
    if not ok:
        return False, "arm failed: %s" % res.get("error")
    base = log_tail_after(None)

    # 2. 粘贴占位符 + 回车
    if not ui(["paste_text", ph]):
        return False, "paste_text failed"
    time.sleep(0.5)
    if not ui(["press_enter"]):
        return False, "press_enter failed"

    # 3. 等 build_patch_done
    ev = wait_log_event(
        lambda e: e.get("type") == "build_patch_done" and e.get("to") == msg,
        timeout=25, since_pos=base)
    if not ev:
        return False, "timeout: build_patch_done not seen"
    detail = {"patch": ev}

    # 4. 等 POST_A CASE0（发送链走完）
    ev2 = wait_log_event(
        lambda e: e.get("type") == "qprobe" and e.get("id") in ("POSTA_C0", "POST_A")
                  and e.get("phase") == "enter",
        timeout=25, since_pos=base)
    detail["post_a"] = bool(ev2)
    return True, detail


def focus_chat(pos):
    return ui(["click_chat", str(pos[0]), str(pos[1])])


def daemon_loop():
    if not os.path.exists(FIFO_PATH):
        os.mkfifo(FIFO_PATH)
    print("path_a_daemon listening on %s" % FIFO_PATH, flush=True)
    while True:
        try:
            with open(FIFO_PATH, "r") as fifo:
                for line in fifo:
                    msg = line.strip()
                    if not msg:
                        continue
                    print("SEND request: %r" % msg, flush=True)
                    ok, detail = path_a_send(msg)
                    print("SEND result: ok=%s %s" % (ok, detail), flush=True)
        except Exception as e:
            print("daemon error: %s (retry in 2s)" % e, flush=True)
            time.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "daemon":
        daemon_loop()
    elif sys.argv[1] == "send" and len(sys.argv) > 2:
        # 单发测试：聚焦测试群 → 发 → 恢复主群焦点
        msg = sys.argv[2]
        print("focusing testroom...", flush=True)
        focus_chat(TESTROOM_POS)
        time.sleep(1.0)
        ok, detail = path_a_send(msg)
        print("RESULT ok=%s" % ok, flush=True)
        print(json.dumps(detail, ensure_ascii=False, default=str)[:2000], flush=True)
        print("restoring focus to main room...", flush=True)
        focus_chat(MAINROOM_POS)
        sys.exit(0 if ok else 1)
    elif sys.argv[1] == "send2" and len(sys.argv) > 2:
        # v2 单发测试：占位符快打 + BUILD 注入（中文/长文本），测试群
        import sendd_v2 as S
        msg = sys.argv[2]
        print("navigating testroom...", flush=True)
        ok, reason = S.navigate("testroom@chatroom")
        print("navigate", ok, reason, flush=True)
        if not ok:
            sys.exit(1)
        t0 = time.time()
        vok, entered, detail = S.send_part_v2(msg)
        dt = time.time() - t0
        print("V2 RESULT ok=%s entered=%s detail=%s (%.1fs)" % (vok, entered, detail, dt), flush=True)
        sys.exit(0 if vok else 1)
    else:
        print(__doc__)
        sys.exit(1)
