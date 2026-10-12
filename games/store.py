#!/usr/bin/env python3
"""
微信群每日小游戏 - SQLite 存储层

表（全部建在 queue.db 里，与 messages 同库，方便同一事务）：
- games: 一局游戏
- game_players: 本局玩家
- game_events: 游戏事件（含渲染用的确定性 facts，JSON）
- daily_game_state: 每天每群的开局状态（保证一天只自动开一局）
- player_stats: 玩家总积分
- message_game_link: messages.id → 游戏事件（worker 渲染用）

注意：本模块不 import msgqueue，避免循环依赖。
调用方先 get_db() 再调 ensure_schema(db)。
"""

import json
import time

GAME_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    game_type TEXT NOT NULL,
    status TEXT DEFAULT 'PENDING',   -- PENDING/ACTIVE/FINISHED/CANCELLED/EXPIRED
    created_at INTEGER NOT NULL,
    started_at INTEGER DEFAULT 0,
    ends_at INTEGER DEFAULT 0,
    finished_at INTEGER DEFAULT 0,
    host_message_id INTEGER DEFAULT 0,
    config_json TEXT DEFAULT '{}',
    state_json TEXT DEFAULT '{}',
    winner_json TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS game_players (
    game_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    joined_at INTEGER NOT NULL,
    score INTEGER DEFAULT 0,
    state_json TEXT DEFAULT '{}',
    PRIMARY KEY (game_id, user_id)
);
CREATE TABLE IF NOT EXISTS game_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    msg_id INTEGER DEFAULT 0,        -- 关联的 messages.id（0=系统事件）
    user_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,         -- JOIN/GUESS/HIT/MOVE/GAME_START/GAME_TICK/GAME_END/...
    content TEXT DEFAULT '',         -- JSON：确定性 facts，供 worker 渲染
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_game_state (
    chat_id TEXT NOT NULL,
    date TEXT NOT NULL,               -- YYYY-MM-DD（北京时间）
    game_type TEXT DEFAULT '',
    started INTEGER DEFAULT 0,
    finished INTEGER DEFAULT 0,
    declined INTEGER DEFAULT 0,      -- vibe check SKIP 次数
    last_game_at INTEGER DEFAULT 0,
    last_propose_at INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, date)
);
CREATE TABLE IF NOT EXISTS player_stats (
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    points INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    games_played INTEGER DEFAULT 0,
    last_played_at INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS message_game_link (
    msg_id INTEGER PRIMARY KEY,      -- messages.id（真实消息或合成消息）
    game_id INTEGER NOT NULL,
    event_id INTEGER DEFAULT 0,      -- game_events.id（渲染 facts 来源）
    kind TEXT DEFAULT ''             -- GAME_INPUT/GAME_START/GAME_TICK/GAME_END/GAME_REPLY/GAME_PROPOSE
);
CREATE INDEX IF NOT EXISTS idx_games_chat_status ON games(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_gevents_game ON game_events(game_id);
CREATE INDEX IF NOT EXISTS idx_gevents_type ON game_events(game_id, event_type);
"""


def ensure_schema(db):
    """幂等建表。"""
    db.executescript(GAME_SCHEMA)


# ---------- games ----------

def create_game(db, chat_id, game_type, config=None, duration_sec=1800,
                commit=True):
    now = int(time.time())
    cur = db.execute(
        """INSERT INTO games
           (chat_id, game_type, status, created_at, started_at, ends_at,
            config_json, state_json)
           VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, '{}')""",
        (chat_id, game_type, now, now, now + duration_sec,
         json.dumps(config or {}, ensure_ascii=False)))
    gid = cur.lastrowid
    if commit:
        db.commit()
    return gid


def get_game(db, game_id):
    row = db.execute(
        "SELECT id, chat_id, game_type, status, created_at, started_at,"
        " ends_at, finished_at, host_message_id, config_json, state_json,"
        " winner_json FROM games WHERE id=?", (game_id,)).fetchone()
    return _row_to_game(row) if row else None


def get_active_game(db, chat_id):
    """一个群同时最多一个 ACTIVE 游戏。"""
    row = db.execute(
        "SELECT id, chat_id, game_type, status, created_at, started_at,"
        " ends_at, finished_at, host_message_id, config_json, state_json,"
        " winner_json FROM games WHERE chat_id=? AND status='ACTIVE'"
        " ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
    return _row_to_game(row) if row else None


def _row_to_game(row):
    return {
        "id": row[0], "chat_id": row[1], "game_type": row[2],
        "status": row[3], "created_at": row[4], "started_at": row[5],
        "ends_at": row[6], "finished_at": row[7],
        "host_message_id": row[8],
        "config": json.loads(row[9] or "{}"),
        "state": json.loads(row[10] or "{}"),
        "winner": json.loads(row[11] or "null"),
    }


def save_game_state(db, game_id, state, commit=True):
    db.execute("UPDATE games SET state_json=? WHERE id=?",
               (json.dumps(state, ensure_ascii=False), game_id))
    if commit:
        db.commit()


def finish_game(db, game_id, status="FINISHED", winner=None, commit=True):
    """status: FINISHED/CANCELLED/EXPIRED"""
    now = int(time.time())
    db.execute(
        "UPDATE games SET status=?, finished_at=?, winner_json=? WHERE id=?",
        (status, now, json.dumps(winner, ensure_ascii=False), game_id))
    if commit:
        db.commit()


def expire_overdue(db, now=None, commit=True):
    """把已过 ends_at 的 ACTIVE 游戏标 EXPIRED。返回过期游戏 id 列表。"""
    now = now or int(time.time())
    rows = db.execute(
        "SELECT id FROM games WHERE status='ACTIVE' AND ends_at < ?",
        (now,)).fetchall()
    ids = [r[0] for r in rows]
    if ids:
        db.execute(
            "UPDATE games SET status='EXPIRED', finished_at=? "
            "WHERE status='ACTIVE' AND ends_at < ?", (now, now))
    if commit:
        db.commit()
    return ids


def list_active_games(db):
    rows = db.execute(
        "SELECT id, chat_id, game_type, status, created_at, started_at,"
        " ends_at, finished_at, host_message_id, config_json, state_json,"
        " winner_json FROM games WHERE status='ACTIVE'").fetchall()
    return [_row_to_game(r) for r in rows]


# ---------- players ----------

def add_player(db, game_id, user_id, display_name="", state=None,
               commit=True):
    now = int(time.time())
    db.execute(
        """INSERT OR IGNORE INTO game_players
           (game_id, user_id, display_name, joined_at, state_json)
           VALUES (?, ?, ?, ?, ?)""",
        (game_id, user_id, display_name, now,
         json.dumps(state or {}, ensure_ascii=False)))
    # games_played +1（幂等：只在首次加入时）
    if db.execute("SELECT changes()").fetchone()[0]:
        _bump_stat(db, game_id, user_id, display_name, "games_played", 1,
                   commit=False)
    if commit:
        db.commit()


def get_players(db, game_id):
    rows = db.execute(
        "SELECT user_id, display_name, joined_at, score, state_json"
        " FROM game_players WHERE game_id=?", (game_id,)).fetchall()
    return [{"user_id": r[0], "display_name": r[1], "joined_at": r[2],
             "score": r[3], "state": json.loads(r[4] or "{}")} for r in rows]


def update_player(db, game_id, user_id, score=None, state=None,
                  display_name=None, commit=True):
    sets, vals = [], []
    if score is not None:
        sets.append("score=?")
        vals.append(score)
    if state is not None:
        sets.append("state_json=?")
        vals.append(json.dumps(state, ensure_ascii=False))
    if display_name is not None:
        sets.append("display_name=?")
        vals.append(display_name)
    if sets:
        db.execute(
            f"UPDATE game_players SET {', '.join(sets)}"
            " WHERE game_id=? AND user_id=?", (*vals, game_id, user_id))
    if commit:
        db.commit()


# ---------- events ----------

def add_event(db, game_id, event_type, facts=None, msg_id=0, user_id="",
              commit=True):
    now = int(time.time())
    cur = db.execute(
        """INSERT INTO game_events
           (game_id, msg_id, user_id, event_type, content, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (game_id, msg_id, user_id, event_type,
         json.dumps(facts or {}, ensure_ascii=False), now))
    eid = cur.lastrowid
    if commit:
        db.commit()
    return eid


def get_event(db, event_id):
    row = db.execute(
        "SELECT id, game_id, msg_id, user_id, event_type, content, created_at"
        " FROM game_events WHERE id=?", (event_id,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "game_id": row[1], "msg_id": row[2],
            "user_id": row[3], "event_type": row[4],
            "facts": json.loads(row[5] or "{}"), "created_at": row[6]}


# ---------- message link ----------

def link_message(db, msg_id, game_id, event_id=0, kind="", commit=True):
    db.execute(
        """INSERT OR REPLACE INTO message_game_link
           (msg_id, game_id, event_id, kind) VALUES (?, ?, ?, ?)""",
        (msg_id, game_id, event_id, kind))
    if commit:
        db.commit()


def get_link(db, msg_id):
    row = db.execute(
        "SELECT msg_id, game_id, event_id, kind FROM message_game_link"
        " WHERE msg_id=?", (msg_id,)).fetchone()
    if not row:
        return None
    return {"msg_id": row[0], "game_id": row[1],
            "event_id": row[2], "kind": row[3]}


def get_render(db, msg_id):
    """给 worker 用的渲染包：link + event facts + game 基本信息。"""
    link = get_link(db, msg_id)
    if not link:
        return None
    event = get_event(db, link["event_id"]) if link["event_id"] else None
    game = get_game(db, link["game_id"])
    return {"link": link, "event": event, "game": game}


# ---------- daily state ----------

def get_daily_state(db, chat_id, date):
    row = db.execute(
        "SELECT chat_id, date, game_type, started, finished, declined,"
        " last_game_at, last_propose_at FROM daily_game_state"
        " WHERE chat_id=? AND date=?", (chat_id, date)).fetchone()
    if not row:
        return {"chat_id": chat_id, "date": date, "game_type": "",
                "started": 0, "finished": 0, "declined": 0,
                "last_game_at": 0, "last_propose_at": 0}
    return {"chat_id": row[0], "date": row[1], "game_type": row[2],
            "started": row[3], "finished": row[4], "declined": row[5],
            "last_game_at": row[6], "last_propose_at": row[7]}


def mark_daily_started(db, chat_id, date, game_type, commit=True):
    now = int(time.time())
    db.execute(
        """INSERT INTO daily_game_state
           (chat_id, date, game_type, started, last_game_at)
           VALUES (?, ?, ?, 1, ?)
           ON CONFLICT(chat_id, date) DO UPDATE SET
             game_type=excluded.game_type, started=1,
             last_game_at=excluded.last_game_at""",
        (chat_id, date, game_type, now))
    if commit:
        db.commit()


def mark_daily_finished(db, chat_id, date, commit=True):
    db.execute(
        """INSERT INTO daily_game_state (chat_id, date, finished)
           VALUES (?, ?, 1)
           ON CONFLICT(chat_id, date) DO UPDATE SET finished=1""",
        (chat_id, date))
    if commit:
        db.commit()


def mark_daily_declined(db, chat_id, date, commit=True):
    now = int(time.time())
    db.execute(
        """INSERT INTO daily_game_state
           (chat_id, date, declined, last_propose_at)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(chat_id, date) DO UPDATE SET
             declined=daily_game_state.declined+1,
             last_propose_at=excluded.last_propose_at""",
        (chat_id, date, now))
    if commit:
        db.commit()


# ---------- player stats / 积分 ----------

def _bump_stat(db, game_id, user_id, display_name, field, delta,
               commit=True):
    game = get_game(db, game_id) if game_id else None
    chat_id = game["chat_id"] if game else ""
    db.execute(
        f"""INSERT INTO player_stats
            (chat_id, user_id, display_name, {field}, last_played_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
              {field}=player_stats.{field}+excluded.{field},
              display_name=excluded.display_name,
              last_played_at=excluded.last_played_at""",
        (chat_id, user_id, display_name or user_id, delta,
         int(time.time())))
    if commit:
        db.commit()


def add_points(db, game_id, user_id, display_name, points, commit=True):
    _bump_stat(db, game_id, user_id, display_name, "points", points,
               commit=False)
    if commit:
        db.commit()


def add_points_by_chat(db, chat_id, user_id, display_name, points,
                       commit=True):
    """按 chat 直接加分（不依赖 game_id，如每日签到）。"""
    now = int(time.time())
    db.execute(
        """INSERT INTO player_stats
           (chat_id, user_id, display_name, points, last_played_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             points=player_stats.points+excluded.points,
             display_name=excluded.display_name,
             last_played_at=excluded.last_played_at""",
        (chat_id, user_id, display_name or user_id, points, now))
    if commit:
        db.commit()


def add_win(db, game_id, user_id, display_name, commit=True):
    _bump_stat(db, game_id, user_id, display_name, "wins", 1, commit=False)
    if commit:
        db.commit()


def get_stats(db, chat_id, user_id):
    row = db.execute(
        "SELECT display_name, points, wins, games_played, last_played_at"
        " FROM player_stats WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)).fetchone()
    if not row:
        return {"display_name": "", "points": 0, "wins": 0,
                "games_played": 0, "last_played_at": 0}
    return {"display_name": row[0], "points": row[1], "wins": row[2],
            "games_played": row[3], "last_played_at": row[4]}


def leaderboard(db, chat_id, limit=5):
    rows = db.execute(
        "SELECT display_name, points, wins, games_played FROM player_stats"
        " WHERE chat_id=? ORDER BY points DESC, wins DESC LIMIT ?",
        (chat_id, limit)).fetchall()
    return [{"display_name": r[0], "points": r[1], "wins": r[2],
             "games_played": r[3]} for r in rows]
