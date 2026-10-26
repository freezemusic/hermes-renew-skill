#!/usr/bin/env python3
"""renew / session rollover helper for Hermes Agent.

Retire a Hermes session and continue in a fresh one:
  1. rename the old session to "<title>_YYYYMMDDHHMM" (rollover time; --old-suffix-format, --tz)
  2. create a new session (same title by default)
  3. post a handover note as the new session's first message (starts a run)
  4. archive the old session (deliberate archive: kept, searchable, un-archivable)

Reads go straight to the profile's state.db opened READ-ONLY; writes go through the
running gateway's API server (`/api/sessions`, `/v1/runs`), which owns the database.
The API_SERVER_KEY is loaded at runtime from the profile's .env (or the environment)
and is never printed or written anywhere. No LLM calls are made by this script.

Subcommands:
  list      [--profile P] [--all]
  dump      [--profile P] --session ID | --self  [--max-chars N]
  rollover  [--profile P] --session ID | --self  --handover FILE [--title T] [--dry-run]
  archive-when-idle  (internal; spawned by `rollover --self`)

Standard library only. Python 3.9+. Windows and POSIX.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ACTIVE_STATUSES = ("queued", "started")
# Defaults for the old-session rename: "<title>_YYYYMMDDHHMM" at rollover time.
DEFAULT_OLD_SUFFIX_FORMAT = "_%Y%m%d%H%M"
DEFAULT_TZ = "local"  # this computer's clock; --tz accepts "UTC" or an IANA name
CARD_ID_RE = re.compile(r"\bt_[0-9a-f]{8}\b")
WIN_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s\"'<>|`*?]+")
POSIX_PATH_RE = re.compile(r"(?<![\w:/.])(?:~|/)(?:[\w.@+-]+/)+[\w.@+-]+")
PLAN_RE = re.compile(r"[\w./\\-]*plans?[\\/][\w./\\-]+\.md|[\w./\\-]+\.plan\.md", re.I)
SECRET_RES = [
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b[\w-]*(api[_-]?key|secret|token|passw(or)?d)[\w-]*\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

T = {
    "en": {
        "preamble": ("[Session handover] This session continues `{old_title}` (old id {old_id}), "
                     "which was retired with /renew. The handover note below was written by the "
                     "previous session. Read it, then reply with a short confirmation of the current "
                     "state and the next step. Do not resume any action that still needs the user's "
                     "approval until the user confirms.\n\n---\n\n"),
    },
    "zh-TW": {
        "preamble": ("【Session 交接】這個 session 接續「{old_title}」（舊 id：{old_id}），舊的已經用 /renew "
                     "封存。下面是上一個 session 寫的交接摘要。請先讀完，再用幾句話確認目前狀態和下一步；"
                     "任何還需要使用者核准的動作，等使用者確認後才繼續。\n\n---\n\n"),
    },
}


# ---------------------------------------------------------------- time
def now_in_tz(name: str):
    """(aware datetime, note). Default 'local' = this computer's clock. An optional IANA name
    needs zoneinfo data (tzdata); if unavailable it falls back to the local clock and says so."""
    utc_now = _dt.datetime.now(_dt.timezone.utc)
    if not name or name.lower() == "local":
        return utc_now.astimezone(), "local"
    if name.upper() in ("UTC", "Z", "ETC/UTC"):
        return utc_now, "UTC"
    try:
        from zoneinfo import ZoneInfo
        return utc_now.astimezone(ZoneInfo(name)), name
    except Exception:
        return utc_now.astimezone(), f"local (fallback: tz '{name}' unavailable)"


# ---------------------------------------------------------------- locations
def hermes_root(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    if os.environ.get("HERMES_ROOT"):
        return Path(os.environ["HERMES_ROOT"]).expanduser()
    home = os.environ.get("HERMES_HOME")
    if home:
        p = Path(home).expanduser()
        return p.parent.parent if p.parent.name == "profiles" else p
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        p = Path(os.environ["LOCALAPPDATA"]) / "hermes"
        if p.exists():
            return p
    return Path.home() / ".hermes"


def self_profile() -> str:
    prof = os.environ.get("HERMES_SESSION_PROFILE") or os.environ.get("HERMES_PROFILE")
    if prof:
        return prof
    home = os.environ.get("HERMES_HOME")
    if home and Path(home).parent.name == "profiles":
        return Path(home).name
    return "default"


def profile_home(root: Path, profile: str) -> Path:
    return root if profile in ("", "default") else root / "profiles" / profile


def read_env_file(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            out[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def open_db(root: Path, profile: str) -> sqlite3.Connection:
    db = profile_home(root, profile) / "state.db"
    if not db.exists():
        sys.exit(f"error: no state.db for profile '{profile}' at {db}")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- read side
def get_session(conn, sid):
    r = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    return dict(r) if r else None


def lineage_ids(conn, sid) -> list:
    """sid plus its compression ancestors (oldest first)."""
    ids, cur, seen = [], sid, set()
    while cur and cur not in seen:
        seen.add(cur)
        ids.append(cur)
        row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (cur,)).fetchone()
        parent = row["parent_session_id"] if row else None
        if not parent:
            break
        prow = conn.execute("SELECT end_reason FROM sessions WHERE id = ?", (parent,)).fetchone()
        if not prow or prow["end_reason"] != "compression":
            break
        cur = parent
    return list(reversed(ids))


def active_admissions(conn, sid) -> list:
    try:
        q = ("SELECT admission_id, status, intent FROM session_admissions WHERE target_session_id = ? "
             f"AND status IN ({','.join('?' * len(ACTIVE_STATUSES))}) ORDER BY seq")
        return [dict(r) for r in conn.execute(q, (sid, *ACTIVE_STATUSES))]
    except sqlite3.OperationalError:
        return []  # older schema: no admissions table


def is_delegate_child(row) -> bool:
    mc = row.get("model_config")
    if not mc:
        return False
    try:
        return isinstance(json.loads(mc), dict) and json.loads(mc).get("_delegate_from") is not None
    except (TypeError, ValueError):
        return False


def fmt_ts(ts) -> str:
    try:
        return _dt.datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def resolve_session(args, conn) -> str:
    if args.session:
        return args.session
    if not getattr(args, "self_", False):
        sys.exit("error: give --session ID or --self")
    for key in ("HERMES_SESSION_ID", "HERMES_SESSION_CHAT_ID"):
        sid = os.environ.get(key)
        if sid and get_session(conn, sid):
            return sid
    sys.exit("error: --self but HERMES_SESSION_ID is not set or not found in this profile's state.db")


def cmd_list(args):
    conn = open_db(args.root, args.profile)
    where = "" if args.all else "WHERE COALESCE(archived,0) = 0"
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM sessions {where} ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT ?",
        (args.limit,))]
    tz = _dt.datetime.now().astimezone().strftime("UTC%z")
    print(f"profile={args.profile}  sessions={'all' if args.all else 'non-archived'}  times={tz}")
    print(f"{'id':<28} {'title':<28} {'source':<11} {'updated':<16} {'arch':<4} {'msgs':>5} run")
    for r in rows:
        if is_delegate_child(r) or (r.get("end_reason") == "compression"):
            continue
        run = "ACTIVE" if active_admissions(conn, r["id"]) else ""
        title = (r.get("title") or "")[:28]
        print(f"{r['id']:<28} {title:<28} {(r.get('source') or ''):<11} "
              f"{fmt_ts(r.get('last_activity_at') or r.get('started_at')):<16} "
              f"{'yes' if r.get('archived') else 'no':<4} {r.get('message_count') or 0:>5} {run}")


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        s = content.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parts = json.loads(s)
                return " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
            except ValueError:
                pass
        return content
    return str(content)


def _call_args_text(raw) -> str:
    """Decoded tool-call arguments (real strings, not JSON-escaped) for mention scanning."""
    out = []
    try:
        for c in json.loads(raw or "[]") or []:
            fn = (c.get("function") or {}) if isinstance(c, dict) else {}
            a = fn.get("arguments")
            if isinstance(a, str):
                try:
                    a = json.loads(a)
                except ValueError:
                    pass
            if isinstance(a, dict):
                out.extend(str(v) for v in a.values() if isinstance(v, (str, int, float)))
            elif a:
                out.append(str(a))
    except (TypeError, ValueError):
        pass
    return " ".join(out)


def redact(s: str) -> str:
    for rx in SECRET_RES:
        s = rx.sub("[REDACTED]", s)
    return s


def clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n].rstrip() + f" …[+{len(s) - n} chars]"


def build_dump(conn, sid, max_chars: int, assistant_chars: int = 700, user_chars: int = 3000) -> str:
    sess = get_session(conn, sid)
    ids = lineage_ids(conn, sid)
    q = (f"SELECT * FROM messages WHERE session_id IN ({','.join('?' * len(ids))}) "
         "AND COALESCE(active,1) = 1 ORDER BY id")
    rows = [dict(r) for r in conn.execute(q, ids)]
    lines, mentions = [], {"paths": set(), "cards": set(), "plans": set()}
    for m in rows:
        role = m.get("role")
        if role in ("session_meta", "system"):
            continue
        txt = _text(m.get("content"))
        blob = txt + " " + _call_args_text(m.get("tool_calls"))
        mentions["cards"].update(CARD_ID_RE.findall(blob))
        mentions["plans"].update(PLAN_RE.findall(blob))
        for rx in (WIN_PATH_RE, POSIX_PATH_RE):
            for p in rx.findall(blob):
                p = p.rstrip(".,;:)\\/")
                if len(p) > 4 and "\\\\" not in p:
                    mentions["paths"].add(p)
        ts = fmt_ts(m.get("timestamp"))
        if role == "user":
            lines.append(f"\n### USER ({ts})\n{clip(txt, user_chars)}")
        elif role == "assistant":
            calls = []
            try:
                for c in json.loads(m.get("tool_calls") or "[]") or []:
                    fn = (c.get("function") or {}) if isinstance(c, dict) else {}
                    name = fn.get("name") or c.get("name") or "tool"
                    argstr = re.sub(r"\s+", " ", str(fn.get("arguments") or ""))
                    calls.append(f"- tool: {name}({clip(argstr, 140)})")
            except (TypeError, ValueError):
                pass
            if txt.strip():
                lines.append(f"\n### ASSISTANT ({ts})\n{clip(txt, assistant_chars)}")
            lines.extend(calls)
        elif role == "tool":
            continue  # tool results omitted; the call line above is enough
    head = [f"# Session dump: {sess.get('title') or sid}",
            f"- id: {sid}", f"- source: {sess.get('source')}",
            f"- started: {fmt_ts(sess.get('started_at'))}  last activity: {fmt_ts(sess.get('last_activity_at'))}",
            f"- messages: {sess.get('message_count')}  lineage: {' -> '.join(ids)}"]
    tail = ["\n## Mentioned"]
    for k in ("cards", "plans", "paths"):
        vals = sorted(mentions[k])[:60]
        tail.append(f"- {k}: " + (", ".join(vals) if vals else "(none)"))
    body = redact("\n".join(lines))
    tail_s = redact("\n".join(tail))
    budget = max_chars - len("\n".join(head)) - len(tail_s) - 200
    if len(body) > budget > 0:
        body = "…[earlier transcript trimmed]…\n" + body[-budget:]
    return "\n".join(head) + "\n\n## Transcript" + body + "\n" + tail_s + "\n"


def out_dir(root: Path) -> Path:
    d = root / "backups" / "session-rollover" / _dt.date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_dump(args):
    conn = open_db(args.root, args.profile)
    sid = resolve_session(args, conn)
    if not get_session(conn, sid):
        sys.exit(f"error: session {sid} not found in profile {args.profile}")
    text = build_dump(conn, sid, args.max_chars)
    path = out_dir(args.root) / f"{sid}.md"
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}  ({len(text)} chars)")


# ---------------------------------------------------------------- write side
class Api:
    def __init__(self, root: Path, profile: str, base_url: str | None):
        env_root = read_env_file(root / ".env")
        port = env_root.get("API_SERVER_PORT") or "8642"
        self.base = (base_url or f"http://127.0.0.1:{port}").rstrip("/")
        self.prefix = "" if profile in ("", "default") else f"/p/{profile}"
        env_prof = read_env_file(profile_home(root, profile) / ".env")
        self._key = (env_prof.get("API_SERVER_KEY") or
                     (env_root.get("API_SERVER_KEY") if profile in ("", "default") else None) or
                     os.environ.get("API_SERVER_KEY") or "")
        self.has_key = bool(self._key)

    def url(self, path):
        return f"{self.base}{self.prefix}{path}"

    def call(self, method, path, body=None, timeout=60):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self._key:
            req.add_header("Authorization", "Bearer " + self._key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            return e.code, payload
        except urllib.error.URLError as e:
            return 0, {"error": {"message": f"gateway unreachable: {e.reason}"}}


def _err(payload) -> str:
    e = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(e, dict):
        return e.get("message") or e.get("code") or json.dumps(e)[:200]
    return str(e or payload)[:200]


def snapshot(row) -> dict:
    keys = ("id", "title", "source", "archived", "pinned", "hidden", "message_count",
            "started_at", "last_activity_at", "ended_at", "end_reason", "parent_session_id")
    return {k: row.get(k) for k in keys} if row else None


def cmd_rollover(args):
    conn = open_db(args.root, args.profile)
    sid = resolve_session(args, conn)
    old = get_session(conn, sid)
    if not old:
        sys.exit(f"error: session {sid} not found in profile {args.profile}")
    if old.get("archived"):
        sys.exit(f"error: session {sid} is already archived")
    own_id = os.environ.get("HERMES_SESSION_ID") or ""
    is_self = bool(args.self_) or (sid == own_id and bool(own_id))
    active = active_admissions(conn, sid)
    recent = time.time() - float(old.get("last_activity_at") or 0) < 120
    if recent and not active and not is_self and not args.force:
        print(f"REFUSED: session {sid} was active in the last 2 minutes (a run may be in progress "
              "outside the admission queue). Wait, or pass --force.")
        sys.exit(3)
    if active and not is_self:
        print(f"REFUSED: session {sid} has an active run/admission:")
        for a in active:
            print(f"  - {a['admission_id']} status={a['status']} intent={a['intent']}")
        print("Stop or wait for the run, or run this from inside that session with --self.")
        sys.exit(3)
    hp = Path(args.handover)
    if not hp.is_file():
        sys.exit(f"error: handover file not found: {hp}")
    handover = hp.read_text(encoding="utf-8-sig").strip()
    if not handover:
        sys.exit("error: handover file is empty")
    if len(handover) > args.max_handover:
        sys.exit(f"error: handover is {len(handover)} chars (> {args.max_handover}); shorten it")
    hits = [rx.pattern[:30] for rx in SECRET_RES if rx.search(handover)]
    if hits:
        sys.exit("error: handover looks like it contains a secret (pattern match); remove it and retry")
    lang = T.get(args.lang, T["en"])
    old_title = old.get("title") or sid
    new_title = args.title or old_title
    stamp_dt, tz_note = now_in_tz(args.tz)
    renamed = old_title + stamp_dt.strftime(args.old_suffix_format)
    message = lang["preamble"].format(old_title=old_title, old_id=sid) + handover
    api = Api(args.root, args.profile, args.base_url)
    plan = [
        f"1. PATCH {api.url('/api/sessions/' + sid)}  title: {old_title!r} -> {renamed!r}",
        f"2. POST  {api.url('/api/sessions')}  {{title: {new_title!r}, source: {old.get('source') or 'api_server'!r}}}",
        f"3. POST  {api.url('/v1/runs')}  {{session_id: <new id>, input: <preamble + handover, {len(message)} chars>}}",
        (f"4. spawn archive-when-idle: wait until {sid} has no queued/started admission, then "
         f"PATCH archived=true (timeout {args.wait}s)") if is_self else
        f"4. PATCH {api.url('/api/sessions/' + sid)}  archived: true",
        f"log: {out_dir(args.root) if not args.dry_run else (args.root / 'backups' / 'session-rollover' / _dt.date.today().isoformat())}"
        f"{os.sep}rollover-{sid}-<time>.json",
    ]
    print(f"profile={args.profile}  old={sid}  title={old_title!r}  msgs={old.get('message_count')}  "
          f"self={is_self}  active_admissions={len(active)}  api_key_loaded={'yes' if api.has_key else 'NO'}  "
          f"rename_time={stamp_dt:%Y-%m-%d %H:%M} [{tz_note}]")
    if args.dry_run:
        print("DRY RUN - no changes made. Would do:")
        print("\n".join("  " + p for p in plan))
        print("--- first message preview (first 400 chars) ---")
        print(message[:400])
        return
    if not api.has_key:
        sys.exit("error: no API_SERVER_KEY found for this profile (.env); cannot call the gateway")
    log = {"profile": args.profile, "started": _dt.datetime.now().isoformat(timespec="seconds"),
           "old_before": snapshot(old), "steps": []}
    log_path = out_dir(args.root) / f"rollover-{sid}-{_dt.datetime.now():%H%M%S}.json"

    def save():
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    st, p = api.call("PATCH", f"/api/sessions/{sid}", {"title": renamed})
    if st == 400 and "title" in _err(p).lower():
        renamed = f"{renamed}{stamp_dt:%S}"  # title taken: add seconds
        st, p = api.call("PATCH", f"/api/sessions/{sid}", {"title": renamed})
    log["steps"].append({"rename": st, "title": renamed}); save()
    if st != 200:
        sys.exit(f"error: rename failed ({st}): {_err(p)}. Nothing else changed. Log: {log_path}")
    st, p = api.call("POST", "/api/sessions", {"title": new_title, "source": old.get("source") or "api_server"})
    new = (p or {}).get("session") or {}
    log["steps"].append({"create": st, "new": new.get("id")}); save()
    if st not in (200, 201) or not new.get("id"):
        api.call("PATCH", f"/api/sessions/{sid}", {"title": old_title})
        sys.exit(f"error: create failed ({st}): {_err(p)}. Old title restored. Log: {log_path}")
    nid = new["id"]
    st, p = api.call("POST", "/v1/runs", {"session_id": nid, "input": message})
    run_id = (p or {}).get("run_id") or (p or {}).get("id")
    log["steps"].append({"post_handover": st, "run_id": run_id}); save()
    if st not in (200, 201, 202):
        print(f"WARNING: handover post failed ({st}): {_err(p)}. New session {nid} exists but is empty; "
              f"old session NOT archived.")
        sys.exit(4)
    if is_self:
        cmd = [sys.executable, os.path.abspath(__file__), "--hermes-root", str(args.root),
               "archive-when-idle", "--profile", args.profile, "--session", sid,
               "--log", str(log_path), "--wait", str(args.wait)]
        if args.base_url:
            cmd[4:4] = ["--base-url", args.base_url]
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen(cmd, **kw)
        log["steps"].append({"archive": "scheduled-after-current-run"}); save()
        archive_note = "scheduled (after this run ends)"
    else:
        st, p = api.call("PATCH", f"/api/sessions/{sid}", {"archived": True})
        log["steps"].append({"archive": st}); save()
        archive_note = "done" if st == 200 else f"FAILED ({st}): {_err(p)}"
    log["new_after"] = snapshot(new)
    save()
    print(f"OK old={sid} renamed={renamed!r}  new={nid} title={new_title!r}  run_id={run_id}  "
          f"archive={archive_note}\nlog: {log_path}")


def cmd_archive_when_idle(args):
    deadline = time.time() + args.wait
    time.sleep(min(20, args.wait))
    last_count, stable_since = None, time.time()
    while time.time() < deadline:
        conn = open_db(args.root, args.profile)
        busy = bool(active_admissions(conn, args.session))
        row = get_session(conn, args.session) or {}
        conn.close()
        cnt = row.get("message_count")
        if cnt != last_count:
            last_count, stable_since = cnt, time.time()
        if not busy and time.time() - stable_since >= 45:
            break
        time.sleep(5)
    api = Api(args.root, args.profile, args.base_url)
    st, p = api.call("PATCH", f"/api/sessions/{args.session}", {"archived": True})
    if args.log:
        try:
            lp = Path(args.log)
            log = json.loads(lp.read_text(encoding="utf-8"))
            log["steps"].append({"archive": st, "at": _dt.datetime.now().isoformat(timespec="seconds"),
                                 "timed_out": time.time() >= deadline})
            lp.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Retire a Hermes session into a fresh one with a handover.")
    ap.add_argument("--hermes-root", help="Hermes root (default: HERMES_ROOT, HERMES_HOME, %%LOCALAPPDATA%%\\hermes or ~/.hermes)")
    ap.add_argument("--base-url", help="gateway API base URL (default http://127.0.0.1:<API_SERVER_PORT>)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, session=True):
        p.add_argument("--profile", help="profile name (default: the current session's profile)")
        if session:
            p.add_argument("--session", help="session id")
            p.add_argument("--self", dest="self_", action="store_true",
                           help="use the session this command runs inside (HERMES_SESSION_ID)")

    p = sub.add_parser("list"); common(p, session=False)
    p.add_argument("--all", action="store_true", help="include archived sessions")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("dump"); common(p)
    p.add_argument("--max-chars", type=int, default=30000)
    p = sub.add_parser("rollover"); common(p)
    p.add_argument("--handover", required=True, help="handover markdown file")
    p.add_argument("--title", help="new session title (default: old title)")
    p.add_argument("--lang", default="en", choices=sorted(T))
    p.add_argument("--old-suffix-format", default=DEFAULT_OLD_SUFFIX_FORMAT,
                   help="strftime suffix appended to the old title (default %(default)s)")
    p.add_argument("--tz", default=DEFAULT_TZ,
                   help="time zone for the suffix: 'local', 'UTC' or an IANA name (default %(default)s)")
    p.add_argument("--max-handover", type=int, default=12000)
    p.add_argument("--wait", type=int, default=900, help="--self: max seconds to wait before archiving")
    p.add_argument("--force", action="store_true", help="skip the 'recently active' guard (not the admission check)")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("archive-when-idle"); common(p)
    p.add_argument("--log"); p.add_argument("--wait", type=int, default=900)
    args = ap.parse_args(argv)
    args.root = hermes_root(args.hermes_root)
    if not getattr(args, "profile", None):
        args.profile = self_profile()
    if not hasattr(args, "self_"):
        args.self_ = False
    {"list": cmd_list, "dump": cmd_dump, "rollover": cmd_rollover,
     "archive-when-idle": cmd_archive_when_idle}[args.cmd](args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
