#!/usr/bin/env bash
# Unit test for the locale-fallback logic in scripts/run.sh.
#
# Verifies:
#   1. Picks the first candidate that exists in `locale -a`.
#   2. Falls through en_US.UTF-8 → C.UTF-8 → en_US.utf8 → C.utf8.
#   3. Errors out cleanly when none are present.
#
# Approach: replays just the picker block with a mocked locale list.
# Independent of the host's real locale state.

set -euo pipefail

passes=0
failures=()

# Helper: run the picker with a mocked `locale -a` output, return the chosen locale.
pick_locale() {
    local mocked_locales="$1"
    local _picked=""
    for cand in en_US.UTF-8 C.UTF-8 en_US.utf8 C.utf8; do
        if printf '%s\n' "${mocked_locales}" | grep -qiE "^${cand}$"; then
            _picked="${cand}"
            break
        fi
    done
    echo "${_picked}"
}

run_case() {
    local name="$1"; shift
    local mock="$1"; shift
    local expected="$1"; shift
    local got
    got="$(pick_locale "${mock}")"
    if [[ "${got}" == "${expected}" ]]; then
        passes=$((passes+1))
        printf '  ✓ %s\n' "${name}"
    else
        failures+=("${name}: expected '${expected}', got '${got}'")
        printf '  ✗ %s\n' "${name}"
    fi
}

printf '%s\n' "RUN.SH locale fallback unit tests"
printf '%s\n' "================================="

# 1. Normal host — has en_US.UTF-8
run_case "en_US.UTF-8 present → picks it first" \
    "en_US.UTF-8
C.UTF-8
ko_KR.UTF-8
POSIX" \
    "en_US.UTF-8"

# 2. en_US.UTF-8 missing, C.UTF-8 present (typical minimal container)
run_case "no en_US.UTF-8 → falls back to C.UTF-8" \
    "C
C.UTF-8
POSIX" \
    "C.UTF-8"

# 3. Lowercase variants (some distros)
run_case "lowercase en_US.utf8 → picks it after upper-case misses" \
    "C
en_US.utf8
ko_KR.utf8" \
    "en_US.utf8"

# 4. Only C.utf8 (lowercase, some old systems)
run_case "only C.utf8 → picks it last in chain" \
    "C
POSIX
C.utf8" \
    "C.utf8"

# 5. Pure Korean host (real-world Keith scenario)
run_case "ko_KR-only host → returns empty (forces error path)" \
    "C
POSIX
ko_KR.UTF-8" \
    ""

# 6. Empty locale list (broken libc install)
run_case "empty locale list → returns empty" \
    "" \
    ""

# 7. Case insensitivity: en_us.utf-8 should still match en_US.UTF-8 (grep -i)
run_case "en_us.utf-8 lowercase variant matches en_US.UTF-8" \
    "en_us.utf-8" \
    "en_US.UTF-8"

# 8. Multiple candidates available — picks first in priority order
run_case "all four available → picks en_US.UTF-8 (highest priority)" \
    "C.UTF-8
en_US.UTF-8
en_US.utf8
C.utf8" \
    "en_US.UTF-8"

# ============================================================================
# ANSIBLE_LOG_PATH default behavior
# ============================================================================

echo
printf '%s\n' "RUN.SH ANSIBLE_LOG_PATH default tests"
printf '%s\n' "====================================="

# These cases run the REAL scripts/run.sh (subcommand `raw`, which execs the
# given command with run.sh's environment), so a regression in run.sh itself is
# caught — not a copy of its logic.
RUN_SH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." &> /dev/null && pwd)/scripts/run.sh"
REPO_LOGS="$(dirname -- "$(dirname -- "${RUN_SH}")")/logs"

check() {   # check <name> <condition-result 0/1> [detail]
    if [[ "$2" == "0" ]]; then
        passes=$((passes+1)); printf '  ✓ %s\n' "$1"
    else
        failures+=("$1${3:+: $3}"); printf '  ✗ %s\n' "$1"
    fi
}
log_path_of() {   # env assignments..., prints the ANSIBLE_LOG_PATH run.sh chose
    # shellcheck disable=SC2016  # expanded by the inner sh, on purpose
    env -u ANSIBLE_LOG_PATH -u ANSIBLE_LOG_DIR "$@" \
        bash "${RUN_SH}" raw sh -c 'printf %s "$ANSIBLE_LOG_PATH"' 2>/dev/null
}

tmp="$(mktemp -d)"

# 1. no preset -> <repo>/logs/ansible-<ts>-raw-sh-<pid>.log, header written
got="$(log_path_of)"
if [[ "$(dirname -- "${got}")" == "${REPO_LOGS}" && "$(basename -- "${got}")" =~ ^ansible-[0-9]{8}_[0-9]{6}-raw-sh-[0-9]+\.log$ ]]; then rc=0; else rc=1; fi
check "no preset → <repo>/logs/ansible-<ts>-<label>-<pid>.log" "${rc}" "got ${got}"
grep -q '^# ===== run.sh ' "${got}" 2>/dev/null
rc=$?
check "header line written to the log" "${rc}"
rm -f "${got}"

# 2. ANSIBLE_LOG_DIR override
got="$(log_path_of ANSIBLE_LOG_DIR="${tmp}/x")"
if [[ "$(dirname -- "${got}")" == "${tmp}/x" && -f "${got}" ]]; then rc=0; else rc=1; fi
check "ANSIBLE_LOG_DIR → that dir (created)" "${rc}" "got ${got}"

# 3. user-set ANSIBLE_LOG_PATH wins
got="$(log_path_of ANSIBLE_LOG_PATH="${tmp}/custom/path.log")"
if [[ "${got}" == "${tmp}/custom/path.log" ]]; then rc=0; else rc=1; fi
check "user-set ANSIBLE_LOG_PATH preserved" "${rc}" "got ${got}"

# 4. ANSIBLE_LOG_PATH wins over ANSIBLE_LOG_DIR
got="$(log_path_of ANSIBLE_LOG_PATH="${tmp}/abs.log" ANSIBLE_LOG_DIR="${tmp}/ignored")"
if [[ "${got}" == "${tmp}/abs.log" ]]; then rc=0; else rc=1; fi
check "ANSIBLE_LOG_PATH overrides ANSIBLE_LOG_DIR" "${rc}" "got ${got}"

# 5. two runs in the same second never share a file (PID in the name)
a="$(log_path_of ANSIBLE_LOG_DIR="${tmp}/same")"; b="$(log_path_of ANSIBLE_LOG_DIR="${tmp}/same")"
if [[ "${a}" != "${b}" ]]; then rc=0; else rc=1; fi
check "concurrent runs get distinct files" "${rc}"

# 6. secrets in arguments are masked in the header
env -u ANSIBLE_LOG_DIR ANSIBLE_LOG_PATH="${tmp}/mask.log" bash "${RUN_SH}" raw true \
    -e 'ansible_become_password=Sup3rS3cret' -e '{"vault_password": "V4ult", "keep": "visible"}' \
    -e 'api_token:T0ken' >/dev/null 2>&1
! grep -qE 'Sup3rS3cret|V4ult|T0ken' "${tmp}/mask.log" && grep -q 'visible' "${tmp}/mask.log"
rc=$?
check "password/token values masked, other values kept" "${rc}" "$(sed -n 2p "${tmp}/mask.log" 2>/dev/null)"

rm -rf "${tmp}"

echo
echo "================================="
echo "PASSED: ${passes}"
if [[ ${#failures[@]} -eq 0 ]]; then
    echo "FAILED: 0"
    exit 0
fi
echo "FAILED: ${#failures[@]}"
for f in "${failures[@]}"; do
    echo "  - ${f}"
done
exit 1
