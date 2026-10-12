#!/usr/bin/env python3
"""
Daily news push - RSS content sources (content layer for news_push.py).

Design constraints (user decision 2026-10-10):
- RSS only: free, no quota, stable. **No Brave Search** (quota reserved for
  image Q&A).
- Global vision, mixed Chinese + global: "cn" group + "global" group,
  roughly half each.
- Hot-search feeds (Weibo/X) only via the public RSSHub instance; if they
  fail, drop them (2026-10-10 test: rsshub.app 403 on all routes, Cloudflare
  blocking this machine's egress IP; mirror rssforever 503).

Implementation notes:
- feedparser does parsing only, NOT HTTP. Testing found feedparser's own HTTP
  layer occasionally hangs 200s+ behind this machine's egress proxy (the same
  URL downloads in 1-6s with explicit urllib), so we download bytes with
  urllib (15s timeout, 2MB cap, browser UA) and then feedparser.parse(bytes).
- Each source has independent try/except + timeout; one source failing only
  skips that source.
- ThreadPoolExecutor fetches in parallel; 14 sources usually finish < 15s.

Unified output: [{title, url, source, published, group, summary}],
group is "cn" or "global", published is epoch seconds (0 when missing),
summary is plain-text content excerpt (up to 800 chars, may be empty).
"""

import concurrent.futures
import re
import time
import urllib.request

import feedparser

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
FETCH_TIMEOUT = 15          # per-source HTTP timeout (seconds)
MAX_BYTES = 2_000_000       # per-source read cap 2MB

# Source definitions: (display name, feed URL, group). Toggle = comment/delete.
# All URLs verified working from this machine on 2026-10-10 (see bottom).
NEWS_SOURCES = [
    # ---------- Chinese group ----------
    ("量子位", "https://www.qbitai.com/feed", "cn"),
    ("少数派", "https://sspai.com/feed", "cn"),
    ("V2EX最热", "https://www.v2ex.com/index.xml?tab=hot", "cn"),
    ("IT之家", "https://www.ithome.com/rss/", "cn"),
    ("Solidot", "https://www.solidot.org/index.rss", "cn"),
    ("极客公园", "https://www.geekpark.net/rss", "cn"),
    ("雷锋网", "https://www.leiphone.com/feed", "cn"),
    # ---------- Global group ----------
    ("Hacker News", "https://news.ycombinator.com/rss", "global"),
    ("TechCrunch", "https://techcrunch.com/feed/", "global"),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "global"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "global"),
    ("GitHub Trending",
     "https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml", "global"),
    ("Product Hunt", "https://www.producthunt.com/feed", "global"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/", "global"),
]


def _fetch_bytes(url, timeout=FETCH_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_BYTES)


def _to_epoch(parsed):
    if not parsed:
        return 0
    try:
        return int(time.mktime(parsed))
    except Exception:
        return 0


def fetch_source(name, url, group, timeout=FETCH_TIMEOUT):
    """Fetch one source. Returns (items, error); on failure items is empty."""
    try:
        raw = _fetch_bytes(url, timeout=timeout)
        d = feedparser.parse(raw)
        if d.bozo and not d.entries:
            return [], "parse_failed: %s" % str(d.get("bozo_exception", ""))[:80]
        items = []
        for e in d.entries:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not title or not link:
                continue
            published = _to_epoch(e.get("published_parsed")
                                  or e.get("updated_parsed"))
            # 2026-10-11：保留内容摘要（去 HTML 标签），供精选时做实质内容
            raw_summary = (e.get("summary") or e.get("description") or "")
            summary = re.sub(r"<[^>]+>", "", raw_summary).strip()
            # 压缩空白，截断到 800 字（够 worker 写实质内容）
            summary = re.sub(r"\s+", " ", summary)[:800]
            items.append({"title": title, "url": link, "source": name,
                          "published": published, "group": group,
                          "summary": summary})
        return items, ""
    except Exception as e:  # noqa: BLE001 - one source failing skips it only
        return [], "%s: %s" % (type(e).__name__, str(e)[:80])


def fetch_all_news(sources=None, timeout=FETCH_TIMEOUT, max_workers=16):
    """Fetch all sources in parallel.

    Returns {"items": [...], "ok": [names...], "failed": {name: reason}}.
    """
    sources = sources or NEWS_SOURCES
    items, ok, failed = [], [], {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max_workers, len(sources))) as ex:
        futs = {ex.submit(fetch_source, n, u, g, timeout): (n, u, g)
                for n, u, g in sources}
        for f in concurrent.futures.as_completed(futs):
            n, _, _ = futs[f]
            try:
                got, err = f.result()
            except Exception as e:  # noqa: BLE001 - safety net
                got, err = [], "unexpected: %s" % e
            if err and not got:
                failed[n] = err
            else:
                ok.append(n)
                items.extend(got)
                if err:
                    failed[n] = err + " (partial)"
    return {"items": items, "ok": ok, "failed": failed}


_TITLE_JUNK_RE = re.compile(
    r"[\s\u3000\-_\u2013\u2014\u00b7|"
    r"\uff5c:\uff1a!\uff01?\uff1f.,\u3002\uff0c;\uff1b"
    r"\"'\u201c\u201d\u2018\u2019()\uff08\uff09"
    r"\[\]\u3010\u3011<>\u300a\u300b]+")


def normalize_title(title):
    """Normalize a title for dedup: strip whitespace/punctuation, lowercase."""
    t = (title or "").lower()
    t = _TITLE_JUNK_RE.sub("", t)
    return t


def dedup_news(items):
    """Dedup by normalized title, keeping the first occurrence."""
    seen, out = set(), []
    for it in items:
        key = normalize_title(it.get("title", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# Source selection test log (2026-10-10, this machine's egress-proxy env,
# feedparser 6.0.14). Timings via explicit urllib download.
#
# Chinese group (working):
#   Qubit     https://www.qbitai.com/feed               200, 10 items, ~3.3s
#   SSPai     https://sspai.com/feed                    200, 10 items, ~3.1s
#   V2EX Hot  https://www.v2ex.com/index.xml?tab=hot    200, 50 items, ~5s
#   ITHome    https://www.ithome.com/rss/               200, 60 items, ~3.0s
#   Solidot   https://www.solidot.org/index.rss         200, 20 items, ~1.8s
#   GeekPark  https://www.geekpark.net/rss              200, 30 items, ~5.9s
#   Leiphone  https://www.leiphone.com/feed             200, 20 items, ~3.7s
# Global group (working):
#   Hacker News https://news.ycombinator.com/rss       200, 30 items, ~1.0s
#   TechCrunch  https://techcrunch.com/feed/           200, 20 items, ~0.6s
#   The Verge   https://www.theverge.com/rss/index.xml 200, 10 items, ~1.0s
#   BBC World   https://feeds.bbci.co.uk/news/world/rss.xml
#                                                     200, 38 items, ~6.3s
#   GitHub Trending
#     https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml
#                                                     200, 9 items, ~0.9s
#     (community GitHub-Actions daily build; RSSHub route 403s)
#   Product Hunt https://www.producthunt.com/feed      200, 50 items, ~4.7s
#   MIT Tech Review https://www.technologyreview.com/feed/
#                                                     200, 10 items, ~0.8s
#
# Dropped:
#   Jiqizhixin (jiqizhixin.com/rss) -> 302 to HTML anti-scrape page
#   Xinzhiyuan (aifrontier.com/feed) -> server closed connection
#   SMZDM (feed.smzdm.com/) -> 403 anti-scrape
#   Jiandan (i.jandan.net/feed, jandan.net/feed) -> 301/403 anti-scrape
#   36kr (36kr.com/feed) -> 200 but non-XML body (anti-scrape obfuscation)
#   Huxiu (huxiu.com/rss/) -> 404
#   Reuters public RSS -> 301 to HTML / old feedburner endpoint disconnected
#     (Reuters discontinued public RSS)
#   github-trending-api.now.sh -> 451 (domain dead)
#   RSSHub public instance (rsshub.app) -> 403 on ALL routes (Cloudflare
#     blocking this egress IP): /weibo/search/hot, /zhihu/hot,
#     /bilibili/ranking, /github/trending, /twitter/trending(404 anyway).
#     -> Weibo/Zhihu/Bilibili/X hot-search feeds dropped for now;
#     mirror rssforever.com 503.
# ---------------------------------------------------------------------------
