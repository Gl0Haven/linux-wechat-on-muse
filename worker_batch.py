#!/usr/bin/env python3
"""
固定 worker 入口脚本（2026-10-09 P1）。
确定性批量检查：无 schema 探测、无 API 猜测，秒级完成。

用法：
  python3 worker_batch.py                    # 检查并输出 JSON（含待处理消息+上下文）
  python3 worker_batch.py --skip ID REASON WID CVER   # commit_skip（fencing）
  python3 worker_batch.py --reply ID WID CVER         # commit_reply，内容从 stdin 读
  python3 worker_batch.py --proactive CHAT CONTENT     # 插入 PROACTIVE READY，内容从 stdin 读

JSON 输出字段：
  frozen: bool, muted: bool,
  worker_id: str,
  explicit: [{id, chat, chat_name, sender_name, content, claim_version, recent_5min, sender_recent}],
  sessions: {chat: [{id, ...}]},  # 每组含 session_ctx
  research_pending: [{id, question, status}],
  proactive: {chat: {allowed, reason, last_activity_min, recent_proactive[], recent_inbound[]}},
  queue_stats: {NEW, READY, GENERATING, ...}
"""
import sys
import os
import json
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def get_worker_id():
    return os.environ.get("WECHAT_WORKER_ID") or f"worker-{uuid.uuid4().hex[:8]}"


def main_inspect():
    import msgqueue
    import blocking_worker

    out = {}
    # 1. freeze 检查
    freeze_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "state", "freeze.flag")
    out["frozen"] = os.path.exists(freeze_path)

    db = msgqueue.get_db()
    # 2026-10-11: 动态续租图片消息（防长任务卡死）
    try:
        renewed = msgqueue.renew_expiring_image_leases(db)
        if renewed:
            out["leases_renewed"] = renewed
    except Exception:
        pass

    # 2. 静默检查（正确：取 tuple 第一个元素）
    muted, _ = msgqueue.is_muted(db)
    out["muted"] = bool(muted)

    # 3. 固定 WORKER_ID（模块级，供 process_batch 使用）
    wid = get_worker_id()
    blocking_worker.WORKER_ID = wid
    out["worker_id"] = wid

    if out["frozen"] or out["muted"]:
        out["explicit"] = []
        out["sessions"] = {}
        out["research_pending"] = []
        out["proactive"] = {}
        out["queue_stats"] = {}
        print(json.dumps(out, ensure_ascii=False))
        return

    # 4. 批量领取（确定性 API，无探测）
    batch = blocking_worker.process_batch(db, max_batch=10)

    # 整理 explicit（只保留 Agent 决策需要的字段）
    explicit = []
    for m in batch.get("explicit", []):
        item = {
            "id": m["id"],
            "chat": m.get("chat"),
            "chat_name": m.get("chat_name"),
            "sender_name": m.get("sender_name"),
            "content": m.get("content"),
            "claim_version": m.get("claim_version"),
            "worker_id": wid,
            "recent_5min": m.get("recent_5min", []),
            "sender_recent": m.get("sender_recent", []),
            # 2026-10-10 图片理解补齐：hook worker 需要知道有没有图、
            # 图截好没，否则会"回了但没看"
            "media_path": m.get("media_path"),
            "needs_media": m.get("needs_media", 0),
            "image_search_policy": m.get("image_search_policy", ""),
            # 2026-10-11 Step 1：图片资源索引（三档 DAT 路径，只做定位不解密）
            "image_resources": m.get("image_resources", {}),
            # 2026-10-11 E2/E3：富语义（引用/表情/链接解析）
            "semantic": m.get("semantic", {}),
            # 2026-10-11 F1：密度软限制提示
            "density_hint": m.get("density_hint", ""),
            # 2026-10-10 游戏/功能渲染包：合成消息（stats/digest/news/meme）
            # 的 facts（固定文本/统计数字/生成指示）必须透给 worker，
            # 否则 worker 只看到 "[GAME:...] #N" 无法生成回复。
            "game": m.get("game"),
        }
        # 2026-10-10 积分生图：IMAGE_GEN 任务带生成指示，worker 直接生成
        #（20 积分已在 wal_reader 扣过；失败要退费）
        if m.get("action") == "IMAGE_GEN":
            item["is_image_gen"] = True
            item["image_gen_instruction"] = (
                "这是一条积分生图任务（用户已付 20 积分）。请："
                "1) 用图片生成工具按 content（prompt）生成图片，"
                f"保存到 ~/workspace/wechat-bridge/media/gen_{m['id']}.png；"
                "2) 生成成功：执行 "
                f"sqlite3 ~/workspace/wechat-bridge/queue.db "
                f"\"UPDATE messages SET media_path='<绝对路径>', action='REPLY', "
                f"status='READY', ready_at=$(date +%s), updated_at=$(date +%s) WHERE id={m['id']};\" "
                "（sendd 会自动发图）；"
                "3) 生成失败：退 20 积分 "
                f"(python3 -c \"import sys; sys.path.insert(0,'$HOME/workspace/wechat-bridge'); "
                "from games import store as g; import msgqueue; db=msgqueue.get_db(); "
                f"r=db.execute(\"SELECT chat,sender,sender_name FROM messages WHERE id={m['id']}\").fetchone(); "
                "g.add_points_by_chat(db,r[0],r[1],r[2],20); db.commit()\")，"
                "再把任务转文字："
                f"sqlite3 ~/workspace/wechat-bridge/queue.db "
                f"\"UPDATE messages SET action='REPLY', reply_content='抱歉，图片生成失败，"
                f"20积分已退回。下次再试试吧。', status='READY', ready_at=$(date +%s), updated_at=$(date +%s) "
                f"WHERE id={m['id']};\"。"
                "然后用 --reply 提交空回复（或 --skip）关闭此任务。"
            )
        explicit.append(item)
    out["explicit"] = explicit

    # 整理 sessions
    sessions = {}
    for chat, msgs in batch.get("sessions", {}).items():
        lst = []
        for m in msgs:
            lst.append({
                "id": m["id"],
                "chat": m.get("chat"),
                "chat_name": m.get("chat_name"),
                "sender_name": m.get("sender_name"),
                "content": m.get("content"),
                "claim_version": m.get("claim_version"),
                "worker_id": wid,
                "media_path": m.get("media_path"),
                "needs_media": m.get("needs_media", 0),
            })
        # session_ctx 只取第一条的（整组共享）
        ctx = msgs[0].get("session_ctx", []) if msgs else []
        sessions[chat] = {"messages": lst, "session_ctx": ctx,
                          "chat_name": msgs[0].get("chat_name") if msgs else chat}
    out["sessions"] = sessions

    # 5. research（确定性表结构）
    rows = db.execute(
        "SELECT id, msg_id, question, status FROM research_jobs "
        "WHERE status != 'DONE' ORDER BY id DESC LIMIT 20").fetchall()
    out["research_pending"] = [
        {"id": r[0], "msg_id": r[1], "question": (r[2] or "")[:100], "status": r[3]}
        for r in rows]

    # 6. 主动搭话资格（两个游戏群）
    proactive = {}
    try:
        from behavior import check_proactive_allowed
        chats = [
            ("YOUR_MAIN_GROUP@chatroom", "[主群]"),
            ("YOUR_TEST_GROUP@chatroom", "[测试群]"),
        ]
        now = int(time.time())
        for chat, name in chats:
            allowed, reason = check_proactive_allowed(db, chat, now)
            # 最近入站活动（分钟前）
            row = db.execute(
                "SELECT MAX(msg_time) FROM messages WHERE chat=? "
                "AND direction='INBOUND'", (chat,)).fetchone()
            last = row[0] if row and row[0] else 0
            age_min = round((now - last) / 60, 1) if last else -1
            # 最近 3 条主动消息（防重复话题）
            recent_proactive = []
            for pr in db.execute(
                    "SELECT content, created_at FROM messages "
                    "WHERE chat=? AND direction='OUTBOUND' AND action='PROACTIVE' "
                    "AND status IN ('SENT','READY','SENDING') "
                    "ORDER BY created_at DESC LIMIT 3", (chat,)):
                recent_proactive.append({
                    "content": (pr[0] or "")[:80],
                    "min_ago": round((now - pr[1]) / 60, 1) if pr[1] else -1,
                })
            # 最近群聊内容（5分钟+10条，供主动搭话看话题）
            recent_inbound = []
            for ib in db.execute(
                    "SELECT sender_name, content, msg_time FROM messages "
                    "WHERE chat=? AND direction='INBOUND' AND msg_time > ? "
                    "ORDER BY msg_time DESC LIMIT 10", (chat, now - 300)):
                recent_inbound.append({
                    "sender": ib[0] or "",
                    "content": (ib[1] or "")[:60],
                    "min_ago": round((now - ib[2]) / 60, 1) if ib[2] else -1,
                })
            proactive[name] = {"allowed": allowed, "reason": reason,
                               "last_activity_min": age_min, "chat": chat,
                               "recent_proactive": recent_proactive,
                               "recent_inbound": recent_inbound}
    except Exception as e:
        proactive["_error"] = str(e)
    out["proactive"] = proactive

    # 7. 队列统计
    stats = {}
    for r in db.execute("SELECT status, COUNT(*) FROM messages GROUP BY status"):
        stats[r[0]] = r[1]
    out["queue_stats"] = stats

    print(json.dumps(out, ensure_ascii=False))


def main_skip(msg_id, reason, worker_id, claim_version):
    import msgqueue
    import blocking_worker
    blocking_worker.WORKER_ID = worker_id
    db = msgqueue.get_db()
    ok = blocking_worker.commit_skip(db, int(msg_id), reason,
                                     worker_id, int(claim_version))
    print(json.dumps({"ok": ok, "action": "skip", "id": int(msg_id)},
                     ensure_ascii=False))


def main_reply(msg_id, worker_id, claim_version):
    import msgqueue
    import blocking_worker
    blocking_worker.WORKER_ID = worker_id
    content = sys.stdin.read()
    db = msgqueue.get_db()
    ok = blocking_worker.commit_reply(db, int(msg_id), content,
                                      worker_id, int(claim_version))
    print(json.dumps({"ok": ok, "action": "reply", "id": int(msg_id)},
                     ensure_ascii=False))


def main_proactive(chat, chat_name):
    """插入一条 PROACTIVE READY 消息，内容从 stdin 读，10 分钟过期。"""
    import msgqueue
    content = sys.stdin.read().strip()
    if not content:
        print(json.dumps({"ok": False, "error": "empty content"}))
        return
    db = msgqueue.get_db()
    now = int(time.time())
    # 直接插入 OUTBOUND PROACTIVE READY（10 分钟过期由 sendd/scheduler 处理）
    # 2026-10-09 修：同时写 reply_content，sendd 读 reply_content 发送，空则误标 SENT
    db.execute("""INSERT INTO messages
        (msg_hash, chat, chat_name, sender, sender_name, content, reply_content,
         msg_time, created_at, should_reply, status, direction, action, not_before)
        VALUES (?, ?, ?, 'MUSE', 'MUSE', ?, ?, ?, ?, 0, 'READY', 'OUTBOUND', 'PROACTIVE', ?)""",
        (f"proactive-{now}-{uuid.uuid4().hex[:8]}", chat, chat_name,
         content, content, now, now, now))
    db.commit()
    mid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    print(json.dumps({"ok": True, "action": "proactive", "id": mid},
                     ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--skip":
        # --skip ID REASON WID CVER
        main_skip(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--reply":
        # --reply ID WID CVER < content.txt
        main_reply(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--proactive":
        # --proactive CHAT CHAT_NAME < content.txt
        main_proactive(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--note":
        # --note ID WID "note text" : worker 存中间思路（长任务超时降级用）
        # 2026-10-11: 图片长任务 worker 定期存思路，超时后 fallback 引用
        import msgqueue
        db = msgqueue.get_db()
        _nid, _wid = sys.argv[2], sys.argv[3]
        _note = sys.argv[4] if len(sys.argv) > 4 else ""
        ok = msgqueue.save_worker_note(db, _nid, _note, _wid)
        print(json.dumps({"ok": ok, "action": "note", "id": _nid}, ensure_ascii=False))
    else:
        main_inspect()
