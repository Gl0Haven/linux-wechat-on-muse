#!/usr/bin/env python3
"""
微信群每日小游戏 - 配置

设计稿: ~/workspace/user/files/upload-2.md（v1）
第一批: guess_word / ng_word / adventure + 简单积分
"""

# 允许开游戏的群（chatroom username）。
# 按用户流程：先在 [测试群] 验证，通过后再加主群。
# 主群: YOUR_MAIN_GROUP@chatroom（[主群]）
# 测试群: YOUR_TEST_GROUP@chatroom（[测试群]）
# 2026-10-07 22:08 用户批准：主群加入白名单
GAMES_ALLOWED_CHATS = {
    "YOUR_TEST_GROUP@chatroom",
    "YOUR_MAIN_GROUP@chatroom",
}

# 每日自动开局候选窗口（北京时间）
DAILY_WINDOW_START = 18  # 18:00
DAILY_WINDOW_END = 23.5  # 23:30

# 群最近 N 分钟有发言才考虑开局
GROUP_ACTIVE_MINUTES = 20

# vibe check 被 SKIP 后的冷却（秒）：1 小时内不再提议
PROPOSE_COOLDOWN_SEC = 3600

# 游戏内 Muse 自动消息连续上限（防刷屏）
MAX_GAME_MSGS_IN_A_ROW = 3

# 游戏选择权重（设计稿 §5；第二批游戏权重保留， renormalize 时自动处理）
GAME_WEIGHTS = {
    "guess_word": 25,   # 每日猜词 / 猜成语
    "ng_word": 20,      # NG 词（已下线，保留权重）
    "adventure": 20,    # 每日探险
    "number_bomb": 20,  # 数字炸弹 / 终极密码
    "trivia": 15,       # 冷知识抢答（第二批）
    "poll": 10,         # 奇怪投票 / Muse审判（第二批）
    "drop": 10,         # 掉落事件 / 抢宝（第二批）
}

# 第一批实际启用的游戏
# 2026-10-07 21:38 用户要求：NG 词公开版开局明牌没意思，先关闭，
# 等以后能私聊发词（隐藏信息）再说。代码保留，只是不启用。
# 2026-10-07 21:47 用户要求：加数字炸弹。
# 2026-10-10 用户要求：加互动故事（测试群试运行，不进每日自动提议）。
ENABLED_GAMES = ["guess_word", "adventure", "number_bomb", "story"]

# bot 自己的微信号（小号）：不参与游戏（它的消息走 OUTBOUND，不进路由）
BOT_WXID = "wxid_YOUR_BOT_ACCOUNT"

# 合成消息的发送者标记（worker 识别用）
GAME_SYSTEM_SENDER = "GAME_SYSTEM"
GAME_SYSTEM_NAME = "游戏系统"

# 积分规则（设计稿 §12）
POINTS = {
    "guess_word_win": 3,
    "trivia_first": 2,
    "ng_word_survive": 3,
    "adventure_per_treasure": 1,
    "drop_win": 1,
    "number_bomb_survive": 1,  # 炸弹模式：没踩中的参与者
    "number_bomb_win": 3,      # 终极密码模式：猜中者
    "story_participate": 2,    # 互动故事：参与者
    "story_twist": 3,          # 互动故事：最佳剧情转折提名者
}
