# Configure and Deploy with the GUI

The steps every deployment shares, once your hosts are in an inventory. Start
from your platform's guide — [Static / on-prem](static.md), [OCI](oci.md),
[AWS](aws.md), [GCP](gcp.md) or [Azure](azure.md) — which sends you here.

Every menu below edits a real file in the repository (`vars/main.yml`,
`vars/vault.yml`, `ansible.cfg`, `inventory*.yml`). Each save shows the exact
diff first, changes only the lines you edited (comments are kept) and backs up
the previous version to `.gui-backups/`. A menu's status dot turns **yellow**
while it has unsaved changes and **red** when the Overview's configuration
check finds an error in it. Full reference: [gui/README.md](../../gui/README.md).

## 1. Point Tier 0 at your Hammerspace cluster

**Hammerspace → Hammerspace API**

| Field | Set to |
|-------|--------|
| Anvil management IP / host | Your Anvil management IP (the sample `10.0.10.15` is flagged on the Overview until you change it). NFS export and jumbo-frame test lists derive from it. |
| API user | An admin-role user (default `admin`) |
| Validate TLS certificates | Off for the usual self-signed Anvil certificate |
| HTTP method | `auto` (switches to curl on Python 3.13+) |

**Controller → Credentials** — the password itself:

1. If `vars/vault.yml` is plaintext, click **Encrypt vars/vault.yml…** and choose a vault
   password. Keep it safe; runs need it.
2. **Set a value** on *Hammerspace API password*, then **Save to file**.

The API field `hammerspace_api_password` keeps pointing at
`{{ vault_hammerspace_api_password }}` — leave it that way. (On Azure you can pull
the password from Key Vault instead: *Azure Key Vault URI* on the same menu.)

If sudo on the hosts asks for a password, also set *Sudo password for the
hosts* here (or type it per run — see step 6).

## 2. Describe the storage

**Tier 0 Configuration → Storage Layout** — pick ONE mode:

| Mode | When | Fields |
|------|------|--------|
| Dynamic discovery (default) | Local NVMe / SSD to be RAIDed per NUMA node; boot drive excluded automatically | *Dynamic drive discovery* on, *Storage device type* (`nvme`, `ssd`, `hdd`, `scsi`, `all`) |
| Existing-mount mode | Filesystems you already built and mounted | *Existing-mount mode: filesystems to use* (no RAID, mkfs or fstab changes) |
| Hardware RAID | A RAID controller presents logical volumes | *Use software RAID* off, *Hardware RAID devices* |
| Manual arrays | Exact control | *Dynamic drive discovery* off, *Manual RAID arrays*, *Manual mount points* |

Then as needed:

- **RAID Sizing & CPU Tuning** — RAID level (0 for Tier 0), grouping (`numa`
  default), drives per array, power-of-2 rounding.
- **Device Exclusions** — drives to keep out (by name, serial, model, NUMA node,
  PCIe address).
- **Pre-checks** — expected drive count, 4K sectors, MTU 9000 targets, optional iperf.
- **Safety Switches** — leave everything off. These are the destructive overrides.

## 3. NFS exports and Hammerspace volumes

- **Tier 0 Configuration → NFS Server & Exports** — add secondary Anvil/DSX IPs
  to *Hammerspace node IPs*, mover/DI IPs to *Mover / DI IPs*, and narrow
  *Client subnets* (the default `0.0.0.0/0` is noted on the Overview).
- **Hammerspace → Volumes & Availability Zones** — thresholds, availability /
  durability, and how the AZ is derived (your platform guide says which setting
  fits).
- Optional: **Volume Groups, Shares & Objectives**, **Cluster Settings**, **S3**.
- Large deployments (10+ nodes): **Hammerspace → API Throttling & Timeouts →
  Serial batch size** `2` so Anvil isn't flooded with volume adds.
- **Performance Tuning**, **Mount Protection**, **Firewall** — the defaults are
  the Hammerspace best practices; change only with a reason.

## 4. Data Instantiator (optional)

**Data Instantiator → Deployment**: turn on *Deploy DI*, choose host /
container / kubernetes, then **Packages** (run `git lfs pull` so `payload/` has
real RPMs — the Overview flags Git-LFS pointer files). DI hosts go in the
`di_nodes` group: **Inventory → Static Hosts** (or your cloud inventory's
groups).

## 5. Check the configuration

Open **Getting Started → Overview**. Fix everything listed as *Error* (each row
has a **Fix** link to the right menu); read the *Warnings*. Typical first-run
items: Anvil IP still the sample, vault password not set, destructive switch on.

## 6. Dry run

**Operate → Run Playbooks**, or the Overview's **Dry-run discovery and pre-checks**:

1. Playbook **Deploy Tier 0 (site.yml)**.
2. **Inventory (-i)**: the first time, `inventory.yml` is ticked. **For a cloud
   deployment untick it and tick your cloud inventory** (`inventory.oci.yml`, ...) —
   the app remembers your selection from then on, on this page and on OS Upgrade Check.
3. Tags `discovery` + `precheck`, **Check mode** on (default).
4. *Vault password* if the vault is encrypted (prefilled once you unlocked
   Credentials). *Sudo password on the hosts* only if sudo asks for one, or tick
   *Use the saved sudo password*.
5. **Run playbook** — output streams live; every run stays in **Job History**, and its
   Ansible log in **Logs & Reports**.

The discovery output shows, per host, which drives were found, which were
excluded (boot/system disks are always protected) and the RAID arrays that
*would* be built. Read it before going further.

Cloud deployments adding nodes to an existing cluster: run **Preflight: find new
instances** (`preflight_check.yml`) first; it writes `.new_instances_limit`,
which you can use as the *Limit* `@.new_instances_limit`.

## 7. Deploy

Same page, playbook **Deploy Tier 0 (site.yml)**, no tags, **Check mode off**.
Use *Limit* to start with one node. The GUI asks you to type `RUN` because this
creates RAID arrays and filesystems.

## 8. Verify

- Run **Verify NFS** (`verify_nfs.yml`).
- In Hammerspace: `anvil> node-list`, `anvil> storage-volume-list`.
- **Show resolved inventory** (on Static Hosts and on each cloud inventory menu) confirms what was targeted.

## 9. Operate

| Task | Where |
|------|-------|
| Before/after an OS upgrade or kernel patch | **Operate → OS Upgrade Check** ([details](../os-upgrade-check.md)) |
| Planned maintenance / RMA (availability-drop) | **Operate → Hammerspace Utilities → Availability drop**, procedure in [RACK_OPERATIONS_RUNBOOK.md](../../RACK_OPERATIONS_RUNBOOK.md) |
| Remove nodes and volumes from Hammerspace | **Hammerspace Utilities → Remove nodes & volumes** (dry run first) |
| Full teardown of a host | **Run Playbooks → Reset Tier 0 host** |
| Re-apply just NFS / perf settings | **Run Playbooks**, tags `discovery` + `nfs` (or `perf`) |
| Read any run's log, report or the GUI audit trail | **Operate → Logs & Reports** |
| Send logs or reports to Hammerspace support | **Logs & Reports**: tick them, **Upload to Hammerspace…** ([details](../../gui/README.md#uploading-to-hammerspace)) |

## Command-line equivalents

Everything here maps to the [manual (CLI) guide](../manual/README.md); the GUI
shows the exact `./scripts/run.sh playbook ...` command for every run.
