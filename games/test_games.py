#!/usr/bin/env python3
"""微信群小游戏单元测试。用临时库，不碰真实 queue.db。"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import msgqueue
from games import store as gstore
from games import manager, router, get_plugin
from games import config as gconfig
from games.config import GAME_SYSTEM_SENDER

import chat_order
# enqueue 会写聊天排序状态；指到临时目录，不碰仓库下的 state/
chat_order.STATE_FILE = os.path.join(tempfile.mkdtemp(), "chat_order.json")

TEST_CHAT = "testroom@chatroom"
for _c in (TEST_CHAT, "other@chatroom"):
    gconfig.GAMES_ALLOWED_CHATS.add(_c)  # 同一个 set 对象，各处可见


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # get_db 会建新库；0 字节文件会被拒
    msgqueue.DB_PATH = path
    db = msgqueue.get_db()
    gstore.ensure_schema(db)
    return db, path


def add_msg(db, sender, name, content, chat=TEST_CHAT, msg_time=None):
    mid = msgqueue.enqueue(
        db, chat, "测试群", sender, name, content,
        msg_time or int(time.time()), should_reply=0, commit=True)
    return mid


def test_guess_word_lifecycle():
    db, path = fresh_db()
    # 先放几条消息当群上下文
    add_msg(db, "u1", "Alice", "hello")
    gid, mid = manager.start_game(db, TEST_CHAT, "guess_word")
    assert gid, f"start failed: {mid}"
    game = gstore.get_game(db, gid)
    secret = game["state"]["secret"]
    assert game["status"] == "ACTIVE"
    # 合成 GAME_START 消息存在
    link = gstore.get_link(db, mid)
    assert link and link["kind"] == "GAME_START"

    # 错误猜测（带 @ 才算猜测，2026-10-10 规则收紧）
    msg_id = add_msg(db, "u1", "Alice", "@Muse 不对的答案")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice", "@Muse 不对的答案",
                             msg_id, int(time.time()), should_reply=0)
    assert r and r["reply_needed"] and r["synth_msg_id"], r
    assert r["suppress_original"] is False  # should_reply=0 不压
    st = gstore.get_stats(db, TEST_CHAT, "u1")
    assert st["points"] == 0

    # EXPLICIT 猜中 → 压住普通回复
    msg_id2 = add_msg(db, "u2", "Bob", "@Muse " + secret)
    r2 = router.route_message(db, TEST_CHAT, "u2", "Bob", "@Muse " + secret,
                              msg_id2, int(time.time()), should_reply=1)
    assert r2["facts"]["result"] == "win", r2["facts"]
    assert r2["suppress_original"] is True
    st2 = gstore.get_stats(db, TEST_CHAT, "u2")
    assert st2["points"] == 3 and st2["wins"] == 1, st2
    game = gstore.get_game(db, gid)
    assert game["status"] == "FINISHED", game["status"]
    # GAME_END 合成消息存在
    ends = db.execute(
        "SELECT COUNT(*) FROM game_events WHERE game_id=? AND event_type='GAME_END'",
        (gid,)).fetchone()[0]
    assert ends == 1
    # 每天只开一局
    from games.scheduler import today_str
    ds = gstore.get_daily_state(db, TEST_CHAT, today_str())
    assert ds["started"] == 1 and ds["finished"] == 1
    print("ok: guess_word lifecycle")


def test_guess_word_timeout_and_hints():
    db, path = fresh_db()
    gid, mid = manager.start_game(db, TEST_CHAT, "guess_word")
    game = gstore.get_game(db, gid)
    # 快进到 6 分钟后 → 应发 hint2
    out = get_plugin("guess_word").tick(
        game, {"db": db, "game_id": gid, "chat_id": TEST_CHAT,
               "now": game["started_at"] + 6 * 60})
    assert out and out["kind"] == "TICK" and out["facts"]["tick"] == "hint", out
    # 快进到超时 → END
    db.execute("UPDATE games SET ends_at=? WHERE id=?",
               (int(time.time()) - 1, gid))
    db.commit()
    rep = manager.tick(db)
    assert rep["ticked"] == [(gid, "END")], rep
    game = gstore.get_game(db, gid)
    assert game["status"] == "FINISHED"
    print("ok: guess_word timeout/hints")


def test_guess_word_attempt_limit():
    db, path = fresh_db()
    gid, _ = manager.start_game(db, TEST_CHAT, "guess_word")
    for i in range(5):
        mid = add_msg(db, "u1", "Alice", f"@Muse 错{i}")
        router.route_message(db, TEST_CHAT, "u1", "Alice", f"@Muse 错{i}",
                             mid, int(time.time()), 0)
    # 2026-10-10: 次数用完后，@ 消息不再被游戏吞掉，放行走正常流水线（r 为 None）
    mid = add_msg(db, "u1", "Alice", "@Muse 第6次")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice", "@Muse 第6次",
                             mid, int(time.time()), 0)
    assert r is None, r
    print("ok: guess_word attempt limit")


def test_ng_word():
    from games.config import BOT_WXID
    db, path = fresh_db()
    now = int(time.time())
    for i, (u, n) in enumerate([("u1", "Alice"), ("u2", "Bob"), ("u3", "Carol")]):
        add_msg(db, u, n, f"发言{i}", msg_time=now - i * 60)
    # bot 自己最近也"发言"过（INBOUND 空消息的真实情况）→ 不得被选为玩家
    add_msg(db, BOT_WXID, "Muse", "", msg_time=now - 30)
    gid, mid = manager.start_game(db, TEST_CHAT, "ng_word")
    assert gid, f"start failed: {mid}"
    game = gstore.get_game(db, gid)
    assigns = game["state"]["assignments"]
    assert len(assigns) == 3, assigns
    assert BOT_WXID not in assigns  # bot 不参局
    # u1 说了自己的 NG 词 → 出局
    word1 = assigns["u1"]["word"]
    mid = add_msg(db, "u1", "Alice", f"今天{word1}天气不错")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice",
                             f"今天{word1}天气不错",
                             mid, int(time.time()), should_reply=0)
    assert r and r["facts"]["result"] == "hit", r
    assert r["suppress_original"] is False  # NG 词不压正常聊天
    # u2 正常聊天 → 静默通过
    mid2 = add_msg(db, "u2", "Bob", "今天天气真好")
    r2 = router.route_message(db, TEST_CHAT, "u2", "Bob", "今天天气真好",
                              mid2, int(time.time()), 0)
    assert r2 and r2["reply_needed"] is False
    assert r2["synth_msg_id"] is None
    # u2 也说 NG 词 → 只剩 u3，提前结算
    word2 = assigns["u2"]["word"]
    mid3 = add_msg(db, "u2", "Bob", f"我{word2}觉得不行")
    r3 = router.route_message(db, TEST_CHAT, "u2", "Bob",
                              f"我{word2}觉得不行",
                              mid3, int(time.time()), 0)
    assert r3["facts"]["finish_now"] is True
    game = gstore.get_game(db, gid)
    assert game["status"] == "FINISHED"
    st3 = gstore.get_stats(db, TEST_CHAT, "u3")
    assert st3["points"] == 3, st3  # 幸存者 +3
    print("ok: ng_word")


def test_adventure():
    db, path = fresh_db()
    gid, mid = manager.start_game(db, TEST_CHAT, "adventure")
    assert gid
    # 走 3 步（2026-10-10 起需 @）
    for i, act in enumerate(["前", "左", "开"]):
        txt = "@Muse " + act
        m = add_msg(db, "u1", "Alice", txt)
        r = router.route_message(db, TEST_CHAT, "u1", "Alice", txt,
                                 m, int(time.time()), 0)
        assert r and r["reply_needed"], (i, r)
        assert r["suppress_original"] is False  # should_reply=0
    game = gstore.get_game(db, gid)
    # 3 步用完 → 自动结算
    assert game["status"] == "FINISHED", game["status"]
    p = game["state"]["players"]["u1"]
    assert p["moves_left"] == 0
    # 太长的聊天不被当成行动
    gid2, _ = manager.start_game(db, TEST_CHAT, "adventure")
    m = add_msg(db, "u2", "Bob", "今天晚上一起去吃火锅吧")
    r = router.route_message(db, TEST_CHAT, "u2", "Bob",
                             "今天晚上一起去吃火锅吧",
                             m, int(time.time()), 0)
    assert r is None  # 普通聊天不误识别
    print("ok: adventure")


def test_persistence_across_restart():
    """模拟 VM 重置：关库重开，游戏状态不丢；过期的被 EXPIRED。"""
    db, path = fresh_db()
    gid, _ = manager.start_game(db, TEST_CHAT, "guess_word")
    game = gstore.get_game(db, gid)
    secret = game["state"]["secret"]
    db.close()
    # "重启"
    db2 = msgqueue.get_db()
    gstore.ensure_schema(db2)
    game2 = gstore.get_active_game(db2, TEST_CHAT)
    assert game2 and game2["state"]["secret"] == secret
    # 过期一局（超过 30 分钟宽限期 → EXPIRED 回收）
    gid3, _ = manager.start_game(db2, "other@chatroom", "adventure")
    db2.execute("UPDATE games SET ends_at=? WHERE id=?",
                (int(time.time()) - 3600, gid3))
    db2.commit()
    rep = manager.tick(db2)
    assert gid3 in rep["expired"], rep
    g3 = gstore.get_game(db2, gid3)
    assert g3["status"] == "EXPIRED"  # 无玩家参与 → 静默过期
    print("ok: persistence")


def test_render_package():
    db, path = fresh_db()
    gid, mid = manager.start_game(db, TEST_CHAT, "guess_word")
    render = gstore.get_render(db, mid)
    assert render["link"]["kind"] == "GAME_START"
    assert render["event"]["facts"]["secret"]
    assert render["event"]["facts"]["category"]  # 开局必须带范围
    assert render["event"]["facts"]["answer_len"] == len(
        render["event"]["facts"]["secret"])
    assert render["game"]["game_type"] == "guess_word"
    print("ok: render package")


def test_no_active_game_noop():
    db, path = fresh_db()
    add_msg(db, "u1", "Alice", "hello world")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice", "hello world",
                             1, int(time.time()), 0)
    assert r is None
    # 非允许群开局被拒
    gid, err = manager.start_game(db, "stranger@chatroom", "guess_word")
    assert gid is None
    print("ok: noop guards")


def test_commands():
    from games import commands as gcmd
    # 解析
    assert gcmd.parse_command("/game") == ("menu", "")
    assert gcmd.parse_command("/game start 猜词") == ("start", "猜词")
    assert gcmd.parse_command("/游戏 榜") == ("top", "")
    assert gcmd.parse_command("  /GAME STOP  ") == ("stop", "")
    assert gcmd.parse_command("hello") is None
    assert gcmd.parse_command("/games") is None
    assert gcmd.parse_command("/游戏人生") is None

    db, path = fresh_db()
    now = int(time.time())

    # 菜单
    m = add_msg(db, "u1", "Alice", "/game")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice", "/game",
                             m, now, should_reply=0)
    assert r and r["event_type"] == "CMD_MENU", r
    assert "小游戏菜单" in r["facts"]["fixed_text"]
    assert r["synth_msg_id"]
    assert r["suppress_original"] is False

    # @ 下的命令压住普通回复
    m = add_msg(db, "u1", "Alice", "/game")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice", "/game",
                             m, now, should_reply=1)
    assert r["suppress_original"] is True

    # 开局
    m = add_msg(db, "u1", "Alice", "/game start 猜词")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice",
                             "/game start 猜词", m, now, 0)
    assert r and r["event_type"] == "CMD_START", r
    assert r["game_id"] and r["synth_msg_id"]
    game = gstore.get_active_game(db, TEST_CHAT)
    assert game and game["game_type"] == "guess_word"

    # 对局中再开 → 拒绝
    m = add_msg(db, "u2", "Bob", "/game start 探险")
    r = router.route_message(db, TEST_CHAT, "u2", "Bob",
                             "/game start 探险", m, now, 0)
    assert r["facts"]["error"] == "already_active", r["facts"]

    # 未知游戏名
    m = add_msg(db, "u2", "Bob", "/game start 狼人杀")
    r = router.route_message(db, TEST_CHAT, "u2", "Bob",
                             "/game start 狼人杀", m, now, 0)
    assert r["facts"]["error"] == "unknown_game"

    # 状态
    m = add_msg(db, "u2", "Bob", "/game status")
    r = router.route_message(db, TEST_CHAT, "u2", "Bob",
                             "/game status", m, now, 0)
    assert "进行中" in r["facts"]["fixed_text"]

    # 结束
    m = add_msg(db, "u1", "Alice", "/game stop")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice",
                             "/game stop", m, now, 0)
    assert r["event_type"] == "CMD_STOP"
    assert gstore.get_active_game(db, TEST_CHAT) is None

    # 无对局时 stop
    m = add_msg(db, "u1", "Alice", "/game stop")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice",
                             "/game stop", m, now, 0)
    assert r["facts"]["error"] == "no_active"

    # 榜（无积分时）
    m = add_msg(db, "u1", "Alice", "/game 榜")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice",
                             "/game 榜", m, now, 0)
    assert "还没人有积分" in r["facts"]["fixed_text"]

    # 已关闭的游戏（2026-10-07 21:38 NG 词下线）
    m = add_msg(db, "u2", "Bob", "/game start NG词")
    r = router.route_message(db, TEST_CHAT, "u2", "Bob",
                             "/game start NG词", m, now, 0)
    assert r["facts"]["error"] == "disabled"
    assert r["facts"]["fixed_text"]
    assert gstore.get_active_game(db, TEST_CHAT) is None
    print("ok: commands")


def test_natural_start():
    """自然开局："来把猜词"/"玩个数字炸弹" 等（无进行中游戏时）。"""
    from games import commands as gcmd

    # 解析
    assert gcmd.parse_natural_start("来把猜词") == "guess_word"
    assert gcmd.parse_natural_start("来一局猜词") == "guess_word"
    assert gcmd.parse_natural_start("玩个数字炸弹") == "number_bomb"
    assert gcmd.parse_natural_start("来把炸弹") == "number_bomb"
    assert gcmd.parse_natural_start("开一局探险") == "adventure"
    assert gcmd.parse_natural_start("来把成语") == "guess_word"
    assert gcmd.parse_natural_start("@Muse 来把猜词") == "guess_word"
    # 非开局意图
    assert gcmd.parse_natural_start("来吧") is None
    assert gcmd.parse_natural_start("玩游戏") is None
    assert gcmd.parse_natural_start("开心") is None
    assert gcmd.parse_natural_start("来一局") is None
    assert gcmd.parse_natural_start("今天天气真好") is None
    assert gcmd.parse_natural_start("开一下箱子") is None
    assert gcmd.parse_natural_start("来把大的") is None
    assert gcmd.parse_natural_start("这是一段很长很长的消息肯定不是开局口令啊") is None

    db, path = fresh_db()
    now = int(time.time())

    # 无进行中游戏 → 自然开局
    m = add_msg(db, "u1", "Alice", "来把猜词")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice",
                             "来把猜词", m, now, 0)
    assert r and r["event_type"] == "CMD_START", r
    assert r["facts"].get("game_type") == "guess_word", r["facts"]
    assert gstore.get_active_game(db, TEST_CHAT)["game_type"] == "guess_word"
    # 开场 GAME_START 事件的 facts 含范围（worker 渲染用）
    ev = db.execute(
        "SELECT content FROM game_events WHERE game_id=? AND event_type='GAME_START'",
        (r["game_id"],)).fetchone()
    import json as _json
    assert _json.loads(ev[0]).get("category"), ev[0]

    # 有进行中游戏 → 不再触发开局；"来把猜词"无@不算猜测（2026-10-10 规则），直接忽略
    m = add_msg(db, "u2", "Bob", "来把猜词")
    r = router.route_message(db, TEST_CHAT, "u2", "Bob",
                             "来把猜词", m, now, 0)
    assert r is None, r

    # 已关闭的游戏：自然说法也给 disabled
    db2, _ = fresh_db()
    m = add_msg(db2, "u1", "Alice", "来把NG词")
    r = router.route_message(db2, TEST_CHAT, "u1", "Alice",
                             "来把NG词", m, now, 0)
    assert r and r["facts"].get("error") == "disabled", r

    print("ok: natural start")


def test_daily():
    """每日签到 + 算命。"""
    from games import daily as gdaily

    # 解析
    assert gdaily.parse_daily("签到") == "checkin"
    assert gdaily.parse_daily("/签到") == "checkin"
    assert gdaily.parse_daily("算一卦") == "fortune"
    assert gdaily.parse_daily("今日运势") == "fortune"
    assert gdaily.parse_daily("算命") == "fortune"
    assert gdaily.parse_daily("帮我算一卦") == "fortune"
    assert gdaily.parse_daily("@Muse 签到") == "checkin"
    assert gdaily.parse_daily("hello") is None
    assert gdaily.parse_daily("签到打卡") is None

    db, path = fresh_db()
    now = int(time.time())

    # 签到：首次
    m = add_msg(db, "u1", "Alice", "签到")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice",
                             "签到", m, now, 0)
    assert r and r["event_type"] == "DAILY_CHECKIN", r
    assert r["facts"]["streak"] == 1
    assert r["facts"]["points_today"] == 1
    assert "签到成功" in r["facts"]["fixed_text"]
    assert gstore.get_stats(db, TEST_CHAT, "u1")["points"] == 1

    # 同一天再签：幂等
    m = add_msg(db, "u1", "Alice", "签到")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice",
                             "签到", m, now, 0)
    assert r["facts"]["already"] is True
    assert gstore.get_stats(db, TEST_CHAT, "u1")["points"] == 1

    # 连续签到：streak+1（伪造昨天的记录）
    import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    yest = (_dt.datetime.now(_ZI("Asia/Shanghai")) -
            _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    db.execute("DELETE FROM daily_checkin WHERE user_id='u2'")
    db.execute("INSERT INTO daily_checkin (chat_id, user_id, date, streak,"
               " points, created_at) VALUES (?,?,?,?,?,?)",
               (TEST_CHAT, "u2", yest, 3, 1, now))
    db.commit()
    m = add_msg(db, "u2", "Bob", "签到")
    r = router.route_message(db, TEST_CHAT, "u2", "Bob",
                             "签到", m, now, 0)
    assert r["facts"]["streak"] == 4, r["facts"]

    # 算命：同一人同一天确定性一致
    for u, n in [("u1", "Alice"), ("u2", "Bob"), ("u3", " amp".strip())]:
        add_msg(db, u, n, "今天好累啊", msg_time=now - 100)
    f1 = gdaily.get_fortune(db, TEST_CHAT, "u1", "Alice")
    f2 = gdaily.get_fortune(db, TEST_CHAT, "u1", "Alice")
    assert f1 == f2, "同一人同一天运势必须一致"
    assert f1["player"] == "Alice"
    assert 1 <= f1["overall"] <= 5
    assert set(f1["scores"]) == {"事业", "财运", "桃花", "健康"}
    assert len(f1["yi"]) == 2 and len(f1["ji"]) == 2
    assert f1["recent_msgs"], "应带上本人近期发言"

    # 不同人很可能不同（种子不同）
    f3 = gdaily.get_fortune(db, TEST_CHAT, "u2", "Bob")
    assert f3 != f1

    # router 走算命
    m = add_msg(db, "u1", "Alice", "算一卦")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice",
                             "算一卦", m, now, 0)
    assert r and r["event_type"] == "DAILY_FORTUNE", r
    assert r["facts"]["player"] == "Alice"
    assert r["synth_msg_id"]

    # 有进行中游戏时，签到/算命也不被当游戏输入
    gid, _ = manager.start_game(db, TEST_CHAT, "guess_word")
    assert gid
    m = add_msg(db, "u3", "Carol", "签到")
    r = router.route_message(db, TEST_CHAT, "u3", "Carol",
                             "签到", m, now, 0)
    assert r and r["event_type"] == "DAILY_CHECKIN", r

    print("ok: daily")


def test_command_priority_over_ng():
    """NG 词对局中，/game 命令不被当成 NG 输入误杀。"""
    db, path = fresh_db()
    now = int(time.time())
    for i, (u, n) in enumerate([("u1", "Alice"), ("u2", "Bob")]):
        add_msg(db, u, n, f"发言{i}", msg_time=now - i * 60)
    gid, _ = manager.start_game(db, TEST_CHAT, "ng_word")
    assert gid
    game = gstore.get_game(db, gid)
    word1 = game["state"]["assignments"]["u1"]["word"]
    # u1 发 /game status，其中若含其 NG 词也不该出局
    cmd = f"/game status"
    m = add_msg(db, "u1", "Alice", cmd)
    r = router.route_message(db, TEST_CHAT, "u1", "Alice", cmd,
                             m, now, 0)
    assert r and r["event_type"] == "CMD_STATUS", r
    game = gstore.get_game(db, gid)
    assert "u1" in game["state"]["alive"], "命令不应触发 NG 判定"
    print("ok: command priority")


def test_number_bomb():
    from games.number_bomb import NumberBombGame
    plugin = NumberBombGame()
    db, path = fresh_db()
    now = int(time.time())
    # 先放几个发言人，用于排轮流顺序
    for i, (u, n) in enumerate([("u1", "Alice"), ("u2", "Bob"), ("u3", "Carol")]):
        add_msg(db, u, n, f"发言{i}", msg_time=now - i * 60)

    def _guess(gid, uid, name, text):
        game = gstore.get_game(db, gid)
        m = add_msg(db, uid, name, text)
        msg = {"msg_id": m, "chat": TEST_CHAT, "sender": uid,
               "sender_name": name, "content": text,
               "msg_time": int(time.time())}
        return manager.handle_input(db, game, msg)

    def _reset_cd(gid, uid="u1"):
        st = gstore.get_game(db, gid)["state"]
        st["last_guess_at"][uid] = 0
        gstore.save_game_state(db, gid, st)

    # 开局
    gid, mid = manager.start_game(db, TEST_CHAT, "number_bomb")
    assert gid, f"start failed: {mid}"
    st = gstore.get_game(db, gid)["state"]
    secret = st["secret"]
    assert 1 <= secret <= 100
    assert st["mode"] in ("bomb", "target")
    assert (st["range_min"], st["range_max"]) == (1, 100)
    # 轮流顺序：按最近发言倒序（u1 最新）
    assert st["turn_order"] == ["u1", "u2", "u3"], st["turn_order"]
    assert st["turn_names"] == ["Alice", "Bob", "Carol"]
    # 开场 facts 不带答案（Muse 不知道 secret），带顺序
    ev = db.execute(
        "SELECT content FROM game_events WHERE game_id=? "
        "AND event_type='GAME_START'", (gid,)).fetchone()
    sfacts = json.loads(ev[0])
    assert "secret" not in sfacts
    assert sfacts["turn_order"] == ["Alice", "Bob", "Carol"]
    assert sfacts["first_player"] == "Alice"

    # 输入识别：轮到 u1，@+纯数字且在范围内（2026-10-10 起需 @）
    g0 = 50 if secret != 50 else 49
    txt0 = "@Muse " + str(g0)
    m = add_msg(db, "u1", "Alice", txt0)
    r = router.route_message(db, TEST_CHAT, "u1", "Alice", txt0,
                             m, now, 1)
    assert r and r["reply_needed"] is True
    assert r["suppress_original"] is True  # EXPLICIT 时游戏回复压住普通回复
    # 没轮到 u3：发的数字不识别
    m = add_msg(db, "u3", "Carol", "60")
    r = router.route_message(db, TEST_CHAT, "u3", "Carol", "60", m, now, 1)
    assert r is None
    # "我觉得是50" 不识别（route_message 返回 None，不进游戏）
    m = add_msg(db, "u1", "Alice", "我觉得是50")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice", "我觉得是50",
                             m, now, 0)
    assert r is None
    # 超出范围不识别
    m = add_msg(db, "u1", "Alice", "150")
    r = router.route_message(db, TEST_CHAT, "u1", "Alice", "150", m, now, 0)
    assert r is None

    # 范围已按第一猜收缩，且轮到 u2
    st = gstore.get_game(db, gid)["state"]
    if g0 < secret:
        assert st["range_min"] == g0 + 1, st
    else:
        assert st["range_max"] == g0 - 1, st
    assert st["turn_idx"] == 1  # 轮到 u2
    # 第一猜的 facts 带模板快车道文本 + 下一位
    ev1 = db.execute(
        "SELECT content FROM game_events WHERE game_id=? "
        "AND event_type='GUESS' ORDER BY id LIMIT 1", (gid,)).fetchone()
    f1 = json.loads(ev1[0])
    assert f1["fast_text"] and str(g0) in f1["fast_text"], f1
    assert f1["next_player"] == "Bob", f1

    # 冷却：把轮次拨回 u1，3 秒内连猜
    st = gstore.get_game(db, gid)["state"]
    st["turn_idx"] = 0
    st["last_guess_at"]["u1"] = int(time.time())
    gstore.save_game_state(db, gid, st)
    res, _, _ = _guess(gid, "u1", "Alice", str(g0))
    assert res["facts"]["result"] == "cooldown", res["facts"]
    assert "fast_text" in res["facts"]  # 模板快车道
    _reset_cd(gid, "u1")

    # 强制 bomb 模式，轮到 u2，踩雷
    st = gstore.get_game(db, gid)["state"]
    st["mode"] = "bomb"
    st["turn_idx"] = 1  # u2
    st["last_guess_at"] = {}
    gstore.save_game_state(db, gid, st)
    res, _, _ = _guess(gid, "u2", "Bob", str(secret))
    assert res["facts"]["result"] == "boom"
    assert res["facts"]["mode"] == "bomb"
    assert res["facts"]["secret"] == secret
    assert res["finish_now"] is True
    assert gstore.get_game(db, gid)["status"] == "FINISHED"
    # u1 参与过，存活 +1；u2 踩雷 0 分
    pts = {r[0]: r[1] for r in db.execute(
        "SELECT user_id, points FROM player_stats")}
    assert pts.get("u1", 0) == 1, pts
    assert pts.get("u2", 0) == 0, pts

    # target 模式：猜中赢 +3（把顺序拨给 u3）
    gid2, _ = manager.start_game(db, TEST_CHAT, "number_bomb")
    st2 = gstore.get_game(db, gid2)["state"]
    st2["mode"] = "target"
    st2["turn_order"] = ["u3"]
    st2["turn_names"] = ["Carol"]
    st2["turn_idx"] = 0
    gstore.save_game_state(db, gid2, st2)
    res, _, _ = _guess(gid2, "u3", "Carol", str(st2["secret"]))
    assert res["facts"]["result"] == "boom"
    assert res["facts"]["mode"] == "target"
    pts = {r[0]: r[1] for r in db.execute(
        "SELECT user_id, points FROM player_stats")}
    assert pts.get("u3", 0) == 3, pts
    wins = {r[0]: r[1] for r in db.execute(
        "SELECT user_id, wins FROM player_stats")}
    assert wins.get("u3", 0) == 1, wins

    # 超时：bomb 模式参与者都存活 +1
    gid3, _ = manager.start_game(db, TEST_CHAT, "number_bomb")
    st3 = gstore.get_game(db, gid3)["state"]
    st3["mode"] = "bomb"
    gstore.save_game_state(db, gid3, st3)
    _guess(gid3, "u1", "Alice", "30" if st3["secret"] != 30 else "31")
    db.execute("UPDATE games SET ends_at=? WHERE id=?",
               (int(time.time()) - 1, gid3))
    db.commit()
    rep = manager.tick(db)
    assert (gid3, "END") in rep["ticked"], rep
    g3 = gstore.get_game(db, gid3)
    assert g3["status"] == "FINISHED"
    ev = db.execute(
        "SELECT content FROM game_events WHERE game_id=? "
        "AND event_type='GAME_END'", (gid3,)).fetchone()
    facts = json.loads(ev[0])
    assert facts["reason"] == "timeout" and facts["secret"] == st3["secret"]
    print("ok: number_bomb")


def test_game_priority_queue():
    """游戏消息在 claim（回复）和 sendd（发送）都插队。"""
    from msgqueue import claim_next_reply, claim_next_ready, mark_ready
    db, path = fresh_db()
    now = int(time.time())
    # 一条更早的普通 EXPLICIT
    m1 = add_msg(db, "u1", "Alice", "@Muse 在吗", msg_time=now - 100)
    db.execute("UPDATE messages SET should_reply=1 WHERE id=?", (m1,))
    # 一条更新的游戏消息
    m2 = add_msg(db, "u9", "游戏系统", "[GAME:REPLY] #1", msg_time=now)
    db.execute("UPDATE messages SET should_reply=1, action='GAME_REPLY'"
               " WHERE id=?", (m2,))
    # GAME_PROPOSE 应该排最后
    m3 = add_msg(db, "u9", "游戏系统", "[GAME:PROPOSE] #2", msg_time=now)
    db.execute("UPDATE messages SET should_reply=1, action='GAME_PROPOSE'"
               " WHERE id=?", (m3,))
    db.commit()
    row = claim_next_reply(db, "test-worker", 120)
    assert row[0] == m2, f"游戏应插队，实际领到 {row[0]}"
    row = claim_next_reply(db, "test-worker", 120)
    assert row[0] == m1, f"其次普通消息，实际 {row[0]}"
    row = claim_next_reply(db, "test-worker", 120)
    assert row[0] == m3, f"PROPOSE 最后，实际 {row[0]}"

    # sendd 侧：READY 的游戏消息优先发
    db2, _ = fresh_db()
    a = add_msg(db2, "u1", "Alice", "x", msg_time=now - 50)
    mark_ready(db2, a, "普通回复")
    b = add_msg(db2, "u9", "游戏系统", "y", msg_time=now)
    mark_ready(db2, b, "游戏回复")
    db2.execute("UPDATE messages SET action='GAME_REPLY' WHERE id=?", (b,))
    db2.commit()
    srow = claim_next_ready(db2, "sendd-test", 60)
    assert srow[0] == b, f"sendd 游戏优先，实际 {srow[0]}"
    print("ok: game priority queue")


def test_invalid_input_hint():
    # 2026-10-09：进行中游戏时，未被 accept 的疑似游戏尝试短消息
    #（should_reply=1/2）→ INVALID_INPUT 提示，不扣步数不改状态；
    # 纯聊天（should_reply=0）→ 保持沉默返回 None。
    import json as _json
    db, path = fresh_db()
    now = int(time.time())

    def _route(uid, name, text, should_reply):
        m = add_msg(db, uid, name, text, msg_time=now)
        return router.route_message(db, TEST_CHAT, uid, name, text,
                                    m, now, should_reply=should_reply)

    # --- 探险：[friend1] 的"飞飞飞飞"（should_reply=2）→ 提示 ---
    gid, mid = manager.start_game(db, TEST_CHAT, "adventure")
    assert gid, f"start failed: {mid}"
    state_before = _json.dumps(gstore.get_game(db, gid)["state"],
                               sort_keys=True)
    r = _route("u1", "Alice", "飞飞飞飞", 2)
    assert r and r["event_type"] == "INVALID_INPUT", r
    assert r["reply_needed"] and r["synth_msg_id"], r
    assert r["suppress_original"] is True, r  # should_reply=2 被压住
    assert "前" in r["facts"]["valid_actions"], r["facts"]
    assert r["facts"]["player"] == "Alice"
    assert r["facts"]["text"] == "飞飞飞飞"
    # 状态没变：没扣步数、没加玩家
    state_after = _json.dumps(gstore.get_game(db, gid)["state"],
                              sort_keys=True)
    assert state_before == state_after, "invalid input must not mutate state"
    # 合成 GAME_REPLY 消息存在且带渲染链
    link = gstore.get_link(db, r["synth_msg_id"])
    assert link and link["kind"] == "GAME_REPLY", link
    rp = gstore.get_render(db, r["synth_msg_id"])
    assert rp["event"]["event_type"] == "INVALID_INPUT"

    # --- [friend1] 原消息"飞 飞 飞 飞"（带空格，7 字）→ 同样要提示 ---
    # 2026-10-09 补：normalize 只去首尾空白，内部空格保留后长度 7 > 6，
    # 未补之前这条精确消息连提示都触发不了（用户投诉的原样）。
    r1b = _route("u1", "Alice", "飞 飞 飞 飞", 2)
    assert r1b and r1b["event_type"] == "INVALID_INPUT", r1b
    assert r1b["reply_needed"] and r1b["synth_msg_id"], r1b
    assert r1b["suppress_original"] is True, r1b
    state_after_b = _json.dumps(gstore.get_game(db, gid)["state"],
                                sort_keys=True)
    assert state_before == state_after_b, "invalid input must not mutate state"

    # --- 同一条消息，should_reply=0（纯聊天）→ 沉默 ---
    r2 = _route("u2", "Bob", "飞飞飞飞", 0)
    assert r2 is None, r2

    # --- "哈哈"类纯聊天即使 should_reply=2？不，"哈哈"通常是 0；
    # 这里验证长消息（>6 字）不触发提示 ---
    r3 = _route("u1", "Alice", "今天天气真不错啊", 2)
    assert r3 is None, r3

    # --- 有效动作仍走正常 MOVE 通道（回归，2026-10-10 起需 @） ---
    r4 = _route("u1", "Alice", "@Muse 前进", 2)
    assert r4 and r4["event_type"] == "MOVE", r4

    # --- 数字炸弹：轮到的玩家发无效短输入 → 提示；没轮到的 → 沉默 ---
    db2, _ = fresh_db()
    for i, (u, n) in enumerate([("u1", "Alice"), ("u2", "Bob")]):
        add_msg(db2, u, n, f"发言{i}", msg_time=now - i * 60)
    gid2, _ = manager.start_game(db2, TEST_CHAT, "number_bomb")
    assert gid2
    st2 = gstore.get_game(db2, gid2)["state"]
    cur = st2["turn_order"][st2["turn_idx"]]
    other = "u2" if cur == "u1" else "u1"
    m = add_msg(db2, cur, "P", "过", msg_time=now)
    r5 = router.route_message(db2, TEST_CHAT, cur, "P", "过",
                              m, now, should_reply=2)
    assert r5 and r5["event_type"] == "INVALID_INPUT", r5
    assert "纯数字" in r5["facts"]["valid_actions"]
    m = add_msg(db2, other, "Q", "过", msg_time=now)
    r6 = router.route_message(db2, TEST_CHAT, other, "Q", "过",
                              m, now, should_reply=2)
    assert r6 is None, r6  # 没轮到的当正常聊天

    # --- NG 词：任何短消息都不提示（存活玩家全被 accept） ---
    db3, _ = fresh_db()
    for i, (u, n) in enumerate([("u1", "Alice"), ("u2", "Bob")]):
        add_msg(db3, u, n, f"发言{i}", msg_time=now - (i + 1) * 60)
    gid3, _ = manager.start_game(db3, TEST_CHAT, "ng_word")
    assert gid3
    m = add_msg(db3, "u9", "路人", "哈哈", msg_time=now)
    r7 = router.route_message(db3, TEST_CHAT, "u9", "路人", "哈哈",
                              m, now, should_reply=2)
    assert r7 is None, r7
    print("ok: invalid input hint")


if __name__ == "__main__":
    test_invalid_input_hint()
    test_guess_word_lifecycle()
    test_guess_word_timeout_and_hints()
    test_guess_word_attempt_limit()
    test_ng_word()
    test_adventure()
    test_persistence_across_restart()
    test_render_package()
    test_no_active_game_noop()
    test_commands()
    test_natural_start()
    # 签到积分是 1-10 加权随机抽奖；固定抽到 1，让积分断言可复现
    import random as _random
    _orig_choices = _random.choices
    _random.choices = lambda *a, **k: [1]
    try:
        test_daily()
    finally:
        _random.choices = _orig_choices
    test_command_priority_over_ng()
    test_game_priority_queue()
    test_number_bomb()
    print("\nALL GAME TESTS PASSED")
