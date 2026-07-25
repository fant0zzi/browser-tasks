# Browser Tasks

Browser Tasks is a human-first workspace harness for reusable browser
automation, UI testing, and web research through the user's authenticated
browser.

The filesystem represents durable user intent, not execution attempts:

```text
one user intent → one stable task workspace → many hidden runs
```

Simple questions, clarifications, status requests, and lookups stay in chat and
do not create task folders.

## Workspace model

```text
tasks/
└── visa-slot-tracker/
    ├── README.md
    ├── deliverables/                 # created on first publication
    │   ├── tracker/
    │   │   ├── README.md
    │   │   ├── run.sh
    │   │   ├── src/
    │   │   ├── tests/
    │   │   └── config.example.toml
    │   └── latest-check.md
    └── .task/
        ├── state.sqlite
        └── runs/                     # created only when artifacts exist
            └── <internal-run-id>/
                ├── evidence/
                ├── receipts/
                ├── delegations/
                └── scratch/
```

A new workspace contains exactly:

```text
README.md
.task/state.sqlite
```

There are no placeholder results or eager empty directories. Optional paths
appear only when content exists.

- `README.md` is the human landing page: purpose, current status, latest
  outcome, available deliverables, and reuse instructions.
- `deliverables/` contains only published user-relevant reports, scripts,
  configuration, datasets, or bundles.
- `.task/` contains agent-owned state, runs, authorization records, evidence,
  receipts, and scratch material.

## Stable names

Workspace names are short semantic slugs:

```text
visa-slot-tracker
microbusiness-ideas
quarterly-expense-report
```

Timestamps and opaque identifiers stay in metadata. A timestamp-shaped task
name is rejected.

Before creating a workspace, search for an existing continuation:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task find "visa slot"
```

Resume, refine, rerun, or maintain the same outcome in the same workspace. A
new top-level workspace is warranted only when the durable objective, owner,
authorization boundary, target account, jurisdiction, confidentiality policy,
or independent delivery lifecycle materially changes.

## Quick start

Create durable work only after the objective is clear:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task init \
  visa-slot-tracker \
  --title "Visa slot tracker" \
  --goal "Build and operate a reusable visa appointment slot tracker"
```

Discover it without knowing an internal ID:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task list
PYTHONPATH=src python3 -m browser_tasks.cli task find "finished visa tracker"
PYTHONPATH=src python3 -m browser_tasks.cli task show visa-slot-tracker
PYTHONPATH=src python3 -m browser_tasks.cli task audit \
  visa-slot-tracker --jsonl
```

Start and finish a hidden run:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task run-start \
  visa-slot-tracker --state EXECUTING --lease-owner agent-session-42

PYTHONPATH=src python3 -m browser_tasks.cli task run-finish \
  visa-slot-tracker <run-id> --state SUCCEEDED \
  --lease-owner agent-session-42
```

Store evidence or receipts only when they actually exist:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task artifact-store \
  visa-slot-tracker <run-id> ./before.txt \
  --category evidence --name before.txt
```

Publish a reusable deliverable:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task publish \
  visa-slot-tracker ./tracker \
  --name tracker \
  --kind browser-automation \
  --description "Reusable visa appointment slot tracker" \
  --entrypoint run.sh \
  --reusable \
  --verified
```

`--verified` is explicit: publication does not assert verification on the
operator's behalf, and completion refuses an unverified deliverable.

Finish only after all completion invariants pass:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task complete \
  visa-slot-tracker \
  --summary "The tracker is verified and ready for reuse."
```

## Runs and crash recovery

Task outcome and run activity are separate. A failed check today does not
invalidate a previously verified tracker.

Task state describes the durable workspace: `DRAFT`, `OPEN`, `COMPLETED`,
`PAUSED`, `FAILED`, `CANCELLED`, or `SUPERSEDED`. Execution details live only
in run states such as `EXECUTING`, `WAITING`, `SUCCEEDED`, and `INTERRUPTED`.

An active run owns a lease and heartbeat. The default lease is one hour, which
outlives the delegation transport's own timeout, and no lease may exceed six
hours so a dead worker cannot hold a workspace forever. A returning owner may
renew or finish its own run even if the lease already lapsed, as long as nobody
recovered it in the meantime.

If its worker disappears, the harness converts the expired run to `INTERRUPTED`,
prunes that run's scratch, and pauses the workspace:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task recover visa-slot-tracker
```

`doctor` also reports an active run whose lease has already expired, so a stale
"running" status is visible rather than silent. When a worker will never return,
take the run over explicitly:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task run-abandon \
  visa-slot-tracker <run-id> --reason "worker host was reimaged"
```

A run may also advance between active states instead of being restarted:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task run-state \
  visa-slot-tracker <run-id> --state VERIFYING --lease-owner agent-session-42
```

Resuming creates a new hidden run linked to the terminal run it continues. It
never creates another top-level folder.

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task resume \
  visa-slot-tracker --lease-owner agent-session-43
```

The harness keeps one writer per workspace. SQLite transactions protect state
changes; visible files are staged and atomically renamed. User-edited
deliverables are detected by digest and never silently overwritten.

Publication and artifact storage record their intent in a journal before they
touch a visible file, so a crash in that window is an interrupted operation
rather than damage that looks like a user edit. Resolve one with:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task repair visa-slot-tracker
```

`repair` finishes or rolls back an interrupted publish, adopts or discards an
interrupted or orphaned artifact, clears abandoned publish staging, prunes scratch
left on terminal runs, and regenerates a `README.md` that lags the recorded state.

It reports what it did (`REPAIRED`) separately from what it could not decide
(`UNRESOLVED`), and exits `2` while anything remains unresolved, so automation
cannot read a diagnostic as a repair. The one state it cannot decide alone is a
crashed publish whose visible content matches neither the staged nor the previous
digest — usually a crash plus a manual edit. Resolve it explicitly:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task repair \
  visa-slot-tracker --adopt-visible
```

`--adopt-visible` registers what is on disk; `--discard-journal` drops the record
and leaves the file to be handled as an unregistered deliverable. Both are
audited.

If a consequential browser action may have succeeded before the worker lost
contact, its status becomes `OUTCOME_UNKNOWN`. It cannot be retried or counted
as complete until an observed result is recorded. The evidence must be the
SHA-256 of an artifact that is actually stored under the task, so "observed
result" cannot be satisfied with free text:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task action-reconcile \
  visa-slot-tracker <action-id> \
  --verified --evidence-sha256 <sha256-of-a-stored-evidence-artifact>
```

## Completion invariants

A task can complete only when:

- no run remains active;
- the outcome summary is substantive;
- the workspace is not archived;
- every declared deliverable exists, is safe, and matches its digest;
- every deliverable was explicitly published as verified (`publish --verified`);
- consequential external actions are reconciled;
- no unresolved action of any class remains;
- no publish staging, journal entry, or unregistered visible file remains.

`doctor` and `complete` share one consistency checker, so a task cannot reach
`COMPLETED` in a state the health check reports as broken. Run the check at any
time; it exits `2` when anything is wrong, in both text and JSON form:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task doctor visa-slot-tracker
PYTHONPATH=src python3 -m browser_tasks.cli task doctor visa-slot-tracker --json
```

Exit codes are stable for scripting: `0` success, `1` unexpected I/O failure,
`2` policy or validation denial, `3` conflict, `4` unknown workspace, `5` scan
findings (`browser-tasks scan` only, so a caller can tell findings from a scan
that could not run).

## Browser and action policy

Surf is the only browser adapter. Browser tabs and authenticated sessions
belong to the user's browser. In-app browsers, direct browser APIs, and silent
transport fallbacks are forbidden.

Before an external browser, reasoning, or research action:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli guard \
  visa-slot-tracker --capability research --tool web-chat
```

Both transports call this guard themselves and refuse to run when it denies, so
it is an enforced precondition rather than a convention. The guard also denies an
unknown, archived, cancelled, or superseded workspace.

Consequential actions require an exact task-bound authorization. The executor
records pre-action state, executes once, observes the result, and verifies
explicit postconditions. An ambiguous external outcome blocks automatic retry.

The authorized envelope covers the postconditions, so a grant approved for a
strictly verified action cannot be spent on the same summary with the checks
removed. A consequential action must declare at least one supported
postcondition, and a verified consequential result must carry evidence.

Every step of that sequence is reachable from the CLI, fenced by the run that
owns the lease:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task bind-adapter \
  visa-slot-tracker --adapter surf:session-42 --resource tab-7

PYTHONPATH=src python3 -m browser_tasks.cli task grant-install \
  visa-slot-tracker --grant-id grant-1 \
  --action-class commit_external \
  --target https://example.test/book \
  --summary "Book the reserved slot" \
  --postcondition '{"type": "url_equals", "value": "https://example.test/done"}' \
  --expires-at 2026-07-26T00:00:00+00:00

PYTHONPATH=src python3 -m browser_tasks.cli task action-intent \
  visa-slot-tracker book-1 \
  --run-id <run-id> --lease-owner agent-session-42 \
  --grant-id grant-1 \
  --action-class commit_external \
  --target https://example.test/book \
  --summary "Book the reserved slot" \
  --postcondition '{"type": "url_equals", "value": "https://example.test/done"}'

PYTHONPATH=src python3 -m browser_tasks.cli task action-result \
  visa-slot-tracker book-1 \
  --run-id <run-id> --lease-owner agent-session-42 \
  --outcome verified --evidence-sha256 <sha256>
```

A consequential intent requires `--grant-id`: reserving execution, validating
and consuming the grant, and recording the intent happen in one transaction that
also requires the caller to hold the workspace's current, unexpired lease. There
is no path that records a consequential intent without an authorization. A worker
whose lease lapsed, or whose run was replaced, cannot act, and a semantically
identical retry is refused while the first attempt is unresolved.

Browser resource claims are released when the run finishes and when the workspace
completes, is archived or is cancelled; release them by hand with
`task release-resources` if a claim outlives its use.

Browser tabs are claimed in a registry beside the workspaces, so two tasks cannot
drive the same tab.

## ChatGPT Web delegation

`tools/web-chat/web-chat.zsh` is the canonical reasoning and research delegate.
It uses Surf UI only and freezes the exact prompt before disclosure.

Prepare without sending:

```sh
tools/web-chat/web-chat.zsh \
  --task-id microbusiness-ideas \
  --purpose research \
  --reasoning best \
  --research standard \
  --task-file /tmp/exact-research-task.md \
  --prepare-only
```

The live phase requires approval of the exact context SHA-256 printed by the
prepare phase. There is no API, alternate browser, or research-provider
fallback.

The frozen prompt ends with a request sentinel, and submission is accepted only
when the delivered message contains both the request id and that sentinel, so a
truncated paste cannot pass verification. The reasoning level is verified from the
composer pill with the selector closed, and the receipt records the research mode
that was actually observed.

Receipts, prompts, and responses live in a private directory under the system
temporary directory. Hand them to the workspace instead of leaving them there:

```sh
tools/web-chat/web-chat.zsh \
  --task-id microbusiness-ideas \
  --record-run-id <run-id> \
  --approved-context-sha <sha256> \
  ...
```

`--record-run-id` is required for a live submission: the receipt and response are
validated against the request identity, stored under `.task/runs/<run-id>/` and
appended to the audit log, after which the temporary directory is removed. Add
`--record-lease-owner` when that run is still active. `--keep` forces retention,
failures always retain the directory so the tab id and frozen prompt survive, and
directories older than seven days are pruned on start. Use `--workspace-root` when
the workspace lives outside this repository.

Preparing context is itself recorded: every prepare appends a
`delegation.prepared` event carrying the request id, purpose, destination and
context SHA-256, so `task audit` shows that context was frozen for disclosure even
when nothing was submitted.

Use Deep Research only when explicitly requested or when a large or exhaustive
corpus also requires cross-source, regulatory, unfamiliar-domain, or
high-branching analysis.

## Repository review

`tools/web-review/web-review.zsh` freezes and verifies a repository, diff, or
selected context, then delegates review through the same strict Surf path.

The packer excludes, case-insensitively:

- `tasks/**`;
- `archive/**`;
- Git internals;
- credential filenames and key material;
- symlinks, which are recorded in `manifest/excluded.txt` rather than packaged;
- unsupported or unsafe entries.

Every packed file is then content-scanned for private keys, authorization and
cookie headers, credentialed URLs, tokens, and binary payloads. A finding fails
the run; a known-benign match must be approved by name and is recorded in the
manifest and the receipt:

```sh
tools/web-review/web-review.zsh repo \
  --task-id harness-hardening \
  --task "Review correctness, security, recovery, and missing validation." \
  --reasoning best \
  --research standard \
  --allow-finding "tests/test_core.py:api_token" \
  --prepare-only
```

Tracked paths that are absent from the worktree are named in
`manifest/missing.txt` and in the receipt, so a snapshot cannot claim to be
complete while dropping files anonymously.

The archive is built reproducibly — sorted entries, fixed mtimes, fixed
ownership, single-threaded compression — so preparing the same tree twice yields
the same bytes and the same context SHA-256. That is what lets the two-phase
approval converge: the hash you approve is the hash the live phase computes.

## Other commands

`task enforce-policy` pins an existing workspace to the delegate-first policy;
`task archive` / `task restore` hide and unhide a workspace; `task alias` adds a
search alias; `task cancel` retires an intent; `task run-heartbeat`,
`task run-state` and `task run-abandon` manage an active run's lease and state;
`task release-resources` gives up browser claims; `task delegation-record` and
`task delegation-prepared` are called by the transports to persist disclosure
evidence; `browser-tasks scan` runs the content scanner over explicit paths.

## Validation

```sh
python3 -m pip install -e '.[dev]'
PYTHONPATH=src pytest
zsh -n tools/web-chat/web-chat.zsh
zsh -n tools/web-chat/smoke-test.zsh
tools/web-chat/smoke-test.zsh
zsh -n tools/web-review/web-review.zsh
zsh -n tools/web-review/smoke-test.zsh
tools/web-review/smoke-test.zsh
```

The smoke suites use fake Surf executables and isolated Git fixtures. They do
not touch a real browser. They create their own workspace root, so the enforced
guard has a real workspace to answer for, and they cover the prepare → approve →
live handshake end to end, including that two preparations of the same tree
produce the same context SHA-256.

Timestamp-shaped and generic workspace names are rejected on every path, not only
at creation; an existing workspace with such a name is reported by `task list` as
damaged rather than silently skipped.
