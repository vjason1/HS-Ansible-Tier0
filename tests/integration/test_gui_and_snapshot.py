#!/usr/bin/env python3
"""Unit + integration tests for the configuration GUI and the OS-upgrade snapshot tools.

    python3 tests/integration/test_gui_and_snapshot.py        (or: python3 -m unittest -v ...)

Covers
  * gui/surgeon.py       comment-preserving edits of the REAL vars/main.yml / ansible.cfg
  * gui/schema.py        every top-level key in vars/main.yml has a GUI field
  * gui/catalog.py       command building rejects unknown playbooks/tags/options
  * gui/tier0_gui.py     token auth, origin check, section load/save, inventory round-trip
  * scripts/tier0_snapshot_diff.py   classification, package/kv tables, exit codes
  * scripts/tier0_config_snapshot.sh read-only, stable output (Linux only)
  * os_upgrade_snapshot.yml          pre -> refuse second pre -> post -> diff (needs ansible-playbook)

No network and no root needed; everything that writes uses a temp directory.
"""

import difflib
import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "gui"))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import yaml  # noqa: E402

import catalog  # noqa: E402
import schema  # noqa: E402
import tier0_gui  # noqa: E402
import tier0_snapshot_diff as differ  # noqa: E402
import hs_upload  # noqa: E402
import logstore  # noqa: E402
from surgeon import (UNSET, SurgeryError, YamlSurgeon, apply_ini_changes,  # noqa: E402
                     apply_yaml_changes)


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def changed_lines(old, new):
    return [l for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]


# ===========================================================================
class TestSurgeon(unittest.TestCase):
    def setUp(self):
        self.src = read("vars/main.yml")

    def test_scalar_edit_touches_one_line_and_keeps_inline_comment(self):
        new, _ = apply_yaml_changes(self.src, {"raid_level": 10})
        self.assertEqual(changed_lines(self.src, new), ["-raid_level: 0", "+raid_level: 10"])
        text = "a: 1  # keep me\nb: 2\n"
        out, _ = apply_yaml_changes(text, {"a": 5})
        self.assertEqual(out, "a: 5  # keep me\nb: 2\n")

    def test_comments_are_never_lost(self):
        changes = {"hammerspace_nodes": ["{{ hammerspace_api_host }}", "10.0.10.16"],
                   "nvme_exclude_devices": ["nvme0n1"], "client_subnets": [],
                   "di_container_cpus": "4", "perf_irq_pin_interfaces": ["ens1f0"]}
        new, _ = apply_yaml_changes(self.src, changes)
        old_comments = [l for l in self.src.splitlines() if l.lstrip().startswith("#")]
        new_comments = [l for l in new.splitlines() if l.lstrip().startswith("#")]
        # Only the one inline comment on the replaced hammerspace_nodes item may go.
        self.assertEqual(old_comments, new_comments)

    def test_insert_lands_under_commented_example(self):
        new, act = apply_yaml_changes(self.src, {"expected_nvme_count": 9})
        self.assertEqual(act["expected_nvme_count"], "inserted")
        lines = new.splitlines()
        i = lines.index("expected_nvme_count: 9")
        self.assertEqual(lines[i - 1], "# expected_nvme_count: 9")

    def test_brand_new_key_goes_next_to_its_neighbors(self):
        nb = {"perf_tcp_sysctl": schema.neighbors_for("perf")}
        new, _ = apply_yaml_changes(self.src, {"perf_tcp_sysctl": {"net.core.rmem_max": 1}}, nb)
        lines = new.splitlines()
        i = lines.index("perf_tcp_sysctl:")
        self.assertTrue(any("perf_tcp_sysctl_enabled" in l for l in lines[i - 3:i]))

    def test_unset_comments_out(self):
        new, act = apply_yaml_changes(self.src, {"existing_mount_subdir": UNSET})
        self.assertEqual(act["existing_mount_subdir"], "commented")
        self.assertNotIn("existing_mount_subdir", yaml.safe_load(new))
        self.assertIn("# existing_mount_subdir: tier0", new)

    def test_types_round_trip(self):
        tricky = {"di_container_memory": "yes", "existing_mount_subdir": "0755",
                  "hammerspace_volume_high_threshold": 0.95, "hammerspace_api_password": "{{ x }}",
                  "raid_arrays": [{"name": "md0", "drives": ["/dev/nvme0n1"]}]}
        new, _ = apply_yaml_changes(self.src, tricky)
        data = yaml.safe_load(new)
        for k, v in tricky.items():
            self.assertEqual(data[k], v, k)

    def test_rejects_duplicate_and_invalid(self):
        with self.assertRaises(SurgeryError):
            apply_yaml_changes("a: 1\na: 2\n", {"a": 3})
        with self.assertRaises(SurgeryError):
            YamlSurgeon("- just\n- a list\n")

    def test_ini(self):
        cfg = read("ansible.cfg")
        new, act = apply_ini_changes(cfg, {("defaults", "inventory"): "inventory.yml",
                                           ("defaults", "forks"): "20"})
        ch = changed_lines(cfg, new)
        self.assertIn("+inventory = inventory.yml", ch)
        self.assertIn("+forks = 20", ch)
        self.assertEqual(len(ch), 3)


# ===========================================================================
class TestSchema(unittest.TestCase):
    def test_every_vars_key_has_a_field(self):
        data = yaml.safe_load(read("vars/main.yml"))
        missing = sorted(set(data) - schema.vars_main_fields() - schema.INTERNAL_VARS_KEYS)
        self.assertEqual(missing, [], "add these vars/main.yml keys to gui/schema.py: %s" % missing)

    def test_no_duplicate_fields_per_file(self):
        seen = {}
        for s in schema.SECTIONS:
            for f in s["fields"]:
                k = (s["file"], f["key"])
                self.assertNotIn(k, seen, "%s defined in %s and %s" % (k, seen.get(k), s["id"]))
                seen[k] = s["id"]

    def test_field_integrity(self):
        keys_by_file = {}
        for s in schema.SECTIONS:
            keys_by_file.setdefault(s["file"], set()).update(f["key"] for f in s["fields"])
        for s in schema.SECTIONS:
            for f in s["fields"]:
                self.assertIn(f["type"], {"bool", "int", "float", "str", "secret", "enum",
                                          "list", "intlist", "yaml"}, f["key"])
                if f["type"] == "enum" and f.get("default") is not None:
                    self.assertIn(f["default"], f["options"], f["key"])
                for k in f.get("show_if", {}):
                    self.assertIn(k, keys_by_file[s["file"]], "%s show_if %s" % (f["key"], k))
                if f["type"] == "yaml" and f.get("example"):
                    yaml.safe_load(f["example"])

    def test_menus_reference_real_sections(self):
        for g in schema.MENUS:
            for it in g["items"]:
                if it["kind"] == "form":
                    self.assertIn(it["id"], schema.SECTIONS_BY_ID)

    def test_editable_files_exist(self):
        for rel in schema.EDITABLE_FILES:
            if rel != "tier0_instances_limit":   # gitignored, created on demand
                self.assertTrue(os.path.exists(os.path.join(REPO, rel)), rel)


# ===========================================================================
class TestCatalog(unittest.TestCase):
    INV = ["inventory.yml"]

    def test_playbooks_exist(self):
        for pb in catalog.PLAYBOOKS:
            self.assertTrue(os.path.isfile(os.path.join(REPO, pb["id"])), pb["id"])
        for ut in catalog.UTILITIES:
            self.assertTrue(os.path.isfile(os.path.join(REPO, ut["id"])), ut["id"])

    def test_build_and_reject(self):
        argv, extra, destructive = catalog.build_playbook_argv(
            REPO, {"playbook": "site.yml", "inventory": ["inventory.yml"], "tags": ["precheck"],
                   "check": True, "extra_vars_text": "hammerspace_serial=2"}, self.INV)
        self.assertEqual(argv[1:3], ["playbook", "site.yml"])
        self.assertIn("--check", argv)
        self.assertEqual(extra, {"hammerspace_serial": 2})
        self.assertIsNone(destructive)
        _, _, destructive = catalog.build_playbook_argv(REPO, {"playbook": "site.yml"}, self.INV)
        self.assertTrue(destructive)
        bad = [{"playbook": "../../etc/passwd"},
               {"playbook": "site.yml", "inventory": ["/etc/hosts"]},
               {"playbook": "site.yml", "tags": ["rm -rf"]},
               {"playbook": "site.yml", "limit": "--become-user=x"},
               {"playbook": "site.yml", "extra_vars_text": "bad name=1"},
               {"playbook": "os_upgrade_snapshot.yml"}]   # snapshot_phase required
        for req in bad:
            with self.assertRaises(catalog.CatalogError, msg=req):
                catalog.build_playbook_argv(REPO, req, self.INV)

    def test_utility_yes_only_without_dry_run(self):
        base = {"script": "cleanup_instance_nodes.py",
                "options": {"host": "10.1.2.3", "user": "admin", "nodes": ["n1", "n2"]}}
        argv, destructive = catalog.build_utility_argv(REPO, base)
        self.assertIn("--dry-run", argv)
        self.assertNotIn("--yes", argv)
        self.assertFalse(destructive)
        self.assertEqual(argv.count("--node"), 2)
        base["options"]["dry_run"] = False
        argv, destructive = catalog.build_utility_argv(REPO, base)
        self.assertIn("--yes", argv)
        self.assertTrue(destructive)
        with self.assertRaises(catalog.CatalogError):
            catalog.build_utility_argv(REPO, {"script": "cleanup_instance_nodes.py", "options": {}})


# ===========================================================================
def make_snapshot(**over):
    sections = {
        "summary.key_facts": ["kernel=5.14.0-427", "nfsd_threads=128"],
        "kernel.version": ["release=5.14.0-427"],
        "packages.installed": ["bash.x86_64 0:5.1.8-6.el9", "nfs-utils.x86_64 1:2.5.4-25.el9",
                               "kernel.x86_64 0:5.14.0-427.el9", "mdadm.x86_64 0:4.2-13.el9"],
        "packages.config_leftovers": ["(end of list)"],
        "nfs.runtime": ["threads=128", "max_block_size=1048576"],
        "raid.mdstat": ["md0 : active raid0 nvme1n1[1] nvme0n1[0]"],
        "kernel.sysctl": ["net.core.rmem_max = 268435456", "vm.swappiness = 10"],
    }
    sections.update(over)
    meta = over.pop("_meta", {}) if "_meta" in over else {}
    out = ["#! tier0-config-snapshot format=1", "#! host: node1",
           "#! boot_id: %s" % meta.get("boot_id", "aaa"), "#! kernel: x"]
    for name, body in sections.items():
        if name.startswith("_"):
            continue
        out += ["", "===== SECTION: %s =====" % name] + body
    return "\n".join(out + ["", "===== END ====="]) + "\n"


class TestDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def test_identical(self):
        a = self.write("a", make_snapshot())
        b = self.write("b", make_snapshot())
        self.assertEqual(differ.main([a, b, "-q"]), 0)
        self.assertTrue(differ.compare(a, b)["identical"])

    def test_typical_upgrade(self):
        a = self.write("pre", make_snapshot())
        post = make_snapshot(**{
            "summary.key_facts": ["kernel=5.14.0-503", "nfsd_threads=8"],
            "kernel.version": ["release=5.14.0-503"],
            "packages.installed": ["bash.x86_64 0:5.1.8-9.el9", "kernel.x86_64 0:5.14.0-427.el9",
                                   "kernel.x86_64 0:5.14.0-503.el9", "mdadm.x86_64 0:4.2-14.el9",
                                   "rsync.x86_64 0:3.2.3-19.el9"],
            "packages.config_leftovers": ["/etc/nfs.conf.rpmnew", "(end of list)"],
            "nfs.runtime": ["threads=8", "max_block_size=1048576"],
            "raid.mdstat": ["md127 : active raid0 nvme1n1[1] nvme0n1[0]"],
            "kernel.sysctl": ["net.core.rmem_max = 212992", "vm.swappiness = 10"],
            "_meta": {"boot_id": "bbb"}})
        b = self.write("post", post)
        html_p, json_p = os.path.join(self.tmp, "d.html"), os.path.join(self.tmp, "d.json")
        self.assertEqual(differ.main([a, b, "-q", "--html", html_p, "--json", json_p]), 1)
        res = json.load(open(json_p))
        sec = {s["name"]: s for s in res["sections"]}
        self.assertEqual(sec["raid.mdstat"]["severity"], "critical")
        self.assertEqual(sec["nfs.runtime"]["detail"]["changed"],
                         [{"key": "threads", "before": "128", "after": "8"}])
        pk = sec["packages.installed"]["detail"]
        self.assertEqual([p["name"] for p in pk["upgraded"]], ["bash.x86_64", "mdadm.x86_64"])
        self.assertEqual([p["name"] for p in pk["removed"]], ["nfs-utils.x86_64"])
        self.assertEqual([p["name"] for p in pk["added"]], ["rsync.x86_64"])
        self.assertEqual(pk["multi_version"][0]["versions_added"], ["0:5.14.0-503.el9"])
        self.assertEqual(sec["kernel.sysctl"]["detail"]["changed"][0]["key"], "net.core.rmem_max")
        self.assertTrue(any("/etc/nfs.conf.rpmnew" in a for a in res["attention"]))
        self.assertEqual(sec["kernel.version"]["severity"], "info")
        self.assertIn("<html>", open(html_p).read())

    def test_same_boot_warning_and_bad_input(self):
        a = self.write("a", make_snapshot())
        b = self.write("b", make_snapshot(**{"nfs.runtime": ["threads=8"]}))
        self.assertTrue(any("same boot" in w for w in differ.compare(a, b)["warnings"]))
        junk = self.write("junk", "hello\n")
        self.assertEqual(differ.main([junk, b, "-q"]), 2)

    def test_version_ordering(self):
        self.assertLess(differ._ver_key("0:5.14.0-427.el9"), differ._ver_key("0:5.14.0-503.el9"))
        self.assertLess(differ._ver_key("1:9.9"), differ._ver_key("2:0.1"))

    def test_summary(self):
        run = os.path.join(self.tmp, "run")
        for host, post in (("n1", make_snapshot()), ("n2", make_snapshot(**{"raid.mdstat": []}))):
            d = os.path.join(run, host)
            os.makedirs(d)
            pre_p, post_p = os.path.join(d, "pre.txt"), os.path.join(d, "post.txt")
            open(pre_p, "w").write(make_snapshot())
            open(post_p, "w").write(post)
            differ.main([pre_p, post_p, "-q", "--json", os.path.join(d, "diff.json")])
        os.makedirs(os.path.join(run, "n3"))
        hosts = {h["host"]: h for h in differ.summarize_run(run)}
        self.assertEqual(hosts["n1"]["status"], "identical")
        self.assertEqual(hosts["n2"]["status"], "critical")
        self.assertEqual(hosts["n3"]["status"], "no-diff")


# ===========================================================================
@unittest.skipUnless(platform.system() == "Linux", "collector targets Linux")
class TestCollector(unittest.TestCase):
    SCRIPT = os.path.join(REPO, "scripts", "tier0_config_snapshot.sh")

    def run_it(self):
        return subprocess.run(["bash", self.SCRIPT], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=300).stdout.decode()

    def test_format_and_stability(self):
        a, b = self.run_it(), self.run_it()
        self.assertTrue(a.startswith("#! tier0-config-snapshot format=1"))
        for must in ("summary.key_facts", "packages.installed", "raid.mdstat", "fs.fstab",
                     "nfs.exports_active", "perf.irq_affinity", "net.links", "services.key_units"):
            self.assertIn("===== SECTION: %s =====" % must, a)
        strip = lambda t: [l for l in t.splitlines() if not l.startswith("#!")]  # noqa: E731
        self.assertEqual(strip(a), strip(b), "collector output is not stable between runs")

    def test_is_read_only(self):
        src = read("scripts/tier0_config_snapshot.sh")
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for forbidden in (r"\bmdadm\s+--(create|stop|remove|zero)", r"\bmkfs", r"\bexportfs\s+-[ar]",
                          r"systemctl\s+(start|stop|restart|enable|disable)", r"\bsysctl\s+-w",
                          r"(?<![/\w.-])modprobe\s", r"\brm\s+-", r"\bdracut\b", r"update-initramfs"):
            self.assertIsNone(re.search(forbidden, code), forbidden)


def _prune_nulls(node):
    """`hosts:` with nothing under it (all entries commented out) is null; Ansible
    treats that exactly like an absent key, and the editor omits it."""
    if isinstance(node, dict):
        out = {k: _prune_nulls(v) for k, v in node.items()
               if not (k in ("hosts", "vars", "children") and v is None)}
        return out or None
    return node


# ===========================================================================
class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.repo_dir = os.path.join(cls.tmp, "repo")
        os.makedirs(os.path.join(cls.repo_dir, "vars"))
        for rel in ("vars/main.yml", "vars/vault.yml", "ansible.cfg", "inventory.yml",
                    "inventory.az.yml", "inventory.oci.yml"):
            shutil.copy(os.path.join(REPO, rel), os.path.join(cls.repo_dir, rel))
        repo = tier0_gui.Repo(cls.repo_dir)
        logstore.ensure_dirs(cls.repo_dir)
        tier0_gui.Handler.app = {"repo": repo, "token": "tok", "verbose": False,
                                 "audit": logstore.Audit(cls.repo_dir),
                                 "jobs": tier0_gui.JobManager(cls.repo_dir,
                                                              os.path.join(cls.repo_dir, logstore.JOBS_DIR))}
        cls.httpd = tier0_gui.ThreadingServer(("127.0.0.1", 0), tier0_gui.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.repo = repo

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp)

    def req(self, method, path, body=None, token="tok", origin=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {}
        if token:
            headers["X-Auth-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
            if origin:
                headers["X-Requested-With"] = "tier0-gui"
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def test_auth(self):
        self.assertEqual(self.req("GET", "/api/meta", token=None)[0], 401)
        self.assertEqual(self.req("GET", "/api/meta", token="nope")[0], 401)
        self.assertEqual(self.req("GET", "/api/meta")[0], 200)
        self.assertEqual(self.req("GET", "/")[0], 200)
        self.assertEqual(self.req("GET", "/static/../tier0_gui.py")[0], 404)
        st, _ = self.req("POST", "/api/section/raid/preview", {"changes": {}}, origin=False)
        self.assertEqual(st, 403)

    def test_section_save_cycle(self):
        st, d = self.req("GET", "/api/section/raid")
        self.assertEqual(st, 200)
        self.assertEqual(d["values"]["raid_level"], {"state": "set", "value": 0})
        body = {"changes": {"raid_level": {"action": "set", "value": "5"}}, "base_hash": d["hash"]}
        st, pv = self.req("POST", "/api/section/raid/preview", body)
        self.assertEqual(st, 200)
        self.assertIn("+raid_level: 5", pv["diff"])
        st, sv = self.req("POST", "/api/section/raid/save", body)
        self.assertEqual(st, 200, sv)
        self.assertTrue(os.path.exists(os.path.join(self.repo_dir, sv["backup"])))
        # stale base hash -> conflict
        st, _ = self.req("POST", "/api/section/raid/save", body)
        self.assertEqual(st, 409)
        # bad enum / unknown field
        st, _ = self.req("POST", "/api/section/raid/preview",
                         {"changes": {"raid_level": {"action": "set", "value": 7}}})
        self.assertEqual(st, 400)
        st, _ = self.req("POST", "/api/section/raid/preview",
                         {"changes": {"repo_root": {"action": "set", "value": "/"}}})
        self.assertEqual(st, 400)

    def test_doc_images(self):
        img_dir = os.path.join(self.repo_dir, "docs", "images", "gui")
        os.makedirs(img_dir, exist_ok=True)
        with open(os.path.join(img_dir, "shot.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        with open(os.path.join(img_dir, "evil.svg"), "w") as fh:
            fh.write("<svg onload='alert(1)'/>")
        st, body = self.req("GET", "/api/doc/image?path=docs/images/gui/shot.png")
        self.assertEqual(st, 200)
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(self.req("GET", "/api/doc/image?path=docs/images/gui/shot.png", token=None)[0], 401)
        for bad in ("docs/images/gui/evil.svg", "vars/vault.yml", "docs/images/../../vars/vault.yml",
                    "../../etc/passwd", "/etc/passwd", "docs/images/gui/missing.png", ""):
            self.assertEqual(self.req("GET", "/api/doc/image?path=" + bad)[0], 404, bad)

    def test_files_allow_list(self):
        self.assertEqual(self.req("GET", "/api/file?path=../../etc/passwd")[0], 403)
        self.assertEqual(self.req("GET", "/api/file?path=site.yml")[0], 403)
        st, _ = self.req("POST", "/api/file/save", {"path": "inventory.yml", "text": "a: [\n"})
        self.assertEqual(st, 400)

    def test_inventory_round_trip(self):
        for rel in ("inventory.yml", "inventory.az.yml"):
            model = tier0_gui.inventory_model(self.repo, rel)
            text = tier0_gui.inventory_render(model)
            before = _prune_nulls(yaml.safe_load(read(rel)))
            after = _prune_nulls(yaml.safe_load(text))
            self.assertEqual(json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True), rel)

    def test_validate_flags_problems(self):
        issues = tier0_gui.validate(self.repo)
        self.assertTrue(all({"level", "area", "message"} <= set(i) for i in issues))

    def test_remembers_last_inventory(self):
        st, meta = self.req("GET", "/api/meta")
        self.assertEqual(meta["last_inventory"], [])
        st, _ = self.req("POST", "/api/state", {"last_inventory": ["inventory.oci.yml"]})
        self.assertEqual(st, 200)
        self.assertEqual(self.req("GET", "/api/meta")[1]["last_inventory"], ["inventory.oci.yml"])
        for bad in (["../../etc/passwd"], ["nope.yml"], "inventory.yml"):
            self.assertEqual(self.req("POST", "/api/state", {"last_inventory": bad})[0], 400, bad)
        # a remembered file that disappears is dropped, not offered
        oci = os.path.join(self.repo_dir, "inventory.oci.yml")
        os.rename(oci, oci + ".bak")
        try:
            self.assertEqual(self.req("GET", "/api/meta")[1]["last_inventory"], [])
        finally:
            os.rename(oci + ".bak", oci)
        self.req("POST", "/api/state", {"last_inventory": []})

    def test_logs_endpoints(self):
        log = os.path.join(self.repo_dir, "logs", "ansible-20261015_000000-playbook-site-1.log")
        with open(log, "w") as fh:
            fh.write("# ===== run.sh header\n# command: playbook site.yml --check\nTASK [x]\n")
        self.assertEqual(self.req("GET", "/api/logs", token=None)[0], 401)
        st, d = self.req("GET", "/api/logs")
        self.assertEqual(st, 200)
        e = [x for x in d["entries"] if x["name"].endswith("playbook-site-1.log")]
        self.assertEqual(e[0]["kind"], "ansible")
        self.assertEqual(e[0]["title"], "playbook site.yml --check")
        st, v = self.req("GET", "/api/logs/view?id=" + e[0]["id"])
        self.assertIn("TASK [x]", v["text"])
        for bad in ("repo:../../etc/passwd", "repo:vars/vault.yml", "repo:logs/../vars/main.yml",
                    "repo:/etc/passwd", "repo:gui/tier0_gui.py", "nope:x", ""):
            self.assertEqual(self.req("GET", "/api/logs/view?id=" + bad)[0], 404, bad)
            self.assertEqual(self.req("GET", "/api/logs/download?id=" + bad)[0], 404, bad)
        st, raw = self.req("GET", "/api/logs/download?id=" + e[0]["id"])
        self.assertEqual(st, 200)

    def test_log_settings_and_audit(self):
        for bad in ({"log_retention_days": -1}, {"log_retention_days": 99999}, {"upload_url": "ftp://x"},
                    {"upload_initials": "J V"}, {"upload_customer": "a;b"}):
            self.assertEqual(self.req("POST", "/api/logs/settings", bad)[0], 400, bad)
        st, sv = self.req("POST", "/api/logs/settings", {"log_retention_days": 30})
        self.assertEqual(st, 200, sv)
        sv = sv.get("settings", sv)
        self.assertEqual(sv["log_retention_days"], 30)
        self.assertEqual(sv["upload_url"], logstore.DEFAULT_UPLOAD_URL)
        events = [a["event"] for a in tier0_gui.Handler.app["audit"].tail()]
        self.assertIn("settings.change", events)

    def test_upload_job_refuses_non_logs(self):
        for ids in (["repo:vars/vault.yml"], ["repo:../../etc/passwd"], []):
            st, d = self.req("POST", "/api/jobs", {"kind": "hs_upload", "ids": ids})
            self.assertEqual(st, 400, ids)

    def test_job_rejects_unconfirmed_destructive(self):
        st, d = self.req("POST", "/api/jobs", {"kind": "playbook", "playbook": "site.yml"})
        self.assertEqual(st, 412)
        self.assertTrue(d["needs_confirm"])


# ===========================================================================
# ===========================================================================
import push_ssh_key  # noqa: E402


class TestSshKeyPush(unittest.TestCase):
    """scripts/push_ssh_key.py + the GUI's ssh_key_push job builder."""

    def test_target_parsing_and_rejection(self):
        t = push_ssh_key.Target("node1=10.0.0.1:2222")
        self.assertEqual((t.name, t.host, t.port), ("node1", "10.0.0.1", 2222))
        self.assertEqual(push_ssh_key.Target("[fe80::1]:22").host, "fe80::1")
        self.assertEqual(push_ssh_key.Target("host.example.com").port, 22)
        for bad in ("-oProxyCommand=x", "a b", "h:99999", "h:abc", "x=;rm", "-h=1.2.3.4"):
            with self.assertRaises(ValueError, msg=bad):
                push_ssh_key.Target(bad)

    def test_refuses_private_key_and_missing_password(self):
        tmp = tempfile.mkdtemp()
        try:
            priv = os.path.join(tmp, "id")
            with open(priv, "w") as fh:
                fh.write("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n")
            env = dict(os.environ, TIER0_SSH_PASSWORD="x")
            r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "push_ssh_key.py"),
                                "--user", "u", "--pubkey", priv, "--target", "127.0.0.1"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if shutil.which("ssh"):
                self.assertEqual(r.returncode, 2)
                self.assertIn(b"never send a private key", r.stderr)
            env.pop("TIER0_SSH_PASSWORD")
            r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "push_ssh_key.py"),
                                "--user", "u", "--pubkey", priv, "--target", "127.0.0.1"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(r.returncode, 2)
        finally:
            shutil.rmtree(tmp)

    def test_remote_script_only_appends(self):
        s = push_ssh_key.REMOTE_SCRIPT
        self.assertIn("grep -qxF", s)          # idempotent: exact-line check before append
        self.assertIn(">> ", s)                # append, never overwrite
        self.assertNotIn("sudo", s)
        self.assertNotRegex(s, r"(^|[^>])> *\"\$HOME/.ssh/authorized_keys")

    def test_gui_job_builder(self):
        repo = tier0_gui.Repo(REPO)
        base = {"user": "ubuntu", "password": "pw", "generate": True,
                "targets": [{"name": "n1", "host": "10.0.0.1", "port": 22}]}
        argv, env, title = tier0_gui.build_ssh_push(repo, dict(base))
        self.assertEqual(env, {"TIER0_SSH_PASSWORD": "pw"})
        self.assertNotIn("pw", argv, "the password must never be on the command line")
        self.assertIn("--generate", argv)
        self.assertEqual(argv[argv.index("--target") + 1], "n1=10.0.0.1:22")
        for bad in ({"user": ""}, {"user": "-oX"}, {"password": ""}, {"targets": []},
                    {"targets": [{"name": "n", "host": "-oProxyCommand=x"}]},
                    {"generate": False, "pubkey": "/etc/shadow"}):
            with self.assertRaises(tier0_gui.ApiError, msg=bad):
                tier0_gui.build_ssh_push(repo, dict(base, **bad))


# ===========================================================================
class TestBecomePassword(unittest.TestCase):
    """Sudo (become) password handed to playbook runs as extra vars."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "vars"))
        self.repo = tier0_gui.Repo(self.tmp)
        self.jm = tier0_gui.JobManager(self.tmp, os.path.join(self.tmp, "jobs"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_vault(self, text):
        with open(os.path.join(self.tmp, "vars", "vault.yml"), "w") as fh:
            fh.write(text)

    def test_typed_password_goes_to_private_temp_file(self):
        cleanup = []
        args = tier0_gui.become_args(self.repo, {"become_password": "s3cret"}, self.jm, cleanup)
        self.assertEqual(args[0], "-e")
        path = args[1][1:]
        self.assertNotIn("s3cret", " ".join(args))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(json.load(open(path)), {"ansible_become_password": "s3cret"})
        self.assertEqual(cleanup, [os.path.dirname(path)])
        shutil.rmtree(cleanup[0])

    def test_saved_password(self):
        with self.assertRaises(tier0_gui.ApiError):        # no vault file
            tier0_gui.become_args(self.repo, {"use_saved_become": True}, self.jm, [])
        self.write_vault("vault_hammerspace_api_password: x\n")
        with self.assertRaises(tier0_gui.ApiError):        # key not set
            tier0_gui.become_args(self.repo, {"use_saved_become": True}, self.jm, [])
        self.write_vault("vault_become_password: pw\n")
        args = tier0_gui.become_args(self.repo, {"use_saved_become": True}, self.jm, [])
        self.assertEqual(args[1], "@" + os.path.join(self.tmp, "vars", "vault.yml"))
        self.assertEqual(json.loads(args[3]), {"ansible_become_password": "{{ vault_become_password }}"})
        cleanup = []
        both = tier0_gui.become_args(self.repo, {"use_saved_become": True, "become_password": "t"},
                                     self.jm, cleanup)
        self.assertTrue(both[-1].startswith("@/"), "a typed password must come last so it wins")
        shutil.rmtree(cleanup[0])

    def test_encrypted_vault_needs_vault_password(self):
        self.write_vault("$ANSIBLE_VAULT;1.1;AES256\n6162\n")
        with self.assertRaises(tier0_gui.ApiError):
            tier0_gui.become_args(self.repo, {"use_saved_become": True}, self.jm, [])
        self.assertTrue(tier0_gui.become_args(self.repo, {"use_saved_become": True, "vault_password": "v"},
                                              self.jm, []))

    def test_schema_has_vault_field(self):
        keys = [f["key"] for f in schema.SECTIONS_BY_ID["credentials"]["fields"]]
        self.assertIn("vault_become_password", keys)


# ===========================================================================
class TestDocLinks(unittest.TestCase):
    """Every relative Markdown link (file and #anchor) in the repo resolves.

    The docs are split across README.md, docs/gui/, docs/manual/ and the guides at
    the root; this keeps a future move from silently breaking cross-references.
    """
    LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

    @staticmethod
    def slug(h):
        h = re.sub(r"[`*_]", "", h.strip().lower())
        return re.sub(r"[^\w\- ]", "", h).replace(" ", "-")

    def anchors(self, path):
        out, fence = set(), False
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.lstrip().startswith("```"):
                    fence = not fence
                m = None if fence else re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
                if m:
                    out.add(self.slug(m.group(1)))
        return out

    def test_screenshots(self):
        """Every GUI guide has screenshots; they exist, are PNGs under docs/images/, are all used, and stay small."""
        used = set()
        must_have = {"README.md", "gui/README.md", "docs/os-upgrade-check.md"} | {
            "docs/gui/" + f for f in os.listdir(os.path.join(REPO, "docs", "gui")) if f.endswith(".md")}
        docs = [os.path.relpath(os.path.join(d, f), REPO).replace(os.sep, "/")
                for d, dirs, files in os.walk(REPO) for f in files
                if f.endswith(".md") and not any(x in d for x in (".git", "payload", "snapshots"))]
        for doc in docs:
            text = read(doc)
            refs = re.findall(r'!\[[^\]]*\]\(([^)\s]+)\)', text) + re.findall(r'<img\s[^>]*src="([^"]+)"', text)
            if doc in must_have:
                self.assertTrue(refs, doc + " has no screenshots")
            for ref in refs:
                full = os.path.normpath(os.path.join(REPO, os.path.dirname(doc), ref))
                rel = os.path.relpath(full, REPO).replace(os.sep, "/")
                self.assertTrue(rel.startswith("docs/images/"), "%s: %s is outside docs/images/" % (doc, ref))
                with open(full, "rb") as fh:
                    self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n", rel)
                used.add(rel)
        img_dir = os.path.join(REPO, "docs", "images", "gui")
        for f in os.listdir(img_dir):
            rel = "docs/images/gui/" + f
            self.assertIn(rel, used, "unused screenshot " + rel)
            self.assertLess(os.path.getsize(os.path.join(img_dir, f)), 300 * 1024, rel + " is too large")

    def test_relative_links_resolve(self):
        broken = []
        for d, dirs, files in os.walk(REPO):
            dirs[:] = [x for x in dirs if x not in (".git", "payload", ".gui-backups", ".gui-jobs", "snapshots")]
            for f in files:
                if not f.endswith(".md"):
                    continue
                p, fence = os.path.join(d, f), False
                with open(p, encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if line.lstrip().startswith("```"):
                            fence = not fence
                        if fence:
                            continue
                        for t in self.LINK.findall(line):
                            if re.match(r"^[a-z]+:", t):
                                continue
                            path, _, frag = t.partition("#")
                            target = p if not path else os.path.normpath(os.path.join(d, path))
                            where = "%s:%d %s" % (os.path.relpath(p, REPO), n, t)
                            if not os.path.exists(target):
                                broken.append("missing file  " + where)
                            elif frag and target.endswith(".md") and frag not in self.anchors(target):
                                broken.append("missing anchor " + where)
        self.assertEqual(broken, [], "\n".join(broken))

    def test_code_fences_balanced(self):
        for d, dirs, files in os.walk(REPO):
            dirs[:] = [x for x in dirs if x not in (".git", "payload")]
            for f in (x for x in files if x.endswith(".md")):
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    n = sum(1 for l in fh if l.lstrip().startswith("```"))
                self.assertEqual(n % 2, 0, "unbalanced ``` fence in " + os.path.relpath(os.path.join(d, f), REPO))

    def test_gui_lists_new_docs(self):
        docs = tier0_gui.doc_list(tier0_gui.Repo(REPO))
        for must in ("README.md", "docs/gui/oci.md", "docs/manual/README.md", "docs/os-upgrade-check.md"):
            self.assertIn(must, docs)


# ===========================================================================
class TestUpload(unittest.TestCase):
    """scripts/hs_upload.py — redaction, bundle, and the toolkit's multipart request."""

    KEEP = ["net.bridge.bridge-nf-pass-vlan-input-dev = 0", "net.ipv4.ipfrag_secret_interval = 0",
            "password_policy_url: http://x", "token_expiry_seconds: 3600",
            'di_k8s_image_pull_secret: "hammerspace-di-pull"',
            '"hammerspace_api_password": "{{ vault_hammerspace_api_password }}"',
            "export GOOGLE_APPLICATION_CREDENTIALS=/path/sa.json",
            "ansible_ssh_private_key_file=/root/.ssh/id_ed25519"]
    MASK = ["ansible_become_password=Sup3r", "ansible_become_pass: Sup3r", "ansible_ssh_pass=x1",
            '"vault_hammerspace_api_password": "RealPw1"', "hammerspace_api_password: plain",
            'vault_s3_secret_key: "wJal"', "export AWS_SECRET_ACCESS_KEY=AKIA", "export AWS_ACCESS_KEY_ID=AKIA2",
            '{"token": "abc.def"}', '"password":"p"', '-H "Authorization: Basic YWRtaW46U2VjcjN0"',
            "curl -sk -u 'admin:Hunter2' https://anvil", "url=https://bob:pa55@host/x",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "logs"))
        os.makedirs(os.path.join(self.repo, "vars"))
        self.log = os.path.join(self.repo, "logs", "ansible-1.log")
        with open(self.log, "w") as fh:
            fh.write("\n".join(self.MASK + self.KEEP) + "\n")
        with open(os.path.join(self.repo, "vars", "vault.yml"), "w") as fh:
            fh.write("vault_hammerspace_api_password: secret\n")
        self.up = os.path.join(self.repo, "logs", "uploads")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_upload(self, *args):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return hs_upload.main(["--repo", self.repo] + list(args))

    def bundles(self):
        return sorted(f for f in os.listdir(self.up) if f.endswith(".tar.gz")) if os.path.isdir(self.up) else []

    def test_redaction_matrix(self):
        for line in self.KEEP:
            self.assertEqual(hs_upload.redact_text(line), (line, 0), "must stay readable: " + line)
        for line in self.MASK:
            out, n = hs_upload.redact_text(line)
            self.assertGreater(n, 0, "must be masked: " + line)
            self.assertIn("********", out)

    def test_bundle_contents_and_names(self):
        import tarfile
        vault = os.path.join(self.repo, "vars", "vault.yml")
        self.assertEqual(self.run_upload("--bundle-only", "--initials", "JV", "--customer", "Acme",
                                         self.log, vault), 0)
        self.assertEqual(self.run_upload("--bundle-only", self.log), 0)       # same second
        self.assertEqual(len(self.bundles()), 2, "a second bundle must not overwrite the first")
        for b in self.bundles():
            self.assertRegex(b, r"^tier0_bundle_[A-Za-z0-9._\-]+_\d{8}_\d{6}(-\d+)?\.tar\.gz$")
        with tarfile.open(os.path.join(self.up, self.bundles()[0])) as tar:
            names = tar.getnames()
            data = {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers() if m.isfile()}
        tops = {n.split("/")[0] for n in names}
        top = [t for t in tops if t.startswith("JV-Acme-tier0_")]
        if not top:   # same-second ordering: the other bundle is first
            with tarfile.open(os.path.join(self.up, self.bundles()[1])) as tar:
                names = tar.getnames()
                data = {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers() if m.isfile()}
            top = [n.split("/")[0] for n in names if n.startswith("JV-Acme-tier0_")]
        top = top[0]
        self.assertFalse(any(n.endswith("vault.yml") for n in names), "vault.yml must never be bundled")
        log_text = data[top + "/logs/ansible-1.log"]
        for secret in ("Sup3r", "RealPw1", "Hunter2", "pa55", "AKIA", "YWRtaW46U2VjcjN0", "wJal"):
            self.assertNotIn(secret, log_text)
        for kept in self.KEEP:
            self.assertIn(kept, log_text)
        self.assertIn("MANIFEST.txt", " ".join(data))

    def test_rejects_bad_names(self):
        self.assertEqual(self.run_upload("--bundle-only", "--initials", "J V", self.log), 2)
        self.assertEqual(self.run_upload("--bundle-only", "--prefix", "../x", self.log), 2)
        self.assertEqual(self.bundles(), [])

    def test_multipart_matches_toolkit_and_failure_keeps_bundle(self):
        import http.server
        from email.parser import BytesParser
        from email.policy import default as email_default
        got = {}

        class Intake(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = self.headers.get("Content-Length")
                body = self.rfile.read(int(n)) if n else b""
                msg = BytesParser(policy=email_default).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if part.get_filename():
                        got["file"] = (part.get_filename(), len(part.get_payload(decode=True)))
                    else:
                        got[name] = part.get_content().strip()
                ok = self.path == "/upload/"
                self.send_response(200 if ok else 500)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        srv = http.server.HTTPServer(("127.0.0.1", 0), Intake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        try:
            self.assertEqual(self.run_upload("--url", "http://127.0.0.1:%d/upload/" % port, self.log), 0)
            self.assertEqual({k: got.get(k) for k in ("payload", "cluster_id", "payload_type")},
                             {"payload": "prosvcs", "cluster_id": "manual", "payload_type": "prosvcs"})
            self.assertTrue(got["file"][0].startswith("tier0_bundle_") and got["file"][1] > 0, got)
            self.assertEqual(self.bundles(), [], "bundle deleted after a successful upload")
            self.assertTrue([f for f in os.listdir(self.up) if f.endswith(".manifest.txt")], "manifest kept")
            self.assertEqual(self.run_upload("--url", "http://127.0.0.1:%d/broken/" % port, self.log), 1)
            self.assertEqual(len(self.bundles()), 1, "bundle kept after a failed upload")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_default_endpoint_is_the_toolkits(self):
        self.assertEqual(hs_upload.UPLOAD_URL, "https://v1proto.sc.hammerspace.com/upload/")
        self.assertEqual(logstore.DEFAULT_UPLOAD_URL, hs_upload.UPLOAD_URL)
        self.assertEqual(dict(hs_upload.FORM_FIELDS),
                         {"payload": "prosvcs", "cluster_id": "manual", "payload_type": "prosvcs"})


# ===========================================================================
class TestLogStore(unittest.TestCase):
    """gui/logstore.py — catalog, path authorization, paging, retention, migration."""

    FILES = {
        "logs/ansible-20261015_000000-playbook-site-1.log": "ansible",
        "logs/ansible.log": "ansible",
        "logs/gui-audit.log": "audit",
        "logs/gui-jobs/20261015-000000-abc123.log": "job",
        "logs/uploads/tier0_bundle_h_20261015_000000.manifest.txt": "upload",
        "snapshots/run1/node1/pre.txt": "report",
        "snapshots/run1/node1/diff.html": "report",
        "snapshots/run1/SUMMARY.txt": "report",
        "preflight_report.txt": "report",
        "instance_report.csv": "report",
    }

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        logstore.ensure_dirs(self.repo)
        for rel in list(self.FILES) + ["vars/vault.yml", "logs/notes.txt", "snapshots/run1/node1/other.sh"]:
            p = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write("x\n")

    def tearDown(self):
        shutil.rmtree(self.repo)

    def repo_entries(self):
        return {e["name"]: e["kind"] for e in logstore.catalog(self.repo) if e["id"].startswith("repo:")}

    def test_catalog_lists_only_logs_and_reports(self):
        self.assertEqual(self.repo_entries(), self.FILES)

    def test_resolve_refuses_everything_else(self):
        for rel in self.FILES:
            self.assertTrue(logstore.resolve(self.repo, "repo:" + rel))
        self.assertTrue(os.path.isdir(logstore.resolve(self.repo, "repo:snapshots/run1", allow_dirs=True)))
        os.symlink(os.path.join(self.repo, "vars", "vault.yml"),
                   os.path.join(self.repo, "logs", "ansible-evil.log"))
        for bad in ("repo:vars/vault.yml", "repo:logs/notes.txt", "repo:snapshots/run1/node1/other.sh",
                    "repo:../x", "repo:/etc/passwd", "repo:logs/../vars/vault.yml", "repo:logs/ansible-evil.log",
                    "repo:snapshots/run1", "repo:logs", "x:y", ""):
            with self.assertRaises(logstore.LogError, msg=bad):
                logstore.resolve(self.repo, bad)

    def test_utf8_safe_paging(self):
        p = os.path.join(self.repo, "logs", "ansible-utf8.log")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("".join("line %04d ✓ résumé 中文 \U0001F642\n" % i for i in range(300)))
        with open(p, encoding="utf-8") as fh:
            whole = fh.read()
        for length in (5, 7, 13, 64, 1000):
            parts, off = [], 0
            while True:
                r = logstore.read_chunk(p, off, length)
                parts.append(r["text"])
                off = r["next"]
                if not r["more_after"]:
                    break
            self.assertEqual("".join(parts), whole, "forward, length %d" % length)
            r = logstore.read_chunk(p, 0, length, tail=True)
            parts, start = [r["text"]], r["offset"]
            while start > 0:
                frm = max(0, start - length)
                r = logstore.read_chunk(p, frm, start - frm)
                parts.append(r["text"])
                start = r["offset"]
            self.assertEqual("".join(reversed(parts)), whole, "backward, length %d" % length)

    def test_retention(self):
        old = time.time() - 40 * 86400
        for rel in self.FILES:
            os.utime(os.path.join(self.repo, rel), (old, old))
        running = "repo:logs/gui-jobs/20261015-000000-abc123.log"
        res = logstore.prune(self.repo, 30, keep_ids={running})
        left = self.repo_entries()
        for rel, kind in self.FILES.items():
            survives = (kind == "report" or rel in ("logs/ansible.log", "logs/gui-audit.log")
                        or "repo:" + rel == running)
            self.assertEqual(rel in left, survives, rel)
        self.assertEqual(res["deleted"], 2)
        self.assertEqual(logstore.prune(self.repo, 0)["deleted"], 0, "0 days = keep forever")
        self.assertEqual(logstore.prune(self.repo, 30)["deleted"], 1, "job log goes once it is not running")

    def test_rotate_and_migrate(self):
        big = os.path.join(self.repo, "logs", "ansible.log")
        with open(big, "wb") as fh:
            fh.truncate(logstore.ROTATE_BYTES + 1)
        self.assertEqual(logstore.rotate(self.repo), ["logs/ansible.log"])
        self.assertFalse(os.path.exists(big))
        self.assertTrue([f for f in os.listdir(os.path.join(self.repo, "logs"))
                         if f.startswith("ansible-catchall-")])
        legacy = os.path.join(self.repo, ".gui-jobs")
        os.makedirs(legacy)
        for ext in ("log", "json"):
            with open(os.path.join(legacy, "20261001-000000-old.%s" % ext), "w") as fh:
                fh.write("{}" if ext == "json" else "old\n")
        self.assertEqual(logstore.migrate_legacy_jobs(self.repo), 2)
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "logs", "gui-jobs", "20261001-000000-old.log")))

    def test_settings_validation(self):
        self.assertEqual(logstore.validate_settings({"log_retention_days": "0"}), {"log_retention_days": 0})
        for bad in ({"log_retention_days": -1}, {"upload_url": "javascript:x"}, {"upload_initials": "a b"}):
            with self.assertRaises(logstore.LogError):
                logstore.validate_settings(bad)


# ===========================================================================
class TestAnsibleLogging(unittest.TestCase):
    """Every ansible run in this repo is logged somewhere the GUI can list."""

    def test_ansible_cfg_catch_all_log_path(self):
        import configparser
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        cp.read(os.path.join(REPO, "ansible.cfg"))
        self.assertEqual(cp.get("defaults", "log_path"), "logs/ansible.log")
        # Ansible silently skips logging when the folder is missing, so it ships with the repo.
        self.assertTrue(os.path.isfile(os.path.join(REPO, "logs", "README")))
        if os.path.isdir(os.path.join(REPO, ".git")):
            r = subprocess.run(["git", "-C", REPO, "check-ignore", "-q", "logs/README"])
            self.assertEqual(r.returncode, 1, "logs/README must be tracked (not ignored)")
            r = subprocess.run(["git", "-C", REPO, "check-ignore", "-q", "logs/ansible-x.log"])
            self.assertEqual(r.returncode, 0, "log files must be gitignored")

    def test_scripts_use_run_sh(self):
        src = read("deploy_new_instances.sh")
        self.assertNotRegex(src, r"(?m)^\s*ansible-playbook ", "call scripts/run.sh so runs get their own log")
        self.assertIn("scripts/run.sh", src)


@unittest.skipUnless(shutil.which("ansible-playbook") and platform.system() == "Linux",
                     "needs ansible-playbook on Linux")
class TestSnapshotPlaybook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inv = os.path.join(self.tmp, "inv.yml")
        with open(self.inv, "w") as fh:
            fh.write("all:\n  children:\n    storage_servers:\n      hosts:\n        localhost:\n"
                     "          ansible_connection: local\n"
                     "          ansible_python_interpreter: %s\n" % sys.executable)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def play(self, phase, *extra):
        env = dict(os.environ, ANSIBLE_LOCALHOST_WARNING="False", ANSIBLE_NOCOLOR="1")
        cmd = ["ansible-playbook", "-i", self.inv, os.path.join(REPO, "os_upgrade_snapshot.yml"),
               "-e", "snapshot_phase=%s" % phase, "-e", "snapshot_root=%s" % self.tmp,
               "-e", "ansible_become=false"] + list(extra)
        return subprocess.run(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=600)

    def test_pre_post_diff(self):
        host = os.path.join(self.tmp, "os-upgrade", "localhost")
        r = self.play("post")
        self.assertNotEqual(r.returncode, 0, "post without a baseline must fail")
        self.assertFalse(os.path.isdir(host), "a refused run must not leave a directory behind")
        r = self.play("pre")
        self.assertEqual(r.returncode, 0, r.stdout.decode()[-2000:])
        self.assertTrue(os.path.isfile(os.path.join(host, "pre.txt")))
        r = self.play("pre")
        self.assertNotEqual(r.returncode, 0, "second pre must refuse to overwrite the baseline")
        self.assertIn(b"already exists", r.stdout)
        r = self.play("post")
        self.assertEqual(r.returncode, 0, r.stdout.decode()[-2000:])
        for f in ("post.txt", "diff.txt", "diff.html", "diff.json"):
            self.assertTrue(os.path.isfile(os.path.join(host, f)), f)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "os-upgrade", "SUMMARY.txt")))
        self.assertEqual(self.play("diff").returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
