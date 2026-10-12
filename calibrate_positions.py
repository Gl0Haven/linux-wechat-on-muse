#!/usr/bin/env python3
"""校准聊天列表 Y 坐标：对候选 Y 逐个点击，用 NCC 判断是哪个聊天。"""
import sys, os, time, json, subprocess

BRIDGE = os.path.dirname(os.path.abspath(__file__))
SEND_UI = os.path.join(BRIDGE, "send-ui.py")
TEMPLATES_DIR = os.path.join(BRIDGE, "templates")
NCC_THRESHOLD = 0.90

def ui(*args, timeout=60):
    try:
        r = subprocess.run(["python3", SEND_UI] + list(args),
            capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return 98, str(e)

def ncc(a_path, b_path):
    try:
        from PIL import Image
        import numpy as np
        a = np.asarray(Image.open(a_path).convert("L"), dtype=np.float64)
        b = np.asarray(Image.open(b_path).convert("L"), dtype=np.float64)
        if a.shape != b.shape:
            return 0.0
        a = a - a.mean(); b = b - b.mean()
        denom = (a**2).sum() * (b**2).sum()
        if denom == 0: return 0.0
        return float((a*b).sum() / (denom ** 0.5))
    except Exception:
        return 0.0

def verify_chat(chat_id):
    template = os.path.join(TEMPLATES_DIR, f"{chat_id}.png")
    if not os.path.exists(template):
        return False, 0.0
    tmp = "/tmp/calibrate_title.png"
    rc, _ = ui("shot_title", tmp, timeout=30)
    if rc != 0:
        return False, 0.0
    score = ncc(tmp, template)
    return score > NCC_THRESHOLD, score

CANDIDATE_Y = [120, 140, 160, 180, 200, 220, 240, 260, 280, 300, 320]
CHATS = {
    "wxid_YOUR_MAIN_ACCOUNT": "大号",
    "YOUR_MAIN_GROUP@chatroom": "[主群]",
    "YOUR_TEST_GROUP@chatroom": "[测试群]",
}

print("开始校准...")
results = {}
for y in CANDIDATE_Y:
    print(f"点击 Y={y}...", flush=True)
    rc, _ = ui("focus")
    if rc != 0:
        print("  focus failed"); continue
    rc, out = ui("click_chat", "150", str(y), timeout=30)
    if rc != 0:
        print(f"  click failed"); continue
    time.sleep(1.5)
    best_chat, best_score = None, 0
    for cid in CHATS:
        passed, score = verify_chat(cid)
        if score > best_score:
            best_score, best_chat = score, cid
    if best_score > 0.9:
        print(f"  → {CHATS[best_chat]} (ncc={best_score:.4f})")
        results[y] = best_chat
    else:
        print(f"  → 未知 (最高 {best_score:.4f})")

print("\n=== 结果 ===")
for y in sorted(results):
    print(f"Y={y}: {results[y]} ({CHATS[results[y]]})")

if results:
    pos_file = os.path.join(BRIDGE, "state", "chat_positions.json")
    data = {"positions": {}, "current_focus": "", "updated_at": int(time.time())}
    try:
        with open(pos_file) as f:
            old = json.load(f)
            data["current_focus"] = old.get("current_focus", "")
    except: pass
    for y, cid in results.items():
        data["positions"][cid] = {"x": 150, "y": y, "note": CHATS[cid]}
    with open(pos_file, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n已更新 {pos_file}")
