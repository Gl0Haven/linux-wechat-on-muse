#!/usr/bin/env python3
"""
关键词群梗彩蛋（功能 B，2026-10-10）。

触发：群里有人说了配置里的关键词（归一化后 substring 匹配），按
概率+冷却命中后，bot 玩梗回一句。

只做加法：旁路接入 wal_reader_daemon（不压原消息），合成消息走
messages 表（should_reply=1）+ game_events（MEME_HIT）+ message_game_link，
复用游戏渲染链路；worker 经 gstore.get_render 取 facts 渲染。

白名单：只在 MEME_CHATS 的群里启用。默认只有测试群
YOUR_TEST_GROUP@chatroom；主群 YOUR_MAIN_GROUP@chatroom 默认关闭。
要开某个群：把群 id 加进下面的 MEME_CHATS 集合即可（无需改别处）。

配置热更新：state/meme_triggers.json 每次调用重新读取，管理命令改完
立即生效，无需重启任何进程。

管理命令（仅 managers 里的大号可用，自然语言）：
  加梗：关键词1,关键词2→回复内容     # 回复内容写"即兴"记为 llm 即兴发挥
  删梗：xxx                          # 按 id 或关键词删
  梗列表 / 看梗列表                    # 列出 id、关键词、概率、冷却、开关
管理命令本身不触发梗回复；改完配置立即热更新生效。
"""
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BRIDGE_DIR, "state", "meme_triggers.json")

# ---------- 开关 ----------
# 只在这些群启用。测试群默认开；主群默认关闭。
# 切换方法：往这个集合里加/删群 id（形如 "REDACTED@chatroom"）即可。
MEME_CHATS = {
    "YOUR_TEST_GROUP@chatroom",   # [测试群]（默认启用）
    "YOUR_MAIN_GROUP@chatroom",   # [主群]（2026-10-10 切入）
}

# bot 自己的消息不触发（小号/合成系统/发送者标记）
BOT_SENDERS = {"GAME_SYSTEM", "wxid_YOUR_BOT_ACCOUNT", "MUSE"}

# 链接 kind：必须以 GAME_ 开头，worker 的 process_one 才会附带渲染包
#（见 blocking_worker.py 的 get_render 附着逻辑）
LINK_KIND = "GAME_MEME"
DEFAULT_PROBABILITY = 0.35
DEFAULT_COOLDOWN_SEC = 90

# 管理命令正则（全角/半角冒号、箭头都认）
_ADD_RE = re.compile(r"^加梗\s*[:：]\s*(.+)$", re.DOTALL)
_DEL_RE = re.compile(r"^删梗\s*[:：]\s*(.+)$", re.DOTALL)
_LIST_WORDS = {"梗列表", "看梗列表", "查看梗列表", "梗都有啥"}
_ARROW_RE = re.compile(r"[→>]")
_KW_SPLIT_RE = re.compile(r"[，,、\s]+")


def _normalize(text):
    """文本归一化（与 games.base.Game.normalize 同逻辑：去空白、全角转半角）。"""
    if not text:
        return ""
    t = text.strip()
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


def _load_config():
    """热更新：每次调用都重新读 JSON。文件缺失/损坏时返回空配置，不抛错。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"[meme] 配置缺失 {CONFIG_PATH}，视为无梗", flush=True)
        return {"triggers": [], "managers": []}
    except Exception as e:
        print(f"[meme] 配置读取失败 {CONFIG_PATH}: {e}，视为无梗", flush=True)
        return {"triggers": [], "managers": []}
    if not isinstance(cfg, dict):
        return {"triggers": [], "managers": []}
    triggers = cfg.get("triggers") or []
    clean = []
    for t in triggers:
        if not isinstance(t, dict):
            continue
        kws = [k for k in (t.get("keywords") or []) if k]
        if not kws:
            continue
        clean.append({
            "id": str(t.get("id") or kws[0]),
            "keywords": kws,
            "probability": float(t.get("probability", DEFAULT_PROBABILITY)),
            "cooldown_sec": int(t.get("cooldown_sec", DEFAULT_COOLDOWN_SEC)),
            "reply": t.get("reply", "llm"),
            "enabled": bool(t.get("enabled", True)),
        })
    managers = [m for m in (cfg.get("managers") or []) if m]
    return {"triggers": clean, "managers": managers}


def _save_config(cfg):
    """原子写回配置（tmp + rename），供管理命令用。"""
    data = {
        "triggers": [
            {"id": t["id"], "keywords": t["keywords"],
             "probability": t["probability"],
             "cooldown_sec": t["cooldown_sec"],
             "reply": t["reply"], "enabled": t["enabled"]}
            for t in cfg["triggers"]
        ],
        "managers": cfg["managers"],
    }
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def _ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS meme_cooldown (
        trigger_id TEXT NOT NULL,
        chat TEXT NOT NULL,
        last_hit INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (trigger_id, chat))""")
    db.commit()


def _enqueue_meme(db, chat, event_type, facts):
    """写合成消息 + 事件 + 链接，返回 (synth_msg_id, event_id)。"""
    from games import store as gstore
    from games.manager import enqueue_game_message
    gstore.ensure_schema(db)
    eid = gstore.add_event(db, 0, event_type, facts, commit=False)
    mid = enqueue_game_message(db, chat, 0, eid, LINK_KIND, commit=False)
    db.commit()
    return mid, eid


def _hit_cooldown(db, trigger_id, chat, cooldown_sec, now):
    """冷却中返回 True；否则更新 last_hit 返回 False。"""
    row = db.execute(
        "SELECT last_hit FROM meme_cooldown WHERE trigger_id=? AND chat=?",
        (trigger_id, chat)).fetchone()
    if row and now - (row[0] or 0) < cooldown_sec:
        return True
    db.execute(
        "INSERT OR REPLACE INTO meme_cooldown (trigger_id, chat, last_hit)"
        " VALUES (?,?,?)", (trigger_id, chat, now))
    db.commit()
    return False


# ---------------- 管理命令 ----------------

def _parse_admin(content):
    """返回 ('add', payload) / ('del', arg) / ('list', None) / None。"""
    text = _normalize(content)
    m = _ADD_RE.match(text)
    if m:
        return ("add", m.group(1).strip())
    m = _DEL_RE.match(text)
    if m:
        return ("del", m.group(1).strip())
    if text in _LIST_WORDS:
        return ("list", None)
    return None


def _unique_id(cfg, base):
    base = re.sub(r"\s+", "", base)[:24] or "custom"
    tid, i = base, 2
    existing = {t["id"] for t in cfg["triggers"]}
    while tid in existing:
        tid = f"{base}_{i}"
        i += 1
    return tid


def _add_trigger_to_cfg(cfg, kws, reply, probability, cooldown_sec):
    """往 cfg 里加一个 trigger 并原子保存，返回 trigger id。"""
    tid = _unique_id(cfg, kws[0])
    cfg["triggers"].append({
        "id": tid, "keywords": kws,
        "probability": float(probability),
        "cooldown_sec": int(cooldown_sec),
        "reply": "llm" if reply == "即兴" else reply,
        "enabled": True,
    })
    _save_config(cfg)
    return tid


def meme_add_trigger(keywords, reply="即兴", probability=0.3,
                     cooldown_sec=120):
    """程序化加梗（供策展 worker 用）。
    keywords 为 list 或逗号/顿号/空格分隔串；reply 写"即兴"则 bot 即兴发挥。
    关键词已存在时直接返回现有 id，不重复加。
    返回 trigger id；输入非法抛 ValueError。"""
    if isinstance(keywords, str):
        kws = [k for k in _KW_SPLIT_RE.split(keywords) if k]
    else:
        kws = [str(k).strip() for k in keywords if str(k).strip()]
    if not kws:
        raise ValueError("关键词不能为空")
    reply = (reply or "").strip()
    if not reply:
        raise ValueError("回复内容不能为空")
    prob = float(probability)
    if not 0 < prob <= 1:
        raise ValueError("概率必须在 (0,1] 内")
    cfg = _load_config()
    norm_new = {_normalize(k) for k in kws}
    for t in cfg["triggers"]:
        if norm_new & {_normalize(k) for k in t["keywords"]}:
            return t["id"]
    return _add_trigger_to_cfg(cfg, kws, reply, prob, int(cooldown_sec))


def _handle_admin(db, chat, sender, sender_name, op, payload, cfg,
                  msg_id, msg_time):
    """执行管理命令，走合成消息回复确认。返回 result dict。"""
    who = sender_name or sender
    facts_base = {"kind": "meme_admin", "operator": who, "op": op}

    if op == "list":
        if not cfg["triggers"]:
            text = "当前没有梗（空的）。"
        else:
            lines = []
            for t in cfg["triggers"]:
                sw = "开" if t["enabled"] else "关"
                lines.append(
                    f"· {t['id']}｜关键词：{','.join(t['keywords'])}｜"
                    f"概率 {t['probability']}｜冷却 {t['cooldown_sec']}s｜{sw}")
            text = "梗列表：\n" + "\n".join(lines)
        facts = {**facts_base, "fixed_text": text,
                 "worker_instruction": "直接发送 fixed_text 的内容。"}
        mid, eid = _enqueue_meme(db, chat, "MEME_ADMIN", facts)
        return {"handled": True, "kind": "meme_admin", "op": "list",
                "synth_msg_id": mid, "event_id": eid, "facts": facts}

    if op == "add":
        parts = _ARROW_RE.split(payload, maxsplit=1)
        if len(parts) != 2:
            text = (f"{who} 加梗格式不对。正确格式：\n"
                    "加梗：关键词1,关键词2→回复内容\n"
                    "（回复内容写「即兴」则 bot 即兴发挥）")
            facts = {**facts_base, "fixed_text": text,
                     "worker_instruction": "直接发送 fixed_text 的内容。"}
            mid, eid = _enqueue_meme(db, chat, "MEME_ADMIN", facts)
            return {"handled": True, "kind": "meme_admin", "op": "add",
                    "ok": False, "synth_msg_id": mid, "event_id": eid,
                    "facts": facts}
        kws = [k for k in _KW_SPLIT_RE.split(parts[0].strip()) if k]
        reply = parts[1].strip()
        if not kws or not reply:
            text = f"{who} 关键词和回复内容都不能为空，再试一次。"
            facts = {**facts_base, "fixed_text": text,
                     "worker_instruction": "直接发送 fixed_text 的内容。"}
            mid, eid = _enqueue_meme(db, chat, "MEME_ADMIN", facts)
            return {"handled": True, "kind": "meme_admin", "op": "add",
                    "ok": False, "synth_msg_id": mid, "event_id": eid,
                    "facts": facts}
        tid = _add_trigger_to_cfg(cfg, kws, reply,
                                    DEFAULT_PROBABILITY, DEFAULT_COOLDOWN_SEC)
        mode = "即兴发挥" if reply == "即兴" else "固定文本"
        text = (f"{who} 已加上梗「{tid}」：关键词 {','.join(kws)}，"
                f"{mode}，概率 {DEFAULT_PROBABILITY}，"
                f"冷却 {DEFAULT_COOLDOWN_SEC}s。立即生效。")
        facts = {**facts_base, "trigger_id": tid, "fixed_text": text,
                 "worker_instruction": "直接发送 fixed_text 的内容。"}
        mid, eid = _enqueue_meme(db, chat, "MEME_ADMIN", facts)
        print(f"[meme] 加梗 {tid} by {who}", flush=True)
        return {"handled": True, "kind": "meme_admin", "op": "add",
                "ok": True, "trigger_id": tid,
                "synth_msg_id": mid, "event_id": eid, "facts": facts}

    if op == "del":
        arg = _normalize(payload)
        hit = None
        for t in cfg["triggers"]:
            if t["id"] == arg or arg in [_normalize(k) for k in t["keywords"]]:
                hit = t
                break
        if not hit:
            text = f"{who} 没找到梗「{payload}」（按 id 或关键词删）。"
            facts = {**facts_base, "fixed_text": text,
                     "worker_instruction": "直接发送 fixed_text 的内容。"}
            mid, eid = _enqueue_meme(db, chat, "MEME_ADMIN", facts)
            return {"handled": True, "kind": "meme_admin", "op": "del",
                    "ok": False, "synth_msg_id": mid, "event_id": eid,
                    "facts": facts}
        cfg["triggers"] = [t for t in cfg["triggers"]
                           if t["id"] != hit["id"]]
        _save_config(cfg)
        text = f"{who} 已删掉梗「{hit['id']}」。立即生效。"
        facts = {**facts_base, "trigger_id": hit["id"], "fixed_text": text,
                 "worker_instruction": "直接发送 fixed_text 的内容。"}
        mid, eid = _enqueue_meme(db, chat, "MEME_ADMIN", facts)
        print(f"[meme] 删梗 {hit['id']} by {who}", flush=True)
        return {"handled": True, "kind": "meme_admin", "op": "del",
                "ok": True, "trigger_id": hit["id"],
                "synth_msg_id": mid, "event_id": eid, "facts": facts}

    return {"handled": False}


# ---------------- 主入口 ----------------

def check_meme(db, chat, chat_name, sender, sender_name, content,
               msg_id, msg_time):
    """
    关键词群梗彩蛋主入口（wal_reader_daemon 旁路调用）。
    返回 result dict 或 None（未命中/被过滤）。
    绝不 suppress 原消息。
    """
    # 1. 白名单
    if chat not in MEME_CHATS:
        return None
    # 2. bot 自己的消息不触发
    if sender in BOT_SENDERS:
        return None
    if not content:
        return None

    # 3. 配置热更新（每次调用重读）
    cfg = _load_config()

    # 4. 管理命令优先（仅 managers 可用；管理命令本身不触发梗回复；
    #    非 managers 发的管理命令形态文本整体忽略——不执行也不触发梗，
    #    防止"删梗：皮套"这类文本被关键词误伤）
    admin = _parse_admin(content)
    if admin is not None:
        if sender in cfg["managers"]:
            return _handle_admin(db, chat, sender, sender_name,
                                 admin[0], admin[1], cfg, msg_id, msg_time)
        return None

    # 5. 静默检查（管理命令在静默期仍可执行，故放后面）
    from msgqueue import is_muted
    muted, _ = is_muted(db, int(time.time()))
    if muted:
        return None

    _ensure_schema(db)
    now = int(time.time())
    text = _normalize(content)
    who = sender_name or sender

    # 6. 逐 trigger：enabled → 关键词 substring 命中 → 冷却 → 概率
    #    一条消息最多触发一个梗（取第一个命中的），防刷屏。
    for t in cfg["triggers"]:
        if not t["enabled"]:
            continue
        hit_kw = next((k for k in t["keywords"]
                       if _normalize(k) and _normalize(k) in text), None)
        if not hit_kw:
            continue
        if _hit_cooldown(db, t["id"], chat, t["cooldown_sec"], now):
            return {"handled": True, "kind": "meme_hit",
                    "trigger_id": t["id"], "cooled_down": True,
                    "synth_msg_id": None}
        if random.random() >= t["probability"]:
            return {"handled": True, "kind": "meme_hit",
                    "trigger_id": t["id"], "rolled_out": True,
                    "synth_msg_id": None}
        # 命中
        if t["reply"] == "llm":
            facts = {
                "kind": "meme_hit",
                "trigger_id": t["id"],
                "hit_keyword": hit_kw,
                "hit_sender": who,
                "hit_content": content[:80],
                "reply_mode": "llm",
                "worker_instruction": (
                    f"群里 {who} 说了「{hit_kw}」（群梗「{t['id']}」被触发）。"
                    "请用一两句玩梗回复：呼应这个群梗、语气轻松搞笑、"
                    "适合手机阅读的短句分段。不要解释自己在玩梗，"
                    "不要 @ 任何人。"),
            }
        else:
            facts = {
                "kind": "meme_hit",
                "trigger_id": t["id"],
                "hit_keyword": hit_kw,
                "hit_sender": who,
                "hit_content": content[:80],
                "reply_mode": "fixed",
                "fixed_text": t["reply"],
                "worker_instruction": (
                    "发出 fixed_text 的内容：语气轻松玩笑，可加一句自然过渡"
                    "衔接上下文；fixed_text 里括号中的语气说明不要照发。"),
            }
        mid, eid = _enqueue_meme(db, chat, "MEME_HIT", facts)
        print(f"[meme] 命中 {t['id']}（词:{hit_kw}）by {who} -> #{mid}",
              flush=True)
        return {"handled": True, "kind": "meme_hit",
                "trigger_id": t["id"], "hit_keyword": hit_kw,
                "reply_mode": facts["reply_mode"],
                "synth_msg_id": mid, "event_id": eid, "facts": facts}

    return None


# ---------- 每周梗策展（2026-10-10，用户要求：bot 自主加梗，用户审核） ----------

CURATION_INTERVAL_SEC = 7 * 86400  # 每个群每 7 天策展一次


def _ensure_curation_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS meme_curation (
        chat TEXT PRIMARY KEY,
        last_run INTEGER NOT NULL DEFAULT 0)""")
    db.commit()


def check_meme_curation(db, now=None):
    """每周梗策展入口（scheduled.py 定时调用）。
    每个 MEME_CHATS 的群每 7 天产生一条 MEME_CURATE 合成消息；
    worker 收到后自主从聊天记录里发掘 1-3 个新梗并加上（保守参数），
    再向群里发一条新增通知（用户随时可 删梗 撤回 / 梗列表 审核）。
    无合格候选时 worker 直接 skip，保持安静。
    返回 {chat: result}。"""
    import msgqueue
    now = int(now or time.time())
    _ensure_schema(db)
    _ensure_curation_schema(db)
    out = {}
    muted, _ = msgqueue.is_muted(db, now)
    for chat in sorted(MEME_CHATS):
        if muted:
            out[chat] = {"skipped": "muted"}
            continue
        row = db.execute(
            "SELECT last_run FROM meme_curation WHERE chat=?",
            (chat,)).fetchone()
        last = row[0] if row else 0
        if now - last < CURATION_INTERVAL_SEC:
            out[chat] = {"skipped": "not_due",
                         "next_in_days": round(
                             (CURATION_INTERVAL_SEC - (now - last)) / 86400, 1)}
            continue
        facts = {
            "kind": "meme_curate",
            "chat": chat,
            "worker_instruction": (
                "你是本群的梗策展人，每周一次，从聊天记录里发掘新梗。\n"
                "1) 先看近 7 天聊天：在 ~/workspace/wechat-bridge 下运行 "
                f"`python3 meme.py recent {chat} 7`。\n"
                "2) 找出 1-3 个真正长出来的群内梗，标准：同一说法出现≥3次、"
                "至少2个人用过、有玩梗/调侃属性、关键词≤6字。\n"
                "不要加：已有触发器的关键词（先运行 `python3 meme.py list` "
                "对照）、人名外号、一次性笑话、敏感/冒犯/政治内容、广告词。\n"
                "3) 每个梗运行：`python3 meme.py madd \"关键词1,关键词2\" "
                "\"即兴\" 0.3 120`（概率一律0.3、冷却120秒、即兴模式；"
                "宁可少加，不要硬凑）。\n"
                "4) 加完后，用这条消息向群里发一条通知：列出新增的梗和关键词，"
                "加一句\"觉得不好玩就发 删梗：xxx，我随时撤\"。\n"
                "5) 如果没有合格候选，一个都不要加，直接用 --skip 关闭这条任务，"
                "保持安静，不要发任何消息。"
            ),
        }
        mid, eid = _enqueue_meme(db, chat, "MEME_CURATE", facts)
        db.execute("INSERT OR REPLACE INTO meme_curation (chat, last_run)"
                   " VALUES (?, ?)", (chat, now))
        db.commit()
        print(f"[meme] 策展任务 -> {chat} #{mid}", flush=True)
        out[chat] = {"ok": True, "synth_msg_id": mid, "event_id": eid}
    return out


def _cli_recent(chat, days):
    """打印某群近 N 天入站消息（sender: content，去重截断），供策展 worker 看。"""
    import msgqueue
    db = msgqueue.get_db()
    try:
        since = int(time.time()) - int(days) * 86400
        rows = db.execute(
            """SELECT sender_name, content FROM messages
               WHERE chat=? AND direction='INBOUND' AND msg_time > ?
               AND sender NOT IN ('GAME_SYSTEM','MUSE')
               ORDER BY msg_time DESC LIMIT 150""",
            (chat, since)).fetchall()
    finally:
        db.close()
    seen = set()
    out = []
    for name, content in reversed(rows):
        c = (content or "").strip().replace("\n", " ")[:40]
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(f"{name or '?'}: {c}")
    print("\n".join(out))


if __name__ == "__main__":
    # CLI：list | parse "<content>" | madd "kw1,kw2" "回复|即兴" [prob] [cd]
    #      | recent <chat> <days>
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "parse":
        print(_parse_admin(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == "madd":
        kws = sys.argv[2] if len(sys.argv) > 2 else ""
        reply = sys.argv[3] if len(sys.argv) > 3 else "即兴"
        prob = float(sys.argv[4]) if len(sys.argv) > 4 else 0.3
        cd = int(sys.argv[5]) if len(sys.argv) > 5 else 120
        try:
            print(meme_add_trigger(kws, reply, prob, cd))
        except ValueError as e:
            print(f"加梗失败：{e}")
    elif cmd == "recent":
        _cli_recent(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 7)
    else:
        cfg = _load_config()
        for t in cfg["triggers"]:
            sw = "开" if t["enabled"] else "关"
            print(f"{t['id']}: {','.join(t['keywords'])} "
                  f"p={t['probability']} cd={t['cooldown_sec']}s {sw} "
                  f"reply={t['reply'][:30]}")
        print(f"managers: {cfg['managers']}")
