# Browser Tasks

Browser Tasks is a task-scoped harness for browser automation, UI testing, and
web research. A local agent remains the orchestrator and the only executor.
Browser drivers perform observable actions; optional reasoning delegates such
as ChatGPT Web provide untrusted planning or review advice.

## Design

```text
user request
    │
    ▼
task store ──► deterministic router ──► optional reasoning delegate
    │                                        │
    │                              untrusted plan/review
    ▼                                        │
policy + authorization ◄─────────────────────┘
    │
    ▼
browser adapter ──► evidence ──► postcondition verification
```

The boundaries are deliberate:

- A task may read only its approved repository context and its own task folder.
- External disclosure and browser side effects are separate authorizations.
- A delegate response can suggest work but cannot authorize or execute it.
- Consequential actions are proposed, authorized, executed once, and verified.
- An ambiguous result blocks automatic retry.

## Quick start

The Python core requires Python 3.11+ and has no runtime dependencies.

```sh
python3 -m browser_tasks.cli task init \
  20260724-120000-example \
  --goal "Check a browser workflow"

python3 -m browser_tasks.cli route \
  --architecture --steps 10 --files 8 --safety-review
```

For a source checkout, set `PYTHONPATH=src` or install it in a virtual
environment. The task command creates the complete task shape:

```text
tasks/<task-id>/
├── request.md
├── notes.md
├── result.md
├── task.json
├── events.jsonl
├── artifacts/
├── evidence/
└── delegations/
```

Runtime task contents are ignored by Git. `tasks/.gitkeep` preserves the root.

## Lifecycle and authorization

The core validates explicit transitions:

```text
NEW → SCOPED → PLANNED → READY → EXECUTING → VERIFYING → COMPLETED
                              ↘ AWAITING_AUTHORIZATION ↗
```

Blocked, cancelled, and failed outcomes are represented explicitly. Completion
requires verified evidence.

Actions are classified as observation, navigation, mutation preparation,
external commit, identity/credential, financial, or destructive. External
commit and higher classes require a task-bound grant for the exact target and
content digest. A web-chat answer is never a grant.

## Delegation policy

The default mode is `suggest`. A deterministic score considers branching,
dependent steps, file count, safety review, repeated failures, and whether a
local test or live observation can answer more cheaply. It uses no extra model
call. Small deterministic workflows stay local. High-scoring planning or
review is delegated only after disclosure is approved for the exact context
hash.

ChatGPT Web is one optional delegate provider. It is useful for complex
architecture, adversarial review, and large evidence synthesis; it is not the
browser harness itself.

## Disclosure controls

Task directories are excluded from repository review packages by default.
The Python scanner reports known credential filenames, private keys,
authorization/cookie headers, credential-bearing URLs, token assignments,
binary files, and task material. Findings are reported rather than silently
redacted so an operator can make a disclosure decision tied to an exact
SHA-256.

The existing `tools/web-review` command remains a compatibility helper while
its snapshot logic is migrated. It retains its hostile-path and mutation smoke
suite, now excludes all `tasks/**` by default, and validates the ChatGPT origin
after submission.

## Validation

```sh
PYTHONPATH=src pytest
zsh -n tools/web-review/web-review.zsh
zsh -n tools/web-review/smoke-test.zsh
tools/web-review/smoke-test.zsh
```

Live ChatGPT submission is intentionally a manually gated integration test.
