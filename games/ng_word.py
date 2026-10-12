#!/usr/bin/env python3
"""
游戏二：NG 词（公开版）

给每人公开分配一个 NG 词，接下来正常聊天，说到自己的词就算死。
第一版：公开词（bot 不能私聊）；Muse 第一版只当裁判不参战。
时长 20 分钟；提前只剩 1 个活人则直接结算。
"""

from .base import Game, recent_speakers
from .words import NG_WORDS
from .config import POINTS, GAME_SYSTEM_SENDER

DURATION_SEC = 20 * 60
MIN_PLAYERS = 2
MAX_PLAYERS = 6


class NgWordGame(Game):
    game_type = "ng_word"
    name = "NG 词"
    duration_sec = DURATION_SEC

    # ---------- 开局 ----------

    def start(self, ctx):
        db = ctx["db"]
        game_id = ctx["game_id"]
        rng = self.make_rng(game_id, "assign")
        speakers = recent_speakers(db, ctx["chat_id"])
        if len(speakers) < MIN_PLAYERS:
            return None, {"error": "not_enough_players",
                          "players_found": len(speakers)}
        words = rng.sample(NG_WORDS, len(speakers))
        assignments = {}
        for sp, w in zip(speakers, words):
            assignments[sp["user_id"]] = {
                "display_name": sp["display_name"], "word": w}
        state = {
            "assignments": assignments,
            "alive": [sp["user_id"] for sp in speakers],
            "dead": {},  # user_id -> {"word": ..., "said": ...}
        }
        facts = {
            "assignments": [
                {"display_name": a["display_name"], "word": a["word"]}
                for a in assignments.values()
            ],
            "duration_min": DURATION_SEC // 60,
        }
        # 玩家落库
        from . import store as gstore
        for sp in speakers:
            gstore.add_player(db, game_id, sp["user_id"],
                              sp["display_name"], commit=False)
        return state, facts

    # ---------- 输入识别 ----------

    def accept_message(self, msg, state):
        # 只检查还没死的玩家发的所有文字消息
        if msg["sender"] == GAME_SYSTEM_SENDER:
            return False
        return msg["sender"] in state.get("alive", [])

    def looks_like_game_attempt(self, msg, state):
        # 存活玩家的全部文字都已被 accept；没参局/已出局的人发的
        # 是聊天，不是游戏尝试。
        return False

    # ---------- 处理 ----------

    def handle_message(self, msg, game, ctx):
        state = game["state"]
        user_id = msg["sender"]
        me = state["assignments"][user_id]
        word = me["word"]
        text = self.normalize(msg.get("content", ""))

        points, wins = [], []
        # 命中自己的 NG 词 → 出局
        if word and word in text:
            state["alive"].remove(user_id)
            state["dead"][user_id] = {
                "display_name": me["display_name"], "word": word,
                "said": msg.get("content", "")[:30],
            }
            alive_names = [state["assignments"][u]["display_name"]
                           for u in state["alive"]]
            dead_names = [d["display_name"]
                          for d in state["dead"].values()]
            finish_now = len(state["alive"]) <= 1
            return {
                "event_type": "HIT",
                "reply_needed": True,
                "facts": {"result": "hit",
                          "player": me["display_name"],
                          "word": word,
                          "alive": alive_names,
                          "dead": dead_names,
                          "finish_now": finish_now},
                "suppress_original": False,  # 正常聊天继续，别压住
                "new_state": state,
                "points": points, "wins": wins,
                "finish_now": finish_now,
            }
        # 没命中：静默通过，不打扰正常聊天
        return {
            "event_type": "CHECK",
            "reply_needed": False,
            "facts": {},
            "suppress_original": False,
            "new_state": None,
            "points": points, "wins": wins, "finish_now": False,
        }

    # ---------- tick / 结算 ----------

    def tick(self, game, ctx):
        if ctx["now"] >= game["ends_at"]:
            return {"kind": "END",
                    "facts": self._finish_facts(game, "timeout"),
                    "new_state": game["state"],
                    "points": self._survivor_points(game),
                    "wins": []}
        return None

    def finish(self, game, reason, ctx):
        return {"facts": self._finish_facts(game, reason),
                "points": self._survivor_points(game),
                "wins": []}

    def _survivor_points(self, game):
        state = game["state"]
        pts = []
        for u in state.get("alive", []):
            a = state["assignments"][u]
            pts.append((u, a["display_name"], POINTS["ng_word_survive"]))
        return pts

    def _finish_facts(self, game, reason):
        state = game["state"]
        return {
            "reason": reason,
            "survivors": [state["assignments"][u]["display_name"]
                          for u in state.get("alive", [])],
            "dead": [{"display_name": d["display_name"], "word": d["word"]}
                     for d in state.get("dead", {}).values()],
            "assignments": [
                {"display_name": a["display_name"], "word": a["word"]}
                for a in state.get("assignments", {}).values()
            ],
        }
