# AppVantage AI — Shared Agent Workspace

[![CI](https://github.com/Knightduster888/RoseAssistAi/actions/workflows/ci.yml/badge.svg)](https://github.com/Knightduster888/RoseAssistAi/actions/workflows/ci.yml)

A self-hosted, dependency-free web board where a human (Corey) and two AI agents (Rose & Alex) collaborate in real time. The agents read and write to a shared inbox; the viewer renders the conversation as a polished chat interface.

## What it does

- **Shared inbox as single source of truth** — every message is a timestamped markdown file in `inbox/`
- **Realtime agent responses** — posting a message immediately wakes the relevant agent(s), which reply directly into the inbox (~10s latency)
- **`@`-mention routing** — `@rose`, `@alex`, or `@both` to target a specific agent
- **Live presence / typing signals** — animated indicator under each agent's avatar while it's thinking or typing
- **Archiving & recall** — compress sessions into dated archives; `/recall <word>` searches live + archived history
- **Portrait avatars** with automated face-crop (YuNet face detection)
- **Session history drawer** with searchable, sender-filtered chat history

## Architecture

```
shared_viewer.py         HTTP server + v2 web UI (std-lib http.server, no Flask)
realtime_trigger.py      Spawns one-shot hermes chat runs on new user messages
presence.py              Agent presence/typing heartbeats
archive.py               Session archiving / compression
recall_cli.py            /recall CLI for searching history (Telegram path)
rose_corey_watch.py      Watcher for new user messages (cron fallback path)
assets/avatars/          Agent avatar portraits
inbox/                   Live conversation (source of truth)
presence/                Heartbeat files
archive/                 Compressed session archives
```

## How it works

1. User posts via the **composer** (or API `POST /api/message`).
2. Message is written to `inbox/<timestamp>_from-corey.md` with `To:`/`Target:` headers.
3. `realtime_trigger.py` spawns a background `hermes chat -q` for each targeted agent.
4. Each agent reads the inbox, composes a reply as its persona, and **writes its reply back to the inbox** (visible in the viewer) — no Telegram-only silo.
5. The viewer polls `/api/threads` and `/api/presence` to render messages and live typing indicators.

A 1-minute cron watcher acts as a fallback so unread messages are never missed if the realtime spawn path fails.

## Running

```bash
python3 shared_viewer.py
# serves on 0.0.0.0:8002
```

No third-party dependencies — uses Python 3 std-lib only.

## Tests / CI

The archive/recall logic is covered by hermetic unit tests (`tests/test_archive.py`) that run inside a temp directory via the `ROSE_SHARED_DIR` env override, so they never touch live workspace data:

```bash
python3 -m unittest discover -s tests -v
```

`archive.py` honours `ROSE_SHARED_DIR` when set (defaults to `/root/shared-agents`) — use it to point at a throwaway dir in your own test harness.

A GitHub Actions workflow (`.github/workflows/ci.yml`) runs a Python syntax gate + the full test suite on every push/PR to `main`, across Python 3.10–3.12.

## Key endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Web UI |
| `GET /api/threads` | All messages |
| `POST /api/message` | Post a message (JSON `{text}`) |
| `GET /api/presence` | Live agent typing/thinking signals |
| `POST /api/archive` | Compress live thread into an archive |
| `GET /api/archive` | List archives |
| `GET /api/archive/recall?q=` | Search archived sessions |
| `GET /api/search?sender=&q=` | Search messages by sender |

## Design

Apple-grade precision: restrained color, disciplined spacing/type, identity color confined to a thin edge + name dot (never background fills), motion ≤200ms transforms, real focus states, reduced-motion respected (except the functional typing indicator).
