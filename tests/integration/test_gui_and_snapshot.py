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
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "gui"))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import yaml  # noqa: E402

import catalog  # noqa: E402
import schema  # noqa: E402
import tier0_gui  # noqa: E402
import tier0_snapshot_diff as differ  # noqa: E402
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
        tier0_gui.Handler.app = {"repo": repo, "token": "tok", "verbose": False,
                                 "jobs": tier0_gui.JobManager(cls.repo_dir, os.path.join(cls.tmp, "jobs"))}
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
