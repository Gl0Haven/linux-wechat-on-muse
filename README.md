# linux-wechat-on-muse

A bridge that connects the **WeChat Linux client** to an AI assistant, so the assistant can read group/DM messages and reply inside WeChat.

> **Personal, non-commercial research project.** Not affiliated with, endorsed by, or supported by Tencent or WeChat. Read the [Disclaimer](#disclaimer--免责声明) before using any of this.

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

## Acknowledgements

This project would not exist without the **Muse** agent platform provided by Meta, which runs the reply-generation side of the pipeline. Thanks to the Muse team for making it available.

## Disclaimer / 免责声明

**English**

- **Purpose.** This is a personal, non-commercial project for learning and research on desktop automation and LLM agents. It is published as a technical reference, not as a product or service.
- **No affiliation.** This project is not affiliated with, authorized, endorsed, or sponsored by Tencent, WeChat, or Meta. "WeChat" and "微信" are trademarks of Tencent; all trademarks belong to their respective owners and are used here only to describe compatibility.
- **What it does and does not do.** It only operates the official, unmodified WeChat Linux client through ordinary GUI input (as a person would with a keyboard and mouse), and reads the local database of the account that is logged in on the same machine. It does **not** modify, patch, inject into, or decompile the client, does **not** implement or reverse-engineer the WeChat network protocol, and does **not** bypass encryption, payment, or login verification. The one environment tweak it makes is keeping the machine's `machine-id` and hostname stable across VM resets (`ensure-deps.sh`), so a long-running VM is not mistaken for a new device; remove that step if you do not want it.
- **Terms of service.** Automating the client may violate WeChat's user agreement and platform rules. Accounts used with this project may be warned, restricted, or banned. You alone are responsible for complying with the WeChat Software License and Service Agreement and all applicable laws and regulations in your jurisdiction.
- **Prohibited uses.** Do not use this project for spam, bulk or unsolicited messaging, marketing, fraud, gambling, impersonation, harassment, scraping or collecting other people's personal information, or any illegal purpose. Only process chats you are a member of, and respect the privacy of other participants — tell your group members that a bot is present.
- **No warranty.** The software is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)). The authors are not liable for account loss, data loss, or any other damage arising from its use.
- **Takedown / removal on request.** If Tencent (WeChat) or Meta (Muse), or any other rights holder, considers this repository inappropriate and asks for it to be removed, I will comply promptly and voluntarily — taking the repository private or deleting it — without dispute. Please open an issue or contact the repository owner through GitHub.
- **Right to change or withdraw.** The author may modify, stop maintaining, or delete this project at any time without notice.

**中文**

- **用途**：本项目为个人非商业项目，仅用于桌面自动化与 LLM agent 的学习研究，作为技术参考公开，不是产品或服务。
- **无关联**：本项目与腾讯、微信、Meta 均无任何关联，未获其授权、认可或赞助。"微信""WeChat"为腾讯公司商标，所有商标归各自权利人所有，此处仅用于说明兼容对象。
- **做什么、不做什么**：本项目只通过普通的键盘鼠标输入操作未经修改的官方微信 Linux 客户端，并读取本机已登录账号的本地数据库；**不**修改、破解、注入或反编译客户端，**不**实现或逆向微信通信协议，**不**绕过加密、支付或登录验证。唯一的环境调整是在虚拟机重置后保持本机 `machine-id` 与主机名不变（`ensure-deps.sh`），避免长期运行的虚拟机被误判为新设备；不需要可自行删除该步骤。
- **服务协议**：自动化操作客户端可能违反《微信软件许可及服务协议》及相关平台规则，所用账号可能被警告、限制或封禁。使用者须自行遵守该协议及所在地法律法规，并自行承担全部后果。
- **禁止用途**：严禁用于垃圾信息、群发营销、诈骗、赌博、冒充他人、骚扰、抓取或收集他人个人信息等任何违法违规用途。仅处理你本人所在的聊天，尊重其他成员隐私，并告知群成员有机器人在群内。
- **不提供担保**：本软件按"原样"提供，不附带任何形式的担保（见 [LICENSE](LICENSE)）。因使用本项目导致的账号、数据或其他任何损失，作者概不负责。
- **应要求删库**：如腾讯（微信）或 Meta（Muse）及其他权利人认为本仓库不妥并要求删除，作者将及时、主动配合，将仓库转为私有或直接删除，不做争辩。请通过 issue 或 GitHub 联系仓库所有者。
- **保留变更权利**：作者保留随时修改、停止维护或删除本项目的权利，恕不另行通知。
