# Browser Tasks

Browser Tasks is a task-scoped harness for browser automation, UI testing, and
web research through the user's authenticated browser.

Its default operating model is:

- **Surf-only browser execution.** Tabs and authenticated sessions belong to
  the user's browser. In-app browsers, direct browser APIs, and silent transport
  fallbacks are forbidden by the task policy.
- **Maximal ChatGPT Web delegation.** The local agent keeps short,
  deterministic observation and execution local. Planning, unfamiliar
  reasoning, architecture, review, and research go to ChatGPT Web.
- **Strongest available reasoning.** Delegations request `best`, which selects
  Max when available and otherwise High. The selected level is verified and
  recorded.
- **Deep Research for large, complex corpora.** Standard research is the
  default. Deep Research is selected only for an explicit request or for
  exhaustive, high-volume work that also requires cross-source, regulatory,
  unfamiliar-domain, or high-branching analysis.
- **One local executor.** Web-chat answers are untrusted advice. They cannot
  authorize or execute browser actions; the local orchestrator validates them
  and performs authorized actions exactly once.

## Design

```text
user request
    │
    ▼
task store ──► deterministic delegate-first router
    │                          │
    │              simple     ├── ChatGPT Web / best reasoning
    │              local      └── ChatGPT Web / Deep Research
    │                                      │
    ▼                              untrusted response
policy + authorization ◄───────────────────┘
    │
    ▼
Surf in user browser ──► evidence ──► postcondition verification
```

The router is enforceable policy, not a recommendation. New tasks default to:

```text
delegation_policy=maximal
browser_policy=user_browser_only
allowed_browser_adapters=surf
delegate_provider=chatgpt-web
delegate_transport=surf-ui
reasoning_effort=best
deep_research_policy=auto
fallback_policy=block
external_tool_policy=surf_chatgpt_only
```

Agents can fail closed before an external action:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli guard \
  20260724-120000-example --capability research --tool web-chat
```

The same guard rejects `firecrawl`, `web.run`, `in-app-browser`, and other
non-allowlisted browser/research routes.

## Quick start

The Python core requires Python 3.11+ and has no runtime dependencies.

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task init \
  20260724-120000-example \
  --goal "Research and execute a browser workflow"
```

Upgrade an existing task to the strict policy:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli task enforce-policy \
  20260724-120000-example
```

Classify a focused current-information task:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli route \
  --web-research \
  --current-information \
  --cross-source-synthesis \
  --steps 10 \
  --disclosure-authorized
```

This returns a ChatGPT Web / Surf UI / standard research route. Add
`--large-research-volume` when the task must search or reconcile a large,
exhaustive corpus; add `--deep-research` only to request Deep Research
explicitly.

A short, deterministic live check stays local only when a direct observation
or test can decide it:

```sh
PYTHONPATH=src python3 -m browser_tasks.cli route \
  --deterministic --steps 2 --live-observation-primary
```

Each task receives:

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

## ChatGPT Web delegate

`tools/web-chat/web-chat.zsh` is the canonical general-purpose delegate. It:

1. freezes the exact prompt and optional attachment;
2. produces a disclosure receipt and deterministic context SHA-256;
3. requires that exact SHA for live submission;
4. opens a dedicated ChatGPT tab through Surf;
5. fills the exact prompt, selects the requested research mode, and verifies
   the composer state immediately before submission;
6. submits once and verifies the new user message;
7. waits for completion and saves the answer, links, URL, tab ID, and hashes.

Prepare without sending:

```sh
tools/web-chat/web-chat.zsh \
  --task-id 20260724-120000-example \
  --purpose research \
  --reasoning best \
  --research deep \
  --task-file /tmp/exact-research-task.md \
  --prepare-only
```

After approval of the printed context SHA:

```sh
tools/web-chat/web-chat.zsh \
  --task-id 20260724-120000-example \
  --purpose research \
  --reasoning best \
  --research deep \
  --task-file /tmp/exact-research-task.md \
  --approved-context-sha <exact-sha>
```

There is intentionally no `--transport api` and no local or in-app browser
fallback.

## Repository review

`tools/web-review/web-review.zsh` safely freezes repository, diff, or selected
context, then hands the verified artifact to the same strict web-chat delegate.
It excludes `tasks/**`, credential filenames, key material, and unsupported
entries; validates source stability and archive manifests; and requires the
delegate's exact disclosure SHA before a live run.

## Lifecycle and authorization

```text
NEW → SCOPED → PLANNED → READY → EXECUTING → VERIFYING → COMPLETED
                              ↘ AWAITING_AUTHORIZATION ↗
```

External disclosure and browser side effects remain independent
authorizations. Consequential actions require a task-bound grant for their
exact target, summary, content digest, expiry, and use count. An ambiguous
result blocks automatic retry.

## Validation

```sh
PYTHONPATH=src pytest
zsh -n tools/web-chat/web-chat.zsh
zsh -n tools/web-chat/smoke-test.zsh
tools/web-chat/smoke-test.zsh
zsh -n tools/web-review/web-review.zsh
zsh -n tools/web-review/smoke-test.zsh
tools/web-review/smoke-test.zsh
```

The web-chat smoke test uses a fake Surf executable. It proves deterministic
disclosure hashes, rejection before Surf on mismatched approval, Max-before-
High selection, post-fill research-mode verification, response capture, and absence of API
fallback without touching a real browser.
