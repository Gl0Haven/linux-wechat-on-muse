#!/usr/bin/env python3
"""定时调度总入口（2026-10-10，功能 A/C/D 共享）。

由 `~/hooks/scripts/wechat-game.sh`（每 5 分钟的 hook）调用：
    python3 scheduled.py
输出 JSON: {"digest": {...}, "news": ..., "stats": ...}

- digest: 必存在（digest.py 是本仓库代码），直接 import。
- news / stats: 由其他子代理并行开发（news_push / stats 模块），
  用 try/except ImportError 包裹，缺失时记 "not_implemented" 不崩。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def tick(db, now=None):
    """跑一轮定时调度。返回 {"digest":..., "news":..., "stats":...}。"""
    res = {}

    # A：群聊日报（必存在）
    try:
        import digest
        res["digest"] = digest.check_daily_digest(db, now=now)
    except Exception as e:
        res["digest"] = {"error": f"digest 异常: {e}"}

    # C：定时推送（news_push.check_news_push，由功能 C 子代理开发）
    try:
        import news_push
        res["news"] = news_push.check_news_push(db, now=now)
    except ImportError:
        res["news"] = "not_implemented"
    except Exception as e:
        res["news"] = {"error": f"news_push 异常: {e}"}

    # D：群聊周统计（stats.check_weekly_stats，由功能 D 子代理开发）
    try:
        import stats
        res["stats"] = stats.check_weekly_stats(db, now=now)
    except ImportError:
        res["stats"] = "not_implemented"
    except Exception as e:
        res["stats"] = {"error": f"stats 异常: {e}"}

    # B-ext：每周梗策展（meme.check_meme_curation，bot 自主发掘新梗）
    try:
        import meme
        res["meme_curation"] = meme.check_meme_curation(db, now=now)
    except Exception as e:
        res["meme_curation"] = {"error": f"meme_curation 异常: {e}"}

    return res


def main(argv):
    import time
    import msgqueue
    db = msgqueue.get_db()
    try:
        out = tick(db)
    finally:
        db.close()
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
