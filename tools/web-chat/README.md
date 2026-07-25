# ChatGPT Web delegate

`web-chat.zsh` is the strict, task-scoped reasoning delegate for Browser Tasks.
It controls the user's authenticated browser through Surf UI and has no API,
in-app-browser, local-model, or search-provider fallback.

The default reasoning policy is `best`: the command selects the strongest
available supported reasoning level, preferring Max and then High, and verifies
the result from the composer pill with the selector closed. Standard research is
the default; `--research deep` is a manual choice for an explicit request or a
large, complex corpus. If the requested mode is unavailable, the command stops.

Before anything else the command runs the task-scoped guard
(`browser_tasks.cli guard`) and refuses to continue unless it allows the tool for
this workspace. An unknown, archived, cancelled or superseded workspace is denied.
Use `--workspace-root` when the workspace lives outside this repository.

Every run has two phases:

1. `--prepare-only` freezes the exact prompt and prints a context SHA-256 bound
   to provider, transport, destination URL, reasoning, mode, and attachment.
2. A live rerun must pass that exact value as `--approved-context-sha`.

Example:

```sh
tools/web-chat/web-chat.zsh \
  --task-id microbusiness-ideas \
  --purpose research \
  --reasoning best \
  --research deep \
  --task-file /tmp/research-task.md \
  --prepare-only
```

After the exact disclosure is approved:

```sh
tools/web-chat/web-chat.zsh \
  --task-id microbusiness-ideas \
  --purpose research \
  --reasoning best \
  --research deep \
  --task-file /tmp/research-task.md \
  --approved-context-sha <exact-sha>
```

A live submission also requires `--record-run-id` (plus `--record-lease-owner`
when that run is active): the receipt and the captured response are validated
against the request identity and stored under `.task/runs/<run-id>/`, so a
disclosure always leaves a trace in the workspace rather than only in `$TMPDIR`.
Every prepare, submitted or not, appends a `delegation.prepared` event.

The command creates a dedicated Surf tab and records its id in the receipt
immediately, verifies the ChatGPT origin, fills the exact prompt, selects the
requested research mode, and verifies the mode and the full prompt immediately
before clicking Send. The frozen prompt ends with a request sentinel, and
submission is accepted only when the delivered message contains both the request
id and that sentinel, so a truncated paste cannot pass. It then waits for
completion, captures the final assistant response plus its links, and records the
research mode it actually observed.

Optional attachments must be frozen regular files whose canonical path has no
symlinked component and does not lead into `tasks/**` or `archive/**`. Both the
filename deny list and a content scan apply: private keys, authorization and
cookie headers, credentialed URLs, tokens and binary payloads are refused. The
prompt text is scanned the same way. An archive produced by `web-review` is
accepted through `--scan-receipt`, whose attestation is verified against the
artifact digest — not trusted from a flag — and whose digest is folded into the
approved context SHA. The attachment digest is re-verified immediately before
upload, and its size and digest are part of the approved context SHA.

The private output directory is removed once the disclosure is recorded in the
workspace; `--keep` retains it, failures always retain it, and directories older
than seven days are pruned on start.
