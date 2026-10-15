# Ansible Automation for Hammerspace Tier 0 / LSS

Automates turning the local NVMe (or SSD/HDD) of GPU and storage servers into
**Hammerspace Tier 0** storage: drive discovery with boot-drive protection,
NUMA-aware RAID, XFS, NFS exports, Hammerspace best-practice tuning, firewall,
mount protection, and registration of nodes, volumes and shares with the
Hammerspace cluster — plus the optional Data Instantiator (DI / NFS mover).
Based on the [Hammerspace Tier 0 Deployment Guide](https://hammerspace.com).

Tier 0 delivers data to GPUs at local-NVMe speed, reducing checkpoint times and
improving AI workload performance by up to 10× compared to networked storage.

## Choose how you want to work

Both paths drive the same playbooks and edit the same files (`vars/main.yml`,
`vars/vault.yml`, `inventory*.yml`, `ansible.cfg`), so you can switch at any time.

| | **GUI** (recommended) | **Manual / CLI** (legacy) |
|---|---|---|
| How | Local web app: forms with defaults and help for every setting, diff preview before each save, playbooks run with live output | Edit YAML by hand, run `ansible-playbook` yourself |
| Start | `python3 gui/tier0_gui.py` | `./scripts/run.sh playbook site.yml ...` |
| Guide | Pick your platform below → **GUI guide** | [Manual (CLI) guide](docs/manual/README.md) |
| Reference | [GUI reference](gui/README.md) | [Variable reference](VARIABLE_REFERENCE.md) |

The GUI needs only Python 3.6+ and PyYAML (installed with Ansible) and no internet
access, so it also runs on air-gapped controllers.

## Pick your platform

| Where your Tier 0 nodes run | GUI guide | Manual (CLI) guide | End-to-end deployment guide (CLI) |
|------------------------------|-----------|--------------------|-----------------------------------|
| **On-prem / bare metal / any hosts you list yourself** | [GUI: static hosts](docs/gui/static.md) | [Static inventory](docs/manual/inventory-static.md) | [Dark site (HDD / HW RAID)](DEPLOYMENT_GUIDE_DARKSITE_HDD.md) |
| **Oracle Cloud (OCI)** | [GUI: OCI](docs/gui/oci.md) | [OCI inventory](docs/manual/inventory-oci.md) | [OCI deployment guide](DEPLOYMENT_GUIDE.md) |
| **AWS EC2** | [GUI: AWS](docs/gui/aws.md) | [AWS inventory](docs/manual/inventory-aws.md) | [OCI guide](DEPLOYMENT_GUIDE.md) from §5 |
| **Microsoft Azure** | [GUI: Azure](docs/gui/azure.md) | [Azure inventory](docs/manual/inventory-azure.md) | [Azure deployment guide](DEPLOYMENT_GUIDE_AZURE.md) · [existing mounts](DEPLOYMENT_GUIDE_AZURE_EXISTING_MOUNT.md) |
| **Google Cloud (GCP)** | [GUI: GCP](docs/gui/gcp.md) | [GCP inventory](docs/manual/inventory-gcp.md) | [OCI guide](DEPLOYMENT_GUIDE.md) from §5 |

Each GUI guide covers the platform-specific part (controller prerequisites,
cloud authentication, the inventory, platform settings) and then continues with
the shared [Configure and Deploy with the GUI](docs/gui/configure-and-deploy.md).

## Quick start (GUI)

```bash
git clone https://github.com/vjason1/HS-Ansible-Tier0.git
cd HS-Ansible-Tier0
git lfs pull                                    # DI RPMs in payload/ (only for DI)
ansible-galaxy collection install -r requirements.yml
python3 gui/tier0_gui.py                        # open the printed http://127.0.0.1:8765/#token=... URL
# headless controller: ssh -L 8765:127.0.0.1:8765 <user>@<controller>, then open the URL locally
```

Then: **Inventory** (your hosts) → **Hammerspace API** + **Credentials** → **Storage
Layout** → **Overview** (configuration check) → **Run Playbooks** (dry run, then deploy).

## What it does

- **Storage** — NVMe/SCSI discovery grouped by NUMA node; the boot/OS disks are
  always excluded; RAID per Hammerspace guidance (power-of-2, CPU-vendor tuning);
  XFS `agcount=512`; UUID fstab; mdadm persistence; re-run safe (adopts existing
  `md127` arrays). Alternatives: hardware RAID, manual arrays, or existing mounts.
- **NFS and tuning** — `/etc/nfs.conf` (128 threads, v3 + v4.2, RDMA), exports with
  `no_root_squash` for Hammerspace/movers and `root_squash` for clients, sunrpc slot
  tables, nfsd direct I/O, read-ahead, NIC IRQ pinning, TCP sysctl, systemd mount
  guards + remount watchdog, firewall (firewalld/UFW/iptables).
- **Hammerspace** — API preflight, node and volume registration with throttling, AZ
  naming and labels (OCI fault domain / GPU fabric, AWS/GCP zones, Azure zone or
  IMDS, inventory groups), volume groups, shares and objectives, S3, cluster settings.
- **Data Instantiator** — host RPM, container (podman/docker) or Kubernetes DaemonSet;
  decommission.
- **Clouds** — dynamic inventories for OCI, AWS, GCP and Azure; preflight to deploy only
  new instances; zero-SSH deployment on OCI (Run Command / event-driven Functions).
- **Operations** — [OS upgrade configuration check](docs/os-upgrade-check.md)
  (before/after diff of everything Tier 0 depends on), availability-drop for
  maintenance, node/volume cleanup, host reset.

Full feature list and directory layout: [manual guide → Features](docs/manual/README.md#features).

## Documentation

| Document | Covers |
|----------|--------|
| [GUI reference](gui/README.md) | Every menu, how saves work, credentials and sudo passwords, SSH key setup, running jobs, security model |
| [Configure and deploy with the GUI](docs/gui/configure-and-deploy.md) | The shared GUI steps after the inventory |
| [Manual (CLI) guide](docs/manual/README.md) | The complete legacy reference: every option in YAML, troubleshooting, utility scripts, DI, zero-SSH, decommission |
| [VARIABLE_REFERENCE.md](VARIABLE_REFERENCE.md) | Every variable with its default |
| [OS Upgrade Configuration Check](docs/os-upgrade-check.md) | Before/after snapshots around OS upgrades |
| [RACK_OPERATIONS_RUNBOOK.md](RACK_OPERATIONS_RUNBOOK.md) | Planned maintenance, RMA, node removal and re-addition |
| [OCI](DEPLOYMENT_GUIDE.md) · [Azure](DEPLOYMENT_GUIDE_AZURE.md) · [Azure existing mounts](DEPLOYMENT_GUIDE_AZURE_EXISTING_MOUNT.md) · [Dark site](DEPLOYMENT_GUIDE_DARKSITE_HDD.md) deployment guides | End-to-end deployment walkthroughs (CLI) |
| [AUTOMATION_GUIDE.md](AUTOMATION_GUIDE.md) | Automated provisioning (OCI Run Command, Functions, cloud-init) |
| [tests/integration/README.md](tests/integration/README.md) | The test suite (`./tests/run_all.sh`) |

## Requirements

- An Ansible controller (Linux or macOS) with Ansible 2.9+ / ansible-core and the
  collections in `requirements.yml`, plus the SDK for your cloud (see your platform guide).
- Targets: Debian/Ubuntu or RHEL/Rocky/Alma/CentOS, reachable over SSH with sudo
  (or run locally with `ansible_connection: local`).
- For Hammerspace registration: the Anvil management IP reachable on port 8443.

## References

- [Hammerspace Ansible examples](https://github.com/hammer-space/ansible)
- [Hammerspace Toolkit (HSTK)](https://github.com/hammer-space/hstk)
- [Hammerspace Grafana dashboards](https://github.com/hammer-space/grafana-dashboards)

## License

MIT
