#!/usr/bin/env python3
"""
微信群每日小游戏 - game_router

设计稿 §2：不要把游戏塞进 should_reply。
所有入站消息：wal_reader → 正常 should_reply 分类 → 同时检查 active game →
game_router 看这条消息是否属于游戏输入。

一条消息可以同时是 should_reply=0 + 游戏输入（如猜词时只发"显卡驱动"）。

返回 None（不是游戏输入）或 dict：
{
  "game_id", "game_type",
  "event_type", "reply_needed", "facts",
  "suppress_original",  # 原消息 should_reply=1/2 时，游戏回复是否替代普通回复
  "event_id", "synth_msg_id",
}
调用方（wal_reader hook）根据 suppress_original 决定是否把原消息标 SKIPPED。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from games import store as gstore  # noqa: E402
from games import get_plugin  # noqa: E402
from games.manager import handle_input, handle_invalid_input  # noqa: E402
from games.config import GAME_SYSTEM_SENDER  # noqa: E402


def _pack_result(game, result, event_id, synth_mid, should_reply):
    return {
        "game_id": game["id"],
        "game_type": game["game_type"],
        "event_type": result.get("event_type"),
        "reply_needed": result.get("reply_needed", False),
        "facts": result.get("facts", {}),
        "suppress_original": bool(result.get("suppress_original"))
                             and should_reply in (1, 2),
        "event_id": event_id,
        "synth_msg_id": synth_mid,
    }


def route_message(db, chat, sender, sender_name, content, msg_id,
                  msg_time, should_reply=0):
    """判断一条入站消息是否为游戏输入；是则处理并落库。"""
    # 自己的合成消息 / OUTBOUND 不参与
    if sender == GAME_SYSTEM_SENDER:
        return None

    # 2026-10-07 每日签到/算命：优先于 /game 命令和游戏输入识别
    #（有进行中游戏时也可用；签到/算命是独立功能，不算游戏输入）
    try:
        from games import daily as _gdaily
        _dkind = _gdaily.parse_daily(content)
        if _dkind:
            return _gdaily.handle_daily(db, chat, sender, sender_name,
                                        _dkind, msg_id, msg_time,
                                        should_reply=should_reply)
    except Exception as e:
        print(f"[game_router] daily error: {e}", flush=True)
        return None

    # 2026-10-07 手动开关：/game 命令优先于一切游戏输入识别
    #（无 active game 时也能用；有 active game 时如 /game stop 不被当 NG 词检查）
    try:
        from games import commands as _gcmd
        if _gcmd.parse_command(content):
            return _gcmd.handle_command(db, chat, sender, sender_name,
                                        content, msg_id, msg_time,
                                        should_reply=should_reply)
    except Exception as e:
        print(f"[game_router] command error: {e}", flush=True)
        return None

    game = gstore.get_active_game(db, chat)
    if not game:
        # 2026-10-07 自然开局："来把猜词"/"玩个数字炸弹"（无进行中游戏时）。
        # 有进行中游戏时不触发，走正常游戏输入识别。
        try:
            from games import commands as _gcmd2
            nat = _gcmd2.parse_natural_start(content)
        except Exception as e:
            print(f"[game_router] natural start error: {e}", flush=True)
            nat = None
        if nat:
            return _gcmd2.handle_command(db, chat, sender, sender_name,
                                         f"/game start {nat}", msg_id,
                                         msg_time, should_reply=should_reply)
        return None
    plugin = get_plugin(game["game_type"])
    if not plugin:
        return None

    msg = {"msg_id": msg_id, "chat": chat, "sender": sender,
           "sender_name": sender_name, "content": content,
           "msg_time": msg_time}
    try:
        if not plugin.accept_message(msg, game["state"]):
            # 2026-10-09 无效输入提示：进行中游戏 + 疑似游戏尝试的短消息
            # + 本来就要考虑回复（should_reply 1/2）→ 回一句无效动作提示
            #（不扣步数、不改状态）。纯聊天（should_reply=0）保持沉默，
            # 避免"哈哈"之类的短聊天也被 bot 搭话。
            if should_reply in (1, 2):
                try:
                    if plugin.looks_like_game_attempt(msg, game["state"]):
                        result, event_id, synth_mid = \
                            handle_invalid_input(db, game, msg)
                        if result is not None:
                            return _pack_result(game, result, event_id,
                                                synth_mid, should_reply)
                except Exception as e:
                    print(f"[game_router] hint error: {e}", flush=True)
            return None
    except Exception as e:
        print(f"[game_router] accept error: {e}", flush=True)
        return None

    try:
        result, event_id, synth_mid = handle_input(db, game, msg)
    except Exception as e:
        print(f"[game_router] handle error: {e}", flush=True)
        return None

    return _pack_result(game, result, event_id, synth_mid, should_reply)
