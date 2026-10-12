#!/usr/bin/env python3
"""
微信群每日小游戏 - Game 基类接口

分工（设计稿 §17）：
- Python（本模块及子类）：答案 / 分数 / 时间 / 玩家 / 状态 / 随机数 / 输赢 / 次数限制
- Muse（worker 渲染）：开场 / 提示 / 吐槽 / 事件描述 / 结果宣布 / 自然语言互动

handle_message / tick / finish 返回的 facts 是"确定性事实"，
worker 只做语言表现，绝不自己编分数、编输赢。
"""

import random
import time


def recent_speakers(db, chat_id, hours=24, limit=8, exclude=()):
    """最近发言过的群友（INBOUND），按最后发言时间倒序。
    exclude: 要排除的 sender 集合（如 bot 自己、游戏系统）。"""
    from .config import GAME_SYSTEM_SENDER, BOT_WXID
    cutoff = int(time.time()) - hours * 3600
    skip = set(exclude) | {GAME_SYSTEM_SENDER, BOT_WXID}
    placeholders = ",".join("?" for _ in skip)
    rows = db.execute(
        f"""SELECT sender, sender_name, MAX(msg_time) AS t
           FROM messages
           WHERE chat=? AND direction='INBOUND' AND msg_time > ?
             AND sender NOT IN ({placeholders})
           GROUP BY sender ORDER BY t DESC LIMIT ?""",
        (chat_id, cutoff, *skip, limit)).fetchall()
    return [{"user_id": r[0], "display_name": r[1] or r[0]} for r in rows]


class Game:
    game_type = "base"
    name = "未命名游戏"
    # tick 检查用的默认时长（秒），子类可覆盖
    duration_sec = 15 * 60

    # ---------- 生命周期 ----------

    def start(self, ctx):
        """开局。ctx: {game_id, chat_id, chat_name, now, rng, ...}
        返回 (state, facts)：state 存 SQLite；facts 给 GAME_START 渲染用。
        必须包含开局所需的全部确定性信息（词、提示、分配等）。"""
        raise NotImplementedError

    def accept_message(self, msg, state):
        """这条消息是不是合法的游戏输入？
        msg: {msg_id, chat, sender, sender_name, content, msg_time}
        纯规则判断，不写库。"""
        raise NotImplementedError

    # ---------- 无效输入提示 ----------
    # 2026-10-09：进行中游戏时，玩家发了疑似游戏尝试但未被 accept 的
    # 短消息（如探险里发"飞飞飞飞"），不再沉默跳过，而是回一句
    # 无效动作提示（不扣步数、不改状态、不加分）。

    def looks_like_game_attempt(self, msg, state):
        """未被 accept_message 接住的消息，是否像一次失败的游戏尝试？
        默认：非空短文本（去全部空白后 ≤6 字）。子类按游戏规则收紧/放宽。
        注意 router 层只对 should_reply=1/2 的消息走提示分支，
        纯聊天（should_reply=0）不会进到这里。"""
        text = self.normalize(msg.get("content", ""))
        # 2026-10-09 补：按去空白后长度算——"飞 飞 飞 飞"（7 字含空格）
        # 这类无效尝试 normalize 只去首尾空白，长度仍超 6，
        # 导致连提示都触发不了（正是用户投诉的原消息）。
        compact = "".join(text.split())
        return bool(compact) and len(compact) <= 6

    def invalid_hint_text(self):
        """本游戏的有效输入说明（给无效提示用）。子类重写。"""
        return "按游戏开场公布的规则输入"

    def invalid_action_hint(self, msg, game, ctx):
        """生成 INVALID_INPUT 结果。返回 None 表示保持沉默（不提示）。"""
        name = msg.get("sender_name") or msg.get("sender", "")
        text = self.normalize(msg.get("content", ""))
        return {
            "event_type": "INVALID_INPUT",
            "reply_needed": True,
            "facts": {
                "player": name,
                "text": text,
                "game_name": self.name,
                "valid_actions": self.invalid_hint_text(),
            },
            "suppress_original": True,
            "new_state": None,
            "points": [],
            "wins": [],
        }

    def handle_message(self, msg, game, ctx):
        """处理一条游戏输入。game 是 store 行（含 state）。
        返回 result dict:
          {
            "event_type": "GUESS" / "HIT" / "MOVE" / ...,
            "reply_needed": True/False,   # 游戏想不想现在说句话
            "facts": {...},               # 确定性事实，供渲染
            "suppress_original": True/False,  # 原消息是 EXPLICIT/2 时是否压住普通回复
            "new_state": {...} 或 None,   # 状态变更（由 manager 落库）
            "points": [(user_id, display_name, pts)],  # 即时加分
            "wins": [(user_id, display_name)],         # 即时记胜场
            "finish_now": False,          # 是否立即结算（如只剩一人）
          }
        """
        raise NotImplementedError

    def tick(self, game, ctx):
        """定时检查（每 5 分钟被 manager 调用一次）。
        返回 None（无事发生）或 {"kind": "TICK"/"END", "facts": {...},
        "points": [...], "wins": [...]}。
        tick 里也可以直接 finish（返回 kind="END"）。"""
        return None

    def finish(self, game, reason, ctx):
        """结算。reason: timeout/finished_early/expired/cancelled
        返回 {"facts": {...}, "points": [...], "wins": [...]}。"""
        return {"facts": {"reason": reason}, "points": [], "wins": []}

    # ---------- 工具 ----------

    @staticmethod
    def make_rng(game_id, salt=""):
        """确定性随机：同一局游戏可复现，VM 重启也不变。"""
        return random.Random(f"{game_id}:{salt}")

    @staticmethod
    def normalize(text):
        """归一化：去空白、全角转半角（只做最常见的）。"""
        if not text:
            return ""
        t = text.strip()
        # 全角转半角（数字字母标点）
        out = []
        for ch in t:
            o = ord(ch)
            if o == 0x3000:
                out.append(" ")
            elif 0xFF01 <= o <= 0xFF5E:
                out.append(chr(o - 0xFEE0))
            else:
                out.append(ch)
        return "".join(out).strip()
