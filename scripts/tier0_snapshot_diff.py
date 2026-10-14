#!/usr/bin/env python3
"""Compare two Tier 0 configuration snapshots and report what changed.

Snapshots are produced by scripts/tier0_config_snapshot.sh (directly, or via
os_upgrade_snapshot.yml). Typical use is before/after an OS upgrade:

    python3 scripts/tier0_snapshot_diff.py pre.txt post.txt
    python3 scripts/tier0_snapshot_diff.py pre.txt post.txt \\
        --html diff.html --json diff.json --text diff.txt

    # Fleet summary over a run directory (<run>/<host>/diff.json):
    python3 scripts/tier0_snapshot_diff.py --summary snapshots/os-upgrade

Instead of one big text diff, each section is compared the way that makes
sense for it:

  * packages.installed  -> upgraded / downgraded / added / removed table
  * key=value sections  -> per-key before/after table (sysctl, nfsd runtime,
                           summary facts, DI state, ...)
  * everything else     -> unified diff

Every section carries a severity (critical / warning / info) from
SECTION_RULES so the report leads with what can break Tier 0 (RAID, mounts,
exports, nfsd runtime) and keeps expected upgrade churn (kernel version,
package versions) out of the way.

Exit codes:  0 = no differences   1 = differences found   2 = usage/parse error
Python 3.6+ standard library only (runs on any Ansible controller).
"""

import argparse
import difflib
import html
import json
import os
import re
import sys
from collections import OrderedDict

FORMAT_MARKER = "tier0-config-snapshot"
SECTION_RE = re.compile(r"^===== SECTION: (?P<name>.+?) =====$")
END_RE = re.compile(r"^===== END =====$")

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

# (section-name prefix, severity, kind, what a change here means)
# First matching prefix wins, so list specific names before broad prefixes.
SECTION_RULES = [
    ("summary.key_facts", "warning", "kv",
     "Headline values (kernel, nfsd threads, md/mount/export counts, failed units)."),
    ("raid.initramfs", "critical", "text",
     "mdadm / RAID modules missing from the new kernel's initramfs -> arrays may not "
     "assemble at boot, or assemble as md127."),
    ("raid.", "critical", "text",
     "RAID array membership, state or mdadm.conf changed. Verify every array is clean "
     "and still mounted at the same path."),
    ("fs.tier0_mounts", "critical", "text",
     "A Tier 0 data mount changed (device, XFS geometry, PrimaryData). Hammerspace may "
     "mark the volume suspected."),
    ("fs.", "critical", "text",
     "fstab or the mount table changed. Check Tier 0 mounts and their options."),
    ("nfs.exports", "critical", "text",
     "Active or configured NFS exports changed. Hammerspace and DI need these exact exports."),
    ("nfs.runtime", "critical", "kv",
     "nfsd runtime (threads, versions, max_block_size, sunrpc slots) changed — "
     "typically a tuning that did not survive the upgrade/reboot."),
    ("nfs.", "warning", "text",
     "NFS server configuration or RPC services changed."),
    ("storage.nvme_controllers", "critical", "text",
     "NVMe controllers changed. A different ctrl name on the same serial means the "
     "kernel renumbered devices."),
    ("storage.block_devices", "critical", "text",
     "Block device inventory changed (disks, UUIDs, labels, mountpoints)."),
    ("storage.queue_settings", "warning", "text",
     "Block-queue tunables (scheduler, read-ahead, nr_requests) reverted or changed."),
    ("services.failed", "critical", "text",
     "Units in the failed state changed."),
    ("services.key_units", "warning", "text",
     "Enablement/activity of NFS, mdmonitor, irqbalance, Hammerspace guard/watchdog or DI "
     "units changed."),
    ("services.tier0_unit_files", "warning", "text",
     "Tier 0 systemd unit files (guards, remount watchdog, IRQ pinning) changed."),
    ("services.", "info", "text",
     "Set of enabled unit files changed (often expected after an upgrade)."),
    ("perf.", "warning", "text",
     "Hammerspace performance-tuning artifacts (sunrpc/nfsd modprobe, sysctl, udev "
     "read-ahead, IRQ pinning) changed or were removed."),
    ("packages.config_leftovers", "warning", "text",
     "New .rpmnew/.rpmsave/.dpkg-* files: the package manager did not merge a config "
     "file. Review each one."),
    ("packages.installed", "info", "packages",
     "Package versions (expected to change during an OS upgrade)."),
    ("packages.", "info", "text", "Package manager / repository configuration."),
    ("kernel.module_params", "warning", "text",
     "Kernel module parameters / modprobe.d options changed."),
    ("kernel.sysctl_files", "warning", "text",
     "Persistent sysctl configuration files changed."),
    ("kernel.sysctl", "warning", "kv", "Kernel sysctl values changed."),
    ("kernel.cmdline", "warning", "text", "Kernel boot parameters changed."),
    ("kernel.tainted", "warning", "kv", "Kernel taint flags changed (out-of-tree / unsigned modules)."),
    ("kernel.", "info", "text", "Kernel version / installed kernels / loaded modules."),
    ("net.links", "critical", "text",
     "Network interfaces changed (name, MTU, state). Interface renames break IRQ pinning, "
     "exports by IP, and RDMA."),
    ("net.nic_drivers", "warning", "text",
     "NIC driver or firmware changed (e.g. inbox vs OFED)."),
    ("net.rdma", "warning", "text", "RDMA stack / links changed."),
    ("net.", "warning", "text", "Network addressing, routes, offloads or /etc/hosts changed."),
    ("firewall.", "warning", "text", "Firewall rules changed."),
    ("security.", "warning", "text", "SELinux / AppArmor state changed."),
    ("di.", "warning", "kv", "Data Instantiator packages / containers changed."),
    ("hardware.", "info", "text", "CPU / governor / THP / tuned profile."),
    ("os.", "info", "text", "OS release (expected to change during an OS upgrade)."),
    ("time.", "info", "kv", "Time synchronization settings."),
]

# Sections whose lines are "key<sep>value" pairs.
KV_SEPARATORS = {
    "kernel.sysctl": " = ",
    "kernel.tainted": "=",
}
DEFAULT_KV_SEP = "="

MAX_DIFF_LINES = 400   # per section in text/HTML output (JSON is complete)


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------
class SnapshotError(Exception):
    pass


def parse_snapshot(path):
    """Return (meta, OrderedDict[section -> list[str]])."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        raise SnapshotError("cannot read %s: %s" % (path, exc))

    meta = OrderedDict()
    sections = OrderedDict()
    current = None
    saw_marker = False
    for raw in lines:
        if raw.startswith("#! "):
            body = raw[3:]
            if body.startswith(FORMAT_MARKER):
                saw_marker = True
                meta["format"] = body.split("format=", 1)[-1].strip()
                continue
            if ":" in body:
                key, val = body.split(":", 1)
                meta[key.strip()] = val.strip()
            continue
        if END_RE.match(raw):
            current = None
            continue
        m = SECTION_RE.match(raw)
        if m:
            current = m.group("name")
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(raw)

    if not saw_marker:
        raise SnapshotError(
            "%s is not a tier0 config snapshot (missing '#! %s' header)" % (path, FORMAT_MARKER))
    # Trim trailing blank lines inside each section (cosmetic separators).
    for name, body in sections.items():
        while body and not body[-1].strip():
            body.pop()
    return meta, sections


def rule_for(section):
    for prefix, severity, kind, desc in SECTION_RULES:
        if section == prefix or section.startswith(prefix):
            return severity, kind, desc
    return "info", "text", "Other configuration."


# ----------------------------------------------------------------------------
# Comparators
# ----------------------------------------------------------------------------
def kv_parse(lines, sep):
    out = OrderedDict()
    for line in lines:
        if sep in line:
            k, v = line.split(sep, 1)
            out[k.strip()] = v.strip()
        elif line.strip():
            out[line.strip()] = ""
    return out


def compare_kv(pre, post, sep):
    a, b = kv_parse(pre, sep), kv_parse(post, sep)
    changed, added, removed = [], [], []
    for k in a:
        if k not in b:
            removed.append({"key": k, "before": a[k]})
        elif a[k] != b[k]:
            changed.append({"key": k, "before": a[k], "after": b[k]})
    for k in b:
        if k not in a:
            added.append({"key": k, "after": b[k]})
    return {"changed": changed, "added": added, "removed": removed}


_VER_SPLIT = re.compile(r"(\d+|[A-Za-z]+)")


def _ver_key(version):
    """Rough rpm/dpkg-style version ordering, good enough to label direction."""
    epoch = 0
    if ":" in version:
        e, version = version.split(":", 1)
        try:
            epoch = int(e or 0)
        except ValueError:
            epoch = 0
    parts = []
    for tok in _VER_SPLIT.findall(version):
        parts.append((1, int(tok), "") if tok.isdigit() else (0, 0, tok))
    return (epoch, parts)


def compare_packages(pre, post):
    def index(lines):
        idx = OrderedDict()
        for line in lines:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            idx.setdefault(parts[0], set()).add(parts[1].strip())
        return idx

    a, b = index(pre), index(post)
    upgraded, downgraded, added, removed, multi = [], [], [], [], []
    for name in sorted(set(a) | set(b)):
        va, vb = a.get(name, set()), b.get(name, set())
        if va == vb:
            continue
        if not va:
            added.append({"name": name, "after": ", ".join(sorted(vb))})
        elif not vb:
            removed.append({"name": name, "before": ", ".join(sorted(va))})
        elif len(va) == 1 and len(vb) == 1:
            old, new = next(iter(va)), next(iter(vb))
            rec = {"name": name, "before": old, "after": new}
            (upgraded if _ver_key(new) >= _ver_key(old) else downgraded).append(rec)
        else:
            # Multi-version packages (kernel, kernel-core, ...)
            multi.append({"name": name,
                          "versions_added": sorted(vb - va, key=_ver_key),
                          "versions_removed": sorted(va - vb, key=_ver_key)})
    return {"upgraded": upgraded, "downgraded": downgraded, "added": added,
            "removed": removed, "multi_version": multi,
            "total_before": sum(len(v) for v in a.values()),
            "total_after": sum(len(v) for v in b.values())}


def compare_text(pre, post):
    diff = list(difflib.unified_diff(pre, post, fromfile="before", tofile="after",
                                     lineterm="", n=2))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return {"diff": diff, "lines_added": added, "lines_removed": removed}


# ----------------------------------------------------------------------------
# Top-level comparison
# ----------------------------------------------------------------------------
def compare(pre_path, post_path):
    pre_meta, pre = parse_snapshot(pre_path)
    post_meta, post = parse_snapshot(post_path)

    names = list(pre.keys()) + [n for n in post.keys() if n not in pre]
    results = []
    for name in names:
        severity, kind, desc = rule_for(name)
        entry = OrderedDict([("name", name), ("severity", severity), ("kind", kind),
                             ("description", desc)])
        if name not in post:
            entry["status"] = "removed"
            entry["detail"] = {"diff": ["-" + l for l in pre[name]]}
        elif name not in pre:
            entry["status"] = "added"
            entry["detail"] = {"diff": ["+" + l for l in post[name]]}
        elif pre[name] == post[name]:
            entry["status"] = "unchanged"
        else:
            entry["status"] = "changed"
            if kind == "packages":
                entry["detail"] = compare_packages(pre[name], post[name])
            elif kind == "kv":
                sep = KV_SEPARATORS.get(name, DEFAULT_KV_SEP)
                entry["detail"] = compare_kv(pre[name], post[name], sep)
                entry["detail"]["diff"] = compare_text(pre[name], post[name])["diff"]
            else:
                entry["detail"] = compare_text(pre[name], post[name])
        results.append(entry)

    changed = [r for r in results if r["status"] != "unchanged"]
    counts = {s: sum(1 for r in changed if r["severity"] == s) for s in SEVERITY_ORDER}

    facts_pre = kv_parse(pre.get("summary.key_facts", []), "=")
    facts_post = kv_parse(post.get("summary.key_facts", []), "=")
    key_facts = [{"key": k, "before": facts_pre.get(k, ""), "after": facts_post.get(k, ""),
                  "changed": facts_pre.get(k) != facts_post.get(k)}
                 for k in list(facts_pre) + [k for k in facts_post if k not in facts_pre]]

    warnings = []
    if pre_meta.get("running_as_root") == "no" or post_meta.get("running_as_root") == "no":
        warnings.append("At least one snapshot was taken without root; some sections may be "
                        "incomplete and show spurious differences.")
    if pre_meta.get("host") and post_meta.get("host") and pre_meta["host"] != post_meta["host"]:
        warnings.append("Snapshots come from different hosts (%s vs %s)."
                        % (pre_meta["host"], post_meta["host"]))
    if pre_meta.get("boot_id") and pre_meta.get("boot_id") == post_meta.get("boot_id"):
        warnings.append("Both snapshots are from the same boot — the node has not been "
                        "rebooted since the 'before' snapshot, so the new kernel and any "
                        "boot-time configuration have not been exercised yet.")

    return OrderedDict([
        ("tool", "tier0_snapshot_diff"),
        ("before", OrderedDict([("file", os.path.abspath(pre_path)), ("meta", pre_meta)])),
        ("after", OrderedDict([("file", os.path.abspath(post_path)), ("meta", post_meta)])),
        ("host", post_meta.get("host") or pre_meta.get("host") or "unknown"),
        ("identical", not changed),
        ("counts", OrderedDict([("sections_total", len(results)),
                                ("sections_changed", len(changed)),
                                ("critical", counts["critical"]),
                                ("warning", counts["warning"]),
                                ("info", counts["info"])])),
        ("warnings", warnings),
        ("key_facts", key_facts),
        ("attention", attention_items(changed)),
        ("sections", results),
    ])


def attention_items(changed):
    """Short human-readable bullets for the top of the report."""
    items = []
    for r in sorted(changed, key=lambda r: (SEVERITY_ORDER[r["severity"]], r["name"])):
        if r["severity"] == "info":
            continue
        d = r.get("detail", {})
        line = "[%s] %s: %s" % (r["severity"].upper(), r["name"], r["status"])
        if r["name"] == "packages.config_leftovers":
            new = [l[1:] for l in d.get("diff", []) if l.startswith("+") and not l.startswith("+++")
                   and "(end of list)" not in l]
            if new:
                line += " — new: " + ", ".join(new[:8]) + (" ..." if len(new) > 8 else "")
        elif r["kind"] == "kv" and d.get("changed"):
            parts = ["%s %s -> %s" % (c["key"], c["before"] or "''", c["after"] or "''")
                     for c in d["changed"][:5]]
            line += " — " + "; ".join(parts) + (" ..." if len(d["changed"]) > 5 else "")
        elif "lines_added" in d:
            line += " (+%d/-%d lines)" % (d["lines_added"], d["lines_removed"])
        items.append(line)
    return items


# ----------------------------------------------------------------------------
# Renderers
# ----------------------------------------------------------------------------
def render_text(res):
    out = []
    w = out.append
    w("=" * 78)
    w("TIER 0 CONFIGURATION DIFF — %s" % res["host"])
    w("=" * 78)
    bm, am = res["before"]["meta"], res["after"]["meta"]
    w("Before : %s  (captured %s, kernel %s)" % (res["before"]["file"], bm.get("captured_utc", "?"),
                                                bm.get("kernel", "?")))
    w("After  : %s  (captured %s, kernel %s)" % (res["after"]["file"], am.get("captured_utc", "?"),
                                                am.get("kernel", "?")))
    c = res["counts"]
    w("")
    if res["identical"]:
        w("RESULT: NO CONFIGURATION CHANGES DETECTED (%d sections compared)" % c["sections_total"])
    else:
        w("RESULT: %d of %d sections changed — critical: %d, warning: %d, info: %d"
          % (c["sections_changed"], c["sections_total"], c["critical"], c["warning"], c["info"]))
    for wmsg in res["warnings"]:
        w("NOTE: " + wmsg)

    if res["key_facts"]:
        w("")
        w("KEY FACTS")
        w("-" * 78)
        kw = max(len(f["key"]) for f in res["key_facts"])
        for f in res["key_facts"]:
            mark = "*" if f["changed"] else " "
            if f["changed"]:
                w("%s %-*s  %s  ->  %s" % (mark, kw, f["key"], f["before"], f["after"]))
            else:
                w("%s %-*s  %s" % (mark, kw, f["key"], f["after"]))

    if res["attention"]:
        w("")
        w("NEEDS ATTENTION")
        w("-" * 78)
        for item in res["attention"]:
            w("  " + item)

    changed = [s for s in res["sections"] if s["status"] != "unchanged"]
    changed.sort(key=lambda s: (SEVERITY_ORDER[s["severity"]], s["name"]))
    for s in changed:
        w("")
        w("#" * 78)
        w("# [%s] %s — %s" % (s["severity"].upper(), s["name"], s["status"]))
        w("# %s" % s["description"])
        w("#" * 78)
        d = s.get("detail", {})
        if s["kind"] == "packages" and s["status"] == "changed":
            w("packages: %d before, %d after" % (d["total_before"], d["total_after"]))
            for label, key in (("UPGRADED", "upgraded"), ("DOWNGRADED", "downgraded")):
                if d[key]:
                    w("%s (%d):" % (label, len(d[key])))
                    for p in d[key]:
                        w("  %-45s %s -> %s" % (p["name"], p["before"], p["after"]))
            if d["added"]:
                w("ADDED (%d):" % len(d["added"]))
                for p in d["added"]:
                    w("  %-45s %s" % (p["name"], p["after"]))
            if d["removed"]:
                w("REMOVED (%d):" % len(d["removed"]))
                for p in d["removed"]:
                    w("  %-45s %s" % (p["name"], p["before"]))
            for p in d["multi_version"]:
                w("MULTI-VERSION %s: +%s  -%s" % (p["name"], ", ".join(p["versions_added"]) or "none",
                                                  ", ".join(p["versions_removed"]) or "none"))
            continue
        if s["kind"] == "kv" and s["status"] == "changed":
            for c2 in d["changed"]:
                w("  ~ %s: %s -> %s" % (c2["key"], c2["before"], c2["after"]))
            for a in d["added"]:
                w("  + %s: %s" % (a["key"], a["after"]))
            for r in d["removed"]:
                w("  - %s: %s" % (r["key"], r["before"]))
            continue
        lines = d.get("diff", [])
        for l in lines[:MAX_DIFF_LINES]:
            w(l)
        if len(lines) > MAX_DIFF_LINES:
            w("... %d more diff lines (see JSON report)" % (len(lines) - MAX_DIFF_LINES))
    w("")
    unchanged = [s["name"] for s in res["sections"] if s["status"] == "unchanged"]
    if unchanged:
        w("UNCHANGED SECTIONS (%d): %s" % (len(unchanged), ", ".join(unchanged)))
    return "\n".join(out) + "\n"


_HTML_CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;
--crit:#b42318;--warn:#b54708;--info:#175cd3;--add:#e6f4ea;--del:#fdecea;--addk:#1e7b34;--delk:#a3271c}
:root:not([data-theme=light]){color-scheme:light dark}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#151514;--panel:#1f1f1d;--ink:#ecebe6;
--muted:#9a9993;--line:#34332f;--crit:#f97066;--warn:#fdb022;--info:#84adff;--add:#14301d;--del:#3a1714;
--addk:#7ad98f;--delk:#ff9b8f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.sub{color:var(--muted);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:16px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card b{display:block;font-size:22px}.crit{color:var(--crit)}.warn{color:var(--warn)}.info{color:var(--info)}
.ok{color:var(--addk)}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13px}
th{color:var(--muted);font-weight:600}
tr.chg td{background:color-mix(in srgb,var(--warn) 10%,transparent)}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
details{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin:8px 0}
summary{cursor:pointer;padding:10px 12px;font-weight:600;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
summary .d{font-weight:400;color:var(--muted);font-size:12px;flex-basis:100%}
.badge{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid currentColor;text-transform:uppercase}
pre{margin:0;padding:10px 12px;overflow-x:auto;border-top:1px solid var(--line)}
.l-add{background:var(--add);color:var(--addk);display:block}.l-del{background:var(--del);color:var(--delk);display:block}
.l-hdr{color:var(--muted);display:block}
ul.att{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px 10px 30px;margin:0}
.note{background:color-mix(in srgb,var(--warn) 12%,var(--panel));border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:8px 0}
"""


def render_html(res):
    e = html.escape
    bm, am = res["before"]["meta"], res["after"]["meta"]
    c = res["counts"]
    parts = ["<!doctype html><html><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<title>Config Diff %s</title><style>%s</style></head><body><main>" % (e(res["host"]), _HTML_CSS),
             "<h1>Configuration diff — %s</h1>" % e(res["host"]),
             "<div class='sub'>Before: %s · kernel %s<br>After: %s · kernel %s</div>" % (
                 e(bm.get("captured_utc", "?")), e(bm.get("kernel", "?")),
                 e(am.get("captured_utc", "?")), e(am.get("kernel", "?")))]
    if res["identical"]:
        parts.append("<div class='cards'><div class='card'><b class='ok'>No changes</b>%d sections compared</div></div>"
                     % c["sections_total"])
    else:
        parts.append("<div class='cards'>"
                     "<div class='card'><b>%d / %d</b>sections changed</div>"
                     "<div class='card'><b class='crit'>%d</b>critical</div>"
                     "<div class='card'><b class='warn'>%d</b>warning</div>"
                     "<div class='card'><b class='info'>%d</b>info</div></div>"
                     % (c["sections_changed"], c["sections_total"], c["critical"], c["warning"], c["info"]))
    for wmsg in res["warnings"]:
        parts.append("<div class='note'>%s</div>" % e(wmsg))
    if res["key_facts"]:
        parts.append("<h2>Key facts</h2><table><tr><th>Item</th><th>Before</th><th>After</th></tr>")
        for f in res["key_facts"]:
            parts.append("<tr class='%s'><td><code>%s</code></td><td>%s</td><td>%s</td></tr>" % (
                "chg" if f["changed"] else "", e(f["key"]), e(f["before"]), e(f["after"])))
        parts.append("</table>")
    if res["attention"]:
        parts.append("<h2>Needs attention</h2><ul class='att'>")
        parts.extend("<li>%s</li>" % e(i) for i in res["attention"])
        parts.append("</ul>")

    changed = sorted([s for s in res["sections"] if s["status"] != "unchanged"],
                     key=lambda s: (SEVERITY_ORDER[s["severity"]], s["name"]))
    if changed:
        parts.append("<h2>Changed sections</h2>")
    sev_cls = {"critical": "crit", "warning": "warn", "info": "info"}
    for s in changed:
        d = s.get("detail", {})
        opened = " open" if s["severity"] == "critical" else ""
        parts.append("<details%s><summary><span class='badge %s'>%s</span><code>%s</code> %s"
                     "<span class='d'>%s</span></summary>" % (
                         opened, sev_cls[s["severity"]], e(s["severity"]), e(s["name"]),
                         e(s["status"]), e(s["description"])))
        if s["kind"] == "packages" and s["status"] == "changed":
            parts.append("<pre>%d packages before, %d after</pre><table><tr><th>Change</th><th>Package</th>"
                         "<th>Before</th><th>After</th></tr>" % (d["total_before"], d["total_after"]))
            for label, key in (("upgraded", "upgraded"), ("downgraded", "downgraded")):
                for p in d[key]:
                    parts.append("<tr><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td></tr>"
                                 % (label, e(p["name"]), e(p["before"]), e(p["after"])))
            for p in d["added"]:
                parts.append("<tr><td>added</td><td><code>%s</code></td><td></td><td>%s</td></tr>"
                             % (e(p["name"]), e(p["after"])))
            for p in d["removed"]:
                parts.append("<tr><td>removed</td><td><code>%s</code></td><td>%s</td><td></td></tr>"
                             % (e(p["name"]), e(p["before"])))
            for p in d["multi_version"]:
                parts.append("<tr><td>multi-version</td><td><code>%s</code></td><td>-%s</td><td>+%s</td></tr>"
                             % (e(p["name"]), e(", ".join(p["versions_removed"])),
                                e(", ".join(p["versions_added"]))))
            parts.append("</table></details>")
            continue
        if s["kind"] == "kv" and s["status"] == "changed":
            parts.append("<table><tr><th>Key</th><th>Before</th><th>After</th></tr>")
            for c2 in d["changed"]:
                parts.append("<tr class='chg'><td><code>%s</code></td><td>%s</td><td>%s</td></tr>"
                             % (e(c2["key"]), e(c2["before"]), e(c2["after"])))
            for a in d["added"]:
                parts.append("<tr><td><code>%s</code></td><td><i>absent</i></td><td>%s</td></tr>"
                             % (e(a["key"]), e(a["after"])))
            for r in d["removed"]:
                parts.append("<tr><td><code>%s</code></td><td>%s</td><td><i>absent</i></td></tr>"
                             % (e(r["key"]), e(r["before"])))
            parts.append("</table></details>")
            continue
        lines = d.get("diff", [])
        parts.append("<pre>")
        for l in lines[:MAX_DIFF_LINES]:
            cls = "l-hdr" if l.startswith(("---", "+++", "@@")) else (
                "l-add" if l.startswith("+") else "l-del" if l.startswith("-") else "")
            parts.append("<span class='%s'>%s</span>\n" % (cls, e(l)) if cls else e(l) + "\n")
        if len(lines) > MAX_DIFF_LINES:
            parts.append("… %d more lines (see JSON report)\n" % (len(lines) - MAX_DIFF_LINES))
        parts.append("</pre></details>")
    unchanged = [s["name"] for s in res["sections"] if s["status"] == "unchanged"]
    if unchanged:
        parts.append("<h2>Unchanged sections (%d)</h2><div class='sub'>%s</div>"
                     % (len(unchanged), e(", ".join(unchanged))))
    parts.append("</main></body></html>")
    return "".join(parts)


# ----------------------------------------------------------------------------
# Fleet summary
# ----------------------------------------------------------------------------
def summarize_run(run_dir):
    hosts = []
    for host in sorted(os.listdir(run_dir)):
        jpath = os.path.join(run_dir, host, "diff.json")
        if not os.path.isfile(jpath):
            hosts.append({"host": host, "status": "no-diff",
                          "pre": os.path.isfile(os.path.join(run_dir, host, "pre.txt")),
                          "post": os.path.isfile(os.path.join(run_dir, host, "post.txt"))})
            continue
        with open(jpath, "r", encoding="utf-8") as fh:
            res = json.load(fh)
        c = res["counts"]
        facts = {f["key"]: f for f in res.get("key_facts", [])}
        k = facts.get("kernel", {})
        hosts.append({"host": host,
                      "status": "identical" if res["identical"] else (
                          "critical" if c["critical"] else "warning" if c["warning"] else "info"),
                      "critical": c["critical"], "warning": c["warning"], "info": c["info"],
                      "kernel_before": k.get("before", ""), "kernel_after": k.get("after", ""),
                      "attention": res.get("attention", [])})
    return hosts


def render_summary(run_dir, hosts):
    out = ["=" * 78, "OS UPGRADE CONFIG DIFF — FLEET SUMMARY", "Run: %s" % os.path.abspath(run_dir),
           "=" * 78, "%-34s %-10s %4s %4s %4s  %s" % ("HOST", "STATUS", "CRIT", "WARN", "INFO", "KERNEL")]
    for h in hosts:
        if h["status"] == "no-diff":
            out.append("%-34s %-10s %s" % (h["host"], "pending",
                                           "pre=%s post=%s" % ("yes" if h["pre"] else "no",
                                                               "yes" if h["post"] else "no")))
            continue
        kern = h["kernel_after"] if h["kernel_before"] == h["kernel_after"] else \
            "%s -> %s" % (h["kernel_before"], h["kernel_after"])
        out.append("%-34s %-10s %4d %4d %4d  %s" % (h["host"], h["status"], h["critical"],
                                                    h["warning"], h["info"], kern))
    flagged = [h for h in hosts if h.get("attention")]
    if flagged:
        out += ["", "NEEDS ATTENTION", "-" * 78]
        for h in flagged:
            out.append("%s:" % h["host"])
            out.extend("  " + a for a in h["attention"])
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before", nargs="?", help="'before' snapshot (e.g. pre.txt)")
    ap.add_argument("after", nargs="?", help="'after' snapshot (e.g. post.txt)")
    ap.add_argument("--text", metavar="FILE", help="write the text report to FILE")
    ap.add_argument("--html", metavar="FILE", help="write a self-contained HTML report to FILE")
    ap.add_argument("--json", metavar="FILE", help="write the full machine-readable result to FILE")
    ap.add_argument("--quiet", "-q", action="store_true", help="do not print the text report")
    ap.add_argument("--summary", metavar="RUN_DIR",
                    help="print a fleet summary for RUN_DIR/<host>/diff.json and exit")
    ap.add_argument("--summary-out", metavar="FILE", help="with --summary, also write it to FILE")
    ap.add_argument("--summary-json", metavar="FILE", help="with --summary, write JSON to FILE")
    args = ap.parse_args(argv)

    if args.summary:
        if not os.path.isdir(args.summary):
            print("error: %s is not a directory" % args.summary, file=sys.stderr)
            return 2
        hosts = summarize_run(args.summary)
        text = render_summary(args.summary, hosts)
        if args.summary_out:
            _write(args.summary_out, text)
        if args.summary_json:
            _write(args.summary_json, json.dumps(hosts, indent=2))
        if not args.quiet:
            sys.stdout.write(text)
        return 1 if any(h["status"] not in ("identical",) for h in hosts) else 0

    if not (args.before and args.after):
        ap.error("before and after snapshots are required (or use --summary RUN_DIR)")
    try:
        res = compare(args.before, args.after)
    except SnapshotError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    text = render_text(res)
    if args.text:
        _write(args.text, text)
    if args.html:
        _write(args.html, render_html(res))
    if args.json:
        _write(args.json, json.dumps(res, indent=2))
    if not args.quiet:
        sys.stdout.write(text)
    return 0 if res["identical"] else 1


def _write(path, content):
    d = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


if __name__ == "__main__":
    sys.exit(main())
