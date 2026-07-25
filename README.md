# Browser Tasks

Cheap, durable automation of long-running work that has to happen **in a real
browser**, with a real logged-in session.

Two ideas carry the whole project:

1. **Your local agent drives the browser. A web chat does the thinking.**
   Planning, architecture, review and research are delegated to a chat you
   already pay a flat subscription for — through its normal browser interface,
   not a metered API. The local agent spends its own budget only on short,
   deterministic work and on the browser actions themselves.
2. **A task is a place, not an attempt.** One user intent gets one folder that
   survives crashes, restarts and weeks of calendar time. Retries, reruns and
   research are hidden inside it.

## What it is good for

**Recurring authenticated checks.** Build a visa-slot tracker, a price watch or
a portal status check once, publish it as a reusable script, then rerun it for
almost nothing. The tenth run costs a browser session and a few local calls — no
re-planning, no re-reasoning.

**Multi-step flows behind a login.** Filling a long application, submitting a
form, booking a slot: flows that break plain scraping because they need a real
session, several dependent steps and a look at the result afterwards.

**Delegated code review.** Freeze a repository snapshot, get its exact digest,
approve that digest, and send it to the web chat in one shot. You get a
full-repository review for the price of a chat message instead of a large API
bill. This repository's own review history was produced that way.

**Research that spans days.** Each research pass is a hidden run inside one
workspace, so the answers accumulate in one place instead of scattering across
folders named after timestamps.

**What it is not:** not a scraper, not a hosted service, not an API client. It
never asks for an API key, and it never opens its own headless browser — it uses
the browser you are already logged into.

## Where the money goes

| Work | Who does it | What it costs |
| --- | --- | --- |
| Planning, architecture, review, research | Web chat, in your browser | Your existing subscription |
| Deciding what to click, clicking it, verifying the result | Local agent | Local agent's own budget |
| Rerunning something already built | Published script | Almost nothing |

The saving is real but specific: it works because the expensive thinking goes
through an interactive chat window on a flat plan. In exchange you accept that a
human is nearby and that the browser session is a shared resource.

## Providers

ChatGPT Web is the default and, today, the only wired delegate. The transport
pins the provider, the destination origin and the browser path, and fails closed
instead of quietly falling back to an API, a different browser, or a different
search tool — a delegation that cannot run the way it was approved does not run
at all.

Any subscription chat that lives in a browser fits the same shape (a tab, a
composer, a reasoning selector, an attachment), but adding one means changing
those pinned checks in `tools/web-chat/web-chat.zsh` and
`src/browser_tasks/policy.py`. It is not a configuration switch yet.

## Requirements

- macOS or Linux with `zsh`
- Python 3.11+, plus `git`, `tar`, `zstd`, `shasum`, `find`, `touch`, `sort`,
  `head`, `grep` and the usual POSIX utilities
- The `surf` command-line tool, connected to your own browser
- A logged-in ChatGPT Web session in that browser

```sh
python3 -m pip install -e '.[dev]'
```

## Quick start

Create a workspace only when the work is durable. A question you just want
answered is a conversation, not a task.

```sh
browser-tasks task init visa-slot-tracker \
  --title "Visa slot tracker" \
  --goal "Build and operate a reusable visa appointment slot tracker"
```

Work happens inside a run, which holds a time-limited lease so a dead process
can never leave the workspace looking busy forever:

```sh
browser-tasks task run-start visa-slot-tracker --lease-owner session-42
# ... browser work ...
browser-tasks task run-finish visa-slot-tracker <run-id> \
  --state SUCCEEDED --lease-owner session-42
```

Publish what is worth keeping, and say explicitly that you verified it:

```sh
browser-tasks task publish visa-slot-tracker ./tracker \
  --name tracker --kind browser-automation \
  --description "Reusable visa appointment slot tracker" \
  --entrypoint run.sh --reusable --verified

browser-tasks task complete visa-slot-tracker \
  --summary "The tracker is verified and ready for reuse."
```

Reuse it later without knowing any internal identifier:

```sh
browser-tasks task find "visa slot"
browser-tasks task deliverables visa-slot-tracker
browser-tasks task resume visa-slot-tracker --lease-owner session-43
```

A workspace stays readable by a human:

```text
tasks/visa-slot-tracker/
├── README.md            # purpose, status, what is ready, how to run it
├── deliverables/        # the reusable script, reports, datasets
└── .task/               # state, evidence, receipts (agent-owned)
```

## Delegating the thinking

Every delegation is two steps: freeze, then submit exactly what was approved.

```sh
# 1. Freeze the prompt and any attachment; prints a context digest
tools/web-chat/web-chat.zsh \
  --task-id visa-slot-tracker \
  --purpose research \
  --task-file /tmp/question.md \
  --prepare-only

# 2. Submit that exact digest, recording the disclosure in the workspace
tools/web-chat/web-chat.zsh \
  --task-id visa-slot-tracker \
  --purpose research \
  --task-file /tmp/question.md \
  --record-run-id <run-id> --record-lease-owner session-42 \
  --approved-context-sha <digest>
```

For a code review, `tools/web-review/web-review.zsh` packages the repository
first and then uses the same path:

```sh
tools/web-review/web-review.zsh repo \
  --task-id harness-hardening \
  --task-file /tmp/review-task.md \
  --prepare-only
```

Before anything leaves the machine it is scanned for keys, tokens, credentialed
URLs and stray binaries; `tasks/` and `archive/` are never uploaded. Packaging
is reproducible, so the digest you approve is the digest that gets sent.

## Two commands worth remembering

```sh
browser-tasks task doctor visa-slot-tracker   # is everything consistent?
browser-tasks task repair visa-slot-tracker   # fix what can be fixed automatically
```

`doctor` exits non-zero when something is wrong, and completing a task runs the
same checks — a workspace cannot be closed in a state `doctor` calls broken.
`repair` finishes or rolls back an interrupted write, clears leftovers, and says
plainly when a state needs your decision instead of guessing.

## Why the statuses exist

Each status family answers one specific way this kind of work breaks:

- **Task states** (`OPEN`, `PAUSED`, `COMPLETED`, …) describe the durable
  intent. **Run states** (`EXECUTING`, `WAITING`, `INTERRUPTED`, …) describe one
  attempt. Kept apart so today's failed check does not condemn a tracker that
  works.
- **Leases** give an active run an owner and an expiry. A worker that dies is
  detected and its run becomes `INTERRUPTED` rather than "running" forever.
- **Action states** include `OUTCOME_UNKNOWN`: we clicked, then lost contact. It
  blocks blind retries, because a second submitted form is worse than none.
- Consequential actions need an explicit authorization, at least one observable
  success condition, and evidence that is actually stored before the result can
  be called verified.

`AGENTS.md` holds the full policy; `browser-tasks task --help` lists every
subcommand; `tools/*/README.md` documents the transports.

Exit codes: `0` success, `1` unexpected I/O failure, `2` denial or validation
error, `3` conflict, `4` unknown workspace, `5` scan findings.

## Validation

```sh
PYTHONPATH=src pytest
tools/web-chat/smoke-test.zsh
tools/web-review/smoke-test.zsh
```

The suites use fake browser executables and isolated Git fixtures. They never
touch a real browser or send anything anywhere.

## License

MIT — see [LICENSE](LICENSE).
