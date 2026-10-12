#!/usr/bin/env python3
"""
游戏五：互动故事（AI 主写的分支小说）

设计文档：~/workspace/wechat-bridge/games/STORY_DESIGN.md（预研 v1，2026-10-10）
借鉴：astrbot_plugin_321roll_lite / interactive-novel / AI Dungeon 说书人 prompt /
      Wayline《AI Dungeon Masters》/ Solo DM Guide / AI-Dungeon-Master 等（见文档 §0）。

玩法：
- 开局后 bot（worker/LLM）写开篇（150~300 字），结尾给出 A/B/C/D 四个选项。
- 群友投票选选项（发 A/B/C/D），多数决；投票窗口 3 分钟；提前全投完可提前结算。
  也允许自由发言定方向（50 字内一句话），语义接近某选项则并入计票，
  否则记为"自定义选项"（一人一条，取最后一条；零 A-D 票时兜底）。
- 高风险选项可标"需检定·难度"，程序掷 d20 判定，结果注入 prompt 由 LLM 演绎。
- 每轮程序掷 d100 决定轻量随机事件（25%，碾压局 40%），注入 prompt 自然融入。
- Python 维护 story bible（世界观/人物/伏笔/倾向），LLM 每轮全量读取；
  6~10 轮后按倾向加权进多结局分支（≥3 真结局 + 坏结局）。

状态机（phase）：opening → voting → voting … → ending → done
- "writing" 阶段被折叠进 voting tick：tick 在关闭投票的同时解析上一章
  （章节消息里自带新【选项】块，玩家直接对着章节消息投票），
  这样每轮只需 1 次 tick（约 5 分钟），8 轮 ≈ 35~50 分钟（见节奏说明）。
- 最小 6 轮才允许进结局（MIN_ROUNDS_BEFORE_ENDING），默认开篇+6章+结局=8 轮。

worker 输出格式契约（与文档 §1/§7 对齐，【】标记给程序解析）：
    第X章·四字短语 / 正文 / --- / 钩子句 / ---
    【选项】
    A. ……（≤25 字，可含"需检定·困难"标注）
    B. ……
    C. ……
    D. ……（人物专属，可选）
    E. 进入结局（剧情已成熟）（仅第 6~7 轮 worker 可加）
    投票3分钟：直接发 A/B/C/D，或自由发言（自由发言优先采用，多条时说书人选最有意思的）
    ---
    【大纲】
    ```json { bible JSON，见文档 §2.1 } ```
- 结局轮另输出【结局：xxx】与【最佳转折：人名 - 理由】。
- 解析失败走重发兜底（最多 3 次，之后强制结算）。

确定性：计票/平票/d100/d20 全部用 Game.make_rng(game_id, salt)，VM 重启可复现。
"""

import json
import os
import re
import time

from .base import Game, recent_speakers
from .config import POINTS, GAME_SYSTEM_SENDER

# 聊天风格档案（2026-10-10 用户要求：D 选项按档案写，不让 LLM 瞎猜；
# 每日 08:22 由 chat-profiles-daily-update cron 更新）
# 格式：{"群昵称": "聊天风格档案-<昵称>.md"}；档案文件不入库
CHAT_PROFILE_FILES = {}


def _load_style_profiles(player_names=()):
    """读聊天风格档案，提取语气画像。

    只返回 player_names 里出现的人（按名字子串匹配）；player_names 为空时
    全返回。档案缺失/读失败时静默跳过，调用方回退到 LLM 上下文推断。
    """
    base = os.path.expanduser("~/workspace/your_files")
    profiles = {}
    for key, fname in CHAT_PROFILE_FILES.items():
        if player_names and not any(
                key.lower() in (n or "").lower() for n in player_names):
            continue
        try:
            text = open(os.path.join(base, fname), encoding="utf-8").read()
        except OSError:
            continue
        m = re.search(r"## 语气画像\n(.*?)(?=\n## |\Z)", text, re.S)
        if not m:
            continue
        portrait = m.group(1).strip()
        if len(portrait) > 350:
            portrait = portrait[:350] + "…"
        profiles[key] = portrait
    return profiles


STYLE_BLOCK_TMPL = """
【群友风格速查（D 选项专用，有档案以档案为准）】
{styles}
D 选项必须从上面挑一人写"一句性格描述 + 具体行动"，不许凭空编性格、不许选档案里没有的人。
"""


def _with_style_block(instruction, player_names):
    """给 worker 指令追加群友风格速查（D 选项用）。

    按 player_names 匹配档案；无匹配时原样返回，worker 回退到上下文推断。
    """
    profiles = _load_style_profiles(player_names)
    if not profiles:
        return instruction
    styles = "\n".join("- %s：%s" % (k, v) for k, v in profiles.items())
    return instruction + STYLE_BLOCK_TMPL.format(styles=styles)

# ---------- 参数（文档 §6.1） ----------

DURATION_SEC = 90 * 60          # 自然超时（安全兜底；正常对局由 max_rounds 结束）
VOTE_WINDOW_SEC = 180           # 投票窗口 3 分钟（2026-10-10 起章节发出后直排一次性唤醒，窗口一关就 tick，实际约 3~5 分钟/轮）
EXTEND_SEC = 120                # 投票延长 1 次 2 分钟（文档 §4.1）
DEFAULT_MAX_ROUNDS = 6         # 默认续写章节数：开篇 + 6 章 + 结局 ≈ 8 轮
MIN_ROUNDS_BEFORE_ENDING = 6   # 用户硬要求：至少 6 轮才允许进结局分支
MAX_ROUNDS_CAP = 10            # 文档 §5.3：第 10 轮无论如何进结局
REEMIT_LIMIT = 3               # worker 格式解析失败最多重发次数
EVENT_TRIGGER = 25              # d100 ≤25 触发随机事件（文档 §4.4）
EVENT_TRIGGER_HOT = 40          # 连续两轮碾压时触发率提到 40
CHECK_DIFFICULTIES = {"简单": 8, "标准": 12, "困难": 15, "极难": 18}  # d20 难度档（S1）
TENDENCY_DIMS = ("courage", "caution", "trust", "ruthless", "curiosity")

# 只在测试群启用。切换方法：把主群加进来即可
#   STORY_TEST_CHATS = {"YOUR_TEST_GROUP@chatroom", "YOUR_MAIN_GROUP@chatroom"}
STORY_TEST_CHATS = {"YOUR_TEST_GROUP@chatroom", "YOUR_MAIN_GROUP@chatroom"}  # [测试群] + [主群]（2026-10-10 切入）

_VOTE_RE = re.compile(r"^[A-Ea-e]$")
_AT_RE = re.compile(r"@\S+")
_OPT_LINE_RE = re.compile(r"^\s*([A-Ea-e])\s*[：:．.]\s*(.+?)\s*$")
_CHECK_RE = re.compile(r"需检定·(简单|标准|困难|极难)")
_EXTEND_KEYWORDS = ("等一下", "等等", "再想想", "先别急", "慢点", "等一等")

# 默认随机事件表（文档 §8.3：题材库可按题材替换，worker 可在 bible 里提供 event_table）
DEFAULT_EVENTS = [
    "深夜起雾，能见度骤降",
    "一位 NPC 情绪明显波动",
    "主角团有个小发现（一件不起眼的东西）",
    "主角旧伤复发，行动受限",
    "天气突变，打乱了所有人的计划",
    "远处传来不属于这个地方的声音",
    "一位配角的秘密意外暴露一角",
    "关键道具暂时失灵/丢失",
]


# ---------- worker 输出格式解析 ----------

def _extract_block(text, name):
    """提取【name】块：从【name】到下一个【xxx】（或文末）之间的内容。"""
    m = re.search(r"【" + re.escape(name) + r"】", text or "")
    if not m:
        return None
    rest = text[m.end():]
    m2 = re.search(r"【[^】]*】", rest)
    if m2:
        rest = rest[:m2.start()]
    return rest.strip()


def parse_options(text):
    """解析【选项】块 → [{"key":"A","text":"..."}]，失败返回 None。

    兼容 "A. xxx"（文档格式）与 "A：xxx"（旧格式）；E 仅 worker 提供时有效。
    """
    block = _extract_block(text, "选项")
    if not block:
        # 2026-10-10 首测兜底：worker 漏写【选项】标记时，直接全文扫描
        # A./B./C./D. 行（开篇实测因此卡死在 opening 阶段）。
        block = text
    opts = []
    for raw_line in block.splitlines():
        line = Game.normalize(raw_line)
        m = _OPT_LINE_RE.match(line)
        if m:
            key = m.group(1).upper()
            val = m.group(2).strip()
            if val and not any(o["key"] == key for o in opts):
                opts.append({"key": key, "text": val})
    return opts if len(opts) >= 2 else None


def _extract_json_bible(text):
    """从文本提取 bible JSON dict。先找 ```json 块，再回退到平衡括号扫描。"""
    if not text:
        return None
    # 2026-10-10 首测：worker 常写 Python 风格的 \' 转义（JSON 里非法），
    # 导致整块解析失败、游戏卡死。先清洗再解析。
    text = text.replace("\\'", "'")
    candidates = []
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if m:
        candidates.append(m.group(1))
    # 回退：平衡括号扫描，找最长的顶层 {...}
    spans = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            depth = 0
            in_str = False
            esc = False
            j = i
            while j < n:
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            spans.append(text[i:j + 1])
                            break
                j += 1
            i = j + 1 if j < n and depth == 0 else i + 1
        else:
            i += 1
    spans.sort(key=len, reverse=True)
    candidates.extend(spans)
    for c in candidates:
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                return d
        except Exception:
            continue
    return None


def parse_chapter_output(text):
    """解析 worker 章节输出 → (options, bible_raw_dict)。

    任一缺失/非法返回 (None, None)，调用方走重发兜底。
    """
    opts = parse_options(text)
    block = _extract_block(text, "大纲")
    raw = _extract_json_bible(block or "")
    if raw is None:
        raw = _extract_json_bible(text or "")
    if not opts or not isinstance(raw, dict):
        return None, None
    return opts, raw


def parse_ending(text):
    """解析【结局：xxx】与【最佳转折：人名 - 理由】。

    返回 (ending_name, {"name","reason","raw"})，失败返回 (None, None)。
    """
    m1 = re.search(r"【结局[：:]\s*([^】]+?)】", text or "")
    m2 = re.search(r"【最佳转折[：:]\s*([^】]+?)】", text or "")
    if not m1 or not m2:
        return None, None
    ending = m1.group(1).strip()
    raw = m2.group(1).strip()
    m3 = re.match(r"(.+?)\s*[-—：:]\s*(.+)", raw)
    if m3:
        name, reason = m3.group(1).strip(), m3.group(2).strip()
    else:
        name, reason = raw, ""
    if not ending or not name:
        return None, None
    return ending, {"name": name, "reason": reason, "raw": raw}


def _strip_blocks(text):
    """去掉格式块，得到纯故事正文（存 chapters[] / 算字数用）。"""
    t = re.sub(r"【选项】.*?(?=【|$)", "", text or "", flags=re.S)
    t = re.sub(r"【大纲】.*?(?=【|$)", "", t, flags=re.S)
    t = re.sub(r"```json.*?```", "", t, flags=re.S)
    t = re.sub(r"【结局[：:][^】]*】", "", t)
    t = re.sub(r"【最佳转折[：:][^】]*】", "", t)
    return t.strip()


# ---------- bible（文档 §2.1，Python 归一化 + 拥有累计字段） ----------

def new_bible(story_id=""):
    return {
        "story_id": story_id,
        "bible_version": 1,
        "title": "",
        "genre": "",
        "tone": "",
        "round": 0,
        "min_rounds_before_ending": MIN_ROUNDS_BEFORE_ENDING,
        "max_rounds": DEFAULT_MAX_ROUNDS,
        "setting": {"world": "", "era": "", "location": "", "rules": []},
        "characters": [],
        "plot_threads": [],
        "foreshadows": [],
        "choices_ledger": [],
        "tendencies": {d: 0 for d in TENDENCY_DIMS},
        "current_goal": "",
        "style_profile": {"pov": "第二人称复数（你们）", "density": "短句为主",
                          "dialogue_style": "口语化"},
        "feedback_tuning": {"spotlight": {}},
        "event_table": list(DEFAULT_EVENTS),
    }


def _as_str_list(v, limit=10, each=120):
    if not isinstance(v, list):
        return []
    out = []
    for x in v[:limit]:
        s = str(x).strip()
        if s:
            out.append(s[:each])
    return out


def normalize_bible(raw, prev, state):
    """把 worker 的 bible JSON 归一化为文档 §2.1 结构。

    - setting/characters/plot_threads/foreshadows/current_goal 等取 worker 的；
    - tendencies：prev 累计值 + worker 的 tendencies_delta（±2 钳制，±5 上限）；
    - choices_ledger / round / min/max_rounds / story_id：Python 拥有，worker 写了也覆盖；
    - event_table：worker 可按题材提供 8~10 条，否则用默认表。
    返回 (bible, delta)。
    """
    raw = raw if isinstance(raw, dict) else {}
    prev = prev if isinstance(prev, dict) else {}
    b = new_bible(state.get("story_id", ""))

    for k in ("title", "genre", "tone", "current_goal"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            b[k] = v.strip()[:200]

    s = raw.get("setting")
    if isinstance(s, dict):
        for k in ("world", "era", "location"):
            if isinstance(s.get(k), str):
                b["setting"][k] = s[k].strip()[:300]
        b["setting"]["rules"] = _as_str_list(s.get("rules"), 8, 120)

    chars = raw.get("characters")
    if isinstance(chars, list):
        for c in chars[:8]:
            if not isinstance(c, dict):
                continue
            nc = {"name": str(c.get("name", ""))[:20],
                  "role": str(c.get("role", "NPC"))[:10],
                  "personality": str(c.get("personality", ""))[:60],
                  "motivation": str(c.get("motivation", ""))[:120],
                  "secret": str(c.get("secret", ""))[:200],
                  "inner_voice": str(c.get("inner_voice", ""))[:120]}
            try:
                nc["attitude"] = max(0, min(6, int(c.get("attitude", 3))))
            except (TypeError, ValueError):
                nc["attitude"] = 3
            if nc["name"]:
                b["characters"].append(nc)

    threads = raw.get("plot_threads")
    if isinstance(threads, list):
        for t in threads[:8]:
            if not isinstance(t, dict):
                continue
            st = str(t.get("status", "active"))
            if st not in ("active", "resolved", "abandoned"):
                st = "active"
            b["plot_threads"].append(
                {"id": str(t.get("id", ""))[:10],
                 "desc": str(t.get("desc", ""))[:200], "status": st})

    fss = raw.get("foreshadows")
    if isinstance(fss, list):
        for f in fss[:12]:
            if not isinstance(f, dict):
                continue
            st = str(f.get("status", "planted"))
            if st not in ("planted", "paid_off"):
                st = "planted"
            nf = {"id": str(f.get("id", ""))[:10],
                  "planted_in_round": f.get("planted_in_round", 0),
                  "hint": str(f.get("hint", ""))[:200],
                  "payoff_plan": str(f.get("payoff_plan", ""))[:200],
                  "status": st,
                  "paid_off_in_round": f.get("paid_off_in_round")}
            try:
                nf["planted_in_round"] = int(nf["planted_in_round"] or 0)
            except (TypeError, ValueError):
                nf["planted_in_round"] = 0
            if nf["hint"]:
                b["foreshadows"].append(nf)

    sp = raw.get("style_profile")
    if isinstance(sp, dict):
        for k in ("pov", "density", "dialogue_style"):
            if isinstance(sp.get(k), str) and sp[k].strip():
                b["style_profile"][k] = sp[k].strip()[:80]

    ft = raw.get("feedback_tuning")
    if isinstance(ft, dict) and isinstance(ft.get("spotlight"), dict):
        b["feedback_tuning"]["spotlight"] = {
            str(k)[:20]: float(v) for k, v in
            list(ft["spotlight"].items())[:8]}

    et = raw.get("event_table")
    if isinstance(et, list) and et:
        evs = [str(x).strip()[:60] for x in et[:10] if str(x).strip()]
        if evs:
            b["event_table"] = evs

    # tendencies：累计值，worker 只给 delta
    tend = dict(prev.get("tendencies") or
                {d: 0 for d in TENDENCY_DIMS})
    delta = {}
    raw_delta = raw.get("tendencies_delta")
    if isinstance(raw_delta, dict):
        for k, v in raw_delta.items():
            if k in TENDENCY_DIMS and isinstance(v, int) and v != 0:
                dv = max(-2, min(2, v))
                tend[k] = max(-5, min(5, tend.get(k, 0) + dv))
                delta[k] = dv
    b["tendencies"] = tend

    # Python 拥有的字段：worker 写了也覆盖
    b["round"] = state.get("round", 0)
    b["min_rounds_before_ending"] = MIN_ROUNDS_BEFORE_ENDING
    b["max_rounds"] = state.get("max_rounds", DEFAULT_MAX_ROUNDS)
    if isinstance(prev.get("choices_ledger"), list):
        b["choices_ledger"] = prev["choices_ledger"]
    return b, delta


# ---------- 合成消息查询 ----------

def _is_sent(status):
    """合成消息是否已发出（可推进游戏）。
    2026-10-10 首测：sendd 验证不确定时会标 SEND_UNCERTAIN（实际已发出，
    用户都投票了），tick 若只认 SENT 会永远卡住。两者都视为已发出。"""
    return status in ("SENT", "SEND_UNCERTAIN")


def _game_start_msg(db, game_id):
    """本局 GAME_START 合成消息（开篇，worker 回填在 reply_content）。"""
    row = db.execute(
        """SELECT m.id, m.reply_content, m.status, m.sent_at
           FROM game_events e
           JOIN message_game_link l ON l.event_id = e.id
               AND l.kind = 'GAME_START'
           JOIN messages m ON m.id = l.msg_id
           WHERE e.game_id = ? AND e.event_type = 'GAME_START'
           ORDER BY e.id DESC LIMIT 1""", (game_id,)).fetchone()
    if not row:
        return None
    return {"event_id": 0, "msg_id": row[0], "reply_content": row[1] or "",
            "status": row[2], "sent_at": row[3] or 0}


def _latest_tick_by_phases(db, game_id, phases):
    """本局最新的、facts.phase 在 phases 中的 GAME_TICK 事件及其消息。"""
    rows = db.execute(
        """SELECT e.id, e.content, m.id, m.reply_content, m.status, m.sent_at
           FROM game_events e
           LEFT JOIN message_game_link l ON l.event_id = e.id
               AND l.kind = 'GAME_TICK'
           LEFT JOIN messages m ON m.id = l.msg_id
           WHERE e.game_id = ? AND e.event_type = 'GAME_TICK'
           ORDER BY e.id DESC""", (game_id,)).fetchall()
    want = set(phases)
    for eid, content, mid, rc, st, sat in rows:
        try:
            facts = json.loads(content or "{}")
        except Exception:
            continue
        if facts.get("phase") in want:
            return {"event_id": eid, "msg_id": mid,
                    "reply_content": rc or "", "status": st,
                    "sent_at": sat or 0}
    return None

# ---------- prompt 模板（文档 §7，可直接套用；{{...}} 为程序填充占位符） ----------

OPENING_INSTRUCTION = """你是"雾港说书人"，专精互动小说的 AI 主持人。
任务：为微信群友创作一局多结局互动小说的【开篇】。

题材：由你定一个最适合群氛围的（悬疑 / 科幻 / 武侠 / 校园怪谈…），基调冷峻克制、带黑色幽默。

铁律（违反会被打回重写）：
1. 你只写世界、环境、NPC 的言行和正在发生的事；绝不替群友决定"我们"做了什么、说了什么、想了什么。
2. 第二人称复数（"你们"）叙述；多感官细节（看见/听见/闻到/触感），不要干巴巴的说明文。
3. 颠覆类型惯例，拒绝俗套反转；反转必须有铺垫——铺垫就是你本篇要埋的伏笔。
4. 本轮埋下至少 2 个伏笔（具体物件 / 反常细节 / 欲言又止的 NPC 话语）。

输出结构（严格遵守。带【】的行是给程序解析的标记，一字不改；```json 块必须是合法 JSON）：
《标题》· 开篇
[正文 150~300 字，短句分段。必须包含：世界锚（时间/地点/核心异常，50~80 字讲清"这是什么世界"、为什么值得讲）；主角团（"你们"）+ 1~2 个关键 NPC 登场（各一句话人设钩子）；核心悬念（本局要解开的大问题）]
---
[钩子句，≤30 字，单独一行，戛然而止]
---
【选项】
A. 稳妥策略（≤25 字）
B. 激进策略（≤25 字；高风险项必须标注"需检定·简单/标准/困难/极难"）
C. 横向思维（≤25 字；建议挂钩某个伏笔）
D. 按{{群友名}}的风格（见本 prompt 末尾【群友风格速查】，有档案以档案为准）：一句性格描述 + 具体行动（≤25 字，人物专属）
投票3分钟：直接发 A/B/C/D，或自由发言（自由发言优先采用，多条时说书人选最有意思的）
---
【大纲】
```json
{
  "title": "故事标题", "genre": "题材", "tone": "基调",
  "setting": {"world": "一句话世界观", "era": "时代", "location": "当前地点",
              "rules": ["世界规则1", "规则2"]},
  "characters": [
    {"name": "名字", "role": "NPC", "personality": "性格关键词",
     "motivation": "他真正想要什么", "secret": "他藏着的秘密（群友未知）",
     "inner_voice": "内心独白一句（定说话语气）", "attitude": 3}
  ],
  "plot_threads": [{"id": "t1", "desc": "主线描述", "status": "active"}],
  "foreshadows": [
    {"id": "f1", "planted_in_round": 0, "hint": "埋了什么",
     "payoff_plan": "计划第几轮如何回收", "status": "planted"}
  ],
  "tendencies": {"courage": 0, "caution": 0, "trust": 0, "ruthless": 0, "curiosity": 0},
  "current_goal": "下一章目标（一句话）",
  "style_profile": {"pov": "第二人称复数", "density": "短句为主", "dialogue_style": "口语化"}
}
```

好开篇范例（学它的节奏和信息量，不要抄内容）：
《雾港灯塔》· 开篇
你们站在雾港码头，咸腥的海风把灯塔的光搅碎。灯塔本该午夜熄灭，可它已经连续亮了第十三个夜晚。守塔人老陈蹲在缆绳旁，怀表停在 3:07，一句话也不说。
---
可今晚，灯塔的光第一次照向了你们。
---
【选项】
A. 退回大厅清点人数（≤25 字）
B. 直接撬开地下室的门（需检定·困难）
---
"""

CONTINUE_INSTRUCTION = """你是"雾港说书人"，继续主持本局互动小说。先做 chain-of-thought，再写正文。

【本局唯一事实来源，一切以此为准；不要依赖"我记得上文"】
```json
{{bible}}
```

【本轮输入】
- 上轮投票结果：{{chosen}}（得票 {{tally}}；{{auto_note}}）
- 检定结果：{{check}}（若所选选项标注"需检定·难度"：d20={{roll}}，难度{{difficulty}}（{{difficulty_name}}），{{result}}，差值{{margin}}。按差值写叙事：大成功（差值≥5）给额外奖励叙事，大失败（差值≤-5）给有代价的转折；不许改结果，不许写死主角团）
- 随机事件：{{random_event}}（程序掷骰结果；若为 null 则无。若有，把它自然融进正文开头 1~2 句，不要单独宣布"触发了随机事件"）
- 热记忆（最近 2 轮正文）：{{hot_memory}}
- 温记忆（更早轮次一句话摘要）：{{warm_memory}}
- 本轮是否安排道德两难：{{dilemma}}（为真时，选项必须是一次真两难：两个选项代价都明确、无完美答案、选哪个都会失去点什么）
- 是否收线轮：{{closing}}（为真时：本章必须回收≥1 个 planted 伏笔，不许新埋伏笔）
- 允许提前结局：{{allow_early_ending}}（为真时，你可在选项末加一行"E. 进入结局（剧情已成熟）"，群友投票 E 得票最高则下轮直接进入结局）

【动笔前先在心里过一遍一致性自检，任一条不满足就调整大纲】
1. 设定一致：不违反 setting.rules；
2. 人物一致：NPC 言行符合 personality/motivation/secret，不突然变脸；
3. 时间线一致：不复活已死角色、不凭空出现已消耗的道具；
4. 伏笔账：回收≥1 个 planted 伏笔或新埋 1 个（收线轮只许回收）；
5. 因果一致：本章的"果"必须能在 bible 里找到"因"。

【写作要求】
1. 先列 3~5 条本章大纲（心里列，不输出），再写正文 150~300 字。
2. 写投票选定方向的行动结果（世界反应 + NPC 行动）；只写世界和 NPC，不替群友做新的决定；NPC 说话用引号，直接、有性格（按 inner_voice 的语气写）。
3. 选项设计：A 稳妥 / B 激进（高风险标"需检定·难度"）/ C 横向（挂钩伏笔）/ D 人物专属（按本 prompt 末尾【群友风格速查】写，有档案以档案为准）；每项≤25 字。
   反面清单（出现即打回重写）：不许"选 X 直接通关"的偷懒选项；不许三个选项殊途同归；不许在选项文字里剧透隐藏代价；不许选项与 bible 冲突；D 项不许是无意义的摆烂项；连续两轮选项分工不许完全相同。
4. 章节标题：第{{n}}章·四字短语。结尾钩子 ≤30 字。

【输出结构（严格遵守）】
第{{n}}章·四字短语
[正文 150~300 字，短句分段]
---
[钩子句 ≤30 字]
---
【选项】
A. …（≤25 字）
B. …（≤25 字）
C. …（≤25 字）
D. …（≤25 字）
投票3分钟：直接发 A/B/C/D，或自由发言（自由发言优先采用，多条时说书人选最有意思的）
---
【大纲】
```json
{ 更新后的 bible JSON：foreshadows 状态更新（planted/paid_off，写 paid_off_in_round）；characters 态度/状态变化；current_goal 写下一章目标；顶层加 "tendencies_delta": {"courage": 1}（本轮投票映射的倾向变化，每项 -2~2 的整数；不要改 tendencies 绝对值，程序会累加）；event_table（如需按题材更新事件表，8~10 条） }
```
"""

# 2026-10-10 用户要求：自由发言优先。有多条时由 worker 选最有意思的一条并说明理由。
CUSTOM_PICK_INSTRUCTION_TMPL = """
【自由发言优先】
本轮有 {n} 条自由发言（按时间排序），它们优先于 A/B/C/D 投票：
{customs_list}
投票情况（仅回退用）：{tally}；回退选项：{fallback}
处理流程：
1. 逐条判断是否为有效剧情想法；纯闲聊（"哈哈哈""有意思"之类）、表情、无意义内容直接忽略。
2. 有有效想法 → 选最有意思的一条。标准：意外性、戏剧张力、能推动剧情或制造两难；不要选最稳妥的那个。
3. 章节开头（标题之前）加一行：💡 本轮采用 @XXX 的想法「……」（引用≤30字），理由：……（为什么选它，≤40字）。
4. 正文按这个想法的方向写（世界反应 + NPC 行动）。
5. 如果全是无效闲聊 → 按回退选项推进，开头提一句"本轮想法都是闲聊，按投票走"。
"""

ENDING_INSTRUCTION = """你是"雾港说书人"，本局进入结局轮。收束一切，回收所有伏笔。

【本局唯一事实来源】
```json
{{bible}}
```

【结局权重（程序按倾向/伏笔/NPC 好感算出，仅供参考）】
{{ending_weights}}
- 你可以偏离最高权重，但必须在正文里给足人物动机（不能硬拗）。
- 真结局要求已回收伏笔 ≥2；planted 状态不许留空——每个没回收的伏笔都必须在本章回收或给出交代。

【写作要求】
1. 结局标题：《{{title}}》· 结局·{{结局名}}（如"结局·雾散（真相线）"）。
2. 正文 300~400 字：按选定结局线收束人物命运；主要 NPC 的 secret 在此揭晓或点到为止；呼应开篇的核心悬念；悲剧结局也要有文学性，不许潦草。
3. 尾声 1~2 句：故事之后的世界，留余味。
4. 输出【结局：xxx】（结局名 4~10 字）与【最佳转折：人名 - 理由】（从投票/自定义方向的提出者中评选本局最佳剧情转折）。

【输出结构（严格遵守）】
《{{title}}》· 结局·{{结局名}}
[正文 300~400 字]
---
[尾声 1~2 句]
---
【结局：{{结局名}}】
【最佳转折：人名 - 一句话理由】
---
【大纲】
```json
{ bible 最终版：所有 foreshadows 状态为 paid_off（写 paid_off_in_round）；plot_threads 全 resolved/abandoned；加 "story_summary": "一句话梗概" }
```
"""

REEMIT_INSTRUCTION = """上一条输出的格式解析失败。请只重新输出【选项】块与【大纲】块（【大纲】内含 ```json bible JSON），严格按要求的格式（一字不改标记），不要输出其他内容。"""

REEMIT_ENDING_INSTRUCTION = """结局标记解析失败。请只重新输出【结局：xxx】与【最佳转折：人名 - 理由】两块，严格按格式，不要输出其他内容。"""

VOTING_OPEN_INSTRUCTION = """开篇已发出。请用 1~2 句话向群友说明投票规则：直接发 A/B/C/D 投票支持某个走向，或用一句话（50 字内）说出你希望的剧情走向（自由发言优先采用；多条自由发言时说书人选最有意思的一条并说明理由）；投票窗口约 3 分钟，无自由发言时得票最高的方向获胜，平票掷骰决定；若无人投票则按最稳妥的选项推进。不要复述开篇剧情。"""


def _recent_story_themes(db, chat_id, limit=5):
    """查最近 N 局故事的标题+题材，供开局去重（2026-10-10 用户要求：不许重复故事）。"""
    import json
    rows = db.execute(
        """SELECT e.content FROM game_events e
           JOIN games g ON g.id = e.game_id
           WHERE g.chat_id=? AND g.game_type='story' AND e.event_type='GAME_START'
           ORDER BY e.id DESC LIMIT ?""",
        (chat_id, limit)).fetchall()
    themes = []
    for (c,) in rows:
        try:
            facts = json.loads(c or "{}")
            # bible 可能在 facts 里，也可能在 state 里
            bible = facts.get("bible") or {}
            title = bible.get("title", "")
            genre = bible.get("genre", "")
            if title or genre:
                themes.append(f"{title}（{genre}）" if title and genre else (title or genre))
        except Exception:
            pass
    return themes


class StoryGame(Game):
    game_type = "story"
    name = "互动故事"
    duration_sec = DURATION_SEC

    # ---------- 开局 ----------

    def start(self, ctx):
        # 测试群 gate：只允许在 STORY_TEST_CHATS 开局。
        # 切换方法：把群 chat_id 加进 STORY_TEST_CHATS（本文件顶部），
        # 或放开为 GAMES_ALLOWED_CHATS。返回 state=None 即开局拒绝。
        if ctx.get("chat_id") not in STORY_TEST_CHATS:
            return None, {"declined": "test_only"}

        db = ctx["db"]
        game_id = ctx["game_id"]
        speakers = recent_speakers(db, ctx["chat_id"], hours=24, limit=8)
        players = {s["user_id"]: s["display_name"] for s in speakers}
        # 预登记玩家进 game_players（记 games_played，供 /game status 用）
        from . import store as gstore
        for uid, nm in players.items():
            gstore.add_player(db, game_id, uid, nm, commit=False)

        # max_rounds 可配（经 manager.start_game(config=...) 传入），
        # 下限 6（用户硬要求：至少 6 轮才允许进结局），上限 10（文档 §5.3）。
        cfg = ctx.get("config") or {}
        try:
            max_rounds = int(cfg.get("max_rounds", DEFAULT_MAX_ROUNDS))
        except (TypeError, ValueError):
            max_rounds = DEFAULT_MAX_ROUNDS
        max_rounds = max(MIN_ROUNDS_BEFORE_ENDING,
                         min(MAX_ROUNDS_CAP, max_rounds))

        state = {
            "story_id": f"story_{game_id}",
            "phase": "opening",
            "round": 0,                 # 已完成的续写轮数（开篇=0）
            "max_rounds": max_rounds,
            "bible": new_bible(f"story_{game_id}"),
            "chapters": [],             # 每章 worker 原文（去格式块）
            "options": [],              # 当前轮选项 [{key, text}]
            "votes": {},                # user_id -> {name, choice, ts}
            "custom": {},               # user_id -> {name, text, ts}
            "vote_counts": {},          # user_id -> {name, votes}（MVP 用）
            "vote_deadline": 0,
            "deadline_bonus": 0,        # 投票延长（每轮限 1 次 +120s）
            # 当前投票章节来源：start=GAME_START 消息，tick=continue-TICK 消息
            "chapter_source": "start",
            "players": players,         # user_id -> display_name
            "starter": "",
            "landslide_streak": 0,      # 连续碾压轮数（随机事件加频率用）
            "crit_fail_streak": 0,      # 连续大失败（坏结局条件用）
            "crit_fail_count": 0,
            "reemit_count": 0,
            "reemit_consumed": 0,
            "ending_consumed": 0,
            # 供 finish() 结算：{ending_name, twist_name, twist_reason, twist_uid}
            "last_ending": None,
        }
        state["bible"]["max_rounds"] = max_rounds
        # 2026-10-10 用户要求：故事不许重复，查最近题材让 worker 避开
        avoid = _recent_story_themes(db, ctx["chat_id"], limit=5)
        worker_instr = _with_style_block(
            OPENING_INSTRUCTION, list(players.values()))
        if avoid:
            worker_instr = (
                f"【去重硬要求】最近已讲过的故事（绝不许重复题材/设定/核心悬念）：\n"
                + "\n".join(f"- {t}" for t in avoid)
                + "\n必须另起一个完全不同的题材和世界观。\n\n"
                + worker_instr
            )
        facts = {
            "round": 0,
            "max_rounds": max_rounds,
            "players": list(players.values()),
            "vote_window_sec": VOTE_WINDOW_SEC,
            # 2026-10-10：D 选项按风格档案写，不让 LLM 瞎猜
            "worker_instruction": worker_instr,
        }
        return state, facts

    # ---------- 输入识别 ----------

    def accept_message(self, msg, state):
        if msg.get("sender") == GAME_SYSTEM_SENDER:
            return False
        # 2026-10-10 修：空 sender（bot 自己的合成回复）也不接受，防自投票
        if not msg.get("sender"):
            return False
        # opening 也接受：开篇发出后、首个 tick 解析完成前的几分钟内，
        # 玩家对着开篇消息的投票不能丢（静默计票，不 suppress 原消息）。
        if state.get("phase") not in ("opening", "voting"):
            return False
        # 2026-10-10 用户要求：所有游戏统一 @bot+内容 才算有效输入
        content = msg.get("content", "")
        if "@" not in content:
            return False
        text = self.normalize(_AT_RE.sub("", content))
        if not text:
            return False
        if _VOTE_RE.match(text):
            # E 只有 worker 在本轮提供了"进入结局"项、且已达最小轮数时才有效
            # （防 worker 提前放 E 导致 6 轮前结束）
            if text.upper() == "E":
                offered_e = any(o.get("key") == "E"
                                for o in state.get("options", []))
                if not offered_e or \
                        state.get("round", 0) + 1 < MIN_ROUNDS_BEFORE_ENDING:
                    return False
            return True
        if text.startswith("/"):
            return False
        # 自定义方向：一句话，≤50 字（静默计票；零 A-E 票时才作为兜底方向）
        # 2026-10-10 用户反馈：纯字母/符号串如 "D E FG" 不是有效剧情方向，拒掉
        #（大概率是误触的投票字母，不是正经发言；正经方向至少得有中文）
        import re as _re
        if not _re.search(r"[\u4e00-\u9fff]", text):
            return False
        return len(text) <= 50

    def invalid_hint_text(self):
        return ("投票请 @我：A / B / C / D（对应剧情选项）；"
                "或 @我 用一句话（50 字内）说出你希望的剧情走向")

    # ---------- 处理 ----------

    def handle_message(self, msg, game, ctx):
        from . import store as gstore
        state = game["state"]
        user_id = msg["sender"]
        name = msg.get("sender_name") or user_id
        now = ctx.get("now", int(time.time()))
        # 2026-10-10：@bot+投票/发言，剥掉 @ 再识别
        text = self.normalize(_AT_RE.sub("", msg.get("content", "")))

        players = state.setdefault("players", {})
        if user_id not in players:
            players[user_id] = name
            gstore.add_player(ctx["db"], game["id"], user_id, name,
                              commit=False)
        else:
            players[user_id] = name  # 刷新昵称

        vc = state.setdefault("vote_counts", {})
        vce = vc.setdefault(user_id, {"name": name, "votes": 0})
        vce["name"] = name
        vce["votes"] += 1

        # 投票延长（文档 §4.1）：窗口内发"等一下/再想想"等，延长 1 次 2 分钟
        if (state.get("phase") == "voting" and text in _EXTEND_KEYWORDS
                and not state.get("deadline_bonus")):
            state["deadline_bonus"] = EXTEND_SEC
            return {
                "event_type": "STORY_EXTEND",
                "reply_needed": False,
                "facts": {"player": name, "extend_sec": EXTEND_SEC,
                          "round": state.get("round", 0)},
                "suppress_original": False,
                "new_state": state,
                "points": [], "wins": [],
                "finish_now": False,
            }

        if _VOTE_RE.match(text):
            # 2026-10-10 用户要求：投票只收第一次，后续改票静默忽略（不覆盖）
            if user_id in state.get("votes", {}):
                return {
                    "event_type": "STORY_VOTE_DUP",
                    "reply_needed": False,
                    "facts": {"player": name, "vote": text.upper(),
                              "round": state.get("round", 0),
                              "already": True},
                    "suppress_original": False,
                    "new_state": state,
                    "points": [], "wins": [],
                    "finish_now": False,
                }
            state.setdefault("votes", {})[user_id] = {
                "name": name, "choice": text.upper(), "ts": now}
            event_type, facts = "STORY_VOTE", {
                "player": name, "vote": text.upper(),
                "round": state.get("round", 0)}
        else:
            # 自定义方向：一人一条，取最后一条
            # 2026-10-10 用户要求：有自由发言则优先采纳（_count_votes 已实现）
            state.setdefault("custom", {})[user_id] = {
                "name": name, "text": text, "ts": now}
            event_type, facts = "STORY_CUSTOM", {
                "player": name, "custom": text,
                "round": state.get("round", 0)}

        # 静默计票：不回复、不 suppress 原消息（避免刷屏）
        return {
            "event_type": event_type,
            "reply_needed": False,
            "facts": facts,
            "suppress_original": False,
            "new_state": state,
            "points": [], "wins": [],
            "finish_now": False,
        }

    # ---------- tick ----------

    def tick(self, game, ctx):
        now = ctx.get("now", int(time.time()))
        state = game["state"]
        phase = state.get("phase", "opening")

        # 自然超时：直接 END。积分在 finish() 里统一结算（防 double count）。
        if now >= game["ends_at"]:
            state["phase"] = "done"
            return {"kind": "END",
                    "facts": self._end_facts(game, "timeout"),
                    "new_state": state, "points": [], "wins": []}

        if phase == "opening":
            return self._tick_opening(game, ctx, state, now)
        if phase == "voting":
            return self._tick_voting(game, ctx, state, now)
        if phase == "ending":
            return self._tick_ending(game, ctx, state, now)
        return None

    def _tick_opening(self, game, ctx, state, now):
        """等 worker 写完开篇 → 解析【选项】+ bible JSON → 转 voting。"""
        db = ctx["db"]
        game_id = game["id"]
        # 是否有未消费的 reemit 回复；有未发送的 reemit 则等它，不重复发
        reemit = _latest_tick_by_phases(db, game_id, ("reemit",))
        m = None
        from_reemit = False
        if (reemit and reemit["msg_id"]
                and state.get("reemit_consumed") != reemit["event_id"]):
            if not _is_sent(reemit["status"]):
                return None
            m, from_reemit = reemit, True
        else:
            m = _game_start_msg(db, game_id)
        if not m or not m["msg_id"] or not _is_sent(m["status"]):
            return None  # worker 还没写完，等下一个 tick

        opts, raw = parse_chapter_output(m["reply_content"])
        if not opts or not isinstance(raw, dict):
            return self._reemit(game, state, "opening")

        bible, _delta = normalize_bible(raw, state["bible"], state)
        state["bible"] = bible
        state["options"] = opts
        state["chapters"] = [_strip_blocks(m["reply_content"])]
        state["round"] = 0
        state["phase"] = "voting"
        state["chapter_source"] = "start"
        state["vote_deadline"] = (m["sent_at"] or now) + VOTE_WINDOW_SEC
        state["deadline_bonus"] = 0
        state["reemit_count"] = 0
        if from_reemit:
            state["reemit_consumed"] = m["event_id"]

        facts = {
            "phase": "voting_open",
            "round": 0,
            "max_rounds": state["max_rounds"],
            "options": opts,
            "vote_deadline": state["vote_deadline"],
            "vote_window_sec": VOTE_WINDOW_SEC,
            "worker_instruction": VOTING_OPEN_INSTRUCTION,
        }
        return {"kind": "TICK", "facts": facts, "new_state": state,
                "points": [], "wins": []}

    def _tick_voting(self, game, ctx, state, now):
        """核心：等投票窗口结束（或全员投完）→ 解析本轮章节 → 计票 → 推进/结局。"""
        db = ctx["db"]
        game_id = game["id"]

        m = None
        from_reemit = False
        if state.get("chapter_source") == "start":
            m = _game_start_msg(db, game_id)
            deadline = (state.get("vote_deadline", 0)
                        + state.get("deadline_bonus", 0))
            # 开篇也可能走过 reemit：有更新的重发回复则用它
            reemit = _latest_tick_by_phases(db, game_id, ("reemit",))
            if (reemit and reemit["msg_id"]
                    and state.get("reemit_consumed") != reemit["event_id"]):
                if not _is_sent(reemit["status"]):
                    return None  # 等 worker 重发
                m, from_reemit = reemit, True
                deadline = 0  # 重发成功后立即推进，不再等窗口
        else:
            cont = _latest_tick_by_phases(db, game_id, ("continue",))
            if not cont or not cont["msg_id"]:
                return None
            reemit = _latest_tick_by_phases(db, game_id, ("reemit",))
            if (reemit and reemit["msg_id"]
                    and reemit["event_id"] > cont["event_id"]
                    and state.get("reemit_consumed") != reemit["event_id"]):
                if not _is_sent(reemit["status"]):
                    return None  # 等 worker 重发
                m, from_reemit = reemit, True
                deadline = 0
            else:
                if not _is_sent(cont["status"]):
                    return None  # worker 还在写章节
                m = cont
                deadline = ((m["sent_at"] or now) + VOTE_WINDOW_SEC
                            + state.get("deadline_bonus", 0))

        if not m or not m["msg_id"]:
            return None
        # 投票窗口内且未全员投完 → 静默等待
        if not from_reemit and now < deadline \
                and not self._all_voted(state):
            return None

        text = m["reply_content"] or ""
        opts, raw = parse_chapter_output(text)
        if not opts or not isinstance(raw, dict):
            return self._reemit(game, state, "voting")

        # 解析成功：归一化 bible（应用 tendencies_delta，保留累计字段）
        bible, delta = normalize_bible(raw, state["bible"], state)
        state["bible"] = bible
        state["options"] = opts
        # 2026-10-10 修：chapter_source=="start" 时 text 是开篇正文，
        # _tick_opening 已将其放入 chapters，此处不再重复 append（防 Chapter 0/1 重复）
        if state.get("chapter_source") != "start":
            state["chapters"].append(_strip_blocks(text))
        if from_reemit:
            state["reemit_consumed"] = m["event_id"]
        state["reemit_count"] = 0

        # ---- 计票（文档 §4.1/§4.2）----
        chosen, tally, auto_safe, early_ending = self._count_votes(
            state, opts, game_id)

        new_round = state.get("round", 0) + 1

        # ---- 检定（文档 §4.5）：所选选项若标"需检定·难度"，程序掷 d20 ----
        check = self._roll_check(game_id, new_round, chosen)
        if check:
            if check["result"] == "crit_fail":
                state["crit_fail_streak"] = \
                    state.get("crit_fail_streak", 0) + 1
                state["crit_fail_count"] = \
                    state.get("crit_fail_count", 0) + 1
            else:
                state["crit_fail_streak"] = 0

        # ---- 轻量随机事件（文档 §4.4）：程序掷 d100 ----
        event_roll, event_text = self._roll_event(game_id, new_round, state)

        # ---- 碾压追踪（连续两轮一边倒 → 下轮事件触发率 40%）----
        tot_ad = sum(tally.values())
        landslide = bool(tally) and tot_ad >= 3 and \
            max(tally.values()) / tot_ad >= 0.75
        state["landslide_streak"] = state.get("landslide_streak", 0) + 1 \
            if landslide else 0

        state["round"] = new_round
        bible = state["bible"]
        bible["round"] = new_round
        # 2026-10-10：自由发言优先时，worker 从多条中选一条；ledger 记全量，
        # 具体选了哪条以章节正文开头的宣布为准。
        if chosen.get("custom_choice"):
            entry = {
                "round": new_round,
                "option": "custom_pick",
                "text": "自由发言 %d 条，worker 择优" % len(
                    chosen.get("customs", [])),
                "by": "",
                "free_text": None,
                "customs": chosen.get("customs", []),
                "fallback": chosen.get("fallback", {}),
                "tally": tally,
                "auto_safe": auto_safe,
                "event_roll": event_roll,
                "event": event_text,
                "check": check,
                "tendencies_delta": delta,
            }
        else:
            entry = {
                "round": new_round,
                "option": chosen.get("key") or "custom",
                "text": chosen.get("text") or chosen.get("custom", ""),
                "by": chosen.get("by", ""),
                "free_text": chosen.get("custom"),
                "tally": tally,
                "auto_safe": auto_safe,
                "event_roll": event_roll,
                "event": event_text,
                "check": check,
                "tendencies_delta": delta,
            }
        bible["choices_ledger"].append(entry)
        state["votes"] = {}
        state["custom"] = {}
        state["vote_counts"] = {}  # 2026-10-10：每轮清空，防跨轮累积误判
        state["deadline_bonus"] = 0
        state["chapter_source"] = "tick"

        tied_n = 0
        if tally:
            _top = max(tally.values())
            tied_n = sum(1 for v in tally.values() if v == _top)

        # ---- 是否进结局 ----
        # E 提前结局（文档 §5.3：6~7 轮 worker 可提供 E 项），或达到 max_rounds
        if early_ending or new_round >= state.get("max_rounds",
                                                  DEFAULT_MAX_ROUNDS):
            state["phase"] = "ending"
            facts = {
                "phase": "ending",
                "round": new_round,
                "max_rounds": state["max_rounds"],
                "bible": bible,
                "hot_memory": self._story_so_far(state, 3),
                "ending_weights": self._ending_weights(state),
                "choice_ledger": bible["choices_ledger"],
                "player_names": list(state.get("players", {}).values()),
                "worker_instruction": ENDING_INSTRUCTION,
            }
            return {"kind": "TICK", "facts": facts, "new_state": state,
                    "points": [], "wins": []}

        # 2026-10-10：自由发言优先。有多条时 worker 选最有意思的一条并说明理由。
        worker_instruction = CONTINUE_INSTRUCTION
        if chosen.get("custom_choice"):
            customs_list = "\n".join(
                "- @%s：%s" % (c.get("by", ""), c.get("text", ""))
                for c in chosen.get("customs", []))
            fb = chosen.get("fallback", {}) or {}
            worker_instruction += CUSTOM_PICK_INSTRUCTION_TMPL.format(
                n=len(chosen.get("customs", [])),
                customs_list=customs_list or "（无）",
                tally=tally or "无投票",
                fallback="%s %s" % (fb.get("key", ""),
                                     fb.get("text", "")))
        # 2026-10-10：D 选项按风格档案写，不让 LLM 瞎猜
        worker_instruction = _with_style_block(
            worker_instruction, list(state.get("players", {}).values()))

        facts = {
            "phase": "continue",
            "round": new_round,
            "max_rounds": state["max_rounds"],
            "chosen": chosen,
            "vote_tally": tally,
            "auto_note": (("总票数<2，按最稳妥选项自动推进" if auto_safe
                           else "多数决" + ("（平票掷骰）" if tied_n > 1
                                            else ""))
                          if not chosen.get("custom_choice")
                          else "自由发言优先（%d 条），worker 选最有意思的一条"
                          % len(chosen.get("customs", []))),
            "check": check,
            "random_event": event_text,
            "event_roll": event_roll,
            "bible": bible,
            "hot_memory": self._story_so_far(state, 2),
            "warm_memory": self._warm_memory(state),
            "dilemma": (new_round % 3 == 0),
            "closing": new_round >= state["max_rounds"] - 2,
            "allow_early_ending": (
                MIN_ROUNDS_BEFORE_ENDING <= new_round
                < state["max_rounds"]),
            "worker_instruction": worker_instruction,
        }
        # 填充 prompt 占位符里用到的轮次（worker_instruction 模板本身保留 {{}}，
        # 由 worker 侧的渲染约定替换；此处仅保证 facts 可读）
        facts["chapter_no"] = new_round + 1
        return {"kind": "TICK", "facts": facts, "new_state": state,
                "points": [], "wins": []}

    def _tick_ending(self, game, ctx, state, now):
        """等 worker 写完结局 → 解析【结局】【最佳转折】→ END。"""
        db = ctx["db"]
        game_id = game["id"]
        e_end = _latest_tick_by_phases(db, game_id, ("ending",))
        if not e_end or not e_end["msg_id"]:
            return None
        # 取 ending 之后（含）的最新 ending/reemit 回复
        cand = _latest_tick_by_phases(db, game_id, ("ending", "reemit"))
        if not cand or cand["event_id"] < e_end["event_id"]:
            cand = e_end
        if (not _is_sent(cand["status"])
                or state.get("ending_consumed") == cand["event_id"]):
            return None

        ending_name, twist = parse_ending(cand["reply_content"])
        if not ending_name or not twist:
            state["reemit_count"] = state.get("reemit_count", 0) + 1
            if state["reemit_count"] > REEMIT_LIMIT:
                state["phase"] = "done"
                state["last_ending"] = None
                return {"kind": "END",
                        "facts": self._end_facts(game, "parse_fail"),
                        "new_state": state, "points": [], "wins": []}
            return {"kind": "TICK",
                    "facts": {"phase": "reemit", "where": "ending",
                              "worker_instruction":
                                  REEMIT_ENDING_INSTRUCTION},
                    "new_state": state, "points": [], "wins": []}

        # 结局 bible（可选）：更新伏笔回收状态，供战报卡用
        final_raw = _extract_json_bible(cand["reply_content"])
        if isinstance(final_raw, dict):
            bible, _d = normalize_bible(final_raw, state["bible"], state)
            # 保留 Python 累计字段（normalize 已处理），直接采用
            state["bible"] = bible

        state["ending_consumed"] = cand["event_id"]
        state["phase"] = "done"
        twist_uid = self._match_player(state, twist["name"])
        state["last_ending"] = {"ending_name": ending_name,
                                "twist_name": twist["name"],
                                "twist_reason": twist["reason"],
                                "twist_uid": twist_uid or ""}
        facts = self._end_facts(game, "finished", ending_name, twist,
                                twist_uid)
        return {"kind": "END", "facts": facts, "new_state": state,
                "points": [], "wins": []}

    def _reemit(self, game, state, where):
        """worker 格式解析失败 → 让 worker 按固定格式重发一次。"""
        state["reemit_count"] = state.get("reemit_count", 0) + 1
        if state["reemit_count"] > REEMIT_LIMIT:
            state["phase"] = "done"
            state["last_ending"] = None
            return {"kind": "END",
                    "facts": self._end_facts(game, "parse_fail"),
                    "new_state": state, "points": [], "wins": []}
        facts = {"phase": "reemit", "where": where,
                 "round": state.get("round", 0),
                 "worker_instruction": REEMIT_INSTRUCTION}
        return {"kind": "TICK", "facts": facts, "new_state": state,
                "points": [], "wins": []}

    # ---------- 投票 / 检定 / 随机事件（文档 §4） ----------

    @staticmethod
    def _all_voted(state):
        """活跃玩家（开局登记名单）是否全部投完 → 提前结算（文档 §4.1）。"""
        players = state.get("players", {}) or {}
        votes = state.get("votes", {}) or {}
        if not players:
            return False
        return all(uid in votes for uid in players)

    def _count_votes(self, state, opts, game_id):
        """计票 → (chosen, tally, auto_safe, early_ending)。

        规则（2026-10-10 更新：自由发言优先）：
        - 有自由发言 → 优先采用，worker（LLM）从多条中选最有意思的一条
          并说明理由；全是闲聊则回退到投票结果；
        - 无自由发言时：一人一票，只收第一次（handle_message 已保证重复投票不覆盖）；
        - 只计入本轮提供的选项字母；E 仅提供时有效；
        - 多数决；平票程序掷骰（确定性随机）；
        - 总票数 <2 → 最稳妥选项（A；无 A 则首个）自动推进；
        - 全零票 → 随机选一个选项（故事不停摆）。
        """
        votes = state.get("votes", {}) or {}
        offered = {o["key"] for o in opts}
        tally = {}
        for _uid, v in votes.items():
            c = (v.get("choice") or "").upper()
            if c in offered:
                tally[c] = tally.get(c, 0) + 1
        customs = state.get("custom", {}) or {}
        total = sum(tally.values()) + len(customs)
        rng = self.make_rng(game_id, f"vote{state.get('round', 0)}")

        def _vote_winner():
            """无有效自由发言时的投票胜者（供回退用）→ (chosen, auto_safe)。"""
            if total < 2:
                # 冷场保护：按最稳妥选项自动推进（S1 超时托管精神）
                safe = next((o for o in opts if o["key"] == "A"), opts[0])
                return ({"key": safe["key"], "text": safe["text"]}, True)
            if tally:
                top = max(tally.values())
                tied = sorted(k for k, v in tally.items() if v == top)
                wkey = rng.choice(tied)
                if wkey == "E":
                    wtext = next(o["text"] for o in opts if o["key"] == "E")
                    return ({"key": wkey, "text": wtext}, False)
                wtext = next(o["text"] for o in opts if o["key"] == wkey)
                return ({"key": wkey, "text": wtext}, False)
            wkey = rng.choice(sorted(offered))
            wtext = next(o["text"] for o in opts if o["key"] == wkey)
            return ({"key": wkey, "text": wtext}, False)

        # 2026-10-10 用户要求：自由发言优先于 A/B/C/D 投票。
        # 多条时不由程序定，由 worker（LLM）选最有意思的一条并说明理由。
        if customs:
            clist = sorted(customs.values(),
                           key=lambda v: (v.get("ts", 0), v.get("name", "")))
            fallback, fallback_auto = _vote_winner()
            return ({"custom_choice": True,
                     "customs": [{"by": v.get("name", ""),
                                  "text": v.get("text", "")[:100]}
                                 for v in clist],
                     "fallback": fallback},
                    tally, fallback_auto, False)

        chosen, auto_safe = _vote_winner()
        # E 胜出 → 进入结局（调用方据 early_ending 判断）
        early_ending = chosen.get("key") == "E"
        return (chosen, tally, auto_safe, early_ending)

    def _roll_check(self, game_id, new_round, chosen):
        """检定（文档 §4.5）：所选选项若标"需检定·难度"，程序掷 d20。

        返回 {"roll","difficulty","difficulty_name","result","margin"}，
        result ∈ success/fail/crit_success/crit_fail；无标注返回 None。
        """
        text = chosen.get("text", "") if isinstance(chosen, dict) else ""
        m = _CHECK_RE.search(text or "")
        if not m:
            return None
        name = m.group(1)
        diff = CHECK_DIFFICULTIES[name]
        rng = self.make_rng(game_id, f"check{new_round}")
        roll = rng.randint(1, 20)
        margin = roll - diff
        if margin >= 5:
            result = "crit_success"
        elif margin <= -5:
            result = "crit_fail"
        elif margin >= 0:
            result = "success"
        else:
            result = "fail"
        return {"roll": roll, "difficulty": diff, "difficulty_name": name,
                "result": result, "margin": margin}

    def _roll_event(self, game_id, new_round, state):
        """轻量随机事件（文档 §4.4）：程序掷 d100，1~25 触发（碾压局 40%）。

        返回 (roll, event_text or None)。事件表可由 worker 按题材提供。
        """
        table = state["bible"].get("event_table") or DEFAULT_EVENTS
        hot = state.get("landslide_streak", 0) >= 2
        rng = self.make_rng(game_id, f"event{new_round}")
        roll = rng.randint(1, 100)
        thresh = EVENT_TRIGGER_HOT if hot else EVENT_TRIGGER
        if roll <= thresh:
            return roll, rng.choice(table)
        return roll, None

    # ---------- 结局判定（文档 §5.2） ----------

    def _ending_weights(self, state):
        """按倾向/伏笔/NPC 好感/检定计算各结局线权重（建议非铁律）。"""
        bible = state.get("bible", {}) or {}
        t = bible.get("tendencies", {}) or {}
        paid = sum(1 for f in bible.get("foreshadows", [])
                   if isinstance(f, dict) and f.get("status") == "paid_off")
        npc_good = sum(1 for c in bible.get("characters", [])
                       if isinstance(c, dict)
                       and (c.get("attitude") or 0) >= 4)
        bad = 0
        if any((t.get(d) or 0) <= -3 for d in TENDENCY_DIMS):
            bad += 2
        if state.get("crit_fail_streak", 0) >= 2:
            bad += 2
        ruthless = t.get("ruthless", 0) or 0
        if ruthless > 0 and ruthless == max(
                [t.get(d, 0) or 0 for d in TENDENCY_DIMS]) \
                and (t.get("trust", 0) or 0) <= -2:
            bad += 2
        return {
            "真相线": max(0, t.get("curiosity", 0) or 0) + paid * 0.5,
            "救赎线": max(0, t.get("trust", 0) or 0) + npc_good,
            "牺牲线": max(0, t.get("courage", 0) or 0)
            + state.get("crit_fail_count", 0),
            "坏结局": bad,
        }

    # ---------- 结算 ----------

    def finish(self, game, reason, ctx):
        """结算积分。

        注意：manager.tick 在 kind=END 时会先 _apply_rewards(out 的 points)，
        再调 finish_game_flow → 本函数 → 再次 _apply_rewards。
        为避免 double count，积分只在这里（finish）给一次，
        tick 的 END 一律返回空 points/wins（与 number_bomb 同一约定）。
        自然超时/解析失败/取消：参与奖；正常结束：参与奖 + 最佳转折奖。
        """
        state = game["state"]
        points = []
        for uid, name in (state.get("players") or {}).items():
            points.append((uid, name, POINTS["story_participate"]))
        last = state.get("last_ending") or {}
        twist_uid = last.get("twist_uid")
        if twist_uid and twist_uid in (state.get("players") or {}):
            points.append((twist_uid, state["players"][twist_uid],
                           POINTS["story_twist"]))
        facts = self._end_facts(game, reason)
        return {"facts": facts, "points": points, "wins": []}

    # ---------- 工具 ----------

    def _end_facts(self, game, reason, ending_name=None, twist=None,
                   twist_uid=None):
        state = game["state"]
        last = state.get("last_ending") or {}
        twist_out = twist or ({"name": last.get("twist_name", ""),
                               "reason": last.get("twist_reason", "")}
                              if last.get("twist_name") else {})
        facts = {
            "phase": "done",
            "reason": reason,
            "game_name": self.name,
            "rounds": state.get("round", 0),
            "max_rounds": state.get("max_rounds", DEFAULT_MAX_ROUNDS),
            "chapters": len(state.get("chapters", [])),
            "total_chars": sum(len(c) for c in state.get("chapters", [])),
            "ending_name": ending_name or last.get("ending_name", ""),
            "twist": twist_out,
            "twist_user_id": twist_uid or last.get("twist_uid", ""),
            "participants": [
                {"user_id": uid, "display_name": nm}
                for uid, nm in (state.get("players") or {}).items()],
            "choice_ledger": state.get("bible", {}).get("choices_ledger",
                                                         []),
            "tendencies": state.get("bible", {}).get("tendencies", {}),
        }
        facts["report_card"] = self._report_card(game, facts["ending_name"])
        return facts

    def _report_card(self, game, ending_name):
        """战报卡（文档 §1.4）：程序生成，非 LLM。"""
        state = game["state"]
        ledger = state.get("bible", {}).get("choices_ledger", []) or []
        key_choices = [{"round": e.get("round"), "option": e.get("option"),
                        "text": e.get("text", "")} for e in ledger[-5:]]
        vc = state.get("vote_counts", {}) or {}
        mvp = None
        if vc:
            uid = max(vc, key=lambda u: vc[u].get("votes", 0))
            mvp = {"user_id": uid, "display_name": vc[uid].get("name", uid),
                   "votes": vc[uid].get("votes", 0)}
        return {
            "rounds": state.get("round", 0),
            "chapters": len(state.get("chapters", [])),
            "total_chars": sum(len(c) for c in state.get("chapters", [])),
            "key_choices": key_choices,
            "ending_name": ending_name or "",
            "mvp": mvp,
        }

    @staticmethod
    def _story_so_far(state, n):
        """热记忆：最近 n 章原文（截断），供 worker 续写参考。"""
        parts = []
        for ch in (state.get("chapters") or [])[-n:]:
            parts.append(ch[:600])
        text = "\n\n".join(parts)
        return text[:1500]

    @staticmethod
    def _warm_memory(state):
        """温记忆：第 3~6 轮压缩为"决策+结果"一句话（文档 §2.2）。"""
        ledger = state.get("bible", {}).get("choices_ledger", []) or []
        out = []
        for e in ledger[-6:-2]:
            out.append("第%s轮：选%s（%s）" % (
                e.get("round"), e.get("option"), e.get("text", "")[:30]))
        return out

    @staticmethod
    def _match_player(state, name):
        """最佳转折的人名 → user_id（best-effort，对不上返回 None）。"""
        if not name:
            return None
        want = Game.normalize(name)
        for uid, nm in (state.get("players") or {}).items():
            if nm == name or Game.normalize(nm) == want:
                return uid
        return None
