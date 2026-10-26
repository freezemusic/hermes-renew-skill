# renew: a Hermes Agent skill for "continue in a fresh session"

`/renew` retires the current Hermes session and continues in a new one with a handover note,
in one sentence from the user. The agent that already holds the context writes the handover;
a small stdlib-only helper (`skills/renew/scripts/rollover.py`) does the bookkeeping:

1. rename the old session to `<title>_YYYYMMDDHHMM` (rollover time, this computer's local clock; e.g. `Work_202610261817`)
2. create a new session with the original title
3. post the handover as the new session's first message (starts a run there, returns `run_id`)
4. archive the old session (deferred until the current turn ends when run with `--self`)

A JSON log of before/after metadata is written to `<hermes root>/backups/session-rollover/<date>/`.

## When to use it

- **Save tokens.** Every turn re-sends the whole transcript; a fresh session that starts from a short handover makes each later turn cheaper.
- **The agent is going in circles.** When it keeps repeating the same mistake or is anchored by earlier failed attempts, a fresh session keeps the decisions and drops the noise.
- **Pick up new skills/tools or a changed model/config.** The skills index, toolset and system prompt are frozen per session.
- **A phase or milestone ends.** Continue the next phase in a clean session; the old one stays findable by its timestamped title.

## Why, compared with what exists

| Option | What it does | Gap this fills |
|---|---|---|
| `/new` (`/reset`) | Empty new session | No carry-over at all |
| `/compress` | Summarises context *inside the same session* | Keeps the frozen toolset/skills index and system prompt; no clean break |
| `/branch` (`/fork`) | Copies the session | Carries the full transcript, old one stays live |
| `/handoff <platform>` | Moves a live CLI session to a messaging platform | Same session id, CLI only |
| `long-conversations` plugin | Stores a handoff brief and injects it into the next session of the same chat/user | Does not create/archive sessions or offer `/renew`; API-server sessions keyed by id are not matched by chat |
| `waypoint` plugin | Desktop sidebar statuses with a "fresh start" handoff | Desktop UI only |
| PR #47320 / #140855, issue #82991 | Portable handoff docs / automatic rollover / "Continue in new chat" | Closed, draft, or open request; none shipped in 0.22.x |

New sessions also re-load the current skills, tools and system prompt, which is often the actual reason to renew.

## Install

As a hub skill from a GitHub repo (once published):

```
hermes skills install freezemusic/hermes-renew-skill/skills/renew
# or: hermes skills tap add freezemusic/hermes-renew-skill   then   hermes skills install renew
```

Manual: copy `skills/renew/` into `<HERMES_HOME>/skills/productivity/renew/` of every profile that should have it
(each profile has its own skills dir), or add the folder's parent to `skills.external_dirs` in each profile's
`config.yaml`. Skills appear in **new** sessions (the skills index is frozen per session).

Requirements: gateway running with the API server enabled and `API_SERVER_KEY` set. Under
`multiplex_profiles`, every named profile needs its own `API_SERVER_KEY` in its `.env`
(named profiles fail closed instead of inheriting the owner key).

## How it works

- Reads: the profile's `state.db`, opened with SQLite `mode=ro` (sessions, messages, `session_admissions`).
- Writes: only through the gateway API (`/p/<profile>` prefix for named profiles):
  `PATCH /api/sessions/{id}` (title, archived), `POST /api/sessions`, `POST /v1/runs` with `session_id`.
- Auth: `Authorization: Bearer <API_SERVER_KEY>` loaded at runtime from the profile `.env`/environment;
  never printed or logged.
- Self-identification: Hermes injects `HERMES_SESSION_ID` and `HERMES_SESSION_PROFILE` into every
  `terminal` command (gateway/session_context.py → tools/environments/local.py), so `--self` needs no ids.
- Active-run guard: a target with a `queued`/`started` admission is refused, and a non-self target active in
  the last 2 minutes needs `--force`. With `--self`, a detached `archive-when-idle` helper archives the old
  session once it has no active admission and its message count is stable for 45 s (default max wait 900 s).
- Old-title suffix: `--old-suffix-format` (strftime, default `_%Y%m%d%H%M`) and `--tz` (default `local`; `UTC` or an IANA
  name such as `Asia/Taipei` if the Python has zoneinfo data/`tzdata`, otherwise it falls back to local and says so).
  If the stamped title is taken, seconds are appended.
- Archive is the deliberate kind (`auto_archived = 0`), so Hermes does not auto-unarchive it on activity.

## Windows / Linux notes

- Pure stdlib, `pathlib`; the detached helper uses `DETACHED_PROCESS` on Windows and `start_new_session` on POSIX.
- Hermes root auto-detection: `--hermes-root`, `HERMES_ROOT`, `HERMES_HOME` (a `profiles/<name>` home maps to its root),
  `%LOCALAPPDATA%\hermes` on Windows, else `~/.hermes`.
- On Windows the Hermes `terminal` tool runs Git Bash; quote paths with forward slashes.

## Limitations

- API-server clients (`/api/sessions/{id}/chat`, `/v1/runs`) do **not** expand slash commands. `/renew` arrives
  as plain text; the skill's "When to Use" tells the agent to treat it as the trigger, but loading is up to the model.
  CLI/TUI/Desktop and messaging gateways expand `/renew` natively because the skill is named `renew`.
- Runs started via `/v1/runs` may not appear in `session_admissions`; the 45 s stability window is the fallback.
- The handover quality is the agent's; the script does not summarise. `dump` produces a compact transcript
  (user messages, truncated assistant replies, one line per tool call, mentioned paths/card ids) for reference.
- Not tested against Hermes versions before 0.22.

## Tests

`pytest -q tests/skills/test_renew_skill.py` (stdlib + pytest, no network).

## License

MIT.
