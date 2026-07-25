#!/bin/zsh

emulate -LR zsh
setopt errexit nounset pipefail
umask 077
export WEB_REVIEW_TEST_MODE=1

readonly SCRIPT_DIR="${0:A:h}"
readonly HARNESS="$SCRIPT_DIR/web-review.zsh"
readonly SMOKE_TASK_ID="web-review-smoke"
readonly SENTINEL="FORBIDDEN_SENTINEL_WEB_REVIEW_7f91a3"
readonly HOSTILE_PATH=':(glob)**'
readonly REAL_TAR_PATH="$(command -v tar)"
readonly REAL_CP_PATH="$(command -v cp)"
smoke_tmp_root="${TMPDIR:-/tmp}"
readonly SMOKE_TMP_ROOT="${smoke_tmp_root:A}"
unset smoke_tmp_root

smoke_workspace_root="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-workspace.XXXXXX")"
export WEB_REVIEW_WORKSPACE_ROOT="$smoke_workspace_root"
PYTHONPATH="${SCRIPT_DIR:h:h}/src${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m browser_tasks.cli --root "$smoke_workspace_root" \
  task init "$SMOKE_TASK_ID" \
  --goal "Exercise the repository review packer against fixtures" >/dev/null

fixture="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-smoke.XXXXXX")"
deletion_fixture="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-deletions.XXXXXX")"
rename_fixture="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-renames.XXXXXX")"
overlap_fixture="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-overlap.XXXXXX")"
inventory_fixture="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-inventory.XXXXXX")"
mutation_fixture="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-mutation.XXXXXX")"
artifact_fixture="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-artifact.XXXXXX")"
mutation_temp="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-mutation-temp.XXXXXX")"
artifact_temp="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-artifact-temp.XXXXXX")"
unpacked="${fixture}.unpacked"
fake_bin="${fixture}.bin"
mutation_bin="${mutation_fixture}.bin"
artifact_bin="${artifact_fixture}.bin"
mutation_marker="${mutation_fixture}.tar-fired"
cleanup_carrier=""
cleanup_sentinel=""
cleanup_symlink=""
typeset -a artifacts weird_paths
artifacts=()
weird_paths=(
  "space name.txt"
  "-leading.txt"
  "[bracket].txt"
  "юникод.txt"
  $'line\nbreak.txt'
)

cleanup() {
  local artifact artifact_dir cleanup_path
  for cleanup_path in \
    "$fixture" "$deletion_fixture" "$rename_fixture" "$overlap_fixture" \
    "$inventory_fixture" "$mutation_fixture" "$artifact_fixture" \
    "$mutation_temp" "$artifact_temp" "$unpacked" "$fake_bin" \
    "$mutation_bin" "$artifact_bin" "$mutation_marker" \
    "$smoke_workspace_root" \
    "$cleanup_carrier" "$cleanup_sentinel" "$cleanup_symlink"; do
    [[ -n "$cleanup_path" ]] && rm -rf -- "$cleanup_path"
  done
  for artifact in "${artifacts[@]}"; do
    artifact_dir="${artifact:h}"
    if [[ "${artifact_dir:h}" == "$SMOKE_TMP_ROOT" \
      && "${artifact_dir:t}" == web-review-output.* ]]; then
      rm -rf -- "$artifact_dir"
    else
      rm -f -- "$artifact" "${artifact}.receipt.txt"
    fi
  done
}
trap cleanup EXIT INT TERM

die() {
  print -u2 -r -- "smoke: $*"
  exit 1
}

prepared_artifact() {
  local output="$1" line
  for line in ${(f)output}; do
    [[ "$line" == "Prepared: "* ]] && {
      print -r -- "${line#Prepared: }"
      return
    }
  done
  return 1
}

populate_fake_bin() {
  local target_bin="$1" omitted="${2:-}" command_name
  mkdir -p -- "$target_bin"
  for command_name in git tar zstd shasum awk cat cp chmod mkdir mktemp rm wc \
    tr date sed base64 find touch sort head grep python3; do
    [[ "$command_name" == "$omitted" ]] && continue
    ln -s -- "$(command -v "$command_name")" "$target_bin/$command_name"
  done
}

assert_clean_patch() {
  local patch="$1" content
  content="$(<"$patch")"
  [[ "$content" != *"$SENTINEL"* ]] \
    || die "sentinel leaked into $patch"
  [[ "$content" != *".env"* ]] \
    || die "forbidden filename leaked into $patch"
}

file_mode() {
  # BSD and GNU stat disagree; the suite claims Linux support, so detect.
  if stat -f '%Lp' "$1" 2>/dev/null; then
    return 0
  fi
  stat -c '%a' "$1"
}

assert_private_output() {
  local artifact="$1" checked_file mode
  [[ "$(file_mode "${artifact:h}")" == "700" ]] \
    || die "output directory is not mode 700"
  for checked_file in "$artifact" "${artifact}.receipt.txt"; do
    mode="$(file_mode "$checked_file")"
    [[ "$mode" == *00 ]] \
      || die "group/other permissions present on $checked_file ($mode)"
  done
  grep -Eq '^source_fingerprint=[0-9a-f]+$' "${artifact}.receipt.txt" \
    || die "receipt is missing source fingerprint"
  grep -Eq '^base_ref=.+$' "${artifact}.receipt.txt" \
    && grep -Eq '^base_sha=[0-9a-f]+$' "${artifact}.receipt.txt" \
    || die "receipt is missing immutable diff base metadata"
}

git -C "$fixture" init -q
git -C "$fixture" config user.name "Web Review Smoke"
git -C "$fixture" config user.email "web-review-smoke@example.invalid"
print -r -- "safe baseline" > "$fixture/safe.txt"
print -r -- "excluded baseline" > "$fixture/.env"
print -r -- "hostile baseline" > "$fixture/$HOSTILE_PATH"
mkdir -p -- "$fixture/dir"
print -r -- "nested baseline" > "$fixture/dir/file.txt"
for weird_path in "${weird_paths[@]}"; do
  print -r -- "weird baseline: ${(qqq)weird_path}" > "$fixture/$weird_path"
done
git -C "$fixture" add safe.txt dir/file.txt
git -C "$fixture" add -f .env
git --literal-pathspecs -C "$fixture" add -- "$HOSTILE_PATH" "${weird_paths[@]}"
git -C "$fixture" commit -qm "test: create fixture"

print -r -- "$SENTINEL" > "$fixture/.env"
if forbidden_only_output="$(
  cd "$fixture"
  "$HARNESS" diff --task-id "$SMOKE_TASK_ID" --base HEAD \
    --task "Forbidden-only smoke." --prepare-only 2>&1
)"; then
  die "forbidden-only diff unexpectedly succeeded"
fi
[[ "$forbidden_only_output" == *"diff contains only excluded paths"* ]] \
  || die "forbidden-only diff did not fail clearly"

print -r -- "safe changed" > "$fixture/safe.txt"
print -r -- "hostile changed" > "$fixture/$HOSTILE_PATH"
for weird_path in "${weird_paths[@]}"; do
  print -r -- "weird changed: ${(qqq)weird_path}" > "$fixture/$weird_path"
done
archive_output="$(
  cd "$fixture"
  "$HARNESS" diff --task-id "$SMOKE_TASK_ID" --base HEAD \
    --task "Regression smoke." --prepare-only
)"
archive="$(prepared_artifact "$archive_output")"
artifacts+=("$archive")
assert_private_output "$archive"

mkdir -p -- "$unpacked"
zstd -q -dc -- "$archive" | tar -xf - -C "$unpacked"
assert_clean_patch "$unpacked/context/review.patch"
[[ "$(<"$unpacked/context/review.patch")" == *"hostile changed"* ]] \
  || die "literal hostile path change is missing from patch"
[[ ! -e "$unpacked/repository/.env" ]] \
  || die "forbidden file entered repository payload"
[[ "$(<"$unpacked/manifest/excluded.txt")" == ".env" ]] \
  || die "exclusion manifest did not record .env"
for weird_path in "${weird_paths[@]}"; do
  [[ -f "$unpacked/repository/$weird_path" ]] \
    || die "weird legal filename was not preserved: ${(qqq)weird_path}"
done

WEB_REVIEW_TEST_MODE=1 "$HARNESS" __verify-payload \
  "$unpacked/repository" "$unpacked/manifest/files.nul"
print -r -- "extra" > "$unpacked/repository/unmanifested-extra.txt"
if WEB_REVIEW_TEST_MODE=1 "$HARNESS" __verify-payload \
  "$unpacked/repository" "$unpacked/manifest/files.nul" >/dev/null 2>&1; then
  die "payload verifier accepted an unmanifested file"
fi
rm -f -- "$unpacked/repository/unmanifested-extra.txt"
print -r -- "excluded" > "$unpacked/repository/.env"
if WEB_REVIEW_TEST_MODE=1 "$HARNESS" __verify-payload \
  "$unpacked/repository" "$unpacked/manifest/files.nul" >/dev/null 2>&1; then
  die "payload verifier accepted an excluded file"
fi
rm -f -- "$unpacked/repository/.env"

plain_output="$(
  cd "$fixture"
  "$HARNESS" diff --task-id "$SMOKE_TASK_ID" --base HEAD \
    --task "Regression smoke." --plain --prepare-only
)"
plain_patch="$(prepared_artifact "$plain_output")"
artifacts+=("$plain_patch")
assert_private_output "$plain_patch"
assert_clean_patch "$plain_patch"

selected_plain_output="$(
  cd "$fixture"
  "$HARNESS" selected --task-id "$SMOKE_TASK_ID" \
    --task "Selected normalization smoke." \
    --plain --prepare-only -- dir/./file.txt
)"
selected_plain="$(prepared_artifact "$selected_plain_output")"
artifacts+=("$selected_plain")
[[ "$(<"$selected_plain")" == "nested baseline" ]] \
  || die "selected --plain did not preserve normalized file contents"
grep -q '^selected_1=dir/file.txt$' "${selected_plain}.receipt.txt" \
  || die "selected redundant ./ component was not normalized"

ln -s -- "$fixture/safe.txt" "$fixture/frozen-symlink"
if WEB_REVIEW_TEST_MODE=1 "$HARNESS" \
  __validate-frozen-regular "$fixture/frozen-symlink" \
  >/dev/null 2>&1; then
  die "frozen selected validation accepted a symlink"
fi

if fingerprint_output="$(
  WEB_REVIEW_TEST_MODE=1 "$HARNESS" __test-fingerprint-mismatch 2>/dev/null
)"; then
  die "fingerprint mismatch unexpectedly succeeded"
fi
[[ ! -e "$fingerprint_output" ]] \
  || die "fingerprint mismatch did not discard its private output"

cleanup_carrier="$(
  mktemp -d "$SMOKE_TMP_ROOT/web-review-output.traversal.XXXXXX"
)"
cleanup_sentinel="$(
  mktemp -d "$SMOKE_TMP_ROOT/web-review-cleanup-sentinel.XXXXXX"
)"
print -r -- "must survive" > "$cleanup_sentinel/sentinel"
traversal_candidate="$cleanup_carrier/../${cleanup_sentinel:t}"
if WEB_REVIEW_TEST_MODE=1 "$HARNESS" \
  __validate-output-dir "$traversal_candidate" \
  >/dev/null 2>&1; then
  die "private output validation accepted a traversal target"
fi
[[ "$(<"$cleanup_sentinel/sentinel")" == "must survive" ]] \
  || die "traversal validation altered its sentinel target"

cleanup_symlink="$cleanup_carrier.link"
ln -s -- "$cleanup_sentinel" "$cleanup_symlink"
if WEB_REVIEW_TEST_MODE=1 "$HARNESS" \
  __validate-output-dir "$cleanup_symlink" >/dev/null 2>&1; then
  die "private output validation accepted a symlink"
fi
[[ "$(<"$cleanup_sentinel/sentinel")" == "must survive" ]] \
  || die "symlink validation altered its sentinel target"

if private_command_output="$(
  WEB_REVIEW_TEST_MODE=0 "$HARNESS" __validate-output-dir "$cleanup_carrier" 2>&1
)"; then
  die "ordinary CLI invocation accepted a private test command"
fi
[[ "$private_command_output" == \
  *"private test commands require WEB_REVIEW_TEST_MODE=1"* ]] \
  || die "private test command rejection was not clear"

PATH=/nonexistent "$HARNESS" --help >/dev/null \
  || die "--help required external commands"
if (
  cd "$fixture"
  "$HARNESS" selected \
    --task-id 20260724-120000-review \
    --task "Timestamp ids must be rejected." \
    --plain --prepare-only -- safe.txt >/dev/null 2>&1
); then
  die "timestamp-shaped review task id unexpectedly succeeded"
fi
populate_fake_bin "$fake_bin"
no_surf_output="$(
  cd "$fixture"
  PATH="$fake_bin" "$HARNESS" selected --task-id "$SMOKE_TASK_ID" \
    --task "Prepare without Surf." --plain --prepare-only -- safe.txt
)"
no_surf_artifact="$(prepared_artifact "$no_surf_output")"
artifacts+=("$no_surf_artifact")
[[ -f "$no_surf_artifact" ]] \
  || die "prepare-only failed without Surf on PATH"

if ui_model_output="$(
  cd "$fixture"
  "$HARNESS" diff --task-id "$SMOKE_TASK_ID" --base HEAD \
    --task "Transport validation smoke." \
    --model smoke-model --prepare-only 2>&1
)"; then
  die "UI transport unexpectedly accepted --model"
fi
[[ "$ui_model_output" == *"--model was removed"* ]] \
  || die "UI transport model rejection was not clear"

git -C "$deletion_fixture" init -q
git -C "$deletion_fixture" config user.name "Web Review Smoke"
git -C "$deletion_fixture" config user.email "web-review-smoke@example.invalid"
for deleted_path in committed-delete.txt staged-delete.txt unstaged-delete.txt; do
  print -r -- "delete me: $deleted_path" > "$deletion_fixture/$deleted_path"
done
git -C "$deletion_fixture" add .
git -C "$deletion_fixture" commit -qm "test: add deletion fixtures"
deletion_base="$(git -C "$deletion_fixture" rev-parse HEAD)"

rm -f -- "$deletion_fixture/committed-delete.txt"
git -C "$deletion_fixture" add -u
git -C "$deletion_fixture" commit -qm "test: committed deletion"
rm -f -- "$deletion_fixture/staged-delete.txt"
git -C "$deletion_fixture" add -u
rm -f -- "$deletion_fixture/unstaged-delete.txt"

deletion_output="$(
  cd "$deletion_fixture"
  "$HARNESS" diff --task-id "$SMOKE_TASK_ID" --base "$deletion_base" \
    --task "Three-layer deletion smoke." --prepare-only
)"
deletion_archive="$(prepared_artifact "$deletion_output")"
artifacts+=("$deletion_archive")
assert_private_output "$deletion_archive"
grep -q '^deleted_count=3$' "${deletion_archive}.receipt.txt" \
  || die "deleted paths were not deduplicated across all three layers"
grep -q '^skipped_submodule_count=0$' "${deletion_archive}.receipt.txt" \
  || die "unexpected skipped submodule count"
grep -q '^missing_count=0$' "${deletion_archive}.receipt.txt" \
  || die "deleted paths were incorrectly counted as missing"
[[ "$deletion_output" == *"Files:     0 included, 0 excluded, 3 deleted"* ]] \
  || die "deletion summary did not report the expected count"

rm -rf -- "$unpacked"
mkdir -p -- "$unpacked"
zstd -q -dc -- "$deletion_archive" | tar -xf - -C "$unpacked"
for deleted_path in committed-delete.txt staged-delete.txt unstaged-delete.txt; do
  grep -q -- "$deleted_path" "$unpacked/context/review.patch" \
    || die "deletion patch is missing $deleted_path"
done
grep -q '^deleted_count=3$' "$unpacked/manifest/snapshot.txt" \
  || die "archive snapshot is missing deletion metadata"

git -C "$rename_fixture" init -q
git -C "$rename_fixture" config user.name "Web Review Smoke"
git -C "$rename_fixture" config user.email "web-review-smoke@example.invalid"
print -r -- "safe rename baseline" > "$rename_fixture/rename-safe.txt"
git -C "$rename_fixture" add rename-safe.txt
git -C "$rename_fixture" commit -qm "test: add rename fixture"
git -C "$rename_fixture" mv rename-safe.txt .config.yaml
print -r -- "$SENTINEL" > "$rename_fixture/.config.yaml"

rename_output="$(
  cd "$rename_fixture"
  "$HARNESS" diff --task-id "$SMOKE_TASK_ID" --base HEAD \
    --task "Safe-to-excluded rename smoke." --prepare-only
)"
rename_archive="$(prepared_artifact "$rename_output")"
artifacts+=("$rename_archive")
grep -q '^deleted_count=1$' "${rename_archive}.receipt.txt" \
  || die "safe side of safe-to-excluded rename was not counted as deleted"
grep -q '^excluded_count=1$' "${rename_archive}.receipt.txt" \
  || die "excluded side of safe-to-excluded rename was not recorded"

rm -rf -- "$unpacked"
mkdir -p -- "$unpacked"
zstd -q -dc -- "$rename_archive" | tar -xf - -C "$unpacked"
grep -q -- 'rename-safe.txt' "$unpacked/context/review.patch" \
  || die "safe side of safe-to-excluded rename is missing from patch"
assert_clean_patch "$unpacked/context/review.patch"
[[ ! -e "$unpacked/repository/.config.yaml" ]] \
  || die "excluded side of safe-to-excluded rename entered payload"

git -C "$overlap_fixture" init -q
git -C "$overlap_fixture" config user.name "Web Review Smoke"
git -C "$overlap_fixture" config user.email "web-review-smoke@example.invalid"
print -r -- "overlap baseline" > "$overlap_fixture/overlap.txt"
git -C "$overlap_fixture" add overlap.txt
git -C "$overlap_fixture" commit -qm "test: add overlap fixture"
overlap_base="$(git -C "$overlap_fixture" rev-parse HEAD)"
rm -f -- "$overlap_fixture/overlap.txt"
git -C "$overlap_fixture" add -u
git -C "$overlap_fixture" commit -qm "test: committed overlap deletion"
git -C "$overlap_fixture" show "$overlap_base:overlap.txt" \
  > "$overlap_fixture/overlap.txt"
git -C "$overlap_fixture" add overlap.txt
rm -f -- "$overlap_fixture/overlap.txt"

overlap_output="$(
  cd "$overlap_fixture"
  "$HARNESS" diff --task-id "$SMOKE_TASK_ID" --base "$overlap_base" \
    --task "Overlapping deletion smoke." --prepare-only
)"
overlap_archive="$(prepared_artifact "$overlap_output")"
artifacts+=("$overlap_archive")
grep -q '^deleted_count=1$' "${overlap_archive}.receipt.txt" \
  || die "same deletion across committed and unstaged layers was not deduplicated"
grep -q '^missing_count=0$' "${overlap_archive}.receipt.txt" \
  || die "overlapping deletion was incorrectly counted as missing"

git -C "$inventory_fixture" init -q
git -C "$inventory_fixture" config user.name "Web Review Smoke"
git -C "$inventory_fixture" config user.email "web-review-smoke@example.invalid"
print -r -- "replace baseline" > "$inventory_fixture/replace-me"
print -r -- "keep baseline" > "$inventory_fixture/keep.txt"
git -C "$inventory_fixture" add replace-me keep.txt
git -C "$inventory_fixture" commit -qm "test: add inventory fixture"
inventory_head="$(git -C "$inventory_fixture" rev-parse HEAD)"
git -C "$inventory_fixture" update-index \
  --add --cacheinfo "160000,$inventory_head,synthetic-submodule"
rm -f -- "$inventory_fixture/replace-me"
mkdir -p -- "$inventory_fixture/replace-me"
print -r -- "directory child" > "$inventory_fixture/replace-me/inside.txt"

inventory_output="$(
  cd "$inventory_fixture"
  "$HARNESS" repo --task-id "$SMOKE_TASK_ID" \
    --task "Inventory counter smoke." --prepare-only
)"
inventory_archive="$(prepared_artifact "$inventory_output")"
artifacts+=("$inventory_archive")
grep -q '^skipped_submodule_count=1$' "${inventory_archive}.receipt.txt" \
  || die "synthetic gitlink was not counted as a skipped submodule"
grep -q '^missing_count=1$' "${inventory_archive}.receipt.txt" \
  || die "tracked file replaced by a directory was not counted as missing"

git -C "$mutation_fixture" init -q
git -C "$mutation_fixture" config user.name "Web Review Smoke"
git -C "$mutation_fixture" config user.email "web-review-smoke@example.invalid"
print -r -- "mutation baseline" > "$mutation_fixture/tracked.txt"
git -C "$mutation_fixture" add tracked.txt
git -C "$mutation_fixture" commit -qm "test: add mutation fixture"
populate_fake_bin "$mutation_bin" tar
print -r -- '#!/bin/zsh
if [[ ! -e "$WEB_REVIEW_SMOKE_TAR_MARKER" ]]; then
  print -r -- "mutated during packaging" >> "$WEB_REVIEW_SMOKE_MUTATE"
  : > "$WEB_REVIEW_SMOKE_TAR_MARKER"
fi
exec "$WEB_REVIEW_SMOKE_REAL_TAR" "$@"
' > "$mutation_bin/tar"
chmod +x "$mutation_bin/tar"
if mutation_output="$(
  cd "$mutation_fixture"
  PATH="$mutation_bin" \
  TMPDIR="$mutation_temp" \
  WEB_REVIEW_SMOKE_REAL_TAR="$REAL_TAR_PATH" \
  WEB_REVIEW_SMOKE_MUTATE="$mutation_fixture/tracked.txt" \
  WEB_REVIEW_SMOKE_TAR_MARKER="$mutation_marker" \
    "$HARNESS" repo --task-id "$SMOKE_TASK_ID" \
      --task "Mutation cleanup smoke." --prepare-only 2>&1
)"; then
  die "repository mutation during packaging unexpectedly succeeded"
fi
[[ "$mutation_output" == \
  *"repository changed while context was being prepared"* ]] \
  || die "repository mutation did not fail through the fingerprint guard"
mutation_outputs=("$mutation_temp"/web-review-output.*(N))
[[ ${#mutation_outputs[@]} -eq 0 ]] \
  || die "fingerprint failure left a private output directory behind"

git -C "$artifact_fixture" init -q
git -C "$artifact_fixture" config user.name "Web Review Smoke"
git -C "$artifact_fixture" config user.email "web-review-smoke@example.invalid"
print -r -- "artifact baseline" > "$artifact_fixture/selected.txt"
git -C "$artifact_fixture" add selected.txt
git -C "$artifact_fixture" commit -qm "test: add artifact fixture"
populate_fake_bin "$artifact_bin" cp
print -r -- '#!/bin/zsh
"$WEB_REVIEW_SMOKE_REAL_CP" "$@"
destination="${@[-1]}"
if [[ "$destination" == "$WEB_REVIEW_SMOKE_OUTPUT_ROOT"/web-review-output.*/* ]]; then
  /bin/rm -f -- "$destination"
  /bin/ln -s -- "$WEB_REVIEW_SMOKE_CP_LINK_TARGET" "$destination"
fi
' > "$artifact_bin/cp"
chmod +x "$artifact_bin/cp"
if artifact_output="$(
  cd "$artifact_fixture"
  PATH="$artifact_bin" \
  TMPDIR="$artifact_temp" \
  WEB_REVIEW_SMOKE_REAL_CP="$REAL_CP_PATH" \
  WEB_REVIEW_SMOKE_OUTPUT_ROOT="$artifact_temp" \
  WEB_REVIEW_SMOKE_CP_LINK_TARGET="$artifact_fixture/selected.txt" \
    "$HARNESS" selected --task-id "$SMOKE_TASK_ID" \
      --task "Final artifact type smoke." \
      --plain --prepare-only -- selected.txt 2>&1
)"; then
  die "selected --plain accepted a final symlink artifact"
fi
[[ "$artifact_output" == \
  *"selected --plain final artifact is not a regular non-symlink file"* ]] \
  || die "selected --plain final artifact rejection was not clear"
artifact_outputs=("$artifact_temp"/web-review-output.*(N))
[[ ${#artifact_outputs[@]} -eq 0 ]] \
  || die "final artifact rejection left a private output directory behind"

delegate_context_sha() {
  local output="$1" line
  for line in ${(f)output}; do
    [[ "$line" == "Context SHA-256: "* ]] && {
      print -r -- "${line#Context SHA-256: }"
      return
    }
  done
  return 1
}

# The two-phase approval must converge: prepare prints a hash, and the live
# rerun has to compute the same one. It could not before the archive was made
# reproducible, so the documented workflow never terminated.
gate_fixture="$(mktemp -d "$SMOKE_TMP_ROOT/web-review-gate.XXXXXX")"
gate_bin="${gate_fixture}.bin"
gate_log="${gate_fixture}.surf.log"
git -C "$gate_fixture" init -q
git -C "$gate_fixture" config user.email smoke@example.test
git -C "$gate_fixture" config user.name "Smoke Runner"
print -r -- "module contents" > "$gate_fixture/module.py"
git -C "$gate_fixture" add module.py
git -C "$gate_fixture" commit -qm "test: gate fixture"
populate_fake_bin "$gate_bin"
print -r -- '#!/bin/zsh
print -r -- "$*" >> "$WEB_REVIEW_SMOKE_SURF_LOG"
if [[ "$1" == "tab.new" ]]; then
  print -r -- "Created tab 7: ChatGPT"
  exit 0
fi
if [[ "$1" == "--tab-id" ]]; then
  shift 2
fi
case "$1" in
  wait|click|upload|key) exit 0 ;;
  page.read)
    print -r -- '"'"'button "High" [e1]'"'"'
    print -r -- '"'"'button "Send prompt" [e2]'"'"'
    print -r -- '"'"'button "Browser Tasks upload proxy" [e3]'"'"'
    exit 0
    ;;
  locate.text)
    [[ "$2" == "Max" ]] && exit 1
    exit 0
    ;;
  locate.role) exit 0 ;;
  js)
    code="$2"
    if [[ "$code" == *"CHATGPT_READY"* ]]; then
      print -r -- "CHATGPT_READY"
    elif [[ "$code" == *"PILL_NOT_FOUND"* ]]; then
      print -r -- "PILL:High"
    elif [[ "$code" == *"PROXY_READY"* ]]; then
      print -r -- "PROXY_READY"
    elif [[ "$code" == *"UPLOAD_READY"* ]]; then
      print -r -- "UPLOAD_READY"
    elif [[ "$code" == *"BASELINE_READY"* ]]; then
      print -r -- "BASELINE_READY"
    elif [[ "$code" == *"STANDARD_RESEARCH_ACTIVE"* ]]; then
      print -r -- "STANDARD_RESEARCH_ACTIVE"
    elif [[ "$code" == *"SUBMITTED"* ]]; then
      print -r -- "SUBMITTED"
    elif [[ "$code" == *"TextEncoder"* ]]; then
      print -r -- "COMPLETE:cmV2aWV3IHJlc3BvbnNl"
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
' > "$gate_bin/surf"
chmod +x "$gate_bin/surf"

typeset -a gate_common
gate_common=(
  repo
  --task-id "$SMOKE_TASK_ID"
  --task "Gate convergence smoke."
)
first_gate="$(cd "$gate_fixture" && "$HARNESS" "${gate_common[@]}" --prepare-only)"
second_gate="$(cd "$gate_fixture" && "$HARNESS" "${gate_common[@]}" --prepare-only)"
artifacts+=("$(prepared_artifact "$first_gate")" "$(prepared_artifact "$second_gate")")
first_gate_sha="$(delegate_context_sha "$first_gate")"
second_gate_sha="$(delegate_context_sha "$second_gate")"
[[ -n "$first_gate_sha" && "$first_gate_sha" == "$second_gate_sha" ]] \
  || die "archive-mode context hash is not reproducible across preparations"

gate_run="$(
  PYTHONPATH="${SCRIPT_DIR:h:h}/src${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m browser_tasks.cli --root "$smoke_workspace_root" \
    task run-start "$SMOKE_TASK_ID" --lease-owner smoke \
    | sed -n 's/.*"run_id": "\([^"]*\)".*/\1/p'
)"
[[ -n "$gate_run" ]] || die "could not start a run for the live gate check"
live_gate="$(
  cd "$gate_fixture"
  PATH="$gate_bin" WEB_REVIEW_SMOKE_SURF_LOG="$gate_log" \
    WEB_CHAT_SMOKE_LOG="$gate_log" \
    "$HARNESS" "${gate_common[@]}" \
      --record-run-id "$gate_run" --record-lease-owner smoke \
      --approved-context-sha "$first_gate_sha"
)"
artifacts+=("$(prepared_artifact "$live_gate")")
[[ "$live_gate" == *"Completed ChatGPT Web delegation"* ]] \
  || die "live submission did not accept the approved context hash"

# A symlink must be excluded rather than packaged, and every omission named.
ln -s -- /etc/hosts "$gate_fixture/link.txt"
print -r -- "kept contents" > "$gate_fixture/keeper.py"
git -C "$gate_fixture" add link.txt keeper.py
git -C "$gate_fixture" commit -qm "test: add symlink and keeper"
rm -f -- "$gate_fixture/module.py"
symlink_output="$(cd "$gate_fixture" && "$HARNESS" "${gate_common[@]}" --prepare-only)"
symlink_artifact="$(prepared_artifact "$symlink_output")"
artifacts+=("$symlink_artifact")
symlink_unpacked="${gate_fixture}.unpacked"
mkdir -p -- "$symlink_unpacked"
zstd -q -dc -- "$symlink_artifact" | tar -xf - -C "$symlink_unpacked"
[[ ! -e "$symlink_unpacked/repository/link.txt" ]] \
  || die "symlink entered the review payload"
grep -q 'link.txt' "$symlink_unpacked/manifest/excluded.txt" \
  || die "excluded symlink was not recorded"
grep -q 'module.py' "$symlink_unpacked/manifest/missing.txt" \
  || die "missing tracked path was not named in the manifest"
grep -q '^missing_1=module.py$' "${symlink_artifact}.receipt.txt" \
  || die "missing tracked path was not named in the receipt"

# A secret under a benign filename must block packaging until it is approved.
print -r -- 'api_key = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"' > "$gate_fixture/config.py"
git -C "$gate_fixture" add config.py
git -C "$gate_fixture" commit -qm "test: add secret"
if secret_output="$(
  cd "$gate_fixture"
  "$HARNESS" "${gate_common[@]}" --prepare-only 2>&1
)"; then
  die "context carrying a secret was packaged without approval"
fi
[[ "$secret_output" == *"unapproved disclosure finding: api_token config.py"* ]] \
  || die "disclosure scan did not name the finding"
approved_output="$(
  cd "$gate_fixture"
  "$HARNESS" "${gate_common[@]}" --prepare-only \
    --allow-finding "config.py:api_token"
)"
approved_artifact="$(prepared_artifact "$approved_output")"
artifacts+=("$approved_artifact")
grep -q '^approved_finding_1=config.py:api_token$' \
  "${approved_artifact}.receipt.txt" \
  || die "approved finding was not recorded in the receipt"

rm -rf -- "$gate_fixture" "$gate_bin" "$gate_log" "$symlink_unpacked"

print -r -- "web-review smoke: PASS"
