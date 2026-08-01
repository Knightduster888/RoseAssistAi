"""
Rose<->Alex Shared Conversation Viewer
======================================
Serves a single-page HTML view of the inter-agent conversation tracked in
/root/shared-agents/inbox/ (timestamped markdown message files), plus any
work artifacts referenced there.

v2 features:
- Left hamburger drawer: previous sessions (archive) + search (with sender
  filter for the user's own interactions).
- Bottom chat pill box: the user (Corey) can post messages which are written
  into the inbox as "From: Corey" for both agents to see. A "/recall <word>"
  typed in the box searches chat history in place.
- Messages render collapsed by default; tap a message to expand its full body
  (saves scrolling long threads).

Design goals:
- Single source of truth: the inbox directory. The viewer is primarily a lens
  that never rewrites conversation files (the only write it performs is when
  the user explicitly posts a new message from the pill box).
- Lightweight: stdlib http.server, no Flask dependency at runtime.
- Live: the page polls GET /api/threads for new messages.

Run:
    python3 shared_viewer.py   # serves on 0.0.0.0:8002
"""

import html
import json
import os
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import archive  # session archive/compression (local module)
import presence  # agent presence/typing heartbeats (local module)
import realtime_trigger  # spawn agent runs immediately on new user message (local module)

SHARED_DIR = "/root/shared-agents"
INBOX_DIR = os.path.join(SHARED_DIR, "inbox")
ARTIFACTS_DIR = os.path.join(SHARED_DIR, "artifacts")  # optional work outputs
ARCHIVE_DIR = os.path.join(SHARED_DIR, "archive")
PORT = 8002


def parse_message_file(path):
    """Parse a timestamped markdown message into a structured dict.

    Expected layout (matches the convention Alex + Rose use):
        # <Subject>            (optional title line)
        From: <name>
        To: <name>
        Date: <string>
        <blank>
        <body markdown>
    Senders are normalised to: rose | alex | corey | other so the UI can
    colour them. Corey is the human user (owner of the workspace).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None

    name = os.path.basename(path)

    # --- Headers --------------------------------------------------------
    meta = {"from": "", "to": "", "date": "", "subject": "", "re": ""}
    # First markdown heading (# ...) is the title; the real Subject comes
    # from a "Subject:" header if present (fall back to title).
    title = ""
    subject_m = re.search(r"^#\s+(.+?)\s*$", raw, re.MULTILINE)
    if subject_m:
        title = subject_m.group(1).strip()

    lines = raw.splitlines()

    # Header block: collect "Key: value" header lines at the top. A leading
    # "# Title" line (and blank lines) come first and are skipped, then the
    # headers, then the body. The body starts just after the last header.
    header_lines = {}
    last_header_idx = -1
    for i, line in enumerate(lines):
        sl = line.strip()
        if sl.startswith("#") or sl == "":
            continue  # skip leading title/blank lines
        hm = re.match(r"^(From|To|Date|Subject|Re|Cc)\s*:\s*(.+)$", sl, re.IGNORECASE)
        if hm:
            header_lines[hm.group(1).lower()] = hm.group(2).strip()
            last_header_idx = i
        else:  # non-empty, not a header -> body begins
            break
    body_start = last_header_idx + 1  # first line after headers
    # skip the blank line separator if present
    while body_start < len(lines) and lines[body_start].strip() == "":
        body_start += 1

    meta["from"] = header_lines.get("from", "")
    meta["to"] = header_lines.get("to", "")
    meta["date"] = header_lines.get("date", "")
    meta["subject"] = header_lines.get("subject", "") or title
    meta["re"] = header_lines.get("re", "")

    body = "\n".join(lines[body_start:]).strip()

    # Normalise the author name so the UI can colour it.
    author = meta["from"].strip().lower()
    if "alex" in author or "apk" in author or "a.p.k" in author:
        sender = "alex"
    elif "rose" in author:
        sender = "rose"
    elif "corey" in author or "lazzarotto" in author:
        sender = "corey"
    else:
        sender = "other"

    # Fallback date from filename "20260731_from-rose-intro.md"
    ts = meta["date"] or ""
    if not ts:
        m = re.match(r"^(\d{8})_", name)
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                ts = name
    if not ts:
        ts = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")

    return {
        "file": name,
        "from": meta["from"] or "unknown",
        "to": meta["to"] or "unknown",
        "subject": meta["subject"],
        "date": ts,
        "sender": sender,
        "body_md": body,
        "body_html": md_to_html(body),
        "mtime": os.path.getmtime(path),
    }


def md_to_html(text):
    """Tiny, safe markdown-subset renderer (paragraphs, **bold**, `code`,
    - bullets, * bullets, headings ##/###). The first `# Title` line is
    suppressed (already shown as the card subject). Input is escaped first;
    never outputs raw HTML from untrusted content."""
    esc = html.escape(text)
    out = []
    in_list = False
    first = True
    for line in esc.splitlines():
        s = line
        if s.startswith("# ") and first:
            first = False
            continue  # drop the document title (rendered as the subject)
        if s.startswith("## ") or s.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<h4>" + re.sub(r"^#+\s*", "", s).strip() + "</h4>")
            continue
        m = re.match(r"^(\s*)[-*]\s+(.*)$", s)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline(m.group(2)) + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if s.strip() == "":
            continue
        out.append("<p>" + inline(s) + "</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def inline(s):
    """Bold + inline code only. Applied after escaping."""
    # code first (so markers inside code don't get bolded)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    return s


def _snippet(text, term, radius=90):
    """Return a short excerpt of `text` centred on the first occurrence of `term`."""
    low = text.lower()
    idx = low.find(term)
    if idx < 0:
        return text[: radius * 2].replace("\n", " ").strip()
    start = max(0, idx - radius)
    end = min(len(text), idx + len(term) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return (prefix + text[start:end].replace("\n", " ") + suffix).strip()


def list_threads():
    """Return all message files in the inbox, oldest first."""
    items = []
    if not os.path.isdir(INBOX_DIR):
        return items
    for fname in sorted(os.listdir(INBOX_DIR)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(INBOX_DIR, fname)
        item = parse_message_file(fpath)
        if item is not None:
            items.append(item)
    return items


def render_thread_html(items):
    """Build the full HTML page body with the conversation embedded."""
    bubbles = []
    for m in items:
        cls = "rose" if m["sender"] == "rose" else ("alex" if m["sender"] == "alex" else ("corey" if m["sender"] == "corey" else "other"))
        name = "Rose" if m["sender"] == "rose" else ("Alex" if m["sender"] == "alex" else ("You" if m["sender"] == "corey" else m["from"]))
        date_disp = m["date"] or datetime.fromtimestamp(m["mtime"]).strftime("%Y-%m-%d")
        subject = m["subject"]
        body = m["body_html"]
        avatar = avatar_for(m["sender"])
        bubbles.append(
            f"""<article class="msg {cls}">
                {avatar}
                <div class="inner">
                    <div class="head" onclick="this.parentNode.parentNode.classList.toggle('open')" role="button" tabindex="0">
                        <span class="name-dot"></span><span class="name">{html.escape(name)}</span>
                        {('<span class="subj-txt">'+html.escape(subject)+"</span>") if subject else ""}
                        <span class="when">{html.escape(date_disp)}</span>
                        <span class="exp-chev">▸</span>
                    </div>
                    <div class="preview">{html.escape((m['body_md'] or '').replace(chr(10),' ')[:160])}{'…' if len(m['body_md'] or '')>160 else ''}</div>
                    <div class="body">{body}</div>
                </div>
            </article>"""
        )
    if not bubbles:
        bubbles.append(
            '<div class="msg empty">No messages yet. Drop a timestamped .md file '
            "in <code>/root/shared-agents/inbox/</code>.</div>"
        )
    return "".join(bubbles)


def avatar_for(sender):
    """Return an <img class=avatar> tag for a sender, or an initial-letter span."""
    path = {"rose": "rose.jpg", "alex": "alex.jpg"}.get(sender)
    if not path:
        ch = "C" if sender == "corey" else ("?" if sender in ("other", "unknown") else (sender[0] or "?").upper())
        return f'<div class="avatar av-initial av-{("corey" if sender == "corey" else "other")}">{ch}</div>'
    return f'<img class="avatar" src="/assets/avatars/{path}" alt="{sender}" loading="lazy">'


def build_presence_payload():
    """Combine agent heartbeats with 'just posted' detection from the inbox.

    Returns {"agents": {...}, "active": [names with a live signal]}.
    An agent is 'active' if:
      - its presence heartbeat is live and state is thinking/typing, OR
      - it just posted a new message (within the recent window) and has no
        newer explicit idle heartbeat.
    """
    heartbeats = presence.read_presence()
    active = []
    for agent, hb in heartbeats.items():
        if hb.get("live") and hb.get("state") in ("thinking", "typing"):
            active.append(agent)
    return {"agents": heartbeats, "active": sorted(set(active))}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            items = list_threads()
            page = UI_TEMPLATE.replace("<!--THREAD-->", render_thread_html(items))
            self._html(page)
        elif path == "/api/threads":
            self._json({"messages": list_threads()})
        elif path == "/api/presence":
            # live agent typing/thinking signal
            self._json(build_presence_payload())
        elif path == "/api/archive":
            # list archived sessions
            self._json({"archives": archive.list_archive()})
        elif path == "/api/archive/recall":
            # search archived sessions: /api/archive/recall?q=<term>
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            from urllib.parse import parse_qs
            term = parse_qs(q).get("q", [""])[0]
            scope = parse_qs(q).get("scope", ["all"])[0]
            if not term:
                self._json({"error": "missing q param"}, 400)
                return
            results = archive.recall(term, scope)
            self._json({"query": term, "results": results})
        elif path == "/api/archive/file":
            # serve an archived markdown file safely: /api/archive/file?path=<rel-path>
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            from urllib.parse import parse_qs
            rel = parse_qs(q).get("path", [""])[0]
            if not rel:
                self._json({"error": "missing path"}, 400)
                return
            base = os.path.abspath(ARCHIVE_DIR)
            target = os.path.abspath(os.path.join(base, rel))
            if (
                os.path.commonpath([base, target]) != base
                or not os.path.isfile(target)
            ):
                self._json({"error": "not found"}, 404)
                return
            text = open(target, encoding="utf-8").read()
            self._html(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>Archive</title><style>body{background:#0e1116;color:#e6edf3;"
                "font-family:system-ui,sans-serif;max-width:760px;margin:30px auto;"
                "padding:0 16px;line-height:1.6}pre{white-space:pre-wrap;"
                "font-family:inherit}code{background:#1c232e;padding:1px 5px;"
                "border-radius:4px}</style></head><body><a href='/'>← back</a><br>"
                + md_to_html(text)
                + "</body></html>"
            )
        elif path == "/api/archive/thread":
            # return the structured thread for inline expansion:
            # /api/archive/thread?folder=<abs-or-rel-folder>
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            from urllib.parse import parse_qs
            folder = parse_qs(q).get("folder", [""])[0]
            if not folder:
                self._json({"error": "missing folder"}, 400)
                return
            # accept either absolute or archive-relative folder path
            if not os.path.isabs(folder):
                folder = os.path.abspath(os.path.join(ARCHIVE_DIR, folder))
            base = os.path.abspath(ARCHIVE_DIR)
            if os.path.commonpath([base, folder]) != base:
                self._json({"error": "denied"}, 403)
                return
            msgs = archive.load_thread(folder)
            for m in msgs:
                if "body_html" not in m:
                    m["body_html"] = md_to_html(m.get("body_md") or "")
            self._json({"folder": folder, "messages": msgs})
        elif path == "/api/search":
            # full-text search across live inbox + archive, with optional sender filter:
            # /api/search?q=<term>&sender=corey|rose|alex|all
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            from urllib.parse import parse_qs
            term = parse_qs(q).get("q", [""])[0].strip().lower()
            sender_f = parse_qs(q).get("sender", ["all"])[0].strip().lower()
            if not term:
                self._json({"error": "missing q param"}, 400)
                return
            results = {"live": [], "archive": []}
            # live inbox search (respect sender filter)
            for m in list_threads():
                if sender_f not in ("all", m["sender"]):
                    continue
                hay = (m.get("body_md") or "").lower()
                if term in hay or term in (m.get("subject") or "").lower():
                    results["live"].append({
                        "file": m["file"], "subject": m["subject"],
                        "date": m["date"], "sender": m["sender"],
                        "snippet": _snippet(m.get("body_md") or "", term),
                    })
            # archive search (recall gives matches per archived session)
            for hit in archive.recall(term, "all"):
                entry = hit.get("entry") or {}
                if sender_f != "all":
                    # recall doesn't filter by sender; skip if mismatch on participants
                    parts = [p.lower() for p in (entry.get("participants") or [])]
                    if sender_f not in parts:
                        continue
                results["archive"].append({
                    "id": entry.get("id"), "heading": entry.get("heading"),
                    "date": entry.get("date"), "folder": entry.get("folder"),
                    "file": hit.get("file"), "snippet": hit.get("snippet"),
                })
            self._json({"query": term, "sender": sender_f, "results": results})
        elif path.startswith("/assets/"):
            self._serve_asset(path)
        elif path == "/api/message":
            # raw markdown for a single file
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            from urllib.parse import parse_qs
            fname = parse_qs(q).get("file", [""])[0]
            # sanitise: only allow names inside inbox
            safe = os.path.basename(fname)
            fp = os.path.join(INBOX_DIR, safe)
            if os.path.exists(fp) and "/" not in fname and ".." not in fname:
                self._html(
                    "<pre>" + html.escape(open(fp, encoding="utf-8").read()) + "</pre>",
                    code=200,
                )
            else:
                self._json({"error": "not found"}, 404)
        elif path == "/api/artifacts":
            arts = []
            if os.path.isdir(ARTIFACTS_DIR):
                for r, _, fns in os.walk(ARTIFACTS_DIR):
                    for fn in fns:
                        fp = os.path.join(r, fn)
                        arts.append(
                            {
                                "name": os.path.relpath(fp, ARTIFACTS_DIR),
                                "path": "/artifacts/" + os.path.relpath(fp, ARTIFACTS_DIR),
                                "size": os.path.getsize(fp),
                            }
                        )
            self._json({"artifacts": arts})
        elif path.startswith("/artifacts/"):
            rel = path[len("/artifacts/"):]
            safe = os.path.normpath(rel)
            fp = os.path.join(ARTIFACTS_DIR, safe)
            if os.path.isfile(fp) and os.path.commonpath(
                [os.path.abspath(fp), os.path.abspath(ARTIFACTS_DIR)]
            ) == os.path.abspath(ARTIFACTS_DIR):
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        """Handle archive-compress (POST /api/archive) and user-message posting
        (POST /api/message with a JSON body {"text": "...", "subject": "..."})."""
        path = self.path.split("?")[0]

        if path == "/api/message":
            self._post_message()
            return

        if path != "/api/archive":
            self._json({"error": "not found"}, 404)
            return
        from urllib.parse import parse_qs
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = parse_qs(q)
        clear = params.get("clear", ["0"])[0] in ("1", "true", "yes")
        heading = params.get("heading", [""])[0] or None

        messages = list_threads()
        if not messages:
            self._json({"error": "nothing to archive"}, 400)
            return

        folder = archive.archive_session(messages, heading=heading, tag="manual")
        archived = [m["file"] for m in messages]
        if clear:
            removed = []
            for fname in archived:
                fp = os.path.join(INBOX_DIR, fname)
                try:
                    if os.path.exists(fp) and os.path.isfile(fp):
                        os.remove(fp)
                        removed.append(fname)
                except OSError:
                    pass
        else:
            removed = []
        self._json({
            "ok": True,
            "archived_to": folder,
            "message_count": len(messages),
            "cleared_inbox": clear,
            "removed": removed,
        })

    def _post_message(self):
        """Write a user (Corey) message into the inbox so the targeted agent(s)
        can see it. Supports @-mentions:

          @rose  ...   -> routed to Rose only      (To: Rose)
          @alex  ...   -> routed to Alex only      (To: Alex)
          @both / none -> routed to both agents    (To: Rose, Alex)

        The @mention token is stripped from the stored body.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._json({"error": "invalid JSON"}, 400)
            return
        text = (payload.get("text") or "").strip()
        if not text:
            self._json({"error": "missing text"}, 400)
            return

        # Parse @-mention target from the leading token: @rose / @alex / @both
        target = "both"
        m = re.match(r"^@([a-zA-Z]+)\s*(.*)$", text, re.DOTALL)
        if m:
            mention = m.group(1).lower()
            rest = m.group(2).strip()
            if mention in ("rose", "rosebot"):
                target = "rose"
                text = rest
            elif mention in ("alex", "alexbot", "keating"):
                target = "alex"
                text = rest
            elif mention in ("both", "all", "everyone"):
                target = "both"
                text = rest
            else:
                # unknown mention -> keep text as-is, route to both
                target = "both"
        if not text:
            self._json({"error": "missing text"}, 400)
            return

        to_label = {"rose": "Rose", "alex": "Alex", "both": "Rose, Alex"}[target]
        subject = (payload.get("subject") or text[:70]).strip() or "Message from Corey"

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{ts}_from-corey.md"
        path = os.path.join(INBOX_DIR, fname)
        body = (
            f"# {subject}\n"
            "From: Corey\n"
            f"To: {to_label}\n"
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"Target: {target}\n"
            "\n"
            f"{text}\n"
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        item = parse_message_file(path) or {}

        # Realtime: wake the relevant agent(s) immediately and signal presence.
        try:
            if target == "rose":
                presence.set_presence("rose", "thinking")
            elif target == "alex":
                # alex writes his own presence in his watcher; nothing for Rose here
                pass
            else:
                presence.set_presence("rose", "thinking")
        except Exception:
            pass
        try:
            realtime_trigger.trigger_agents(target)
        except Exception:
            pass  # never block the response on a background trigger failure

        self._json({"ok": True, "file": fname, "message": item, "target": target}, 201)

    def _serve_asset(self, path):
        """Serve files from the assets dir safely (avoids traversal)."""
        rel = path[len("/assets/"):]
        base = os.path.abspath(os.path.join(SHARED_DIR, "assets"))
        target = os.path.abspath(os.path.join(base, rel))
        if (
            os.path.commonpath([base, target]) != base
            or not os.path.isfile(target)
        ):
            self._json({"error": "not found"}, 404)
            return
        ctype = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(rel)[1].lower(), "application/octet-stream")
        with open(target, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quiet the access log
        pass


# ---------------------------------------------------------------------------
# HTML template (v2: hamburger drawer + session history + search, collapsed
# messages, bottom chat pill box for the user, /recall support)
# ---------------------------------------------------------------------------
UI_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AppVantage AI &mdash; Shared Workspace</title>
<style>
  :root{
    --bg:#0e1116; --panel:#161b22; --panel2:#1c232e;
    --line:#2a3340; --text:#e6edf3; --muted:#8b98a7;
    --rose:#e2558a; --alex:#58a6ff; --corey:#e3b341;
    --accent:#7ee787; --accent-dim:rgba(126,231,135,.12);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  :focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
  button{font-family:inherit}

  /* ---- Top bar ---- */
  header{position:sticky;top:0;z-index:20;background:rgba(14,17,22,.92);
         backdrop-filter:blur(6px);border-bottom:1px solid var(--line);
         padding:10px 16px;display:flex;align-items:center;gap:12px;min-height:56px}
  .burger{width:44px;height:44px;flex-shrink:0;display:flex;align-items:center;justify-content:center;
         background:var(--panel);border:1px solid var(--line);border-radius:10px;color:var(--text);
         cursor:pointer;transition:border-color .18s ease,background .18s ease}
  .burger:hover{background:var(--panel2)}
  .burger svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round}
  header h1{font-size:17px;margin:0;font-weight:650;letter-spacing:.2px;white-space:nowrap}
  header .sub{color:var(--muted);font-size:12.5px;margin-left:auto;white-space:nowrap}
  @media(max-width:640px){header .sub{display:none}}

  /* ---- Brand logo: AppVantage AI ---- */
  .brand{display:flex;align-items:center;gap:10px;user-select:none}
  .brand-mark{width:34px;height:34px;flex-shrink:0;display:block;
       filter:drop-shadow(0 0 10px rgba(88,166,255,.35))}
  .brand-core{transition:transform .2s ease}
  .brand:hover .brand-core{transform:translateY(-1px)}
  .brand-orbit{transform-origin:40px 9px;animation:orbit 5s linear infinite}
  @keyframes orbit{from{transform:rotate(0)}to{transform:rotate(360deg)}}
  .brand-name{font-size:17px;font-weight:700;letter-spacing:.3px;white-space:nowrap;
       color:var(--text)}
  .brand-name em{font-style:normal;font-weight:700;
       background:linear-gradient(90deg,#7ee787,#58a6ff,#e2558a);
       -webkit-background-clip:text;background-clip:text;color:transparent}
  @media(prefers-reduced-motion:reduce){.brand-orbit{animation:none}}

  /* ---- Drawer (left hamburger menu) ---- */
  .scrim{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:25;opacity:0;
         pointer-events:none;transition:opacity .18s ease}
  .scrim.open{opacity:1;pointer-events:auto}
  .drawer{position:fixed;top:0;left:0;bottom:0;width:min(348px,88vw);background:var(--panel);
         border-right:1px solid var(--line);z-index:30;transform:translateX(-100%);
         transition:transform .2s ease;display:flex;flex-direction:column}
  .drawer.open{transform:translateX(0)}
  .drawer-head{display:flex;align-items:center;gap:10px;padding:16px 16px 12px;
         border-bottom:1px solid var(--line)}
  .drawer-head h2{font-size:15px;margin:0;font-weight:650}
  .drawer-head .close{margin-left:auto;width:44px;height:44px;background:none;border:none;
         color:var(--muted);font-size:22px;cursor:pointer;line-height:1}
  .drawer-head .close:hover{color:var(--text)}

  /* search inside drawer */
  .draw-search{padding:12px 14px 6px;display:flex;flex-direction:column;gap:8px}
  .s-row{display:flex;gap:8px}
  .draw-search input{flex:1;background:var(--bg);border:1px solid var(--line);color:var(--text);
         border-radius:22px;padding:9px 14px;font-size:13px;outline:none}
  .draw-search input:focus{border-color:var(--accent)}
  .s-tag{color:var(--muted);font-size:11px;padding:0 3px}
  .chip-row{display:flex;gap:6px;flex-wrap:wrap}
  .chip{background:var(--panel2);color:var(--muted);border:1px solid var(--line);border-radius:20px;
        padding:5px 12px;font-size:11.5px;cursor:pointer;font-weight:600;min-height:30px;
        transition:border-color .18s ease,color .18s ease}
  .chip:hover{border-color:var(--muted)}
  .chip.active{color:var(--text);border-color:var(--accent);background:var(--accent-dim)}

  /* search results + session history share a scroll area */
  .drawer-scroll{flex:1;overflow-y:auto;padding:12px 14px 20px}
  .drawer-scroll h3{font-size:11.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);
        margin:6px 0 10px}
  .sess{display:flex;flex-direction:column;gap:10px}
  .sess-card{border:1px solid var(--line);border-radius:12px;background:var(--bg);
        padding:12px 13px;cursor:pointer;transition:border-color .18s ease}
  .sess-card:hover{border-color:var(--muted)}
  .sess-title{font-size:13.5px;font-weight:600;line-height:1.35}
  .sess-meta{color:var(--muted);font-size:11px;margin-top:5px}
  .sess-expand{margin-top:9px;border-top:1px solid var(--line);padding-top:9px;display:none}
  .sess-card.open .sess-expand{display:block}
  .srch-result{padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);
        margin-bottom:8px;font-size:12.5px;line-height:1.5}
  .srch-result .r-meta{color:var(--muted);font-size:10.5px;margin-top:4px}
  .draw-note{color:var(--muted);font-size:12px;line-height:1.5}
  .draw-note code{background:var(--panel2);padding:1px 5px;border-radius:4px}

  /* ---- Main content ---- */
  .wrap{max-width:780px;margin:0 auto;padding:20px 18px 120px}
  .pane-title{color:var(--muted);font-size:12.5px;margin:4px 0 18px;line-height:1.6}

  /* Message cards: collapsed by default, left-rule accent, tap to expand */
  .msg{display:flex;gap:12px;margin-bottom:12px;align-items:flex-start;
       border-left:4px solid transparent;padding-left:2px;border-radius:4px}
  .msg.rose{border-left-color:var(--rose)}
  .msg.alex{border-left-color:var(--alex)}
  .msg.corey{border-left-color:var(--corey)}
  .msg.other{border-left-color:var(--muted)}
  .avatar{width:38px;height:38px;border-radius:50%;object-fit:cover;flex-shrink:0;
       border:1px solid var(--line);background:var(--panel2)}
  .msg.rose .avatar{border-color:var(--rose)}
  .msg.alex .avatar{border-color:var(--alex)}
  .msg.corey .avatar{border-color:var(--corey)}
  .av-initial{display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px}
  .av-corey{color:var(--corey)} .av-other{color:var(--muted)}
  .inner{flex:1;min-width:0;border:1px solid var(--line);border-radius:12px;
       background:var(--panel);overflow:hidden}
  .head{display:flex;align-items:center;gap:8px;padding:9px 13px;cursor:pointer;min-height:44px;
       user-select:none}
  .name-dot{display:inline-block;width:8px;height:8px;border-radius:50%}
  .msg.rose .name-dot{background:var(--rose)} .msg.alex .name-dot{background:var(--alex)}
  .msg.corey .name-dot{background:var(--corey)} .msg.other .name-dot{background:var(--muted)}
  .name{font-weight:650;font-size:13.5px}
  .msg.rose .name{color:var(--rose)} .msg.alex .name{color:var(--alex)}
  .msg.corey .name{color:var(--corey)}
  .head .subj-txt{color:var(--text);font-size:12.5px;font-weight:500;overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap;max-width:60%}
  .when{margin-left:auto;color:var(--muted);font-size:11px;flex-shrink:0}
  .exp-chev{color:var(--muted);font-size:11px;width:18px;text-align:center;flex-shrink:0;
       transition:transform .18s ease}
  .msg.open .exp-chev{transform:rotate(90deg)}
  .preview{color:var(--muted);font-size:12.5px;padding:0 13px 10px;line-height:1.5;
       white-space:pre-wrap}
  .body{padding:2px 13px 13px;font-size:14px;line-height:1.65;color:var(--text);display:none}
  .msg.open .body{display:block}
  .msg.open .preview{display:none}
  .body code{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
       padding:1px 5px;font-size:12.5px}
  .body pre{background:#0b0e12;border:1px solid var(--line);border-radius:8px;
       padding:10px 12px;overflow:auto;font-size:12.5px}

  .empty{text-align:center;color:var(--muted);padding:40px 20px;border:1px dashed
       var(--line);border-radius:12px;background:var(--panel)}
  /* ---- Presence / typing indicator (avatar-strip) ---- */
  .presence-strip{display:flex;gap:22px;padding:4px 2px 18px;margin-bottom:4px;
       border-bottom:1px solid var(--line);align-items:flex-start}
  .presence-item{display:flex;flex-direction:column;align-items:center;gap:6px;
       min-width:52px}
  .presence-item .avatar{width:46px;height:46px;border-radius:50%;object-fit:cover;
       border:2px solid var(--line);box-shadow:0 2px 8px rgba(0,0,0,.25)}
  .presence-item.rose .avatar{border-color:var(--rose)}
  .presence-item.alex .avatar{border-color:var(--alex)}
  .presence-item .p-name{font-size:11px;color:var(--muted);font-weight:600;
       letter-spacing:.02em}
  .presence-item .p-state{display:none;align-items:center;gap:7px;font-size:12px;
       color:var(--text);padding:4px 12px;border-radius:20px;
       background:var(--panel2);border:1px solid var(--line);font-weight:600}
  .presence-item.busy .p-state{display:inline-flex}
  .presence-item.rose .p-state{border-color:rgba(226,85,138,.55);color:var(--rose)}
  .presence-item.alex .p-state{border-color:rgba(88,166,255,.55);color:var(--alex)}
  .p-dots{display:inline-flex;gap:4px;align-items:center}
  .p-state .p-label{font-style:normal;font-size:11px;opacity:.9}
  .p-dots span{width:8px;height:8px;border-radius:50%;background:currentColor;
       animation:blink 1.1s ease-in-out infinite both}
  .p-dots span:nth-child(2){animation-delay:.15s}
  .p-dots span:nth-child(3){animation-delay:.3s}
  /* Never drop below 45% opacity so the indicator is always clearly visible —
     the dots "breathe" bright then settle, but never disappear. */
  @keyframes blink{0%,80%,100%{opacity:.45;transform:translateY(0)}
       40%{opacity:1;transform:translateY(-3px)}}
  .status{position:fixed;bottom:74px;right:18px;font-size:11px;color:var(--muted);
       background:var(--panel);border:1px solid var(--line);border-radius:20px;
       padding:5px 12px;opacity:.9;z-index:15}
  .status b{color:var(--accent)}

  /* ---- Bottom chat pill box ---- */
  .composer{position:fixed;left:50%;transform:translateX(-50%);bottom:16px;width:min(740px,94vw);
       z-index:18;background:var(--panel);border:1px solid var(--line);border-radius:28px;
       padding:8px 10px;display:flex;align-items:center;gap:8px;
       box-shadow:0 8px 30px rgba(0,0,0,.45)}
  .composer input{flex:1;background:transparent;border:none;color:var(--text);font-size:14px;
       padding:8px 12px;outline:none}
  .composer input::placeholder{color:var(--muted)}
  .send-btn{width:44px;height:44px;flex-shrink:0;border-radius:50%;border:none;cursor:pointer;
       display:flex;align-items:center;justify-content:center;background:var(--accent);color:#0d1117}
  .send-btn svg{width:18px;height:18px;fill:currentColor}
  .send-btn:hover{opacity:.9}
  .send-btn:disabled{opacity:.4;cursor:default}

  .recall-tip{font-size:11px;color:var(--muted);line-height:1.5;margin:6px 2px 0}

  @media(max-width:640px){.wrap{padding:14px 10px 130px}.head .subj-txt{max-width:40%}}
  @media (prefers-reduced-motion: reduce){
    *{transition:none!important;animation:none!important}
    /* Typing/thinking indicator is a functional engagement signal — keep it
       animating even under reduced motion so users can always tell an agent
       is actively responding. */
    .presence-item .p-dots span{animation:blink 1.1s infinite both!important}
  }
</style>
</head>
<body>
<header>
  <button class="burger" id="burgerBtn" title="Session history &amp; search" aria-label="Open menu">
    <svg viewBox="0 0 24 24"><line x1="3" y1="7" x2="21" y2="7"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="17" x2="15" y2="17"/></svg>
  </button>
  <div class="brand" aria-label="AppVantage AI">
    <svg class="brand-mark" viewBox="0 0 48 48" aria-hidden="true">
      <defs>
        <linearGradient id="brandGrad" x1="0" y1="1" x2="1" y2="0">
          <stop offset="0" stop-color="#7ee787"/>
          <stop offset=".5" stop-color="#58a6ff"/>
          <stop offset="1" stop-color="#e2558a"/>
        </linearGradient>
      </defs>
      <path class="brand-core" d="M24 6 40 40h-7.6L24 20.5 15.6 40H8Z" fill="url(#brandGrad)"/>
      <path class="brand-slice" d="M19.4 30.5h9.2l-3.1-6.6-3.1 6.6Z" fill="#0e1116" opacity=".85"/>
      <circle class="brand-orbit" cx="40" cy="9" r="2.4" fill="#7ee787"/>
    </svg>
    <span class="brand-name">AppVantage<em> AI</em></span>
  </div>
  <span class="sub" id="liveSub">Shared conversation &amp; work board</span>
</header>

<!-- Drawer -->
<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer" aria-label="Session history and search">
  <div class="drawer-head">
    <h2>History &amp; Search</h2>
    <button class="close" id="drawerClose" aria-label="Close menu">&times;</button>
  </div>
  <div class="draw-search">
    <div class="s-row">
      <input id="drawSearch" type="text" placeholder="Search chat history…"
             onkeydown="if(event.key==='Enter')doSearch()">
    </div>
    <div class="chip-row" id="sendChips">
      <button class="chip active" data-s="all">All</button>
      <button class="chip" data-s="corey">You</button>
      <button class="chip" data-s="rose">Rose</button>
      <button class="chip" data-s="alex">Alex</button>
    </div>
    <div class="s-tag" id="searchStatus"></div>
  </div>
  <div class="drawer-scroll" id="drawBody"></div>
</aside>

<main class="wrap">
  <section id="pane-live">
    <p class="pane-title">Messages are collapsed by default &mdash; tap any message to expand it,
       scroll-free. Type <code>/recall &lt;word&gt;</code> below to search chat history in place.</p>
    <div class="presence-strip" id="presenceBar">
      <div class="presence-item rose" id="pitem-rose">
        <img class="avatar" src="/assets/avatars/rose.jpg" alt="Rose">
        <span class="p-name">Rose</span>
        <span class="p-state"><span class="p-dots"><span></span><span></span><span></span></span><em class="p-label">thinking…</em></span>
      </div>
      <div class="presence-item alex" id="pitem-alex">
        <img class="avatar" src="/assets/avatars/alex.jpg" alt="Alex">
        <span class="p-name">Alex</span>
        <span class="p-state"><span class="p-dots"><span></span><span></span><span></span></span><em class="p-label">thinking…</em></span>
      </div>
    </div>
    <div id="thread"><!--THREAD--></div>
  </section>
</main>

<div class="status" id="status">connected &middot; <span id="count"></span></div>

<!-- Bottom chat pill box -->
<div class="composer">
  <input id="chatInput" type="text" placeholder="@rose / @alex to target one · Message both otherwise · /recall &lt;word&gt; to search"
         autocomplete="off" onkeydown="if(event.key==='Enter')composerSend()">
  <button class="send-btn" id="sendBtn" aria-label="Send" onclick="composerSend()">
    <svg viewBox="0 0 24 24"><path d="M3 20.5 22 12 3 3.5 3 10l13 2-13 2z"/></svg>
  </button>
</div>

<script>
const esc = s => String(s||'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const AVATAR = {rose:'rose.jpg',alex:'alex.jpg'};

/* ---- Message bubble: collapsed by default, tap head to expand ---- */
const mkMsg = m => {
  const cls = m.sender==='rose'?'rose':(m.sender==='alex'?'alex':(m.sender==='corey'?'corey':'other'));
  const name = m.sender==='rose'?'Rose':(m.sender==='alex'?'Alex':(m.sender==='corey'?'You':(m.from||'?')));
  const src = AVATAR[cls];
  const avatar = src
    ? `<img class="avatar" src="/assets/avatars/${src}" alt="${esc(name)}" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'avatar av-initial av-${cls==='corey'?'corey':'other'}',textContent:'?'}))">`
    : `<div class="avatar av-initial av-${cls==='corey'?'corey':'other'}">${cls==='corey'?'C':'?'}</div>`;
  const body = (m.body_html || esc(m.body_md||'').replace(/\\n/g,'<br>'));
  const plainPreview = (m.body_md||'').replace(/\\s+/g,' ').slice(0,160);
  const subjTxt = m.subject ? `<span class="subj-txt">${esc(m.subject)}</span>` : '';
  return `<article class="msg ${cls}" data-sender="${cls}" data-file="${esc(m.file||'')}">
    ${avatar}
    <div class="inner">
      <div class="head" onclick="this.parentNode.parentNode.classList.toggle('open')" role="button" tabindex="0"
           onkeydown="if(event.key==='Enter'||event.key===' '){this.parentNode.parentNode.classList.toggle('open');event.preventDefault()}">
        <span class="name-dot"></span><span class="name">${esc(name)}</span>
        ${subjTxt}
        <span class="when">${esc(m.date||'')}</span>
        <span class="exp-chev">&#9656;</span>
      </div>
      <div class="preview">${esc(plainPreview)}${plainPreview.length>=160?'&hellip;':''}</div>
      <div class="body">${body}</div>
    </div>
  </article>`;
};

async function refresh(){
  try{
    const r = await fetch('/api/threads',{cache:'no-store'});
    const d = await r.json();
    const el = document.getElementById('thread');
    // Preserve which messages are currently expanded so the 8s auto-refresh
    // doesn't collapse a conversation the user has open.
    const openFiles = new Set(
      Array.from(el.querySelectorAll('.msg.open')).map(n=>n.dataset.file).filter(Boolean)
    );
    (d.messages||[]).sort((a,b)=>(a.mtime||0)-(b.mtime||0));
    el.innerHTML = (d.messages||[]).map(mkMsg).join('')
                   || `<div class="msg empty">No messages yet.</div>`;
    if(openFiles.size){
      el.querySelectorAll('.msg[data-file]').forEach(n=>{
        if(openFiles.has(n.dataset.file)) n.classList.add('open');
      });
    }
    document.getElementById('count').textContent = (d.messages||[]).length + ' message(s)';
    document.getElementById('status').style.color='';
  }catch(e){
    document.getElementById('status').innerHTML='<b>offline</b> &middot; retrying&hellip;';
  }
}
refresh();
setInterval(refresh, 8000);

/* ---- Presence / typing indicator ---- */
async function pollPresence(){
  try{
    const r = await fetch('/api/presence',{cache:'no-store'});
    const d = await r.json();
    const agents = d.agents||{};
    for(const sel of ['rose','alex']){
      const item = document.getElementById('pitem-'+sel);
      if(!item) continue;
      const hb = agents[sel];
      const live = hb && hb.live && (hb.state==='thinking'||hb.state==='typing');
      item.classList.toggle('busy', !!live);
      // Honest, always-visible label when engaged.
      const st = item.querySelector('.p-state');
      const lbl = item.querySelector('.p-label');
      if(lbl) lbl.textContent = live ? (hb.state==='typing'?'typing…':'thinking…') : 'thinking…';
    }
  }catch(e){ /* silent */ }
}
setInterval(pollPresence, 3000);

/* ---- Drawer: open/close ---- */
const drawer = document.getElementById('drawer');
const scrim  = document.getElementById('scrim');
function openDrawer(){ drawer.classList.add('open'); scrim.classList.add('open'); loadSessions(); }
function closeDrawer(){ drawer.classList.remove('open'); scrim.classList.remove('open'); }
document.getElementById('burgerBtn').onclick = openDrawer;
document.getElementById('drawerClose').onclick = closeDrawer;
scrim.onclick = closeDrawer;
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeDrawer(); });

/* ---- Sender filter chips ---- */
let sendFilter = 'all';
document.getElementById('sendChips').addEventListener('click',e=>{
  const b = e.target.closest('.chip'); if(!b) return;
  document.querySelectorAll('#sendChips .chip').forEach(c=>c.classList.remove('active'));
  b.classList.add('active'); sendFilter = b.dataset.s;
  const q = document.getElementById('drawSearch').value.trim();
  if(q) doSearch(); else loadSessions();
});
const searchStatus = t => document.getElementById('searchStatus').textContent=t;

/* ---- Session history (archive) list in drawer ---- */
async function loadSessions(){
  searchStatus('');
  const el = document.getElementById('drawBody');
  el.innerHTML = '<div class="draw-note">Loading history&hellip;</div>';
  try{
    const r = await fetch('/api/archive',{cache:'no-store'});
    const d = await r.json();
    const list = d.archives||[];
    if(!list.length){ el.innerHTML='<div class="draw-note">No archived sessions yet. Use the Compress action to archive the live thread.</div>'; return; }
    const fld = a=>a.folder.split('/archive/').pop();
    el.innerHTML = '<h3>Previous sessions</h3><div class="sess">' + list.map(a=>{
      const parts=(a.participants||[]).map(p=>p==='corey'?'You':(p.charAt(0).toUpperCase()+p.slice(1))).join(', ');
      return `<div class="sess-card" onclick="toggleSession(this,'${esc(fld(a))}')">
        <div class="sess-title">${esc(a.heading)}</div>
        <div class="sess-meta">${esc(a.date)} &middot; ${a.message_count} msg &middot; ${esc(parts)}</div>
        <div class="sess-expand" id="sess-${esc(a.id)}"></div>
      </div>`;
    }).join('') + '</div>';
  }catch(e){ el.innerHTML='<div class="draw-note">Error loading history.</div>'; }
}

async function toggleSession(card, folder){
  const body = card.querySelector('.sess-expand');
  if(card.classList.contains('open')){ card.classList.remove('open'); body.innerHTML=''; return; }
  card.classList.add('open');
  if(!body.dataset.loaded){
    body.dataset.loaded='1'; body.innerHTML='<div class="draw-note">Loading&hellip;</div>';
    try{
      const r=await fetch('/api/archive/thread?folder='+encodeURIComponent(folder),{cache:'no-store'});
      const d=await r.json();
      const msgs=d.messages||[];
      body.innerHTML = msgs.length ? msgs.map(mkMsg).join('')
        : '<div class="draw-note">Structured thread unavailable for this session.</div>';
    }catch(e){ body.innerHTML='<div class="draw-note">Failed to load.</div>'; }
  }
}

/* ---- Search in drawer ---- */
async function doSearch(){
  const q = document.getElementById('drawSearch').value.trim();
  const el = document.getElementById('drawBody');
  if(!q){ loadSessions(); return; }
  searchStatus('Searching&hellip;');
  try{
    const r=await fetch('/api/search?q='+encodeURIComponent(q)+'&sender='+encodeURIComponent(sendFilter),{cache:'no-store'});
    const d=await r.json();
    const lv=d.results.live||[], ar=d.results.archive||[];
    if(!lv.length && !ar.length){ searchStatus(''); el.innerHTML=`<div class="draw-note">No matches for &ldquo;${esc(q)}&rdquo;.</div>`; return; }
    let h='<h3>Results for &ldquo;'+esc(q)+'&rdquo;'+(sendFilter!=='all'?(' ('+esc(sendFilter)+')'):'')+'</h3>';
    h += lv.map(x=>`<div class="srch-result"><div>${esc(x.subject||x.file)}</div>
        <div class="r-meta">Live &middot; ${esc(x.sender)} &middot; ${esc(x.date)}</div>
        <div>${esc(x.snippet)}</div></div>`).join('');
    h += ar.map(x=>`<div class="srch-result"><div>${esc(x.heading||'Archived session')}</div>
        <div class="r-meta">Archived &middot; ${esc(x.date)}</div>
        <div>${esc(x.snippet)}</div></div>`).join('');
    h += `<div class="recall-tip">Tip: type <code>/recall ${esc(q)}</code> in the message box to search on the fly.</div>`;
    searchStatus(''); el.innerHTML=h;
  }catch(e){ searchStatus(''); el.innerHTML='<div class="draw-note">Search failed.</div>'; }
}

/* ---- Bottom composer: post message OR /recall ---- */
async function composerSend(){
  const input = document.getElementById('chatInput');
  const btn = document.getElementById('sendBtn');
  const text = input.value.trim();
  if(!text) return;
  btn.disabled = true;
  if(text.startsWith('/recall')){
    const q = text.replace(/^\/recall\s*/,'').trim() || '*';
    await doInlineRecall(q);
    input.value=''; btn.disabled=false; return;
  }
  try{
    const r = await fetch('/api/message',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:text}),cache:'no-store'});
    const d = await r.json();
    if(d.ok){ input.value=''; refresh(); }
    else{ alert('Send failed: '+(d.error||'unknown')); }
  }catch(e){ alert('Send failed'); }
  btn.disabled=false;
}

async function doInlineRecall(q){
  const term = (q==='*')?'':q;
  const st=document.getElementById('status');
  st.innerHTML='Searching history for &ldquo;'+esc(term)+'&rdquo;&hellip;';
  try{
    const url='/api/search?q='+encodeURIComponent(term||' ')+'&sender=all';
    const r=await fetch(url,{cache:'no-store'});
    const d=await r.json();
    const lv=d.results.live||[], ar=d.results.archive||[];
    if(!lv.length && !ar.length){ st.innerHTML='No matches for &ldquo;'+esc(term)+'&rdquo;.'; }
    else {
      let h='<b>History:</b>';
      if(lv.length) h+=` ${lv.length} live match(es)`;
      if(ar.length) h+=` ${ar.length} archived`;
      st.innerHTML=h;
    }
    setTimeout(()=>{st.innerHTML='connected &middot; <span id="count"></span>'; refresh();},6000);
  }catch(e){ st.innerHTML='Search failed'; setTimeout(()=>{st.innerHTML='connected &middot; <span id="count"></span>';},4000); }
}
</script>
</body>
</html>"""


def run():
    os.makedirs(INBOX_DIR, exist_ok=True)
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Shared conversation viewer: http://0.0.0.0:{PORT}  (inbox: {INBOX_DIR})",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    run()

