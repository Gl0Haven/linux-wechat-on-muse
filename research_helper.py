#!/usr/bin/env python3
"""
步骤2 Research helper（2026-10-06）。

分流：REPLY（直接答）/ RESEARCH（需要联网）/ SKIP（不回复）
护栏：并发<1、排队<3、每日<10次、内存≥400MB、未静默
"""

import os
import sys
import time
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db, is_muted

# 配置（文档第 10 节默认值）
RESEARCH_MAX_CONCURRENT = 1
RESEARCH_MAX_QUEUE = 3
RESEARCH_DEADLINE_SECS = 120
RESEARCH_DAILY_LIMIT = 10
RESEARCH_MIN_MEM_MB = 400

# 触发 RESEARCH 的关键词/模式
RESEARCH_TRIGGERS = [
    r"今天|现在|最新|最近",           # 时间敏感
    r"新闻|比赛|比分|价格|天气|股价|汇率",
    r"版本|更新|发布",                # 软件版本
    r"查一下|帮我查|搜索一下",        # 明确要求
    r"github|网站|官网",              # 具体网站/项目
]

# 不触发的模式
RESEARCH_SKIP_PATTERNS = [
    r"^(哈哈|呵呵|嘿嘿|哦|嗯|好|行|可以|谢谢|收到)",
    r"你好|在吗|早安|晚安",           # 打招呼
]

def classify_research_need(content):
    """
    分流：返回 'RESEARCH' / 'REPLY' / 'SKIP'。
    这是规则初筛，最终由主 worker（Muse）结合上下文决定。
    """
    text = (content or "").strip()
    if not text or len(text) < 2:
        return "SKIP"
    # 明确不触发
    for pat in RESEARCH_SKIP_PATTERNS:
        if re.search(pat, text):
            return "REPLY"  # 普通闲聊，直接答
    # 触发研究
    for pat in RESEARCH_TRIGGERS:
        if re.search(pat, text):
            return "RESEARCH"
    return "REPLY"

def check_guardrails(db, now=None):
    """
    护栏检查（5.6）。返回 (ok: bool, reason: str)。
    任一不满足 → 不研究，按已知直接回答。
    """
    now = now or int(time.time())

    # 1. 并发 < 1
    running = db.execute(
        "SELECT COUNT(*) FROM research_jobs WHERE status='RUNNING'"
    ).fetchone()[0]
    if running >= RESEARCH_MAX_CONCURRENT:
        return False, f"并发已满 ({running})"

    # 2. 排队未满
    queued = db.execute(
        "SELECT COUNT(*) FROM research_jobs WHERE status='PENDING'"
    ).fetchone()[0]
    if queued >= RESEARCH_MAX_QUEUE:
        return False, f"排队已满 ({queued})"

    # 3. 每日上限
    day_start = now - (now % 86400)
    today = db.execute(
        "SELECT COUNT(*) FROM research_jobs WHERE created_at >= ?",
        (day_start,)).fetchone()[0]
    if today >= RESEARCH_DAILY_LIMIT:
        return False, f"今日已达上限 ({today}/{RESEARCH_DAILY_LIMIT})"

    # 4. 内存 ≥ 400MB
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mem_kb = int(line.split()[1])
                    mem_mb = mem_kb // 1024
                    if mem_mb < RESEARCH_MIN_MEM_MB:
                        return False, f"内存不足 ({mem_mb}MB < 400MB)"
                    break
    except Exception:
        pass  # 读不到则跳过此项

    # 5. 未静默
    muted, _ = is_muted(db, now)
    if muted:
        return False, "静默期间"

    return True, "ok"

def create_research_job(db, msg_id, chat_id, asker_wxid, question, commit=True):
    """
    创建 research job。返回 job_id 或 None（护栏不通过）。
    同时把原消息标记为 WAITING_RESEARCH（不占用 claim）。
    """
    now = int(time.time())
    ok, reason = check_guardrails(db, now)
    if not ok:
        print(f"[research] guardrail blocked: {reason}", flush=True)
        return None

    # 合并：同一群、同一提问人 60 秒内的相近问题
    existing = db.execute("""
        SELECT id FROM research_jobs
        WHERE chat_id=? AND asker_wxid=?
          AND created_at >= ? AND status IN ('PENDING','RUNNING')
        LIMIT 1
    """, (chat_id, asker_wxid, now - 60)).fetchone()
    if existing:
        print(f"[research] merged into job #{existing[0]}", flush=True)
        return existing[0]

    cur = db.execute("""
        INSERT INTO research_jobs
        (msg_id, chat_id, asker_wxid, question, status,
         created_at, deadline_at)
        VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
    """, (msg_id, chat_id, asker_wxid, question[:500],
          now, now + RESEARCH_DEADLINE_SECS))
    job_id = cur.lastrowid

    # 原消息标记为等待研究
    db.execute("""
        UPDATE messages SET status='WAITING_RESEARCH',
            research_status='REQUESTED',
            research_started_at=?, updated_at=?
        WHERE id=?
    """, (now, now, msg_id))

    if commit:
        db.commit()
    print(f"[research] job #{job_id} created for msg #{msg_id}", flush=True)
    return job_id

def get_pending_job(db):
    """取一个待处理的研究任务（worker 用）。"""
    row = db.execute("""
        SELECT id, msg_id, chat_id, asker_wxid, question, deadline_at
        FROM research_jobs
        WHERE status='PENDING'
        ORDER BY created_at ASC LIMIT 1
    """).fetchone()
    return row

def mark_job_running(db, job_id, commit=True):
    now = int(time.time())
    db.execute("""
        UPDATE research_jobs SET status='RUNNING', started_at=?
        WHERE id=? AND status='PENDING'
    """, (now, job_id))
    if commit:
        db.commit()
    return db.execute("SELECT changes()").fetchone()[0] > 0

def mark_job_done(db, job_id, result_json, brave_calls=0, open_calls=0,
                  commit=True):
    """subagent 完成后自己调用：写结果。"""
    now = int(time.time())
    db.execute("""
        UPDATE research_jobs
        SET status='DONE', result_json=?, finished_at=?,
            brave_calls=?, open_calls=?
        WHERE id=?
    """, (result_json[:8000], now, brave_calls, open_calls, job_id))
    # 更新原消息的研究状态
    db.execute("""
        UPDATE messages SET research_status='DONE',
            research_finished_at=?, updated_at=?
        WHERE id=(SELECT msg_id FROM research_jobs WHERE id=?)
    """, (now, now, job_id))
    if commit:
        db.commit()

def mark_job_timeout(db, job_id, commit=True):
    now = int(time.time())
    db.execute("""
        UPDATE research_jobs SET status='TIMEOUT', finished_at=?,
            error='deadline exceeded'
        WHERE id=? AND status IN ('PENDING','RUNNING')
    """, (now, job_id))
    db.execute("""
        UPDATE messages SET research_status='TIMEOUT', updated_at=?
        WHERE id=(SELECT msg_id FROM research_jobs WHERE id=?)
    """, (now, job_id))
    if commit:
        db.commit()

def check_timeouts(db, commit=True):
    """检查超时的 job，标记 TIMEOUT。返回超时的 job 列表。"""
    now = int(time.time())
    rows = db.execute("""
        SELECT id, msg_id FROM research_jobs
        WHERE status IN ('PENDING','RUNNING') AND deadline_at <= ?
    """, (now,)).fetchall()
    for job_id, _ in rows:
        mark_job_timeout(db, job_id, commit=False)
    if commit and rows:
        db.commit()
    return rows

def reclaim_stuck_jobs(db, stuck_secs=90, max_retries=3, commit=True):
    """回收卡住的 RUNNING job：标记了 RUNNING 但超过 stuck_secs 秒还没 DONE，
    说明 subagent 根本没起来（spawn 失败或 worker 中途退出）。打回 PENDING 重试，
    超过 max_retries 次则标记为 FAILED 不再重试。返回回收的 job 列表。"""
    now = int(time.time())
    rows = db.execute("""
        SELECT id, msg_id, COALESCE(retry_count, 0) FROM research_jobs
        WHERE status='RUNNING' AND started_at <= ?
    """, (now - stuck_secs,)).fetchall()
    reclaimed = []
    for job_id, msg_id, retry_count in rows:
        if retry_count >= max_retries:
            db.execute("""
                UPDATE research_jobs SET status='FAILED',
                    error='stuck too many times', finished_at=?
                WHERE id=?
            """, (now, job_id))
            db.execute("""
                UPDATE messages SET research_status='FAILED', updated_at=?
                WHERE id=?
            """, (now, msg_id))
            print(f"[research] job #{job_id} FAILED after {retry_count} stuck retries", flush=True)
        else:
            db.execute("""
                UPDATE research_jobs SET status='PENDING', started_at=0,
                    retry_count=COALESCE(retry_count,0)+1,
                    deadline_at=?
                WHERE id=?
            """, (now + RESEARCH_DEADLINE_SECS, job_id))
            db.execute("""
                UPDATE messages SET research_status='REQUESTED', updated_at=?
                WHERE id=?
            """, (now, msg_id))
            print(f"[research] job #{job_id} reclaimed from stuck RUNNING (retry {retry_count+1})", flush=True)
        reclaimed.append(job_id)
    if commit and reclaimed:
        db.commit()
    return reclaimed

def get_done_jobs(db):
    """取已完成待处理的 job（worker 用来生成回复）。"""
    return db.execute("""
        SELECT j.id, j.msg_id, j.chat_id, j.asker_wxid, j.question,
               j.result_json, j.status,
               m.sender_name, m.content
        FROM research_jobs j
        JOIN messages m ON m.id = j.msg_id
        WHERE j.status IN ('DONE','TIMEOUT')
          AND m.status = 'WAITING_RESEARCH'
        ORDER BY j.finished_at ASC
    """).fetchall()


def check_cache_first(db, question):
    """
    研究前先查缓存（步骤3）。
    返回 (hit: bool, fact_text: str|None)。
    命中则直接用，不联网。
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fact_cache import lookup
    hit, rec = lookup(db, question)
    if hit:
        print(f"[research] 缓存命中，跳过联网 (hits={rec['hit_count']})",
              flush=True)
        return True, rec["fact_text"]
    return False, None


def save_to_cache(db, question, result_json_str, job_id=0):
    """
    研究完成后存入缓存（STAGED）。
    由 worker 在校验通过后调用。
    """
    import sys, os, json
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fact_cache import stage, promote
    try:
        data = json.loads(result_json_str)
    except Exception:
        return None
    # 取第一个 fact 或 answer
    fact_text = data.get("answer", "")
    sources = data.get("sources", [])
    source_url = sources[0].get("url", "") if sources else ""
    source_name = sources[0].get("title", "") if sources else ""
    confidence = data.get("confidence", "medium")
    cid = stage(db, question, fact_text, source_url, source_name,
                confidence, job_id)
    if cid:
        # 尝试晋升（官方来源或多来源）
        promote(db, cid)
    return cid
