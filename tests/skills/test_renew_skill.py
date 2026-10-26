"""Hermetic tests for optional-skills/productivity/renew/scripts/rollover.py (no network)."""
import importlib.util
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

SCRIPT = next(p for p in (
    Path(__file__).resolve().parents[2] / "skills" / "renew" / "scripts" / "rollover.py",
    Path(__file__).resolve().parents[2] / "optional-skills" / "productivity" / "renew" / "scripts" / "rollover.py",
) if p.exists())


def load():
    spec = importlib.util.spec_from_file_location("rollover", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def root(tmp_path, monkeypatch):
    for k in ("HERMES_HOME", "HERMES_ROOT", "HERMES_SESSION_ID", "HERMES_SESSION_PROFILE",
              "HERMES_PROFILE", "HERMES_SESSION_CHAT_ID", "API_SERVER_KEY"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "profiles" / "demo"
    home.mkdir(parents=True)
    (tmp_path / ".env").write_text("API_SERVER_PORT=18642\n")
    (home / ".env").write_text("API_SERVER_KEY=" + "x" * 32 + "\n")
    db = sqlite3.connect(home / "state.db")
    db.executescript("""
    CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model_config TEXT,
      parent_session_id TEXT, started_at REAL, ended_at REAL, end_reason TEXT, message_count INT,
      last_activity_at REAL, archived INT DEFAULT 0, pinned INT DEFAULT 0, hidden INT DEFAULT 0);
    CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
      tool_calls TEXT, timestamp REAL, active INT DEFAULT 1);
    CREATE TABLE session_admissions (seq INTEGER PRIMARY KEY, admission_id TEXT, target_session_id TEXT,
      status TEXT, intent TEXT);
    """)
    old = time.time() - 3600
    db.execute("INSERT INTO sessions VALUES ('s1','api_server','Work',NULL,NULL,?,NULL,NULL,3,?,0,0,0)", (old, old))
    db.execute("INSERT INTO sessions VALUES ('s2','api_server','Busy',NULL,NULL,?,NULL,NULL,1,?,0,0,0)", (old, old))
    db.execute("INSERT INTO messages (session_id,role,content,timestamp) VALUES ('s1','user','fix C:/repo/a.py card t_0123abcd',?)", (old,))
    db.execute("INSERT INTO messages (session_id,role,content,tool_calls,timestamp) VALUES ('s1','assistant','done',?,?)",
               (json.dumps([{"function": {"name": "terminal", "arguments": json.dumps({"command": "ls /srv/app/x.log"})}}]), old))
    db.execute("INSERT INTO session_admissions (admission_id,target_session_id,status,intent) VALUES ('a1','s2','started','queue')")
    db.commit(); db.close()
    return tmp_path


def run(mod, root, *argv):
    mod.main(["--hermes-root", str(root), *argv])


def test_list_and_dump(root, capsys):
    mod = load()
    run(mod, root, "list", "--profile", "demo")
    out = capsys.readouterr().out
    assert "s1" in out and "Work" in out and "ACTIVE" in out
    run(mod, root, "dump", "--profile", "demo", "--session", "s1")
    out = capsys.readouterr().out
    path = Path(out.split("wrote ")[1].split("  (")[0])
    text = path.read_text(encoding="utf-8")
    assert "t_0123abcd" in text and "C:/repo/a.py" in text and "tool: terminal" in text


def test_dry_run_makes_no_calls(root, tmp_path, capsys, monkeypatch):
    mod = load()
    monkeypatch.setattr(mod.Api, "call", lambda *a, **k: pytest.fail("network call in dry run"))
    hf = tmp_path / "h.md"; hf.write_text("# Handover\n## Next step\nship it\n", encoding="utf-8")
    run(mod, root, "rollover", "--profile", "demo", "--session", "s1", "--handover", str(hf), "--dry-run")
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "/p/demo/api/sessions/s1" in out
    assert re.search(r"'Work' -> 'Work_\d{12}'", out) and "[local]" in out
    assert "api_key_loaded=yes" in out and "x" * 32 not in out


def test_refuses_active_run_unless_self(root, tmp_path, capsys, monkeypatch):
    mod = load()
    hf = tmp_path / "h.md"; hf.write_text("handover", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        run(mod, root, "rollover", "--profile", "demo", "--session", "s2", "--handover", str(hf), "--dry-run")
    assert e.value.code == 3 and "REFUSED" in capsys.readouterr().out
    monkeypatch.setenv("HERMES_SESSION_ID", "s2"); monkeypatch.setenv("HERMES_SESSION_PROFILE", "demo")
    run(mod, root, "rollover", "--self", "--handover", str(hf), "--dry-run")
    out = capsys.readouterr().out
    assert "self=True" in out and "archive-when-idle" in out


def test_refuses_secret_in_handover(root, tmp_path):
    mod = load()
    hf = tmp_path / "h.md"; hf.write_text("api_key = abcdefghijklmnop", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        run(mod, root, "rollover", "--profile", "demo", "--session", "s1", "--handover", str(hf), "--dry-run")
    assert "secret" in str(e.value.code)


def test_real_rollover_sequence(root, tmp_path, capsys, monkeypatch):
    mod = load()
    calls = []

    def fake(self, method, path, body=None, timeout=60):
        calls.append((method, path, body))
        if method == "POST" and path == "/api/sessions":
            return 201, {"session": {"id": "s9", "title": body["title"]}}
        if path == "/v1/runs":
            return 202, {"run_id": "run_1"}
        return 200, {"session": {}}
    monkeypatch.setattr(mod.Api, "call", fake)
    hf = tmp_path / "h.md"; hf.write_text("# Handover\nnext", encoding="utf-8")
    run(mod, root, "rollover", "--profile", "demo", "--session", "s1", "--handover", str(hf))
    assert [c[0] for c in calls] == ["PATCH", "POST", "POST", "PATCH"]
    assert re.fullmatch(r"Work_\d{12}", calls[0][2]["title"]) and calls[3][2] == {"archived": True}
    assert calls[2][2]["session_id"] == "s9" and "next" in calls[2][2]["input"]
    assert "run_id=run_1" in capsys.readouterr().out


def test_suffix_format_and_tz(root, tmp_path, capsys):
    mod = load()
    hf = tmp_path / "h.md"; hf.write_text("handover", encoding="utf-8")
    run(mod, root, "rollover", "--profile", "demo", "--session", "s1", "--handover", str(hf),
        "--old-suffix-format=-%Y%m%d", "--tz", "UTC", "--dry-run")
    out = capsys.readouterr().out
    assert re.search(r"'Work' -> 'Work-\d{8}'", out) and "[UTC]" in out
    run(mod, root, "rollover", "--profile", "demo", "--session", "s1", "--handover", str(hf),
        "--tz", "No/Such_Zone", "--dry-run")
    assert "fallback" in capsys.readouterr().out
