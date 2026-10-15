# OS Upgrade Configuration Check

> Works the same from the GUI (**Operate → OS Upgrade Check**) and from the command line.


Operating-system upgrades and kernel patches routinely undo Tier 0
configuration without any error: a new initramfs without `mdadm.conf`
(arrays come back as `md127`), `/etc/nfs.conf` replaced by the package
(nfsd drops from 128 threads to 8), sunrpc slot tables back to the kernel
default of 2, `irqbalance` re-enabled over the IRQ pinning, a renamed NIC,
read-ahead back to 128 KB. `os_upgrade_snapshot.yml` captures each node's
configuration before and after the upgrade and tells you exactly what changed.

### Workflow

```bash
# 1. Before the upgrade (read-only)
./scripts/run.sh playbook os_upgrade_snapshot.yml -i inventory.yml \
  -e snapshot_phase=pre -e snapshot_run=2026-10-rocky-9.5

# 2. Upgrade the OS and reboot the node(s) with your normal process.

# 3. After the upgrade: capture again and diff
./scripts/run.sh playbook os_upgrade_snapshot.yml -i inventory.yml \
  -e snapshot_phase=post -e snapshot_run=2026-10-rocky-9.5

# Rebuild the reports from existing snapshots without contacting hosts
./scripts/run.sh playbook os_upgrade_snapshot.yml -e snapshot_phase=diff -e snapshot_run=2026-10-rocky-9.5
```

The same workflow is a guided four-step page in the GUI (**Operate → OS
Upgrade Check**), which also renders every report and can compare any two
archived captures of a host. Its **Upload to Hammerspace…** buttons send a host's
reports, or a whole run, to Hammerspace with secrets masked (from a shell:
`python3 scripts/hs_upload.py snapshots/<run>`); see
[gui/README.md](../gui/README.md#uploading-to-hammerspace).

![OS Upgrade Check in the GUI: the four steps and the per-host results of a run](images/gui/upgrade-check.png)

| Variable | Default | Meaning |
|----------|---------|---------|
| `snapshot_phase` | *(required)* | `pre`, `post`, or `diff` |
| `snapshot_run` | `os-upgrade` | Run name — use one per maintenance window |
| `snapshot_hosts` | `storage_servers` | Host pattern, e.g. `di_nodes` or `storage_servers:di_nodes` |
| `snapshot_root` | `<repo>/snapshots` | Output directory on the controller |
| `snapshot_overwrite` | `false` | Allow replacing an existing `pre.txt` |
| `snapshot_mount_root` | `mount_base_path` | Root of the Tier 0 data mounts |

Safety: the collector only queries state. A second `pre` for the same run is
refused (it would replace the baseline with post-upgrade state); `post` is
refused for a host without a baseline; `--check` works.

### Output

```
snapshots/2026-10-rocky-9.5/
├── SUMMARY.txt / SUMMARY.json      one line per host: status, critical/warning/info counts, kernel change
└── node101/
    ├── pre.txt   post.txt          latest captures
    ├── pre-20261014T231729Z.txt    every capture is archived with a UTC timestamp
    ├── diff.txt                    text report
    ├── diff.html                   self-contained HTML report (open in any browser)
    └── diff.json                   machine-readable result
```

In the GUI, *Open report* shows the same diff for one host — what needs
attention first, then headline values before and after:

![Upgrade report for one host: what needs attention and key facts before and after](images/gui/upgrade-report.png)

### What is captured and how it is compared

The snapshot has ~50 sections. Volatile values (counters, timestamps, PIDs,
IRQ numbers, autofs fds, resync progress, free space) are normalized, so
two captures of an unchanged node are identical. Each section has a severity:

| Severity | Sections | Why |
|----------|----------|-----|
| **critical** | `raid.*` (mdstat, `mdadm --detail`, mdadm.conf, initramfs contents), `fs.*` (fstab, mounts, XFS geometry, PrimaryData presence), `nfs.exports_*`, `nfs.runtime` (threads, versions, max_block_size, sunrpc slots), `storage.nvme_controllers` (keyed by serial, so renumbering shows), `storage.block_devices`, `net.links`, `services.failed` | Tier 0 data path — a change here can make Hammerspace mark volumes suspected |
| **warning** | `perf.*` (modprobe/sysctl/udev files, script hashes, IRQ affinity), `kernel.sysctl` / `module_params` / `cmdline`, `storage.queue_settings`, `services.key_units`, `packages.config_leftovers`, NIC drivers/firmware, RDMA, firewall, SELinux, DI | Tuning or behaviour that silently reverts |
| **info** | OS release, kernel version, installed kernels, packages, repositories, enabled unit files, CPU | Expected to change during an upgrade |

Instead of raw text, packages are reported as upgraded / downgraded / added /
removed (multi-version packages such as `kernel` separately), and key=value
sections (sysctl, nfsd runtime, headline facts) as before → after tables. The
report also warns when both captures are from the same boot (the node has not
been rebooted onto the new kernel yet).

### Without Ansible

The collector and the diff tool also work by hand on a single node:

```bash
sudo bash scripts/tier0_config_snapshot.sh > pre.txt
# ...upgrade, reboot...
sudo bash scripts/tier0_config_snapshot.sh > post.txt
python3 scripts/tier0_snapshot_diff.py pre.txt post.txt --html diff.html   # exit 0 = identical, 1 = changed
```

Environment overrides for the collector: `TIER0_MOUNT_ROOT` (default
`/hammerspace`), `TIER0_SNAPSHOT_TIMEOUT` (per-command timeout, default 30 s).

For where this fits in planned maintenance (availability-drop, health checks)
and how to fix the usual findings, see
[RACK_OPERATIONS_RUNBOOK.md](../RACK_OPERATIONS_RUNBOOK.md) §3 Step 1b and §4 Step 1b.
