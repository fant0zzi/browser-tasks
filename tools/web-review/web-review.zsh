#!/bin/zsh

emulate -LR zsh
setopt errexit nounset pipefail
umask 077

readonly PROGRAM="${0:t}"
readonly SCRIPT_DIR="${0:A:h}"
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
  --task TEXT       Review task sent with the context.
  --task-file FILE  Read the review task from a file.
  --base REF        Diff base. Defaults to origin/HEAD, main, or master.
  --transport KIND  Submission transport: ui (default) or api.
  --chat-url URL    ChatGPT URL for ui transport (or WEB_REVIEW_CHAT_URL).
  --model MODEL     Model for --transport api only.
  --plain           Send a frozen patch (diff) or one regular file (selected).
  --prepare-only    Build and verify the package without uploading it.
  -h, --help        Show this help.

The context file and its receipt are written to a private directory under the
system temporary directory. Upload always requires an interactive confirmation.
This command never applies or imports the answer.
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
  local path part basename
  path="$1"
  basename="${path:t}"

  [[ "$path" == tasks/* ]] && return 0

  for part in ${(s:/:)path}; do
    [[ "$part" == ".git" ]] && return 0
  done

  [[ "$basename" == ".env.example" ]] && return 1
  [[ "$basename" == ".env" || "$basename" == .env.* ]] && return 0
  [[ "$basename" == ".config.yaml" || "$basename" == ".config.yml" ]] && return 0
  [[ "$basename" == *.pem || "$basename" == *.key ]] && return 0
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
        [[ "$?" -eq 1 ]] || return
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
    [[ -L "$payload_root/$file" || -f "$payload_root/$file" ]] \
      || fail "archive verification found missing or unsupported manifest entry: $file"
    expected[$file]=1
    (( expected_count += 1 ))
  done < "$manifest_file"

  entries=("$payload_root"/**/*(DN))
  for entry in "${entries[@]}"; do
    if [[ -d "$entry" && ! -L "$entry" ]]; then
      continue
    fi
    [[ -f "$entry" || -L "$entry" ]] \
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

submit_via_api() {
  local prompt="$1" context_file="$2" model="$3"
  local -a command_args

  command_args=(chatgpt "$prompt" --file "$context_file")
  [[ -n "$model" ]] && command_args+=(--model "$model")
  surf "${command_args[@]}"
}

submit_via_ui() {
  local prompt="$1" context_file="$2" chat_url="$3"
  local tab_output tab_id proxy_output read_output proxy_ref send_ref
  local baseline_output attachment_output submission_output
  local proxy_js baseline_js attachment_js submission_js
  local -a targeted_surf
  integer attempt proxy_ready=0 baseline_ready=0 attachment_ready=0
  integer prompt_sent=0 send_clicked=0

  tab_output="$(surf tab.new "$chat_url")"
  [[ "$tab_output" == "Created tab "*:* ]] \
    || fail "UI transport could not parse the new tab id: $tab_output"
  tab_id="${tab_output#Created tab }"
  tab_id="${tab_id%%:*}"
  [[ "$tab_id" == <-> ]] || fail "UI transport received an invalid tab id: $tab_id"
  targeted_surf=(surf --tab-id "$tab_id")

  proxy_js='
    const target = document.querySelector("#upload-files");
    if (!target) return "TARGET_NOT_FOUND";
    let proxy = document.querySelector("#web-review-upload-proxy");
    if (!proxy) {
      proxy = document.createElement("input");
      proxy.id = "web-review-upload-proxy";
      proxy.type = "file";
      proxy.setAttribute("aria-label", "Web review upload proxy");
      proxy.style.cssText = "position:fixed;left:12px;bottom:12px;z-index:2147483647;width:280px;height:36px;opacity:1;";
      proxy.addEventListener("change", () => {
        const currentTarget = document.querySelector("#upload-files");
        if (!currentTarget) return;
        const expectedName = proxy.files && proxy.files[0]
          ? proxy.files[0].name
          : "";
        document.documentElement.dataset.webReviewExpectedAttachmentName =
          expectedName;
        currentTarget.files = proxy.files;
        currentTarget.dispatchEvent(new Event("change", { bubbles: true }));
      });
      document.body.appendChild(proxy);
    }
    return "READY";
  '
  baseline_js='
    const baseline = document.querySelectorAll(
      "[data-message-author-role=\"user\"]"
    ).length;
    document.documentElement.dataset.webReviewUserMessageBaseline =
      String(baseline);
    return "BASELINE_READY";
  '
  attachment_js='
    const expected =
      document.documentElement.dataset.webReviewExpectedAttachmentName || "";
    if (!expected) return "ATTACHMENT_WAITING";
    const visible = (node) => {
      const style = window.getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.display !== "none" &&
        style.visibility !== "hidden" &&
        rect.width > 0 &&
        rect.height > 0;
    };
    const errorNodes = Array.from(document.querySelectorAll(
      "[role=\"alert\"], [aria-live=\"assertive\"], [data-testid*=\"error\"]"
    )).filter(visible);
    const errorText = errorNodes
      .map((node) => node.innerText || node.textContent || "")
      .join(" ")
      .toLowerCase();
    if (
      /(upload|file|attachment)/.test(errorText) &&
      /(fail|error|reject|unsupported|too large|could not|couldn.t)/.test(errorText)
    ) {
      return "ATTACHMENT_ERROR";
    }
    const nameNode = Array.from(document.querySelectorAll("body *"))
      .filter(visible)
      .find((node) => {
        const text = (node.innerText || node.textContent || "").trim();
        const aria = (node.getAttribute("aria-label") || "").trim();
        const title = (node.getAttribute("title") || "").trim();
        return text === expected || aria === expected || title === expected;
      });
    if (!nameNode) return "ATTACHMENT_WAITING";
    const container = nameNode.closest(
      "[data-testid*=\"attachment\"], [class*=\"attachment\"], li, article, div"
    ) || nameNode.parentElement;
    if (
      container &&
      (
        container.matches("[aria-busy=\"true\"]") ||
        container.querySelector("[aria-busy=\"true\"], [role=\"progressbar\"]")
      )
    ) {
      return "ATTACHMENT_WAITING";
    }
    return "ATTACHMENT_READY";
  '
  submission_js='
    const originReady = window.location.origin === "https://chatgpt.com";
    const pathReady = window.location.pathname.includes("/c/");
    const composer =
      document.querySelector("#prompt-textarea") ||
      document.querySelector("[role=\"textbox\"]");
    if (!composer) return "WAITING";
    const composerValue =
      typeof composer.value === "string"
        ? composer.value
        : (composer.innerText || composer.textContent || "");
    const baseline = Number.parseInt(
      document.documentElement.dataset.webReviewUserMessageBaseline || "",
      10
    );
    const currentUserMessages = document.querySelectorAll(
      "[data-message-author-role=\"user\"]"
    ).length;
    const expected =
      document.documentElement.dataset.webReviewExpectedAttachmentName || "";
    const userMessages = Array.from(document.querySelectorAll(
      "[data-message-author-role=\"user\"]"
    ));
    const newUserMessages = Number.isFinite(baseline)
      ? userMessages.slice(baseline)
      : [];
    const attachmentOwned = expected && newUserMessages.some((message) => {
      return Array.from(message.querySelectorAll("*")).some((node) => {
        const text = (node.innerText || node.textContent || "").trim();
        const aria = (node.getAttribute("aria-label") || "").trim();
        const title = (node.getAttribute("title") || "").trim();
        return text === expected || aria === expected || title === expected;
      });
    });
    const messageAdded =
      Number.isFinite(baseline) &&
      currentUserMessages > baseline &&
      newUserMessages.length > 0;
    if (
      originReady &&
      pathReady &&
      composerValue.trim().length === 0 &&
      messageAdded &&
      attachmentOwned
    ) {
      return "SUBMITTED";
    }
    return "WAITING";
  '

  for attempt in {1..30}; do
    proxy_output="$("${targeted_surf[@]}" js "$proxy_js" 2>/dev/null || true)"
    if [[ "$proxy_output" == *"READY"* ]]; then
      proxy_ready=1
      break
    fi
    "${targeted_surf[@]}" wait 1 >/dev/null
  done
  (( proxy_ready )) || fail "UI transport could not find ChatGPT's upload input in tab $tab_id"

  for attempt in {1..10}; do
    read_output="$("${targeted_surf[@]}" page.read --all 2>/dev/null || true)"
    proxy_ref="$(
      print -r -- "$read_output" \
        | awk '/^[[:space:]]*button "Web review upload proxy" \[e[0-9]+\]/ {
            if (match($0, /\[e[0-9]+\]/)) {
              print substr($0, RSTART + 1, RLENGTH - 2)
              exit
            }
          }'
    )"
    if [[ -n "$proxy_ref" ]]; then
      break
    fi
    "${targeted_surf[@]}" wait 1 >/dev/null
  done
  [[ -n "${proxy_ref:-}" ]] || fail "UI transport could not resolve the upload proxy ref in tab $tab_id"

  baseline_output="$("${targeted_surf[@]}" js "$baseline_js" 2>/dev/null || true)"
  [[ "$baseline_output" == *"BASELINE_READY"* ]] && baseline_ready=1
  (( baseline_ready )) || fail "UI transport could not record the user-message baseline in tab $tab_id"

  "${targeted_surf[@]}" upload --ref "$proxy_ref" --files "$context_file"
  for attempt in {1..120}; do
    attachment_output="$("${targeted_surf[@]}" js "$attachment_js" 2>/dev/null || true)"
    [[ "$attachment_output" == *"ATTACHMENT_ERROR"* ]] \
      && fail "UI transport observed a ChatGPT attachment upload error in tab $tab_id"
    if [[ "$attachment_output" == *"ATTACHMENT_READY"* ]]; then
      attachment_ready=1
      break
    fi
    "${targeted_surf[@]}" wait 1 >/dev/null
  done
  (( attachment_ready )) \
    || fail "UI transport did not observe a completed visible attachment in tab $tab_id"

  "${targeted_surf[@]}" locate.role textbox --name "Chat with ChatGPT" --action fill --value "$prompt"

  for attempt in {1..120}; do
    if (( send_clicked )); then
      submission_output="$("${targeted_surf[@]}" js "$submission_js" 2>/dev/null || true)"
      if [[ "$submission_output" == *"SUBMITTED"* ]]; then
        prompt_sent=1
        break
      fi
    else
      read_output="$("${targeted_surf[@]}" page.read --all 2>/dev/null || true)"
      send_ref="$(
        print -r -- "$read_output" \
          | awk '/^[[:space:]]*button "Send prompt" \[e[0-9]+\]/ && $0 !~ /\[disabled\]/ {
              if (match($0, /\[e[0-9]+\]/)) {
                print substr($0, RSTART + 1, RLENGTH - 2)
                exit
              }
            }'
      )"
      if [[ -n "$send_ref" ]]; then
        "${targeted_surf[@]}" click "$send_ref" >/dev/null 2>&1 \
          || fail "UI transport could not click enabled Send prompt in tab $tab_id"
        send_clicked=1
      fi
    fi
    "${targeted_surf[@]}" wait 1 >/dev/null
  done
  (( prompt_sent )) \
    || fail "UI transport did not observe a new user message owning the expected attachment, /c/ URL, and empty composer after ${send_clicked} Send prompt click in tab $tab_id; retry manually"

  print -r -- "Submitted in ChatGPT tab: $tab_id"
  print -r -- "Chat URL: $chat_url"
  print -r -- "Monitor the response in that browser tab; the harness will not capture or import it."
}

submit_to_web_chat() {
  local prompt="$1" context_file="$2"

  case "$transport" in
    ui) submit_via_ui "$prompt" "$context_file" "$chat_url" ;;
    api) submit_via_api "$prompt" "$context_file" "$model" ;;
  esac
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

task_text=""
task_file=""
base_ref=""
model=""
transport=ui
chat_url="${WEB_REVIEW_CHAT_URL:-https://chatgpt.com}"
prepare_only=0
plain=0
typeset -a selected_paths
selected_paths=()

while [[ $# -gt 0 ]]; do
  case "$1" in
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
      [[ $# -ge 2 ]] || fail "--model requires a value"
      model="$2"
      shift 2
      ;;
    --transport)
      [[ $# -ge 2 ]] || fail "--transport requires a value"
      transport="$2"
      shift 2
      ;;
    --chat-url)
      [[ $# -ge 2 ]] || fail "--chat-url requires a value"
      chat_url="$2"
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
  [[ -f "$task_file" ]] || fail "task file not found: $task_file"
  task_text="$(<"$task_file")"
fi
[[ -n "${task_text//[[:space:]]/}" ]] || fail "a non-empty --task or --task-file is required"

[[ "$transport" == "ui" || "$transport" == "api" ]] \
  || fail "--transport must be 'ui' or 'api'"
[[ -z "$model" || "$transport" == "api" ]] || fail "--model is valid only with --transport api"
if [[ "$transport" == "ui" ]]; then
  [[ "$chat_url" == "https://chatgpt.com" || "$chat_url" == "https://chatgpt.com/"* ]] \
    || fail "UI chat URL must use the https://chatgpt.com origin"
  [[ "$chat_url" != *[[:space:]]* ]] || fail "UI chat URL must not contain whitespace"
fi
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

for command_name in git tar zstd shasum awk cat cp chmod mkdir mktemp rm wc tr date; do
  require_command "$command_name"
done

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
: > "$all_candidates"
: > "$selected_files"
: > "$excluded_files"

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
  if [[ ! -f "$repo_root/$file" && ! -L "$repo_root/$file" ]]; then
    if [[ "$mode" == "diff" && -n "${deleted_paths[$file]-}" ]]; then
      continue
    fi
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
} > "$manifest_dir/snapshot.txt"

integer verified_manifest_count=0
while IFS= read -r -d '' file; do
  is_excluded "$file" && fail "excluded path survived manifest filtering: $file"
  [[ -f "$repository_dir/$file" || -L "$repository_dir/$file" ]] \
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
  tar -cf - -C "$bundle_root" . | zstd -q -10 -T0 -o "$artifact"

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
  [[ "$transport" == "ui" ]] && printf 'chat_url=%q\n' "$chat_url"
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
  print -r -- "verified_artifact_count=$verified_artifact_count"
} > "$receipt"
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
print -r -- "Transport: $transport"
[[ "$transport" == "ui" ]] && print -r -- "Destination: $chat_url"

if (( prepare_only )); then
  exit 0
fi

[[ -t 0 ]] || fail "upload requires an interactive terminal; rerun there or use --prepare-only"
print -u2 -r -- ""
print -u2 -r -- "This will upload the prepared context file to ChatGPT through Surf."
read -q "reply?Continue? [y/N] "
print -u2 -r -- ""
[[ "$reply" == [yY] ]] || {
  print -u2 -r -- "Upload cancelled; prepared files were kept."
  exit 0
}

require_command surf
prompt="$(render_prompt "$task_text" "$artifact_hash" "$artifact_bytes")"
submit_to_web_chat "$prompt" "$artifact"
