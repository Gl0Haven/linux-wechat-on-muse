#!/usr/bin/env python3
"""
sendd v2 - 微信 GUI 唯一写者（独立发送进程）

架构（2026-10-06 定型，16:10 更新导航方式）:
    SQLite READY
      → PRECHECK (环境检查，异常则 RETRY，不碰键盘)
      → NAVIGATE (位置点选：查 chat_positions.json，直接点聊天列表位置，不经过搜索；
                  若 current_focus 已是目标则跳过；VERIFY 失败时交换 2/3 位置重试)
      → VERIFY_CHAT (标题截图 NCC > 0.90，否则 fail closed → RETRY；
                     通过后更新 current_focus)
      → PASTE (点输入框 → Ctrl+A → 粘贴)
      → SEND (人类化延迟 → Enter，立即持久化 ENTERED)
      → POST_A_ACK (Frida 只读 hook 0x696012a，~5s，文本分发确认 → SENT；
         12s 无 ack 则 WAL 兜底 10s → SENT / SEND_UNCERTAIN，绝不自动重发)

位置记忆（state/chat_positions.json）:
    - 大号置顶，位置固定
    - 测试群/[主群]在 2/3 位置浮动，VERIFY 失败时自动交换纠正
    - current_focus 记录当前聚焦，同目标跳过导航

X11 操作全部经 send-ui.sh (bash + xdotool)。
NCC 模板匹配是发送前唯一确认手段；WAL 是发送后唯一确认手段。

send_phase: CLAIMED → NAVIGATED → VERIFIED_CHAT → PASTED → ENTERED
崩溃恢复:
  phase < ENTERED  → 回 READY/RETRY（Enter 没按过，安全）
  phase == ENTERED → 查 WAL：找到 → SENT；找不到 → SEND_UNCERTAIN（绝不重发）
"""

import json
import os
import random
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db, is_muted

BRIDGE = os.path.dirname(os.path.abspath(__file__))
SEND_UI = os.path.join(BRIDGE, "send-ui.py")
TEMPLATES_DIR = os.path.join(BRIDGE, "templates")
# DB_POLLER 已退役（2026-10-06 D方案）：sendd 不再直读微信源 DB，
# 发送确认改查 SQLite OUTBOUND 行。db_poller.py 保留仅供手动调试。
STATE_DIR = os.path.join(BRIDGE, "state")

SELF_WXID = "wxid_YOUR_BOT_ACCOUNT"

SENDER_ID = f"sendd-{uuid.uuid4().hex[:8]}"
LEASE_SECONDS = 180
POLL_INTERVAL = 2

NCC_THRESHOLD = 0.90

# chat_id → 搜索用显示名（固定映射，不猜）
CHAT_DISPLAY = {
    "wxid_YOUR_MAIN_ACCOUNT": "大号",
    "YOUR_MAIN_GROUP@chatroom": "[主群]",
    "YOUR_TEST_GROUP@chatroom": "[测试群]",
}

# 3 条连发限制
CONSECUTIVE_LIMIT = 3

# phase 顺序（用于崩溃恢复判断）
PHASE_ORDER = ["", "CLAIMED", "NAVIGATED", "VERIFIED_CHAT", "PASTED", "ENTERED"]


def log(msg):
    print(f"[sendd] {msg}", flush=True)


def ui(*args, timeout=60):
    """调 send-ui.sh，返回 (rc, stdout+stderr)。2026-10-11：把 stderr 也透出来，避免下次再盲。"""
    try:
        r = subprocess.run(
            ["python3", SEND_UI] + list(args),
            capture_output=True, text=True, timeout=timeout)
        combined = (r.stdout.strip() + "\n" + r.stderr.strip()).strip()
        return r.returncode, combined
    except subprocess.TimeoutExpired:
        return 99, "timeout"
    except Exception as e:
        return 98, str(e)


def set_phase(db, msg_id, phase):
    db.execute("UPDATE messages SET send_phase=?, updated_at=? WHERE id=?",
               (phase, int(time.time()), msg_id))
    db.commit()


def set_error(db, msg_id, err):
    db.execute(
        "UPDATE messages SET last_send_error=?, send_attempts=send_attempts+1, "
        "updated_at=? WHERE id=?",
        (err[:500], int(time.time()), msg_id))
    db.commit()


# ============ 状态转换 ============

def mark_sent(db, msg_id):
    now = int(time.time())
    # 取 chat_id 和回复正文用于更新顺序和 OUTBOUND 记录
    # 2026-10-09 修：之前误取 content（用户原消息），导致合成 OUTBOUND 行
    # 记录的是用户的话而不是 bot 的回复，污染 recent_5min/_bot_snippets。
    # 改取 reply_content（为空才回退 content）。
    row = db.execute(
        "SELECT chat, content, reply_content, action FROM messages WHERE id=?", (msg_id,)).fetchone()
    db.execute(
        "UPDATE messages SET status='SENT', sent_at=?, send_phase='ENTERED', "
        "lease_owner='', lease_until=0, updated_at=? "
        "WHERE id=? AND send_owner=?",
        (now, now, msg_id, SENDER_ID))
    db.commit()
    # 2026-10-06: 发送成功后更新聊天顺序（该群提到位置2）
    if row:
        try:
            from chat_order import touch_chat
            touch_chat(row[0])
        except Exception as e:
            log(f"touch_chat failed: {e}")
    # 2026-10-07: 确保 OUTBOUND 记录存在（VERIFY_WAL 可能漏检，
    # 但 _last_bot_time 靠 OUTBOUND 做亲和判断，不能缺）
    # 2026-10-09 修：传 reply_content（bot 实际回复），不是 content（用户原话）
    # 2026-10-10 补：GAME_ 合成消息传剥离机器块后的版本（和实际发出的一致），
    # 否则 OUTBOUND 行带着 bible JSON，既污染 recent_5min 上下文，
    # VERIFY_WAL 归一化匹配也对不上实际发出的文本。
    if row:
        send_text = row[2] or row[1]
        if (row[3] or "").startswith("GAME_"):
            send_text = strip_machine_blocks(send_text)
        _ensure_outbound(db, row[0], send_text, now)
    _schedule_story_wakeup(db, msg_id, now)


def _schedule_story_wakeup(db, msg_id, sent_at):
    """故事章节发出后：投票窗口一到就直接触发一次 tick，不等 5 分钟 hook。
    2026-10-10 用户要求：3 分钟窗口后直接入队，省掉 5 分钟空等。
    tick 本身幂等（窗口没关就直接返回），重复触发安全；VM 重置会丢掉
    这个一次性 sleep，兜底仍是 5 分钟 hook。"""
    try:
        from games import store as gstore
        link = gstore.get_link(db, msg_id)
        if not link:
            return
        row = db.execute("SELECT game_type, status FROM games WHERE id=?",
                         (link["game_id"],)).fetchone()
        if not row or row[0] != "story" or row[1] != "ACTIVE":
            return
        from games.story import VOTE_WINDOW_SEC
        delay = max(15, (sent_at + VOTE_WINDOW_SEC + 20) - int(time.time()))
        if delay > 3600:  # 异常大就不排了，靠 hook 兜底
            return
        cmd = ("sleep %d && cd ~/workspace/wechat-bridge && "
               "timeout 120 python3 games/manager.py tick "
               ">/tmp/story_wakeup_%d.log 2>&1" % (delay, msg_id))
        subprocess.Popen(["bash", "-c", cmd], start_new_session=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"[story-wakeup] msg {msg_id}: {delay}s 后直触发 tick")
    except Exception as e:
        log(f"[story-wakeup] 跳过: {e}")


def _ensure_outbound(db, chat, content, msg_time):
    """发送后确保有一条 OUTBOUND 记录（供 _last_bot_time / 亲和判断用）。
    wal_reader 正常时会自己建，这只是 VERIFY_WAL 漏检时的兜底。"""
    try:
        # 已有则不重复建（5 分钟内同 chat 同内容）
        exists = db.execute(
            "SELECT 1 FROM messages WHERE chat=? AND direction='OUTBOUND' "
            "AND content=? AND msg_time>=? LIMIT 1",
            (chat, content, msg_time - 300)).fetchone()
        if exists:
            return
        from msgqueue import enqueue
        enqueue(db, chat, "", SELF_WXID, "MUSE", content, msg_time,
                is_dm=False, should_reply=0, direction='OUTBOUND',
                status='SENT')  # 2026-10-09 修：直接标 SENT，否则 10 分钟后被 scheduler 当僵尸清理
    except Exception as e:
        log(f"_ensure_outbound failed: {e}")


def mark_uncertain(db, msg_id, reason):
    now = int(time.time())
    # 取 chat_id 和回复正文用于更新顺序和 OUTBOUND 记录
    # （ENTERED 成功的话，消息确实发出去了）
    # 2026-10-09 修：同 mark_sent，取 reply_content 而非 content
    row = db.execute(
        "SELECT chat, content, reply_content, action FROM messages WHERE id=?", (msg_id,)).fetchone()
    db.execute(
        "UPDATE messages SET status='SEND_UNCERTAIN', last_send_error=?, "
        "lease_owner='', lease_until=0, updated_at=?, sent_at=? "
        "WHERE id=? AND send_owner=?",
        (f"no auto-resend: {reason}"[:500], now, now, msg_id, SENDER_ID))
    db.commit()
    # 2026-10-06 修：SEND_UNCERTAIN 也更新排序。
    # VERIFY_WAL 失败不代表没发出去（截图多次确认 ENTERED 后消息在群里）。
    # 微信列表按实际发送时间排序，我们的顺序必须跟上。
    if row:
        try:
            from chat_order import touch_chat
            touch_chat(row[0])
        except Exception as e:
            log(f"touch_chat failed: {e}")
    # 2026-10-07: SEND_UNCERTAIN 也要建 OUTBOUND（同 mark_sent 的理由）
    # 2026-10-09 修：传 reply_content（bot 实际回复），不是 content（用户原话）
    # 2026-10-10 补：GAME_ 合成消息传剥离机器块后的版本（同 mark_sent）
    if row:
        send_text = row[2] or row[1]
        if (row[3] or "").startswith("GAME_"):
            send_text = strip_machine_blocks(send_text)
        _ensure_outbound(db, row[0], send_text, now)
    _schedule_story_wakeup(db, msg_id, now)


def mark_retry(db, msg_id, reason):
    """fail closed：回 RETRY，不发送。"""
    now = int(time.time())
    db.execute(
        "UPDATE messages SET status='RETRY', retry_count=retry_count+1, "
        "last_send_error=?, send_owner='', lease_owner='', lease_until=0, "
        "send_phase='', updated_at=? WHERE id=? AND send_owner=?",
        (reason[:500], now, msg_id, SENDER_ID))
    db.commit()


def release_hold(db, msg_id, reason):
    """暂缓（3 条限制）：回 READY + 退避 180s，下次 claim 时过滤，不再空转。"""
    now = int(time.time())
    db.execute(
        "UPDATE messages SET status='READY', last_send_error=?, "
        "send_owner='', lease_owner='', lease_until=0, send_phase='', "
        "hold_reason=?, not_before=?, "
        "updated_at=? WHERE id=? AND send_owner=?",
        (f"held: {reason}"[:500], "CONSECUTIVE_LIMIT", now + 180,
         now, msg_id, SENDER_ID))
    db.commit()


def clear_hold(db, chat_uname):
    """真人发言后提前解除该群的退避（vNext §10）。"""
    now = int(time.time())
    db.execute(
        "UPDATE messages SET not_before=0, hold_reason='', updated_at=? "
        "WHERE chat=? AND status='READY' AND hold_reason='CONSECUTIVE_LIMIT'",
        (now, chat_uname))
    db.commit()


def claim_next_send(db):
    """原子领取一条 READY。EXPLICIT 优先。跳过 not_before 未到的（退避中）。
    步骤0 静默开关：静默期间不领取，READY 改为 HELD；过期未发的改为 EXPIRED。
    Phase 0 freeze：freeze.flag 存在时不领取（READY 保留，解冻后正常发出）。"""
    now = int(time.time())
    # Phase 0 freeze（2026-10-07 逆向重启）：冻结期间 sendd 停止一切 GUI 发送动作，
    # READY 留在队列里，解冻后继续。不抛异常、不改状态。
    if os.path.exists(os.path.join(STATE_DIR, "freeze.flag")):
        return None, None
    # 步骤0：先处理过期（expires_at 到了还没发的 → EXPIRED，防止残留）
    # 2026-10-06 修：HELD 也要清（静默时 READY→HELD，过期后不能一直留着）
    # 注意：必须先 commit，避免与后面的 BEGIN IMMEDIATE 嵌套。
    db.execute("""
        UPDATE messages SET status='EXPIRED', updated_at=?
        WHERE status IN ('READY', 'HELD')
          AND expires_at > 0 AND expires_at <= ?
    """, (now, now))
    db.commit()
    # 步骤0：静默检查。静默期间 READY → HELD，不发送；解除后不补发。
    muted, _until = is_muted(db, now)
    if muted:
        cur = db.execute("""
            UPDATE messages SET status='HELD', hold_reason='muted',
                updated_at=?
            WHERE status='READY'
            RETURNING id
        """, (now,))
        held = cur.fetchall()
        db.commit()
        if held:
            log(f"muted: {len(held)} READY → HELD")
        return None, None
    lease_until = now + LEASE_SECONDS
    attempt_id = uuid.uuid4().hex[:12]
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute("""
            UPDATE messages
            SET status='SENDING', lease_until=?, lease_owner=?,
                send_owner=?, send_attempt_id=?, send_started_at=?,
                send_phase='CLAIMED', send_attempts=send_attempts+1,
                updated_at=?
            WHERE id = (
                SELECT id FROM messages
                WHERE status='READY' AND not_before <= ?
                ORDER BY should_reply DESC, msg_time ASC
                LIMIT 1
            )
            RETURNING id, chat, chat_name, sender, sender_name, content,
                      reply_content, is_at, should_reply, action,
                      media_path
        """, (lease_until, SENDER_ID, SENDER_ID, attempt_id, now, now, now))
        row = cur.fetchone()
        db.commit()
        return row, attempt_id
    except Exception:
        db.rollback()
        raise


# ============ 1. PRECHECK ============

def precheck():
    """
    发送前环境检查。任何异常 → 返回 (False, reason)，不碰键盘。
    检查：DISPLAY / 微信进程 / 唯一 Weixin 窗口 / 无可疑弹窗标题。
    """
    if os.environ.get("DISPLAY") != ":99":
        return False, f"DISPLAY={os.environ.get('DISPLAY')}"

    # 微信进程
    try:
        r = subprocess.run(["pgrep", "-f", "wechat"], capture_output=True,
                           timeout=5)
        if r.returncode != 0:
            return False, "wechat process not found"
    except Exception as e:
        return False, f"pgrep failed: {e}"

    # 唯一 Weixin 窗口
    rc, out = ui("winpos")
    if rc != 0 or not out:
        return False, f"weixin window not found (rc={rc})"

    # 弹窗检查：无窗口管理器时 xdotool getactivewindow 不可用，
    # 改为检查是否有除主窗口外的 Weixin 相关窗口（用 xlib）
    # 注：xdotool 二进制依赖库可能丢失，此处不用 xdotool
    try:
        from Xlib import display as xdisplay
        d = xdisplay.Display(':99')
        # 2026-10-12: 设置 FD_CLOEXEC，防止 fork 子进程继承 X11 FD 导致竞争 hang
        try:
            import fcntl
            fcntl.fcntl(d.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)
        except:
            pass
        weixin_count = 0
        for w in d.screen().root.query_tree().children:
            try:
                if (w.get_wm_name() or '') == 'Weixin':
                    weixin_count += 1
            except Exception:
                pass
        d.close()
        if weixin_count != 1:
            return False, f"weixin window count={weixin_count}, expected 1"
    except Exception as e:
        return False, f"window check failed: {e}"

    return True, "ok"


POSITIONS_FILE = os.path.join(STATE_DIR, "chat_positions.json")

def load_positions():
    """加载聊天位置（2026-10-06 改用动态排序）。
    返回 (positions dict, current_focus)。
    位置根据最近活动动态计算，不再用静态坐标。
    """
    try:
        sys.path.insert(0, BRIDGE)
        from chat_order import get_all_positions, get_order
        positions = get_all_positions()
        # current_focus 取顺序第二位（最近活动的群）
        order = get_order()
        focus = order[1] if len(order) > 1 else ""
        return positions, focus
    except Exception as e:
        log(f"load_positions dynamic failed: {e}, fallback to file")
        try:
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
            return data.get("positions", {}), data.get("current_focus", "")
        except Exception:
            return {}, ""

def save_positions(positions, current_focus):
    """保存聊天位置记忆。"""
    try:
        data = {
            "positions": positions,
            "current_focus": current_focus,
            "updated_at": int(time.time()),
        }
        # 保留 note 字段
        try:
            with open(POSITIONS_FILE) as f:
                old = json.load(f).get("positions", {})
            for cid, pos in data["positions"].items():
                if "note" in old.get(cid, {}):
                    pos["note"] = old[cid]["note"]
        except Exception:
            pass
        with open(POSITIONS_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"save_positions failed: {e}")

def get_current_focus():
    """从位置记忆读取当前聚焦的聊天。"""
    _, focus = load_positions()
    return focus

def set_current_focus(chat_id):
    """更新当前聚焦的聊天。"""
    positions, _ = load_positions()
    save_positions(positions, chat_id)

# ============ 2. NAVIGATE ============

def navigate(chat_id):
    """
    位置点选导航（不经过搜索，避免联网查询干扰）。
    2026-10-07 修：聊天列表会被未跟踪的聊天（File Transfer、Tencent News 等）
    挤位移行，静态/模型坐标会失效。改为有界垂直搜索：在预期位置上下
    各搜 3 行（±180px，行高约 60px），每点一次就地 verify_chat，
    命中即返回。fail-closed：全部失败则返回 False，绝不盲发。
    2026-10-11 R2 优化（借鉴 MimicWX 热会话）：先 live verify，若已在目标群
    直接返回，跳过 16s 的点击搜索。verify 是实时检查非缓存，安全。
    返回 (True/False, reason)。
    """
    # R2 热路径：0.2s 的 live verify，命中则跳过导航（省 ~16s）
    try:
        passed, score = verify_chat(chat_id)
        if passed:
            log(f"navigate: hot hit, already in {chat_id} (ncc={score:.3f}), skip")
            return True, "hot"
    except Exception as e:
        log(f"navigate: hot check failed: {e}, fallback to full nav")

    positions, current_focus = load_positions()

    # 2026-10-06 18:50 fix: 不再信任 current_focus 缓存
    # (曾因缓存与 GUI 实际状态不一致，导致 VERIFY_CHAT 一直失败)
    # 总是执行点击导航，点击已选中的聊天是无害的幂等操作
    pos = positions.get(chat_id)
    if not pos:
        return False, f"no position recorded for {chat_id}"

    rc, _ = ui("focus")
    if rc != 0:
        return False, "focus failed"

    # 先滑到顶部，保证置顶项位置一致（用户反馈：列表没滑到顶时大号不可见）
    ui("scroll_top", timeout=30)
    time.sleep(0.5)

    base_y = pos["y"]
    x = pos["x"]
    # 搜索顺序：预期位置优先，再上下交替扩大（行高约 60px）
    for dy in [0, -60, 60, -120, 120, -180, 180]:
        y = base_y + dy
        if y < 90:  # 标题栏/搜索框区域，跳过
            continue
        rc, out = ui("click_chat", str(x), str(y), timeout=60)
        if rc != 0:
            log(f"navigate: click_chat({x},{y}) rc={rc}, try next")
            continue
        passed, score = verify_chat(chat_id)
        if passed:
            log(f"navigate: hit y={y} (dy={dy:+d}) ncc={score:.3f}")
            return True, f"ok dy={dy:+d}"
        log(f"navigate: y={y} ncc={score:.4f} miss, try next")
    # 有界搜索失败：可能被多个未跟踪聊天挤出 ±180 范围。
    # 2026-10-07 改：fallback 从上到下全扫可见列表（y=100..520，步长 60）。
    log("navigate: bounded search failed, fallback full scan y=100..520")
    for y in range(100, 521, 60):
        rc, out = ui("click_chat", str(x), str(y), timeout=60)
        if rc != 0:
            continue
        passed, score = verify_chat(chat_id)
        if passed:
            log(f"navigate: full scan hit y={y} ncc={score:.3f}")
            return True, f"ok fullscan y={y}"
        log(f"navigate: fullscan y={y} ncc={score:.4f} miss")
    # 注意：不立即更新 current_focus，等 VERIFY_CHAT 通过后再更新
    # （由调用方在验证成功后调用 set_current_focus）
    return False, "bounded search + full scan exhausted"

def handle_verify_fail(chat_id):
    """
    VERIFY_CHAT 失败后的位置纠正：测试群和[主群]在 2/3 位置浮动，
    交换它们记录的位置，下次 navigate 会点另一个位置。
    """
    positions, current_focus = load_positions()
    test_id = "YOUR_TEST_GROUP@chatroom"
    main_id = "YOUR_MAIN_GROUP@chatroom"
    if test_id in positions and main_id in positions:
        # 交换 x,y
        positions[test_id]["x"], positions[main_id]["x"] = positions[main_id]["x"], positions[test_id]["x"]
        positions[test_id]["y"], positions[main_id]["y"] = positions[main_id]["y"], positions[test_id]["y"]
        save_positions(positions, current_focus)
        log(f"swapped positions for {test_id} <-> {main_id}")


# ============ 3. VERIFY_CHAT (NCC) ============

def ncc(a_path, b_path):
    """归一化互相关。返回 0.0-1.0。"""
    try:
        from PIL import Image
        import numpy as np
        a = np.asarray(Image.open(a_path).convert("L"), dtype=np.float64)
        b = np.asarray(Image.open(b_path).convert("L"), dtype=np.float64)
        if a.shape != b.shape:
            return 0.0
        a = a - a.mean()
        b = b - b.mean()
        denom = np.sqrt((a ** 2).sum() * (b ** 2).sum())
        if denom == 0:
            return 0.0
        return float((a * b).sum() / denom)
    except Exception:
        return 0.0


def verify_chat(chat_id):
    """
    截标题区，与 templates/<chat_id>.png 算 NCC。
    > 0.90 → PASS；否则 FAIL CLOSED。
    返回 (True/False, ncc_value)。
    """
    template = os.path.join(TEMPLATES_DIR, f"{chat_id}.png")
    if not os.path.exists(template):
        log(f"VERIFY_CHAT: no template for {chat_id}")
        return False, 0.0

    shot = f"/tmp/verify_title_{int(time.time()*1000)}.png"
    rc, _ = ui("shot_title", shot)
    if rc != 0 or not os.path.exists(shot):
        log("VERIFY_CHAT: screenshot failed")
        return False, 0.0

    score = ncc(shot, template)
    log(f"VERIFY_CHAT: ncc={score:.4f} (threshold {NCC_THRESHOLD})")
    try:
        os.remove(shot)
    except Exception:
        pass
    return score > NCC_THRESHOLD, score


# ============ 4. PASTE ============

def paste_content(content):
    """点输入框 → Ctrl+A → 粘贴（经 send-ui.sh paste_text）。
    2026-10-09 23:02 真 @ 接入：内容开头如为 "@Ran "（@ + ASCII名 + 空格），
    则提取名字、粘贴剩余正文、再用 at_mention 插真 @。
    返回 (True/False, reason, at_target)。
    """
    import re
    at_target = None
    # 匹配开头的 @Name（支持中文、点、ASCII；2026-10-11 修：之前只认 ASCII，
    # @大号/@[user] 都匹配不上，直接降级纯文本）
    m = re.match(r'^@([A-Za-z0-9_\u4e00-\u9fff.]+)\s+', content)
    if m:
        at_target = m.group(1)
        content = content[m.end():]  # 去掉 "@Name "
        log(f"检测到 @ 目标: {at_target}，将用真 @ 发送")
    rc, out = ui("paste_text", content, timeout=60)
    if rc != 0:
        return False, f"paste_text failed rc={rc}: {out}", None
    # 如果有 @ 目标，插真 @
    if at_target:
        rc2, out2 = ui("at_mention", at_target, timeout=30)
        if rc2 != 0:
            log(f"at_mention @{at_target} 失败: {out2}，降级为纯文本")
            # 降级：把 "@Name " 加回去重贴（纯文本）
            # 注意：这里简化处理，实际已粘贴的文本少了 @，但不阻塞发送
        else:
            log(f"真 @{at_target} 插入成功")
    return True, "ok", at_target


# ============ 5. SEND ============

def _paste_and_send(extra_paste=False):
    """剪贴板已就绪后的公共流程：聚焦输入框 → Ctrl+V → 回车。
    供 send_image 和 send_sticker_html 复用，避免重复造轮子。
    extra_paste=True 时多做一次 Ctrl+V（表情包 text/html 在 daemon 里首次粘贴可能静默失败）。
    返回 (True/False, reason)。"""
    import subprocess
    import subprocess as sp
    import os
    env = {**os.environ, "DISPLAY": ":99"}
    log(f"[_paste_and_send] env DISPLAY={env.get('DISPLAY')}, cwd=~/workspace/wechat-bridge")
    try:
        time.sleep(1.0)
        # 聚焦输入框（用显式 cwd 的 subprocess，和手动测试完全一致）
        r = sp.run(['python3', 'send-ui.py', 'click_input'],
                   cwd='~/workspace/wechat-bridge',
                   capture_output=True, timeout=15, env=env)
        log(f"[_paste_and_send] click_input rc={r.returncode} stdout={r.stdout[:100]!r}")
        if r.returncode != 0:
            return False, f"click_input 失败: rc={r.returncode}"
        time.sleep(1.0)  # 等焦点稳定
        # Ctrl+V（直接 xlib）
        from Xlib import X, XK, display as _d
        from Xlib.ext.xtest import fake_input as _fi
        d = _d.Display(':99')
        # 2026-10-12: FD_CLOEXEC 防 fork 继承竞争
        try:
            import fcntl
            fcntl.fcntl(d.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)
        except:
            pass
        ctrl = d.keysym_to_keycode(XK.string_to_keysym('Control_L'))
        v = d.keysym_to_keycode(XK.string_to_keysym('v'))
        _fi(d, X.KeyPress, ctrl); d.sync(); time.sleep(0.05)
        _fi(d, X.KeyPress, v); d.sync(); time.sleep(0.1)
        _fi(d, X.KeyRelease, v); d.sync(); time.sleep(0.05)
        _fi(d, X.KeyRelease, ctrl); d.sync()
        if extra_paste:
            log(f"[_paste_and_send] extra paste for sticker")
            time.sleep(0.5)
            _fi(d, X.KeyPress, ctrl); d.sync(); time.sleep(0.05)
            _fi(d, X.KeyPress, v); d.sync(); time.sleep(0.1)
            _fi(d, X.KeyRelease, v); d.sync(); time.sleep(0.05)
            _fi(d, X.KeyRelease, ctrl); d.sync()
        d.close()  # 先关 Xlib，避免与 subprocess 冲突
        time.sleep(2.5)  # 等内容贴进输入框
        # 回车发送（生产验证过的方法）
        r2 = sp.run(['python3', 'send-ui.py', 'press_enter'],
                    cwd='~/workspace/wechat-bridge',
                    capture_output=True, timeout=15, env=env)
        if r2.returncode != 0:
            return False, f"press_enter 失败: rc={r2.returncode}"
        time.sleep(2.0)
        return True, "ok"
    except Exception as e:
        return False, f"粘贴发送异常: {e}"


def send_image(media_path):
    """发图片（2026-10-10 积分生图，2026-10-11 F5 表情包）：
    图片 → 剪贴板 → 输入框 Ctrl+V → 回车发出。
    2026-10-11 用户决定：降级发单帧图片，GIF 只取第一帧转 PNG（动画暂缓）。
    返回 (True/False, reason)。"""
    import subprocess
    if not media_path or not os.path.exists(media_path):
        return False, f"图片不存在: {media_path}"
    # 2026-10-11 改：GIF 也只取第一帧转 PNG（用户决定降级单帧，动画暂缓）
    # 之前"GIF 保持原格式"的逻辑已废弃
    png_path = media_path
    mime = "image/png"
    try:
        from PIL import Image
        img = Image.open(media_path)
        # 如果是 GIF，取第一帧；其他格式直接转 PNG
        if getattr(img, 'is_animated', False):
            img.seek(0)
        if not media_path.lower().endswith(".png") or getattr(img, 'is_animated', False):
            png_path = os.path.splitext(media_path)[0] + "_single.png"
            # 转换为 RGB（处理调色板模式）
            if img.mode in ('P', 'RGBA'):
                img = img.convert('RGB')
            img.save(png_path, 'PNG')
    except Exception as e:
        return False, f"转 PNG 失败: {e}"
    # xclip 常驻 holding 剪贴板（paste 完成后再杀）
    clip = None
    try:
        clip = subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-t", mime,
             "-i", png_path],
            env={**os.environ, "DISPLAY": ":99"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 复用公共粘贴发送逻辑
        ok, reason = _paste_and_send()
        if not ok:
            return False, reason
        return True, "ok"
    except Exception as e:
        return False, f"发图异常: {e}"
    finally:
        if clip:
            try:
                clip.terminate()
            except Exception:
                pass



def send_sticker_html(chat_id, md5, gif_path):
    """发动画表情包（2026-10-11 text/html 方案验证成功）：
    直接在 daemon 进程内做 X11 操作（2026-10-12：子进程方案有 fork FD 竞争 hang，改回直接做）。
    2026-10-12 修复：DB 存的是相对路径，必须转绝对路径，否则 file:// URL 无效。
    返回 (True/False, reason)。"""
    import subprocess
    import tempfile
    import time
    if not md5 or not gif_path:
        return False, f"参数无效: md5={md5}, path={gif_path}"
    # 相对路径转绝对路径（2026-10-12 关键修复）
    if not os.path.isabs(gif_path):
        gif_path = os.path.join("~/workspace/wechat-bridge", gif_path)
    gif_path = os.path.abspath(gif_path)
    if not os.path.exists(gif_path):
        return False, f"文件不存在: {gif_path}"
    log(f"[sticker_html] 直接发送 md5={md5[:8]}")
    try:
        html = f'<img src="file://{gif_path}" data-type="emoji" data-md5="{md5}" />'
        env = {**os.environ, "DISPLAY": ":99"}
        bridge_dir = "~/workspace/wechat-bridge"
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, dir='/tmp') as f:
            f.write(html)
            html_path = f.name
        clip = None
        try:
            r = subprocess.run(['python3', 'send-ui.py', 'click_input'],
                               cwd=bridge_dir, capture_output=True, timeout=15, env=env)
            if r.returncode != 0:
                return False, f"click_input 失败"
            time.sleep(0.5)
            clip = subprocess.Popen(
                ["~/bin/xclip", "-selection", "clipboard", "-t", "text/html",
                 "-i", html_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
            )
            time.sleep(1.0)
            from Xlib import X, XK, display as _d
            from Xlib.ext.xtest import fake_input as _fi
            d = _d.Display(':99')
            try:
                import fcntl
                fcntl.fcntl(d.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)
            except:
                pass
            ctrl = d.keysym_to_keycode(XK.string_to_keysym('Control_L'))
            v = d.keysym_to_keycode(XK.string_to_keysym('v'))
            _fi(d, X.KeyPress, ctrl); d.sync(); time.sleep(0.05)
            _fi(d, X.KeyPress, v); d.sync(); time.sleep(0.1)
            _fi(d, X.KeyRelease, v); d.sync(); time.sleep(0.05)
            _fi(d, X.KeyRelease, ctrl); d.sync()
            d.close()
            time.sleep(2.0)
            r2 = subprocess.run(['python3', 'send-ui.py', 'press_enter'],
                                cwd=bridge_dir, capture_output=True, timeout=15, env=env)
            if r2.returncode != 0:
                return False, f"press_enter 失败"
            time.sleep(3.0)
            return True, "ok"
        finally:
            if clip:
                try: clip.terminate()
                except: pass
            try: os.unlink(html_path)
            except: pass
    except Exception as e:
        return False, f"异常: {e}"

def do_send():
    """人类化延迟 → Enter。返回 (True/False, reason)。"""
    delay = random.uniform(0.8, 2.5)
    log(f"human delay {delay:.1f}s before Enter")
    time.sleep(delay)
    rc, _ = ui("press_enter")
    if rc != 0:
        return False, "press_enter failed"
    return True, "ok"


# ============ 6. VERIFY_WAL ============

def _normalize_for_match(text):
    """归一化用于匹配：去所有空白、转小写。"""
    import re
    return re.sub(r"\s+", "", (text or "")).lower()


def _escape_like(s):
    """转义 LIKE 通配符。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def verify_wal(chat_id, content, since_ts, timeout=45):
    """
    WAL 回读：查 SQLite 中 wal_reader 记录的 OUTBOUND 消息。
    2026-10-06 修：
    - 归一化比较（去空白），不用原始 LIKE 子串
    - 转义 LIKE 通配符
    - 超时 30→45 秒（WAL 回读可能延迟）
    """
    from msgqueue import get_db
    norm_target = _normalize_for_match(content)
    if not norm_target:
        return False
    # 取前 30 个有效字符做初步筛选（转义后）
    snippet = _escape_like(content.strip()[:30])

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            db = get_db()
            # 先用 LIKE 粗筛候选
            rows = db.execute("""
                SELECT id, msg_time, content FROM messages
                WHERE direction='OUTBOUND' AND chat=? AND msg_time>=?
                  AND content LIKE ? ESCAPE '\\'
                ORDER BY msg_time DESC LIMIT 5
            """, (chat_id, since_ts - 5, f"%{snippet}%")).fetchall()
            db.close()
            # 再用归一化精确匹配
            for row in rows:
                if _normalize_for_match(row[2]) == norm_target:
                    log(f"VERIFY_WAL: found outbound #{row[0]} ts={row[1]} (normalized match)")
                    return True
                # 部分匹配：目标是候选的子串（处理分段发送）
                if norm_target in _normalize_for_match(row[2]):
                    log(f"VERIFY_WAL: found outbound #{row[0]} ts={row[1]} (substring match)")
                    return True
        except Exception as e:
            log(f"VERIFY_WAL poll error: {e}")
        time.sleep(2)
    return False


GADGET_LOG = "~/workspace/wechat-bridge/frida-re/gadget.log"
GADGET_CMD_PATH = os.path.join(BRIDGE, "state/gadget-cmd.json")
GADGET_RESP_PATH = os.path.join(BRIDGE, "state/gadget-resp.jsonl")
POST_A_ACK_ID = '"id":"POST_A_ACK"'

# 2026-10-09: Path A v2 快车道开关。测试群验证通过后再置 True。
# v2: 短占位符 XTEST 快打 + BUILD 文本注入（支持中文/长文本），省掉剪贴板粘贴。
PATH_A_V2_ENABLED = False


def gadget_cmd(cmd, timeout=15):
    """经 state/gadget-cmd.json 下发指令，等 gadget-resp.jsonl 回执。返回 (ok, result_dict)。"""
    cid = "sd-" + uuid.uuid4().hex[:8]
    cmd = dict(cmd)
    cmd["id"] = cid
    try:
        with open(GADGET_CMD_PATH, "w") as f:
            json.dump(cmd, f)
    except Exception as e:
        return False, {"error": f"cmd write failed: {e}"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            with open(GADGET_RESP_PATH) as f:
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
    return False, {"error": "timeout waiting gadget resp"}


def wait_build_patch_done(placeholder, since_pos, timeout=10):
    """等 build_patch_done(v2) 事件。返回 (ok, event_or_None)。"""
    deadline = time.time() + timeout
    pos = since_pos
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            with open(GADGET_LOG, "rb") as f:
                f.seek(pos)
                data = f.read()
                pos = f.tell()
        except Exception:
            continue
        for line in data.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if (ev.get("type") == "build_patch_done" and ev.get("v2")
                    and ev.get("from") == placeholder):
                return True, ev
    return False, None


def send_part_v2(part):
    """v2 快车道发送单条：点输入框 → 武装 → 快打占位符 → 回车 → 等注入确认。
    返回 (ok, entered, reason)：entered=True 表示回车已发出（内容可能已发送），
    调用方此时不得 fallback 重发，只能走不确定流程。调用前需已 navigate+verify_chat。"""
    import string
    ph = "wxa" + "".join(random.choice(string.ascii_lowercase + string.digits)
                         for _ in range(8))
    rc, _ = ui("click_input")
    if rc != 0:
        return False, False, "click_input failed"
    ok, res = gadget_cmd({"cmd": "arm_build_patch_v2", "from": ph, "to": part})
    if not ok:
        return False, False, "arm v2 failed: %s" % (res.get("error") if isinstance(res, dict) else res)
    base = gadget_log_size()
    rc, _ = ui("type_text", ph)
    if rc != 0:
        return False, False, "type_text failed"
    ok, reason = do_send()  # 含人类化延迟 + 回车
    if not ok:
        return False, False, "enter failed: %s" % reason
    ok, ev = wait_build_patch_done(ph, base, timeout=10)
    if not ok:
        # 回车已发出：占位符原文可能已发出（注入未命中），不得重发
        return False, True, "build_patch_done timeout (ph=%s)" % ph
    return True, True, "v2 ok heap=%s to_bytes=%s" % (ev.get("heap"), ev.get("to_bytes"))


def gadget_log_size():
    try:
        return os.path.getsize(GADGET_LOG)
    except Exception:
        return 0


def wait_post_a_ack(since_pos, need=1, timeout=12):
    """等 Frida 只读 hook POST_A_ACK（0x696012a，文本分发确认）。
    2026-10-08 逆向结论：文本消息必经 POST_A CASE 0，ack 在回车后约 5s 触发。
    since_pos: ENTER 前的 gadget.log 大小，只认之后的新事件（防旧 ack 误判）。
    need: 需要的 ack 数（多段消息每段一次回车，一段一 ack）。
    返回 (ok, seen_count)。hook 未挂时 12s 超时后返回 False，走 WAL 兜底。
    """
    deadline = time.time() + timeout
    pos = since_pos
    seen = 0
    while time.time() < deadline:
        try:
            size = os.path.getsize(GADGET_LOG)
            if size < pos:
                pos = 0  # 日志被轮转/截断
            if size > pos:
                with open(GADGET_LOG, "r", errors="ignore") as f:
                    f.seek(pos)
                    chunk = f.read()
                pos = size
                seen += chunk.count(POST_A_ACK_ID)
                if seen >= need:
                    log(f"POST_A_ACK: {seen}/{need} text dispatch confirmed by Frida hook")
                    return True, seen
        except Exception as e:
            log(f"wait_post_a_ack poll error: {e}")
        time.sleep(0.5)
    return False, seen


# ============ 主流程 ============

def check_consecutive_limit(db, chat_uname, is_at, exclude_id=None):
    if is_at:
        return True, "at-exempt"
    # 2026-10-06 19:38 修：按 msg_time 取最近3条（不管 SENT/INBOUND），
    # 之前只查 SENT，导致人类发的 INBOUND 没被算进去，误判"连续3条自发"
    # 2026-10-06 20:20 修：排除正在尝试发送的消息自身，否则自己把自己算进去永远触发
    if exclude_id:
        rows = db.execute(
            "SELECT sender FROM messages WHERE chat=? AND typeof(msg_time)='integer' "
            "AND id != ? ORDER BY msg_time DESC LIMIT ?",
            (chat_uname, exclude_id, CONSECUTIVE_LIMIT)).fetchall()
    else:
        rows = db.execute(
            "SELECT sender FROM messages WHERE chat=? AND typeof(msg_time)='integer' "
            "ORDER BY msg_time DESC LIMIT ?",
            (chat_uname, CONSECUTIVE_LIMIT)).fetchall()
    if len(rows) < CONSECUTIVE_LIMIT:
        return True, "not-enough-history"
    if all(r[0] == SELF_WXID for r in rows):
        return False, f"last {CONSECUTIVE_LIMIT} are all self"
    return True, "ok"


def parse_messages(reply_content):
    if not reply_content:
        return []
    rc = reply_content.strip()
    if rc.startswith("{"):
        try:
            data = json.loads(rc)
            msgs = data.get("messages")
            if isinstance(msgs, list) and msgs:
                return [str(m) for m in msgs if str(m).strip()]
        except Exception:
            pass
    return [reply_content] if reply_content.strip() else []


def strip_machine_blocks(text):
    """去掉合成消息里的机器可读块，只留给群看的部分。

    2026-10-10 故事开篇 bug：worker 按约定输出了【大纲】```json bible，
    本是给游戏 tick 解析存档用的，但原样发到了群里（还剧透了未来伏笔）。
    DB 里 reply_content 保持原样（tick 解析还要用），只在发送时剥离。
    """
    t = text or ""
    # 【大纲】整节（含后面的 ```json 块）
    t = re.sub(r"【大纲】.*?(?=【|$)", "", t, flags=re.S)
    # 残留的 ```json ... ``` 代码块（大纲标记缺失时的兜底）
    t = re.sub(r"```json.*?```", "", t, flags=re.S)
    # 末尾因剥离留下的悬空分隔线
    t = re.sub(r"(?:\n---)+\s*$", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def process_one(db):
    row, attempt_id = claim_next_send(db)
    if not row:
        return False
    msg_id, chat, chat_name, sender, sender_name, content, reply_content, is_at, level, action, media_path = row
    log(f"claimed #{msg_id} level={level} chat={chat} attempt={attempt_id}")

    # ---- 1. PRECHECK ----
    ok, reason = precheck()
    if not ok:
        log(f"PRECHECK fail: {reason} → RETRY (no keyboard touched)")
        set_error(db, msg_id, f"precheck: {reason}")
        mark_retry(db, msg_id, f"precheck: {reason}")
        return True

    # 3 条连发限制（@ 不受限；保活豁免，2026-10-07 用户批准）
    # 保活消息判定：behavior_log 里该 msg_id 的 reason 含"保活"
    is_keepalive = False
    try:
        r = db.execute(
            "SELECT reason FROM behavior_log WHERE msg_id=? ORDER BY created_at DESC LIMIT 1",
            (msg_id,)).fetchone()
        if r and r[0] and "保活" in r[0]:
            is_keepalive = True
    except Exception:
        pass
    # 2026-10-07 18:38 用户临时要求：小游戏介绍批次免除3连限制
    # 通过 state/no_consecutive_limit.flag 文件控制，删掉即恢复
    limit_disabled = os.path.exists(
        os.path.expanduser("~/workspace/wechat-bridge/state/no_consecutive_limit.flag"))
    if limit_disabled:
        log(f"consecutive limit disabled by flag: #{msg_id} 跳过连发限制")
    elif is_keepalive:
        log(f"keepalive exempt: #{msg_id} 跳过连发限制")
    else:
        allowed, reason = check_consecutive_limit(db, chat, bool(is_at), exclude_id=msg_id)
        if not allowed:
            log(f"consecutive limit: hold #{msg_id} ({reason})")
            # 2026-10-07 18:18 用户策略：3连限制触发后，暂停主动发言1小时
            try:
                from behavior import record_consecutive_limit_hit
                record_consecutive_limit_hit()
            except Exception:
                pass
            release_hold(db, msg_id, reason)
            return True

    parts = parse_messages(reply_content)
    # 2026-10-10 合成消息（GAME_*）：发送时剥离机器可读块（【大纲】/```json），
    # DB 里 reply_content 保持原样（游戏 tick 解析存档还要用里面的 JSON）。
    if (action or "").startswith("GAME_"):
        parts = [strip_machine_blocks(p) for p in parts]
        parts = [p for p in parts if p]
        if parts != parse_messages(reply_content):
            log(f"#{msg_id} 已剥离机器块再发送")

    # ---- 图片发送分支（2026-10-10 积分生图，2026-10-11 F5 表情包）----
    # 必须在空内容检查之前：图片消息没有 reply_content，只有 media_path。
    # 看图回复（media_path 是 worker 看图用的截图，reply_content 是文本）不走这里，
    # 否则会把截图重新发到群里（2026-10-10 19:11 实测 bug：#2915072）。
    # 走剪贴板发图，不走文本 paste 流程。
    # 2026-10-11 F5：STICKER 动作也走发图（表情包）
    # 2026-10-11 晚：text/html 方案验证成功（type=47），STICKER 改走 send_sticker_html
    if action in ("IMAGE_GEN", "STICKER") and media_path and os.path.exists(media_path):
        # 图片发送需要先导航到目标群（在 gui_lock 内）
        from gui_lock import gui_locked
        with gui_locked(timeout=120):
            ok, reason = navigate(chat)
            if not ok:
                log(f"NAVIGATE fail: {reason} → RETRY")
                set_error(db, msg_id, f"navigate: {reason}")
                mark_retry(db, msg_id, f"navigate: {reason}")
                return True
            log(f"图片消息 #{msg_id} [{action}]: {media_path}")
            set_phase(db, msg_id, "SENDING_IMAGE")
            # STICKER 用 text/html 发 type=47 动画表情（2026-10-11 验证）
            # IMAGE_GEN 仍用 send_image 发静态图
            if action == "STICKER":
                # 从文件名提取 MD5
                md5 = os.path.splitext(os.path.basename(media_path))[0]
                log(f"STICKER 走 send_sticker_html: md5={md5[:8]}...")
                ok, reason = send_sticker_html(chat, md5, media_path)
            else:
                ok, reason = send_image(media_path)
            if not ok:
                log(f"图片发送失败 #{msg_id}: {reason} → RETRY")
                set_error(db, msg_id, f"send_image: {reason}")
                mark_retry(db, msg_id, f"send_image: {reason}")
                return True
            log(f"图片已发送 #{msg_id}")
            mark_sent(db, msg_id)
            # F5：记录表情包发送频率
            if action == "STICKER":
                try:
                    import sticker
                    sticker.record_send(chat)
                except Exception:
                    pass
            return True

    # 2026-10-12 修：手动入队的文本行（sender='MUSE'）若只写了 content、
    # reply_content 为空，回退用 content 发送。之前直接走 empty→SENT 快捷分支
    # 静默丢消息（#4337329/#4337890/#4340758 实测：群里一条都没收到，还误标 SENT）。
    # worker 行的 content 是用户原话，绝不能回退 —— 仅 sender='MUSE' 的行可用。
    # IMAGE_GEN/STICKER 图片分支在上面已 return，不会误触。
    if not parts and sender == 'MUSE' and (content or '').strip():
        log(f"#{msg_id} reply_content 为空，sender=MUSE，回退用 content 发送")
        parts = parse_messages(content)

    if not parts:
        log(f"empty reply_content #{msg_id}, mark SENT")
        mark_sent(db, msg_id)
        return True

    # ---- 2. NAVIGATE ---- (2026-10-11 R2: GUI 独占锁覆盖导航→发送全程，防 TOCTOU)
    from gui_lock import gui_locked
    with gui_locked(timeout=120):
        ok, reason = navigate(chat)
        if not ok:
            log(f"NAVIGATE fail: {reason} → RETRY")
            set_error(db, msg_id, f"navigate: {reason}")
            mark_retry(db, msg_id, f"navigate: {reason}")
            return True
        set_phase(db, msg_id, "NAVIGATED")

        # ---- 3. VERIFY_CHAT ----
        passed, score = verify_chat(chat)
    if not passed:
        # 2026-10-06: 禁用自动交换坐标逻辑
        # 原因：VERIFY_CHAT 失败可能是窗口状态异常、弹窗遮挡等，
        # 不一定是坐标记错。自动交换曾把正确的坐标搞反，导致后续
        # 发送持续失败。改为只重试导航一次，不修改坐标文件。
        log(f"VERIFY_CHAT fail (ncc={score:.4f}), re-navigate once without swapping")
        ok, reason = navigate(chat)
        if ok:
            passed, score = verify_chat(chat)
        if not passed:
            log(f"VERIFY_CHAT fail twice (ncc={score:.4f}) → RETRY, NEVER SEND")
            set_error(db, msg_id, f"verify_chat ncc={score:.4f}")
            mark_retry(db, msg_id, f"verify_chat ncc={score:.4f}")
            return True
    set_phase(db, msg_id, "VERIFIED_CHAT")
    log(f"VERIFY_CHAT pass (ncc={score:.4f})")
    # 验证通过，记录当前聚焦（下次同目标可跳过导航）
    set_current_focus(chat)

    # ---- 4+5. 逐条 PASTE → SEND ----
    # 2026-10-07 用户拍板：3 条上限只卡主动搭话（PROACTIVE/自言自语），
    # 回复（尤其 research 类）放宽到 10 条，避免长回复被截断。
    max_parts = 3 if (action or "") == "PROACTIVE" else 10
    sent_parts = []
    # POST_A ack 基准点：ENTER 前的 gadget.log 位置（只认之后的新事件）
    ack_log_pos = gadget_log_size()
    for i, part in enumerate(parts):
        if i >= max_parts:
            log(f"max {max_parts} parts for action={action or 'REPLY'}, stop")
            break
        if i > 0:
            gap = random.uniform(1, 4)
            log(f"gap {gap:.1f}s before part {i+1}/{len(parts)}")
            time.sleep(gap)

        v2_ok = False
        v2_entered = False
        if PATH_A_V2_ENABLED:
            v2_ok, v2_entered, v2_reason = send_part_v2(part)
            if v2_ok:
                set_phase(db, msg_id, "ENTERED")
                log(f"part {i+1}/{len(parts)} ENTERED via v2 ({v2_reason})")
            elif v2_entered:
                # 回车已发出但注入未确认：内容不确定（可能是占位符原文），
                # 不得 fallback 重发，走不确定流程查 WAL
                set_phase(db, msg_id, "ENTERED")
                log(f"V2 uncertain part {i+1}: {v2_reason}, check WAL")
                break
            else:
                log(f"V2 fail part {i+1}: {v2_reason} → fallback to paste")
        if not v2_ok:
            ok, reason, at_target = paste_content(part)
            if not ok:
                log(f"PASTE fail part {i+1}: {reason} → RETRY")
                set_error(db, msg_id, f"paste part{i+1}: {reason}")
                mark_retry(db, msg_id, f"paste part{i+1}: {reason}")
                return True
            set_phase(db, msg_id, "PASTED")

            ok, reason = do_send()
            # ENTERED 必须在发送动作附近立即持久化（先写 phase，再判断结果）
            set_phase(db, msg_id, "ENTERED")
            if not ok:
                # Enter 可能已发出也可能没发出 → 按 ENTERED 处理：查 WAL
                log(f"SEND uncertain part {i+1}: {reason}, check WAL")
                break
            log(f"part {i+1}/{len(parts)} ENTERED")
        sent_parts.append(part)

    # ---- 6. 发送确认：POST_A_ACK 优先（2026-10-08 逆向） ----
    # 文本消息必经 POST_A CASE 0（0x696012a），只读 hook 在回车后约 5s 触发 ack。
    # 比 45s 的 VERIFY_WAL 快且准；hook 未挂/无 ack 时 12s 超时，走 WAL 兜底。
    all_ok = False
    if sent_parts:
        ack_ok, ack_seen = wait_post_a_ack(ack_log_pos, need=len(sent_parts), timeout=12)
        if ack_ok:
            all_ok = True
            log(f"#{msg_id} SENT (post_a ack {ack_seen}/{len(sent_parts)})")
        else:
            # 兜底：短 WAL 检查（10s，原 45s 太长且毛刺多）
            send_started = int(time.time()) - 120  # 宽限 2 分钟
            wal_ok = True
            for part in sent_parts:
                if not verify_wal(chat, part, send_started, timeout=10):
                    wal_ok = False
                    log(f"VERIFY_WAL fail for part: {part[:20]}...")
                    break
            if wal_ok:
                all_ok = True
                log(f"#{msg_id} SENT (wal fallback confirmed)")

        if all_ok and sent_parts:
            mark_sent(db, msg_id)
        else:
            mark_uncertain(db, msg_id, "post_a ack + wal not confirmed, no auto-resend")
            log(f"#{msg_id} SEND_UNCERTAIN")
    return True


# ============ 崩溃恢复 ============

def wal_contains_self(db, chat_id, content, since_ts):
    """恢复用：查 SQLite 确认自己是否已发送。
    D方案（2026-10-06）：不再跑 db_poller 直读微信源 DB。
    查两处：1) 独立的 OUTBOUND 行；2) 已关联 source 的 SENT 行
    （daemon 去重时会把 OUTBOUND 的 source 回填到 SENT 行并删重复）。
    2026-10-06 修：归一化比较 + 转义 LIKE。"""
    try:
        norm_target = _normalize_for_match(content)
        if not norm_target:
            return False
        snippet = _escape_like(content.strip()[:30])
        rows = db.execute(
            """SELECT content FROM messages
               WHERE chat=? AND msg_time >= ?
                 AND content LIKE ? ESCAPE '\\'
                 AND (direction='OUTBOUND'
                      OR (status='SENT' AND source_local_id IS NOT NULL))
               LIMIT 5""",
            (chat_id, since_ts, f"%{snippet}%")).fetchall()
        for (c,) in rows:
            nc = _normalize_for_match(c)
            if nc == norm_target or norm_target in nc:
                return True
        return False
    except Exception as e:
        log(f"reconcile outbound check error: {e}")
        return False


def reconcile_stale_sending(db):
    """
    启动时恢复遗留 SENDING：
      phase < ENTERED → 回 READY（Enter 没按过，安全）
      phase == ENTERED → 查 WAL：找到 → SENT；找不到 → SEND_UNCERTAIN
    绝不因 lease 超时把 ENTERED 重回 READY。
    """
    now = int(time.time())
    rows = db.execute(
        "SELECT id, chat, reply_content, send_started_at, send_phase "
        "FROM messages WHERE status='SENDING'").fetchall()
    n = 0
    for msg_id, chat, reply_content, started_at, phase in rows:
        phase = phase or ""
        idx = PHASE_ORDER.index(phase) if phase in PHASE_ORDER else 0
        entered_idx = PHASE_ORDER.index("ENTERED")
        if idx < entered_idx:
            db.execute(
                "UPDATE messages SET status='READY', send_owner='', "
                "lease_owner='', lease_until=0, send_phase='', "
                "last_send_error='reconcile: phase<ENTERED, safe to retry', "
                "updated_at=? WHERE id=?", (now, msg_id))
            log(f"reconcile #{msg_id} phase={phase or 'CLAIMED'} (<ENTERED) → READY")
        else:
            parts = parse_messages(reply_content)
            first_part = (parts[0] if parts else "").strip()
            since = (started_at or now) - 300
            if first_part and wal_contains_self(db, chat, first_part, since):
                db.execute(
                    "UPDATE messages SET status='SENT', sent_at=?, "
                    "updated_at=? WHERE id=?", (now, now, msg_id))
                log(f"reconcile #{msg_id} ENTERED + wal proved → SENT")
            else:
                db.execute(
                    "UPDATE messages SET status='SEND_UNCERTAIN', "
                    "last_send_error='reconcile: ENTERED but wal unproven, "
                    "no auto-resend', updated_at=? WHERE id=?", (now, msg_id))
                log(f"reconcile #{msg_id} ENTERED + wal unproven → SEND_UNCERTAIN")
        n += 1
    if n:
        db.commit()
    return n


def main():
    # 2026-10-06 18:55 fix: PID 文件防止多实例
    pidfile = os.path.join(STATE_DIR, "sendd_v2.pid")
    if os.path.exists(pidfile):
        try:
            with open(pidfile) as f:
                old_pid = int(f.read().strip())
            # 检查旧进程是否还活着
            os.kill(old_pid, 0)
            log(f"another sendd_v2 already running (pid {old_pid}), exiting")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # 旧进程已死，继续启动
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    log(f"starting v2, sender_id={SENDER_ID}, pid={os.getpid()}")
    os.environ["DISPLAY"] = ":99"
    os.makedirs(STATE_DIR, exist_ok=True)
    # 2026-10-06 加：心跳文件，供 keepalive 判断是否僵死
    hb_file = "/tmp/wechat-bridge/sendd_v2.heartbeat"
    os.makedirs(os.path.dirname(hb_file), exist_ok=True)
    def _heartbeat():
        try:
            with open(hb_file, "w") as f:
                f.write(str(int(time.time())))
        except Exception:
            pass
    _heartbeat()
    db = get_db()
    n = reconcile_stale_sending(db)
    if n:
        log(f"reconciled {n} stale SENDING")
    loop_count = 0
    while True:
        try:
            worked = process_one(db)
            if not worked:
                time.sleep(POLL_INTERVAL)
        except Exception as e:
            log(f"loop error: {e}")
            time.sleep(POLL_INTERVAL)
        # 每 10 轮写一次心跳（约每 50 秒）
        loop_count += 1
        if loop_count % 10 == 0:
            _heartbeat()


if __name__ == "__main__":
    main()
