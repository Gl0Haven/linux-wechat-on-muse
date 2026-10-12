#!/usr/bin/env python3
"""
阻塞式 Muse worker - SQLite 队列的唯一消费者（主路径）

流程:
  查 SQLite 有无 NEW/可恢复任务
    → 有: claim (带 owner_id + lease) → 生成正文 → 写 READY → 继续
    → 无: wait_for_at.py --timeout 120 阻塞等待 → 醒来再查

worker 跑一段时间/处理若干任务后主动退出，让 supervisor 重拉。
- 最多处理 MAX_JOBS 个任务，或运行 MAX_RUNTIME 秒，满足任一即退出
- 生成期间定期续 lease，防止被误判死亡

注意: 这是给 subagent worker 调用的入口，不是独立 daemon。
subagent 的 prompt 会指导它调用这个脚本的各个函数。
"""

import sys
import os
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import (
    get_db, claim_next_reply, mark_ready, mark_dead, mark_skipped,
    get_group_context, get_last_muse_reply_target, get_sender_recent,
    get_recent_context, get_merged_context,
)

# worker 生命周期限制
MAX_JOBS = 20          # 最多处理 20 个任务后退出
MAX_RUNTIME = 2700     # 最多运行 45 分钟后退出
LEASE_SECONDS = 300    # claim 时 lease 5 分钟
RENEW_INTERVAL = 120   # 每 2 分钟续一次 lease

WORKER_ID = os.environ.get("WECHAT_WORKER_ID") or f"worker-{uuid.uuid4().hex[:8]}"

def renew_lease(db, msg_id):
    """续 lease"""
    new_until = int(time.time()) + LEASE_SECONDS
    db.execute("""
        UPDATE messages SET lease_until=?, updated_at=?
        WHERE id=? AND lease_owner=?
    """, (new_until, int(time.time()), msg_id, WORKER_ID))
    db.commit()

def process_one(db):
    """
    尝试 claim 并返回一个待处理的消息。
    返回 (msg_dict, needs_generation) 或 (None, False)。
    实际的 LLM 生成由调用方 (subagent) 完成，这里只做 claim。
    msg_dict 含 should_reply：1=EXPLICIT 直接回；2=CONTEXTUAL 需 Muse 先判定。
    """
    row = claim_next_reply(db, WORKER_ID, lease_seconds=LEASE_SECONDS)
    if not row:
        return None
    # 2026-10-11: 图片消息租约延长到 10 分钟（看图 3-6min，5min 不够）
    # row[9]=media_path, row[10]=needs_media
    _has_media = (len(row) > 10 and (row[9] or row[10]))
    if _has_media:
        try:
            db.execute(
                "UPDATE messages SET lease_until=?, updated_at=? WHERE id=? AND lease_owner=?",
                (int(time.time()) + 600, int(time.time()), row[0], WORKER_ID))
            db.commit()
            print(f"[worker] 图片消息 #{row[0]} 租约延长至 10 分钟", flush=True)
        except Exception as e:
            print(f"[worker] 延长租约失败: {e}", flush=True)
    # F1 2026-10-11: 密度检查（硬限制直接跳过，EXPLICIT 不受限）
    # row[7] 是 should_reply
    if row[7] == 2:  # 只对 CONTEXTUAL 限流，@ 保持快速回复
        try:
            import reply_density
            allowed, hint = reply_density.check_density(row[1])
            if not allowed:
                from msgqueue import mark_skipped
                mark_skipped(db, row[0], "density_limit")
                print(f"[worker] 密度超限跳过 #{row[0]}", flush=True)
                return None
            if hint:
                # 软限制：存 hint 供 prompt 用
                pass  # 由后续 prompt 组装读取
        except Exception as e:
            print(f"[worker] 密度检查失败: {e}", flush=True)
    # row: id, chat, chat_name, sender, sender_name, content, msg_time,
    #      should_reply, claim_version, media_path, needs_media
    msg = {
        "id": row[0],
        "chat": row[1],
        "chat_name": row[2],
        "sender": row[3],
        "sender_name": row[4],
        "content": row[5],
        "msg_time": row[6],
        "should_reply": row[7],
        "worker_id": WORKER_ID,
        "claim_version": row[8] if len(row) > 8 else 0,  # 2026-10-09 fencing token
        # 2026-10-09 图片理解：media_path 有值=已截屏；needs_media=1 且
        # media_path 为空=待截图，worker 应先跑 capture_image.py 再生成回复
        "media_path": row[9] if len(row) > 9 else None,
        "needs_media": row[10] if len(row) > 10 else 0,
    }
    # F1 软限制 hint 透给 prompt
    try:
        import reply_density
        _, _hint = reply_density.check_density(msg["chat"])
        if _hint:
            msg["density_hint"] = _hint
    except Exception:
        pass
    # 2026-10-09 21:45 图片搜图政策（用户允许）→ 2026-10-09 22:42 收紧：
    # 只有明确问图里"是什么/是谁"（带询问语气指向图片内容）时，才用
    # brave_search 验证。单张图最多搜2次。优先肉眼判断，搜不到就按
    # 看到的说，不要瞎编。非询问语气（如单纯描述、感叹）不搜。
    if msg["needs_media"] == 1:
        msg["image_search_policy"] = (
            "仅当用户明确询问图片内容（图里是什么、是谁、在哪，带疑问语气）"
            "且肉眼无法确定时，才可用 "
            "~/workspace/skills/brave-search/bin/brave_search.py "
            "搜一下验证。单张图最多搜2次。优先用肉眼判断，搜不到就按看到"
            "的说，不要瞎编。非询问语气一律不搜。"
        )
    # 2026-10-09 图片理解自动截图：needs_media=1 且 media_path 为空时，
    # 自动跑 capture_image.py 截图，worker 无需手动处理。失败不阻塞回复。
    # 2026-10-11 E1 修订：若 image_index 已尝试解密并标记 decrypt_failed，
    # 不再回退 GUI 截图（避免误触/抢焦点），worker 直接按"图片不可用"处理。
    _decrypt_failed = False
    if msg["needs_media"] == 1 and not msg["media_path"]:
        try:
            _logrow = db.execute(
                "SELECT state FROM image_index_log WHERE msg_id=?",
                (msg["id"],)).fetchone()
            _decrypt_failed = bool(_logrow and _logrow[0] == "decrypt_failed")
        except Exception:
            pass
    if msg["needs_media"] == 1 and not msg["media_path"] and not _decrypt_failed:
        try:
            import subprocess
            subprocess.run(
                ["python3", "capture_image.py", msg["chat"], str(msg["id"])],
                cwd="~/workspace/wechat-bridge",
                timeout=120, capture_output=True, text=True
            )
            row2 = db.execute(
                "SELECT media_path FROM messages WHERE id=?",
                (msg["id"],)).fetchone()
            if row2 and row2[0]:
                msg["media_path"] = row2[0]
                print(f"[process_one] 图片已截图: {row2[0]}", flush=True)
        except Exception as e:
            print(f"[process_one] 图片截图失败: {e}", flush=True)
    # 2026-10-11 E1：图片资源索引 + V2 解密（message_resource.db → DAT → PNG）。
    # worker 优先用解密图（media_path）；解密失败记 decrypt_failed，不回退截图。
    if msg["needs_media"] == 1:
        try:
            import image_index
            indexed = image_index.get_indexed(db, msg["id"])
            if indexed:
                msg["image_resources"] = indexed
                print(f"[process_one] 图片资源索引: "
                      f"{list(indexed.keys())} (msg #{msg['id']})", flush=True)
        except Exception as e:
            print(f"[process_one] 图片索引读取失败: {e}", flush=True)
    # 2026-10-09 22:39 图片指代问题：纯文本问句指向之前发的图
    # （如"刚才那张图右下角是谁"），自动找30分钟内最新的图，把 media_path 挂上。
    # 否则 worker 看不到图，直接 SKIP。
    if not msg["media_path"] and msg["content"]:
        _img_kw = ["图片", "照片", "那张图", "这张图", "刚才的图", "最新的图",
                   "右下角", "左下角", "右上角", "左上角", "中间那个", "皮套"]
        if any(k in msg["content"] for k in _img_kw):
            try:
                row3 = db.execute(
                    """SELECT id, media_path FROM messages
                       WHERE chat=? AND content='[图片]'
                       AND media_path IS NOT NULL AND media_path != ''
                       AND msg_time > ? AND msg_time <= ?
                       ORDER BY msg_time DESC LIMIT 1""",
                    (msg["chat"], msg["msg_time"] - 1800,
                     msg["msg_time"])).fetchone()
                if row3 and row3[1]:
                    msg["media_path"] = row3[1]
                    msg["referenced_image_id"] = row3[0]
                    # 2026-10-09 22:42 图片指代问句也给搜索政策（同收紧版）
                    msg["image_search_policy"] = (
                        "仅当用户明确询问图片内容（图里是什么、是谁、在哪，"
                        "带疑问语气）且肉眼无法确定时，才可用 "
                        "~/workspace/skills/brave-search/bin/brave_search.py "
                        "搜一下验证。单张图最多搜2次。优先用肉眼判断，搜不"
                        "到就按看到的说，不要瞎编。非询问语气一律不搜。"
                    )
                    print(f"[process_one] 图片指代: 关联到 #{row3[0]} "
                          f"({row3[1]})", flush=True)
            except Exception as e:
                print(f"[process_one] 图片指代查找失败: {e}", flush=True)
    # 上下文组装：
    # - CONTEXTUAL(2)：群最近 10 条 + Muse 上次回复对象，供 Muse 判定是否在对它说
    # - EXPLICIT(1)：发送者最近 3 条（排除当前），用户要求没 @ 的前文和 @ 合在一起回
    # - 2026-10-06 用户要求：所有回复都附带5分钟全量上下文，避免漏掉前后脚的消息
    msg["recent_5min"] = get_recent_context(db, msg["chat"], minutes=5)

    # 2026-10-11 E2/E3：富语义解析（引用/表情/链接）。只读，不阻塞。
    # 引用消息回查原图/原文，表情包提描述，链接提标题。
    try:
        import msg_parse
        _content = msg.get("content") or ""
        _enriched = None
        if "<refermsg>" in _content:
            _enriched = msg_parse.enrich_message(49, _content)
        elif "<appmsg" in _content:
            _enriched = msg_parse.enrich_message(49, _content)
        elif "<emoji" in _content or (
                _content.strip().startswith("[") and _content.strip().endswith("]")):
            _enriched = msg_parse.enrich_message(47, _content)
        if _enriched and _enriched.get("kind") != "unknown":
            msg["semantic"] = _enriched
            print(f"[process_one] 语义解析: kind={_enriched['kind']}"
                  f" summary={_enriched['summary'][:60]}", flush=True)
    except Exception as e:
        print(f"[process_one] 语义解析失败: {e}", flush=True)

    if msg["should_reply"] == 2:
        msg["context"] = get_group_context(db, msg["chat"], limit=10)
        msg["last_muse_reply_target"] = get_last_muse_reply_target(
            db, msg["chat"])
    else:
        msg["sender_recent"] = get_sender_recent(
            db, msg["chat"], msg["sender"], msg["id"], limit=3)

    # 2026-10-06 自动 research 触发（不依赖 LLM 自觉）：
    # claim 后立即用规则判断，命中则自动建 job，原消息转 WAITING_RESEARCH。
    # worker 收到 research_triggered=true 时不要直接生成，等 research 完成。
    msg["research_triggered"] = False
    msg["research_job_id"] = None
    try:
        from research_helper import (classify_research_need,
                                     check_cache_first, check_guardrails,
                                     create_research_job)
        from behavior import can_trigger_research, REPLY, log_behavior
        # 只有 EXPLICIT(1) 才自动触发；CONTEXTUAL 需 Muse 先判定是否在对它说
        if msg["should_reply"] == 1:
            need = classify_research_need(msg["content"])
            if need == "RESEARCH" and can_trigger_research(REPLY):
                # 先查缓存
                hit, _ = check_cache_first(db, msg["content"])
                if not hit:
                    ok, _ = check_guardrails(db)
                    if ok:
                        jid = create_research_job(
                            db, msg["id"], msg["chat"], msg["sender"],
                            msg["content"])
                        if jid:
                            msg["research_triggered"] = True
                            msg["research_job_id"] = jid
                            # 2026-10-06 补：research 触发审计
                            try:
                                log_behavior(db, msg["chat"], REPLY,
                                             f"auto-research job #{jid}",
                                             msg["id"])
                            except Exception:
                                pass
    except Exception as e:
        # research 模块异常不阻塞正常回复
        print(f"[process_one] auto-research 跳过: {e}", flush=True)
    # 2026-10-07 小游戏：如果这条消息关联了游戏事件，附带渲染包。
    # msg["game"] = {"link":..., "event":{"facts":...}, "game":{...}}
    # facts 是确定性事实（答案/输赢/分数），worker 只做语言表现。
    try:
        from games import store as _gstore
        _gstore.ensure_schema(db)
        _render = _gstore.get_render(db, msg["id"])
        if _render and (_render["link"]["kind"] or "").startswith("GAME_"):
            msg["game"] = _render
    except Exception as e:
        print(f"[process_one] game attach 跳过: {e}", flush=True)
    return msg

def commit_skip(db, msg_id, reason="muse judged SKIP", worker_id=None,
                claim_version=None):
    """CONTEXTUAL 经 Muse 判定不回复 → SKIPPED 独立终态
    2026-10-09：改用原子 fencing 提交，不再 SELECT-then-UPDATE"""
    wid = worker_id or WORKER_ID
    # 取 chat 用于审计（不校验 lease，只读）
    row = db.execute(
        "SELECT chat FROM messages WHERE id=?", (msg_id,)
    ).fetchone()
    chat = row[0] if row else ""
    ok = mark_skipped(db, msg_id, reason, wid, claim_version)
    if not ok:
        print(f"[worker] fencing 拒绝：msg #{msg_id} 已被其他 worker 接管",
              flush=True)
        return False
    # 2026-10-06 补：行为审计 + 群聊记忆
    try:
        from behavior import log_behavior, SKIP
        log_behavior(db, chat, SKIP, reason, msg_id)
    except Exception:
        pass
    try:
        from chat_memory import log_muse_action
        log_muse_action(db, chat, "SKIP", f"msg #{msg_id}: {reason[:50]}")
    except Exception:
        pass
    # 2026-10-07 补：标记 event worker 干过活（防卡住误判）
    try:
        from event_worker_state import mark_work_done
        mark_work_done()
    except Exception:
        pass
    print(f"[worker] msg #{msg_id} 记为 SKIPPED", flush=True)
    return True


def _maybe_send_sticker(db, msg_id, chat, reply_text):
    """2026-10-11：根据回复内容决定是否配一张表情图。
    
    2026-10-11 19:30 用户要求：不要一刀切禁用，而是让 wal_reader 能识别
    bot 发的贴纸（通过 queue.db 的 STICKER 记录关联），只跳过 bot 自己的，
    用户发的图正常触发读图。
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    
    # 排除游戏/日报/新闻/STICKER 自身
    # 排除读图回复（needs_media=1 的消息，worker 已经看过图了，再配表情会冲突）
    try:
        row = db.execute("SELECT action, needs_media FROM messages WHERE id=?", (msg_id,)).fetchone()
        action = (row[0] or "") if row else ""
        needs_media = row[1] if row and len(row) > 1 else 0
        if action.startswith("GAME_") or action == "STICKER":
            return
        if needs_media == 1:
            # 读图任务：用户发了图，bot 正在分析回复，此时配表情包会干扰
            return
    except:
        pass
    
    text = (reply_text or "").strip()
    # 放宽：200字以内都可配图（之前30字太严）
    if len(text) > 200:
        return
    
    try:
        import sticker
        # 频率检查
        if not sticker.check_rate_limit(chat):
            return
        
        # 情绪关键词 → emotion_hint
        emotion_hint = None
        emotion_map = {
            "哈哈": "开心", "呵呵": "开心", "笑死": "开心",
            "收到": "好的", "好的": "好的", "明白": "好的", "嗯嗯": "好的",
            "晚安": "晚安", "早安": "打招呼", "你好": "打招呼", "嗨": "打招呼",
            "谢谢": "感谢", "感谢": "感谢",
            "无语": "无语",
            "加油": "加油", "厉害": "厉害", "牛": "厉害",
            "可爱": "可爱", "呆萌": "可爱",
        }
        for kw, emo in emotion_map.items():
            if kw in text:
                emotion_hint = emo
                break
        
        # 从库里选一张
        picked = sticker.select_sticker(db, emotion_hint=emotion_hint) if hasattr(sticker, 'select_sticker') else None
        if not picked:
            # 回退：用 sticker.py 的 pick_sticker（关键词匹配）
            import sqlite3
            # 直接查库
            if emotion_hint:
                row = db.execute("""
                    SELECT md5, file_path FROM sticker_library
                    WHERE resource_status='readable' AND file_path IS NOT NULL
                    AND emotion_tags LIKE ?
                    ORDER BY RANDOM() LIMIT 1
                """, (f"%{emotion_hint}%",)).fetchone()
            else:
                row = db.execute("""
                    SELECT md5, file_path FROM sticker_library
                    WHERE resource_status='readable' AND file_path IS NOT NULL
                    ORDER BY RANDOM() LIMIT 1
                """).fetchone()
            if not row or not os.path.exists(row[1]):
                return
            md5, file_path = row[0], row[1]
        else:
            md5, file_path = picked["md5"], picked["file_path"]
        
        # 入队 STICKER 消息（参考 sticker.py send_md5 的逻辑）
        import hashlib
        import time
        now = int(time.time())
        content = f"[表情包]"
        msg_hash = hashlib.md5(f"{chat}:{now}:{md5}:auto".encode()).hexdigest()
        # 检查去重
        exists = db.execute("SELECT id FROM messages WHERE msg_hash=?", (msg_hash,)).fetchone()
        if exists:
            return
        db.execute("""
            INSERT INTO messages
            (chat, sender, sender_name, content, msg_time, created_at,
             should_reply, status, direction, action, media_path, msg_hash)
            VALUES (?, 'GAME_SYSTEM', 'Muse', ?, ?, ?, 0, 'READY', 'OUTBOUND', 'STICKER', ?, ?)
        """, (chat, content, now, now, file_path, msg_hash))
        db.commit()
        sticker.record_send(chat)
        print(f"[worker] 已配表情图 {md5[:8]}: {file_path}", flush=True)
    except Exception as e:
        # 静默失败，不影响主回复
        print(f"[worker] 配图跳过: {e}", flush=True)


def commit_reply(db, msg_id, reply_content, worker_id=None,
                 claim_version=None):
    """提交生成的回复
    2026-10-09：改用原子 fencing 提交，不再 SELECT-then-UPDATE
    2026-10-11：提交后尝试配一张表情图（单帧降级版）"""
    wid = worker_id or WORKER_ID
    row = db.execute(
        "SELECT chat FROM messages WHERE id=?", (msg_id,)
    ).fetchone()
    chat = row[0] if row else ""
    ok = mark_ready(db, msg_id, reply_content, wid, claim_version)
    if not ok:
        print(f"[worker] fencing 拒绝：msg #{msg_id} 已被其他 worker 接管",
              flush=True)
        return False
    # 2026-10-11：表情包自动配图（启发式，不打扰主流程）
    try:
        _maybe_send_sticker(db, msg_id, chat, reply_content)
    except Exception as e:
        print(f"[worker] 表情配图失败（不影响主回复）: {e}", flush=True)
    # 2026-10-06 补：行为审计 + 群聊记忆
    try:
        from behavior import log_behavior, REPLY
        log_behavior(db, chat, REPLY, "muse generated", msg_id,
                     detail=reply_content[:200])
    except Exception:
        pass
    try:
        from chat_memory import log_muse_action
        log_muse_action(db, chat, "REPLY",
                        f"msg #{msg_id}: {reply_content[:50]}")
    except Exception:
        pass
    # 2026-10-07 补：标记 event worker 干过活（防卡住误判）
    try:
        from event_worker_state import mark_work_done
        mark_work_done()
    except Exception:
        pass
    print(f"[worker] msg #{msg_id} 已写 READY", flush=True)
    return True

def mark_failed(db, msg_id, error=""):
    """标记失败，进入 RETRY"""
    db.execute("""
        UPDATE messages
        SET status='RETRY', retry_count=retry_count+1,
            lease_owner='', lease_until=0, error=?
        WHERE id=? AND lease_owner=?
    """, (error, msg_id, WORKER_ID))
    db.commit()

def get_stats():
    """返回 worker 应该继续还是退出"""
    # 由调用方跟踪，这里提供检查函数
    pass

def process_batch(db, max_batch=10):
    """
    2026-10-09 Batch2：会话级批量领取。
    返回 {"explicit": [...], "sessions": {chat_id: [...]}}。
    - explicit：每条走快速路径，直接生成回复
    - sessions：每组一次 LLM 会话级裁决，返回 consumed_message_ids
    每条含 claim_version，commit 时用 fencing 校验。
    """
    from msgqueue import claim_batch
    batch = claim_batch(db, WORKER_ID, lease_seconds=LEASE_SECONDS,
                        max_batch=max_batch)
    # 2026-10-11: 图片消息租约延长到 10 分钟
    try:
        _now = int(time.time())
        for _m in batch.get("explicit", []):
            if _m.get("media_path") or _m.get("needs_media"):
                db.execute(
                    "UPDATE messages SET lease_until=?, updated_at=? WHERE id=? AND lease_owner=?",
                    (_now + 600, _now, _m["id"], WORKER_ID))
        for _msgs in batch.get("sessions", {}).values():
            for _m in _msgs:
                if _m.get("media_path") or _m.get("needs_media"):
                    db.execute(
                        "UPDATE messages SET lease_until=?, updated_at=? WHERE id=? AND lease_owner=?",
                        (_now + 600, _now, _m["id"], WORKER_ID))
        db.commit()
    except Exception:
        pass
    # 为每条附加上下文（复用 process_one 的逻辑）
    for m in batch["explicit"]:
        m["worker_id"] = WORKER_ID
        m["recent_5min"] = get_recent_context(db, m["chat"], minutes=5)
        m["sender_recent"] = get_sender_recent(
            db, m["chat"], m["sender"], m["id"], limit=3)
        # 2026-10-10 游戏/功能渲染包（hook 路径补齐）：
        # 合成消息（stats/digest/news/meme 等，link kind 以 GAME_ 开头）
        # 必须附带 facts，否则 worker 只看到 "[GAME:...] #N" 无法生成回复。
        # 逻辑同 process_one，失败不阻塞。
        try:
            from games import store as _gstore
            _gstore.ensure_schema(db)
            _render = _gstore.get_render(db, m["id"])
            if _render and (_render["link"]["kind"] or "").startswith("GAME_"):
                m["game"] = _render
        except Exception as e:
            print(f"[process_batch] game attach 跳过: {e}", flush=True)
        # 2026-10-10 图片理解补齐（hook 路径）：needs_media=1 且 media_path
        # 为空时自动跑 capture_image.py 截图。逻辑同 process_one，失败不阻塞。
        if m.get("needs_media") == 1:
            m["image_search_policy"] = (
                "仅当用户明确询问图片内容（图里是什么、是谁、在哪，"
                "带疑问语气）且肉眼无法确定时，才可用 "
                "~/workspace/skills/brave-search/bin/brave_search.py "
                "搜一下验证。单张图最多搜2次。优先用肉眼判断，搜不到就按"
                "看到的说，不要瞎编。非询问语气一律不搜。"
            )
        if m.get("needs_media") == 1 and not m.get("media_path"):
            try:
                import subprocess
                subprocess.run(
                    ["python3", "capture_image.py", m["chat"], str(m["id"])],
                    cwd="~/workspace/wechat-bridge",
                    timeout=120, capture_output=True, text=True
                )
                row2 = db.execute(
                    "SELECT media_path FROM messages WHERE id=?",
                    (m["id"],)).fetchone()
                if row2 and row2[0]:
                    m["media_path"] = row2[0]
                    print(f"[process_batch] 图片已截图: {row2[0]}", flush=True)
            except Exception as e:
                print(f"[process_batch] 图片截图失败: {e}", flush=True)
    for chat, msgs in batch["sessions"].items():
        # 会话级：整组的合并上下文，供一次 LLM 裁决
        ctx = get_merged_context(db, chat)
        for m in msgs:
            m["worker_id"] = WORKER_ID
            m["session_ctx"] = ctx
            m["session_ids"] = [x["id"] for x in msgs]
    return batch

if __name__ == "__main__":
    # 命令行测试
    db = get_db()
    print(f"Worker ID: {WORKER_ID}")
    msg = process_one(db)
    if msg:
        print(f"Claimed: #{msg['id']} from {msg['sender_name']}: "
              f"{msg['content'][:40]}")
    else:
        print("No pending @ messages")
