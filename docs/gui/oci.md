# Oracle Cloud (OCI) — GUI Guide

For Tier 0 on OCI instances (e.g. `BM.GPU.GB200-v3.4` GPU shapes, `VM.DenseIO`)
discovered automatically through the OCI dynamic inventory. The
[manual (CLI) version](../manual/inventory-oci.md) and the end-to-end
[DEPLOYMENT_GUIDE.md](../../DEPLOYMENT_GUIDE.md) cover the same ground from the
command line.

| Step | Where |
|------|-------|
| 1. Prepare the controller + OCI access | terminal (once) |
| 2. Start the GUI | terminal |
| 3. Select your instances | **Inventory → OCI (dynamic)** |
| 4. SSH access | your OCI SSH key on the controller |
| 5. AZ strategy | **Hammerspace → Volumes & Availability Zones** |
| 6. Configure and deploy | [shared GUI steps](configure-and-deploy.md) |

## 1. Prepare the controller and OCI access

The GUI can't sign in to OCI for you — do this once in a terminal on the controller:

```bash
pip3 install ansible oci
git clone https://github.com/vjason1/HS-Ansible-Tier0.git
cd HS-Ansible-Tier0
git lfs pull                                   # DI RPMs, only if you deploy DI
ansible-galaxy collection install -r requirements.yml   # includes oracle.oci

# OCI CLI + API key (creates ~/.oci/config)
bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)" -- --accept-all-defaults
oci setup config

# Your compartment OCID (needed by some utilities)
oci iam compartment list --query "data[].{name:name, id:id}" --output table
```

## 2. Start the GUI

```bash
python3 gui/tier0_gui.py
```

Open the printed URL (headless controller: `ssh -L 8765:127.0.0.1:8765 <user>@<controller>` first).

## 3. Select your instances — Inventory → OCI (dynamic)

This menu edits `inventory.oci.yml` (the file name must end in `.oci.yml` for the plugin).

| Field | What to set |
|-------|-------------|
| **Regions** | Your region(s), one per line, e.g. `us-sanjose-1` |
| **Include sub-compartments** | On to search child compartments |
| **Host filters (Jinja2)** | Which instances are considered, e.g. `lifecycle_state == 'RUNNING'` and `shape == 'BM.GPU.GB200-v3.4'` |
| **Groups** | The `storage_servers` expression — **everything it matches gets its local drives RAIDed**, e.g. `storage_servers: "shape == 'BM.GPU.GB200-v3.4'"` |
| **Hostname preference** | `display_name` first, so instance names become Hammerspace node names |

The SSH user and connection details live under `compose:` (`ansible_user: "'ubuntu'"`,
`ansible_host: private_ip`), which the form doesn't cover — click **Edit the whole
file** if your image uses another user. The same file exposes OCI metadata as host
variables (`oci_fault_domain`, `oci_shape`, `oci_ocpus`, `oci_memory_gb`,
`oci_local_disks`, …) and creates `fd_FAULT_DOMAIN_N` / `ad_…` groups.

**Save to file**, then **Show resolved inventory**: check that exactly the intended
instances appear under `storage_servers` before going further.

## 4. SSH access

Ansible connects to `private_ip` with your OCI instance key. Make it available
without hard-coding a path in the repo: an `IdentityFile` entry in `~/.ssh/config`,
`ssh-add` into an agent, or **Controller → Ansible Settings → SSH private key**.

Instances you can only reach with a password (custom images)? Add them to
**Static Hosts** temporarily and use **Copy SSH key to hosts…** — see the
[static guide](static.md#4-give-ansible-ssh-access--copy-ssh-key-to-hosts).

GPU images normally have passwordless sudo; if not, see *Sudo* in the
[shared steps](configure-and-deploy.md#1-point-tier-0-at-your-hammerspace-cluster).

## 5. Choose how AZs are assigned

**Hammerspace → Volumes & Availability Zones**:

| Strategy | Settings | Result |
|----------|----------|--------|
| **Fault domain** | *AZ prefix in volume names* on, *AZ prefix mode* `auto`; for node labels also *AZ mapping for nodes* + *Apply AZ labels via API* | `FAULT-DOMAIN-1` → `AZ1:` …, from inventory metadata |
| **GPU memory fabric** (GB200 / NVLink fabrics) | *OCI GPU-fabric AZ detection* on | Instances sharing a GPU fabric share an AZ; mappings learned from existing volume names and `gpu_fabric_data.txt` |
| **None** | *AZ prefix in volume names* off | `instance-name::/hammerspace/hsvol0` |

To re-map fabrics later: **Run Playbooks → Collect OCI GPU fabric data**, then
**Hammerspace Utilities → Assign AZ prefixes (OCI GPU fabric)** (dry run first) and,
if you want instance names to show the AZ, **Rename OCI instances with AZ**. The
fabric procedure is §9 of [DEPLOYMENT_GUIDE.md](../../DEPLOYMENT_GUIDE.md).

## 6. Configure and deploy

Continue with the [shared GUI steps](configure-and-deploy.md). OCI specifics:

- **The first time, on the Run page, untick `inventory.yml` and tick `inventory.oci.yml`.**
  The app remembers that choice for every later run and for OS Upgrade Check. To make
  it the command-line default too: **Controller → Ansible Settings → Default inventory**.
- Adding instances to a running cluster: run **Preflight: find new instances** first,
  then deploy with *Limit* `@.new_instances_limit` so registered nodes are skipped.
- Many nodes at once: **API Throttling & Timeouts → Serial batch size** `2`.
- Storage: GPU shapes' local NVMe → *Storage device type* `nvme` (default).

## Alternatives without SSH

`oci_deploy.py` (OCI Run Command) and the `oci-function/` event-driven auto-provisioning
deploy without any SSH access; each instance clones this repository and runs the
playbook locally. These are command-line / Terraform tools — see *Zero-SSH Deployment*
in the [manual guide](../manual/README.md#zero-ssh-deployment-oci-run-command).

## Related

- [Manual (CLI) OCI inventory](../manual/inventory-oci.md) · [DEPLOYMENT_GUIDE.md](../../DEPLOYMENT_GUIDE.md) (OCI end to end)
- [GUI reference](../../gui/README.md) · [OS Upgrade Configuration Check](../os-upgrade-check.md) · [Rack operations runbook](../../RACK_OPERATIONS_RUNBOOK.md)
