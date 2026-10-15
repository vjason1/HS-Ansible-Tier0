#!/usr/bin/env python3
"""Tier 0 Configuration GUI — a local web UI for this Ansible repository.

    python3 gui/tier0_gui.py                 # http://127.0.0.1:8765, prints a login URL
    python3 gui/tier0_gui.py --port 9000 --no-browser
    python3 gui/tier0_gui.py --read-only     # browse/validate only; no saves, no runs

From a workstation, reach a headless controller through SSH:
    ssh -L 8765:127.0.0.1:8765 user@controller   then open the printed URL locally.

What it does
  * One menu per configuration area, each bound to ONE file (vars/main.yml split into
    topic menus, vars/vault.yml, ansible.cfg, inventory.yml, the cloud inventories).
  * Saves are surgical: only the lines of the changed keys are rewritten, every other
    line and comment stays byte-identical, the result is re-parsed and verified, and a
    timestamped backup goes to .gui-backups/ first. A diff preview is shown before saving.
  * Runs playbooks / utility scripts as background jobs with live output.
  * OS Upgrade Check: guided pre/post snapshot + diff (os_upgrade_snapshot.yml).

Security: binds to 127.0.0.1 by default and every API call needs the random token in
the printed URL. Only files in an allow-list can be edited; only catalogued playbooks
and scripts can be run, always as argv lists (never through a shell).

Requirements: Python 3.6+ and PyYAML (installed with Ansible). No internet access and
no third-party web assets are needed, so it works on dark-site controllers.
"""

import argparse
import configparser
import difflib
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import yaml  # noqa: E402
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip3 install pyyaml  (it is normally installed with Ansible)")

import catalog  # noqa: E402
import logstore  # noqa: E402
import schema  # noqa: E402
from jobs import JobManager  # noqa: E402
from surgeon import (UNSET, SurgeryError, YamlSurgeon, _IndentedDumper,  # noqa: E402
                     apply_ini_changes, apply_yaml_changes)

VERSION = "1.0.0"
MAX_BODY = 4 * 1024 * 1024
BACKUPS_KEPT = 30
SECRET_TYPES = {"secret"}
SECRET_LINE = re.compile(r"^([+\- ]\s*)((?:vault_\w+|di_k8s_registry_password)\s*:\s*)(.+)$")


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super(ApiError, self).__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# ===========================================================================
# Repository access
# ===========================================================================
class Repo(object):
    def __init__(self, root, read_only=False):
        self.root = os.path.abspath(root)
        self.read_only = read_only
        self.backup_dir = os.path.join(self.root, ".gui-backups")
        self.snapshot_root = os.path.join(self.root, "snapshots")
        self._write_lock = threading.Lock()

    # -- paths ---------------------------------------------------------------
    def path(self, rel):
        full = os.path.abspath(os.path.join(self.root, rel))
        if not (full == self.root or full.startswith(self.root + os.sep)):
            raise ApiError(400, "path escapes the repository: %s" % rel)
        return full

    def editable(self, rel):
        if rel not in schema.EDITABLE_FILES:
            raise ApiError(403, "%s is not an editable configuration file" % rel)
        return self.path(rel)

    # -- read / write ----------------------------------------------------------
    def read(self, rel, must_exist=True):
        full = self.path(rel)
        if not os.path.exists(full):
            if must_exist:
                raise ApiError(404, "%s does not exist" % rel)
            return "", sha("")
        with open(full, "r", encoding="utf-8") as fh:
            text = fh.read()
        return text, sha(text)

    def write(self, rel, text, base_hash=None):
        if self.read_only:
            raise ApiError(403, "the GUI was started with --read-only")
        full = self.editable(rel)
        with self._write_lock:
            current, cur_hash = self.read(rel, must_exist=False)
            if base_hash and base_hash != cur_hash:
                raise ApiError(409, "%s changed on disk since you loaded it. Reload the page to "
                                    "see the current version." % rel)
            backup = None
            if os.path.exists(full):
                os.makedirs(self.backup_dir, mode=0o700, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                base = rel.replace("/", "__")
                backup = os.path.join(self.backup_dir, "%s.%s" % (base, stamp))
                shutil.copy2(full, backup)
                self._prune_backups(base)
            mode = os.stat(full).st_mode & 0o777 if os.path.exists(full) else 0o644
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(full), prefix=".gui-tmp-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.chmod(tmp, mode)
                os.replace(tmp, full)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        return sha(text), (os.path.relpath(backup, self.root) if backup else None)

    def _prune_backups(self, base):
        files = sorted(f for f in os.listdir(self.backup_dir) if f.startswith(base + "."))
        for old in files[:-BACKUPS_KEPT]:
            try:
                os.unlink(os.path.join(self.backup_dir, old))
            except OSError:
                pass

    def inventory_files(self):
        out = []
        for name in sorted(os.listdir(self.root)):
            if name.startswith("inventory") and name.endswith((".yml", ".yaml", ".ini")):
                out.append(name)
        return out

    def git_info(self):
        def git(*args):
            try:
                return subprocess.check_output(["git", "-C", self.root] + list(args),
                                               stderr=subprocess.DEVNULL, timeout=5).decode().strip()
            except Exception:
                return ""
        return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "commit": git("rev-parse", "--short", "HEAD"),
                "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def unified(old, new, name):
    return list(difflib.unified_diff(old.splitlines(), new.splitlines(), "a/" + name, "b/" + name,
                                     lineterm="", n=2))


def mask_secrets(lines):
    out = []
    for line in lines:
        m = SECRET_LINE.match(line)
        out.append(m.group(1) + m.group(2) + "********" if m else line)
    return out


# ===========================================================================
# Ansible Vault helpers
# ===========================================================================
def is_vault_encrypted(text):
    return text.lstrip().startswith("$ANSIBLE_VAULT")


def _vault_cmd(args, password, stdin_text=None):
    exe = shutil.which("ansible-vault")
    if not exe:
        raise ApiError(500, "ansible-vault is not installed on this controller")
    d = tempfile.mkdtemp(prefix="tier0gui-vault-")
    try:
        pw = os.path.join(d, "pw")
        fd = os.open(pw, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(password)
        proc = subprocess.run([exe] + args + ["--vault-password-file", pw],
                              input=(stdin_text or "").encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace")
            if "Decryption failed" in err or "no vault secrets" in err.lower():
                raise ApiError(400, "the vault password is incorrect", locked=True)
            lines = [l.strip() for l in err.splitlines() if l.strip() and l.strip() != "[...]"]
            raise ApiError(400, "ansible-vault failed: %s" % (" ".join(lines[-3:]) or proc.returncode))
        return proc.stdout.decode()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def vault_decrypt(text, password):
    if not password:
        raise ApiError(423, "vars/vault.yml is encrypted — enter the vault password", locked=True)
    return _vault_cmd(["decrypt", "--output", "-", "-"], password, text)


def vault_encrypt(plaintext, password):
    return _vault_cmd(["encrypt", "--output", "-", "-"], password, plaintext)


# ===========================================================================
# Sections (schema-driven forms)
# ===========================================================================
def coerce(field, raw):
    t = field["type"]
    try:
        if t == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("true", "1", "yes", "on")
        if t == "int":
            return int(raw)
        if t == "float":
            return float(raw)
        if t == "enum":
            for opt in field["options"]:
                if raw == opt or str(raw) == str(opt):
                    return opt
            raise ValueError("must be one of %s" % ", ".join(str(o) for o in field["options"]))
        if t == "list":
            items = raw if isinstance(raw, list) else str(raw).splitlines()
            return [str(i).strip() for i in items if str(i).strip()]
        if t == "intlist":
            items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
            return [int(i) for i in items if str(i).strip() != ""]
        if t == "yaml":
            if not isinstance(raw, str):
                return raw
            if not raw.strip():
                raise ValueError("empty — use 'unset' to remove the setting")
            return yaml.safe_load(raw)
        return "" if raw is None else str(raw)
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        raise ApiError(400, "%s: %s" % (field["key"], exc))


def load_section(repo, sid, vault_password=None):
    sec = schema.SECTIONS_BY_ID.get(sid)
    if not sec:
        raise ApiError(404, "unknown section %s" % sid)
    text, h = repo.read(sec["file"], must_exist=False)
    result = {"section": sec, "file": sec["file"], "hash": h, "values": {},
              "exists": os.path.exists(repo.path(sec["file"])), "encrypted": False}
    if sec["file"] == "ansible.cfg":
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read_string(text)
        commented = set(re.findall(r"^[#;]\s*([A-Za-z_]+)\s*=", text, re.M))
        for f in sec["fields"]:
            s, k = f["key"].split(".", 1)
            if cp.has_option(s, k):
                result["values"][f["key"]] = {"state": "set", "value": cp.get(s, k)}
            else:
                result["values"][f["key"]] = {"state": "unset" if k in commented else "absent"}
        return result

    if sec["file"] == "vars/vault.yml" and is_vault_encrypted(text):
        result["encrypted"] = True
        if not vault_password:
            result["locked"] = True
            return result
        text = vault_decrypt(text, vault_password)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ApiError(500, "%s is not valid YAML: %s" % (sec["file"], exc))
    surgeon = YamlSurgeon(text) if text.strip() else None
    for f in sec["fields"]:
        k = f["key"]
        if isinstance(data, dict) and k in data:
            result["values"][k] = {"state": "set", "value": data[k]}
            if f["type"] == "yaml":
                result["values"][k]["yaml_text"] = yaml_text(data[k])
        elif surgeon and surgeon.find_commented(k) is not None:
            result["values"][k] = {"state": "unset"}
        else:
            result["values"][k] = {"state": "absent"}
    return result


def yaml_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)) and not value:
        return "[]\n" if isinstance(value, list) else "{}\n"
    return yaml.dump(value, Dumper=_IndentedDumper, default_flow_style=False, sort_keys=False,
                     allow_unicode=True, width=1000)


def plan_section_change(repo, sid, body):
    """Return (rel_path, old_text, new_text, actions, display_old, display_new)."""
    if repo.read_only:
        raise ApiError(403, "the GUI was started with --read-only")
    sec = schema.SECTIONS_BY_ID.get(sid)
    if not sec:
        raise ApiError(404, "unknown section %s" % sid)
    fields = {f["key"]: f for f in sec["fields"]}
    raw_changes = body.get("changes") or {}
    if not raw_changes:
        raise ApiError(400, "no changes")
    changes = {}
    for key, ch in raw_changes.items():
        if key not in fields:
            raise ApiError(400, "unknown field %s" % key)
        if ch.get("action") == "unset":
            changes[key] = UNSET
        else:
            changes[key] = coerce(fields[key], ch.get("value"))

    old_text, cur_hash = repo.read(sec["file"], must_exist=False)
    if body.get("base_hash") and body["base_hash"] != cur_hash:
        raise ApiError(409, "%s changed on disk since you loaded it — reload first." % sec["file"])

    if sec["file"] == "ansible.cfg":
        ini_changes = {tuple(k.split(".", 1)): (v if v is UNSET else _ini_str(v))
                       for k, v in changes.items()}
        try:
            new_text, actions = apply_ini_changes(old_text, ini_changes)
        except SurgeryError as exc:
            raise ApiError(400, str(exc))
        return sec["file"], old_text, new_text, actions, old_text, new_text

    encrypted = sec["file"] == "vars/vault.yml" and is_vault_encrypted(old_text)
    plain = vault_decrypt(old_text, body.get("vault_password")) if encrypted else old_text
    if not plain.strip():
        plain = "---\n"
    neighbors = {k: schema.neighbors_for(sid) for k in changes}
    try:
        new_plain, actions = apply_yaml_changes(plain, changes, neighbors)
    except SurgeryError as exc:
        raise ApiError(400, "edit rejected, nothing was written: %s" % exc)
    new_text = vault_encrypt(new_plain, body["vault_password"]) if encrypted else new_plain
    return sec["file"], old_text, new_text, actions, plain, new_plain


def _ini_str(v):
    if isinstance(v, bool):
        return "True" if v else "False"
    return str(v)


# ===========================================================================
# Static inventory (inventory.yml / inventory.az.yml)
# ===========================================================================
HOST_RE = re.compile(r"^[A-Za-z0-9_.:\-\[\]]+$")
GROUP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


def inventory_model(repo, rel):
    if rel not in ("inventory.yml", "inventory.az.yml"):
        raise ApiError(400, "structured editing supports inventory.yml and inventory.az.yml")
    text, h = repo.read(rel, must_exist=False)
    data = yaml.safe_load(text) if text.strip() else {}
    data = data or {}
    root = data.get("all") or {}
    groups = []
    complex_reasons = []

    def hosts_of(node):
        out = []
        for name, hv in ((node or {}).get("hosts") or {}).items():
            out.append({"name": str(name), "vars": hv or {}})
        return out

    for gname, g in ((root.get("children") or {}).items()):
        g = g or {}
        sub = []
        for sname, s in ((g.get("children") or {}).items()):
            s = s or {}
            if s.get("children"):
                complex_reasons.append("%s/%s has nested children" % (gname, sname))
            sub.append({"name": sname, "hosts": hosts_of(s), "vars": s.get("vars") or {}})
        groups.append({"name": gname, "hosts": hosts_of(g), "vars": g.get("vars") or {},
                       "children": sub})
    if root.get("hosts"):
        complex_reasons.append("hosts defined directly under 'all'")
    header = []
    for line in text.splitlines():
        if line.startswith("#") or line.strip() in ("", "---"):
            header.append(line)
        else:
            break
    return {"file": rel, "hash": h, "groups": groups, "all_vars": root.get("vars") or {},
            "complex": complex_reasons, "header": "\n".join(header)}


def inventory_render(model):
    def clean_vars(v):
        if not v:
            return None
        if not isinstance(v, dict):
            raise ApiError(400, "vars must be a mapping")
        for k in v:
            if not GROUP_RE.match(str(k)):
                raise ApiError(400, "bad variable name %r" % k)
        return v

    def hosts_block(hosts, where):
        out = {}
        for h in hosts or []:
            name = str(h.get("name", "")).strip()
            if not name or not HOST_RE.match(name):
                raise ApiError(400, "bad host name %r in %s" % (name, where))
            if name in out:
                raise ApiError(400, "host %s listed twice in %s" % (name, where))
            out[name] = clean_vars(h.get("vars"))
        return out

    children = {}
    for g in model.get("groups") or []:
        gname = str(g.get("name", "")).strip()
        if not GROUP_RE.match(gname):
            raise ApiError(400, "bad group name %r" % gname)
        node = {}
        hb = hosts_block(g.get("hosts"), gname)
        if hb:
            node["hosts"] = hb
        sub = {}
        for s in g.get("children") or []:
            sname = str(s.get("name", "")).strip()
            if not GROUP_RE.match(sname):
                raise ApiError(400, "bad group name %r" % sname)
            snode = {}
            shb = hosts_block(s.get("hosts"), "%s/%s" % (gname, sname))
            if shb:
                snode["hosts"] = shb
            if s.get("vars"):
                snode["vars"] = clean_vars(s["vars"])
            sub[sname] = snode or None
        if sub:
            node["children"] = sub
        if g.get("vars"):
            node["vars"] = clean_vars(g["vars"])
        children[gname] = node or None
    root = {"children": children}
    if model.get("all_vars"):
        root["vars"] = clean_vars(model["all_vars"])
    body = yaml.safe_dump({"all": root}, default_flow_style=False, sort_keys=False, width=1000)
    body = body.replace(": null\n", ":\n")
    header = model.get("header") or "---"
    if "---" not in header.splitlines():
        header = "---\n" + header
    if "Tier 0 configuration GUI" not in header:
        header += "\n# Last written by the Tier 0 configuration GUI (comments inside the tree " \
                  "are not preserved; backups in .gui-backups/)"
    text = header.rstrip("\n") + "\n\n" + body
    yaml.safe_load(text)  # must round-trip
    return text


# ===========================================================================
# Validation (cross-field checks shown on Overview and before runs)
# ===========================================================================
SAMPLE_VALUES = {"hammerspace_api_host": "10.0.10.15"}


def validate(repo):
    issues = []

    def add(level, area, msg, menu=None):
        issues.append({"level": level, "area": area, "message": msg, "menu": menu})

    try:
        v = yaml.safe_load(repo.read("vars/main.yml")[0]) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"level": "error", "area": "vars/main.yml", "message": str(exc), "menu": "files"}]
    g = v.get

    host = str(g("hammerspace_api_host") or "").strip()
    if not host:
        add("warning", "Hammerspace API", "hammerspace_api_host is empty — no registration.", "hs-api")
    elif host == SAMPLE_VALUES["hammerspace_api_host"]:
        add("warning", "Hammerspace API", "hammerspace_api_host is still the sample value "
            "10.0.10.15 — set your Anvil management IP.", "hs-api")
    pw = str(g("hammerspace_api_password") or "")
    if pw and "{{" not in pw:
        add("warning", "Hammerspace API", "hammerspace_api_password is a plaintext value in "
            "vars/main.yml — move it to Credentials (vault).", "hs-api")
    try:
        hi, lo = float(g("hammerspace_volume_high_threshold", 0.98)), float(g("hammerspace_volume_low_threshold", 0.9))
        if lo >= hi:
            add("error", "Volumes", "low threshold (%s) must be below high threshold (%s)." % (lo, hi), "volumes-az")
    except (TypeError, ValueError):
        add("error", "Volumes", "thresholds must be numbers", "volumes-az")

    if not g("use_dynamic_discovery", True) and not (g("raid_arrays") or g("mount_points")
                                                     or g("hw_raid_devices") or g("existing_mount_paths")):
        add("error", "Storage", "Dynamic discovery is off but no raid_arrays, mount_points, "
            "hw_raid_devices or existing_mount_paths are defined — site.yml will fail its assert.",
            "storage-layout")
    if g("existing_mount_paths"):
        add("info", "Storage", "Existing-mount mode is active: discovery, RAID and mkfs are skipped.",
            "storage-layout")
    for key, menu in (("force_raid_recreate", "safety"), ("force_fs_recreate", "safety"),
                      ("format_nvme_to_4k", "safety"), ("allow_empty_protected_disks", "safety"),
                      ("skip_confirmation", "safety")):
        if g(key):
            add("error" if key != "skip_confirmation" else "warning", "Safety",
                "%s is ENABLED (destructive override)." % key, menu)
    if g("format_nvme_to_4k") and g("nvme_format_confirm") != "YES_I_UNDERSTAND_THIS_IS_DESTRUCTIVE":
        add("warning", "Safety", "format_nvme_to_4k is on but nvme_format_confirm is not set — "
            "formatting will be refused.", "safety")
    if g("release_ephemeral_mountpoints"):
        add("warning", "Storage", "release_ephemeral_mountpoints is set (%s): those disks will be "
            "unmounted and become Tier 0 candidates." % ", ".join(map(str, g("release_ephemeral_mountpoints"))),
            "storage-layout")
    if all(str(g(k, "n")).lower() == "n" for k in ("nfs_vers3", "nfs_vers4_0", "nfs_vers4_1", "nfs_vers4_2")):
        add("error", "NFS", "Every NFS version is disabled.", "nfs")
    if "0.0.0.0/0" in (g("client_subnets") or []):
        add("info", "NFS", "client_subnets includes 0.0.0.0/0 — every host may mount the exports "
            "(root_squash).", "nfs")
    if g("iperf_test_enabled") and not g("iperf_test_targets"):
        add("warning", "Pre-checks", "iperf test enabled without iperf_test_targets.", "precheck")
    if g("hammerspace_create_shares") and not g("hammerspace_shares"):
        add("warning", "Shares", "hammerspace_create_shares is on but no shares are defined.", "groups-shares")

    if g("deploy_di"):
        mode = g("di_deployment_type", "host")
        if mode == "kubernetes" and not g("di_k8s_registry_namespace"):
            add("error", "DI", "Kubernetes mode requires di_k8s_registry_namespace.", "di-k8s")
        if g("di_rpm_source", "directory") == "directory":
            pdir = os.path.join(repo.root, "payload")
            rpms = [f for f in os.listdir(pdir)] if os.path.isdir(pdir) else []
            rpms = [f for f in rpms if f.endswith(".rpm")]
            if not rpms:
                add("error", "DI", "di_rpm_source is 'directory' but payload/ has no RPMs.", "di-packages")
            else:
                lfs = [f for f in rpms if _is_lfs_pointer(os.path.join(pdir, f))]
                if lfs:
                    add("error", "DI", "%d RPM(s) in payload/ are Git LFS pointers — run "
                        "'git lfs pull'." % len(lfs), "di-packages")
        try:
            inv = inventory_model(repo, "inventory.yml")
            di = [x for x in inv["groups"] if x["name"] == "di_nodes"]
            count = sum(len(x["hosts"]) + sum(len(c["hosts"]) for c in x["children"]) for x in di)
            if not count:
                add("warning", "DI", "deploy_di is true but inventory.yml has no di_nodes hosts.", "inventory")
        except ApiError:
            pass

    # ansible.cfg inventory
    try:
        cfg_text = repo.read("ansible.cfg")[0]
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        cp.read_string(cfg_text)
        inv = cp.get("defaults", "inventory", fallback="")
        for part in [p.strip() for p in inv.split(",") if p.strip()]:
            if not os.path.exists(repo.path(part)):
                add("error", "Ansible", "ansible.cfg inventory %s does not exist." % part, "ansible-cfg")
        app_inv = remembered_inventory(repo) or (["inventory.yml"] if os.path.exists(repo.path("inventory.yml")) else [])
        cfg_inv = [x.strip() for x in inv.split(",") if x.strip()]
        if app_inv and cfg_inv and app_inv != cfg_inv:
            add("info", "Ansible", "This app's runs use %s%s; ansible.cfg defaults to %s, which is what "
                "command-line runs without -i use." % (", ".join(app_inv),
                " (your last selection)" if remembered_inventory(repo) else "", ", ".join(cfg_inv)),
                "ansible-cfg")
    except ApiError:
        add("warning", "Ansible", "ansible.cfg not found.", "ansible-cfg")

    # vault
    vtext = repo.read("vars/vault.yml", must_exist=False)[0]
    if vtext and not is_vault_encrypted(vtext):
        data = yaml.safe_load(vtext) or {}
        if any(str(k).startswith("vault_") and val for k, val in data.items()):
            add("warning", "Credentials", "vars/vault.yml contains secrets in PLAINTEXT — encrypt it.",
                "credentials")
        if not data.get("vault_hammerspace_api_password") and "{{ vault_hammerspace_api_password }}" in pw:
            add("warning", "Credentials", "vault_hammerspace_api_password is not set (pass it with "
                "-e or set it under Credentials).", "credentials")
    if not shutil.which("ansible-playbook"):
        add("error", "Controller", "ansible-playbook is not on PATH — install Ansible to run playbooks.",
            "overview")
    return issues


def _is_lfs_pointer(path):
    try:
        if os.path.getsize(path) > 1024:
            return False
        with open(path, "rb") as fh:
            return fh.read(40).startswith(b"version https://git-lfs")
    except OSError:
        return False


# ===========================================================================
# OS upgrade snapshots
# ===========================================================================
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:\-]+$")


def _snapshot_header(path):
    meta = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line.startswith("#! "):
                    break
                if ":" in line:
                    k, v = line[3:].split(":", 1)
                    meta[k.strip()] = v.strip()
    except OSError:
        pass
    return meta


def upgrade_runs(repo):
    root = repo.snapshot_root
    runs = []
    if not os.path.isdir(root):
        return runs
    for run in sorted(os.listdir(root)):
        rdir = os.path.join(root, run)
        if not os.path.isdir(rdir):
            continue
        hosts = []
        for host in sorted(os.listdir(rdir)):
            hdir = os.path.join(rdir, host)
            if not os.path.isdir(hdir):
                continue
            files = sorted(os.listdir(hdir))
            entry = {"host": host, "archives": [f for f in files if re.match(r"^(pre|post)-.*\.txt$", f)]}
            for phase in ("pre", "post"):
                p = os.path.join(hdir, phase + ".txt")
                entry[phase] = _snapshot_header(p) if os.path.isfile(p) else None
            dj = os.path.join(hdir, "diff.json")
            if os.path.isfile(dj):
                try:
                    with open(dj) as fh:
                        d = json.load(fh)
                    entry["diff"] = {"identical": d["identical"], "counts": d["counts"],
                                     "attention": d.get("attention", [])[:6]}
                except (OSError, ValueError, KeyError):
                    entry["diff"] = None
            else:
                entry["diff"] = None
            hosts.append(entry)
        runs.append({"run": run, "hosts": hosts,
                     "mtime": os.path.getmtime(rdir)})
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


def _snap_host_dir(repo, run, host):
    if not (SAFE_NAME.match(run or "") and SAFE_NAME.match(host or "")):
        raise ApiError(400, "bad run/host name")
    d = os.path.join(repo.snapshot_root, run, host)
    if not os.path.isdir(d):
        raise ApiError(404, "no snapshots for %s/%s" % (run, host))
    return d


# ===========================================================================
# sudo (become) password for playbook runs
# ===========================================================================
# The plays gather facts (which already needs sudo) BEFORE their pre_tasks load
# vars/vault.yml, so a sudo password kept only in the vault would arrive too late.
# Both ways below hand it to ansible-playbook as EXTRA VARS, which exist from the
# first task on:
#   * typed for this run  -> 0600 temp file  -e @file  (deleted when the job ends;
#                            equivalent to --ask-become-pass / -K)
#   * saved in Credentials -> -e @vars/vault.yml plus a reference to
#                            vault_become_password (needs the vault password if
#                            the vault is encrypted)
BECOME_VAR_REF = {"ansible_become_password": "{{ vault_become_password }}"}


def become_args(repo, body, jm, cleanup):
    args = []
    if body.get("use_saved_become"):
        vault = repo.path("vars/vault.yml")
        if not os.path.isfile(vault):
            raise ApiError(400, "vars/vault.yml does not exist — set the sudo password under Credentials first")
        text = repo.read("vars/vault.yml")[0]
        if is_vault_encrypted(text):
            if not (body.get("vault_password") or body.get("vault_password_file")):
                raise ApiError(400, "vars/vault.yml is encrypted — give the vault password so the saved "
                                    "sudo password can be read")
        else:
            data = yaml.safe_load(text) or {}
            if not data.get("vault_become_password"):
                raise ApiError(400, "vault_become_password is not set — add it under Credentials, or type "
                                    "the sudo password for this run")
        args += ["-e", "@" + vault, "-e", json.dumps(BECOME_VAR_REF)]
    if body.get("become_password"):
        d = jm.secret_dir()
        f = os.path.join(d, "become.json")
        fd = os.open(f, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"ansible_become_password": body["become_password"]}, fh)
        cleanup.append(d)
        args += ["-e", "@" + f]   # last -e wins: a typed password overrides the saved one
    return args


# ===========================================================================
# SSH key bootstrap (Static Hosts -> "Copy SSH key to hosts")
# ===========================================================================
def ssh_dir():
    return os.path.expanduser("~/.ssh")


def list_ssh_keys():
    """Public keys of the user running the GUI (the identity Ansible will use)."""
    keys = []
    d = ssh_dir()
    if not os.path.isdir(d):
        return keys
    for name in sorted(os.listdir(d)):
        if not name.endswith(".pub"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path) as fh:
                line = fh.read().strip()
        except OSError:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        fp = ""
        if shutil.which("ssh-keygen"):
            try:
                fp = subprocess.check_output(["ssh-keygen", "-lf", path], stderr=subprocess.DEVNULL,
                                             timeout=10).decode().split()[1]
            except Exception:  # noqa: BLE001
                pass
        priv = path[:-4]
        keys.append({"path": path, "type": parts[0], "comment": parts[2] if len(parts) > 2 else "",
                     "fingerprint": fp, "private_key": priv if os.path.isfile(priv) else None})
    keys.sort(key=lambda k: (k["private_key"] is None, "ed25519" not in k["type"], k["path"]))
    return keys


def build_ssh_push(repo, body):
    sys.path.insert(0, os.path.join(repo.root, "scripts"))
    import push_ssh_key  # noqa: E402
    user = str(body.get("user") or "").strip()
    if not push_ssh_key.USER_RE.match(user):
        raise ApiError(400, "enter the SSH user to log in as (letters, digits, . _ -)")
    if not body.get("password"):
        raise ApiError(400, "enter the SSH password for %s" % user)
    targets = body.get("targets") or []
    if not targets:
        raise ApiError(400, "select at least one host")
    specs = []
    for t in targets:
        spec = "%s=%s:%d" % (t.get("name") or t.get("host"), t.get("host"), int(t.get("port") or 22))
        try:
            push_ssh_key.Target(spec)
        except ValueError as exc:
            raise ApiError(400, "host %s: %s" % (t.get("name") or t.get("host"), exc))
        specs.append(spec)
    argv = [sys.executable, os.path.join(repo.root, "scripts", "push_ssh_key.py"), "--user", user]
    if body.get("generate"):
        target_key = os.path.join(ssh_dir(), "id_ed25519")
        if os.path.exists(target_key):
            target_key = os.path.join(ssh_dir(), "id_ed25519_tier0")
        argv += ["--generate", target_key]
    else:
        allowed = {k["path"] for k in list_ssh_keys()}
        if body.get("pubkey") not in allowed:
            raise ApiError(400, "choose one of the controller's public keys")
        argv += ["--pubkey", body["pubkey"]]
    if body.get("ignore_host_keys"):
        argv.append("--ignore-host-keys")
    for spec in specs:
        argv += ["--target", spec]
    return argv, {"TIER0_SSH_PASSWORD": body["password"]}, "Copy SSH key to %d host(s) as %s" % (len(specs), user)


# ===========================================================================
# Remembered UI choices (.gui-state.json, gitignored)
# ===========================================================================
# Kept on the server, not in the browser, so the choice follows the operator
# across browsers, machines and SSH tunnels. Only UI preferences live here —
# never configuration or secrets.
STATE_FILE = ".gui-state.json"


def load_state(repo):
    try:
        with open(repo.path(STATE_FILE)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remembered_inventory(repo):
    """Last inventory selection, dropping files that no longer exist."""
    files = set(repo.inventory_files())
    inv = [i for i in load_state(repo).get("last_inventory") or [] if i in files]
    return inv


_STATE_LOCK = threading.Lock()


def save_state(repo, state):
    path = repo.path(STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def log_settings(repo):
    return logstore.settings_from_state(load_state(repo))


def _state_post(handler, q):
    repo = handler.app["repo"]
    body = handler._body()
    with _STATE_LOCK:
        state = load_state(repo)
        if "last_inventory" in body:
            inv = body["last_inventory"]
            if not isinstance(inv, list) or any(i not in repo.inventory_files() for i in inv):
                raise ApiError(400, "unknown inventory file")
            state["last_inventory"] = inv
        save_state(repo, state)
    handler._send(200, {"ok": True, "state": state})


# ===========================================================================
# Upload to Hammerspace (scripts/hs_upload.py, same intake as HS_PS_Toolkit.sh)
# ===========================================================================
def _hs_upload_module(repo):
    sys.path.insert(0, os.path.join(repo.root, "scripts"))
    import hs_upload  # noqa: E402
    return hs_upload


def redact_argv(argv):
    """argv for the audit log: secrets never reach argv by design (files/env), but
    free-form extra vars could contain one, so mask anyway."""
    try:
        return [_hs_upload_module(Handler.app["repo"]).redact_text(str(a))[0] for a in argv]
    except Exception:  # noqa: BLE001
        return ["(argv not recorded)"]


def build_upload(repo, body):
    ids = body.get("ids") or []
    if not ids:
        raise ApiError(400, "select at least one log or report")
    paths = []
    for i in ids:
        try:
            paths.append(logstore.resolve(repo.root, i, allow_dirs=True))
        except logstore.LogError:
            raise ApiError(400, "not a log or report: %s" % i)
    st = log_settings(repo)
    initials = str(body.get("initials", st["upload_initials"]) or "").strip()
    customer = str(body.get("customer", st["upload_customer"]) or "").strip()
    try:
        logstore.validate_settings({"upload_initials": initials, "upload_customer": customer})
    except logstore.LogError as exc:
        raise ApiError(400, str(exc))
    note = re.sub(r"[\x00-\x1f]", " ", str(body.get("note") or ""))[:500]
    argv = [sys.executable, os.path.join(repo.root, "scripts", "hs_upload.py"),
            "--repo", repo.root, "--url", st["upload_url"]]
    if initials:
        argv += ["--initials", initials]
    if customer:
        argv += ["--customer", customer]
    if note:
        argv += ["--note", note]
    if not st["upload_verify_tls"]:
        argv.append("--insecure")
    if body.get("include_config"):
        argv.append("--include-config")
    if body.get("bundle_only"):
        argv.append("--bundle-only")
    argv.append("--")
    argv += paths
    # Remember the engineer details for next time (like the toolkit asks once per session).
    with _STATE_LOCK:
        state = load_state(repo)
        state.setdefault("logs", {}).update({"upload_initials": initials, "upload_customer": customer})
        save_state(repo, state)
    title = ("Build bundle" if body.get("bundle_only") else "Upload to Hammerspace") + \
        " (%d item%s)" % (len(paths), "" if len(paths) == 1 else "s")
    meta = {"items": ids, "bundle_only": bool(body.get("bundle_only")), "url": st["upload_url"],
            "include_config": bool(body.get("include_config"))}
    return argv, title, meta


def run_maintenance(app, reason="scheduled"):
    """Rotate the append-only logs and apply the retention period."""
    repo, jm = app["repo"], app["jobs"]
    days = log_settings(repo)["log_retention_days"]
    keep = set()
    for j in jm.running():
        keep.add("repo:" + os.path.relpath(j.log_path, repo.root).replace(os.sep, "/"))
        al = (j.meta or {}).get("ansible_log")
        if al:
            keep.add("repo:" + al)
    rotated = logstore.rotate(repo.root)
    result = logstore.prune(repo.root, days, keep)
    jm.drop_missing()
    result.update({"rotated": rotated, "retention_days": days})
    if result["deleted"] or rotated or reason == "manual":
        app["audit"].log("logs.maintenance", reason=reason, **result)
    return result


def traceback_print():
    import traceback
    traceback.print_exc()


def _ssh_keys(handler, q):
    handler._send(200, {"keys": list_ssh_keys(), "ssh_dir": ssh_dir(),
                        "ssh_client": bool(shutil.which("ssh"))})


def _differ():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
    import tier0_snapshot_diff  # noqa: E402
    return tier0_snapshot_diff


# ===========================================================================
# Docs
# ===========================================================================
def doc_list(repo):
    """Markdown docs shown in the GUI: repo root, docs/**, gui/ and tests READMEs."""
    docs = [n for n in sorted(os.listdir(repo.root)) if n.lower().endswith(".md")]
    ddir = repo.path("docs")
    if os.path.isdir(ddir):
        for d, dirs, files in os.walk(ddir):
            dirs.sort()
            for f in sorted(files):
                if f.lower().endswith(".md"):
                    docs.append(os.path.relpath(os.path.join(d, f), repo.root))
    for extra in ("gui/README.md", "tests/integration/README.md"):
        if os.path.isfile(repo.path(extra)):
            docs.append(extra)
    first = [d for d in ("README.md", "docs/gui/configure-and-deploy.md", "docs/gui/static.md",
                         "docs/gui/oci.md", "docs/gui/aws.md", "docs/gui/azure.md", "docs/gui/gcp.md",
                         "gui/README.md", "docs/manual/README.md", "VARIABLE_REFERENCE.md",
                         "docs/os-upgrade-check.md") if d in docs]
    return first + [d for d in docs if d not in first]


DOC_GROUPS = [
    ("Start here", lambda d: d == "README.md"),
    ("GUI guides", lambda d: d.startswith("docs/gui/") or d == "gui/README.md"),
    ("Manual (CLI) guides", lambda d: d.startswith("docs/manual/")),
    ("Deployment & operations", lambda d: d.startswith(("DEPLOYMENT_GUIDE", "AUTOMATION_GUIDE",
                                                         "RACK_OPERATIONS")) or d == "docs/os-upgrade-check.md"),
    ("Reference", lambda d: d in ("VARIABLE_REFERENCE.md", "tests/integration/README.md")),
    ("Project notes", lambda d: True),
]


def doc_entries(repo):
    """doc_list() with a title (first '# ' heading) and a group for each document."""
    out = []
    for path in doc_list(repo):
        title = path
        try:
            with open(repo.path(path), encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
        except OSError:
            pass
        group = next(g for g, match in DOC_GROUPS if match(path))
        out.append({"path": path, "title": title, "group": group})
    order = [g for g, _ in DOC_GROUPS]
    out.sort(key=lambda e: order.index(e["group"]))   # stable: keeps doc_list order within a group
    return out


# ===========================================================================
# HTTP layer
# ===========================================================================
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Tier0GUI/" + VERSION
    app = None  # set in main()

    def log_message(self, fmt, *args):
        if self.app["verbose"]:
            sys.stderr.write("[gui] %s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ---------------------------------------------------------------
    def _send(self, status, body, ctype="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "frame-ancestors 'none'")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self, query):
        token = self.headers.get("X-Auth-Token") or (query.get("token") or [""])[0]
        return hmac.compare_digest(token.encode(), self.app["token"].encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            raise ApiError(400, "invalid JSON")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path
        try:
            if method == "GET" and (path == "/" or path.startswith("/static/")):
                return self._static(path)
            if path == "/favicon.ico":
                return self._static("/static/favicon.svg")
            if not self._authorized(query):
                raise ApiError(401, "missing or invalid token — open the URL printed by tier0_gui.py")
            if method == "POST" and self.headers.get("X-Requested-With") != "tier0-gui":
                raise ApiError(403, "bad request origin")
            route = ROUTES.get((method, path))
            if route:
                return route(self, query)
            for (m, prefix), fn in PREFIX_ROUTES:
                if m == method and path.startswith(prefix):
                    return fn(self, query, path[len(prefix):])
            raise ApiError(404, "no such endpoint")
        except ApiError as exc:
            payload = {"error": exc.message}
            payload.update(exc.extra)
            self._send(exc.status, payload)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send(500, {"error": "internal error: %s" % exc})

    def _static(self, path):
        rel = "index.html" if path == "/" else path[len("/static/"):]
        full = os.path.abspath(os.path.join(HERE, "static", rel))
        if not full.startswith(os.path.join(HERE, "static") + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def audit(self, event, **fields):
        try:
            self.app["audit"].log(event, client=self.client_address[0], **fields)
        except Exception:  # noqa: BLE001  (never fail a request because auditing failed)
            traceback_print()

    # -- endpoints ----------------------------------------------------------------
    def api_meta(self, q):
        repo = self.app["repo"]
        self._send(200, {
            "version": VERSION, "repo": repo.root, "read_only": repo.read_only,
            "git": repo.git_info(),
            "last_inventory": remembered_inventory(repo),
            "menus": schema.MENUS, "sections": schema.SECTIONS_BY_ID,
            "playbooks": catalog.PLAYBOOKS, "utilities": catalog.UTILITIES,
            "inventories": repo.inventory_files(), "editable_files": schema.EDITABLE_FILES,
        })

    def api_overview(self, q):
        repo = self.app["repo"]
        cfg_inv = ""
        try:
            cp = configparser.RawConfigParser(strict=False, interpolation=None)
            cp.read_string(repo.read("ansible.cfg")[0])
            cfg_inv = cp.get("defaults", "inventory", fallback="")
        except ApiError:
            pass
        inv_counts = {}
        try:
            for g in inventory_model(repo, "inventory.yml")["groups"]:
                inv_counts[g["name"]] = len(g["hosts"]) + sum(len(c["hosts"]) for c in g["children"])
        except Exception:  # noqa: BLE001
            pass
        vtext = repo.read("vars/vault.yml", must_exist=False)[0]
        ap = shutil.which("ansible-playbook")
        ansible_ver = ""
        if ap:
            try:
                ansible_ver = subprocess.check_output([ap, "--version"], stderr=subprocess.DEVNULL,
                                                      timeout=20).decode().splitlines()[0]
            except Exception:  # noqa: BLE001
                ansible_ver = "installed (version check failed)"
        self._send(200, {
            "git": repo.git_info(), "python": sys.version.split()[0], "ansible": ansible_ver,
            "ansible_cfg_inventory": cfg_inv, "static_inventory_counts": inv_counts,
            "vault": "missing" if not vtext else ("encrypted" if is_vault_encrypted(vtext) else "plaintext"),
            "issues": validate(repo), "jobs": self.app["jobs"].list()[:5],
            "upgrade_runs": [{"run": r["run"], "hosts": len(r["hosts"])} for r in upgrade_runs(repo)][:5],
        })

    def api_section_get(self, q, sid):
        self._send(200, load_section(self.app["repo"], sid))

    def api_section_post(self, q, rest):
        sid, _, action = rest.partition("/")
        body = self._body()
        repo = self.app["repo"]
        if action == "read":  # vault unlock
            return self._send(200, load_section(repo, sid, body.get("vault_password")))
        rel, old, new, actions, d_old, d_new = plan_section_change(repo, sid, body)
        diff = unified(d_old, d_new, rel)
        if rel == "vars/vault.yml":
            diff = mask_secrets(diff)
        if action == "preview":
            return self._send(200, {"file": rel, "diff": diff, "actions": actions})
        if action == "save":
            new_hash, backup = repo.write(rel, new, body.get("base_hash"))
            self.audit("config.save", file=rel, menu=sid, changes=actions, backup=backup)
            return self._send(200, {"file": rel, "hash": new_hash, "backup": backup,
                                    "actions": actions, "diff": diff})
        raise ApiError(404, "unknown action")

    def api_vault_encrypt(self, q):
        repo = self.app["repo"]
        body = self._body()
        pw = body.get("vault_password") or ""
        if len(pw) < 8:
            raise ApiError(400, "use a vault password of at least 8 characters")
        text, h = repo.read("vars/vault.yml")
        if is_vault_encrypted(text):
            raise ApiError(400, "vars/vault.yml is already encrypted")
        new_hash, backup = repo.write("vars/vault.yml", vault_encrypt(text, pw), body.get("base_hash"))
        # The backup is plaintext — remove it so encryption actually protects the secrets.
        if backup:
            try:
                os.unlink(repo.path(backup))
            except OSError:
                pass
        self.audit("vault.encrypt", file="vars/vault.yml")
        self._send(200, {"hash": new_hash, "encrypted": True})

    def api_file_get(self, q):
        repo = self.app["repo"]
        rel = (q.get("path") or [""])[0]
        repo.editable(rel)
        text, h = repo.read(rel, must_exist=False)
        enc = rel == "vars/vault.yml" and is_vault_encrypted(text)
        self._send(200, {"path": rel, "text": text, "hash": h, "encrypted": enc,
                         "exists": os.path.exists(repo.path(rel))})

    def api_file_post(self, q, action):
        repo = self.app["repo"]
        body = self._body()
        rel = body.get("path", "")
        repo.editable(rel)
        new = body.get("text", "")
        if not new.endswith("\n"):
            new += "\n"
        old, h = repo.read(rel, must_exist=False)
        if rel == "vars/vault.yml" and is_vault_encrypted(old) and not is_vault_encrypted(new):
            raise ApiError(400, "refusing to overwrite the encrypted vault with plaintext here — "
                                "use the Credentials menu")
        if rel.endswith((".yml", ".yaml")) and not is_vault_encrypted(new):
            try:
                yaml.safe_load(new)
            except yaml.YAMLError as exc:
                raise ApiError(400, "not valid YAML, nothing written: %s" % exc)
        elif rel.endswith(".cfg"):
            try:
                configparser.RawConfigParser(strict=False, interpolation=None).read_string(new)
            except configparser.Error as exc:
                raise ApiError(400, "not valid INI, nothing written: %s" % exc)
        diff = unified(old, new, rel)
        if action == "preview":
            return self._send(200, {"diff": diff})
        new_hash, backup = repo.write(rel, new, body.get("base_hash"))
        self.audit("config.save", file=rel, menu="raw-editor", lines_changed=len(
            [l for l in diff if l[:1] in "+-" and not l.startswith(("+++", "---"))]), backup=backup)
        self._send(200, {"hash": new_hash, "backup": backup, "diff": diff})

    def api_inventory_get(self, q):
        rel = (q.get("file") or ["inventory.yml"])[0]
        self._send(200, inventory_model(self.app["repo"], rel))

    def api_inventory_post(self, q, action):
        repo = self.app["repo"]
        body = self._body()
        rel = body.get("file", "inventory.yml")
        if rel not in ("inventory.yml", "inventory.az.yml"):
            raise ApiError(400, "unsupported inventory file")
        new = inventory_render(body.get("model") or {})
        old, _ = repo.read(rel, must_exist=False)
        diff = unified(old, new, rel)
        if action == "preview":
            return self._send(200, {"diff": diff})
        new_hash, backup = repo.write(rel, new, body.get("base_hash"))
        self.audit("config.save", file=rel, menu="static-hosts", lines_changed=len(
            [l for l in diff if l[:1] in "+-" and not l.startswith(("+++", "---"))]), backup=backup)
        self._send(200, {"hash": new_hash, "backup": backup, "diff": diff})

    def api_validate(self, q):
        self._send(200, {"issues": validate(self.app["repo"])})

    # -- jobs -------------------------------------------------------------------------
    def api_jobs_list(self, q):
        self._send(200, {"jobs": self.app["jobs"].list()})

    def api_jobs_start(self, q):
        repo = self.app["repo"]
        if repo.read_only:
            raise ApiError(403, "the GUI was started with --read-only")
        body = self._body()
        jm = self.app["jobs"]
        kind = body.get("kind")
        env, cleanup, meta = {}, [], {"kind": kind}
        try:
            if kind == "playbook":
                argv, extra, destructive = catalog.build_playbook_argv(repo.root, body, repo.inventory_files())
                if destructive and not body.get("confirm"):
                    raise ApiError(412, destructive, needs_confirm=True)
                if extra:
                    argv += ["-e", json.dumps(extra)]
                title = "%s%s" % (body.get("playbook"), " (check)" if body.get("check") else "")
                meta.update({"playbook": body.get("playbook"), "extra": extra})
            elif kind == "utility":
                argv, destructive = catalog.build_utility_argv(repo.root, body, sys.executable)
                if destructive and not body.get("confirm"):
                    raise ApiError(412, "This will change the Hammerspace cluster (no --dry-run).",
                                   needs_confirm=True)
                if body.get("hs_password"):
                    env["HAMMERSPACE_PASSWORD"] = body["hs_password"]
                title = body.get("script")
            elif kind == "ssh_key_push":
                argv, env, title = build_ssh_push(repo, body)
                meta.update({"user": body.get("user"), "hosts": len(body.get("targets") or [])})
            elif kind == "inventory_graph":
                inv = body.get("inventory")
                if inv not in repo.inventory_files():
                    raise ApiError(400, "unknown inventory")
                argv = ["%s/scripts/run.sh" % repo.root, "inventory", "-i", inv, "--graph", "--vars"]
                title = "ansible-inventory --graph %s" % inv
            elif kind == "hs_upload":
                argv, title, umeta = build_upload(repo, body)
                meta.update(umeta)
            else:
                raise ApiError(400, "unknown job kind")
        except catalog.CatalogError as exc:
            raise ApiError(400, str(exc))

        if kind == "playbook":
            if body.get("vault_password"):
                d = jm.secret_dir()
                pw = os.path.join(d, "vault-pass")
                fd = os.open(pw, os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(body["vault_password"])
                argv += ["--vault-password-file", pw]
                cleanup.append(d)
            elif body.get("vault_password_file"):
                vpf = os.path.expanduser(str(body["vault_password_file"]))
                if not os.path.isfile(vpf):
                    raise ApiError(400, "vault password file %s not found" % vpf)
                argv += ["--vault-password-file", vpf]
            argv += become_args(repo, body, jm, cleanup)
        if kind in ("playbook", "inventory_graph"):
            # A dedicated Ansible log for this run, linked to the job, so the
            # Logs page can show both the job output and Ansible's own log.
            label = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(
                str(body.get("playbook") or "inventory")).replace(".yml", ""))[:40]
            rel_log = "logs/ansible-%s-gui-%s-%s.log" % (time.strftime("%Y%m%d_%H%M%S"), label,
                                                          secrets.token_hex(3))
            env["ANSIBLE_LOG_PATH"] = repo.path(rel_log)
            meta["ansible_log"] = rel_log
        job = jm.start(title, argv, env_extra=env, cleanup_paths=cleanup, meta=meta)
        self.audit("job.start", job=job.id, kind=kind, title=title, argv=redact_argv(argv),
                   ansible_log=meta.get("ansible_log"))
        self._send(200, job.to_dict())

    def api_job(self, q, rest):
        job_id, _, action = rest.partition("/")
        job = self.app["jobs"].get(job_id)
        if not job:
            raise ApiError(404, "no such job")
        if self.command == "POST" and action == "stop":
            job.stop()
            return self._send(200, job.to_dict())
        offset = int((q.get("offset") or ["0"])[0])
        text, new_offset = job.read(offset)
        d = job.to_dict()
        d.update({"text": text, "offset": new_offset})
        self._send(200, d)

    # -- logs & reports -----------------------------------------------------------------
    def api_logs(self, q):
        repo, jm = self.app["repo"], self.app["jobs"]
        jm.drop_missing()
        roots = logstore.roots(repo.root)
        self._send(200, {"entries": logstore.catalog(repo.root, jm.list()),
                         "settings": log_settings(repo),
                         "default_upload_url": logstore.DEFAULT_UPLOAD_URL,
                         "dirs": {"logs": os.path.join(repo.root, "logs"),
                                  "legacy": roots.get("home")},
                         "activity": self.app["audit"].tail(int((q.get("activity") or ["200"])[0]))})

    def _log_path(self, q, allow_dirs=False):
        try:
            return logstore.resolve(self.app["repo"].root, (q.get("id") or [""])[0], allow_dirs)
        except logstore.LogError as exc:
            raise ApiError(404, str(exc))

    def api_logs_view(self, q):
        path = self._log_path(q)
        length = max(1, min(int((q.get("length") or [str(512 * 1024)])[0]), 2 * 1024 * 1024))
        self._send(200, logstore.read_chunk(path, int((q.get("offset") or ["0"])[0]), length,
                                            tail=(q.get("tail") or [""])[0] == "1"))

    def api_logs_download(self, q):
        path = self._log_path(q)
        name = os.path.basename(path)
        if (q.get("inline") or [""])[0] == "1" and name.endswith(".html"):
            with open(path, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        ctype = "application/gzip" if name.endswith(".gz") else "text/plain; charset=utf-8"
        with open(path, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype, {"Content-Disposition": 'attachment; filename="%s"' % name})

    def api_logs_settings(self, q):
        repo = self.app["repo"]
        if self.command == "GET":
            return self._send(200, log_settings(repo))
        if repo.read_only:
            raise ApiError(403, "the GUI was started with --read-only")
        try:
            changes = logstore.validate_settings(self._body())
        except logstore.LogError as exc:
            raise ApiError(400, str(exc))
        with _STATE_LOCK:
            state = load_state(repo)
            state.setdefault("logs", {}).update(changes)
            save_state(repo, state)
        self.audit("settings.change", changes={k: v for k, v in changes.items()})
        self._send(200, log_settings(repo))

    def api_logs_prune(self, q):
        repo = self.app["repo"]
        if repo.read_only:
            raise ApiError(403, "the GUI was started with --read-only")
        self._send(200, run_maintenance(self.app, reason="manual"))

    # -- upgrade ------------------------------------------------------------------------
    def api_upgrade_runs(self, q):
        self._send(200, {"runs": upgrade_runs(self.app["repo"]),
                         "root": os.path.relpath(self.app["repo"].snapshot_root, self.app["repo"].root)})

    def api_upgrade_report(self, q):
        repo = self.app["repo"]
        run, host = (q.get("run") or [""])[0], (q.get("host") or [""])[0]
        d = _snap_host_dir(repo, run, host)
        fmt = (q.get("format") or ["json"])[0]
        if fmt == "html":
            p = os.path.join(d, "diff.html")
            if not os.path.isfile(p):
                raise ApiError(404, "no diff.html yet")
            with open(p, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if fmt in ("pre", "post", "txt"):
            p = os.path.join(d, {"pre": "pre.txt", "post": "post.txt", "txt": "diff.txt"}[fmt])
            if not os.path.isfile(p):
                raise ApiError(404, "%s not found" % os.path.basename(p))
            with open(p, "rb") as fh:
                return self._send(200, fh.read(), "text/plain; charset=utf-8")
        p = os.path.join(d, "diff.json")
        if not os.path.isfile(p):
            raise ApiError(404, "no diff yet for %s/%s" % (run, host))
        with open(p) as fh:
            self._send(200, json.load(fh))

    def api_upgrade_compare(self, q):
        repo = self.app["repo"]
        body = self._body()
        d = _snap_host_dir(repo, body.get("run"), body.get("host"))
        names = []
        for k in ("before", "after"):
            n = body.get(k) or ""
            if not re.match(r"^(pre|post)(-[0-9TZ]+)?\.txt$", n) or not os.path.isfile(os.path.join(d, n)):
                raise ApiError(400, "bad snapshot name %r" % n)
            names.append(os.path.join(d, n))
        differ = _differ()
        try:
            res = differ.compare(names[0], names[1])
        except differ.SnapshotError as exc:
            raise ApiError(400, str(exc))
        self._send(200, res)


ROUTES = {
    ("GET", "/api/meta"): Handler.api_meta,
    ("GET", "/api/overview"): Handler.api_overview,
    ("GET", "/api/validate"): Handler.api_validate,
    ("GET", "/api/file"): Handler.api_file_get,
    ("GET", "/api/inventory"): Handler.api_inventory_get,
    ("GET", "/api/jobs"): Handler.api_jobs_list,
    ("POST", "/api/jobs"): Handler.api_jobs_start,
    ("POST", "/api/vault/encrypt"): Handler.api_vault_encrypt,
    ("GET", "/api/upgrade/runs"): Handler.api_upgrade_runs,
    ("GET", "/api/upgrade/report"): Handler.api_upgrade_report,
    ("POST", "/api/upgrade/compare"): Handler.api_upgrade_compare,
}
PREFIX_ROUTES = [
    (("GET", "/api/section/"), Handler.api_section_get),
    (("POST", "/api/section/"), Handler.api_section_post),
    (("POST", "/api/file/"), Handler.api_file_post),
    (("POST", "/api/inventory/"), Handler.api_inventory_post),
    (("GET", "/api/jobs/"), Handler.api_job),
    (("POST", "/api/jobs/"), Handler.api_job),
]


def _doc_get(handler, q):
    repo = handler.app["repo"]
    name = (q.get("name") or [""])[0]
    if name not in doc_list(repo):
        raise ApiError(404, "unknown document")
    handler._send(200, {"name": name, "text": repo.read(name)[0]})


def _docs_list(handler, q):
    repo = handler.app["repo"]
    handler._send(200, {"docs": doc_list(repo), "entries": doc_entries(repo)})


ROUTES[("GET", "/api/docs")] = _docs_list
ROUTES[("GET", "/api/ssh/keys")] = _ssh_keys
ROUTES[("POST", "/api/state")] = _state_post
ROUTES[("GET", "/api/doc")] = _doc_get
ROUTES[("GET", "/api/logs")] = Handler.api_logs
ROUTES[("GET", "/api/logs/view")] = Handler.api_logs_view
ROUTES[("GET", "/api/logs/download")] = Handler.api_logs_download
ROUTES[("GET", "/api/logs/settings")] = Handler.api_logs_settings
ROUTES[("POST", "/api/logs/settings")] = Handler.api_logs_settings
ROUTES[("POST", "/api/logs/prune")] = Handler.api_logs_prune


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tier 0 configuration GUI (local web UI).")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1; use an SSH tunnel for remote access)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TIER0_GUI_PORT", 8765)))
    ap.add_argument("--repo", default=os.path.dirname(HERE), help="repository root (default: parent of gui/)")
    ap.add_argument("--token", default=os.environ.get("TIER0_GUI_TOKEN"),
                    help="access token (default: random per start; or set TIER0_GUI_TOKEN)")
    ap.add_argument("--read-only", action="store_true", help="disable saving files and running jobs")
    ap.add_argument("--no-browser", action="store_true", help="do not try to open a browser")
    ap.add_argument("--verbose", action="store_true", help="log every HTTP request")
    args = ap.parse_args(argv)

    repo = Repo(args.repo, read_only=args.read_only)
    if not os.path.isfile(repo.path("vars/main.yml")):
        sys.exit("%s does not look like the Tier 0 repo (vars/main.yml missing)" % repo.root)
    token = args.token or secrets.token_urlsafe(18)
    logstore.ensure_dirs(repo.root)
    migrated = logstore.migrate_legacy_jobs(repo.root)
    audit = logstore.Audit(repo.root)

    def job_finished(job):
        audit.log("job.finish", job=job.id, title=job.title, status=job.status, rc=job.returncode,
                  seconds=round((job.finished or 0) - (job.started or 0), 1),
                  ansible_log=(job.meta or {}).get("ansible_log"))

    Handler.app = {"repo": repo, "token": token, "verbose": args.verbose, "audit": audit,
                   "jobs": JobManager(repo.root, os.path.join(repo.root, logstore.JOBS_DIR),
                                      on_finish=job_finished)}
    audit.log("gui.start", version=VERSION, read_only=repo.read_only, bind=args.host, port=args.port,
              migrated_job_files=migrated)
    if not repo.read_only:
        def maintenance_loop():
            while True:
                try:
                    run_maintenance(Handler.app)
                except Exception:  # noqa: BLE001
                    traceback_print()
                time.sleep(6 * 3600)
        threading.Thread(target=maintenance_loop, name="log-maintenance", daemon=True).start()
    try:
        httpd = ThreadingServer((args.host, args.port), Handler)
    except OSError as exc:
        sys.exit("cannot listen on %s:%d — %s (try --port)" % (args.host, args.port, exc))

    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = "http://%s:%d/#token=%s" % (shown_host, args.port, token)
    print("Tier 0 Configuration GUI %s" % VERSION)
    print("  repository : %s%s" % (repo.root, "  (READ-ONLY)" if repo.read_only else ""))
    print("  open       : %s" % url)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  WARNING    : listening on %s — anyone who can reach this port and has the token "
              "can edit configuration and run playbooks. Prefer an SSH tunnel." % args.host)
    else:
        print("  remote use : ssh -L %d:127.0.0.1:%d <user>@%s   then open the URL above"
              % (args.port, args.port, socket.gethostname()))
    print("  stop       : Ctrl-C")
    sys.stdout.flush()
    if not args.no_browser and os.environ.get("DISPLAY") or sys.platform == "darwin" and not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
