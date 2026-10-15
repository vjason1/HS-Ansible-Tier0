# Azure Dynamic Inventory

> Part of the **manual (CLI) guide** — [back to the manual guide](README.md#2-configure-inventory).
> Using the GUI instead? See [GUI: Azure](../gui/azure.md).

Auto-discover VMs from Microsoft Azure.

> **Full walkthrough:** [DEPLOYMENT_GUIDE_AZURE.md](../../DEPLOYMENT_GUIDE_AZURE.md)
> covers the end-to-end Azure deployment — SKU/disk-layout selection,
> Accelerated Networking, NSG rules, the ephemeral resource disk, and zonal vs
> availability-set AZ mapping. The steps below are the inventory setup only.

## 1. Install the Azure Ansible collection and Python SDK
```bash
ansible-galaxy collection install azure.azcollection

# The collection ships its own (large) Python requirements file
pip3 install -r ~/.ansible/collections/ansible_collections/azure/azcollection/requirements.txt
```

## 2. Configure Azure authentication (pick one)
```bash
# Option 1: Azure CLI (simplest for interactive use)
az login
az account set --subscription <subscription-id>

# Option 2: Service principal via environment variables
export AZURE_SUBSCRIPTION_ID='...'
export AZURE_CLIENT_ID='...'
export AZURE_SECRET='...'
export AZURE_TENANT='...'

# Option 3: Managed identity — set `auth_source: msi` in inventory.azure.yml
# when the Ansible controller is itself an Azure VM.
```

## 3. Edit `inventory.azure.yml`
Set `include_vm_resource_groups` to the resource group(s) holding the Tier 0
VMs, and set `conditional_groups.storage_servers` to select them by VM size:

```yaml
plugin: azure.azcollection.azure_rm

include_vm_resource_groups:
  - tier0-rg

plain_host_names: true
auth_source: auto

conditional_groups:
  storage_servers: >-
    virtual_machine_size is defined and
    virtual_machine_size in ['Standard_L16s_v3', 'Standard_L32s_v3']
```

> **This filter is destructive if too broad** — whatever lands in
> `storage_servers` gets its disks RAIDed and mkfs'd. Verify with
> `ansible-inventory -i inventory.azure.yml --graph` before the first run.
> `DEPLOYMENT_GUIDE_AZURE.md` §4.3–4.4 cover finding your resource group and
> the three filter options (exact sizes / SKU family / tag).

> **Naming matters:** the plugin is only recognised for files ending in
> `azure_rm.yml` or `azure.yml`. `inventory.azure.yml` satisfies the second
> rule. Do **not** rename it to something like `inventory-azure.yml`.

## 4. Test the inventory
```bash
ansible-inventory -i inventory.azure.yml --list
ansible-inventory -i inventory.azure.yml --graph
ansible -i inventory.azure.yml storage_servers -m ping
```

## 5. Deploy
```bash
./scripts/run.sh -i inventory.azure.yml site.yml
```

**Azure Instance Metadata Exposed:**

| Variable | Source | Description | Example |
|----------|--------|-------------|---------|
| `azure_zone` | inventory + IMDS | Availability Zone, **1-based**. Empty on non-zonal VMs | `"2"` |
| `azure_fault_domain` | IMDS only | Platform fault domain, **0-based** | `"1"` |
| `azure_location` | inventory + IMDS | Region | `"westus2"` |
| `azure_vm_size` | inventory + IMDS | VM SKU | `"Standard_L16s_v3"` |
| `azure_resource_group` | inventory + IMDS | Resource group | `"tier0-rg"` |

**Azure AZ Mapping:**

| Azure placement | Hammerspace AZ | Note |
|-----------------|----------------|------|
| `zone` = `"1"` | `AZ1` | 1-based, used verbatim |
| `zone` = `"2"` | `AZ2` | |
| `platformFaultDomain` = `0` | `AZ1` | **0-based — mapped as +1** |
| `platformFaultDomain` = `1` | `AZ2` | |

Zone wins when both are present. Set `hammerspace_node_az` per host to override.

## 6. Non-zonal VMs (availability sets) — enable IMDS detection
The inventory plugin exposes `availability_zone` but **never** the fault
domain. If your VMs are in an availability set rather than availability zones,
the inventory alone gives every node the same (empty) zone. Turn on IMDS
detection in `vars/main.yml` so the fault domain is read from the VM itself:

```yaml
hammerspace_enable_az_mapping: true
hammerspace_azure_imds_az: true
```

This queries `http://169.254.169.254/metadata/instance/compute` on each host.
It is safe to leave enabled on non-Azure hosts — the request has a short
timeout, failure is tolerated, and the AZ chain simply falls through.

## 7. Azure ephemeral resource disk
Azure VMs get a temporary **resource disk** (usually `/dev/sdb`) that
cloud-init formats and mounts at `/mnt`. It is **wiped on
deallocate/redeploy** and must never join a Tier 0 RAID set. Disk discovery
protects it automatically — `/mnt` and `/mnt/resource` are in the
SYSTEM-mountpoint allow-list in
`roles/nvme_discovery/tasks/detect_boot_device.yml`. Verify before a real run:

```bash
./scripts/run.sh -i inventory.azure.yml site.yml --check --tags discovery
```

and confirm the "Disk classification" output lists the resource disk under
STRICT protected.

Choose `storage_type` to match the VM SKU:

| Azure SKU family | Local storage | `storage_type` |
|------------------|---------------|----------------|
| Lsv3 / Lasv3 (storage optimized) | Local NVMe | `nvme` |
| ND / NC (GPU) | Local NVMe | `nvme` |
| Attached Premium SSD v2 / Ultra Disk | Managed disks as `/dev/sd*` | `ssd` |

---

Next: configure variables and run — [manual guide §3](README.md#3-configure-variables).
