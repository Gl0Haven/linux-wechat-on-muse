# linux-wechat-on-muse

A bridge that connects the **WeChat Linux client** to an AI assistant, so the assistant can read group/DM messages and reply inside WeChat.

## How it works

```
WeChat Linux client (under Xvfb)
        │  SQLite WAL tail
        ▼
wal_reader_daemon.py ──► queue.db (SQLite message queue)
                              │  claim → generate → READY
                              ▼
                     blocking_worker / worker_batch (LLM reply generation)
                              │  READY
                              ▼
                     sendd_v2.py ──► GUI automation (X11/xdotool/XTEST)
                                          click, paste, Enter
```

- **Ingest**: `wal_reader_daemon.py` tails the WeChat client's SQLite WAL and enqueues inbound messages with reply-worthiness classification (`should_reply`).
- **Queue**: `msgqueue.py` / `queue.db` — a SQLite-backed pipeline (`NEW → GENERATING → READY → SENDING → SENT`), with leases so multiple workers don't double-handle a message.
- **Reply**: worker modules claim messages, build context (recent messages, sender history, chat memory), and write replies.
- **Send**: `sendd_v2.py` is the single GUI writer — navigates to the right chat via `send-ui.py`, pastes text (or stickers/images via clipboard), and verifies delivery through the WAL.
- **Supervision**: `keepalive.sh` keeps Xvfb, the WeChat client, proxy, reader, sender, and scheduler alive; `unified_health.py` / `health_monitor.py` report status; `sync_heartbeat.py` nudges the client when push goes quiet.
- **Extras**: `games/` (group games: story voting, quizzes, gacha, etc.), `news_push.py` / `digest.py` (scheduled digests), `sticker.py` (sticker library), `meme.py` (meme curation).

## Repository layout

| Path | Contents |
|---|---|
| `*.py`, `*.sh` (root) | Current pipeline: ingest, queue, worker, sender, supervision, extras |
| `games/` | Group games |
| `legacy/` | Retired components from the earlier architecture (DB poller, old sender, old worker loops, one-off analysis scripts). Kept for reference; not started by `keepalive.sh` and not guaranteed to run. |
| `MEDIA_UNDERSTANDING_DESIGN.md`, `games/STORY_DESIGN.md` | Design notes |

## LLM backend

Reply generation is **not** a standalone LLM client. It was built to run inside the Muse agent runtime: a scheduled agent run claims messages via `blocking_worker.py` / `msgqueue.py`, writes the reply text, and commits it back to the queue. To use another backend, write a loop that calls `claim_next_reply` → your model → `mark_ready` / `mark_skipped` (pass `worker_id` and `claim_version` so stale workers cannot overwrite newer results).

## Prerequisites

- WeChat for Linux client (tested with 4.1.x) installed and logged in
- Xvfb (virtual display), `xdotool`/`scrot` or equivalents
- Optional: `proxychains-ng` if the client must go through a proxy
- Python 3.10+
- `pip install -r requirements.txt`
- A stable egress IP if your network needs one (the client is picky about IP changes); see `chain_proxy.py`
- `ensure-deps.sh` pins `/etc/machine-id` and the hostname (set `WECHAT_HOSTNAME`, default `wechat-bridge`) across VM resets so the client is not treated as a new device. Understand what this does before running it.

## Configuration

You **must** configure your own identities before running — the repo ships with placeholders:

| Placeholder | Meaning |
|---|---|
| `wxid_YOUR_MAIN_ACCOUNT` | Your own WeChat ID |
| `wxid_YOUR_BOT_ACCOUNT` | The WeChat account running the client |
| `YOUR_MAIN_GROUP@chatroom` | Main group chat ID |
| `YOUR_TEST_GROUP@chatroom` | Test group chat ID |
| `YOUR_PROXY_IP` | Your fixed egress IP (if used) |

Search the tree for `YOUR_` and replace with your values. Per-group behavior switches (`NEWS_CHATS`, `DIGEST_CHATS`, game whitelists, etc.) live near the top of each module.

Runtime state goes in `state/` (git-ignored); never commit `*.db`, `*.log`, or screenshots.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This drives the official WeChat Linux client via GUI automation. That violates WeChat's ToS automation clauses — use a throwaway account, expect possible restrictions, and keep reply behavior conservative (rate limits, whitelists, and human-like delays are built in for this reason).
