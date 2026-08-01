"""
Rose<->Alex presence heartbeats.

Agents write a small JSON heartbeat into /root/shared-agents/presence/ so the
viewer can show a live "typing / thinking" signal while an agent is engaged.

Conventions
-----------
- File per agent: presence/rose.json, presence/alex.json
- Shape: {"state": "thinking"|"typing"|"idle", "agent": "rose", "at": iso-ts}
- "thinking"  = agent's watcher fired, the agent is now composing (cron run).
- "typing"    = agent is actively writing a response (optional granularity).
- "idle"      = agent finished / not engaged.
- A heartbeat is only "live" for a short window (see STALE_MS). The viewer
  treats anything older than that as absent (no typing bubble).

The viewer polls GET /api/presence (shared_viewer.py) which calls
read_presence() here.
"""
import json
import os
import time
from datetime import datetime, timezone

SHARED_DIR = "/root/shared-agents"
PRESENCE_DIR = os.path.join(SHARED_DIR, "presence")

# A heartbeat older than this is treated as stale/absent.
STALE_MS = 45_000
# A banner this recent from an agent is treated as "just posted".
JUST_POSTED_MS = 20_000


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now_ms():
    return time.time() * 1000


def ensure_dir():
    os.makedirs(PRESENCE_DIR, exist_ok=True)


def set_presence(agent, state):
    """Write a heartbeat for `agent` (rose|alex). Returns the ISO timestamp
    written (used as a reference point for safe clearing later)."""
    ensure_dir()
    path = os.path.join(PRESENCE_DIR, f"{agent}.json")
    payload = {"state": state, "agent": agent, "at": _now_iso()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return payload["at"]


def read_presence():
    """Return {agent: {state, at, live, ago_ms}} for current heartbeats.

    Only reports an agent when it has a heartbeat file with a recognised
    state and the heartbeat is still within the live window.
    """
    if not os.path.isdir(PRESENCE_DIR):
        return {}
    out = {}
    now = _now_ms()
    for fname in os.listdir(PRESENCE_DIR):
        if not fname.endswith(".json"):
            continue
        agent = fname[:-5]
        path = os.path.join(PRESENCE_DIR, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        state = (data.get("state") or "").strip().lower()
        if state not in ("thinking", "typing", "idle"):
            continue
        try:
            at = datetime.fromisoformat(data.get("at", "").replace("Z", "+00:00"))
            # naive -> assume UTC
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            ago_ms = now - at.timestamp() * 1000
        except Exception:
            ago_ms = STALE_MS + 1
        if ago_ms > STALE_MS:
            # stale -> treat as idle/absent
            out[agent] = {"state": "idle", "at": data.get("at"), "live": False, "ago_ms": ago_ms}
            continue
        out[agent] = {"state": state, "at": data.get("at"), "live": True, "ago_ms": ago_ms}
    return out


def clear_if_older(agent, before_iso):
    """Set `agent` to idle ONLY if its current heartbeat is not newer than
    `before_iso`. Used to safely end a typing period without clobbering a fresher
    "thinking"/"typing" signal from a subsequent trigger. Returns True if cleared,
    False if a fresher heartbeat was left intact.
    """
    try:
        before = datetime.fromisoformat(before_iso.replace("Z", "+00:00"))
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)
    except Exception:
        before = None
    path = os.path.join(PRESENCE_DIR, f"{agent}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cur = json.load(fh)
    except Exception:
        cur = None
    # If current heartbeat is stale or absent, safe to clear.
    if cur is None:
        set_presence(agent, "idle")
        return True
    try:
        cur_at = datetime.fromisoformat((cur.get("at") or "").replace("Z", "+00:00"))
        if cur_at.tzinfo is None:
            cur_at = cur_at.replace(tzinfo=timezone.utc)
    except Exception:
        cur_at = None
    # Only clear if current signal is NOT newer than our spawn reference.
    if before is None or cur_at is None or cur_at <= before:
        set_presence(agent, "idle")
        return True
    return False


def clear_all():
    """Remove all presence heartbeats (used on teardown/tests)."""
    if os.path.isdir(PRESENCE_DIR):
        for fname in os.listdir(PRESENCE_DIR):
            if fname.endswith(".json"):
                try:
                    os.remove(os.path.join(PRESENCE_DIR, fname))
                except OSError:
                    pass


if __name__ == "__main__":
    # quick self-test
    clear_all()
    print("after clear:", read_presence())
    set_presence("rose", "thinking")
    print("after set:", read_presence())
    clear_all()
