#!/usr/bin/env python3
"""
数字炸弹 / 终极密码

- 系统随机 secret ∈ [1, 100]，Muse 开场公布规则（不公布答案）。
- 玩家只发纯数字（且在当前范围内）才算游戏输入，
  "我觉得是56" 这类不识别，当正常聊天。
- guess < secret → range_min = guess + 1（MISS_LOW）
  guess > secret → range_max = guess - 1（MISS_HIGH）
  guess == secret → BOOM，结束。
- 模式：BOMB（默认，踩中输，其余参与者 +1）/
        TARGET（小概率，猜中赢 +3）。
- 每人 3 秒冷却；10 分钟无人命中则超时结算。
"""

import re
import time

from .base import Game, recent_speakers
from .config import POINTS

DURATION_SEC = 10 * 60
COOLDOWN_SEC = 3
MAX_TURN_PLAYERS = 8
_NUM_RE = re.compile(r"^\d+$")
_AT_RE = re.compile(r"@\S+")


def _current_player(state):
    """本轮该谁猜。无顺序（单人/无发言记录）时返回 None=谁都行。"""
    order = state.get("turn_order") or []
    if not order:
        return None
    return order[state.get("turn_idx", 0) % len(order)]


class NumberBombGame(Game):
    game_type = "number_bomb"
    name = "数字炸弹"
    duration_sec = DURATION_SEC

    # ---------- 生命周期 ----------

    def start(self, ctx):
        db = ctx["db"]
        game_id = ctx["game_id"]
        rng = self.make_rng(game_id, "secret")
        secret = rng.randint(1, 100)
        # 80% 踩中输 / 20% 猜中赢（确定性随机，VM 重启不变）
        mode = "target" if rng.random() < 0.2 else "bomb"
        # 轮流顺序：最近发言过的群友（不含 bot），按最近发言倒序
        speakers = recent_speakers(db, ctx["chat_id"],
                                   limit=MAX_TURN_PLAYERS)
        turn_order = [s["user_id"] for s in speakers]
        turn_names = [s["display_name"] for s in speakers]
        state = {
            "secret": secret,
            "mode": mode,
            "range_min": 1,
            "range_max": 100,
            "guesses": [],          # [{user_id, display_name, guess, ts}]
            "players": {},          # user_id -> display_name
            "last_guess_at": {},    # user_id -> ts（冷却）
            "turn_order": turn_order,
            "turn_names": turn_names,
            "turn_idx": 0,
            "loser": None,
            "winner": None,
        }
        facts = {
            # 注意：开场 facts 不带 secret，Muse 不知道答案
            "mode": mode,
            "mode_desc": ("踩中炸弹的人输，活到最后的人得分"
                          if mode == "bomb" else "第一个猜中的人获胜"),
            "range_min": 1,
            "range_max": 100,
            "duration_min": DURATION_SEC // 60,
            "cooldown_sec": COOLDOWN_SEC,
            "turn_order": turn_names,   # 开场公布顺序
            "first_player": turn_names[0] if turn_names else None,
        }
        return state, facts

    def accept_message(self, msg, state):
        """纯数字、在当前范围内、且轮到该玩家 → 游戏输入。
        没轮到的玩家发的数字不识别（当正常聊天）。
        2026-10-10 用户要求：@bot+内容 才算有效输入。"""
        cur = _current_player(state)
        if cur is not None and msg.get("sender") != cur:
            return False
        content = msg.get("content", "")
        if "@" not in content:
            return False
        text = self.normalize(_AT_RE.sub("", content))
        if not _NUM_RE.match(text):
            return False
        try:
            n = int(text)
        except ValueError:
            return False
        return state["range_min"] <= n <= state["range_max"]

    def looks_like_game_attempt(self, msg, state):
        # 只有轮到的玩家发的短消息才算尝试；
        # 没轮到的玩家发的数字/短文本按原设计当正常聊天。
        if _current_player(state) != msg.get("sender"):
            return False
        text = self.normalize(msg.get("content", ""))
        # 2026-10-09：与基类一致，去全部空白后算长度
        compact = "".join(text.split())
        return bool(compact) and len(compact) <= 6

    def invalid_hint_text(self):
        return "轮到你了，直接发 1~100 的纯数字（别带空格和别的字）"

    def _advance_turn(self, state):
        order = state.get("turn_order") or []
        if order:
            state["turn_idx"] = (state.get("turn_idx", 0) + 1) % len(order)

    def _next_player_name(self, state):
        order = state.get("turn_order") or []
        names = state.get("turn_names") or []
        if not order:
            return None
        idx = (state.get("turn_idx", 0)) % len(order)
        return names[idx] if idx < len(names) else order[idx]

    def handle_message(self, msg, game, ctx):
        state = game["state"]
        user_id = msg["sender"]
        name = msg.get("sender_name") or user_id
        now = ctx.get("now", int(time.time()))
        # 2026-10-10：@bot+数字，剥掉 @ 再转 int
        n = int(self.normalize(_AT_RE.sub("", msg.get("content", ""))))
        secret = state["secret"]
        mode = state["mode"]

        points, wins = [], []

        # 冷却：同一人 3 秒内连猜
        last = state["last_guess_at"].get(user_id, 0)
        if now - last < COOLDOWN_SEC:
            return {
                "event_type": "GUESS",
                "reply_needed": True,
                "facts": {"result": "cooldown", "guess": n, "player": name,
                          "cooldown_sec": COOLDOWN_SEC,
                          "range_min": state["range_min"],
                          "range_max": state["range_max"],
                          "fast_text": f"手速太快了，歇 {COOLDOWN_SEC} 秒再猜。"},
                "suppress_original": True,
                "new_state": state,
                "points": points, "wins": wins, "finish_now": False,
            }

        state["last_guess_at"][user_id] = now
        state["players"][user_id] = name
        state["guesses"].append({"user_id": user_id, "display_name": name,
                                 "guess": n, "ts": now})

        if n < secret:
            state["range_min"] = n + 1
            result = "miss_low"
            miss_word = "没炸"
        elif n > secret:
            state["range_max"] = n - 1
            result = "miss_high"
            miss_word = "还活着"
        else:
            # BOOM
            if mode == "bomb":
                state["loser"] = {"user_id": user_id, "display_name": name}
                survivors = [d for uid, d in state["players"].items()
                             if uid != user_id]
                for uid, d in state["players"].items():
                    if uid != user_id:
                        points.append((uid, d, POINTS["number_bomb_survive"]))
                facts = {"result": "boom", "mode": "bomb", "guess": n,
                         "player": name, "secret": secret,
                         "survivors": survivors,
                         "guess_count": len(state["guesses"])}
            else:
                state["winner"] = {"user_id": user_id, "display_name": name}
                points.append((user_id, name, POINTS["number_bomb_win"]))
                wins.append((user_id, name))
                facts = {"result": "boom", "mode": "target", "guess": n,
                         "player": name, "secret": secret,
                         "guess_count": len(state["guesses"])}
            return {
                "event_type": "BOOM",
                "reply_needed": True,
                "facts": facts,
                "suppress_original": True,
                "new_state": state,
                "points": points, "wins": wins, "finish_now": True,
            }

        # 有效猜测 → 轮到下一个人
        self._advance_turn(state)
        next_name = self._next_player_name(state)
        next_bit = f"下一个：{next_name}。" if next_name else "下一个。"
        fast = (f"{n} {miss_word}！范围缩小：{state['range_min']}～"
                f"{state['range_max']}。{next_bit}")
        return {
            "event_type": "GUESS",
            "reply_needed": True,
            "facts": {"result": result, "guess": n, "player": name,
                      "range_min": state["range_min"],
                      "range_max": state["range_max"],
                      "guess_count": len(state["guesses"]),
                      "next_player": next_name,
                      # 机械回复走模板快车道，worker 直接发送不重新生成
                      "fast_text": fast},
            "suppress_original": True,
            "new_state": state,
            "points": points, "wins": wins, "finish_now": False,
        }

    def tick(self, game, ctx):
        now = ctx.get("now", int(time.time()))
        if now >= game["ends_at"]:
            return {"kind": "END", "facts": {"reason": "timeout"}}
        return None

    def finish(self, game, reason, ctx):
        state = game["state"]
        secret = state["secret"]
        mode = state["mode"]
        players = state["players"]
        points, wins = [], []

        if reason == "timeout":
            # BOMB：没人踩中，参与者都算存活，各 +1
            # TARGET：没人猜中，无人得分
            survivors = list(players.values())
            if mode == "bomb":
                for uid, d in players.items():
                    points.append((uid, d, POINTS["number_bomb_survive"]))
            facts = {"reason": "timeout", "mode": mode, "secret": secret,
                     "survivors": survivors,
                     "guess_count": len(state["guesses"])}
        elif reason == "cancelled":
            facts = {"reason": "cancelled", "mode": mode, "secret": secret,
                     "guess_count": len(state["guesses"])}
        else:
            facts = {"reason": reason, "mode": mode, "secret": secret}
        return {"facts": facts, "points": points, "wins": wins}
