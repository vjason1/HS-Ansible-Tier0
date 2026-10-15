#!/usr/bin/env bash
# Wrapper for ansible-playbook / ansible-inventory that forces a UTF-8 locale.
#
# Background: Ansible refuses to start when the system locale isn't UTF-8 (the
# Korean ko_KR incident on dskbd079). This wrapper finds a UTF-8 locale that is
# actually installed on the host and exports it before invoking ansible.
#
# Tries (in order): en_US.UTF-8, C.UTF-8, en_US.utf8, C.utf8. If none is
# present, prints a clear instruction for generating one and exits non-zero
# instead of letting ansible bomb with a cryptic error.
#
# Usage:
#   ./scripts/run.sh playbook site.yml
#   ./scripts/run.sh playbook site.yml -e deploy_di=true --tags di
#   ./scripts/run.sh inventory -i inventory.yml --list
#   ./scripts/run.sh raw ansible-playbook site.yml      # pass any ansible-* command

set -euo pipefail

# locale -a output normalizes casing — match case-insensitively.
_available_locales="$(locale -a 2>/dev/null || true)"
_picked=""
for cand in en_US.UTF-8 C.UTF-8 en_US.utf8 C.utf8; do
    if printf '%s\n' "${_available_locales}" | grep -qiE "^${cand}$"; then
        _picked="${cand}"
        break
    fi
done

if [[ -z "${_picked}" ]]; then
    cat >&2 <<EOF
ERROR: no UTF-8 locale is installed on this host. ansible cannot start.

Generate one:
  # Debian / Ubuntu
  sudo locale-gen en_US.UTF-8
  # RHEL / Rocky / Alma
  sudo dnf install -y glibc-langpack-en
  # Or fall back to C.UTF-8 (universal):
  sudo localedef -i en_US -f UTF-8 en_US.UTF-8

Available locales on this host:
${_available_locales:-  (none)}
EOF
    exit 1
fi

export LANG="${_picked}"
export LC_ALL="${_picked}"
export LANGUAGE="${_picked}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd)"

# ANSIBLE_LOG_PATH: every run gets its own log file so nothing is lost and the
# configuration GUI (Operate -> Logs & Reports) can list it.
#   default : <repo>/logs/ansible-<YYYYmmdd_HHMMSS>-<playbook>-<pid>.log
#   override: ANSIBLE_LOG_DIR=/dir   (same file name, other directory)
#             ANSIBLE_LOG_PATH=/file (used as-is; the user's value always wins)
# Shell aliases (e.g. `alias ansible-playbook='ANSIBLE_LOG_PATH=...'`) do NOT
# propagate through `exec`, which is why this is set here.
# Runs that bypass this wrapper are still logged: ansible.cfg sets
# log_path = logs/ansible.log as the catch-all.
_label="${2:-}"
_label="$(basename -- "${_label%.yml}" 2>/dev/null)"
_label="${_label//[^A-Za-z0-9_.-]/_}"
_label="${_label:0:40}"
if [[ -z "${ANSIBLE_LOG_PATH:-}" ]]; then
    _log_dir="${ANSIBLE_LOG_DIR:-${REPO_ROOT}/logs}"
    mkdir -p "${_log_dir}" 2>/dev/null || true
    # Declare-then-export (SC2155): $(...) failure must not be hidden by export's exit code.
    _log_file="${_log_dir}/ansible-$(date +%Y%m%d_%H%M%S)-${1:-run}${_label:+-${_label}}-$$.log"
    export ANSIBLE_LOG_PATH="${_log_file}"
fi
mkdir -p "$(dirname -- "${ANSIBLE_LOG_PATH}")" 2>/dev/null || true

# Header: who ran what, from where. Values of password/secret/token-like
# arguments (key=value, key: value, "key":"value") are masked so the log never
# records them; each argument is masked before it is shell-quoted.
_mask_arg() {
    printf '%s' "$1" | sed -E 's/(([A-Za-z0-9_.-]*(pass|password|passwd|passphrase|secret|token|api_?key|access_?key|secret_?key|private_?key|credentials?))"?[[:space:]]*[:=][[:space:]]*"?)[^",}[:space:]]+/\1********/Ig'
}
_masked_args=""
for _a in "$@"; do
    _masked_args+="$(printf '%q' "$(_mask_arg "${_a}")") "
done
{
    printf '# ===== run.sh %s  user=%s  host=%s  cwd=%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S %z')" "$(id -un 2>/dev/null || echo "?")" \
        "$(hostname 2>/dev/null || echo "?")" "${REPO_ROOT}"
    printf '# command: %s\n' "${_masked_args% }"
} >> "${ANSIBLE_LOG_PATH}" 2>/dev/null || true
echo "[run.sh] locale=${_picked}  ANSIBLE_LOG_PATH=${ANSIBLE_LOG_PATH}" >&2

cd "${REPO_ROOT}"

if [[ $# -lt 1 ]]; then
    cat <<EOF
Usage: $0 <subcommand> [args...]

Subcommands:
  playbook   <args>   -> ansible-playbook <args>
  inventory  <args>   -> ansible-inventory <args>
  vault      <args>   -> ansible-vault <args>
  raw        <cmd>    -> exec <cmd> (full ansible-* command)

Active locale     : ${_picked}
ANSIBLE_LOG_PATH  : ${ANSIBLE_LOG_PATH}

Logs go to <repo>/logs/ (one file per run; viewable in the GUI under Logs & Reports).
Tip: override the log path with ANSIBLE_LOG_PATH=/your/path ./scripts/run.sh ...
     or change the directory with ANSIBLE_LOG_DIR=/your/dir ./scripts/run.sh ...
EOF
    exit 1
fi

sub="$1"
shift

case "${sub}" in
    playbook)   exec ansible-playbook "$@" ;;
    inventory)  exec ansible-inventory "$@" ;;
    vault)      exec ansible-vault "$@" ;;
    raw)        exec "$@" ;;
    *)
        echo "Unknown subcommand: ${sub}" >&2
        exit 2
        ;;
esac
