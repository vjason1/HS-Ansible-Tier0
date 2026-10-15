# Static Hosts (On-Prem / Manual Host List) — GUI Guide

For bare-metal and on-prem Tier 0 / LSS servers, or any hosts you list by name
and IP yourself. Everything is done in the configuration GUI; the
[manual (CLI) version](../manual/inventory-static.md) covers the same setup by
editing `inventory.yml` by hand.

| Step | Where |
|------|-------|
| 1. Prepare the controller | terminal (once) |
| 2. Start the GUI | terminal |
| 3. List your hosts | **Inventory → Static Hosts** |
| 4. Give Ansible SSH access | **Static Hosts → Copy SSH key to hosts…** |
| 5. Configure and deploy | [shared GUI steps](configure-and-deploy.md) |

## 1. Prepare the controller

The controller is the machine you run Ansible from (a workstation, bastion, or
one of the servers). It needs SSH reach to every host.

```bash
# Ansible (any one of these)
sudo dnf install -y ansible-core python3-pip      # RHEL / Rocky / Alma
sudo apt install -y ansible python3-pip            # Debian / Ubuntu
pip3 install ansible                                # anywhere else

git clone https://github.com/vjason1/HS-Ansible-Tier0.git
cd HS-Ansible-Tier0
git lfs pull                                        # DI RPMs in payload/ (only if you deploy DI)
ansible-galaxy collection install -r requirements.yml
```

No cloud SDK is needed for static hosts. On an air-gapped site, follow
[DEPLOYMENT_GUIDE_DARKSITE_HDD.md](../../DEPLOYMENT_GUIDE_DARKSITE_HDD.md) to stage the
bundle — the GUI itself needs no internet access.

## 2. Start the GUI

```bash
python3 gui/tier0_gui.py
```

Open the printed `http://127.0.0.1:8765/#token=…` URL. On a headless controller,
tunnel from your workstation first: `ssh -L 8765:127.0.0.1:8765 <user>@<controller>`.

## 3. List your hosts — Inventory → Static Hosts

The page edits `inventory.yml` as a tree of groups and hosts.

1. **storage_servers** — the Tier 0 / LSS nodes whose local drives become
   Tier 0 storage. **Add host** for each: *Host name* (any label; it becomes the
   Hammerspace node name) and *ansible_host* (IP or DNS name).
2. **Group variables** for `storage_servers`, one `key=value` per line — usually:
   ```
   ansible_user=ubuntu
   ansible_python_interpreter=/usr/bin/python3
   ```
   (`ansible_ssh_private_key_file` is filled in for you by step 4.)
3. Optional groups (**Add …** buttons at the bottom):
   - **di_nodes** — Data Instantiator / mover hosts (host vars `di_node_ip=…`).
     The same host may be in both groups (co-located DI).
   - **nfs_clients** — clients for the **Verify NFS** playbook.
4. **Availability zones**: click **Add AZ subgroup** on a group and move hosts
   into `AZ1`, `AZ2`, … subgroups. With *Hammerspace → Volumes & Availability
   Zones → AZ prefix in volume names* on, volumes are named
   `AZ1:node101::/hammerspace/hsvol0` automatically — no per-host settings.
   Hammerspace recommends at least 4 AZs when data lives only on Tier 0, 6 for
   best redundancy, with the same number of nodes per AZ.
5. **Save to file** → review the diff → **Save to file**.
6. **Show resolved inventory** runs `ansible-inventory --graph --vars` so you can
   confirm groups and variables.

Special cases:

- **Running on the storage server itself** (no SSH): one host named `localhost`
  with the variable `ansible_connection=local`.
- **Hosts behind a bastion**: group variable
  `ansible_ssh_common_args=-o ProxyJump=user@bastion.example.com`.
- `inventory.az.yml` (choose it in the *File* drop-down) is an AZ-grouped sample.
  "az" there means availability zone, not Azure.

## 4. Give Ansible SSH access — Copy SSH key to hosts

If you can already `ssh <user>@<host>` from the controller without a password,
skip this. Otherwise click **Copy SSH key to hosts…** on the Static Hosts page:

1. **SSH user** and **Password** — the account to log in as; the key is
   authorized for this user. (Prefilled from `ansible_user` when all hosts share one.)
2. **Public key to install** — the controller's key, or **Generate a new ed25519
   key pair** if it has none.
3. Tick the hosts → **Copy key**.

Each host gets the controller's *public* key appended to `~/.ssh/authorized_keys`
(once), and key-only login is then verified. With *Write ansible_user and
ansible_ssh_private_key_file into the inventory* on (default), Static Hosts turns
yellow — open it, review, **Save to file**. No private key file is copied anywhere.

The hosts must allow password logins for that user (`PasswordAuthentication yes`;
for `root` also `PermitRootLogin yes`). The job log states the reason for any
failure.

**Sudo:** the playbooks use sudo. If it asks for a password on your hosts, save it
under **Controller → Credentials → Sudo password for the hosts** and tick *Use the
saved sudo password* when you run, or type it on the run page each time.

## 5. Configure and deploy

Continue with the [shared GUI steps](configure-and-deploy.md): Hammerspace API and
credentials → storage layout → NFS / volumes → Overview check → dry run → deploy →
verify.

On-prem specifics for those steps:

| Topic | Setting |
|-------|---------|
| Drives | **Storage Layout → Storage device type** `nvme` (default). HDD/SSD servers: `hdd` / `ssd` / `scsi`, see [DEPLOYMENT_GUIDE_DARKSITE_HDD.md](../../DEPLOYMENT_GUIDE_DARKSITE_HDD.md). Hardware RAID: *Use software RAID* off + *Hardware RAID devices*. |
| AZ | AZ subgroups (step 3) + **Volumes & AZ → AZ prefix in volume names** on; for node labels also *AZ mapping for nodes* and *Apply AZ labels via API*. |
| Firewall | **Firewall** — leave on unless the network team manages host firewalls. The bare-metal playbook variant *Deploy on-prem NVMe (site-onprem-nvme.yml)* skips firewall configuration entirely. |
| Inventory on the Run page | `inventory.yml` is ticked by default — nothing to change. |

## Related

- [Manual (CLI) static inventory](../manual/inventory-static.md)
- [GUI reference](../../gui/README.md) · [Variable reference](../../VARIABLE_REFERENCE.md)
- [OS Upgrade Configuration Check](../os-upgrade-check.md) · [Rack operations runbook](../../RACK_OPERATIONS_RUNBOOK.md)
