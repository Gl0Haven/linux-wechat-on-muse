#!/usr/bin/env python3
"""
WAL 长时间 CRIT 时的 OCR 核实：截图微信客户端，用 OCR 识别聊天内容，
判断是真掉线（客户端有新消息但 WAL 没更新）还是群里没人说话（误报）。

触发条件：WAL CRIT 持续超过 1 小时（wal_age > 3600）。
输出：state/ocr_verify_result.json
  - verdict: "REAL_FAILURE" / "IDLE_FALSE_ALARM" / "UNCERTAIN"
  - ocr_text: OCR 识别到的文本片段
  - db_latest: 数据库里最新消息时间
"""
import os
import sys
import json
import time
import sqlite3
import subprocess

BRIDGE = os.path.expanduser("~/workspace/wechat-bridge")
STATE = os.path.join(BRIDGE, "state")
OCR_RESULT = os.path.join(STATE, "ocr_verify_result.json")

def run(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, r.stdout.strip()

def main():
    os.makedirs(STATE, exist_ok=True)
    now = int(time.time())

    # 1. 读 WAL age
    health_path = os.path.join(STATE, "health.json")
    wal_age = 0
    try:
        with open(health_path) as f:
            h = json.load(f)
            wal_age = (h.get("signals") or {}).get("wal_age_s", 0) \
                or h.get("wal_age_s", 0) or h.get("wal_age", 0)
    except Exception as e:
        print(f"health.json 读取失败: {e}")
        sys.exit(1)

    if wal_age < 3600:
        print(f"WAL age {wal_age}s < 1h，无需 OCR 核实")
        sys.exit(0)

    print(f"WAL 已红 {wal_age}s，启动 OCR 核实...")

    # 2. 截图微信客户端
    shot_path = "/tmp/wal_ocr_check.png"
    os.environ["DISPLAY"] = ":99"
    ok, _ = run(["scrot", shot_path], timeout=15)
    if not ok or not os.path.exists(shot_path):
        print("截图失败")
        _write_result("UNCERTAIN", "截图失败", now)
        sys.exit(2)

    # 3. OCR（中英混合）
    ocr_txt_path = "/tmp/wal_ocr_check"
    ok, _ = run(["tesseract", shot_path, ocr_txt_path, "-l", "chi_sim+eng"],
                timeout=60)
    ocr_text = ""
    try:
        with open(ocr_txt_path + ".txt", encoding="utf-8", errors="ignore") as f:
            ocr_text = f.read()
    except Exception:
        pass

    # 4. 查 DB 最新消息时间
    db_latest = 0
    db_latest_str = ""
    try:
        db = sqlite3.connect(os.path.join(BRIDGE, "queue.db"), timeout=10)
        r = db.execute("SELECT MAX(msg_time) FROM messages").fetchone()
        if r and r[0]:
            db_latest = int(r[0])
            db_latest_str = time.strftime("%H:%M:%S", time.localtime(db_latest))
        db.close()
    except Exception as e:
        print(f"DB 查询失败: {e}")

    # 5. 判定：OCR 文本里找时间戳（如 21:59、22:05），看有没有比 DB 更新的消息
    import re
    # 找 HH:MM 格式的时间
    times = re.findall(r"(\d{1,2}):(\d{2})", ocr_text)
    now_hm = time.localtime(now)
    ocr_has_newer = False
    for h, m in times:
        try:
            th, tm = int(h), int(m)
            # 只看今天的时间（合理范围）
            if 0 <= th <= 23 and 0 <= tm <= 59:
                # 构造今天该时间的时间戳
                t = time.mktime((now_hm.tm_year, now_hm.tm_mon, now_hm.tm_mday,
                                 th, tm, 0, 0, 0, -1))
                # 如果 OCR 里的时间比 DB 最新消息新超过 5 分钟，可能是漏消息
                if t > db_latest + 300 and t <= now:
                    ocr_has_newer = True
                    break
        except Exception:
            continue

    if ocr_has_newer:
        verdict = "REAL_FAILURE"
        print("判定：真掉线！OCR 发现客户端有 DB 里没有的新消息")
    else:
        # 再看 OCR 有没有识别到聊天内容
        if len(ocr_text.strip()) < 50:
            verdict = "UNCERTAIN"
            print("判定：不确定，OCR 文本太少")
        else:
            verdict = "IDLE_FALSE_ALARM"
            print("判定：误报，群里没人说话，WAL 红是正常的")

    _write_result(verdict, ocr_text[:500], now, db_latest_str, wal_age)
    print(f"结果已写 {OCR_RESULT}: {verdict}")

def _write_result(verdict, ocr_text, now, db_latest="", wal_age=0):
    with open(OCR_RESULT, "w", encoding="utf-8") as f:
        json.dump({
            "verdict": verdict,
            "ocr_text_snippet": ocr_text,
            "db_latest": db_latest,
            "wal_age_s": wal_age,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(now)),
        }, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
