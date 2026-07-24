# Repository context delegate

`web-review.zsh` freezes and verifies repository context, then delegates the
review through the canonical `tools/web-chat/web-chat.zsh` path.

The only live provider and transport are ChatGPT Web and Surf UI in the user's
browser. The removed `--transport api` and `--model` options are rejected.
Reasoning defaults to `best`; Deep Research is opt-in when the deterministic
router classifies the review as a research task.

## Requirements

- macOS or Linux with zsh
- `git`, `tar`, `zstd`, `shasum`, and standard POSIX utilities
- Surf connected to the user's authenticated browser for a live run

## Two-phase usage

Every invocation requires the active task ID. First prepare and inspect the
frozen repository artifact plus the final web-delegate disclosure:

```sh
tools/web-review/web-review.zsh diff \
  --task-id 20260724-120000-example \
  --base origin/main \
  --task "Review correctness, security, and missing validation." \
  --reasoning best \
  --research standard \
  --prepare-only
```

The output contains:

- repository artifact path, receipt, SHA-256, size, and inventory;
- exact ChatGPT prompt path and receipt;
- final context SHA-256 bound to prompt, attachment, provider, transport,
  reasoning, and research mode.

After approving that exact final context SHA:

```sh
tools/web-review/web-review.zsh diff \
  --task-id 20260724-120000-example \
  --base origin/main \
  --task "Review correctness, security, and missing validation." \
  --reasoning best \
  --research standard \
  --approved-context-sha <exact-sha>
```

The live phase opens a dedicated ChatGPT tab through Surf, selects and verifies
reasoning/research modes, uploads the frozen artifact, submits once, waits for
completion, and saves the response and receipt. A missing mode, connection, or
postcondition stops the run; there is no fallback.

## Context modes

- `repo`: tracked files plus non-ignored untracked files.
- `diff`: committed, staged, and unstaged patches plus current changed files.
- `selected`: only literal repository-relative paths supplied after `--`.
- `--plain`: one frozen patch, or one selected regular file.

Examples:

```sh
tools/web-review/web-review.zsh repo \
  --task-id 20260724-120000-example \
  --task-file /tmp/review-task.md \
  --prepare-only

tools/web-review/web-review.zsh selected \
  --task-id 20260724-120000-example \
  --task "Review path safety." \
  --plain --prepare-only -- tools/web-review/web-review.zsh
```

`--chat-url` and `WEB_REVIEW_CHAT_URL` may select a ChatGPT Project or
conversation path, but the origin must remain exactly `https://chatgpt.com`.

## Snapshot and disclosure safety

Every run creates a private mode-`0700` directory under the canonical system
temporary directory. Artifacts and receipts are mode `0600`.

The packer:

- excludes all `tasks/**`;
- excludes `.env`, `.env.*` except `.env.example`, `.config.yaml`,
  `.config.yml`, `*.pem`, `*.key`, `.git` components, and AppleDouble files;
- rejects traversal, symlinks, unsupported entries, and unsafe plain inputs;
- records deleted, excluded, missing, and skipped submodule entries;
- verifies archive payload and manifest equality;
- fingerprints repository state before and after preparation and discards a
  mixed snapshot if the source changes.

The web-chat delegate separately hashes the exact prompt and attachment. Live
submission requires that final hash, not merely the repository artifact hash.

## Validation

```sh
zsh -n tools/web-review/web-review.zsh
zsh -n tools/web-review/smoke-test.zsh
tools/web-review/smoke-test.zsh

zsh -n tools/web-chat/web-chat.zsh
zsh -n tools/web-chat/smoke-test.zsh
tools/web-chat/smoke-test.zsh
```

The repository smoke suite uses isolated temporary Git fixtures and performs no
live submission. The web-chat smoke suite uses a fake Surf executable to verify
reasoning selection, Deep Research selection, exact disclosure gating,
response capture, and absence of API fallback.
