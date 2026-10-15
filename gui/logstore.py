"""Logs, reports, activity audit, retention and upload settings for the GUI.

Everything the tooling writes lands under <repo>/logs (gitignored):

    logs/ansible-<ts>-<label>-<pid>.log   one per run via scripts/run.sh (GUI runs too)
    logs/ansible.log                      catch-all from ansible.cfg (runs outside run.sh)
    logs/gui-jobs/<id>.log|.json          output + metadata of every GUI job
    logs/gui-audit.log                    GUI activity, one JSON object per line
    logs/uploads/<bundle>.manifest.txt    what was sent to Hammerspace (+ .tar.gz if kept)

Reports are listed too (never deleted by retention): snapshots/** from the OS
upgrade check, preflight_report.txt, instance_report.csv, gpu_fabric_data.txt and
.new_instances_limit. Logs from the old run.sh default (~/logs) are listed read-only.

Files are addressed by ids "repo:<relpath>" / "home:<relpath>". resolve() only
accepts paths that match the patterns below and stay inside their root after
symlinks are resolved, so the viewer can't be used to read anything else.
"""

import datetime
import getpass
import json
import os
import re
import shutil
import threading
import time

LOGS = "logs"
JOBS_DIR = "logs/gui-jobs"
AUDIT = "logs/gui-audit.log"
UPLOADS = "logs/uploads"
LEGACY_JOBS_DIR = ".gui-jobs"
ROTATE_BYTES = 20 * 1024 * 1024   # ansible.log / gui-audit.log are rotated past this size

DEFAULT_UPLOAD_URL = "https://v1proto.sc.hammerspace.com/upload/"
SETTINGS_DEFAULTS = {
    "log_retention_days": 365,          # 0 = keep forever; reports are never deleted
    "upload_url": DEFAULT_UPLOAD_URL,   # same intake as HS_PS_Toolkit.sh
    "upload_verify_tls": True,          # the toolkit's curl uses --insecure (False)
    "upload_initials": "",
    "upload_customer": "",
}
ALNUM = re.compile(r"^[A-Za-z0-9]*$")

# (kind, label, regex on the root-relative path)
PATTERNS = {
    "repo": [
        ("ansible", "Ansible run", re.compile(r"^logs/ansible[^/]*\.log$")),
        ("job", "GUI job", re.compile(r"^logs/gui-jobs/[A-Za-z0-9_\-]+\.(log|json)$")),
        ("audit", "GUI activity", re.compile(r"^logs/gui-audit[^/]*\.log$")),
        ("upload", "Upload", re.compile(r"^logs/uploads/[A-Za-z0-9_\-.]+\.(manifest\.txt|tar\.gz)$")),
        ("report", "OS upgrade", re.compile(r"^snapshots/[A-Za-z0-9_.:\-]+/([A-Za-z0-9_.:\-]+/)?"
                                            r"((pre|post)(-[0-9TZ]+)?\.txt|diff\.(txt|html|json)|SUMMARY\.(txt|json))$")),
        ("report", "Preflight", re.compile(r"^(preflight_report\.txt|\.new_instances_limit)$")),
        ("report", "Instance report", re.compile(r"^instance_report\.csv$")),
        ("report", "GPU fabric", re.compile(r"^gpu_fabric_data\.txt$")),
    ],
    "home": [
        ("ansible", "Ansible run (~/logs)", re.compile(r"^ansible[^/]*\.log$")),
    ],
}
# Directories that may be bundled as a whole (OS upgrade runs / hosts).
DIR_PATTERN = re.compile(r"^snapshots/[A-Za-z0-9_.:\-]+(/[A-Za-z0-9_.:\-]+)?$")
PRUNABLE = {"ansible", "job", "audit", "upload"}


class LogError(Exception):
    pass


def roots(repo_root):
    out = {"repo": os.path.realpath(repo_root)}
    home_logs = os.path.realpath(os.path.expanduser("~/logs"))
    if os.path.isdir(home_logs) and home_logs != os.path.join(out["repo"], LOGS):
        out["home"] = home_logs
    return out


def ensure_dirs(repo_root):
    for d in (LOGS, JOBS_DIR, UPLOADS):
        os.makedirs(os.path.join(repo_root, d), exist_ok=True)


def migrate_legacy_jobs(repo_root):
    """Move GUI job history from .gui-jobs/ (before logs/ existed) to logs/gui-jobs/."""
    old = os.path.join(repo_root, LEGACY_JOBS_DIR)
    new = os.path.join(repo_root, JOBS_DIR)
    moved = 0
    if os.path.isdir(old):
        os.makedirs(new, exist_ok=True)
        for f in os.listdir(old):
            src, dst = os.path.join(old, f), os.path.join(new, f)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.move(src, dst)
                moved += 1
        try:
            os.rmdir(old)
        except OSError:
            pass
    return moved


# ----------------------------------------------------------------------------
# Settings (stored in .gui-state.json next to last_inventory)
# ----------------------------------------------------------------------------
def settings_from_state(state):
    s = dict(SETTINGS_DEFAULTS)
    s.update({k: v for k, v in (state.get("logs") or {}).items() if k in SETTINGS_DEFAULTS})
    return s


def validate_settings(body):
    out = {}
    if "log_retention_days" in body:
        try:
            days = int(body["log_retention_days"])
        except (TypeError, ValueError):
            raise LogError("retention must be a whole number of days")
        if not 0 <= days <= 3650:
            raise LogError("retention must be between 0 (keep forever) and 3650 days")
        out["log_retention_days"] = days
    if "upload_url" in body:
        url = str(body["upload_url"]).strip()
        if not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?(/[^\s]*)?$", url):
            raise LogError("upload URL must be an http(s) URL")
        out["upload_url"] = url
    if "upload_verify_tls" in body:
        out["upload_verify_tls"] = bool(body["upload_verify_tls"])
    for k in ("upload_initials", "upload_customer"):
        if k in body:
            v = str(body[k]).strip()
            if not ALNUM.match(v):
                raise LogError("initials and customer name must be letters and numbers only")
            out[k] = v
    return out


# ----------------------------------------------------------------------------
# Audit trail
# ----------------------------------------------------------------------------
class Audit(object):
    def __init__(self, repo_root):
        self.path = os.path.join(repo_root, AUDIT)
        self._lock = threading.Lock()
        try:
            self.user = getpass.getuser()
        except Exception:  # noqa: BLE001
            self.user = "?"

    def log(self, event, **fields):
        rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
               "event": event, "user": self.user}
        rec.update(fields)
        line = json.dumps(rec, default=str, sort_keys=False)
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a") as fh:
                fh.write(line + "\n")

    def tail(self, limit=200):
        try:
            with open(self.path) as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return []
        out = []
        for l in reversed(lines):
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
        return out


# ----------------------------------------------------------------------------
# Catalog
# ----------------------------------------------------------------------------
def _classify(root_key, rel):
    for kind, label, rx in PATTERNS.get(root_key, []):
        if rx.match(rel):
            return kind, label
    return None, None


def _ansible_title(path):
    """First 'command:' header written by scripts/run.sh, if any."""
    try:
        with open(path, errors="replace") as fh:
            for _ in range(4):
                line = fh.readline()
                if line.startswith("# command: "):
                    return line[len("# command: "):].strip()[:160]
    except OSError:
        pass
    name = os.path.basename(path)
    if name == "ansible.log":
        return "runs outside scripts/run.sh (ansible.cfg log_path)"
    return ""


def catalog(repo_root, jobs=None):
    """All logs and reports, newest first."""
    entries = []
    job_meta = {}
    for j in (jobs or []):
        job_meta[j["id"]] = j
    linked = {}   # ansible log rel path -> job id
    for j in job_meta.values():
        al = (j.get("meta") or {}).get("ansible_log")
        if al:
            linked[al] = j["id"]

    for root_key, root in roots(repo_root).items():
        if root_key == "repo":
            scan = [os.path.join(root, LOGS), os.path.join(root, "snapshots")]
            scan_files = [os.path.join(root, f) for f in ("preflight_report.txt", "instance_report.csv",
                                                          "gpu_fabric_data.txt", ".new_instances_limit")]
        else:
            scan, scan_files = [root], []
        paths = [f for f in scan_files if os.path.isfile(f)]
        for base in scan:
            for d, dirs, files in os.walk(base):
                dirs[:] = [x for x in dirs if not x.startswith(".")]
                paths += [os.path.join(d, f) for f in files]
        for p in paths:
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            kind, label = _classify(root_key, rel)
            if not kind:
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            e = {"id": "%s:%s" % (root_key, rel), "kind": kind, "label": label, "name": rel,
                 "size": st.st_size, "mtime": st.st_mtime, "title": ""}
            if kind == "ansible":
                e["title"] = _ansible_title(p)
                if rel in linked:
                    e["job"] = linked[rel]
            elif kind == "job":
                jid = os.path.basename(rel).rsplit(".", 1)[0]
                if rel.endswith(".json"):
                    continue    # metadata shown with the .log entry
                j = job_meta.get(jid, {})
                e.update({"title": j.get("title", jid), "job": jid, "status": j.get("status", "")})
                al = (j.get("meta") or {}).get("ansible_log")
                if al:
                    e["ansible_log"] = "repo:" + al
            elif kind == "report":
                e["title"] = rel
            elif kind == "upload":
                e["title"] = os.path.basename(rel)
            entries.append(e)
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def resolve(repo_root, entry_id, allow_dirs=False):
    """Map an id to an absolute path, or raise LogError. Directories only in snapshots/."""
    root_key, _, rel = str(entry_id).partition(":")
    rts = roots(repo_root)
    if root_key not in rts or not rel or rel.startswith("/") or "\\" in rel:
        raise LogError("unknown log or report")
    root = rts[root_key]
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(root + os.sep):
        raise LogError("unknown log or report")
    real_rel = os.path.relpath(full, root).replace(os.sep, "/")
    if allow_dirs and root_key == "repo" and DIR_PATTERN.match(real_rel) and os.path.isdir(full):
        return full
    kind, _ = _classify(root_key, real_rel)
    if not kind or not os.path.isfile(full):
        raise LogError("unknown log or report")
    return full


def read_chunk(path, offset=0, length=512 * 1024, tail=False):
    size = os.path.getsize(path)
    if tail:
        offset = max(0, size - length)
    offset = max(0, min(int(offset), size))
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(length)
    # Keep chunks on UTF-8 character boundaries so paging never splits a
    # multi-byte character (which would show as U+FFFD at the seams).
    skip = 0
    while offset > 0 and skip < 3 and skip < len(data) and (data[skip] & 0xC0) == 0x80:
        skip += 1
    data, offset = data[skip:], offset + skip
    if offset + len(data) < size:
        for back in range(1, min(4, len(data)) + 1):
            byte = data[-back]
            if byte & 0xC0 == 0x80:
                continue                      # continuation byte, keep looking for the lead byte
            need = 1 if byte < 0x80 else 2 if byte >> 5 == 0x6 else 3 if byte >> 4 == 0xE else 4
            if need > back:
                data = data[:-back]           # incomplete final character: leave it for the next chunk
            break
    return {"text": data.decode("utf-8", errors="replace"), "offset": offset,
            "next": offset + len(data), "size": size, "more_after": offset + len(data) < size,
            "more_before": offset > 0}


# ----------------------------------------------------------------------------
# Retention
# ----------------------------------------------------------------------------
def rotate(repo_root):
    """Rename the two append-only logs once they pass ROTATE_BYTES; retention then
    ages the dated copies out like any other log."""
    rotated = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for rel, new in (("logs/ansible.log", "logs/ansible-catchall-%s.log"),
                     (AUDIT, "logs/gui-audit-%s.log")):
        p = os.path.join(repo_root, rel)
        if os.path.isfile(p) and os.path.getsize(p) > ROTATE_BYTES:
            os.replace(p, os.path.join(repo_root, new % stamp))
            rotated.append(rel)
    return rotated


def prune(repo_root, days, keep_ids=()):
    """Delete logs older than `days` (0 = keep forever). Reports are never deleted.
    keep_ids: entries that must survive (e.g. logs of running jobs)."""
    if not days:
        return {"deleted": 0, "bytes": 0}
    cutoff = time.time() - days * 86400
    deleted, freed = 0, 0
    keep = set(keep_ids)
    for e in catalog(repo_root):
        if e["kind"] not in PRUNABLE or not e["id"].startswith("repo:") or e["id"] in keep:
            continue
        if e["name"] in ("logs/ansible.log", AUDIT):
            continue      # live append files: rotated, not deleted
        if e["mtime"] < cutoff:
            p = os.path.join(repo_root, e["name"])
            targets = [p]
            if e["kind"] == "job":
                targets.append(p[:-len(".log")] + ".json")
            for t in targets:
                try:
                    freed += os.path.getsize(t)
                    os.remove(t)
                    deleted += 1
                except OSError:
                    pass
    return {"deleted": deleted, "bytes": freed}
