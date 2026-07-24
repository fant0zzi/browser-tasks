# ChatGPT Web delegate

`web-chat.zsh` is the strict, task-scoped reasoning delegate for Browser Tasks.
It controls the user's authenticated browser through Surf UI and has no API,
in-app-browser, local-model, or search-provider fallback.

The default reasoning policy is `best`: the command selects the strongest
available supported reasoning level, preferring Max and then High. Standard
research is the default. Use `--research deep` only for an explicit request or
when the deterministic router identifies a large, complex research corpus. If
the requested mode is unavailable, the command stops.

Every run has two phases:

1. `--prepare-only` freezes the exact prompt and prints a context SHA-256 bound
   to provider, transport, destination URL, reasoning, mode, and attachment.
2. A live rerun must pass that exact value as `--approved-context-sha`.

Example:

```sh
tools/web-chat/web-chat.zsh \
  --task-id 20260724-120000-example \
  --purpose research \
  --reasoning best \
  --research deep \
  --task-file /tmp/research-task.md \
  --prepare-only
```

After the exact disclosure is approved:

```sh
tools/web-chat/web-chat.zsh \
  --task-id 20260724-120000-example \
  --purpose research \
  --reasoning best \
  --research deep \
  --task-file /tmp/research-task.md \
  --approved-context-sha <exact-sha>
```

The command creates a dedicated Surf tab, verifies ChatGPT origin, fills the
exact prompt, selects the requested research mode, and verifies the exact
composer mode immediately before clicking Send. It then waits for completion,
captures the final assistant response plus its links, and emits a receipt with
the verified research mode.

Optional attachments must be frozen regular files outside `tasks/**`.
Credential-like filenames are rejected. Their digest and size are included in
the approved context SHA.
