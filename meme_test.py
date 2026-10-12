#!/usr/bin/env python3
"""
功能 B（关键词群梗彩蛋）单元测试。

跑法：python3 meme_test.py
全部在 /tmp 测试库上跑，绝不碰真实 queue.db、不发真实消息。
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DB = os.path.join(tempfile.gettempdir(), "meme_test.db")
TEST_CFG = os.path.join(tempfile.gettempdir(), "meme_test_triggers.json")

import msgqueue
msgqueue.DB_PATH = TEST_DB
import meme
meme.CONFIG_PATH = TEST_CFG

from games import store as gstore

TEST_CHAT = "YOUR_TEST_GROUP@chatroom"   # 测试群（白名单内）
OTHER_CHAT = "other@chatroom"            # 不在 MEME_CHATS 白名单内
BOSS = "wxid_YOUR_MAIN_ACCOUNT"          # 大号（manager）
ALICE = "wxid_REDACTED"


def fresh_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    db = msgqueue.get_db()
    # 生产 queue.db 里有、但 get_db() 新建空库不会建的列（上游 latent bug，
    # 报告里会注明；这里补齐以模拟真实环境）
    cols = [r[1] for r in db.execute("PRAGMA table_info(messages)")]
    if "media_path" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN media_path TEXT")
    if "needs_media" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN needs_media INTEGER DEFAULT 0")
    db.commit()
    return db


def write_cfg(triggers, managers=None):
    cfg = {"triggers": triggers,
           "managers": managers if managers is not None else [BOSS]}
    with open(TEST_CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def base_triggers(**kw):
    t = [
        {"id": "bianchangmoji", "keywords": ["鞭长莫及"],
         "probability": 1.0, "cooldown_sec": 90,
         "reply": "llm", "enabled": True},
        {"id": "pitao", "keywords": ["皮套", "皮套人"],
         "probability": 1.0, "cooldown_sec": 90,
         "reply": "固定回复文本", "enabled": True},
        {"id": "dian", "keywords": ["典"], "probability": 1.0,
         "cooldown_sec": 90, "reply": "llm", "enabled": False},
    ]
    for x in t:
        x.update(kw.get(x["id"], {}))
    return t


PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def synth_msgs(db):
    return db.execute(
        "SELECT id, content, action, should_reply, status, sender"
        " FROM messages WHERE sender='GAME_SYSTEM'"
        " ORDER BY id").fetchall()


def test_hit_llm():
    print("== 关键词命中（llm 模式） ==")
    db = fresh_db()
    write_cfg(base_triggers())
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "这波操作真是鞭长莫及啊", 1001, int(time.time()))
    check("返回 handled/meme_hit", r and r["kind"] == "meme_hit"
          and r["trigger_id"] == "bianchangmoji", str(r))
    check("reply_mode=llm", r and r["reply_mode"] == "llm")
    msgs = synth_msgs(db)
    check("合成消息已入队", len(msgs) == 1, str(msgs))
    m = msgs[0]
    check("should_reply=1", m[3] == 1)
    check("action=GAME_MEME", m[2] == "GAME_MEME", str(m[2]))
    render = gstore.get_render(db, m[0])
    check("get_render 可取", render and render["event"]["event_type"] == "MEME_HIT")
    facts = render["event"]["facts"]
    check("facts 有 worker_instruction", bool(facts.get("worker_instruction")))
    check("facts 含 trigger_id/命中词", facts.get("trigger_id") == "bianchangmoji"
          and facts.get("hit_keyword") == "鞭长莫及", str(facts))
    check("facts 无 fixed_text（llm 模式）", "fixed_text" not in facts)
    db.close()


def test_hit_fixed():
    print("== 关键词命中（fixed 模式） ==")
    db = fresh_db()
    write_cfg(base_triggers())
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "皮套人集合了", 1002, int(time.time()))
    check("命中 pitao", r and r["trigger_id"] == "pitao", str(r))
    render = gstore.get_render(db, r["synth_msg_id"])
    facts = render["event"]["facts"]
    check("reply_mode=fixed 且 fixed_text 正确",
          facts["reply_mode"] == "fixed"
          and facts["fixed_text"] == "固定回复文本", str(facts))
    db.close()


def test_miss_and_disabled():
    print("== 未命中 / disabled ==")
    db = fresh_db()
    write_cfg(base_triggers())
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "今天天气不错", 1003, int(time.time()))
    check("无关键词 → None", r is None, str(r))
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "典中典", 1004, int(time.time()))
    check("disabled 梗 → None", r is None, str(r))
    check("无合成消息", len(synth_msgs(db)) == 0)
    db.close()


def test_probability_bounds():
    print("== 概率边界 ==")
    db = fresh_db()
    write_cfg(base_triggers(bianchangmoji={"probability": 0.0}))
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "鞭长莫及", 1005, int(time.time()))
    check("probability=0 → rolled_out 不入队",
          r and r.get("rolled_out") and len(synth_msgs(db)) == 0, str(r))
    write_cfg(base_triggers(bianchangmoji={"probability": 1.0}))
    hits = 0
    for i in range(10):
        # 每次清冷却，纯测概率
        db.execute("DELETE FROM meme_cooldown")
        r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                            "鞭长莫及", 2000 + i, int(time.time()))
        if r and r.get("synth_msg_id"):
            hits += 1
    check("probability=1 → 10/10 命中", hits == 10, f"hits={hits}")
    db.close()


def test_cooldown():
    print("== 冷却 ==")
    db = fresh_db()
    write_cfg(base_triggers())
    kw = {"chat": TEST_CHAT, "chat_name": "测试群", "sender": ALICE,
          "sender_name": "Alice", "content": "鞭长莫及", "msg_time": int(time.time())}
    r1 = meme.check_meme(db, msg_id=1010, **kw)
    r2 = meme.check_meme(db, msg_id=1011, **kw)
    check("第一次命中", r1 and r1.get("synth_msg_id"), str(r1))
    check("第二次被冷却", r2 and r2.get("cooled_down")
          and len(synth_msgs(db)) == 1, str(r2))
    # 冷却过期（手动回拨 last_hit）
    db.execute("UPDATE meme_cooldown SET last_hit=? WHERE trigger_id=?",
               (int(time.time()) - 100, "bianchangmoji"))
    db.commit()
    r3 = meme.check_meme(db, msg_id=1012, **kw)
    check("冷却过期后再次命中", r3 and r3.get("synth_msg_id")
          and len(synth_msgs(db)) == 2, str(r3))
    db.close()


def test_admin_add_del_list():
    print("== 管理命令：增删查 ==")
    db = fresh_db()
    write_cfg(base_triggers())
    # 加梗（即兴）
    r = meme.check_meme(db, TEST_CHAT, "测试群", BOSS, "[user]",
                        "加梗：急急急,火火火→即兴", 1100, int(time.time()))
    check("加梗 handled", r and r["kind"] == "meme_admin"
          and r["op"] == "add" and r["ok"], str(r))
    cfg = json.load(open(TEST_CFG))
    new = [t for t in cfg["triggers"] if t["id"] == r["trigger_id"]]
    check("配置已写入且 reply=llm",
          len(new) == 1 and new[0]["reply"] == "llm"
          and new[0]["keywords"] == ["急急急", "火火火"], str(new))
    # 新梗立即生效（热更新）
    r2 = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                         "急急急", 1101, int(time.time()))
    check("新梗立即触发", r2 and r2["trigger_id"] == r["trigger_id"], str(r2))
    # 列表
    r3 = meme.check_meme(db, TEST_CHAT, "测试群", BOSS, "[user]",
                         "梗列表", 1102, int(time.time()))
    facts = gstore.get_render(db, r3["synth_msg_id"])["event"]["facts"]
    check("梗列表含新梗", r3 and r3["op"] == "list"
          and r["trigger_id"] in facts["fixed_text"], facts["fixed_text"][:80])
    # 按关键词删
    r4 = meme.check_meme(db, TEST_CHAT, "测试群", BOSS, "[user]",
                         "删梗：火火火", 1103, int(time.time()))
    check("按关键词删除", r4 and r4["op"] == "del" and r4["ok"], str(r4))
    cfg = json.load(open(TEST_CFG))
    check("配置已删", r["trigger_id"] not in [t["id"] for t in cfg["triggers"]])
    # 按 id 删
    r5 = meme.check_meme(db, TEST_CHAT, "测试群", BOSS, "[user]",
                         "删梗：pitao", 1104, int(time.time()))
    check("按 id 删除", r5 and r5["ok"] and r5["trigger_id"] == "pitao", str(r5))
    # 管理命令本身不触发梗回复
    check("删梗命令没触发 MEME_HIT",
          gstore.get_event(db, r5["event_id"])["event_type"] == "MEME_ADMIN")
    # 删不存在的
    r6 = meme.check_meme(db, TEST_CHAT, "测试群", BOSS, "[user]",
                         "删梗：不存在的梗", 1105, int(time.time()))
    check("删不存在 → ok=False", r6 and r6["op"] == "del" and not r6["ok"], str(r6))
    db.close()


def test_admin_not_boss():
    print("== 非大号管理命令被忽略 ==")
    db = fresh_db()
    write_cfg(base_triggers())
    before = json.load(open(TEST_CFG))
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "加梗：捣乱→哈哈", 1200, int(time.time()))
    check("非大号加梗 → None", r is None, str(r))
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "删梗：皮套", 1201, int(time.time()))
    check("非大号删梗 → None（且不触发皮套梗）", r is None, str(r))
    check("配置未被改", json.load(open(TEST_CFG)) == before)
    check("无合成消息", len(synth_msgs(db)) == 0)
    # 大号的管理命令形态含关键词也不触发梗
    r = meme.check_meme(db, TEST_CHAT, "测试群", BOSS, "[user]",
                        "删梗：皮套", 1202, int(time.time()))
    check("大号删梗命令不触发梗回复",
          r and r["kind"] == "meme_admin" and r["trigger_id"] == "pitao", str(r))
    db.close()


def test_self_and_chat_filter():
    print("== bot 自身消息 / 非白名单群 ==")
    db = fresh_db()
    write_cfg(base_triggers())
    for s in ["GAME_SYSTEM", "wxid_YOUR_BOT_ACCOUNT", "MUSE"]:
        r = meme.check_meme(db, TEST_CHAT, "测试群", s, "bot",
                            "鞭长莫及", 1300, int(time.time()))
        check(f"sender={s} 不触发", r is None, str(r))
    r = meme.check_meme(db, OTHER_CHAT, "主群", ALICE, "Alice",
                        "鞭长莫及", 1301, int(time.time()))
    check("非白名单群不触发", r is None, str(r))
    check("无合成消息", len(synth_msgs(db)) == 0)
    db.close()


def test_hot_reload():
    print("== 热更新 ==")
    db = fresh_db()
    write_cfg(base_triggers())
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "典", 1400, int(time.time()))
    check("改前 disabled 梗不触发", r is None, str(r))
    # 直接改 JSON（模拟手改/管理命令），下一次调用即生效
    cfg = json.load(open(TEST_CFG))
    for t in cfg["triggers"]:
        if t["id"] == "dian":
            t["enabled"] = True
    with open(TEST_CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "典", 1401, int(time.time()))
    check("改后立即触发", r and r["trigger_id"] == "dian", str(r))
    db.close()


def test_mute():
    print("== 静默期不触发 ==")
    db = fresh_db()
    write_cfg(base_triggers())
    db.execute("INSERT OR REPLACE INTO mute_state"
               " (scope, mute_until, set_by_wxid, reason, created_at)"
               " VALUES ('global', ?, '', 'test', ?)",
               (int(time.time()) + 3600, int(time.time())))
    db.commit()
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "鞭长莫及", 1500, int(time.time()))
    check("静默期关键词不触发", r is None, str(r))
    # 管理命令在静默期仍可用
    r = meme.check_meme(db, TEST_CHAT, "测试群", BOSS, "[user]",
                        "看梗列表", 1501, int(time.time()))
    check("静默期管理命令仍可用", r and r["kind"] == "meme_admin", str(r))
    db.close()


def test_normalize():
    print("== 归一化 ==")
    db = fresh_db()
    write_cfg(base_triggers())
    r = meme.check_meme(db, TEST_CHAT, "测试群", ALICE, "Alice",
                        "　鞭长莫及！", 1600, int(time.time()))  # 全角空格
    check("全角空格归一化后命中", r and r["trigger_id"] == "bianchangmoji", str(r))
    db.close()


if __name__ == "__main__":
    for fn in [test_hit_llm, test_hit_fixed, test_miss_and_disabled,
               test_probability_bounds, test_cooldown, test_admin_add_del_list,
               test_admin_not_boss, test_self_and_chat_filter,
               test_hot_reload, test_mute, test_normalize]:
        try:
            fn()
        except Exception as e:
            FAIL += 1
            print(f"  ERROR {fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
