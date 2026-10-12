#!/usr/bin/env python3
"""步骤3 事实缓存（2026-10-06）。短期缓存，不做长期知识。"""

import os
import re
import sys
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db

TTL_RULES = [
    (r"天气|气温|下雨|晴|阴|雪", 30 * 60),
    (r"比分|比赛|价格|售价|汇率|股价", 10 * 60),
    (r"新闻|头条|热点", 3 * 3600),
    (r"版本|更新|发布|github|项目", 24 * 3600),
]
DEFAULT_TTL = 7 * 24 * 3600

OFFICIAL_DOMAINS = [
    "apple.com", "microsoft.com", "google.com",
    "weather.com", "github.com",
    ".gov.cn", ".edu.cn",
]


def normalize_topic(question):
    text = (question or "").strip().lower()
    # 去 @MUSE / @xxx
    text = re.sub(r"@muse\b", "", text)
    text = re.sub(r"@[\w\u4e00-\u9fff]+", "", text)
    text = text.strip()
    # 去追问前缀：再查一下、帮我查一下、问一下、查一下、说一下、告诉我等
    text = re.sub(
        r"^(再|又|还)?(帮我)?(查一下|查询|问一下|说一下|告诉我|讲一下|搜一下|搜索一下)[，,、:：\s]*",
        "", text)
    # 去标点
    text = re.sub(r"[？?！!。，,、；;：:「」『』\"'（）()\[\]]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 100:
        h = hashlib.sha256(text.encode()).hexdigest()[:8]
        text = text[:100] + "#" + h
    return text


def get_ttl(question):
    q = (question or "").lower()
    for pat, ttl in TTL_RULES:
        if re.search(pat, q):
            return ttl
    return DEFAULT_TTL


def lookup(db, question):
    key = normalize_topic(question)
    now = int(time.time())
    row = db.execute(
        "SELECT id, fact_text, source_url, source_name,"
        " confidence, hit_count FROM fact_cache"
        " WHERE topic_key=? AND status='ACTIVE' AND expires_at > ?"
        " ORDER BY fetched_at DESC LIMIT 1",
        (key, now)).fetchone()
    if not row:
        return False, None
    db.execute(
        "UPDATE fact_cache SET hit_count = hit_count + 1 WHERE id=?",
        (row[0],))
    db.commit()
    return True, {
        "id": row[0],
        "fact_text": row[1],
        "source_url": row[2],
        "source_name": row[3],
        "confidence": row[4],
        "hit_count": row[5] + 1,
    }


def _contains_sensitive(text):
    if not text:
        return False
    if re.search(r"wxid_[a-z0-9]+", text):
        return True
    if "@chatroom" in text:
        return True
    return False


def stage(db, question, fact_text, source_url="", source_name="",
          confidence="medium", job_id=0, commit=True):
    if _contains_sensitive(fact_text):
        print("[fact_cache] 拒绝：内容含敏感信息", flush=True)
        return None
    key = normalize_topic(question)
    now = int(time.time())
    ttl = get_ttl(question)
    # 2026-10-06 修：改用普通 INSERT（不用 OR REPLACE），
    # 允许多个独立来源为同一话题各写一条 STAGED
    cur = db.execute(
        "INSERT INTO fact_cache"
        " (topic_key, fact_text, source_url, source_name,"
        " fetched_at, expires_at, confidence, status, job_id, hit_count)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'STAGED', ?, 0)",
        (key, fact_text[:2000], source_url[:500], source_name[:200],
         now, now + ttl, confidence, job_id))
    if commit:
        db.commit()
    print("[fact_cache] STAGED: %s..." % key[:40], flush=True)
    return cur.lastrowid


def _is_official(url):
    if not url:
        return False
    url_lower = url.lower()
    return any(dom in url_lower for dom in OFFICIAL_DOMAINS)


def _domain(url):
    m = re.search(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def promote(db, cache_id, commit=True):
    row = db.execute(
        "SELECT topic_key, source_url FROM fact_cache"
        " WHERE id=? AND status='STAGED'",
        (cache_id,)).fetchone()
    if not row:
        return False, "记录不存在或非 STAGED"
    topic_key, source_url = row
    if _is_official(source_url):
        db.execute(
            "UPDATE fact_cache SET status='ACTIVE' WHERE id=?",
            (cache_id,))
        if commit:
            db.commit()
        print("[fact_cache] #%d 晋升 ACTIVE（官方来源）" % cache_id,
              flush=True)
        return True, "官方来源"
    siblings = db.execute(
        "SELECT source_url FROM fact_cache"
        " WHERE topic_key=? AND status='STAGED' AND id != ?",
        (topic_key, cache_id)).fetchall()
    domains = set()
    d = _domain(source_url)
    if d:
        domains.add(d)
    for (url,) in siblings:
        d = _domain(url)
        if d:
            domains.add(d)
    if len(domains) >= 2:
        # 2026-10-06 修：该话题所有 STAGED 一起晋升（多个独立来源互相印证）
        db.execute(
            "UPDATE fact_cache SET status='ACTIVE'"
            " WHERE topic_key=? AND status='STAGED'",
            (topic_key,))
        if commit:
            db.commit()
        print("[fact_cache] #%d 晋升 ACTIVE（%d 独立来源，同话题 STAGED 全晋升）"
              % (cache_id, len(domains)), flush=True)
        return True, "%d 独立来源" % len(domains)
    return False, "独立来源不足（%d/2）" % len(domains)


def reject(db, cache_id, commit=True):
    db.execute("UPDATE fact_cache SET status='REJECTED' WHERE id=?",
               (cache_id,))
    if commit:
        db.commit()


def cleanup_expired(db, commit=True):
    now = int(time.time())
    cur = db.execute(
        "DELETE FROM fact_cache WHERE expires_at <= ?", (now,))
    n = cur.rowcount
    if commit:
        db.commit()
    if n:
        print("[fact_cache] 清理过期 %d 条" % n, flush=True)
    return n


def get_stats(db):
    now = int(time.time())
    total = db.execute("SELECT COUNT(*) FROM fact_cache").fetchone()[0]
    active = db.execute(
        "SELECT COUNT(*) FROM fact_cache"
        " WHERE status='ACTIVE' AND expires_at > ?",
        (now,)).fetchone()[0]
    staged = db.execute(
        "SELECT COUNT(*) FROM fact_cache WHERE status='STAGED'").fetchone()[0]
    hits = db.execute("SELECT SUM(hit_count) FROM fact_cache").fetchone()[0]
    return {"total": total, "active": active, "staged": staged,
            "total_hits": hits or 0}
