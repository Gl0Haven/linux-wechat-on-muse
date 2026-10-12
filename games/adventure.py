#!/usr/bin/env python3
"""
游戏三：每日探险（短次版）

每人每局 3 步，走到哪算哪。发方向/动作关键词即行动：
前 后 左 右 开 拿 搜 跑 走 进 退 摸 敲（或 A/B/C）
事件由确定性随机决定，Muse 负责讲成故事。
"""

import re
import time

from .base import Game
from .words import ADVENTURE_LOCATIONS
from .config import POINTS, GAME_SYSTEM_SENDER

_AT_RE = re.compile(r"@\S+")

DURATION_SEC = 20 * 60
MOVES_PER_PLAYER = 3
START_HP = 3
MAX_PLAYERS = 8

ACTION_KEYWORDS = ("前", "后", "左", "右", "上", "下", "开", "拿", "搜",
                   "跑", "走", "进", "退", "摸", "敲", "踢", "爬",
                   "A", "B", "C", "1", "2", "3")

TREASURES = [
    "坏掉的 4090", "甲骨文注册失败证明", "神秘 Cloudflare Token",
    "半块中秋月饼", "生锈的机械键盘", "永不断电的充电宝",
    "写着密码的小纸条", "限量版 Pikachu 玩偶", "过期三年的优惠券",
    "会发光的蘑菇", "刻着'到此一游'的砖头", "没拆封的神秘快递",
]

TRAPS = [
    "机柜门", "香蕉皮", "松动的地板", "突然掉落的吊灯",
    "漏水的水管", "绊脚的网线",
]

NPCS = [
    "流浪猫保安队长", "深夜修 bug 的前辈", "卖烤肠的大爷",
    "迷路的外卖小哥",
]


class AdventureGame(Game):
    game_type = "adventure"
    name = "每日探险"
    duration_sec = DURATION_SEC

    # ---------- 开局 ----------

    def start(self, ctx):
        rng = self.make_rng(ctx["game_id"], "start")
        location = rng.choice(ADVENTURE_LOCATIONS)
        state = {
            "location": location,
            "players": {},   # user_id -> {name, hp, moves_left, treasures[]}
            "move_count": 0,
        }
        facts = {
            "location": location,
            "moves_per_player": MOVES_PER_PLAYER,
            "start_hp": START_HP,
            "duration_min": DURATION_SEC // 60,
        }
        return state, facts

    # ---------- 输入识别 ----------

    def accept_message(self, msg, state):
        if msg["sender"] == GAME_SYSTEM_SENDER:
            return False
        # 2026-10-10 用户要求：所有游戏统一 @bot+内容 才算有效输入
        content = msg.get("content", "")
        if "@" not in content:
            return False
        text = self.normalize(_AT_RE.sub("", content))
        if not text or len(text) > 6:
            return False
        players = state.get("players", {})
        p = players.get(msg["sender"])
        if p and p["moves_left"] <= 0:
            return False
        if not p and len(players) >= MAX_PLAYERS:
            return False
        return any(k in text for k in ACTION_KEYWORDS)

    def invalid_hint_text(self):
        return ("有效动作请 @我：前 后 左 右 上 下 开 拿 搜 跑 走 进 退 摸 敲 踢 爬，"
                "或 A/B/C、1/2/3（6 字以内）")

    # ---------- 处理 ----------

    def handle_message(self, msg, game, ctx):
        from . import store as gstore
        state = game["state"]
        user_id = msg["sender"]
        name = msg.get("sender_name") or user_id
        players = state.setdefault("players", {})

        # 新玩家加入
        if user_id not in players:
            players[user_id] = {"name": name, "hp": START_HP,
                                "moves_left": MOVES_PER_PLAYER,
                                "treasures": []}
            gstore.add_player(ctx["db"], game["id"], user_id, name,
                              commit=False)

        p = players[user_id]
        p["name"] = name  # 刷新昵称
        # 2026-10-10：@bot+动作，剥掉 @ 再识别
        action = self.normalize(_AT_RE.sub("", msg.get("content", "")))
        p["moves_left"] -= 1
        state["move_count"] += 1

        rng = self.make_rng(game["id"], f"move{state['move_count']}")
        roll = rng.random()
        points = []
        facts = {"player": name, "action": action,
                 "location": state["location"],
                 "hp": p["hp"], "moves_left": p["moves_left"]}

        if roll < 0.30:
            # 宝藏
            item = rng.choice(TREASURES)
            p["treasures"].append(item)
            points.append((user_id, name, POINTS["adventure_per_treasure"]))
            facts.update({"event": "TREASURE", "item": item,
                          "treasure_count": len(p["treasures"])})
        elif roll < 0.55:
            # 陷阱
            trap = rng.choice(TRAPS)
            dmg = rng.randint(1, 2)
            p["hp"] = max(0, p["hp"] - dmg)
            facts.update({"event": "TRAP", "trap": trap, "damage": dmg,
                          "hp": p["hp"]})
            if p["hp"] <= 0:
                p["moves_left"] = 0
                facts["down"] = True
        elif roll < 0.70:
            # NPC
            npc = rng.choice(NPCS)
            bonus = rng.choice([0, 1])
            if bonus:
                p["treasures"].append(f"{npc}的谢礼")
                points.append((user_id, name,
                               POINTS["adventure_per_treasure"]))
            facts.update({"event": "NPC", "npc": npc, "bonus": bool(bonus),
                          "treasure_count": len(p["treasures"])})
        elif roll < 0.80:
            # 怪事
            facts.update({"event": "WEIRD",
                          "weird_seed": rng.randint(1, 6)})
        else:
            facts.update({"event": "NOTHING"})

        # 是否整局结束：有人玩过 且 所有人步数用完/倒地
        active_left = any(q["moves_left"] > 0 for q in players.values())
        finish_now = bool(players) and not active_left

        return {
            "event_type": "MOVE",
            "reply_needed": True,
            "facts": facts,
            "suppress_original": True,
            "new_state": state,
            "points": points, "wins": [],
            "finish_now": finish_now,
        }

    # ---------- tick / 结算 ----------

    def tick(self, game, ctx):
        state = game["state"]
        players = state.get("players", {})
        if players and not any(q["moves_left"] > 0
                               for q in players.values()):
            return {"kind": "END",
                    "facts": self._finish_facts(game, "all_done"),
                    "new_state": state, "points": [], "wins": []}
        if ctx["now"] >= game["ends_at"]:
            return {"kind": "END",
                    "facts": self._finish_facts(game, "timeout"),
                    "new_state": state, "points": [], "wins": []}
        return None

    def finish(self, game, reason, ctx):
        return {"facts": self._finish_facts(game, reason),
                "points": [], "wins": []}

    def _finish_facts(self, game, reason):
        state = game["state"]
        players = state.get("players", {})
        ranking = sorted(
            ({"display_name": q["name"],
              "treasures": len(q["treasures"]),
              "hp": q["hp"]} for q in players.values()),
            key=lambda r: (-r["treasures"], -r["hp"]))
        return {
            "reason": reason,
            "location": state.get("location"),
            "total_moves": state.get("move_count", 0),
            "ranking": ranking,
        }
