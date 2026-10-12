#!/usr/bin/env python3
"""
微信群每日小游戏 - scheduler（每日开局调度）

每天候选窗口 18:00–23:30（北京时间），tick 每 5 分钟检查一次：
- 该群今天还没开过
- 群最近 20 分钟有人说话
- 当前没有 ACTIVE 游戏
- 距上次提议超过冷却时间
- 未静默、微信在线、sendd 存活（best-effort）
满足 → 返回候选，由 worker（Muse）看群气氛做 OPEN_GAME / SKIP 决定。
"""

import datetime
import time

from . import store as gstore
from .config import (
    GAMES_ALLOWED_CHATS, DAILY_WINDOW_START, DAILY_WINDOW_END,
    GROUP_ACTIVE_MINUTES, PROPOSE_COOLDOWN_SEC, ENABLED_GAMES,
    GAME_WEIGHTS,
)


def _beijing_now(now=None):
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.fromtimestamp(now or time.time(), tz=tz)


def today_str(now=None):
    return _beijing_now(now).strftime("%Y-%m-%d")


def in_window(now=None):
    h = _beijing_now(now).hour + _beijing_now(now).minute / 60.0
    return DAILY_WINDOW_START <= h < DAILY_WINDOW_END


def group_active_recently(db, chat_id, minutes=GROUP_ACTIVE_MINUTES,
                          now=None):
    now = now or int(time.time())
    row = db.execute(
        "SELECT MAX(msg_time) FROM messages WHERE chat=?",
        (chat_id,)).fetchone()
    if not row or not row[0]:
        return False
    return (now - row[0]) <= minutes * 60


def system_healthy_for_game(bridge_dir, now=None):
    """开局前健康检查（best-effort）：微信在线 + sendd 存活。
    返回 (ok, reason)。"""
    import json
    import os
    # 登录态
    try:
        with open(os.path.join(bridge_dir, "state", "login_state.json")) as f:
            login = json.load(f).get("state", "")
        if login != "LOGGED_IN":
            return False, f"微信未登录({login})"
    except Exception as e:
        return False, f"读登录态失败: {e}"
    # sendd 存活
    try:
        pid_file = os.path.join(bridge_dir, "state", "sendd_v2.pid")
        with open(pid_file) as f:
            pid = int(f.read().strip().split()[0])
        os.kill(pid, 0)
    except Exception as e:
        return False, f"sendd 不存活: {e}"
    return True, "ok"


def pick_game(rng=None):
    """按权重选一个第一批游戏。"""
    import random
    rng = rng or random
    types = [t for t in ENABLED_GAMES if t in GAME_WEIGHTS]
    weights = [GAME_WEIGHTS[t] for t in types]
    return rng.choices(types, weights=weights, k=1)[0]


def check_daily_propose(db, chat_id, bridge_dir, now=None):
    """是否适合提议开一局。返回 None 或
    {"game_type": ..., "game_name": ..., "reason": ...}。"""
    from . import get_plugin
    now = now or int(time.time())

    if chat_id not in GAMES_ALLOWED_CHATS:
        return None
    if not in_window(now):
        return None

    date = today_str(now)
    ds = gstore.get_daily_state(db, chat_id, date)
    if ds["started"]:
        return None  # 今天已开过
    if gstore.get_active_game(db, chat_id):
        return None  # 已有进行中的游戏
    if now - (ds["last_propose_at"] or 0) < PROPOSE_COOLDOWN_SEC:
        return None  # 冷却中

    # 静默检查
    try:
        import sys
        import os
        sys.path.insert(0, bridge_dir)
        from msgqueue import is_muted
        muted, _ = is_muted(db, now)
        if muted:
            return None
    except Exception:
        pass

    if not group_active_recently(db, chat_id, now=now):
        return None

    ok, reason = system_healthy_for_game(bridge_dir, now)
    if not ok:
        return None

    game_type = pick_game()
    plugin = get_plugin(game_type)
    return {"game_type": game_type,
            "game_name": plugin.name if plugin else game_type,
            "reason": "窗口内、群活跃、无进行中游戏、系统健康"}
