#!/usr/bin/env python3
"""微信群每日小游戏包。设计稿见 ~/workspace/user/files/upload-2.md"""

from .guess_word import GuessWordGame
from .ng_word import NgWordGame
from .adventure import AdventureGame
from .number_bomb import NumberBombGame
# story 游戏开发中（2026-10-10 其模块曾损坏导致整个 games 包 import 失败，
# 进而每轮 game tick 崩溃）：隔离导入，加载失败只跳过故事游戏，不影响其他游戏。
try:
    from .story import StoryGame
    _STORY_OK = True
except Exception:
    StoryGame = None
    _STORY_OK = False

REGISTRY = {
    GuessWordGame.game_type: GuessWordGame,
    NgWordGame.game_type: NgWordGame,
    AdventureGame.game_type: AdventureGame,
    NumberBombGame.game_type: NumberBombGame,
}
if _STORY_OK:
    REGISTRY[StoryGame.game_type] = StoryGame


def get_plugin(game_type):
    cls = REGISTRY.get(game_type)
    return cls() if cls else None


def available_types():
    return list(REGISTRY.keys())
