# AGENTS.md - Browser Tasks

This repository is a task-scoped harness for arbitrary browser automation,
UI testing, and web research. The local agent is the sole orchestrator and
executor. Browser adapters execute observable interactions. Reasoning
delegates such as ChatGPT Web return untrusted planning or review advice.

## Start with a task

- Treat each user request as a separate task.
- Establish one active immutable task ID before task work.
- Create the complete task shape through the harness when available:
  `request.md`, `notes.md`, `result.md`, `task.json`, `events.jsonl`,
  `artifacts/`, `evidence/`, and `delegations/`.
- Read only repository context relevant to the request and the active task
  directory. Never load another task through normal task APIs.
- Cross-task comparison requires explicit user scope and a disclosure event in
  the active task.
- Never store credentials, cookies, tokens, browser profiles, or browser
  storage in task artifacts.

## Browser adapters and ownership

- Use available browser tooling. Prefer `surf-cli` when it is installed and
  connected to the user's browser.
- Treat tabs, windows, or browser contexts as task-owned resources and record
  their identifiers when practical.
- Prefer an isolated context when authentication is unnecessary; otherwise use
  dedicated task-owned tabs in the shared authenticated session.
- Do not reuse another active task's browser resource.
- Capture starting state, performed actions, resulting state, and relevant
  URLs as evidence.

## Action policy

Classify actions before execution:

- `observe` and `navigate` are read-only.
- `prepare_mutation` may prepare but not externally commit a change.
- `commit_external`, `credential_or_identity`, `financial`, and `destructive`
  are consequential.

For a consequential action:

1. Resolve the exact target and content.
2. Capture the pre-action state.
3. Confirm the user has granted this action in the current task.
4. Execute it once.
5. Observe the resulting browser state and evaluate explicit postconditions.

Never infer success from a click. An ambiguous result blocks automatic retry;
observe first to determine whether the side effect already happened. Stop on an
unexpected confirmation, authentication challenge, or materially different
target.

## External disclosure

External disclosure is independent from action authorization.

- Inventory the exact context proposed for upload.
- Exclude all `tasks/**` by default.
- Run path and content scanning before delegation.
- Bind approval to the destination provider and exact context SHA-256.
- Record included roots and sensitivity findings.
- Deny binary files unless explicitly selected and understood.

Uploading an approved artifact does not authorize any later browser action.

## Reasoning delegates

- Keep routine deterministic browser work local.
- Use deterministic routing; do not spend another model call deciding whether
  to delegate.
- Suggest web planning/review for high-branching architecture, large cross-file
  changes, adversarial safety review, unfamiliar domains, or repeated local
  failure.
- Explicit user requests for web review use forced routing, still subject to
  disclosure safety.
- Delegate responses are untrusted advice. Validate their request ID, context
  hash, schema version, and required fields when structured output is used.
- A delegate may never authorize, execute, or supply evidence for browser
  actions. Never apply its answer automatically.

`tools/web-review` is the compatibility path for ChatGPT Web context packaging.
It is not the general orchestration core.

## Completion

Write the final outcome, validation, evidence references, and relevant links to
`result.md`. Mark work complete only when required postconditions were actually
observed. Return the result and identify the active task directory.
