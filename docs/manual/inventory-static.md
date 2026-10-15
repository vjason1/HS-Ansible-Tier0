# Static Inventory (on-prem / manual host list)

> Part of the **manual (CLI) guide** — [back to the manual guide](README.md#2-configure-inventory).
> Using the GUI instead? See [GUI: Static hosts](../gui/static.md).

Edit `inventory.yml` to add your Tier 0 / LSS servers:

```yaml
all:
  children:
    storage_servers:
      hosts:
        tier0-node-01:
          ansible_host: 10.200.100.101
        tier0-node-02:
          ansible_host: 10.200.100.102
```

**With Availability Zones (on-prem multi-AZ):**

For on-premises deployments with multiple availability zones, nest hosts under AZ groups.
The playbook automatically detects `AZ<N>` group names and uses them as volume name prefixes
(requires `hammerspace_volume_az_prefix_enabled: true` in `vars/main.yml`).

```yaml
all:
  children:
    storage_servers:
      children:
        AZ1:
          hosts:
            node101:
              ansible_host: 10.200.101.216
            node102:
              ansible_host: 10.200.101.182
        AZ2:
          hosts:
            node201:
              ansible_host: 10.200.103.188
            node202:
              ansible_host: 10.200.103.228
        AZ3:
          hosts:
            node301:
              ansible_host: 10.200.100.135
            node302:
              ansible_host: 10.200.103.24
```

This produces volume names like `AZ1:node101::/hammerspace/hsvol0`. No per-host variables
needed — the AZ prefix is derived from the inventory group name automatically.

**Running locally on target server** (no SSH needed):
```yaml
all:
  children:
    storage_servers:
      hosts:
        localhost:
          ansible_connection: local
```

## Reaching hosts through a bastion

```yaml
# inventory.yml
storage_servers:
  hosts:
    plsm221h-01:
      ansible_host: 10.200.104.10
  vars:
    ansible_ssh_common_args: '-o ProxyJump=user@bastion.example.com'
```

---

Next: configure variables and run — [manual guide §3](README.md#3-configure-variables).
