# Web review harness

`web-review.zsh` is an opt-in local helper for sending repository context to a
ChatGPT web session through Surf. The default `ui` transport drives the existing
signed-in ChatGPT browser session because `surf chatgpt` may be stopped by a
Cloudflare challenge. It is intentionally not wired into
`AGENTS.md`, the repository pipeline, shell startup files, git hooks, or normal
agent routing.

The local agent remains the orchestrator and implementer. The web chat receives
a read-only review contract, and its answer is not imported or applied
automatically.

## Requirements

- macOS with zsh
- `git`, BSD `tar`, `zstd`, `shasum`
- [Surf CLI](https://github.com/nicobailon/surf-cli), authenticated to ChatGPT,
  only for an actual submission

Check the installed adapter:

```sh
surf --version
surf chatgpt --help
```

## Usage

Prepare a full repository package without uploading:

```sh
tools/web-review/web-review.zsh repo \
  --task "Review the repository architecture and identify the highest-risk gaps." \
  --prepare-only
```

Review the complete change from a base through committed, staged, and unstaged
state:

```sh
tools/web-review/web-review.zsh diff \
  --base origin/main \
  --task-file /tmp/review-task.md
```

Review only selected repository-relative paths:

```sh
tools/web-review/web-review.zsh selected \
  --task "Review this harness for path-safety and missing context." \
  --prepare-only -- \
  tools/web-review docs/backend-conventions.md
```

Send only a frozen patch, without an archive:

```sh
tools/web-review/web-review.zsh diff \
  --base origin/main \
  --task "Review this change." \
  --plain
```

Send one frozen copy of a regular file:

```sh
tools/web-review/web-review.zsh selected \
  --task "Review this file." \
  --plain -- tools/web-review/web-review.zsh
```

Open a specific ChatGPT Project for the default UI transport:

```sh
tools/web-review/web-review.zsh diff \
  --task "Review this change." \
  --chat-url "https://chatgpt.com/g/g-p-EXAMPLE/project"
```

`WEB_REVIEW_CHAT_URL` supplies the UI destination when `--chat-url` is omitted;
the final default is `https://chatgpt.com`. UI destinations must use that exact
HTTPS origin; Project and conversation paths are allowed. The UI transport
opens a separate tab and uploads through ChatGPT's visible file input. Before
filling or sending the prompt, it waits boundedly until the page shows the exact
artifact basename with no busy/progress state; a visible upload rejection fails
the run. It then fills the prompt, waits for an enabled `Send prompt`, and
clicks its exact element ref once. It reports success only after the URL
contains `/c/`, the composer is empty, and a new user-authored message beyond
the pre-upload baseline owns the expected attachment. A redirect, an existing
conversation URL, or a filename elsewhere on the page cannot satisfy the
submission check. It prints the tab id and requested URL, then exits. It does
not capture, save, or import the response.

The legacy Surf ChatGPT adapter remains available explicitly:

```sh
tools/web-review/web-review.zsh diff \
  --task "Review this change." \
  --transport api \
  --model gpt-4o
```

`--model` is accepted only with `--transport api`.

Without `--prepare-only`, the tool prepares and verifies the context file first,
shows its receipt, and then requires an interactive confirmation before calling
the selected transport. No provider model is pinned by this harness.

## Context modes

- `repo`: tracked files plus untracked files that are not excluded by Git.
- `diff`: three patch sections (`base...HEAD`, staged vs. `HEAD`, and unstaged
  vs. the index), safe untracked-file patches, plus current versions of changed
  and untracked files. Every patch command is scoped to the same mandatory
  filename exclusions as the file inventory and forces literal Git pathspecs.
- `selected`: the eligible files under literal repository-relative paths.
  Absolute paths, `..` traversal, and `.git` paths are rejected.

Deleted files are represented by patches in `diff` mode and are never passed to
`tar`. Committed, staged, and unstaged deletions are deduplicated and reported
as `deleted_count`. Submodule entries are skipped rather than recursively
archived and reported separately as `skipped_submodule_count`; other vanished
inventory entries use `missing_count`. Deletion-only diffs are valid even when
there is no current file to package.

`--plain` is valid only for `diff` or for `selected` with exactly one regular
file. It is rejected for `repo`, selected directories, symlinks, and multiple
selected paths. Redundant `.` components are normalized, while absolute paths,
`.git`, and `..` traversal are rejected. A plain diff produces a frozen
`.patch` inside the private run directory; a plain selected run revalidates the
frozen payload entry as a regular non-symlink immediately before making a
non-following, byte-verified copy.

## Context file and receipt

Every run creates a private mode-`0700` direct child of the canonical system
temporary directory (`${TMPDIR:-/tmp}`):

```text
${TMPDIR:-/tmp}/web-review-output.XXXXXX/
  <repo>-<mode>-<timestamp>-<pid>.tar.zst
  <repo>-<mode>-<timestamp>-<pid>.tar.zst.receipt.txt
```

Plain runs create a `.patch` or selected-file copy with the same
`.receipt.txt` suffix. Every receipt records the artifact path, format, byte
size, SHA-256, source-state fingerprint, snapshot, mode, immutable base SHA plus
its selected ref, selection, counts, and excluded names. The script runs with
`umask 077`; artifacts and receipts have no group/other permissions.

An archive contains:

- `repository/`: selected current files;
- `context/review.patch`: diff layers in `diff` mode;
- `context/changed-files.txt`: escaped changed-path inventory;
- `manifest/files.nul`: canonical NUL-delimited file inventory;
- `manifest/files.txt`: human-readable shell-escaped inventory;
- `manifest/excluded.txt`: shell-escaped names rejected by mandatory rules;
- `manifest/snapshot.txt`: HEAD, dirty state, mode, base, and counts;
- `request/task.md` and `request/review-contract.md`.

The archive cannot contain its own final hash, so the sidecar receipt adds the
final byte size and SHA-256. The selected context file's hash is also injected
into the Surf prompt. Keep the receipt with any saved review result and reject
an answer that echoes a different HEAD or hash.

The source fingerprint covers HEAD, index entries, tracked working-tree diffs,
and the contents of non-ignored untracked files. It is computed before and
after preparation. If it changes, the new private output directory is deleted
and the run fails instead of declaring a mixed snapshot prepared.

Outputs are retained until you remove the exact run directory:

```sh
rm -rf -- "${TMPDIR:-/tmp}/web-review-output.ABC123"
```

Use the concrete path printed by the harness; do not use a broad temporary-file
glob. Automatic discard first canonicalizes the path and refuses anything that
is not an existing non-symlink direct child named `web-review-output.*`.

## Exclusions

Git ignore rules are honored for untracked files. These names are excluded even
when tracked:

- `.env`
- `.env.*`, except the exact safe example name `.env.example`
- `.config.yaml`, `.config.yml`
- `*.pem`, `*.key`
- `.git` path components and AppleDouble `._*` files

The inventory is checked before packaging and checked again after extracting
the finished archive into a temporary verification directory. Verification
independently enumerates the extracted repository payload and requires exact
two-way equality with `manifest/files.nul`; unsupported, excluded,
unmanifested, duplicate, missing, and non-canonical entries fail the run.

This is a deliberately narrow convenience boundary, not a general secret
scanner. Review the receipt and package scope before confirming an upload.

## Smoke checks

```sh
zsh -n tools/web-review/web-review.zsh
zsh -n tools/web-review/smoke-test.zsh
tools/web-review/web-review.zsh --help
tools/web-review/web-review.zsh selected \
  --task "Smoke-test selected context." \
  --prepare-only -- tools/web-review
tools/web-review/web-review.zsh diff \
  --base HEAD \
  --task "Smoke-test diff context." \
  --prepare-only
tools/web-review/web-review.zsh diff \
  --base HEAD \
  --task "Smoke-test a plain diff." \
  --plain --prepare-only
tools/web-review/web-review.zsh selected \
  --task "Smoke-test one plain file." \
  --plain --prepare-only -- tools/web-review/prompt.md
tools/web-review/smoke-test.zsh
```

The checks create only temporary packages and receipts under the system
temporary directory. They do not upload anything. `smoke-test.zsh` uses
isolated temporary Git repositories
to prove exclusion of a changed `.env`, literal handling of a hostile Git
pathspec, preservation of spaces, leading dashes, brackets, Unicode, and
newlines in filenames, exact payload/manifest equality, selected-path
normalization, frozen symlink rejection, source-mutation cleanup, and
committed/staged/unstaged deletion metadata and patches. It also runs help with
an empty `PATH` and prepare-only with a `PATH` that contains every packaging
dependency except Surf. Additional fixtures mutate a tracked file from the real
packaging `tar` call, overlap the same deletion across diff layers, synthesize a
gitlink, replace a tracked file with a directory, force a final plain artifact
to become a symlink, and verify that private test verbs require explicit test
mode.

The smoke suite deliberately does not submit through the UI transport. Browser
submission remains an explicit, confirmation-gated integration action. For a
manual UI check, use a small disposable selected file without `--prepare-only`.
Confirm that the prompt is not filled or sent until its exact basename is shown
as a completed attachment, and that success is printed only after the new user
message contains that attachment. Repeat with an unsupported or oversized
disposable file when the current ChatGPT UI can reject it; the harness must
stop on the visible upload error before filling or clicking Send.

The UI adapter necessarily depends on ChatGPT's current DOM and accessibility
labels. It is version-sensitive and intentionally safe-failing: an unknown
upload, composer, Send, attachment, or submitted-message state stops the run
instead of guessing or issuing another click. Rejected-upload behavior therefore
remains a manual browser check when the live UI cannot produce a deterministic
rejection fixture.
