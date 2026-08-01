#!/usr/bin/env python3
"""
Rose's /recall CLI — search the shared Rose<->Alex conversation history.

Usage:
    python3 recall_cli.py <word>
    python3 recall_cli.py <word> [--sender corey|rose|alex|all] [--max 5]

Searches both the live inbox (/root/shared-agents/inbox) and the archived
sessions (/root/shared-agents/archive), returning dated, contexted hits so
the results can be relayed back to the user (e.g. in a Telegram DM when the
user types "/recall <word>").

Exit code 0 if matches found, 1 if none.
"""
import json
import os
import re
import sys

SHARED_DIR = "/root/shared-agents"
INBOX_DIR = os.path.join(SHARED_DIR, "inbox")
ARCHIVE_DIR = os.path.join(SHARED_DIR, "archive")
INDEX_FILE = os.path.join(ARCHIVE_DIR, "index.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shared_viewer as v  # reuse parser + snippet helpers


def snippet(text, term, radius=90):
    low = text.lower()
    idx = low.find(term)
    if idx < 0:
        return text[: radius * 2].replace("\n", " ").strip()
    start = max(0, idx - radius)
    end = min(len(text), idx + len(term) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return (prefix + text[start:end].replace("\n", " ") + suffix).strip()


def search_live(term, sender="all"):
    hits = []
    for fname in sorted(os.listdir(INBOX_DIR) if os.path.isdir(INBOX_DIR) else []):
        if not fname.endswith(".md"):
            continue
        m = v.parse_message_file(os.path.join(INBOX_DIR, fname))
        if not m:
            continue
        if sender != "all" and m["sender"] != sender:
            continue
        hay = (m.get("body_md") or "").lower() + " " + (m.get("subject") or "").lower()
        if term.lower() in hay:
            hits.append({
                "kind": "live",
                "file": m["file"],
                "sender": m["sender"],
                "subject": m["subject"],
                "date": m["date"],
                "snippet": snippet(m.get("body_md") or "", term),
            })
    return hits


def search_archive(term, sender="all"):
    hits = []
    if not os.path.isfile(INDEX_FILE):
        return hits
    with open(INDEX_FILE, encoding="utf-8") as fh:
        entries = json.load(fh)
    for e in entries:
        folder = e.get("folder", "")
        tpath = os.path.join(folder, "transcript.md")
        if not os.path.isfile(tpath):
            continue
        text = open(tpath, encoding="utf-8").read()
        if sender != "all":
            parts = [p.lower() for p in (e.get("participants") or [])]
            if sender not in parts:
                continue
        if term.lower() in text.lower():
            hits.append({
                "kind": "archive",
                "heading": e.get("heading", ""),
                "date": e.get("date", ""),
                "id": e.get("id", ""),
                "folder": folder,
                "snippet": snippet(text, term),
            })
    return hits


def main():
    argv = sys.argv[1:]
    sender = "all"
    # strip --sender value (and the flag) from the positional list
    clean = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--sender":
            if i + 1 < len(argv):
                sender = argv[i + 1]
                i += 2
                continue
        clean.append(a)
        i += 1
    if sender not in ("all", "corey", "rose", "alex"):
        sender = "all"
    if not clean:
        print("usage: recall_cli.py <word> [--sender corey|rose|alex|all]")
        return 2
    term = " ".join(clean).lower()

    live = search_live(term, sender)
    arch = search_archive(term, sender)

    lines = [f"🔎 /recall {term}" + (f"  (sender: {sender})" if sender != "all" else "")]
    if not live and not arch:
        lines.append("No matches found in shared history.")
        print("\n".join(lines))
        return 1
    if live:
        lines.append(f"\n📌 Live conversation — {len(live)} match(es):")
        for h in live[:6]:
            who = {"corey": "You", "rose": "Rose", "alex": "Alex"}.get(h["sender"], h["sender"])
            lines.append(f"• {who} · {h['date']} · {h['subject'] or h['file']}")
            lines.append(f"    {h['snippet']}")
    if arch:
        lines.append(f"\n🗂️ Archived — {len(arch)} session(s):")
        for h in arch[:6]:
            lines.append(f"• {h['date']} · {h['heading']}")
            lines.append(f"    {h['snippet']}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
