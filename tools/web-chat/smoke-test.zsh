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
  wait|click|upload)
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

typeset -a common
common=(
  --task-id 20260724-120000-smoke
  --purpose research
  --reasoning best
  --research deep
  --task "Investigate a complex question with sources."
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

live="$(
  PATH="$fake_bin:$PATH" WEB_CHAT_SMOKE_LOG="$surf_log" \
    WEB_CHAT_SMOKE_RESEARCH=deep \
    "$HARNESS" "${common[@]}" --approved-context-sha "$first_sha"
)"
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
  --task-id 20260724-120000-smoke
  --purpose research
  --reasoning best
  --research standard
  --task "Investigate a focused question with sources."
)
standard_prepared="$("$HARNESS" "${standard_common[@]}" --prepare-only)"
standard_sha="$(context_sha "$standard_prepared")"
standard_live="$(
  PATH="$fake_bin:$PATH" WEB_CHAT_SMOKE_LOG="$standard_log" \
    WEB_CHAT_SMOKE_RESEARCH=standard \
    "$HARNESS" "${standard_common[@]}" --approved-context-sha "$standard_sha"
)"
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

print -r -- "web-chat smoke: PASS"
