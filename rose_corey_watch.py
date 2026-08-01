#!/usr/bin/env python3
"""
Rose's inbox watcher for USER messages.

The shared inbox /root/shared-agents/inbox/ is where message files from the
user (Corey, "from-corey") and from the two agents land. Alex's watcher only
reacts to agent-to-agent ("from-rose") traffic; this watcher makes ROSE aware
of when the USER posts a new message into the pill box, so she can engage.

Idempotent: tracks already-seen filenames in a state file so each message is
reported exactly once.

Output contract (consumed by the cron job prompt):
    NO_NEW                 -> nothing new, stay silent
    NEW_FROM_COREY         -> followed by the message content, one per new file
"""
import os
import re
import sys
import datetime

INBOX = "/root/shared-agents/inbox"
STATE_FILE = "/root/shared-agents/rose_corey_watch_state.txt"


def main():
    if not os.path.isdir(INBOX):
        print("NO_INBOX")
        return

    known = set()
    if os.path.exists(STATE_FILE):
        known = {ln.strip() for ln in open(STATE_FILE) if ln.strip()}

    fresh = []
    try:
        files = sorted(
            os.listdir(INBOX),
            key=lambda f: os.path.getmtime(os.path.join(INBOX, f)),
        )
    except Exception:
        files = []

    for f in files:
        if not f.endswith(".md"):
            continue
        if f in known:
            continue
        path = os.path.join(INBOX, f)
        # Author from filename convention: YYYYMMDD[_HHMMSS]_from-<author>[-topic].md
        m = re.match(r"\d{8}(?:_\d{6})?[_-]from-([a-z0-9]+)", f)
        author = m.group(1) if m else "?"
        if author.lower() != "corey":
            # Only report USER (from-corey) messages. Agent messages are
            # handled by the other watch loops.
            known.add(f)
            continue
        # Respect @-mention targeting: a message "Target: alex" is for Alex
        # only; Rose should not report it.
        try:
            meta = open(path, encoding="utf-8").read()
        except Exception:
            meta = ""
        tm = re.search(r"^Target\s*:\s*(\S+)\s*$", meta, re.MULTILINE | re.IGNORECASE)
        tgt = tm.group(1).lower() if tm else "both"
        if tgt in ("alex",):
            known.add(f)  # not for Rose; don't wake her on it
            continue
        try:
            content = open(path, encoding="utf-8").read()
        except Exception as e:
            content = f"(could not read: {e})"
        fresh.append((f, content))
        known.add(f)

    with open(STATE_FILE, "w") as fh:
        fh.write("\n".join(sorted(known)) + "\n")

    if not fresh:
        print("NO_NEW")
        return

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"NEW_FROM_COREY count={len(fresh)} at {now}")
    for f, content in fresh:
        print(f"=== FILE: {f} ===")
        lines = content.splitlines()
        print("\n".join(lines[:80]))
        if len(lines) > 80:
            print(f"[... {len(lines)-80} more lines ...]")


if __name__ == "__main__":
    main()
