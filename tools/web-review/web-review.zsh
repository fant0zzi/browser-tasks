#!/bin/zsh

emulate -LR zsh
setopt errexit nounset pipefail
umask 077

readonly PROGRAM="${0:t}"
readonly SCRIPT_DIR="${0:A:h}"
readonly WEB_CHAT_DELEGATE="${SCRIPT_DIR:h}/web-chat/web-chat.zsh"
temp_root="${TMPDIR:-/tmp}"
readonly SYSTEM_TMP_ROOT="${temp_root:A}"
unset temp_root

usage() {
  print -r -- 'Usage:
  web-review.zsh repo     --task TEXT | --task-file FILE [options]
  web-review.zsh diff     --task TEXT | --task-file FILE [--base REF] [options]
  web-review.zsh selected --task TEXT | --task-file FILE [options] -- PATH...

Modes:
  repo       Package tracked and non-ignored untracked files.
  diff       Package committed, staged, and unstaged patches plus changed files.
  selected   Package only the named repository-relative files or directories.

Options:
  --task-id ID      Active Browser Tasks task ID.
  --task TEXT       Review task sent with the context.
  --task-file FILE  Read the review task from a file.
  --base REF        Diff base. Defaults to origin/HEAD, main, or master.
  --chat-url URL    ChatGPT URL for Surf UI (or WEB_REVIEW_CHAT_URL).
  --reasoning LEVEL best|high|max (default: best).
  --research MODE   standard|deep (default: standard).
  --approved-context-sha H
                    Exact SHA printed by the delegate prepare-only phase.
  --workspace-root DIR
                    Root holding tasks/ (default: this repository).
  --record-run-id RUN
                    Store receipt and response under that run; required live.
  --record-lease-owner OWNER
                    Lease owner of the recording run when it is active.
  --allow-finding PATH:KIND
                    Approve one known-benign disclosure scan finding.
  --plain           Send a frozen patch (diff) or one regular file (selected).
  --prepare-only    Build, verify, and prepare exact disclosure without upload.
  -h, --help        Show this help.

The context file and its receipt are written to a private directory under the
system temporary directory. The only live transport is ChatGPT Web through Surf
UI in the user browser. The delegate captures the answer and never falls back.
'
}

fail() {
  print -u2 -r -- "$PROGRAM: $*"
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

is_excluded() {
  # Case-insensitive: the default macOS volume is case-insensitive, so `TASKS/x`
  # and `.ENV` reach the same bytes as their lowercase spelling.
  # Never name a local `path`: zsh ties `path` to `PATH`, so assigning to it
  # corrupts command lookup for the rest of the run.
  local candidate part basename
  candidate="${1:l}"
  basename="${candidate:t}"

  [[ "$candidate" == tasks/* || "$candidate" == archive/* ]] && return 0

  for part in ${(s:/:)candidate}; do
    [[ "$part" == ".git" ]] && return 0
  done

  [[ "$basename" == ".env.example" ]] && return 1
  [[ "$basename" == ".env" || "$basename" == .env.* ]] && return 0
  [[ "$basename" == ".config.yaml" || "$basename" == ".config.yml" ]] && return 0
  [[ "$basename" == ".netrc" || "$basename" == ".npmrc" ]] && return 0
  [[ "$basename" == ".pypirc" || "$basename" == ".git-credentials" ]] && return 0
  [[ "$basename" == id_* ]] && return 0
  [[ "$basename" == *.pem || "$basename" == *.key ]] && return 0
  [[ "$basename" == *.p12 || "$basename" == *.pfx ]] && return 0
  [[ "$basename" == *.jks || "$basename" == *.keystore ]] && return 0
  [[ "$basename" == *.p8 || "$basename" == *.ppk || "$basename" == *.kdbx ]] && return 0
  [[ "$basename" == ._* ]] && return 0
  return 1
}

normalize_repo_relative_path() {
  local input="$1" part
  local -a normalized
  normalized=()

  [[ -n "$input" ]] || fail "selected path must not be empty"
  [[ "$input" != /* ]] || fail "selected path must be repository-relative: $input"

  for part in ${(s:/:)input}; do
    case "$part" in
      ""|.) ;;
      ..) fail "selected path escapes the repository: $input" ;;
      .git) fail "selected path enters .git: $input" ;;
      *) normalized+=("$part") ;;
    esac
  done
  if [[ ${#normalized[@]} -eq 0 ]]; then
    REPLY="."
  else
    REPLY="${(j:/:)normalized}"
  fi
}

is_selected() {
  local file="$1" selected
  for selected in "${selected_paths[@]}"; do
    [[ "$selected" == "." || "$file" == "$selected" || "$file" == "$selected"/* ]] && return 0
  done
  return 1
}

resolve_diff_base() {
  local requested="$1" candidate resolved=""
  local -a candidates

  if [[ -n "$requested" ]]; then
    candidates=("$requested")
  else
    candidate="$(git -C "$repo_root" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    [[ -n "$candidate" ]] && candidates+=("$candidate")
    candidates+=(main master origin/main origin/master)
  fi

  for candidate in "${candidates[@]}"; do
    if git -C "$repo_root" rev-parse --verify --quiet "${candidate}^{commit}" >/dev/null; then
      resolved="$candidate"
      break
    fi
  done

  [[ -n "$resolved" ]] || fail "could not resolve a diff base; pass --base REF"
  print -r -- "$resolved"
}

write_diff_context() {
  local patch_file="$1" base_ref="$2" base_sha="$3" file
  local -a safe_paths
  safe_paths=("${safe_changed_paths[@]}")

  {
    print -r -- "# Web review diff"
    print -r -- "# base-ref: $base_ref"
    print -r -- "# base-sha: $base_sha"
    print -r -- "# head: $head_sha"
    print -r -- ""
    print -r -- "## Committed changes: git diff ${base_sha}...HEAD"
    git --literal-pathspecs -C "$repo_root" diff --no-ext-diff --binary --no-renames "$base_sha"...HEAD -- "${safe_paths[@]}"
    print -r -- ""
    print -r -- "## Staged changes: git diff --cached HEAD"
    git --literal-pathspecs -C "$repo_root" diff --no-ext-diff --binary --no-renames --cached HEAD -- "${safe_paths[@]}"
    print -r -- ""
    print -r -- "## Unstaged changes: git diff"
    git --literal-pathspecs -C "$repo_root" diff --no-ext-diff --binary --no-renames -- "${safe_paths[@]}"
    print -r -- ""
    print -r -- "## Untracked files"
    for file in "${safe_untracked_paths[@]}"; do
      git --literal-pathspecs -C "$repo_root" diff --no-ext-diff --binary --no-index -- /dev/null "$file" || {
        # git diff --no-index reports 1 for "differences found"; anything else
        # is a real failure that must not exit with an empty message.
        [[ "$?" -eq 1 ]] \
          || fail "git diff --no-index failed for untracked path: $file"
      }
    done
  } > "$patch_file"
}

source_fingerprint() {
  local prefix="$1"
  local index_list="${prefix}-index.nul"
  local untracked_list="${prefix}-untracked.nul"

  git -C "$repo_root" ls-files --stage -z > "$index_list"
  git -C "$repo_root" ls-files -z --others --exclude-standard --no-empty-directory > "$untracked_list"

  {
    print -rn -- "HEAD"$'\0'
    git -C "$repo_root" rev-parse --verify HEAD
    print -rn -- $'\0'"INDEX"$'\0'
    command cat -- "$index_list"
    print -rn -- "TRACKED_WORKTREE"$'\0'
    git -C "$repo_root" diff --no-ext-diff --binary
    print -rn -- $'\0'"UNTRACKED"$'\0'
    if [[ -s "$untracked_list" ]]; then
      (
        cd "$repo_root"
        tar -cf - --null --no-recursion -T "$untracked_list"
      )
    fi
  } | shasum -a 256 | awk '{print $1}'
}

validate_frozen_regular() {
  local frozen_path="$1" subject="${2:-selected --plain frozen source}"
  [[ -f "$frozen_path" && ! -L "$frozen_path" ]] \
    || fail "$subject is not a regular non-symlink file"
}

verify_repository_payload() {
  local payload_root="$1" manifest_file="$2" entry relative file part
  integer expected_count=0 actual_count=0
  typeset -A expected actual
  local -a entries

  [[ -d "$payload_root" && ! -L "$payload_root" ]] \
    || fail "archive verification failed: repository payload root is invalid"
  [[ -f "$manifest_file" && ! -L "$manifest_file" ]] \
    || fail "archive verification failed: file manifest is invalid"

  while IFS= read -r -d '' file; do
    [[ -n "$file" && "$file" != /* ]] \
      || fail "archive verification found an invalid manifest path"
    for part in ${(s:/:)file}; do
      [[ -n "$part" && "$part" != "." && "$part" != ".." ]] \
        || fail "archive verification found a non-canonical manifest path: $file"
    done
    is_excluded "$file" && fail "archive verification found excluded manifest path: $file"
    [[ -z "${expected[$file]-}" ]] \
      || fail "archive verification found duplicate manifest path: $file"
    [[ -f "$payload_root/$file" && ! -L "$payload_root/$file" ]] \
      || fail "archive verification found missing or unsupported manifest entry: $file"
    expected[$file]=1
    (( expected_count += 1 ))
  done < "$manifest_file"

  entries=("$payload_root"/**/*(DN))
  for entry in "${entries[@]}"; do
    if [[ -d "$entry" && ! -L "$entry" ]]; then
      continue
    fi
    [[ -f "$entry" && ! -L "$entry" ]] \
      || fail "archive verification found unsupported payload entry: $entry"
    relative="${entry#"$payload_root"/}"
    is_excluded "$relative" && fail "archive verification found excluded payload path: $relative"
    [[ -n "${expected[$relative]-}" ]] \
      || fail "archive verification found unmanifested payload path: $relative"
    [[ -z "${actual[$relative]-}" ]] \
      || fail "archive verification enumerated duplicate payload path: $relative"
    actual[$relative]=1
    (( actual_count += 1 ))
  done

  [[ "$actual_count" -eq "$expected_count" ]] \
    || fail "archive verification manifest and payload counts differ"
  for file in ${(k)expected}; do
    [[ -n "${actual[$file]-}" ]] \
      || fail "archive verification payload is missing manifest path: $file"
  done
  print -r -- "$actual_count"
}

readonly REPO_TOOLS_ROOT="${SCRIPT_DIR:h:h}"

browser_tasks_cli() {
  PYTHONPATH="$REPO_TOOLS_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m browser_tasks.cli "$@"
}

typeset -gA allowed_findings
typeset -ga approved_findings
approved_findings=()

scan_context() {
  local root="$1"
  shift
  local output line kind finding_path
  integer unapproved=0 scan_status=0
  output="$(browser_tasks_cli scan --repo-root "$root" "$@" 2>&1)" \
    && scan_status=0 || scan_status=$?
  (( scan_status == 0 )) && return 0
  # Only exit code 5 means "findings". Treating every non-zero status as
  # findings made any scan failure with tab-free output read as clean.
  (( scan_status == 5 )) \
    || fail "disclosure scan could not run:"$'\n'"$output"
  for line in ${(f)output}; do
    [[ "$line" == *$'\t'* ]] || continue
    kind="${line%%$'\t'*}"
    finding_path="${line#*$'\t'}"
    if [[ -n "${allowed_findings[$finding_path:$kind]-}" ]]; then
      approved_findings+=("$finding_path:$kind")
      continue
    fi
    print -u2 -r -- "$PROGRAM: unapproved disclosure finding: $kind $finding_path"
    (( unapproved += 1 ))
  done
  (( unapproved == 0 )) \
    || fail "disclosure scan rejected the context; approve a known-benign match with --allow-finding PATH:KIND"
}

validate_private_output_dir() {
  local candidate="$1" canonical
  [[ -d "$candidate" && ! -L "$candidate" ]] \
    || fail "refusing to remove an invalid private output directory"
  canonical="${candidate:A}"
  [[ "${canonical:h}" == "$SYSTEM_TMP_ROOT" ]] \
    || fail "refusing to remove output outside the system temporary directory"
  [[ "${canonical:t}" == web-review-output.* ]] \
    || fail "refusing to remove an unexpected output directory"
  REPLY="$canonical"
}

discard_private_output_dir() {
  validate_private_output_dir "$1"
  rm -rf -- "$REPLY"
}

enforce_source_fingerprint() {
  local before="$1" after="$2" private_output_dir="$3"
  [[ "$before" == "$after" ]] && return 0
  discard_private_output_dir "$private_output_dir"
  fail "repository changed while context was being prepared; discarded the output"
}

render_prompt() {
  local template task="$1" context_hash="$2" context_bytes="$3"
  template="$(<"$SCRIPT_DIR/prompt.md")"

  print -r -- "$template"
  print -r -- ""
  print -r -- "## Run metadata"
  print -r -- "- Snapshot HEAD: \`$head_sha\`"
  print -r -- "- Working tree dirty: \`$dirty\`"
  print -r -- "- Context mode: \`$mode\`"
  print -r -- "- Context format: \`$context_format\`"
  print -r -- "- Submission transport: \`$transport\`"
  print -r -- "- Diff base ref: \`${resolved_base_ref:-none}\`"
  print -r -- "- Diff base SHA: \`${resolved_base_sha:-none}\`"
  print -r -- "- Context file SHA-256: \`$context_hash\`"
  print -r -- "- Context file bytes: \`$context_bytes\`"
  print -r -- ""
  print -r -- "## Task"
  print -r -- "$task"
}

if [[ "${1:-}" == __* && "${WEB_REVIEW_TEST_MODE:-}" != 1 ]]; then
  fail "private test commands require WEB_REVIEW_TEST_MODE=1"
fi

case "${1:-}" in
  __verify-payload)
    [[ $# -eq 3 ]] || fail "__verify-payload requires ROOT and MANIFEST"
    verify_repository_payload "$2" "$3" >/dev/null
    exit 0
    ;;
  __validate-frozen-regular)
    [[ $# -eq 2 ]] || fail "__validate-frozen-regular requires PATH"
    validate_frozen_regular "$2"
    exit 0
    ;;
  __test-fingerprint-mismatch)
    [[ $# -eq 1 ]] || fail "__test-fingerprint-mismatch takes no arguments"
    require_command mktemp
    require_command rm
    test_output_dir="$(
      mktemp -d "$SYSTEM_TMP_ROOT/web-review-output.XXXXXX"
    )"
    print -r -- "$test_output_dir"
    enforce_source_fingerprint before after "$test_output_dir"
    exit 0
    ;;
  __validate-output-dir)
    [[ $# -eq 2 ]] || fail "__validate-output-dir requires PATH"
    validate_private_output_dir "$2"
    exit 0
    ;;
esac

[[ $# -gt 0 ]] || {
  usage
  exit 1
}

mode="$1"
shift

case "$mode" in
  repo|diff|selected) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *) fail "unknown mode: $mode" ;;
esac

task_id=""
task_text=""
task_file=""
base_ref=""
transport=surf-ui
chat_url="${WEB_REVIEW_CHAT_URL:-https://chatgpt.com}"
reasoning=best
research_mode=standard
approved_context_sha=""
prepare_only=0
plain=0
workspace_root="${WEB_REVIEW_WORKSPACE_ROOT:-$REPO_TOOLS_ROOT}"
record_run_id=""
record_lease_owner=""
typeset -a selected_paths
selected_paths=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task-id)
      [[ $# -ge 2 ]] || fail "--task-id requires a value"
      task_id="$2"
      shift 2
      ;;
    --task)
      [[ $# -ge 2 ]] || fail "--task requires a value"
      task_text="$2"
      shift 2
      ;;
    --task-file)
      [[ $# -ge 2 ]] || fail "--task-file requires a value"
      task_file="$2"
      shift 2
      ;;
    --base)
      [[ $# -ge 2 ]] || fail "--base requires a value"
      base_ref="$2"
      shift 2
      ;;
    --model)
      fail "--model was removed; ChatGPT Web reasoning is selected in Surf UI"
      ;;
    --transport)
      fail "--transport was removed; only Surf UI is allowed"
      ;;
    --chat-url)
      [[ $# -ge 2 ]] || fail "--chat-url requires a value"
      chat_url="$2"
      shift 2
      ;;
    --reasoning)
      [[ $# -ge 2 ]] || fail "--reasoning requires a value"
      reasoning="$2"
      shift 2
      ;;
    --research)
      [[ $# -ge 2 ]] || fail "--research requires a value"
      research_mode="$2"
      shift 2
      ;;
    --approved-context-sha)
      [[ $# -ge 2 ]] || fail "--approved-context-sha requires a value"
      approved_context_sha="$2"
      shift 2
      ;;
    --workspace-root)
      [[ $# -ge 2 ]] || fail "--workspace-root requires a value"
      workspace_root="$2"
      shift 2
      ;;
    --record-run-id)
      [[ $# -ge 2 ]] || fail "--record-run-id requires a value"
      record_run_id="$2"
      shift 2
      ;;
    --record-lease-owner)
      [[ $# -ge 2 ]] || fail "--record-lease-owner requires a value"
      record_lease_owner="$2"
      shift 2
      ;;
    --allow-finding)
      [[ $# -ge 2 ]] || fail "--allow-finding requires PATH:KIND"
      [[ "$2" == *:* ]] || fail "--allow-finding expects PATH:KIND"
      allowed_findings[$2]=1
      shift 2
      ;;
    --prepare-only)
      prepare_only=1
      shift
      ;;
    --plain)
      plain=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      selected_paths+=("$@")
      break
      ;;
    -*)
      fail "unknown option: $1"
      ;;
    *)
      selected_paths+=("$1")
      shift
      ;;
  esac
done

[[ -z "$task_text" || -z "$task_file" ]] || fail "use either --task or --task-file, not both"
if [[ -n "$task_file" ]]; then
  # Mirrors the delegate: a symlinked task file must not pull unintended
  # content into the frozen prompt.
  [[ -f "$task_file" && ! -L "$task_file" ]] \
    || fail "task file must be a regular non-symlink file: $task_file"
  task_text="$(<"$task_file")"
fi
[[ -n "${task_text//[[:space:]]/}" ]] || fail "a non-empty --task or --task-file is required"
[[ "$task_id" =~ '^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$' ]] \
  && [[ ! "$task_id" =~ '^[0-9]{8}-[0-9]{6}($|-)' ]] \
  || fail "invalid or missing --task-id"
[[ "$reasoning" == best || "$reasoning" == high || "$reasoning" == max ]] \
  || fail "--reasoning must be best, high, or max"
[[ "$research_mode" == standard || "$research_mode" == deep ]] \
  || fail "--research must be standard or deep"
[[ "$chat_url" == "https://chatgpt.com" || "$chat_url" == "https://chatgpt.com/"* ]] \
  || fail "UI chat URL must use the https://chatgpt.com origin"
[[ "$chat_url" != *[[:space:]]* ]] || fail "UI chat URL must not contain whitespace"
[[ "$mode" == "diff" || -z "$base_ref" ]] || fail "--base is valid only in diff mode"
[[ "$mode" != "repo" || "$plain" -eq 0 ]] || fail "--plain is not supported in repo mode"
if [[ "$mode" == "selected" ]]; then
  [[ ${#selected_paths[@]} -gt 0 ]] || fail "selected mode requires at least one path"
  if (( plain )); then
    [[ ${#selected_paths[@]} -eq 1 ]] || fail "selected --plain requires exactly one regular file"
  fi
else
  [[ ${#selected_paths[@]} -eq 0 ]] || fail "$mode mode does not accept selected paths"
fi

for command_name in git tar zstd shasum awk cat cp chmod mkdir mktemp rm wc tr date \
  find touch sort head python3; do
  require_command "$command_name"
done

# A denied guard is terminal for this route, so ask before any packaging work.
guard_output="$(
  browser_tasks_cli --root "$workspace_root" guard "$task_id" \
    --capability reasoning --tool web-review 2>&1
)" || fail "task guard denied web-review for $task_id:"$'\n'"$guard_output"

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a Git repository"
repo_root="${repo_root:A}"
repo_name="${repo_root:t}"
repo_name="${repo_name//[^A-Za-z0-9._-]/-}"

if [[ "$mode" == "selected" ]]; then
  typeset -a normalized_paths
  normalized_paths=()
  for selected in "${selected_paths[@]}"; do
    normalize_repo_relative_path "$selected"
    normalized_paths+=("$REPLY")
  done
  selected_paths=("${normalized_paths[@]}")
  if (( plain )); then
    [[ -f "$repo_root/${selected_paths[1]}" && ! -L "$repo_root/${selected_paths[1]}" ]] \
      || fail "selected --plain requires one regular file, not a directory or symlink"
  fi
fi

head_sha="$(git -C "$repo_root" rev-parse --verify HEAD 2>/dev/null)" || fail "repository has no HEAD commit"
branch="$(git -C "$repo_root" symbolic-ref --quiet --short HEAD 2>/dev/null || print -r -- detached)"
if [[ -n "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=normal)" ]]; then
  dirty=true
else
  dirty=false
fi

resolved_base_ref=""
resolved_base_sha=""
if [[ "$mode" == "diff" ]]; then
  resolved_base_ref="$(resolve_diff_base "$base_ref")"
  resolved_base_sha="$(
    git -C "$repo_root" rev-parse --verify "${resolved_base_ref}^{commit}"
  )"
fi

work_dir="$(mktemp -d "$SYSTEM_TMP_ROOT/web-review.XXXXXX")"
output_dir="$(mktemp -d "$SYSTEM_TMP_ROOT/web-review-output.XXXXXX")"
chmod 700 "$output_dir"
keep_output=0
cleanup_runtime() {
  rm -rf -- "$work_dir"
  if (( ! keep_output )) && [[ -e "$output_dir" || -L "$output_dir" ]]; then
    discard_private_output_dir "$output_dir"
  fi
}
trap cleanup_runtime EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

source_fingerprint_before="$(source_fingerprint "$work_dir/source-before")"
head_after_fingerprint="$(git -C "$repo_root" rev-parse --verify HEAD 2>/dev/null)" \
  || fail "repository HEAD changed while starting context preparation"
if [[ -n "$(git -C "$repo_root" status --porcelain=v1 --untracked-files=normal)" ]]; then
  dirty_after_fingerprint=true
else
  dirty_after_fingerprint=false
fi
[[ "$head_after_fingerprint" == "$head_sha" && "$dirty_after_fingerprint" == "$dirty" ]] \
  || fail "repository HEAD or dirty state changed while starting context preparation"

bundle_root="$work_dir/bundle"
repository_dir="$bundle_root/repository"
manifest_dir="$bundle_root/manifest"
context_dir="$bundle_root/context"
request_dir="$bundle_root/request"
mkdir -p -- "$repository_dir" "$manifest_dir" "$context_dir" "$request_dir"

all_candidates="$work_dir/all-candidates.nul"
selected_files="$manifest_dir/files.nul"
excluded_files="$manifest_dir/excluded.nul"
missing_files="$manifest_dir/missing.nul"
: > "$all_candidates"
: > "$selected_files"
: > "$excluded_files"
: > "$missing_files"

{
  git -C "$repo_root" ls-files -z
  git -C "$repo_root" ls-files -z --others --exclude-standard --no-empty-directory
} > "$all_candidates"

typeset -A submodule_paths
index_entries="$work_dir/index-entries.nul"
git -C "$repo_root" ls-files --stage -z > "$index_entries"
while IFS= read -r -d '' index_entry; do
  if [[ "$index_entry" == "160000 "*$'\t'* ]]; then
    file="${index_entry#*$'\t'}"
    submodule_paths[$file]=1
  fi
done < "$index_entries"

integer candidate_count=0 included_count=0 excluded_count=0 missing_count=0
integer safe_changed_count=0 deleted_count=0 skipped_submodule_count=0
typeset -A changed_paths safe_changed_map untracked_paths deleted_paths
typeset -a safe_changed_paths safe_untracked_paths
safe_changed_paths=()
safe_untracked_paths=()
if [[ "$mode" == "diff" ]]; then
  changed_list="$work_dir/changed.nul"
  untracked_list="$work_dir/untracked.nul"
  deleted_list="$work_dir/deleted.nul"
  {
    git -C "$repo_root" diff --no-renames --name-only -z "$resolved_base_sha"...HEAD
    git -C "$repo_root" diff --no-renames --name-only -z --cached HEAD
    git -C "$repo_root" diff --no-renames --name-only -z
    git -C "$repo_root" ls-files -z --others --exclude-standard --no-empty-directory
  } > "$changed_list"
  {
    git -C "$repo_root" diff --no-renames --diff-filter=D --name-only -z "$resolved_base_sha"...HEAD
    git -C "$repo_root" diff --no-renames --diff-filter=D --name-only -z --cached HEAD
    git -C "$repo_root" diff --no-renames --diff-filter=D --name-only -z
  } > "$deleted_list"
  git -C "$repo_root" ls-files -z --others --exclude-standard --no-empty-directory > "$untracked_list"
  while IFS= read -r -d '' file; do
    changed_paths[$file]=1
  done < "$changed_list"
  while IFS= read -r -d '' file; do
    untracked_paths[$file]=1
  done < "$untracked_list"
  while IFS= read -r -d '' file; do
    deleted_paths[$file]=1
  done < "$deleted_list"

  for file in ${(ok)changed_paths}; do
    if is_excluded "$file"; then
      print -rn -- "$file"$'\0' >> "$excluded_files"
      (( excluded_count += 1 ))
      continue
    fi
    if [[ -L "$repo_root/$file" ]]; then
      # The payload packer excludes symlinks; the patch must not disclose the
      # target the receipt says was excluded.
      print -rn -- "$file"$'\0' >> "$excluded_files"
      (( excluded_count += 1 ))
      continue
    fi
    safe_changed_map[$file]=1
    safe_changed_paths+=("$file")
    (( safe_changed_count += 1 ))
    [[ -n "${deleted_paths[$file]-}" ]] && (( deleted_count += 1 ))
    [[ -n "${untracked_paths[$file]-}" ]] && safe_untracked_paths+=("$file")
  done

  if (( safe_changed_count == 0 )); then
    (( excluded_count > 0 )) && fail "diff contains only excluded paths"
    fail "diff has no changes"
  fi
  write_diff_context "$context_dir/review.patch" "$resolved_base_ref" "$resolved_base_sha"
fi

while IFS= read -r -d '' file; do
  (( candidate_count += 1 ))

  if [[ "$mode" == "diff" && -z "${safe_changed_map[$file]-}" ]]; then
    continue
  fi
  if [[ "$mode" == "selected" ]] && ! is_selected "$file"; then
    continue
  fi
  if [[ "$mode" != "diff" ]] && is_excluded "$file"; then
    print -rn -- "$file"$'\0' >> "$excluded_files"
    (( excluded_count += 1 ))
    continue
  fi
  if [[ -n "${submodule_paths[$file]-}" ]]; then
    (( skipped_submodule_count += 1 ))
    continue
  fi
  if [[ -L "$repo_root/$file" ]]; then
    # A symlink in the payload discloses its target path and is a hazard for
    # whoever unpacks the archive; the README promised they were rejected.
    print -rn -- "$file"$'\0' >> "$excluded_files"
    (( excluded_count += 1 ))
    continue
  fi
  if [[ ! -f "$repo_root/$file" ]]; then
    if [[ "$mode" == "diff" && -n "${deleted_paths[$file]-}" ]]; then
      continue
    fi
    # Name every omission: an anonymous count cannot tell a reviewer whether a
    # module was removed or silently dropped by the packer.
    print -rn -- "$file"$'\0' >> "$missing_files"
    (( missing_count += 1 ))
    continue
  fi

  print -rn -- "$file"$'\0' >> "$selected_files"
  (( included_count += 1 ))
done < "$all_candidates"

if [[ "$mode" != "diff" ]]; then
  [[ "$included_count" -gt 0 ]] || fail "no eligible files matched mode '$mode'"
fi

if (( included_count > 0 )); then
  (
    cd "$repo_root"
    tar -cf - --null --no-recursion -T "$selected_files"
  ) | tar -xf - -C "$repository_dir"
fi

# Content scanning is a hard disclosure requirement (AGENTS.md), and a filename
# denylist cannot see a token inside `config.py`. Fail closed before the bytes
# are ever packaged; a known-benign match must be approved by name.
if (( included_count > 0 )); then
  scan_context "$repository_dir" --from-nul "$selected_files"
fi
if [[ "$mode" == "diff" && -f "$context_dir/review.patch" ]]; then
  scan_context "$context_dir" -- review.patch
fi

{
  while IFS= read -r -d '' file; do
    printf '%q\n' "$file"
  done < "$selected_files"
} > "$manifest_dir/files.txt"

{
  while IFS= read -r -d '' file; do
    printf '%q\n' "$file"
  done < "$excluded_files"
} > "$manifest_dir/excluded.txt"

{
  while IFS= read -r -d '' file; do
    printf '%q\t%s\n' "$file" \
      "$(git --literal-pathspecs -C "$repo_root" status --porcelain=v1 \
        -- "$file" | head -1)"
  done < "$missing_files"
} > "$manifest_dir/missing.txt"

if [[ "$mode" == "selected" ]]; then
  {
    for selected in "${selected_paths[@]}"; do
      printf '%q\n' "$selected"
    done
  } > "$manifest_dir/selected.txt"
fi

if [[ "$mode" == "diff" ]]; then
  {
    for file in "${safe_changed_paths[@]}"; do
      printf '%q\n' "$file"
    done
  } > "$context_dir/changed-files.txt"
fi

cp "$SCRIPT_DIR/prompt.md" "$request_dir/review-contract.md"
print -r -- "$task_text" > "$request_dir/task.md"
# The review task is operator text that ships inside the archive, so it is
# scanned like any other disclosed file.
scan_context "$request_dir" -- task.md

{
  print -r -- "format_version=1"
  print -r -- "repository=$repo_name"
  print -r -- "branch=$branch"
  print -r -- "head=$head_sha"
  print -r -- "dirty=$dirty"
  print -r -- "mode=$mode"
  print -r -- "base_ref=${resolved_base_ref:-none}"
  print -r -- "base_sha=${resolved_base_sha:-none}"
  print -r -- "candidate_count=$candidate_count"
  print -r -- "included_count=$included_count"
  print -r -- "excluded_count=$excluded_count"
  print -r -- "deleted_count=$deleted_count"
  print -r -- "skipped_submodule_count=$skipped_submodule_count"
  print -r -- "missing_count=$missing_count"
  print -r -- "approved_finding_count=${#approved_findings[@]}"
  integer approved_index=0
  for approved in "${approved_findings[@]}"; do
    (( approved_index += 1 ))
    printf 'approved_finding_%d=%s\n' "$approved_index" "$approved"
  done
} > "$manifest_dir/snapshot.txt"

integer verified_manifest_count=0
while IFS= read -r -d '' file; do
  is_excluded "$file" && fail "excluded path survived manifest filtering: $file"
  [[ -f "$repository_dir/$file" && ! -L "$repository_dir/$file" ]] \
    || fail "manifest file missing from bundle: $file"
  (( verified_manifest_count += 1 ))
done < "$selected_files"
[[ "$verified_manifest_count" -eq "$included_count" ]] || fail "manifest count mismatch"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
context_format=archive
verified_artifact_count="$verified_manifest_count"

if (( plain )); then
  context_format=plain
  if [[ "$mode" == "diff" ]]; then
    artifact="$output_dir/${repo_name}-diff-${timestamp}-$$.patch"
    cp -- "$context_dir/review.patch" "$artifact"
  else
    selected_basename="${selected_paths[1]:t}"
    selected_basename="${selected_basename//[^A-Za-z0-9._-]/-}"
    [[ -n "$selected_basename" ]] || selected_basename=context
    artifact="$output_dir/${repo_name}-selected-${timestamp}-$$-${selected_basename}"
    frozen_selected="$repository_dir/${selected_paths[1]}"
    validate_frozen_regular "$frozen_selected"
    cp -P -- "$frozen_selected" "$artifact"
    source_hash="$(shasum -a 256 "$frozen_selected" | awk '{print $1}')"
    copied_hash="$(shasum -a 256 "$artifact" | awk '{print $1}')"
    [[ "$source_hash" == "$copied_hash" ]] || fail "plain selected copy verification failed"
    validate_frozen_regular "$artifact" "selected --plain final artifact"
  fi
else
  artifact="$output_dir/${repo_name}-${mode}-${timestamp}-$$.tar.zst"
  # Reproducible packaging: the two-phase approval compares a hash of these
  # bytes, so member order and mtimes must not vary between preparations.
  # Without this, prepare printed one hash and the live rerun computed another,
  # and the approval gate could never converge.
  # TZ=UTC because `touch -t` reads the stamp in local time: without it the
  # normalised mtime — and therefore the approved hash — shifts with the
  # environment, which is the same non-convergence in a different disguise.
  TZ=UTC find "$bundle_root" -depth -exec touch -h -t 198001010000 -- {} + \
    || fail "could not normalise bundle timestamps for reproducible packaging"
  bundle_entries="$work_dir/bundle-entries.nul"
  (
    cd "$bundle_root"
    find . -mindepth 1 -print0
  ) | LC_ALL=C sort -z > "$bundle_entries"
  # ustar rather than pax: libarchive's pax writer emits per-run extended
  # headers, which makes the bytes differ between otherwise identical
  # preparations and breaks the approval gate. ustar caps a stored path at 100
  # characters (or 155 + 100 when it can split on a slash), so refuse a bundle
  # it cannot represent instead of writing a broken archive.
  while IFS= read -r -d '' bundle_entry; do
    [[ ${#bundle_entry} -le 100 ]] && continue
    fail "path is too long for reproducible packaging: ${bundle_entry#./}"
  done < "$bundle_entries"
  (
    cd "$bundle_root"
    LC_ALL=C tar -cf - --format=ustar --null --no-recursion \
      --uid 0 --gid 0 --numeric-owner -T "$bundle_entries"
  ) | zstd -q -10 --single-thread -o "$artifact"

  verify_dir="$work_dir/verify"
  mkdir -p -- "$verify_dir"
  zstd -q -dc -- "$artifact" | tar -xf - -C "$verify_dir"

  [[ -f "$verify_dir/manifest/files.nul" ]] || fail "archive verification failed: manifest missing"
  [[ -f "$verify_dir/manifest/excluded.txt" ]] || fail "archive verification failed: exclusion manifest missing"
  verified_archive_count="$(
    verify_repository_payload \
      "$verify_dir/repository" \
      "$verify_dir/manifest/files.nul"
  )"
  [[ "$verified_archive_count" -eq "$included_count" ]] || fail "archive file count mismatch"
  verified_artifact_count="$verified_archive_count"
fi

receipt="${artifact}.receipt.txt"
artifact_bytes="$(wc -c < "$artifact" | tr -d '[:space:]')"
artifact_hash="$(shasum -a 256 "$artifact" | awk '{print $1}')"
source_fingerprint_after="$(source_fingerprint "$work_dir/source-after")"
enforce_source_fingerprint \
  "$source_fingerprint_before" \
  "$source_fingerprint_after" \
  "$output_dir"

{
  print -r -- "format_version=1"
  print -r -- "artifact=$artifact"
  print -r -- "artifact_sha256=$artifact_hash"
  print -r -- "artifact_bytes=$artifact_bytes"
  print -r -- "context_format=$context_format"
  print -r -- "transport=$transport"
  # Always recorded: the destination is part of the disclosure inventory, and
  # --chat-url can point at a specific conversation or project.
  printf 'chat_url=%s\n' "$chat_url"
  print -r -- "repository=$repo_name"
  print -r -- "branch=$branch"
  print -r -- "head=$head_sha"
  print -r -- "source_fingerprint=$source_fingerprint_before"
  print -r -- "dirty=$dirty"
  print -r -- "mode=$mode"
  print -r -- "base_ref=${resolved_base_ref:-none}"
  print -r -- "base_sha=${resolved_base_sha:-none}"
  print -r -- "selected_count=${#selected_paths[@]}"
  integer selected_index=0
  for selected in "${selected_paths[@]}"; do
    (( selected_index += 1 ))
    printf 'selected_%d=%q\n' "$selected_index" "$selected"
  done
  print -r -- "candidate_count=$candidate_count"
  print -r -- "included_count=$included_count"
  print -r -- "excluded_count=$excluded_count"
  integer excluded_index=0
  while IFS= read -r -d '' file; do
    (( excluded_index += 1 ))
    printf 'excluded_%d=%q\n' "$excluded_index" "$file"
  done < "$excluded_files"
  print -r -- "deleted_count=$deleted_count"
  print -r -- "skipped_submodule_count=$skipped_submodule_count"
  print -r -- "missing_count=$missing_count"
  integer missing_index=0
  while IFS= read -r -d '' file; do
    (( missing_index += 1 ))
    printf 'missing_%d=%q\n' "$missing_index" "$file"
  done < "$missing_files"
  print -r -- "approved_finding_count=${#approved_findings[@]}"
  integer receipt_finding_index=0
  for approved in "${approved_findings[@]}"; do
    (( receipt_finding_index += 1 ))
    printf 'approved_finding_%d=%s\n' "$receipt_finding_index" "$approved"
  done
  print -r -- "verified_artifact_count=$verified_artifact_count"
} > "$receipt"

# Attestation the delegate verifies against the artifact digest, so accepting a
# packed archive is not a matter of trusting a command-line flag.
scan_attestation="${artifact}.scan-receipt.txt"
{
  print -r -- "format_version=1"
  print -r -- "artifact_sha256=$artifact_hash"
  print -r -- "scan=clean"
  print -r -- "scanner=browser_tasks.cli scan"
  print -r -- "approved_finding_count=${#approved_findings[@]}"
  integer attested_index=0
  for approved in "${approved_findings[@]}"; do
    (( attested_index += 1 ))
    printf 'approved_finding_%d=%s\n' "$attested_index" "$approved"
  done
} > "$scan_attestation"
chmod 600 "$scan_attestation"
keep_output=1

print -r -- "Prepared: $artifact"
print -r -- "Receipt:  $receipt"
print -r -- "SHA-256:  $artifact_hash"
print -r -- "Files:     $included_count included, $excluded_count excluded, $deleted_count deleted"
print -r -- "Skipped:   $skipped_submodule_count submodules, $missing_count missing"
if [[ "$context_format" == "archive" ]]; then
  print -r -- "Excluded:  manifest/excluded.txt in the archive and receipt"
else
  print -r -- "Excluded:  recorded in the receipt"
fi
print -r -- "Size:      $artifact_bytes bytes"
print -r -- "Transport: Surf UI (user browser)"
print -r -- "Destination: $chat_url"

prompt="$(render_prompt "$task_text" "$artifact_hash" "$artifact_bytes")"
[[ -x "$WEB_CHAT_DELEGATE" ]] \
  || fail "strict web-chat delegate is missing or not executable"
typeset -a delegate_args
delegate_args=(
  --task-id "$task_id"
  --workspace-root "$workspace_root"
  --purpose review
  --reasoning "$reasoning"
  --research "$research_mode"
  --chat-url "$chat_url"
  --task "$prompt"
  --attachment "$artifact"
  # Every packed file was content-scanned above; the delegate verifies this
  # attestation against the artifact digest instead of trusting a flag.
  --scan-receipt "$scan_attestation"
)
[[ -z "$record_run_id" ]] || delegate_args+=(--record-run-id "$record_run_id")
[[ -z "$record_lease_owner" ]] \
  || delegate_args+=(--record-lease-owner "$record_lease_owner")
if (( prepare_only )); then
  "$WEB_CHAT_DELEGATE" "${delegate_args[@]}" --prepare-only
  exit 0
fi
[[ -n "$approved_context_sha" ]] \
  || fail "live submission requires --approved-context-sha from prepare-only"
"$WEB_CHAT_DELEGATE" "${delegate_args[@]}" \
  --approved-context-sha "$approved_context_sha"
