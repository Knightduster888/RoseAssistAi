#!/usr/bin/env python3
"""
Realtime agent trigger.

When a new message from Corey lands in the inbox (via the viewer pill box),
the viewer calls trigger_agents() which spawns one-shot `hermes chat -q` runs
for Rose and Alex in the background. Each one-shot agent reads the shared
inbox (source of truth), engages with the newest user message, and delivers
its response to Corey's Telegram DM automatically.

This bypasses the cron polling latency (no waiting for a tick) — the agent
wakes within a second of the message being posted. Each run is an independent
fresh session; agents use the inbox as their memory, so this is safe.

Environment
-----------
HERMES_BIN  : path to the hermes CLI (default: /usr/local/bin/hermes)
"""
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime

import presence  # local module: agent presence/typing heartbeats

HERMES_BIN = os.environ.get("HERMES_BIN", "/usr/local/bin/hermes")
SHARED_DIR = "/root/shared-agents"
INBOX_DIR = os.path.join(SHARED_DIR, "inbox")

# Cooldown so bursts of rapid user messages don't stack up unbounded concurrent
# hermes runs, while still letting the NEXT message trigger a fresh run once the
# current one has had time to finish. Runs take ~7-13s, so 25s is safe.
# Key fix: this is a TIMESTAMP per profile, cleared by time passing — the old
# `_PENDING` set added on each trigger and never removed, so every realtime
# trigger after the first one was silently dropped.
_COOLDOWN_S = 25.0
_LAST_FIRED = {}
_LOCK = threading.Lock()


def _one_shot(profile, newest_file, subject, preview, dest_file):
    """Spawn a single background hermes run for `profile` to engage the newest
    user message. Runs detached (setsid, stdout to a log) so it survives the
    viewer process and doesn't block the HTTP response.

    Sets the agent's presence to "thinking" at spawn and clears it back to
    "idle" once the run finishes, so the viewer's typing indicator is truthful
    and always matches actual agent engagement.
    """
    presence_agent = "alex" if profile == "apk" else "rose"
    spawn_iso = presence.set_presence(presence_agent, "thinking")
    profile_author = presence_agent

    # Explicit source + destination paths let the agent reply in exactly two
    # file operations (one read, one write) instead of wandering the directory.
    src_path = os.path.join(INBOX_DIR, newest_file)
    # dest_file is the caller-chosen timestamped name (e.g. 20260802_HHMMSS_from-rose.md)
    dest_path = os.path.join(INBOX_DIR, dest_file)
    display_name = "Alex (A.P.K.)" if profile_author == "alex" else "Rose"

    prompt = (
        f"New message from the user Corey has just landed in the shared agent inbox.\n\n"
        f"SOURCE: read ONLY this file: {src_path}\n\n"
        f"Then reply to Corey as {display_name} with a short, warm, natural reply "
        f"to exactly what he said.{' Address him as a mate (Aussie tone).' if profile_author=='alex' else ''}\n\n"
        f"WRITE BACK: put your reply in this exact new file: {dest_path}\n"
        f"Write the file with this exact body (these headers verbatim):\n"
        f"# <your subject>\n"
        f"From: {display_name}\n"
        f"To: Corey\n"
        f"Date: <current date>\n"
        f"\n"
        f"<your reply — 2 to 4 sentences, conversational>\n\n"
        f"Do NOT explore the inbox or list directories. Read the SOURCE file, "
        f"then write the WRITE BACK file. That's it. No other tool calls. "
        f"When done, output a single line confirming the filename you wrote."
    )
    log = f"/tmp/realtime_{profile}.log"
    cmd = (
        f"{shlex.quote(HERMES_BIN)} --profile {profile} chat -q {shlex.quote(prompt)} "
        f"-t file --source realtime-inbox --yolo >> {log} 2>&1"
    )
    try:
        # setsid detaches so the child is its own session leader and survives
        # the viewer process terminating.
        subprocess.Popen(
            ["setsid", "bash", "-c", cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Clear the typing indicator after the run's typical lifetime, but only
        # if no NEWER trigger refreshed this agent's presence meanwhile (prevents
        # an old timer from erasing a fresh "thinking" signal from a later spawn).
        threading.Timer(
            30.0, lambda a=presence_agent, iso=spawn_iso: presence.clear_if_older(a, iso)
        ).start()
    except Exception as e:
        with _LOCK:
            _LAST_FIRED[profile] = 0.0  # release cooldown so it can retry
        print(f"[realtime_trigger] spawn failed for {profile}: {e}", file=sys.stderr)
        return False
    return True


def newest_corey_file():
    """Return the most recent from-corey message file name, or None."""
    if not os.path.isdir(INBOX_DIR):
        return None
    best = None
    for f in os.listdir(INBOX_DIR):
        if not (f.startswith("20") and "_from-corey" in f and f.endswith(".md")):
            continue
        fp = os.path.join(INBOX_DIR, f)
        if best is None or os.path.getmtime(fp) > os.path.getmtime(best):
            best = fp
    return os.path.basename(best) if best else None


def trigger_agents(target="both"):
    """Spawn background runs for the requested agent(s) when a user message lands.

    target: "rose" | "apk" | "both" (default both). Controls which agent wakes,
            so @-mention routing in the viewer only triggers the mentioned agent.
    Returns the list of profiles that were triggered.
    """
    newest = newest_corey_file()
    if not newest:
        return []
    fp = os.path.join(INBOX_DIR, newest)
    try:
        with open(fp, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        content = ""
    # Extract subject (first line) + a preview of the body.
    lines = [l for l in content.splitlines() if l.strip()]
    subject = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), newest)
    body_lines = [l for l in lines if not l.startswith(("From:", "To:", "Date:", "#"))]
    preview = " ".join(body_lines)[:300]

    profiles = {
        "rose": ["rose"],
        "apk": ["apk"],
        "both": ["rose", "apk"],
    }.get((target or "both").lower().strip(), ["rose", "apk"])

    fired = []
    now = time.monotonic()
    # One shared timestamp so both agents' reply files group nicely.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for profile in profiles:
        with _LOCK:
            last = _LAST_FIRED.get(profile, 0.0)
            if now - last < _COOLDOWN_S:
                continue  # a run for this agent just went out; let it finish
            _LAST_FIRED[profile] = now  # record firing time (released by time)
        author = "alex" if profile == "apk" else "rose"
        dest_file = f"{stamp}_from-{author}.md"
        if _one_shot(profile, newest, subject, preview, dest_file):
            fired.append(profile)
        else:
            with _LOCK:
                _LAST_FIRED[profile] = 0.0
    return fired


if __name__ == "__main__":
    # CLI test: python3 realtime_trigger.py
    import time
    print("newest corey file:", newest_corey_file())
    print("triggering agents...")
    print("fired:", trigger_agents())
    time.sleep(2)
    print("done.")
