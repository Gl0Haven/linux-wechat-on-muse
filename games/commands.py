#!/usr/bin/env python3
"""
小游戏手动开关：/game 命令 + 简要菜单

命令（群里任何人可用，仅限允许开游戏的群）：
  /game                 → 菜单
  /game start 猜词      → 开一局（猜词/探险，也可用英文名）
  /game stop            → 结束当前这局
  /game status          → 当前游戏状态
  /game 榜              → 积分榜

也支持 /游戏 前缀。facts 里 fixed_text 是直接发送的文本，
worker 不改写（可加一句自然过渡语）。
"""

import re
import time

from . import store as gstore
from .config import GAME_SYSTEM_SENDER, ENABLED_GAMES

PREFIXES = ("/game", "/游戏")

GAME_ALIASES = {
    "guess_word": "guess_word", "guess": "guess_word",
    "猜词": "guess_word", "猜成语": "guess_word", "成语": "guess_word",
    "ng_word": "ng_word", "ng": "ng_word",
    "ng词": "ng_word", "NG词": "ng_word",
    "adventure": "adventure",
    "探险": "adventure", "冒险": "adventure",
    "number_bomb": "number_bomb", "bomb": "number_bomb",
    "数字炸弹": "number_bomb", "炸弹": "number_bomb", "终极密码": "number_bomb",
    "story": "story",
    "故事接龙": "story", "互动故事": "story", "讲故事": "story",
}

GAME_NAMES = {
    "guess_word": "猜词",
    "ng_word": "NG 词",
    "adventure": "探险",
    "number_bomb": "数字炸弹",
    "story": "互动故事",
}

MENU_TEXT = (
    "🎮 小游戏菜单\n"
    "1️⃣ 猜词：每天一个词/成语，15 分钟，一人 5 次机会\n"
    "　 /game start 猜词\n"
    "2️⃣ 探险：废弃机房寻宝，每人 3 步\n"
    "　 /game start 探险\n"
    "3️⃣ 数字炸弹：1～100 埋了个雷，猜数字缩范围，踩中输\n"
    "　 /game start 数字炸弹\n"
    "4️⃣ 互动故事：AI 主写分支小说，你投票/自由发言定走向，一局约 40-50 分钟\n"
    "　 /game start 故事接龙\n"
    "—\n"
    "/game status 看状态　/game stop 结束本局　/game 榜 看积分\n"
    "猜词可指定范围，如：/game start 猜词 成语\n"
    "也可以直接说「来把猜词」/「玩个数字炸弹」开局\n"
    "📌 每日签到：发「签到」（连续签到加分）\n"
    "🔮 今日运势：发「算一卦」"
)

# 自然开局说法："来把猜词" / "玩个数字炸弹" / "开一局探险"
# 只认"来/玩/开"+量词+游戏名（GAME_ALIASES），且消息总长 ≤12 字，
# 避免把正常聊天误判。有进行中游戏时不触发（router 里 guard）。
_NATURAL_START_RE = re.compile(r"^(来|玩|开)[一两]?[把个局次盘]?\s*(.+?)\s*(吧|呗|啦|呀)?$")
_AT_PREFIX_RE = re.compile(r"^@\S+\s*")


def parse_natural_start(content):
    """自然开局识别。返回 game_type（如 'guess_word'），或 None。"""
    text = (content or "").strip()
    if not text:
        return None
    # 去掉开头的 @某人（@Muse 来把猜词 也认）
    text = _AT_PREFIX_RE.sub("", text).strip()
    if not text or len(text) > 12:
        return None
    m = _NATURAL_START_RE.match(text)
    if not m:
        return None
    name = m.group(2).strip()
    if not name:
        return None
    return GAME_ALIASES.get(name)


def parse_command(content):
    """解析命令。返回 (cmd, arg) 或 None。
    cmd: menu / start / stop / status / top"""
    from .base import Game
    text = Game.normalize(content or "").strip()
    low = text.lower()
    for p in PREFIXES:
        if low.startswith(p):
            rest = text[len(p):]
            # 前缀后必须是结束或空白（/games 不触发）
            if rest and not rest[0].isspace():
                continue
            rest = rest.strip()
            if not rest:
                return ("menu", "")
            parts = rest.split(None, 1)
            verb = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            if verb in ("start", "开始", "开", "开局"):
                return ("start", arg)
            if verb in ("stop", "结束", "取消", "关", "终"):
                return ("stop", "")
            if verb in ("status", "状态", "查看"):
                return ("status", "")
            if verb in ("top", "榜", "排行", "排行榜", "积分"):
                return ("top", "")
            return ("menu", "")
    return None


def _reply(db, chat, game_id, event_type, facts, msg_id=0, user_id=""):
    """记事件 + 写合成 GAME_REPLY 消息。返回 synthetic msg id。"""
    from .manager import enqueue_game_message
    eid = gstore.add_event(db, game_id, event_type, facts, msg_id=msg_id,
                           user_id=user_id, commit=False)
    mid = enqueue_game_message(db, chat, game_id, eid, "GAME_REPLY",
                               commit=False)
    db.commit()
    return eid, mid


def handle_command(db, chat, sender, sender_name, content, msg_id,
                   msg_time, should_reply=0):
    """处理 /game 命令。返回 router 风格的 result dict，或 None。"""
    from . import get_plugin
    from .manager import start_game, finish_game_flow

    parsed = parse_command(content)
    if not parsed:
        return None
    cmd, arg = parsed
    game = gstore.get_active_game(db, chat)
    gid = game["id"] if game else 0
    suppress = should_reply in (1, 2)
    base = {"game_id": gid, "suppress_original": suppress,
            "reply_needed": True, "event_id": 0, "synth_msg_id": None}

    if cmd == "menu":
        facts = {"cmd": "menu", "fixed_text": MENU_TEXT}
        eid, mid = _reply(db, chat, gid, "CMD_MENU", facts,
                          msg_id=msg_id, user_id=sender)
        return {**base, "event_type": "CMD_MENU", "facts": facts,
                "event_id": eid, "synth_msg_id": mid}

    if cmd == "start":
        parts = arg.strip().split(None, 1)
        want = GAME_ALIASES.get(parts[0]) if parts else None
        category = parts[1].strip() if len(parts) > 1 else ""
        if not want:
            facts = {"cmd": "start", "error": "unknown_game", "arg": arg,
                     "fixed_text": f"没这个游戏：{arg or '（空）'}。\n{MENU_TEXT}"}
            eid, mid = _reply(db, chat, gid, "CMD_START", facts,
                              msg_id=msg_id, user_id=sender)
            return {**base, "event_type": "CMD_START", "facts": facts,
                    "event_id": eid, "synth_msg_id": mid}
        if want not in ENABLED_GAMES:
            facts = {"cmd": "start", "error": "disabled",
                     "fixed_text": "这个游戏暂时关闭了，玩点别的吧。"}
            eid, mid = _reply(db, chat, gid, "CMD_START", facts,
                              msg_id=msg_id, user_id=sender)
            return {**base, "event_type": "CMD_START", "facts": facts,
                    "event_id": eid, "synth_msg_id": mid}
        if game:
            plugin = get_plugin(game["game_type"])
            gname = plugin.name if plugin else game["game_type"]
            facts = {"cmd": "start", "error": "already_active",
                     "fixed_text": f"已经有一局{gname}在玩了，先玩完这局。"}
            eid, mid = _reply(db, chat, gid, "CMD_START", facts,
                              msg_id=msg_id, user_id=sender)
            return {**base, "event_type": "CMD_START", "facts": facts,
                    "event_id": eid, "synth_msg_id": mid}
        cfg = {"category": category} if category else None
        new_gid, new_mid = start_game(db, chat, want, config=cfg)
        if not new_gid:
            detail = str(new_mid)
            # 互动故事测试群 gate：给群友一句人话，而不是 raw error
            if "test_only" in detail:
                fixed = "互动故事目前只在测试群试运行，这个群再等等。"
            else:
                fixed = f"开局失败：{detail}"
            facts = {"cmd": "start", "error": "start_failed",
                     "detail": detail,
                     "fixed_text": fixed}
            eid, mid = _reply(db, chat, 0, "CMD_START", facts,
                              msg_id=msg_id, user_id=sender)
            return {**base, "game_id": 0, "event_type": "CMD_START",
                    "facts": facts, "event_id": eid, "synth_msg_id": mid}
        # start_game 已写好 GAME_START 合成消息，这里不再另写回复；
        # 命令消息链到游戏上（审计用）
        if msg_id:
            gstore.link_message(db, msg_id, new_gid, 0, "GAME_INPUT",
                                commit=True)
        return {**base, "game_id": new_gid, "event_type": "CMD_START",
                "facts": {"cmd": "start", "game_type": want},
                "reply_needed": False, "synth_msg_id": new_mid}

    if cmd == "stop":
        if not game:
            facts = {"cmd": "stop", "error": "no_active",
                     "fixed_text": "现在没有在玩的游戏。"}
            eid, mid = _reply(db, chat, 0, "CMD_STOP", facts,
                              msg_id=msg_id, user_id=sender)
            return {**base, "game_id": 0, "event_type": "CMD_STOP",
                    "facts": facts, "event_id": eid, "synth_msg_id": mid}
        plugin = get_plugin(game["game_type"])
        gname = plugin.name if plugin else game["game_type"]
        finish_game_flow(db, game, "cancelled")
        # finish_game_flow 已写 GAME_END，这里不再另写
        if msg_id:
            gstore.link_message(db, msg_id, game["id"], 0, "GAME_INPUT",
                                commit=True)
        return {**base, "event_type": "CMD_STOP",
                "facts": {"cmd": "stop", "game_name": gname},
                "reply_needed": False}

    if cmd == "status":
        if not game:
            facts = {"cmd": "status",
                     "fixed_text": "现在没有在玩的游戏。发 /game 看菜单。"}
        else:
            plugin = get_plugin(game["game_type"])
            gname = plugin.name if plugin else game["game_type"]
            left = max(0, game["ends_at"] - int(time.time()))
            players = gstore.get_players(db, game["id"])
            names = "、".join(p["display_name"] for p in players) or "还没人"
            facts = {"cmd": "status",
                     "fixed_text": f"🎮 {gname}进行中，剩余约 {left // 60} 分钟。\n"
                                   f"参与：{names}"}
        eid, mid = _reply(db, chat, gid, "CMD_STATUS", facts,
                          msg_id=msg_id, user_id=sender)
        return {**base, "event_type": "CMD_STATUS", "facts": facts,
                "event_id": eid, "synth_msg_id": mid}

    if cmd == "top":
        board = gstore.leaderboard(db, chat, limit=5)
        if not board:
            text = "还没人有积分，先开一局吧。发 /game 看菜单。"
        else:
            lines = [f"{i + 1}. {r['display_name']}　{r['points']} 分"
                     for i, r in enumerate(board)]
            text = "🏆 积分榜\n" + "\n".join(lines)
        facts = {"cmd": "top", "fixed_text": text}
        eid, mid = _reply(db, chat, gid, "CMD_TOP", facts,
                          msg_id=msg_id, user_id=sender)
        return {**base, "event_type": "CMD_TOP", "facts": facts,
                "event_id": eid, "synth_msg_id": mid}

    return None
