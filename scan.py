#!/usr/bin/env python3
"""tokendust scan - count this machine's AI token usage, write one file.

Reads the local logs of the agents installed here (Claude Code, Codex, OpenCode,
Gemini CLI, and optionally Cursor's dashboard) and writes
``tokendust-<hostname>.json`` next to you. Drop that file on index.html and the
board sums every machine you give it.

No network calls, no config, no install: Python 3.8+ standard library only.

    python3 scan.py                 # every agent found, last 400 days
    python3 scan.py --days 90
    python3 scan.py --out ~/Desktop # where to write the file

Nothing but daily totals per tool and model leaves the machine: no prompts, no
paths, no project names, no session ids. ``--cursor`` reads Cursor's dashboard
with the token the Cursor app already stored, which is a network call; it is off
unless you ask for it.

Counting rules match ccusage: one request counted once, cache read/write kept
apart from fresh input, sub-agent replays not counted twice.
"""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import glob
import json
import os
import platform
import re
import sqlite3
import sys
import urllib.request

VERSION = "0.1.0"

# Everything the share card says about where it came from. Editing these five
# lines rebrands the scanner and the board together.
BRAND = {
    "name": "tokendust",
    "tagline": "what your AI actually costs",
    "url": "zedongpeng.com/tokendust",
    "handle": "@tokendust",
}

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai, no DST
HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".local", "state", "tokendust")
CACHE = os.path.join(HOME, ".cache", "tokendust")

TOOLS = ("claude-code", "codex", "opencode", "gemini", "cursor")


# ------------------------------------------------------------------ helpers

def day_of(value):
    """ISO string or epoch milliseconds -> YYYY-MM-DD in Asia/Shanghai."""
    if isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(value / 1000, TZ)
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TZ)
    return moment.strftime("%Y-%m-%d")


def count(value):
    return value if isinstance(value, int) and value > 0 else 0


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, data, **options):
    """Write through a temporary file so a reader never sees half a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(data, handle, **options)
    os.replace(path + ".tmp", path)


class Totals:
    def __init__(self):
        self.rows = {}

    def add(self, day, tool, model, fresh, cache_read, cache_write, output, cost=None):
        row = self.rows.setdefault((day, tool, model or "unknown"), [0, 0, 0, 0, 0, None])
        row[0] += 1
        row[1] += fresh
        row[2] += cache_read
        row[3] += cache_write
        row[4] += output
        if cost is not None:
            row[5] = (row[5] or 0) + cost

    def merge(self, rows):
        for day, tool, model, requests, fresh, read, write, output, cost in rows:
            row = self.rows.setdefault((day, tool, model), [0, 0, 0, 0, 0, None])
            for index, value in enumerate((requests, fresh, read, write, output)):
                row[index] += value
            if cost is not None:
                row[5] = (row[5] or 0) + cost

    def export(self, since):
        out = []
        for (day, tool, model), values in sorted(self.rows.items()):
            if day < since or not sum(values[1:5]):
                continue
            cost = values[5]
            out.append([day, tool, model] + values[:5] +
                       [round(cost, 6) if cost is not None else None])
        return out


# ------------------------------------------------------------- claude code

def claude_usage(usage):
    return (count(usage.get("input_tokens")), count(usage.get("cache_read_input_tokens")),
            count(usage.get("cache_creation_input_tokens")), count(usage.get("output_tokens")))


def claude_code(totals):
    """Dedupe the way ccusage does.

    Claude Code writes one line per content block and can copy a response into
    several transcripts, so message id + request id identify one request.
    Sidechain logs (subagents/) replay parent messages under a new request id;
    those copies are dropped when the parent entry is present.
    """
    records = {}
    roots = (os.path.join(HOME, ".claude", "projects"),
             os.path.join(HOME, ".config", "claude", "projects"),
             os.path.join(HOME, ".claude", "local", "projects"))
    for root in roots:
        for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            try:
                handle = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for line in handle:
                    if '"usage"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                        message = entry["message"]
                        usage = message["usage"]
                        model = message.get("model")
                        day = day_of(entry["timestamp"])
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                    if entry.get("type") != "assistant" or not model or model == "<synthetic>":
                        continue
                    key = (message.get("id"), entry.get("requestId") or entry["timestamp"])
                    values = claude_usage(usage)
                    previous = records.get(key)
                    if previous is None or values[3] > previous[2][3]:
                        records[key] = (day, model, values, entry.get("isSidechain") is True)
                    for index, step in enumerate(usage.get("iterations") or []):
                        if isinstance(step, dict) and step.get("type") == "advisor_message" and step.get("model"):
                            records[key + ("advisor", index)] = (day, step["model"], claude_usage(step), False)
    primary = {key[0] for key, record in records.items() if not record[3]}
    for key, (day, model, values, sidechain) in records.items():
        if sidechain and key[0] in primary:
            continue
        totals.add(day, "claude-code", model, *values)


# ------------------------------------------------------------------- codex

def codex_file(path):
    """Per-request usage from one rollout file.

    A sub-agent rollout first replays its parent's history; as in ccusage, its
    own usage starts at task_started or a trigger_turn inter-agent message.
    Usage before the first turn_context has no model and is skipped, as are
    repeated snapshots of the same cumulative total.
    """
    rows = Totals()
    model, last_total, replaying = None, None, False
    with open(path, encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle):
            if number == 0 and '"session_meta"' in line:
                try:
                    meta = json.loads(line)["payload"]
                    replaying = bool(meta.get("forked_from_id")) or isinstance(meta.get("source"), dict)
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
                continue
            if replaying and ('"task_started"' in line or (
                    '"inter_agent_communication' in line and '"trigger_turn":true' in line.replace(" ", ""))):
                replaying = False
                continue
            if '"turn_context"' in line:
                try:
                    model = json.loads(line)["payload"].get("model") or model or "unknown"
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
                continue
            if replaying or model is None or '"token_count"' not in line:
                continue
            try:
                entry = json.loads(line)
                info = entry["payload"]["info"]
                usage = info["last_token_usage"]
                total = json.dumps(info.get("total_token_usage"), sort_keys=True)
            except (ValueError, KeyError, TypeError):
                continue
            if total == last_total:
                continue
            last_total = total
            cached = count(usage.get("cached_input_tokens"))
            written = count(usage.get("cache_write_input_tokens"))
            fresh = max(count(usage.get("input_tokens")) - cached - written, 0)
            rows.add(day_of(entry["timestamp"]), "codex", model, fresh, cached, written,
                     count(usage.get("output_tokens")))
    return rows.export("")


def codex(totals):
    """Rollout files never change once a session ends, so cache them by size and mtime."""
    cache_path = os.path.join(CACHE, "codex-v3.json")
    cache = read_json(cache_path, {}) or {}
    fresh_cache = {}
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    paths = {}
    for folder in ("archived_sessions", "sessions"):  # the active copy wins
        base = os.path.join(codex_home, folder)
        for path in glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True):
            paths[os.path.relpath(path, base)] = path
    for path in paths.values():
        try:
            stat = os.stat(path)
        except OSError:
            continue
        stamp = [stat.st_size, int(stat.st_mtime)]
        hit = cache.get(path)
        rows = hit["rows"] if hit and hit.get("stamp") == stamp else codex_file(path)
        fresh_cache[path] = {"stamp": stamp, "rows": rows}
        totals.merge(rows)
    write_json(cache_path, fresh_cache)


# ---------------------------------------------------------------- opencode

def opencode(totals, since_ms):
    base = os.environ.get("OPENCODE_DATA_DIR") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "opencode")
    paths = (glob.glob(os.path.join(base, "opencode.db")) +
             glob.glob(os.path.join(base, "opencode-*.db")))
    for path in sorted(paths):
        opencode_db(totals, path, since_ms)


def opencode_db(totals, path, since_ms):
    """Reasoning tokens are reported apart from output and billed as output."""
    database = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    try:
        query = """SELECT time_created, json_extract(data, '$.modelID'), json_extract(data, '$.tokens.input'),
                          json_extract(data, '$.tokens.cache.read'), json_extract(data, '$.tokens.cache.write'),
                          json_extract(data, '$.tokens.output'), json_extract(data, '$.tokens.reasoning'),
                          json_extract(data, '$.cost')
                   FROM message WHERE time_created >= ? AND json_extract(data, '$.role') = 'assistant'"""
        for created, model, fresh, read, write, output, reasoning, cost in database.execute(query, (since_ms,)):
            if not any((fresh, read, write, output, reasoning)):
                continue
            totals.add(day_of(created), "opencode", model, count(fresh), count(read), count(write),
                       count(output) + count(reasoning),
                       cost if isinstance(cost, (int, float)) and cost > 0 else None)
    finally:
        database.close()


# ------------------------------------------------------------------ gemini

def gemini(totals):
    """Gemini CLI keeps one session file per chat under ~/.gemini/tmp/<project>/chats.

    Token counts live on the messages that carry them; the file's own totals are
    cumulative and would double count.
    """
    base = os.environ.get("GEMINI_CLI_HOME") or os.path.join(HOME, ".gemini")
    roots = [os.path.join(base, "tmp", "**", "chats", "*.json"),
             os.path.join(base, "history", "**", "*.json")]
    seen = set()
    for pattern in roots:
        for path in glob.glob(pattern, recursive=True):
            if path in seen:
                continue
            seen.add(path)
            session = read_json(path)
            if not isinstance(session, dict):
                continue
            model = (session.get("model") or session.get("modelName") or "gemini").strip().lower()
            for message in session.get("messages") or []:
                usage = message.get("tokens") if isinstance(message, dict) else None
                if not isinstance(usage, dict):
                    continue
                stamp = message.get("timestamp") or session.get("startTime") or session.get("lastUpdated")
                try:
                    day = day_of(stamp)
                except (ValueError, TypeError):
                    continue
                cached = count(usage.get("cached"))
                think = count(usage.get("thoughts"))
                fresh = max(count(usage.get("input") or usage.get("prompt")) - cached, 0)
                totals.add(day, "gemini", model, fresh, cached, 0,
                           count(usage.get("output") or usage.get("candidates")) + think)


# ------------------------------------------------------------------ cursor

CURSOR_EVENTS = "https://cursor.com/api/dashboard/get-filtered-usage-events"


def cursor_state():
    for path in (os.path.join(HOME, "Library", "Application Support", "Cursor", "User", "globalStorage", "state.vscdb"),
                 os.path.join(HOME, ".config", "Cursor", "User", "globalStorage", "state.vscdb")):
        if os.path.exists(path):
            return path
    return None


def cursor_session(path):
    """Dashboard cookie built from the signed-in app's access token; None when signed out."""
    database = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    try:
        found = database.execute("SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'").fetchone()
    finally:
        database.close()
    if not found or not found[0]:
        return None
    token = found[0]
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    return "WorkosCursorSessionToken=%s%%3A%%3A%s" % (claims["sub"].split("|")[-1], token)


def cursor_page(session, start_ms, end_ms, page, size):
    body = json.dumps({"startDate": str(start_ms), "endDate": str(end_ms),
                       "page": page, "pageSize": size}).encode()
    request = urllib.request.Request(CURSOR_EVENTS, data=body, headers={
        "Content-Type": "application/json", "Origin": "https://cursor.com",
        "Cookie": session, "User-Agent": "tokendust/%s" % VERSION})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response).get("usageEventsDisplay") or []


def cursor_model(name):
    """claude-opus-5-thinking-medium -> claude-opus-5; "default" is Cursor's Auto."""
    name = (name or "").lower()
    if name in ("", "default", "auto"):
        return "cursor-auto"
    name = re.sub(r"^cursor-", "", name)
    while True:
        base = re.sub(r"-(thinking|fast|low|medium|high|xhigh|max)$", "", name)
        if base == name:
            return name
        name = base


def cursor(totals, since_ms, size=500):
    """Cursor keeps usage server-side (~3.5 months), not in local logs.

    Only one machine should be signed in to the same account, or the board will
    count that account once per machine.
    """
    path = cursor_state()
    if not path:
        raise RuntimeError("Cursor is not installed here")
    session = cursor_session(path)
    if not session:
        raise RuntimeError("Cursor is not signed in")
    end_ms = int(datetime.now(TZ).timestamp() * 1000)
    page = 1
    while True:
        events = cursor_page(session, since_ms, end_ms, page, size)
        for event in events:
            usage = event.get("tokenUsage")
            if not usage:  # request-priced calls carry no token counts
                continue
            cents = usage.get("totalCents")
            totals.add(day_of(int(event["timestamp"])), "cursor",
                       cursor_model(event.get("model")),
                       count(usage.get("inputTokens")), count(usage.get("cacheReadTokens")),
                       count(usage.get("cacheWriteTokens")), count(usage.get("outputTokens")),
                       cents / 100 if isinstance(cents, (int, float)) and cents > 0 else None)
        if len(events) < size:
            return
        page += 1


# -------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(
        description="Count this machine's AI token usage into one tokendust file.")
    parser.add_argument("--host", default=platform.node() or "this-machine",
                        help="machine name shown on the board")
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--tools", default="",
                        help="comma-separated subset of %s (default: all that are found)"
                             % ",".join(TOOLS))
    parser.add_argument("--cursor", action="store_true",
                        help="also read Cursor's dashboard (network call, needs a signed-in app)")
    parser.add_argument("--out", default=".", help="directory for the tokendust-*.json file")
    args = parser.parse_args()

    now = datetime.now(TZ)
    since = (now - timedelta(days=args.days)).strftime("%Y-%m-%d")
    since_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)
    wanted = {tool.strip() for tool in args.tools.split(",") if tool.strip()}
    if not wanted:
        # Cursor is the only source that costs a network call, so it is opt-in.
        wanted = set(TOOLS) - {"cursor"}
    if args.cursor:
        wanted.add("cursor")

    totals = Totals()
    errors = []
    found = []
    for name, collect in (("claude-code", claude_code), ("codex", codex),
                          ("opencode", lambda t: opencode(t, since_ms)),
                          ("gemini", gemini),
                          ("cursor", lambda t: cursor(t, since_ms))):
        if wanted and name not in wanted:
            continue
        try:
            collect(totals)
            found.append(name)
        except Exception as error:  # one broken source must not hide the others
            errors.append("%s: %s" % (name, type(error).__name__))

    rows = totals.export(since)
    host = args.host.strip() or "this-machine"
    snapshot = {
        "schema": "tokendust/1",
        "version": VERSION,
        "brand": BRAND,
        "host": host,
        "generated_at": now.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "columns": ["date", "tool", "model", "requests", "input", "cache_read",
                    "cache_write", "output", "recorded_cost"],
        "rows": rows,
        "errors": errors,
    }

    served = [name for name in found if name_has_rows(rows, name)]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", host).strip("-") or "machine"
    path = os.path.join(os.path.expanduser(args.out), "tokendust-%s.json" % safe)
    write_json(path, snapshot, ensure_ascii=False, separators=(",", ":"))

    tokens = sum(sum(row[4:8]) for row in rows)
    print("%s: %d rows, %s tokens from %s" % (
        path, len(rows), compact(tokens), ", ".join(sorted(served)) or "nothing"))
    for error in errors:
        print("  skipped " + error, file=sys.stderr)
    print("Next: drop it on https://%s/ (nothing is uploaded)." % BRAND["url"])


def name_has_rows(rows, tool):
    return any(row[1] == tool for row in rows)


def compact(value):
    for unit, scale in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= scale:
            return "%.1f%s" % (value / scale, unit) if value < 10 * scale else "%d%s" % (value / scale, unit)
    return str(int(value))


if __name__ == "__main__":
    main()
