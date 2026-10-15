# OCI Dynamic Inventory

> Part of the **manual (CLI) guide** — [back to the manual guide](README.md#2-configure-inventory).
> Using the GUI instead? See [GUI: OCI](../gui/oci.md).

Auto-discover instances from Oracle Cloud Infrastructure.

## 1. Install OCI CLI
```bash
# Interactive install (prompts for directories)
sudo bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"

# Non-interactive install (uses defaults)
sudo bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)" -- --accept-all-defaults

# Verify installation
oci --version
```

## 2. Install OCI Ansible collection and Python SDK
```bash
# Install OCI Ansible collection
ansible-galaxy collection install oracle.oci

# Install OCI Python SDK
pip3 install oci
```

## 3. Configure OCI authentication
```bash
# Create OCI config directory
mkdir -p ~/.oci

# Interactive setup (creates config file and API key)
oci setup config

# Or copy existing config from another machine
# scp user@source:~/.oci/config ~/.oci/
# scp user@source:~/.oci/oci_api_key.pem ~/.oci/
```

## 4. Edit `inventory.oci.yml`
Note: The file MUST be named with `.oci.yml` extension for the OCI plugin to recognize it.

```yaml
---
plugin: oracle.oci.oci
regions:
  - us-sanjose-1  # Your region
fetch_hosts_from_subcompartments: true

hostname_format_preferences:
  - "display_name"  # Use instance name as inventory_hostname
  - "private_ip"

# Filter to only running instances with specific shape
include_filters:
  - lifecycle_state: "RUNNING"
    shape: "VM.DenseIO.E5.Flex"  # Or BM.GPU.GB200-v3.4

# Create storage_servers group
groups:
  storage_servers: "'VM.DenseIO.E5.Flex' in shape"

# Set connection variables and OCI metadata
compose:
  ansible_host: private_ip
  ansible_user: "'ubuntu'"  # Quoted string literal for Jinja2
  ansible_python_interpreter: "'/usr/bin/python3'"
  ansible_become: true
  # OCI metadata as host variables
  oci_fault_domain: fault_domain
  oci_availability_domain: availability_domain
  # CPU and shape details
  oci_ocpus: shape_config.ocpus | default('')
  oci_memory_gb: shape_config.memory_in_gbs | default('')
  oci_processor_description: shape_config.processor_description | default('')
  oci_networking_bandwidth_gbps: shape_config.networking_bandwidth_in_gbps | default('')
  oci_local_disks: shape_config.local_disks | default('')
  oci_local_disks_total_gb: shape_config.local_disks_total_size_in_gbs | default('')
  # Hammerspace AZ prefix (disabled by default, configure in vars/main.yml)
  # Uncomment to enable auto-detection from fault domain:
  # hammerspace_volume_az_prefix: fault_domain | regex_replace('FAULT-DOMAIN-', 'AZ') ~ ":"

# Create groups based on fault domain and availability domain
keyed_groups:
  - key: fault_domain
    prefix: fd
    separator: "_"
  - key: availability_domain
    prefix: ad
    separator: "_"
```

## 5. Update `ansible.cfg` to use dynamic inventory and SSH key
```ini
[defaults]
inventory = inventory.oci.yml
private_key_file = /home/ubuntu/.ssh/ansible_admin_key
```

## 6. Test the inventory
```bash
# List discovered hosts
ansible-inventory -i inventory.oci.yml --list

# Show as graph
ansible-inventory -i inventory.oci.yml --graph

# Ping all storage servers
ansible storage_servers -m ping
```

**Find your compartment OCID:**
```bash
oci iam compartment list --query "data[].{name:name, id:id}" --output table
```

## OCI Instance Details (CPU, Memory, Storage)

The inventory automatically exposes instance shape details as host variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `oci_processor_description` | CPU type | "AMD EPYC 9J14 96-Core Processor" |
| `oci_ocpus` | Number of OCPUs | 32 |
| `oci_memory_gb` | Memory in GB | 512 |
| `oci_networking_bandwidth_gbps` | Network bandwidth | 200 |
| `oci_local_disks` | Number of local NVMe disks | 8 |
| `oci_local_disks_total_gb` | Total local disk size in GB | 51200 |

**Query instance details:**
```bash
# List CPU info for all hosts
ansible-inventory --list | jq '._meta.hostvars | to_entries[] | {host: .key, cpu: .value.oci_processor_description, ocpus: .value.oci_ocpus}'

# Full details for specific host
ansible-inventory --host <instance-name>
```

**Use in playbooks:**
```yaml
- name: Display instance specs
  debug:
    msg: |
      Host: {{ inventory_hostname }}
      CPU: {{ oci_processor_description }}
      OCPUs: {{ oci_ocpus }}
      Memory: {{ oci_memory_gb }} GB
      Network: {{ oci_networking_bandwidth_gbps }} Gbps
      Local Disks: {{ oci_local_disks }} ({{ oci_local_disks_total_gb }} GB total)
```

---

Next: configure variables and run — [manual guide §3](README.md#3-configure-variables).
