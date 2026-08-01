"""
Rose<->Alex Session Archive
===========================
Compresses a finished conversation (inbox) into a dated, headed archive
package and provides recall/search so BOTH agents can retrieve past context.

Archive layout (per session):
    /root/shared-agents/archive/
        <YYYY-MM-DD>_<heading-slug>/
            SUMMARY.md        # compressed human/LLM-readable summary
            transcript.md     # full original thread, verbatim, with headers
    /root/shared-agents/archive/index.json   # searchable registry (date, heading,
                                             # participants, summary blurb, paths)

Usage:
    from archive import archive_session, list_archive, recall, derive_heading

Designed to be called either:
  - by an agent (Rose/Alex) at the end of a session, OR
  - by the viewer when a "compress" action is requested.
"""
import json
import os
import re
import shutil
from datetime import datetime

SHARED_DIR = "/root/shared-agents"
INBOX_DIR = os.path.join(SHARED_DIR, "inbox")
ARCHIVE_DIR = os.path.join(SHARED_DIR, "archive")
INDEX_PATH = os.path.join(ARCHIVE_DIR, "index.json")


# --------------------------------------------------------------------------
# Heading derivation
# --------------------------------------------------------------------------
def derive_heading(messages):
    """Derive a short conversation heading from the thread.

    Prefers the subject/heading of the first substantive message; falls back
    to a purpose label based on participants."""
    if not messages:
        return "empty-session"
    # First message with a non-generic subject wins
    for m in messages:
        subj = (m.get("subject") or "").strip()
        low = subj.lower()
        if subj and not any(
            g in low for g in ("re:", "direct communication", "established", "received")
        ):
            return subj
    # fall back to participant-based label
    senders = sorted({m.get("sender", "?") for m in messages})
    return " & ".join(s.capitalize() for s in senders) + " collaboration"


def _slugify(text, max_len=48):
    """Turn text into a filesystem-safe slug."""
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:max_len] or "session"


# --------------------------------------------------------------------------
# Compression
# --------------------------------------------------------------------------
def compress_thread(messages):
    """Produce a compressed representation of a thread.

    Returns (summary_md, transcript_md) where:
      - summary_md is a tight, decision-oriented digest
      - transcript_md is the full original thread, verbatim
    """
    if not messages:
        return ("# (empty session)\n", "")

    lines_summary = ["# Session Summary", ""]
    lines_summary.append(f"- Messages: {len(messages)}")
    lines_summary.append(f"- Date range: {messages[0].get('date','?')} → {messages[-1].get('date','?')}")
    lines_summary.append("- Participants: " + ", ".join(sorted({m.get('sender','?').capitalize() for m in messages})))
    lines_summary.append("")

    # Collapse repeated topics: group by subject heading.
    groups = []
    cur = None
    for m in messages:
        subj = (m.get("subject") or "").strip() or "(no subject)"
        if cur is None or cur["subject"] != subj:
            cur = {"subject": subj, "msgs": []}
            groups.append(cur)
        cur["msgs"].append(m)

    for g in groups:
        lines_summary.append(f"## {g['subject']}")
        for m in g["msgs"]:
            who = m.get("sender", "?").capitalize()
            body = (m.get("body_md") or "").strip()
            # first non-header line as a digest snippet
            snippet = " ".join(body.split())[:160]
            lines_summary.append(f"- {who}: {snippet}")
        lines_summary.append("")

    summary_md = "\n".join(lines_summary).strip() + "\n"

    # Full transcript: reconstruct with clear separators
    t = []
    for m in messages:
        who = m.get("sender", "?").capitalize()
        t.append(f"--- {who} · {m.get('date','')} · {m.get('subject','')} ---")
        t.append((m.get("body_md") or "").strip())
        t.append("")
    transcript_md = "\n".join(t).strip() + "\n"

    return summary_md, transcript_md


# --------------------------------------------------------------------------
# Archive write
# --------------------------------------------------------------------------
def archive_session(messages, heading=None, tag="auto", dedupe=False):
    """Seal the given messages into a dated, headed archive package.

    Returns the archive folder path. If `dedupe` is True, skips messages that
    are already present in a previous archived transcript."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    ts = datetime.now()
    date_str = ts.strftime("%Y-%m-%d")
    stamp = ts.strftime("%Y%m%d_%H%M%S")

    heading = heading or derive_heading(messages)
    slug = _slugify(heading)

    # Unique, dated folder: YYYY-MM-DD_<stamp>_<slug>
    folder = os.path.join(ARCHIVE_DIR, f"{date_str}_{stamp}_{slug}")
    os.makedirs(folder, exist_ok=True)

    summary_md, transcript_md = compress_thread(messages)

    summary_path = os.path.join(folder, "SUMMARY.md")
    transcript_path = os.path.join(folder, "transcript.md")
    thread_path = os.path.join(folder, "thread.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary_md)
    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(transcript_md)
    # Structured copy so the original chat can be re-expanded inline with
    # the same sender/date/avatar mapping (not just markdown).
    with open(thread_path, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)

    # --- update index ---
    index = _load_index()
    entry = {
        "id": f"{date_str}_{slug}",
        "date": date_str,
        "timestamp": ts.isoformat(),
        "heading": heading,
        "folder": folder,
        "summary": os.path.relpath(summary_path, ARCHIVE_DIR),
        "transcript": os.path.relpath(transcript_path, ARCHIVE_DIR),
        "thread": os.path.relpath(thread_path, ARCHIVE_DIR),
        "message_count": len(messages),
        "participants": sorted({m.get("sender", "?") for m in messages}),
        "tag": tag,
    }
    # keep newest first
    index = [e for e in index if e.get("id") != entry["id"]]
    index.insert(0, entry)
    _save_index(index)

    return folder


def load_thread(folder):
    """Load the structured thread (list of message dicts) for an archived
    session folder. Returns [] if not available."""
    thread_path = os.path.join(folder, "thread.json")
    if not os.path.isfile(thread_path):
        return []
    try:
        with open(thread_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _load_index():
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_index(index):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Recall / search
# --------------------------------------------------------------------------
def list_archive():
    """Return all archive index entries, newest first."""
    return _load_index()


def recall(keyword, scope="all"):
    """Search archived sessions.

    scope: 'all' | 'summary' | 'transcript'
    Returns a list of matched {archive_entry, matched_file, snippet} dicts.
    """
    keyword = keyword.lower().strip()
    if not keyword:
        return []
    index = _load_index()
    results = []
    for entry in index:
        paths = []
        if scope in ("all", "summary"):
            paths.append(entry.get("summary"))
        if scope in ("all", "transcript"):
            paths.append(entry.get("transcript"))
        for p in paths:
            fp = os.path.join(ARCHIVE_DIR, p) if p else None
            if not fp or not os.path.isfile(fp):
                continue
            try:
                text = open(fp, encoding="utf-8").read().lower()
            except OSError:
                continue
            if keyword in text:
                idx = text.find(keyword)
                start = max(0, idx - 80)
                end = min(len(text), idx + 160)
                results.append({
                    "entry": entry,
                    "file": os.path.basename(p),
                    "snippet": text[start:end].strip(),
                })
    return results


if __name__ == "__main__":
    # lightweight self-test
    demo = [
        {"sender": "rose", "date": "2026-07-31", "subject": "Design brief",
         "body_md": "We should build an Apple-quality UI with restrained color."},
        {"sender": "alex", "date": "2026-07-31", "subject": "Design brief",
         "body_md": "Agreed, let's use Inter and respect prefers-reduced-motion."},
    ]
    folder = archive_session(demo, heading="Shared workspace design")
    print("archived to:", folder)
    print("recall 'Inter':", len(recall("Inter")))
    print("index entries:", len(list_archive()))
