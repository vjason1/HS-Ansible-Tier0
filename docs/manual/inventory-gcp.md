# GCP Compute Engine Dynamic Inventory

> Part of the **manual (CLI) guide** — [back to the manual guide](README.md#2-configure-inventory).
> Using the GUI instead? See [GUI: GCP](../gui/gcp.md).

Auto-discover instances from Google Cloud Platform Compute Engine.

## 1. Install GCP Ansible collection and Python SDK
```bash
# Install GCP Ansible collection
ansible-galaxy collection install google.cloud

# Install GCP Python SDK
pip3 install google-auth requests
```

## 2. Configure GCP authentication
```bash
# Option 1: Service account file (recommended for automation)
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# Option 2: Application default credentials (for local development)
gcloud auth application-default login

# Option 3: Compute Engine default service account (when running on GCE)
# No configuration needed - uses instance metadata
```

## 3. Edit `inventory.gcp.yml`
**Important:** Update the `projects` field with your GCP project ID:

```yaml
plugin: google.cloud.gcp_compute
projects:
  - your-gcp-project-id    # REQUIRED: Replace with your project ID

zones:
  - us-central1-a
  - us-central1-b
  - us-central1-c

auth_kind: application

# Filter to only running instances
filters:
  - status = RUNNING
  # - labels.role = storage    # Filter by label

# Create storage_servers group
groups:
  storage_servers: "'tier0' in (labels.role | default(''))"

compose:
  ansible_host: networkInterfaces[0].networkIP
  ansible_user: "'ubuntu'"
  # Hammerspace AZ prefix from zone
  hammerspace_volume_az_prefix: >-
    "AZ" ~ (((zone | basename)[-1:] | lower | ord) - 96) ~ ":"
```

## 4. Update `ansible.cfg` to use GCP inventory
```ini
[defaults]
inventory = inventory.gcp.yml
```

## 5. Test the inventory
```bash
# List discovered hosts
ansible-inventory -i inventory.gcp.yml --list

# Show as graph
ansible-inventory -i inventory.gcp.yml --graph

# Ping all storage servers
ansible storage_servers -m ping
```

**GCP Instance Metadata Exposed:**

| Variable | Description | Example |
|----------|-------------|---------|
| `gcp_instance_name` | Instance name | "tier0-node-01" |
| `gcp_machine_type` | Machine type | "a2-highgpu-8g" |
| `gcp_zone` | Zone | "us-central1-a" |
| `gcp_region` | Region | "us-central1" |
| `gcp_private_ip` | Internal IP | "10.128.0.10" |

**GCP Zone Mapping:**

| GCP Zone | Hammerspace AZ Prefix |
|----------|----------------------|
| us-central1-a | `AZ1:` |
| us-central1-b | `AZ2:` |
| us-central1-c | `AZ3:` |

---

Next: configure variables and run — [manual guide §3](README.md#3-configure-variables).
