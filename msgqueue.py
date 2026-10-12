#!/usr/bin/env python3
"""
微信桥接 SQLite 队列 - 替代 messages.jsonl / at_queue.jsonl / db_cursor.json

状态机: NEW -> GENERATING (带 lease) -> READY -> SENDING -> SENT
异常: RETRY, DEAD, SEND_UNCERTAIN (绝不自动重发)
"""

import sqlite3
import json
import time
import hashlib
import os

DB_PATH = os.path.expanduser("~/workspace/wechat-bridge/queue.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_hash TEXT UNIQUE NOT NULL,  -- 指纹去重（source-backed 行绑定源身份后天然唯一，
                                   -- UNIQUE 只对无 source 的手动消息起去重作用）
    chat TEXT NOT NULL,             -- 群 username 或单聊 wxid
    chat_name TEXT,                 -- 群名或联系人备注
    sender TEXT NOT NULL,           -- 发送者 wxid
    sender_name TEXT,               -- 发送者昵称
    content TEXT NOT NULL,
    msg_time INTEGER NOT NULL,      -- 微信消息时间戳
    created_at INTEGER NOT NULL,    -- 入库时间
    is_at INTEGER DEFAULT 0,        -- 是否 @ 了小号
    is_dm INTEGER DEFAULT 0,        -- 是否单聊
    should_reply INTEGER DEFAULT 0, -- 读取层判定：0=IGNORE, 1=EXPLICIT, 2=CONTEXTUAL
    status TEXT DEFAULT 'NEW',      -- NEW, GENERATING, READY, SENDING, SENT, RETRY, DEAD, SEND_UNCERTAIN, SKIPPED, ARCHIVED
    lease_until INTEGER DEFAULT 0,  -- lease 到期时间 (unix timestamp)
    lease_owner TEXT DEFAULT '',    -- 持有 lease 的 worker ID (owner_id)
    retry_count INTEGER DEFAULT 0,
    reply_content TEXT DEFAULT '',  -- 生成的回复正文
    sent_at INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    updated_at INTEGER DEFAULT 0,   -- 最后更新时间（用于 heartbeat 续租）
    send_attempt_id TEXT DEFAULT '', -- 本次发送尝试 ID（sendd 每次 claim 生成）
    send_started_at INTEGER DEFAULT 0, -- 本次发送开始时间（用于 SENDING 恢复时查 WAL）
    send_owner TEXT DEFAULT '',      -- 发送进程 ID（sendd 实例）
    send_phase TEXT DEFAULT '',     -- 发送阶段：CLAIMED/NAVIGATED/VERIFIED_CHAT/PASTED/ENTERED
    send_attempts INTEGER DEFAULT 0, -- 累计发送尝试次数
    last_send_error TEXT DEFAULT '', -- 最后一次发送错误
    action TEXT DEFAULT '',          -- vNext: REPLY/CONTEXTUAL/PROACTIVE/FOLLOWUP
    not_before INTEGER DEFAULT 0,    -- vNext: 退避，不早于此时可领取/发送 (unix timestamp)
    priority INTEGER DEFAULT 0,      -- vNext: 优先级 EXPLICIT=100/CONTEXTUAL=60/FOLLOWUP=30/PROACTIVE=10
    hold_reason TEXT DEFAULT '',      -- vNext: 暂缓原因，如 CONSECUTIVE_LIMIT
    used_research INTEGER DEFAULT 0,  -- vNext: 是否用过联网 research
    research_status TEXT DEFAULT 'NONE', -- vNext: NONE/REQUESTED/RUNNING/DONE/FAILED/TIMEOUT
    research_started_at INTEGER DEFAULT 0,
    research_finished_at INTEGER DEFAULT 0,
    research_summary TEXT DEFAULT '',
    research_sources TEXT DEFAULT '', -- JSON 字符串
    research_error TEXT DEFAULT '',
    direction TEXT DEFAULT 'INBOUND', -- INBOUND=收到的消息，OUTBOUND=本号发出的（仅供发送验证）
    -- D方案 source 唯一键（2026-10-06）：消息身份用微信源字段，不用时间戳
    source_db TEXT DEFAULT NULL,      -- 微信源库，如 message/message_0.db
    source_table TEXT DEFAULT NULL,   -- 微信源表，如 Msg_<md5>
    source_local_id INTEGER DEFAULT NULL,  -- 微信 local_id（消息身份主键）
    source_server_id INTEGER DEFAULT NULL, -- 微信 server_id（非零时可额外校验）
    sort_seq INTEGER DEFAULT NULL,    -- 微信 sort_seq（消息顺序，单调）
    source_rowid INTEGER DEFAULT NULL,-- 微信源 rowid（辅助调试）
    recovered INTEGER DEFAULT 0,      -- 历史补扫标记：1=补回来的旧消息，只做上下文
    claim_version INTEGER DEFAULT 0  -- 2026-10-09 fencing token：每次 claim 原子+1，commit 时校验
);

CREATE INDEX IF NOT EXISTS idx_status ON messages(status);
CREATE INDEX IF NOT EXISTS idx_lease ON messages(status, lease_until);
CREATE INDEX IF NOT EXISTS idx_hash ON messages(msg_hash);
CREATE INDEX IF NOT EXISTS idx_chat_time ON messages(chat, msg_time);
-- 注意：idx_source_unique 不在这里建（旧表 CREATE TABLE IF NOT EXISTS 不加列，
-- 索引会因列不存在而失败）。统一由 get_db() 的 migration 部分在补列后创建。

CREATE INDEX IF NOT EXISTS idx_status ON messages(status);
CREATE INDEX IF NOT EXISTS idx_lease ON messages(status, lease_until);
CREATE INDEX IF NOT EXISTS idx_hash ON messages(msg_hash);
CREATE INDEX IF NOT EXISTS idx_chat_time ON messages(chat, msg_time);

-- 游标表：替代 db_cursor.json
CREATE TABLE IF NOT EXISTS cursor (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 群上下文表：每个群最近 N 条消息，供 Muse 参考
CREATE TABLE IF NOT EXISTS group_context (
    chat TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

def get_db():
    # 防御：0 字节的库文件是"连错路径"的典型症状，直接报错不建空库
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) == 0:
        raise RuntimeError(
            f"DB file {DB_PATH} is 0 bytes — wrong path? refusing to use it")
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL;")
    # 2026-10-12 修复：DDL 撞锁保护。get_db() 每次连接都跑全套 DDL（幂等），
    # 多进程并发时 DROP INDEX 等需独占锁的操作会报 database is locked，
    # 曾崩掉 health_monitor 和 sync_heartbeat。DDL 全是 IF NOT EXISTS 幂等，
    # 撞锁时跳过即可（持锁的进程会完成迁移）。
    try:
        _run_ddl(db)
    except sqlite3.OperationalError as e:
        if "locked" not in str(e).lower():
            raise
        # 撞锁：别的进程正在做 DDL，跳过（schema 最终一致）
    return db


def _run_ddl(db):
    """get_db() 的 DDL 部分（幂等）。抽出来以便撞锁时整体跳过。"""
    db.executescript(SCHEMA)
    # 存量库补列（IF NOT EXISTS 风格，幂等）
    cols = [r[1] for r in db.execute("PRAGMA table_info(messages)")]
    if "send_attempt_id" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN send_attempt_id TEXT DEFAULT ''")
    if "send_started_at" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN send_started_at INTEGER DEFAULT 0")
    if "send_owner" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN send_owner TEXT DEFAULT ''")
    if "send_phase" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN send_phase TEXT DEFAULT ''")
    if "send_attempts" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN send_attempts INTEGER DEFAULT 0")
    if "last_send_error" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN last_send_error TEXT DEFAULT ''")
    # vNext 字段（幂等补列）
    vnext_cols = [
        ("action", "TEXT DEFAULT ''"),
        ("not_before", "INTEGER DEFAULT 0"),
        ("priority", "INTEGER DEFAULT 0"),
        ("hold_reason", "TEXT DEFAULT ''"),
        ("used_research", "INTEGER DEFAULT 0"),
        ("research_status", "TEXT DEFAULT 'NONE'"),
        ("research_started_at", "INTEGER DEFAULT 0"),
        ("research_finished_at", "INTEGER DEFAULT 0"),
        ("research_summary", "TEXT DEFAULT ''"),
        ("research_sources", "TEXT DEFAULT ''"),
        ("research_error", "TEXT DEFAULT ''"),
    ]
    for col_name, col_def in vnext_cols:
        if col_name not in cols:
            db.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_def}")
    # D方案字段（2026-10-06）：direction + source 唯一键列 + recovered
    # 用户指出：enqueue() INSERT direction 但 SCHEMA 里没有，新建空库会 no column
    dscheme_cols = [
        ("direction", "TEXT DEFAULT 'INBOUND'"),
        ("source_db", "TEXT DEFAULT NULL"),
        ("source_table", "TEXT DEFAULT NULL"),
        ("source_local_id", "INTEGER DEFAULT NULL"),
        ("source_server_id", "INTEGER DEFAULT NULL"),
        ("sort_seq", "INTEGER DEFAULT NULL"),
        ("source_rowid", "INTEGER DEFAULT NULL"),
        ("recovered", "INTEGER DEFAULT 0"),
    ]
    for col_name, col_def in dscheme_cols:
        if col_name not in cols:
            db.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_def}")
    # D方案源唯一键索引（SCHEMA 里已有 CREATE UNIQUE INDEX IF NOT EXISTS，
    # 这里再保一遍，防止旧库建表时 SCHEMA 还没这行）
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_source_unique
        ON messages(source_db, source_table, source_local_id)""")
    # not_before 索引（claim 时过滤用）
    db.execute("CREATE INDEX IF NOT EXISTS idx_not_before ON messages(status, not_before)")
    # 步骤0 静默开关（2026-10-06）：mute_state 表 + expires_at 字段
    db.execute("""CREATE TABLE IF NOT EXISTS mute_state (
        scope TEXT PRIMARY KEY,      -- 'global' 全局静默
        mute_until INTEGER NOT NULL, -- 静默到何时（unix timestamp），0=未静默
        set_by_wxid TEXT DEFAULT '', -- 谁设置的
        reason TEXT DEFAULT '',      -- 原因/触发词
        created_at INTEGER NOT NULL
    )""")
    # messages 表加 expires_at（READY 过期时间，默认 10 分钟）
    cols2 = [r[1] for r in db.execute("PRAGMA table_info(messages)")]
    if "expires_at" not in cols2:
        db.execute("ALTER TABLE messages ADD COLUMN expires_at INTEGER DEFAULT 0")
    # 2026-10-09 fencing token：claim_version（防幽灵 worker 覆盖）
    if "claim_version" not in cols2:
        db.execute("ALTER TABLE messages ADD COLUMN claim_version INTEGER DEFAULT 0")
    # 2026-10-10 图片字段：enqueue()/claim RETURNING 用 media_path/needs_media，
    # 新建空库时补上（生产库此前为手工加列）
    if "media_path" not in cols2:
        db.execute("ALTER TABLE messages ADD COLUMN media_path TEXT DEFAULT ''")
    if "needs_media" not in cols2:
        db.execute("ALTER TABLE messages ADD COLUMN needs_media INTEGER DEFAULT 0")
    # 延迟统计字段：claim / mark_ready 会写，latency_report 读（生产库此前为手工加列）
    if "worker_claimed_at" not in cols2:
        db.execute("ALTER TABLE messages ADD COLUMN worker_claimed_at INTEGER DEFAULT 0")
    if "ready_at" not in cols2:
        db.execute("ALTER TABLE messages ADD COLUMN ready_at INTEGER DEFAULT 0")
    # 步骤2 Research（2026-10-06）：research_jobs 表
    db.execute("""CREATE TABLE IF NOT EXISTS research_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id INTEGER NOT NULL,         -- 关联的 messages.id
        chat_id TEXT NOT NULL,           -- 群
        asker_wxid TEXT DEFAULT '',      -- 提问人
        question TEXT NOT NULL,          -- 规整后的问题
        status TEXT DEFAULT 'PENDING',   -- PENDING/RUNNING/DONE/FAILED/TIMEOUT
        created_at INTEGER NOT NULL,
        started_at INTEGER DEFAULT 0,
        finished_at INTEGER DEFAULT 0,
        deadline_at INTEGER DEFAULT 0,   -- 截止时间（默认创建+120秒）
        result_json TEXT DEFAULT '',     -- subagent 的结构化结果
        error TEXT DEFAULT '',
        brave_calls INTEGER DEFAULT 0,
        open_calls INTEGER DEFAULT 0
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_rjobs_status
        ON research_jobs(status)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_rjobs_msg
        ON research_jobs(msg_id)""")
    # 步骤3 事实缓存（2026-10-06）：fact_cache 表（先建表，步骤3 实现逻辑）
    db.execute("""CREATE TABLE IF NOT EXISTS fact_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_key TEXT NOT NULL,         -- 规整后的问题或实体
        fact_text TEXT NOT NULL,
        source_url TEXT DEFAULT '',
        source_name TEXT DEFAULT '',
        fetched_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        confidence TEXT DEFAULT 'medium', -- high/medium/low
        status TEXT DEFAULT 'STAGED',    -- STAGED/ACTIVE/REJECTED
        job_id INTEGER DEFAULT 0,
        hit_count INTEGER DEFAULT 0
    )""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_fcache_topic
        ON fact_cache(topic_key)""")
    # 2026-10-06 修：topic_key 改为非唯一索引，允许多条 STAGED 记录
    # （同一话题的多个独立来源），支撑"两个独立来源晋升 ACTIVE"逻辑
    db.execute("DROP INDEX IF EXISTS idx_fcache_topic")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_fcache_topic
        ON fact_cache(topic_key)""")
    # 步骤6 群聊记忆（2026-10-06）：chat_state 表（先建表，步骤6 实现逻辑）
    db.execute("""CREATE TABLE IF NOT EXISTS chat_state (
        chat TEXT PRIMARY KEY,
        current_topic TEXT DEFAULT '',
        open_questions TEXT DEFAULT '',   -- JSON 数组
        muse_recent_actions TEXT DEFAULT '', -- JSON 数组
        updated_at INTEGER NOT NULL
    )""")
    return db

def msg_fingerprint(chat, sender, content, msg_time):
    """生成消息指纹用于去重"""
    raw = f"{chat}|{sender}|{content}|{msg_time}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def enqueue(db, chat, chat_name, sender, sender_name, content, msg_time,
           is_at=False, is_dm=False, should_reply=0, direction='INBOUND',
           source=None, commit=True, status='NEW', needs_media=0):
    """入队，重复消息返回 None。should_reply: 0=IGNORE, 1=EXPLICIT, 2=CONTEXTUAL
    direction: INBOUND=收到的消息，OUTBOUND=本号发出的（仅供发送验证，不回复）
    source: dict(source_db, source_table, local_id, server_id, sort_seq, rowid)，
        有则走源唯一键去重（INSERT OR IGNORE），无则走 msg_hash 去重。
    commit: False 时不提交，由调用方统一事务提交（D方案：入库+游标同事务）。
    status: 初始状态。2026-10-09：OUTBOUND 验证记录应直接标 SENT，不标 NEW
       （NEW 的 OUTBOUND 是无主僵尸，无人负责终结）。
    """
    fp = msg_fingerprint(chat, sender, content, msg_time)
    now = int(time.time())
    level = int(should_reply) if isinstance(should_reply, int) else (1 if should_reply else 0)
    src = source or {}
    if src.get("local_id") is not None:
        # D方案（2026-10-06）：source-backed 行的 hash 必须绑定源身份。
        # 否则"同一发送人同一秒连发两次相同文字"会被 msg_hash UNIQUE 误杀，
        # 即使两条消息的 source_local_id 不同。绑定后每条源消息的 hash
        # 天然唯一，UNIQUE 约束只对无 source 的手动消息起去重作用。
        fp = msg_fingerprint(
            f"{src.get('source_db')}|{src.get('source_table')}",
            f"local:{src.get('local_id')}",
            content, msg_time)
    try:
        db.execute("""
            INSERT OR IGNORE INTO messages
            (msg_hash, chat, chat_name, sender, sender_name, content, msg_time,
             created_at, is_at, is_dm, should_reply, status, direction,
             source_db, source_table, source_local_id, source_server_id,
             sort_seq, source_rowid, needs_media, not_before)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?)
        """, (fp, chat, chat_name, sender, sender_name, content, msg_time,
              now, 1 if is_at else 0, 1 if is_dm else 0, level, status, direction,
              src.get("source_db"), src.get("source_table"),
              src.get("local_id"), src.get("server_id"),
              src.get("sort_seq"), src.get("rowid"), 1 if needs_media else 0,
              # F1 2026-10-11: CONTEXTUAL 合并窗口 15s，EXPLICIT 立即
              now + 15 if level == 2 else 0))
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            return None  # 被 IGNORE（重复）
        if commit:
            db.commit()
        mid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        # 2026-10-06: 新消息入队 → 更新聊天顺序（该群提到位置2）
        # 只对群聊做（chat 以 @chatroom 结尾），单聊不影响
        if chat and chat.endswith("@chatroom"):
            try:
                import os as _os
                import sys as _sys
                _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
                from chat_order import touch_chat
                touch_chat(chat)
            except Exception:
                pass
        # F2 2026-10-11: 群活动 → 重置主动状态机的未接话计数
        if direction == 'INBOUND' and chat and "@chatroom" in chat:
            try:
                import proactive_state
                proactive_state.on_group_activity(chat)
            except Exception:
                pass
        return mid
    except sqlite3.IntegrityError:
        return None  # msg_hash 重复

def claim_next_reply(db, worker_id, lease_seconds=120):
    """
    领取下一条需要回复的消息（should_reply IN (1,2)）。
    level=1 EXPLICIT：直接回复正文。
    level=2 CONTEXTUAL：worker 把群上下文一起给 Muse，由 Muse 判断
      “这句是不是在对我说”——不是则返回固定标记 SKIP（记为已处理不发送），
      是则正常回答正文。不引入第二个 LLM。
    只取状态为 NEW 或 GENERATING 但 lease 已过期的。
    原子操作：BEGIN IMMEDIATE 确保只有一个 worker 能 claim 成功。
    步骤0 静默开关：静默期间不 claim，新消息标记 SKIPPED（reason=muted）。
    返回 (id, chat, chat_name, sender, sender_name, content, msg_time) 或 None。
    """
    now = int(time.time())
    # 步骤0：静默检查。静默期间把待回复消息直接标 SKIPPED，不生成回复。
    muted, _until = is_muted(db, now)
    if muted:
        db.execute("BEGIN IMMEDIATE")
        try:
            cur = db.execute("""
                UPDATE messages
                SET status='SKIPPED', hold_reason='muted',
                    updated_at=?
                WHERE id = (
                    SELECT id FROM messages
                    WHERE should_reply IN (1, 2)
                      AND status IN ('NEW', 'GENERATING', 'RETRY')
                      AND (status != 'GENERATING' OR lease_until < ?)
                    LIMIT 1
                )
                RETURNING id
            """, (now, now))
            row = cur.fetchone()
            db.commit()
            if row:
                print(f"[worker] muted: msg #{row[0]} → SKIPPED", flush=True)
            return None
        except Exception:
            db.rollback()
            return None
    lease_until = now + lease_seconds
    # 使用 IMMEDIATE 事务确保原子性
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute("""
            UPDATE messages
            SET status='GENERATING', lease_until=?, lease_owner=?,
                claim_version=claim_version+1, updated_at=?,
                worker_claimed_at=?
            WHERE id = (
                SELECT id FROM messages
                WHERE should_reply IN (1, 2)
                  AND status IN ('NEW', 'GENERATING', 'RETRY')
                  AND (status != 'GENERATING' OR lease_until < ?)
                  AND not_before <= ?
                ORDER BY
                  -- 2026-10-07 小游戏加速：游戏互动消息优先插队；
                  -- GAME_PROPOSE 是可选提议，放最后
                  CASE WHEN action IN ('GAME_REPLY', 'GAME_START',
                                       'GAME_END', 'GAME_TICK') THEN 0
                       WHEN action = 'GAME_PROPOSE' THEN 3
                       ELSE 1 END,
                  -- EXPLICIT(1) 优先于 CONTEXTUAL(2)；DESC 会反（2>1），用 CASE
                  CASE should_reply WHEN 1 THEN 0 WHEN 2 THEN 1 ELSE 2 END,
                  msg_time ASC
                LIMIT 1
            )
            RETURNING id, chat, chat_name, sender, sender_name,
                      content, msg_time, should_reply, claim_version,
                      media_path, needs_media
        """, (lease_until, worker_id, now, now, now, now))
        row = cur.fetchone()
        db.commit()
        return row
    except Exception:
        db.rollback()
        raise

def claim_next_at(db, worker_id, lease_seconds=120):
    """
    [兼容旧接口] 领取下一条需要处理的 @ 消息。
    新代码请用 claim_next_reply()。
    """
    return claim_next_reply(db, worker_id, lease_seconds)

def claim_batch(db, worker_id, lease_seconds=300, max_batch=10):
    """
    2026-10-09 Batch2：会话级批量领取。
    - should_reply=1（EXPLICIT）：全部领取，走快速路径
    - should_reply=2（CONTEXTUAL）：按 chat 分组，每组最多取最近的 N 条，
      供一次 LLM 会话级裁决
    返回：{"explicit": [...], "sessions": {chat_id: [...]}}
    每条含 claim_version（fencing token）。
    原子操作：BEGIN IMMEDIATE。
    """
    now = int(time.time())
    lease_until = now + lease_seconds
    # 静默检查
    muted, _ = is_muted(db, now)
    if muted:
        return {"explicit": [], "sessions": {}}

    db.execute("BEGIN IMMEDIATE")
    try:
        # 先领 EXPLICIT（快速路径，不聚合）
        cur = db.execute("""
            UPDATE messages
            SET status='GENERATING', lease_until=?, lease_owner=?,
                claim_version=claim_version+1, updated_at=?,
                worker_claimed_at=?
            WHERE id IN (
                SELECT id FROM messages
                WHERE should_reply=1
                  AND status IN ('NEW', 'GENERATING', 'RETRY')
                  AND (status != 'GENERATING' OR lease_until < ?)
                ORDER BY msg_time ASC
                LIMIT ?
            )
            RETURNING id, chat, chat_name, sender, sender_name,
                      content, msg_time, should_reply, claim_version,
                      media_path, needs_media, action
        """, (lease_until, worker_id, now, now, now, max_batch))
        explicit = [dict(zip(
            ["id","chat","chat_name","sender","sender_name",
             "content","msg_time","should_reply","claim_version",
             "media_path","needs_media","action"], r))
            for r in cur.fetchall()]

        # 再领 CONTEXTUAL，按 chat 聚合（每组最多 5 条，1-3s 聚合窗口由调用方控制）
        cur = db.execute("""
            UPDATE messages
            SET status='GENERATING', lease_until=?, lease_owner=?,
                claim_version=claim_version+1, updated_at=?,
                worker_claimed_at=?
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, chat,
                           ROW_NUMBER() OVER (PARTITION BY chat ORDER BY msg_time DESC) as rn
                    FROM messages
                    WHERE should_reply=2
                      AND status IN ('NEW', 'GENERATING', 'RETRY')
                      AND (status != 'GENERATING' OR lease_until < ?)
                ) WHERE rn <= 5
                LIMIT ?
            )
            RETURNING id, chat, chat_name, sender, sender_name,
                      content, msg_time, should_reply, claim_version,
                      media_path, needs_media, action
        """, (lease_until, worker_id, now, now, now, max_batch))
        sessions = {}
        for r in cur.fetchall():
            m = dict(zip(
                ["id","chat","chat_name","sender","sender_name",
                 "content","msg_time","should_reply","claim_version",
                 "media_path","needs_media","action"], r))
            sessions.setdefault(m["chat"], []).append(m)
        # 每组按时间正序
        for chat in sessions:
            sessions[chat].sort(key=lambda x: x["msg_time"])

        db.commit()
        return {"explicit": explicit, "sessions": sessions}
    except Exception:
        db.rollback()
        raise

def mark_ready(db, msg_id, reply_content, worker_id=None, claim_version=None):
    """Muse 生成完回复，标记为待发送。步骤0：设置 expires_at（默认10分钟过期）。
    2026-10-09 fencing：如传入 worker_id+claim_version，原子校验租约和版本，
    0 行更新 = 被顶掉，拒绝提交。"""
    import time
    now = int(time.time())
    if worker_id is not None and claim_version is not None:
        cur = db.execute(
            "UPDATE messages SET status='READY', reply_content=?, "
            "expires_at=?, updated_at=?, ready_at=? WHERE id=? "
            "AND status='GENERATING' AND lease_owner=? AND claim_version=?",
            (reply_content, now + READY_EXPIRE_SECONDS, now, now, msg_id,
             worker_id, claim_version))
        db.commit()
        return cur.rowcount > 0
    db.execute(
        "UPDATE messages SET status='READY', reply_content=?, "
        "expires_at=?, updated_at=?, ready_at=? WHERE id=?",
        (reply_content, now + READY_EXPIRE_SECONDS, now, now, msg_id))
    db.commit()
    return True

def claim_next_ready(db, worker_id, lease_seconds=60):
    """发送进程领取下一条待发送的回复"""
    now = int(time.time())
    lease_until = now + lease_seconds
    cur = db.execute("""
        UPDATE messages
        SET status='SENDING', lease_until=?, lease_owner=?
        WHERE id = (
            SELECT id FROM messages
            WHERE status IN ('READY', 'SENDING')
              AND (status != 'SENDING' OR lease_until < ?)
            ORDER BY
              -- 2026-10-07 小游戏加速：游戏消息优先发送
              CASE WHEN action IN ('GAME_REPLY', 'GAME_START',
                                   'GAME_END', 'GAME_TICK') THEN 0
                   ELSE 1 END,
              msg_time ASC
            LIMIT 1
        )
        RETURNING id, chat, chat_name, reply_content
    """, (lease_until, worker_id, now))
    row = cur.fetchone()
    db.commit()
    return row

def renew_expiring_image_leases(db, max_renews=3, renew_secs=300, threshold_secs=60):
    """动态续租（2026-10-11）：只针对图片消息。

    临近过期（<60s）且仍在 GENERATING 的图片消息，自动续 5 分钟。
    最多续 3 次（10min 初始 + 3*5min = 25min 上限），防死循环卡死。
    到上限仍未完成 → 自动生成降级回复（诚实说明做不出来）。
    返回续租的消息 ID 列表。
    """
    import time
    now = int(time.time())
    rows = db.execute("""
        SELECT id, lease_renews, sender_name, chat FROM messages
        WHERE status='GENERATING'
          AND lease_until < ?
          AND lease_until > ?
          AND (media_path IS NOT NULL AND media_path != '' OR needs_media=1)
    """, (now + threshold_secs, now - 300)).fetchall()
    renewed = []
    for msg_id, renews, sender_name, chat in rows:
        if renews < max_renews:
            db.execute("""
                UPDATE messages SET lease_until=?, lease_renews=?, updated_at=?
                WHERE id=? AND status='GENERATING'
            """, (now + renew_secs, renews + 1, now, msg_id))
            renewed.append(msg_id)
            print(f"[lease] 图片 #{msg_id} 自动续租 5min（第{renews+1}次）", flush=True)
        else:
            # 到上限：生成降级回复
            _force_fallback_reply(db, msg_id, sender_name, now)
    if renewed:
        db.commit()
    return renewed

def _force_fallback_reply(db, msg_id, sender_name, now):
    """到续租上限仍未完成 → 诚实降级回复。
    2026-10-11 用户要求：不说套词，结合图片实际内容和已输出的内容综合，
    详细说做了什么、想到什么，再说时间不够不死啃了。
    worker_notes 有值时优先用 worker 自己的半截思路（不编）。
    """
    import os
    row = db.execute(
        "SELECT content, media_path, created_at, worker_notes FROM messages WHERE id=?",
        (msg_id,)).fetchone()
    orig_content = (row[0] if row else "")[:80]
    media_path = row[1] if row else ""
    wait_min = (now - row[2]) // 60 if row and row[2] else 0
    worker_notes = (row[3] if row else "") or ""

    # 图片实际信息
    img_info = ""
    if media_path and os.path.exists(media_path):
        try:
            from PIL import Image
            with Image.open(media_path) as im:
                img_info = f"{im.width}×{im.height}"
        except Exception:
            pass

    parts = [f"@{sender_name} "]
    # 优先用 worker 自己的半截思路
    if worker_notes:
        parts.append(f"我看了 {wait_min} 分钟，")
        parts.append(f"目前的想法是：{worker_notes[:200]}")
        parts.append("但还没完全做出来，时间不够了先不死磕。")
    else:
        # 没有 notes 时用已知事实，不编
        if "数学" in orig_content or "题" in orig_content or "MUSE" in orig_content:
            parts.append("这道题我认认真真看了 ")
        elif img_info:
            parts.append(f"这张 {img_info} 的图我认认真真看了 ")
        else:
            parts.append("这张图我认认真真看了 ")
        if wait_min > 0:
            parts.append(f"{wait_min} 分钟，")
        parts.append("实话说没啃下来。")
        if "矩阵" in orig_content or "数表" in orig_content:
            parts.append("矩阵性质 P 的证明题，三问层层递进，")
        parts.append("时间不够了，先不死磕了。")
    parts.append("你把题干打字发我，我分段试试。")
    fallback = "".join(parts)

    db.execute("""
        UPDATE messages SET status='READY', reply_content=?, updated_at=?,
               lease_owner=NULL, expires_at=?, ready_at=?
        WHERE id=? AND status='GENERATING'
    """, (fallback, now, now + 600, now, msg_id))
    db.commit()
    print(f"[lease] 图片 #{msg_id} 到上限，降级回复", flush=True)

def save_worker_note(db, msg_id, note, worker_id=None):
    """worker 存中间思路（供超时降级时用）。"""
    import time
    now = int(time.time())
    # 追加，不覆盖
    row = db.execute("SELECT worker_notes FROM messages WHERE id=?", (msg_id,)).fetchone()
    existing = (row[0] if row and row[0] else "")
    new_notes = (existing + "\n" + note).strip()[:1000]  # 最多 1000 字
    if worker_id:
        db.execute("""
            UPDATE messages SET worker_notes=?, updated_at=?
            WHERE id=? AND lease_owner=?
        """, (new_notes, now, msg_id, worker_id))
    else:
        db.execute("""
            UPDATE messages SET worker_notes=?, updated_at=?
            WHERE id=?
        """, (new_notes, now, msg_id))
    db.commit()
    return db.total_changes > 0

def mark_sent(db, msg_id):
    db.execute(
        "UPDATE messages SET status='SENT', sent_at=? WHERE id=?",
        (int(time.time()), msg_id))
    db.commit()
    # F1 2026-10-11: 记录回复密度
    # F2 2026-10-11: 主动消息记未接话计数
    try:
        r = db.execute("SELECT chat, action FROM messages WHERE id=?", (msg_id,)).fetchone()
        if r and r[0]:
            import reply_density
            reply_density.record_reply(r[0])
            if r[1] == "PROACTIVE":
                import proactive_state
                paused = proactive_state.on_proactive_sent(r[0])
                if paused:
                    print(f"[proactive] {r[0]} 连续未接话，已自动暂停", flush=True)
    except Exception:
        pass

def mark_uncertain(db, msg_id, error=""):
    """发送后无法确认，绝不自动重发"""
    db.execute(
        "UPDATE messages SET status='SEND_UNCERTAIN', error=? WHERE id=?",
        (error, msg_id))
    db.commit()

def mark_dead(db, msg_id, error=""):
    db.execute(
        "UPDATE messages SET status='DEAD', error=? WHERE id=?",
        (error, msg_id))
    db.commit()

def mark_skipped(db, msg_id, reason="", worker_id=None, claim_version=None):
    """
    CONTEXTUAL 经 Muse 判定不需要回复 → SKIPPED 独立终态，防止再次领取。
    2026-10-09 fencing：如传入 worker_id+claim_version，原子校验，0 行=被顶掉。
    """
    if worker_id is not None and claim_version is not None:
        cur = db.execute(
            "UPDATE messages SET status='SKIPPED', error=?, "
            "lease_owner='', lease_until=0, updated_at=? WHERE id=? "
            "AND status='GENERATING' AND lease_owner=? AND claim_version=?",
            (reason, int(time.time()), msg_id, worker_id, claim_version))
        db.commit()
        return cur.rowcount > 0
    db.execute(
        "UPDATE messages SET status='SKIPPED', error=?, "
        "lease_owner='', lease_until=0, updated_at=? WHERE id=?",
        (reason, int(time.time()), msg_id))
    db.commit()
    return True

def get_group_context(db, chat, limit=10):
    """
    取某群最近 limit 条消息（含 Muse 自己发的），供 CONTEXTUAL 判定用。
    返回 [(sender_name, content, is_own, msg_time), ...] 按时间正序。
    注意：wal_reader 目前跳过本号消息不入库，is_own 的行可能缺失；
    切生产前需补（见 TECH_DOC 已知缺口）。
    """
    rows = db.execute("""
        SELECT sender_name, content, sender, msg_time
        FROM messages
        WHERE chat=? AND typeof(msg_time)='integer'
        ORDER BY COALESCE(sort_seq, msg_time * 1000) DESC, id DESC LIMIT ?
    """, (chat, limit)).fetchall()
    SELF = "wxid_YOUR_BOT_ACCOUNT"
    return [(r[0], r[1], r[2] == SELF, r[3]) for r in reversed(rows)]

def get_sender_recent(db, chat, sender, exclude_msg_id, limit=3):
    """
    取某人在该群最近 limit 条消息（排除当前这条），供 EXPLICIT 回复时结合上下文。
    用户要求：没 @ 的那句 + 后面 @ 的那句，要合在一起回答。
    """
    rows = db.execute("""
        SELECT content, msg_time FROM messages
        WHERE chat=? AND sender=? AND id != ? AND typeof(msg_time)='integer'
        ORDER BY COALESCE(sort_seq, msg_time * 1000) DESC, id DESC LIMIT ?
    """, (chat, sender, exclude_msg_id, limit)).fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]

def get_recent_context(db, chat, minutes=5, fallback_limit=10):
    """
    取某群最近 N 分钟内的所有消息（2026-10-06 用户要求：回复和主动搭话都用5分钟全量上下文）。
    返回 [(sender_name, content, msg_time), ...] 按时间正序。
    包含所有人的消息，不限条数，只限时间窗口。
    排序用 sort_seq（微信的真实消息顺序，create_time 可能乱序），
    无 sort_seq 的老行 fallback 到 msg_time*1000。

    2026-10-07 用户优化：如果 5 分钟窗口为空，则 fallback 取最近 fallback_limit 条
    （默认 10 条），避免群闲置时上下文完全空白。

    chat 参数：chat ID（如 YOUR_MAIN_GROUP@chatroom）或群名（自动映射）。
    注意：messages 表用 chat 列存 ID，chat_name 存显示名。
    """
    import time
    # chatroom ID 到 chat 列值的映射（chat 列存的就是 ID 本身）
    # 如果传入的是群名，保持原样让 SQL 去匹配（可能为空，由调用方保证传 ID）
    since = int(time.time()) - minutes * 60
    rows = db.execute("""
        SELECT sender_name, content, msg_time FROM messages
        WHERE chat=? AND typeof(msg_time)='integer' AND msg_time >= ?
        ORDER BY COALESCE(sort_seq, msg_time * 1000), id ASC
    """, (chat, since)).fetchall()
    if rows:
        return [(r[0], r[1], r[2]) for r in rows]
    # fallback：取最近 N 条
    rows = db.execute("""
        SELECT sender_name, content, msg_time FROM messages
        WHERE chat=? AND typeof(msg_time)='integer'
        ORDER BY COALESCE(sort_seq, msg_time * 1000) DESC, id DESC
        LIMIT ?
    """, (chat, fallback_limit)).fetchall()
    # 按时间正序返回
    return [(r[0], r[1], r[2]) for r in reversed(rows)]


def get_recent_n(db, chat, limit=20):
    """取某群最近 N 条消息（2026-10-08 用户要求：上下文 = 5分钟 + 最近20条交叉去重）。
    返回 [(sender_name, content, msg_time), ...] 按时间正序。"""
    rows = db.execute("""
        SELECT sender_name, content, msg_time FROM messages
        WHERE chat=? AND typeof(msg_time)='integer'
        ORDER BY COALESCE(sort_seq, msg_time * 1000) DESC, id DESC
        LIMIT ?
    """, (chat, limit)).fetchall()
    return [(r[0], r[1], r[2]) for r in reversed(rows)]


def get_merged_context(db, chat, minutes=5, limit=20):
    """5分钟上下文 + 最近N条，交叉合并去重（按 msg_time+content 去重）。
    2026-10-08 用户要求。返回 [(sender_name, content, msg_time), ...] 按时间正序。"""
    seen = set()
    merged = []
    for src in (get_recent_context(db, chat, minutes=minutes),
                get_recent_n(db, chat, limit=limit)):
        for sender_name, content, msg_time in src:
            key = (msg_time, content)
            if key not in seen:
                seen.add(key)
                merged.append((sender_name, content, msg_time))
    merged.sort(key=lambda x: (x[2], x[1]))
    return merged


def get_last_muse_reply_target(db, chat):
    """Muse 在该群最后一次回复的对象（sender_name），用于 CONTEXTUAL 上下文。"""
    row = db.execute("""
        SELECT sender_name FROM messages
        WHERE chat=? AND status IN ('SENT','READY')
          AND reply_content != '' AND typeof(msg_time)='integer'
        ORDER BY msg_time DESC LIMIT 1
    """, (chat,)).fetchone()
    return row[0] if row else ""

def get_cursor(db, key, default="0"):
    row = db.execute(
        "SELECT value FROM cursor WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


# ============ 步骤0：静默开关（2026-10-06） ============

# 只有用户大号的指令有效
MUTE_MASTER_WXID = "wxid_YOUR_MAIN_ACCOUNT"
MUTE_KEYWORDS = ["闭嘴", "先别说话", "别说话", "安静", "先别回", "先闭嘴"]
UNMUTE_KEYWORDS = ["恢复", "可以说话了", "开口吧", "可以说了"]
MUTE_DEFAULT_SECONDS = 3600  # 默认静默 60 分钟
READY_EXPIRE_SECONDS = 600   # READY 默认 10 分钟过期

def is_muted(db, now=None):
    """是否处于静默期。返回 (muted: bool, mute_until: int)。"""
    import time
    now = now or int(time.time())
    row = db.execute(
        "SELECT mute_until FROM mute_state WHERE scope='global'").fetchone()
    if not row:
        return False, 0
    until = row[0] or 0
    if until > now:
        return True, until
    return False, 0

def set_mute(db, until, set_by, reason, commit=True):
    """设置静默。"""
    import time
    now = int(time.time())
    db.execute("""INSERT OR REPLACE INTO mute_state
        (scope, mute_until, set_by_wxid, reason, created_at)
        VALUES ('global', ?, ?, ?, ?)""",
        (until, set_by, reason, now))
    if commit:
        db.commit()

def clear_mute(db, commit=True):
    """解除静默。"""
    db.execute("DELETE FROM mute_state WHERE scope='global'")
    if commit:
        db.commit()

def detect_mute_command(sender_wxid, content):
    """规则识别禁言/恢复指令（不靠 LLM）。
    返回 'mute' / 'unmute' / None。只有大号的指令有效。"""
    if sender_wxid != MUTE_MASTER_WXID:
        return None
    text = (content or "").strip()
    # 恢复词优先（避免"恢复"被误判）
    for kw in UNMUTE_KEYWORDS:
        if kw in text:
            return "unmute"
    for kw in MUTE_KEYWORDS:
        if kw in text:
            return "mute"
    return None

def set_cursor(db, key, value):
    db.execute(
        "INSERT OR REPLACE INTO cursor (key, value) VALUES (?, ?)",
        (key, str(value)))
    db.commit()

def set_cursor_nocommit(db, key, value):
    """D方案：不提交版本，供 ingest 事务内调用（入库+游标同事务）。"""
    db.execute(
        "INSERT OR REPLACE INTO cursor (key, value) VALUES (?, ?)",
        (key, str(value)))

if __name__ == "__main__":
    db = get_db()
    print(f"Queue DB ready: {DB_PATH}")
    # 显示统计
    for row in db.execute(
        "SELECT status, COUNT(*) FROM messages GROUP BY status"):
        print(f"  {row[0]}: {row[1]}")
