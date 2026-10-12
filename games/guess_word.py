#!/usr/bin/env python3
"""
游戏一：每日猜词 / 猜成语

Muse 每天选一个词/成语，玩家直接发答案（不用 /guess）。
一人最多猜 5 次，15 分钟后公布。
"""

import json
import re
import time

from .base import Game
from .words import GUESS_WORDS
from .config import POINTS

MAX_ATTEMPTS = 5
DURATION_SEC = 15 * 60
HINT_AT = [5 * 60, 10 * 60]  # 第二条、第三条提示的发放时间（相对开局）

_AT_RE = re.compile(r"@\S+")


def _recent_secrets(db, chat_id, game_type, days=7):
    """最近 N 天用过的词，避免重复。"""
    cutoff = int(time.time()) - days * 86400
    rows = db.execute(
        """SELECT content FROM game_events e
           JOIN games g ON g.id = e.game_id
           WHERE g.chat_id=? AND g.game_type=? AND e.event_type='GAME_START'
             AND e.created_at > ?""",
        (chat_id, game_type, cutoff)).fetchall()
    used = set()
    for (c,) in rows:
        try:
            used.add(json.loads(c or "{}").get("secret", ""))
        except Exception:
            pass
    return used


class GuessWordGame(Game):
    game_type = "guess_word"
    name = "每日猜词"
    duration_sec = DURATION_SEC

    # ---------- 开局 ----------

    def start(self, ctx):
        db = ctx["db"]
        game_id = ctx["game_id"]
        rng = self.make_rng(game_id, "secret")
        want_category = (ctx.get("config") or {}).get("category")
        used = _recent_secrets(db, ctx["chat_id"], self.game_type)
        def _cat_match(c):
            # 精确匹配，或 "成语" 匹配 "成语·动物" 这类细分（前缀+·）
            return (not want_category or c == want_category
                    or c.startswith(want_category + "·"))
        pool = [(w, c, h) for (w, c, h) in GUESS_WORDS
                if w not in used and _cat_match(c)]
        if not pool:
            # 退化：本范围内不再避重
            pool = [(w, c, h) for (w, c, h) in GUESS_WORDS
                    if _cat_match(c)]
        if not pool:
            pool = [(w, c, h) for (w, c, h) in GUESS_WORDS]
        secret, category, hints = rng.choice(pool)
        state = {
            "secret": secret,
            "category": category,
            "hints": hints,
            "hint_idx": 1,          # 下一条待发提示的下标（0 已在开场发出）
            "attempts": {},         # user_id -> 次数
            "winner": None,
        }
        facts = {
            "secret": secret,
            "category": category,          # 开局即公布范围
            "answer_len": len(secret),     # 开局即公布字数
            "hint1": hints[0],
            "max_attempts": MAX_ATTEMPTS,
            "duration_min": DURATION_SEC // 60,
        }
        return state, facts

    # ---------- 输入识别 ----------

    # 非猜测信号：问句/请求/闲聊标记，出现则判定为"别的事情"，放行走正常流水线
    # 2026-10-10 用户要求：@bot 消息可能是别的事情，要做好区分
    _NON_GUESS_RE = None

    @classmethod
    def _non_guess_pattern(cls):
        if cls._NON_GUESS_RE is None:
            import re
            # 问号、疑问词、请求词、语气词——成语里基本不会出现
            cls._NON_GUESS_RE = re.compile(
                r"[?？]|吗|呢|怎么|为什么|什么|谁|哪|是否|能不能|可不可以|"
                r"请|帮我|给我|替我|麻烦|谢谢|哈哈|嘿嘿|好的|早安|晚安|"
                r"你觉得|你认为|是不是|对不对|好不好|行不行"
            )
        return cls._NON_GUESS_RE

    def accept_message(self, msg, state):
        if state.get("winner"):
            return False
        content = msg.get("content", "")
        # 2026-10-10 用户要求：只有 @bot + 成语才算猜测，避免把闲聊当成猜测扣次数
        if "@" not in content:
            return False
        # 2026-10-10 worker 反馈：次数用完后，@bot 消息不应再被游戏吞掉
        #（如 "@MUSE 点麦当劳" 是正常 @ 请求，不是猜测），放行走正常回复流水线
        user_id = msg.get("sender")
        attempts = (state.get("attempts") or {})
        if attempts.get(user_id, 0) >= MAX_ATTEMPTS:
            return False
        text = self.normalize(_AT_RE.sub("", content))
        # 2026-10-10 用户要求：@bot 消息可能是别的事情（提问/点餐/闲聊），
        # 含非猜测信号则放行，不当猜测吞掉
        if self._non_guess_pattern().search(text):
            return False
        # 去掉空白后 1~12 个字符才算一次猜（太长的肯定是聊天）
        compact = text.replace(" ", "").replace("\u3000", "")
        return 1 <= len(compact) <= 12

    def invalid_hint_text(self):
        return "猜成语请 @我 再发答案（12 字以内）"

    # ---------- 处理 ----------

    def handle_message(self, msg, game, ctx):
        state = game["state"]
        user_id = msg["sender"]
        name = msg.get("sender_name") or user_id
        guess = self.normalize(_AT_RE.sub("", msg.get("content", "")))
        compact = guess.replace(" ", "")
        attempts = state.setdefault("attempts", {})
        used = attempts.get(user_id, 0)

        points, wins = [], []

        # 次数用完
        if used >= MAX_ATTEMPTS:
            return {
                "event_type": "GUESS",
                "reply_needed": True,
                "facts": {"result": "exhausted", "guess": guess,
                          "player": name, "secret": state["secret"]},
                "suppress_original": True,
                "new_state": state,
                "points": points, "wins": wins, "finish_now": False,
            }

        attempts[user_id] = used + 1
        left = MAX_ATTEMPTS - attempts[user_id]

        # 猜中
        if compact == state["secret"]:
            state["winner"] = {"user_id": user_id, "display_name": name}
            points.append((user_id, name, POINTS["guess_word_win"]))
            wins.append((user_id, name))
            return {
                "event_type": "WIN",
                "reply_needed": True,
                "facts": {"result": "win", "guess": guess, "player": name,
                          "secret": state["secret"],
                          "tries": attempts[user_id]},
                "suppress_original": True,
                "new_state": state,
                "points": points, "wins": wins, "finish_now": True,
            }

        # 猜错：算个确定性"接近度"供 Muse 吐槽用
        closeness = self._closeness(compact, state["secret"])
        return {
            "event_type": "GUESS",
            "reply_needed": True,
            "facts": {"result": "wrong", "guess": guess, "player": name,
                      "secret": state["secret"],
                      "closeness": closeness,  # hot/warm/cold
                      "attempts_left": left,
                      "attempts_used": attempts[user_id]},
            "suppress_original": True,
            "new_state": state,
            "points": points, "wins": wins, "finish_now": False,
        }

    @staticmethod
    def _closeness(guess, secret):
        if not secret:
            return "cold"
        common = len(set(guess) & set(secret))
        ratio = common / max(len(set(secret)), 1)
        if ratio >= 0.6:
            return "hot"
        if ratio >= 0.3:
            return "warm"
        return "cold"

    # ---------- tick ----------

    def tick(self, game, ctx):
        state = game["state"]
        now = ctx["now"]
        elapsed = now - game["started_at"]

        # 发提示
        hint_idx = state.get("hint_idx", 1)
        hints = state.get("hints", [])
        if hint_idx < len(hints) and hint_idx - 1 < len(HINT_AT):
            if elapsed >= HINT_AT[hint_idx - 1]:
                state["hint_idx"] = hint_idx + 1
                return {"kind": "TICK",
                        "facts": {"tick": "hint",
                                  "hint_no": hint_idx + 1,
                                  "hint": hints[hint_idx],
                                  "secret": state["secret"]},
                        "new_state": state,
                        "points": [], "wins": []}
        # 时间到：结算
        if now >= game["ends_at"]:
            return {"kind": "END",
                    "facts": self._finish_facts(game, "timeout"),
                    "new_state": state, "points": [], "wins": []}
        return None

    def finish(self, game, reason, ctx):
        return {"facts": self._finish_facts(game, reason),
                "points": [], "wins": []}

    def _finish_facts(self, game, reason):
        state = game["state"]
        attempts = state.get("attempts", {})
        return {
            "reason": reason,  # timeout/win/expired/cancelled
            "secret": state["secret"],
            "winner": state.get("winner"),
            "total_guesses": sum(attempts.values()),
            "players": len(attempts),
        }
