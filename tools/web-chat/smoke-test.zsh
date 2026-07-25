#!/bin/zsh

emulate -LR zsh
setopt errexit nounset pipefail
umask 077

readonly SCRIPT_DIR="${0:A:h}"
readonly HARNESS="$SCRIPT_DIR/web-chat.zsh"
smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/web-chat-smoke.XXXXXX")"
fake_bin="$smoke_root/bin"
surf_log="$smoke_root/surf.log"
mkdir -p "$fake_bin"

cleanup() {
  rm -rf -- "$smoke_root"
}
trap cleanup EXIT INT TERM

die() {
  print -u2 -r -- "web-chat smoke: $*"
  exit 1
}

context_sha() {
  local output="$1" line
  for line in ${(f)output}; do
    [[ "$line" == "Context SHA-256: "* ]] && {
      print -r -- "${line#Context SHA-256: }"
      return
    }
  done
  return 1
}

response_path() {
  local output="$1" line
  for line in ${(f)output}; do
    [[ "$line" == "Response: "* ]] && {
      print -r -- "$line" | sed 's/^Response:[[:space:]]*//'
      return
    }
  done
  return 1
}

receipt_path() {
  local output="$1" line
  for line in ${(f)output}; do
    [[ "$line" == "Receipt: "* ]] && {
      print -r -- "$line" | sed 's/^Receipt:[[:space:]]*//'
      return
    }
  done
  return 1
}

cat > "$fake_bin/surf" <<'EOF'
#!/bin/zsh
print -r -- "$*" >> "$WEB_CHAT_SMOKE_LOG"
if [[ "$1" == "tab.new" ]]; then
  print -r -- "Created tab 42: ChatGPT"
  exit 0
fi
if [[ "$1" == "--tab-id" ]]; then
  shift 2
fi
case "$1" in
  wait|click|upload|key)
    exit 0
    ;;
  page.read)
    print -r -- 'button "High" [e1]'
    print -r -- 'button "Send prompt" [e2]'
    print -r -- 'button "Browser Tasks upload proxy" [e3]'
    exit 0
    ;;
  locate.text)
    [[ "$2" == "Max" ]] && exit 1
    exit 0
    ;;
  locate.role)
    exit 0
    ;;
  js)
    code="$2"
    if [[ "$code" == *"CHATGPT_READY"* ]]; then
      print -r -- "CHATGPT_READY"
    elif [[ "$code" == *"DEEP_RESEARCH_ACTIVE"* &&
            "$code" == *"STANDARD_RESEARCH_ACTIVE"* ]]; then
      if [[ "${WEB_CHAT_SMOKE_RESEARCH:-standard}" == deep ]]; then
        print -r -- "DEEP_RESEARCH_ACTIVE"
      else
        print -r -- "STANDARD_RESEARCH_ACTIVE"
      fi
    elif [[ "$code" == *"PILL_NOT_FOUND"* ]]; then
      print -r -- "PILL:${WEB_CHAT_SMOKE_PILL:-High}"
    elif [[ "$code" == *"BASELINE_READY"* ]]; then
      print -r -- "BASELINE_READY"
    elif [[ "$code" == *"SUBMITTED"* ]]; then
      print -r -- "SUBMITTED"
    elif [[ "$code" == *"TextEncoder"* ]]; then
      print -r -- "COMPLETE:ZGVsZWdhdGUgcmVzcG9uc2U="
    elif [[ "$code" == *"window.location.href"* ]]; then
      print -r -- "https://chatgpt.com/c/fake"
    else
      print -r -- "READY"
    fi
    exit 0
    ;;
esac
print -u2 -r -- "unexpected fake Surf command: $*"
exit 1
EOF
chmod +x "$fake_bin/surf"

"$HARNESS" --help >/dev/null

# The guard is a real precondition now, so the suite needs a real workspace.
workspace_root="$smoke_root/workspace"
mkdir -p "$workspace_root"
browser_tasks_cli() {
  PYTHONPATH="${SCRIPT_DIR:h:h}/src${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m browser_tasks.cli "$@"
}
browser_tasks_cli --root "$workspace_root" task init web-chat-smoke \
  --goal "Exercise the web-chat delegate against a fake Surf" >/dev/null

smoke_run_start() {
  browser_tasks_cli --root "$workspace_root" task run-start web-chat-smoke \
    --lease-owner smoke | sed -n 's/.*"run_id": "\([^"]*\)".*/\1/p'
}

smoke_run_finish() {
  browser_tasks_cli --root "$workspace_root" task run-finish web-chat-smoke \
    "$1" --lease-owner smoke --state SUCCEEDED >/dev/null
}

for legacy in 20260724-120000-smoke 20260724-120000; do
  if "$HARNESS" \
    --task-id "$legacy" \
    --workspace-root "$workspace_root" \
    --task "Timestamp ids must be rejected." \
    --prepare-only >/dev/null 2>&1; then
    die "timestamp-shaped task id unexpectedly succeeded: $legacy"
  fi
done

if "$HARNESS" \
  --task-id no-such-workspace \
  --workspace-root "$workspace_root" \
  --task "The guard must deny an unknown workspace." \
  --prepare-only >/dev/null 2>&1; then
  die "unknown workspace unexpectedly passed the guard"
fi

browser_tasks_cli --root "$workspace_root" task archive web-chat-smoke >/dev/null
if "$HARNESS" \
  --task-id web-chat-smoke \
  --workspace-root "$workspace_root" \
  --task "The guard must deny an archived workspace." \
  --prepare-only >/dev/null 2>&1; then
  die "archived workspace unexpectedly passed the guard"
fi
browser_tasks_cli --root "$workspace_root" task restore web-chat-smoke >/dev/null

secret_dir="$smoke_root/secret"
mkdir -p "$secret_dir"
print -r -- 'api_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"' > "$secret_dir/notes.md"
if "$HARNESS" \
  --task-id web-chat-smoke \
  --workspace-root "$workspace_root" \
  --task "A secret in a benign filename must be refused." \
  --attachment "$secret_dir/notes.md" \
  --prepare-only >/dev/null 2>&1; then
  die "attachment carrying a secret was not refused"
fi

typeset -a common
common=(
  --task-id web-chat-smoke
  --workspace-root "$workspace_root"
  --purpose research
  --reasoning best
  --research deep
  --task "Investigate a complex question with sources."
  --keep
)

first="$("$HARNESS" "${common[@]}" --prepare-only)"
second="$("$HARNESS" "${common[@]}" --prepare-only)"
first_sha="$(context_sha "$first")"
second_sha="$(context_sha "$second")"
[[ "$first_sha" == "$second_sha" ]] \
  || die "prepare-only context hash is not deterministic"

if PATH="$fake_bin:$PATH" WEB_CHAT_SMOKE_LOG="$surf_log" \
  "$HARNESS" "${common[@]}" --approved-context-sha wrong \
  >/dev/null 2>&1; then
  die "mismatched disclosure hash unexpectedly submitted"
fi
[[ ! -e "$surf_log" ]] \
  || die "Surf was invoked before disclosure hash validation"

if PATH="$fake_bin:$PATH" WEB_CHAT_SMOKE_LOG="$surf_log" \
  "$HARNESS" "${common[@]}" --approved-context-sha "$first_sha" \
  >/dev/null 2>&1; then
  die "live submission without --record-run-id unexpectedly succeeded"
fi
[[ ! -e "$surf_log" ]] \
  || die "Surf was invoked before the recording precondition was checked"

deep_run="$(smoke_run_start)"
[[ -n "$deep_run" ]] || die "could not start a run for the deep delegation"
live="$(
  PATH="$fake_bin:$PATH" WEB_CHAT_SMOKE_LOG="$surf_log" \
    WEB_CHAT_SMOKE_RESEARCH=deep \
    "$HARNESS" "${common[@]}" --record-run-id "$deep_run" --record-lease-owner smoke \
      --approved-context-sha "$first_sha"
)"
smoke_run_finish "$deep_run"
[[ "$live" == *"Completed ChatGPT Web delegation"* ]] \
  || die "fake Surf delegation did not complete"
response="$(response_path "$live")"
[[ -f "$response" && "$(<"$response")" == "delegate response" ]] \
  || die "captured response is missing or incorrect"
receipt="$(receipt_path "$live")"
grep -q '^requested_research_mode=deep$' "$receipt" \
  || die "receipt does not record requested Deep Research"
grep -q '^verified_research_mode=deep$' "$receipt" \
  || die "receipt does not record verified Deep Research"
grep -q '^tab.new https://chatgpt.com$' "$surf_log" \
  || die "delegation did not create a dedicated ChatGPT tab"
grep -q 'locate.text Max --exact --action click' "$surf_log" \
  || die "best reasoning did not try Max first"
grep -q 'locate.text High --exact --action click' "$surf_log" \
  || die "best reasoning did not select the strongest available level"
grep -q 'locate.text Deep research --exact --action click' "$surf_log" \
  || die "Deep Research was not selected"
fill_line="$(
  grep -n 'locate.role textbox --name Chat with ChatGPT --action fill' "$surf_log" \
    | tail -1 | cut -d: -f1
)"
deep_line="$(
  grep -n 'locate.text Deep research --exact --action click' "$surf_log" \
    | tail -1 | cut -d: -f1
)"
mode_line="$(
  grep -n 'DEEP_RESEARCH_ACTIVE' "$surf_log" | tail -1 | cut -d: -f1
)"
send_line="$(
  grep -n '^click e2 ' "$surf_log" | tail -1 | cut -d: -f1
)"
[[ -n "$fill_line" && -n "$deep_line" && -n "$mode_line" && -n "$send_line" ]] \
  || die "Deep Research ordering evidence is incomplete"
(( fill_line < deep_line && deep_line < mode_line && mode_line < send_line )) \
  || die "Deep Research was not selected and verified after prompt fill"
if grep -Eq '(^| )chatgpt( |$)|--model|api' "$surf_log"; then
  die "forbidden API or model transport appeared in Surf calls"
fi

standard_log="$smoke_root/surf-standard.log"
typeset -a standard_common
standard_common=(
  --task-id web-chat-smoke
  --workspace-root "$workspace_root"
  --purpose research
  --reasoning best
  --research standard
  --task "Investigate a focused question with sources."
  --keep
)
standard_prepared="$("$HARNESS" "${standard_common[@]}" --prepare-only)"
standard_sha="$(context_sha "$standard_prepared")"
standard_run="$(smoke_run_start)"
standard_live="$(
  PATH="$fake_bin:$PATH" WEB_CHAT_SMOKE_LOG="$standard_log" \
    WEB_CHAT_SMOKE_RESEARCH=standard \
    "$HARNESS" "${standard_common[@]}" --record-run-id "$standard_run" --record-lease-owner smoke \
      --approved-context-sha "$standard_sha"
)"
smoke_run_finish "$standard_run"
[[ "$standard_live" == *"Completed ChatGPT Web delegation"* ]] \
  || die "standard research delegation did not complete"
standard_receipt="$(receipt_path "$standard_live")"
grep -q '^requested_research_mode=standard$' "$standard_receipt" \
  || die "receipt does not record requested standard research"
grep -q '^verified_research_mode=standard$' "$standard_receipt" \
  || die "receipt does not record verified standard research"
if grep -q 'locate.text Deep research --exact --action click' "$standard_log"; then
  die "standard research unexpectedly selected Deep Research"
fi
grep -q 'STANDARD_RESEARCH_ACTIVE' "$standard_log" \
  || die "standard research mode was not verified before submission"
grep -q '^surf_tab_id=42$' "$standard_receipt" \
  || die "receipt does not record the Surf tab id"

# The response and the receipt must be able to land inside the workspace
# instead of living only in the system temp directory.
record_run="$(smoke_run_start)"
[[ -n "$record_run" ]] || die "could not start a run for delegation recording"
recorded_log="$smoke_root/surf-recorded.log"
recorded_sha="$(
  context_sha "$("$HARNESS" "${standard_common[@]}" --prepare-only)"
)"
recorded="$(
  PATH="$fake_bin:$PATH" WEB_CHAT_SMOKE_LOG="$recorded_log" \
    WEB_CHAT_SMOKE_RESEARCH=standard \
    "$HARNESS" "${standard_common[@]}" \
      --record-run-id "$record_run" --record-lease-owner smoke \
      --approved-context-sha "$recorded_sha"
)"
[[ "$recorded" == *"Completed ChatGPT Web delegation"* ]] \
  || die "recorded delegation did not complete"
browser_tasks_cli --root "$workspace_root" task show web-chat-smoke --json \
  | grep -q '"category": "receipts"' \
  || die "delegation receipt was not stored in the workspace"
browser_tasks_cli --root "$workspace_root" task show web-chat-smoke --json \
  | grep -q '"category": "delegations"' \
  || die "delegation response was not stored in the workspace"
browser_tasks_cli --root "$workspace_root" task audit web-chat-smoke \
  | grep -q 'delegation.recorded' \
  || die "delegation was not recorded in the audit log"

print -r -- "web-chat smoke: PASS"
