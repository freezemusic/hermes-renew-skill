---
name: renew
description: Retire this session into a fresh one with a handover.
version: 1.0.0
author: freezemusic
license: MIT
metadata:
  hermes:
    tags: [Productivity, Sessions, Handover, Context]
    related_skills: []
    requires_toolsets: [terminal]
---

# Renew Skill

Ends the current session cleanly and continues in a new one: you (the agent in the old session) write a short handover note, then `scripts/rollover.py` renames the old session to `<title>_YYYYMMDDHHMM` (the rollover time on this computer's clock), creates a new session with the same title, posts the handover as its first message (which starts a run there), and archives the old session after the current turn ends. It does not summarise with an extra LLM call, does not delete anything, and does not copy the old transcript.

## When to Use

- The user types `/renew`, or says "renew this session", "start a fresh session and carry on", "this session is too long / stale".
- zh-TW triggers: 「換新 session」「了結這個 session」「開新 session 接著做」.
- A fresh session is needed to pick up newly installed skills, tools, credentials or a changed system prompt (the toolset and skills index are frozen per session).
- In API-server clients (e.g. Overlay-style frontends using `/api/sessions/{id}/chat` or `/v1/runs`) slash commands are not expanded; treat a literal `/renew` message as this trigger.

## Prerequisites

- Hermes gateway running with the API server enabled (`API_SERVER_ENABLED=true`, `API_SERVER_KEY` set). For a named profile under `multiplex_profiles`, that profile's own `.env` must hold its `API_SERVER_KEY` (named profiles never inherit the owner key).
- Python 3.9+ (stdlib only). The script reads `state.db` read-only and writes only through the API.

## How to Run

Invoke through the `terminal` tool from this skill's directory (shown by `skill_view`):

```
python scripts/rollover.py rollover --self --handover <handover.md> --dry-run
python scripts/rollover.py rollover --self --handover <handover.md>
```

`--self` takes the session id and profile from `HERMES_SESSION_ID` / `HERMES_SESSION_PROFILE`, which Hermes injects into every terminal command.

## Quick Reference

| Command | Effect |
|---|---|
| `list [--all]` | Sessions of the profile: id, title, source, updated, archived, message count, active run |
| `dump --self [--max-chars N]` | Compact transcript to `<hermes root>/backups/session-rollover/<date>/<id>.md` |
| `rollover --self --handover F [--title T] [--lang en\|zh-TW] [--dry-run]` | Rename, create, post handover, archive (after this turn) |
| `--old-suffix-format FMT` | strftime suffix for the old title (default `_%Y%m%d%H%M`, e.g. `_202610261817`) |
| `--tz local\|UTC\|<IANA>` | Clock for the suffix (default `local` = this computer; IANA names need tzdata, else local) |
| `--profile P --session ID` | Target another session explicitly (refused while it has an active run) |
| `--hermes-root DIR`, `--base-url URL` | Override auto-detected Hermes root / gateway URL |

## Procedure

1. Unless the user already said to go ahead, confirm in one sentence: the old session will be renamed `<title>_YYYYMMDDHHMM` and archived, and a handover goes to a new session with the same title.
2. Write the handover with `write_file` to `<hermes root>/backups/session-rollover/handover-<session id>.md`, 1,500-3,000 characters (hard cap 12,000), in the user's language, with these sections: Goal; Decisions (with reasons); Current state (what is done and verified); Open items (ticket/card ids, pending approvals); Files and paths (plans, branches, commits, logs); Next step; Rules and constraints the user set. Facts and decisions only, no pasted transcripts. Never include passwords, tokens, API keys or vault contents; write "stored in the vault/.env" instead. Run `dump --self` first if you need to refresh details.
3. Dry run: `python scripts/rollover.py rollover --self --handover <file> --dry-run`. Check `self=True`, `api_key_loaded=yes` and the titles.
4. Run the same command without `--dry-run`. Add `--lang zh-TW` for a Traditional Chinese preamble, `--title "<new>"` to retitle.
5. Tell the user briefly: the new session's title, that it received the handover and is confirming the next step, that the old one is renamed `<title>_YYYYMMDDHHMM` and archived after this turn (still under Archived), and to switch to the new session.

## Pitfalls

- `REFUSED ... active run`: the target has a queued/started admission. Only `--self` (run from inside that session) bypasses it; with `--self` archiving is deferred by a detached `archive-when-idle` helper until the session has no active admission and its message count has been stable for 45 s (max `--wait`, default 900 s).
- `REFUSED ... active in the last 2 minutes`: a non-self target was just used; wait or pass `--force`.
- Title conflicts: if `<title>_YYYYMMDDHHMM` already exists, seconds are appended.
- `create failed` restores the old title. `handover post failed` leaves an empty new session and does not archive the old one; report the new id.
- Handover text that matches a secret pattern is refused. Remove the secret, do not bypass.
- The archive is a deliberate archive: Hermes will not auto-unarchive it on later activity. Unarchive from the Desktop "Archived" list or via `PATCH /api/sessions/{id} {"archived": false}`.
- Do not retry the same failing command more than once; report the error.

## Verification

`python scripts/rollover.py list --all` shows the old session archived as `<title>_YYYYMMDDHHMM` and the new session with the original title and at least 2 messages. Each run writes `rollover-<id>-<time>.json` (before/after metadata) to `<hermes root>/backups/session-rollover/<date>/`.
