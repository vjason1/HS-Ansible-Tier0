"""Playbooks and utility scripts the GUI can run, and how to build their argv.

Nothing here ever goes through a shell: the GUI sends structured options, the
server validates them against these definitions, and the command is executed
as an argv list. Unknown playbooks, scripts or options are rejected.
"""

import re

# Option types: bool, int, str, enum(options), list (comma/newline separated)
TAGS_SITE = ["discovery", "precheck", "validation", "storage", "nvme", "existing-mounts", "raid",
             "filesystem", "nfs", "nfs-exports", "perf", "tuning", "sunrpc", "nfsd", "readahead",
             "irq", "tcp", "firewall", "hammerspace", "integration", "gpu-fabric-az",
             "azure-imds-az", "di", "di-precheck", "di-dependencies", "di-install", "di-selinux",
             "di-firewall", "di-services", "di-register", "di-activate", "di-container",
             "di-exports", "di-k8s", "di-k8s-build", "di-k8s-status"]

PLAYBOOKS = [
    {"id": "site.yml", "title": "Deploy Tier 0 (site.yml)",
     "description": "Full deployment: discovery → precheck → RAID → XFS → NFS → perf → firewall → "
                    "Hammerspace registration, then DI if enabled. Use Check mode first, or limit "
                    "with tags (discovery,precheck shows what WOULD be configured).",
     "destructive": "Creates RAID arrays and filesystems on the target nodes' data drives.",
     "tags": TAGS_SITE,
     "extra": [
         {"key": "deploy_di", "type": "bool", "label": "Deploy DI (deploy_di)"},
         {"key": "di_activate", "type": "bool", "label": "Activate DI (di_activate)"},
         {"key": "di_deployment_type", "type": "enum", "label": "DI mode override",
          "options": ["", "host", "container", "kubernetes"]},
         {"key": "hammerspace_serial", "type": "int", "label": "hammerspace_serial"},
     ]},
    {"id": "site-onprem-nvme.yml", "title": "Deploy on-prem NVMe (site-onprem-nvme.yml)",
     "description": "Bare-metal variant: NVMe + software RAID, no firewall configuration.",
     "destructive": "Creates RAID arrays and filesystems on the target nodes' data drives.",
     "tags": TAGS_SITE, "extra": []},
    {"id": "preflight_check.yml", "title": "Preflight: find new instances",
     "description": "Compares the inventory with nodes registered in Hammerspace and writes "
                    ".new_instances_limit + preflight_report.txt. Read-only.",
     "tags": [], "extra": []},
    {"id": "verify_nfs.yml", "title": "Verify NFS",
     "description": "Verification playbook for exports / mounts.", "tags": [], "extra": []},
    {"id": "os_upgrade_snapshot.yml", "title": "OS upgrade snapshot",
     "description": "Read-only configuration snapshot / diff. The OS Upgrade Check menu drives "
                    "this playbook with a guided workflow.",
     "tags": [], "extra": [
         {"key": "snapshot_phase", "type": "enum", "label": "Phase", "options": ["pre", "post", "diff"],
          "required": True},
         {"key": "snapshot_run", "type": "str", "label": "Run name"},
         {"key": "snapshot_hosts", "type": "str", "label": "Host pattern"},
         {"key": "snapshot_overwrite", "type": "bool", "label": "Overwrite existing baseline"},
     ]},
    {"id": "generate_instance_report.yml", "title": "Instance AZ report (CSV)",
     "description": "Writes instance_report.csv (display name, fault domain, AZ).", "tags": [], "extra": []},
    {"id": "collect_gpu_fabric.yml", "title": "Collect OCI GPU fabric data",
     "description": "Writes gpu_fabric_data.txt for assign_az_to_volumes.py.", "tags": [], "extra": []},
    {"id": "build_di_image.yml", "title": "Build DI container image tar",
     "description": "Builds payload/hammerspace-di-<arch>.tar on a build host (use Limit).",
     "tags": [], "extra": []},
    {"id": "plays/drop_caches_nfs.yml", "title": "Drop caches + restart NFS",
     "description": "Flushes page cache, compacts memory and bounces the NFS server.",
     "destructive": "Restarts the NFS server on the targets (brief client interruption).",
     "tags": [], "extra": [{"key": "target_hosts", "type": "str", "label": "target_hosts"}]},
    {"id": "decommission_di.yml", "title": "Decommission DI nodes",
     "description": "Evacuate volumes, deregister and tear down DI (serial: 1).",
     "destructive": "Removes DI nodes from the cluster.", "tags": [],
     "extra": [{"key": "deploy_di", "type": "bool", "label": "deploy_di (k8s mode needs true)"}]},
    {"id": "decommission_tier0.yml", "title": "Decommission Tier 0 (API only)",
     "description": "Removes nodes and volumes from Hammerspace via the API. Touches no data on "
                    "hosts. Without decommission_confirm it only prints the plan.",
     "destructive": "Deletes Hammerspace volumes and nodes.", "tags": [],
     "extra": [{"key": "decommission_confirm", "type": "bool", "label": "decommission_confirm"},
               {"key": "decommission_explicit_volume_group_leave", "type": "bool",
                "label": "Explicit volume-group leave"}]},
    {"id": "reset-tier0-host.yml", "title": "Reset Tier 0 host (full teardown)",
     "description": "Reverses site.yml: HS cleanup, guards, NFS, unmount, destroy RAID, wipe.",
     "destructive": "DESTROYS RAID arrays and wipes data drives.", "tags": [],
     "extra": [{"key": "reset_confirm", "type": "bool", "label": "reset_confirm", "required": True},
               {"key": "reset_hammerspace_cleanup", "type": "bool", "label": "Remove from Hammerspace"},
               {"key": "reset_remove_mount_dirs", "type": "bool", "label": "Remove mount dirs"},
               {"key": "reset_run_blkdiscard", "type": "bool", "label": "blkdiscard NVMe"},
               {"key": "reset_mount_root", "type": "str", "label": "Mount root"}]},
]
PLAYBOOKS_BY_ID = {p["id"]: p for p in PLAYBOOKS}

_HS_CONN = [
    {"key": "host", "flag": "--host", "type": "str", "label": "Anvil host", "required": True,
     "default_from": "hammerspace_api_host"},
    {"key": "port", "flag": "--port", "type": "int", "label": "Port", "default": 8443},
    {"key": "user", "flag": "--user", "type": "str", "label": "User", "required": True,
     "default_from": "hammerspace_api_user"},
    {"key": "password_file", "flag": "--password-file", "type": "str", "label": "Password file",
     "placeholder": "~/.hs_password"},
]
_NODE_FILTER = [
    {"key": "nodes", "flag": "--node", "type": "list", "label": "Node names (repeatable)"},
    {"key": "prefix", "flag": "--prefix", "type": "str", "label": "Name prefix"},
    {"key": "contains", "flag": "--contains", "type": "str", "label": "Name contains"},
    {"key": "pattern", "flag": "--pattern", "type": "str", "label": "Name regex"},
]

UTILITIES = [
    {"id": "cleanup_instance_nodes.py", "title": "Remove nodes & volumes",
     "description": "Removes nodes and their volumes from Hammerspace (each volume delete is "
                    "awaited; a node is removed only after all its volumes are gone).",
     "destructive": True,
     "options": _HS_CONN + _NODE_FILTER + [
         {"key": "list_nodes", "flag": "--list-nodes", "type": "bool", "label": "Just list nodes"},
         {"key": "parallel", "flag": "--parallel", "type": "int", "label": "Parallel deletes", "default": 1},
         {"key": "dry_run", "flag": "--dry-run", "type": "bool", "label": "Dry run", "default": True}]},
    {"id": "set_availability_drop.py", "title": "Availability drop (RMA / maintenance)",
     "description": "Disable availability-drop before a planned shutdown so volumes rejoin "
                    "automatically; re-enable afterwards; or run a health check.",
     "destructive": True,
     "options": _HS_CONN + _NODE_FILTER + [
         {"key": "all_nodes", "flag": "--all-nodes", "type": "bool", "label": "All nodes"},
         {"key": "action", "type": "enum", "label": "Action", "required": True,
          "options": ["--check", "--disable", "--enable", "--health-check"]},
         {"key": "dry_run", "flag": "--dry-run", "type": "bool", "label": "Dry run", "default": True}]},
    {"id": "set_volume_thresholds.py", "title": "Volume thresholds",
     "description": "Check or change high/low utilization thresholds on existing volumes.",
     "destructive": True,
     "options": _HS_CONN + _NODE_FILTER + [
         {"key": "instances_file", "flag": "--instances-file", "type": "str", "label": "Instances file"},
         {"key": "all_nodes", "flag": "--all-nodes", "type": "bool", "label": "All nodes"},
         {"key": "check", "flag": "--check", "type": "bool", "label": "Only show current values"},
         {"key": "high", "flag": "--high", "type": "str", "label": "High (e.g. 0.98 or 98)"},
         {"key": "low", "flag": "--low", "type": "str", "label": "Low (e.g. 0.90 or 90)"},
         {"key": "dry_run", "flag": "--dry-run", "type": "bool", "label": "Dry run", "default": True}]},
    {"id": "add_volumes_to_group.py", "title": "Add volumes to a volume group",
     "description": "Adds the volumes of instances listed in a file to an existing group.",
     "destructive": True,
     "options": _HS_CONN + [
         {"key": "group", "flag": "--group", "type": "str", "label": "Volume group", "required": True},
         {"key": "instances_file", "flag": "--instances-file", "type": "str", "label": "Instances file",
          "default": "tier0_instances_limit"},
         {"key": "az", "flag": "--az", "type": "str", "label": "Only AZ (e.g. AZ1)"},
         {"key": "list", "flag": "--list", "type": "bool", "label": "Just list members"},
         {"key": "dry_run", "flag": "--dry-run", "type": "bool", "label": "Dry run", "default": True}]},
    {"id": "assign_az_to_volumes.py", "title": "Assign AZ prefixes (OCI GPU fabric)",
     "description": "Renames volumes with an AZ prefix from GPU memory fabric or fault domain.",
     "destructive": True,
     "options": _HS_CONN + [
         {"key": "gpu_fabric_file", "flag": "--gpu-fabric-file", "type": "str", "label": "GPU fabric file",
          "default": "gpu_fabric_data.txt"},
         {"key": "compartment_id", "flag": "--compartment-id", "type": "str", "label": "Compartment OCID"},
         {"key": "az_source", "flag": "--az-source", "type": "enum", "label": "AZ source",
          "options": ["gpu_fabric", "fault_domain"], "default": "gpu_fabric"},
         {"key": "az_maps", "flag": "--az-map", "type": "list", "label": "Explicit FABRIC_OCID=AZ#"},
         {"key": "report_only", "flag": "--report-only", "type": "bool", "label": "Report only"},
         {"key": "dry_run", "flag": "--dry-run", "type": "bool", "label": "Dry run", "default": True}]},
    {"id": "rename_oci_instances_az.py", "title": "Rename OCI instances with AZ",
     "description": "Prepends the Hammerspace AZ to OCI instance display names.",
     "destructive": True,
     "options": _HS_CONN + [
         {"key": "compartment_id", "flag": "--compartment-id", "type": "str", "label": "Compartment OCID",
          "required": True},
         {"key": "instances_file", "flag": "--instances-file", "type": "str", "label": "Instances file"},
         {"key": "name_pattern", "flag": "--name-pattern", "type": "str", "label": "Name regex"},
         {"key": "skip_existing", "flag": "--skip-existing", "type": "bool", "label": "Skip already renamed"},
         {"key": "dry_run", "flag": "--dry-run", "type": "bool", "label": "Dry run", "default": True}]},
]
UTILITIES_BY_ID = {u["id"]: u for u in UTILITIES}

_SAFE_TEXT = re.compile(r"^[^\x00-\x1f]*$")


class CatalogError(ValueError):
    pass


def _text(value, label):
    value = "" if value is None else str(value).strip()
    if not _SAFE_TEXT.match(value):
        raise CatalogError("%s contains control characters" % label)
    return value


def build_playbook_argv(repo, req, inventory_files):
    """Return (argv, extra_vars, destructive_reason) for a playbook request."""
    pb = PLAYBOOKS_BY_ID.get(req.get("playbook"))
    if not pb:
        raise CatalogError("unknown playbook %r" % req.get("playbook"))
    argv = ["%s/scripts/run.sh" % repo, "playbook", pb["id"]]

    invs = req.get("inventory") or []
    if isinstance(invs, str):
        invs = [invs]
    for inv in invs:
        if inv not in inventory_files:
            raise CatalogError("unknown inventory %r" % inv)
        argv += ["-i", inv]

    limit = _text(req.get("limit"), "limit")
    if limit:
        if limit.startswith("-"):
            raise CatalogError("limit may not start with '-'")
        argv += ["--limit", limit]

    allowed_tags = set(pb["tags"]) | {"always"}
    for flag, key in (("--tags", "tags"), ("--skip-tags", "skip_tags")):
        tags = [t for t in (req.get(key) or []) if t]
        bad = [t for t in tags if t not in allowed_tags]
        if bad:
            raise CatalogError("unknown tag(s): %s" % ", ".join(bad))
        if tags:
            argv += [flag, ",".join(tags)]

    if req.get("check"):
        argv.append("--check")
    if req.get("diff"):
        argv.append("--diff")
    verb = int(req.get("verbosity") or 0)
    if verb:
        argv.append("-" + "v" * max(1, min(verb, 4)))

    extra = {}
    for opt in pb["extra"]:
        if opt["key"] not in (req.get("extra") or {}):
            continue
        raw = req["extra"][opt["key"]]
        if raw in (None, ""):
            continue
        if opt["type"] == "bool":
            extra[opt["key"]] = bool(raw)
        elif opt["type"] == "int":
            extra[opt["key"]] = int(raw)
        elif opt["type"] == "enum":
            if raw not in opt["options"]:
                raise CatalogError("bad value for %s" % opt["key"])
            extra[opt["key"]] = raw
        else:
            extra[opt["key"]] = _text(raw, opt["key"])
    for opt in pb["extra"]:
        if opt.get("required") and opt["key"] not in extra:
            raise CatalogError("%s is required" % opt["label"])

    # Free-form extra vars: KEY=VALUE lines, strictly validated names.
    for line in (req.get("extra_vars_text") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CatalogError("extra var line must be key=value: %r" % line)
        k, v = line.split("=", 1)
        k = k.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            raise CatalogError("bad extra var name %r" % k)
        extra[k] = _coerce(v.strip())

    destructive = pb.get("destructive") if not req.get("check") else None
    return argv, extra, destructive


def _coerce(v):
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if re.match(r"^-?\d+$", v):
        return int(v)
    return v


def build_utility_argv(repo, req, python="python3"):
    ut = UTILITIES_BY_ID.get(req.get("script"))
    if not ut:
        raise CatalogError("unknown script %r" % req.get("script"))
    argv = [python, "%s/%s" % (repo, ut["id"])]
    values = req.get("options") or {}
    dry = False
    for opt in ut["options"]:
        val = values.get(opt["key"], opt.get("default"))
        if opt["type"] == "bool":
            if val:
                argv.append(opt["flag"])
                if opt["key"] == "dry_run":
                    dry = True
            continue
        if val in (None, "", []):
            if opt.get("required"):
                raise CatalogError("%s is required" % opt["label"])
            continue
        if opt["type"] == "enum":
            if val not in opt["options"]:
                raise CatalogError("bad value for %s" % opt["label"])
            argv.append(val) if "flag" not in opt else argv.extend([opt["flag"], val])
        elif opt["type"] == "int":
            argv += [opt["flag"], str(int(val))]
        elif opt["type"] == "list":
            items = val if isinstance(val, list) else re.split(r"[,\n]", str(val))
            for item in items:
                item = _text(item, opt["label"])
                if item:
                    argv += [opt["flag"], item]
        else:
            text = _text(val, opt["label"])
            if text.startswith("-"):
                raise CatalogError("%s may not start with '-'" % opt["label"])
            argv += [opt["flag"], text]
    # Scripts prompt for confirmation on real changes; there is no TTY here,
    # so the GUI confirms instead and passes --yes.
    if not dry:
        argv.append("--yes")
    return argv, (ut["destructive"] and not dry)
