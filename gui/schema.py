"""Menu / field definitions for the Tier 0 configuration GUI.

Every menu maps to ONE configuration file, and every field to one variable in
that file. Labels, defaults and help text are taken from README.md,
VARIABLE_REFERENCE.md and the comments in vars/main.yml — when you add a
variable to the repo, add it here too (tests/integration/test_gui_schema.py
fails if a top-level key of vars/main.yml has no field).

Field types
  bool   true/false switch          int / float   number input
  str    single-line text           secret        masked text
  enum   drop-down (options)        list          list of strings, one per line
  intlist list of integers          yaml          free-form YAML (lists of dicts, maps)

Field options
  default   documented default (shown as a hint; NOT written unless changed)
  help      help text shown under the field
  optional  may be "unset" (commented out) so the role default applies
  show_if   {other_key: value | [values]} - only shown when that condition holds
  danger    red warning shown when the field is enabled / non-default
  example   YAML inserted by the "Insert example" button (yaml fields)
"""


def F(key, ftype, label, help="", **kw):
    field = {"key": key, "type": ftype, "label": label, "help": help}
    field.update(kw)
    return field


def S(sid, title, description, fields, file="vars/main.yml", **kw):
    sec = {"id": sid, "title": title, "description": description, "file": file, "fields": fields}
    sec.update(kw)
    return sec


VARS = "vars/main.yml"
VAULT = "vars/vault.yml"

# ---------------------------------------------------------------------------
# vars/main.yml
# ---------------------------------------------------------------------------
STORAGE_LAYOUT = S(
    "storage-layout", "Storage Layout",
    "How Tier 0 finds and lays out local storage. Pick ONE mode: dynamic discovery "
    "(default — NVMe found automatically, grouped by NUMA node, boot drive excluded), "
    "manual RAID arrays, hardware RAID devices, or existing-mount mode (filesystems you "
    "already built). Docs: manual guide → Dynamic NVMe Discovery.",
    [
        F("use_dynamic_discovery", "bool", "Dynamic drive discovery",
          "Auto-discover drives, exclude the boot device, group by NUMA node and build one "
          "array per NUMA node. Turn off to use the manual RAID arrays below.", default=True),
        F("storage_type", "enum", "Storage device type",
          "Which devices discovery considers. ssd/hdd scan ONLY /dev/sd* using the kernel "
          "rotational flag; Azure synthetic SCSI reports rotational=1 even for Premium SSD, "
          "so use 'scsi' there.",
          options=["nvme", "hdd", "ssd", "scsi", "all"], default="nvme"),
        F("use_raid", "bool", "Use software RAID (mdadm)",
          "Set false for a hardware RAID controller (list its devices below) or for per-drive "
          "mounts (raid_grouping_strategy: per_drive). Over ~1000 projected volumes, RAID is "
          "recommended.", default=True),
        F("hw_raid_devices", "list", "Hardware RAID devices",
          "Logical volumes presented by a HW RAID controller (e.g. /dev/sda, /dev/md126, "
          "/dev/cciss/c0d0). Filesystems are created directly on these; mdadm is not run.",
          optional=True, show_if={"use_raid": False}, placeholder="/dev/sda\n/dev/sdb"),
        F("mount_base_path", "str", "Mount base path",
          "Arrays mount as <base>/hsvol0, <base>/hsvol1, ...", default="/hammerspace"),
        F("xfs_agcount", "int", "XFS allocation group count",
          "mkfs.xfs -d agcount=N. Hammerspace recommends 512. Unset to let mkfs.xfs choose.",
          default=512, optional=True),
        F("existing_mount_paths", "yaml", "Existing-mount mode: filesystems to use",
          "Use filesystems that are ALREADY mounted instead of discovery + RAID + mkfs. Each "
          "entry is a mountpoint or {path, subdir}. The playbook only mkdirs an export "
          "directory inside each — no mdadm, mkfs, fstab or mounts. Refused: paths that are "
          "not mountpoints, and system mounts. Leave [] for normal discovery.",
          default=[], example="- /mnt/raid0\n- path: /data/array1\n  subdir: tier0\n- path: /data/array2\n  subdir: ''\n"),
        F("existing_mount_subdir", "str", "Existing-mount export subdirectory",
          "Directory created inside each existing mount and exported. Per-entry 'subdir' "
          "overrides it; '' exports the mountpoint itself.", default="tier0", optional=True),
        F("existing_mount_dir_mode", "str", "Export directory mode", default="0755", optional=True),
        F("existing_mount_dir_owner", "str", "Export directory owner", default="root", optional=True),
        F("existing_mount_dir_group", "str", "Export directory group", default="root", optional=True),
        F("release_ephemeral_mountpoints", "list", "Release cloud scratch mounts (OPT-IN)",
          "Unmount these and drop them from /etc/fstab BEFORE discovery so their disks become "
          "Tier 0 candidates (Azure/CycleCloud HB-series: /tmp and /nvme). Mountpoints sharing "
          "a disk with /, /boot, /usr, /var, /etc, /home or active swap are always refused. "
          "A busy /tmp usually needs a reboot and re-run.",
          default=[], danger="Most destructive switch in the repo: the released disks are then "
                             "RAIDed and formatted.", placeholder="/tmp\n/nvme"),
        F("raid_arrays", "yaml", "Manual RAID arrays",
          "Used only when dynamic discovery is off. Tips: power-of-2 set sizes, NUMA-local "
          "drives, 512 KiB chunk.", optional=True, show_if={"use_dynamic_discovery": False},
          example="- name: md0\n  device: /dev/md0\n  level: 0\n  drives:\n    - /dev/nvme0n1\n"
                  "    - /dev/nvme1n1\n    - /dev/nvme2n1\n    - /dev/nvme3n1\n"),
        F("mount_points", "yaml", "Manual mount points",
          "Used only when dynamic discovery is off.", optional=True,
          show_if={"use_dynamic_discovery": False},
          example="- path: /hammerspace/hsvol0\n  device: /dev/md0\n  fstype: xfs\n"
                  "  label: hammerspace-hsvol0\n  mount_opts: defaults,nofail,discard\n"),
    ])

RAID_TUNING = S(
    "raid", "RAID Sizing & CPU Tuning",
    "How discovered drives are grouped into arrays, and the CPU-vendor tuning applied to "
    "them. Docs: manual guide → RAID Array Sizing Options, CPU-Optimized RAID Configuration.",
    [
        F("raid_level", "enum", "RAID level", "0 = stripe (Tier 0 default), 1 mirror, 5 parity, "
          "10 stripe+mirror.", options=[0, 1, 5, 10], default=0),
        F("raid_grouping_strategy", "enum", "Grouping strategy",
          "numa: one array per NUMA node (then split by max drives). single: one array across "
          "all drives. per_drive: no RAID, every drive mounted individually.",
          options=["numa", "single", "per_drive"], default="numa"),
        F("raid_max_drives_per_array", "int", "Max drives per array",
          "0 = unlimited (all drives of a NUMA node in one array). 4 → arrays of 4, etc.",
          default=0, min=0),
        F("raid_min_drives_per_array", "int", "Min drives per array",
          "Groups with fewer drives are skipped (useful for uneven NUMA layouts).", default=2, min=1),
        F("raid_power_of_2_drives", "bool", "Round down to a power of 2",
          "6 drives → 4 per array. Recommended for RAID 0.", default=False),
        F("raid_leftover_drives", "enum", "Leftover drives",
          "Drives that don't fit a full array: skip, separate (smaller array), add_last "
          "(uneven last array), individual (mounted without RAID).",
          options=["skip", "separate", "add_last", "individual"], default="skip"),
        F("cpu_optimized_raid", "bool", "CPU-optimized RAID tuning",
          "Detect the CPU (EPYC Genoa/Milan, Xeon SPR, Grace, Graviton, Ampere, ...) and apply "
          "its chunk size / queue depth / scheduler profile.", default=True),
        F("cpu_vendor_profile", "enum", "Force CPU profile",
          "Override auto-detection.", optional=True, default="auto",
          options=["auto", "amd_epyc", "amd_epyc_genoa", "intel_xeon", "intel_xeon_sapphire",
                   "arm_neoverse_v2", "arm_nvidia_grace", "arm_ampere_altra", "arm_graviton3",
                   "arm_graviton2", "arm_generic", "generic"]),
        F("raid_chunk_size", "int", "RAID chunk size (KB)",
          "Takes precedence over the CPU profile. 512 for EPYC/ARM, 256 for Xeon.",
          optional=True, default=512),
        F("nvme_queue_depth", "int", "NVMe queue depth", "Overrides the CPU profile.",
          optional=True, default=1024),
        F("nvme_io_scheduler", "enum", "NVMe I/O scheduler", "Overrides the CPU profile.",
          optional=True, options=["none", "mq-deadline", "kyber"], default="none"),
        F("wait_for_sync", "bool", "Wait for RAID sync",
          "Wait for mirrored/parity arrays to finish their initial sync.", optional=True, default=True),
        F("sync_timeout", "int", "Sync timeout (minutes)", optional=True, default=180),
        F("raid_sync_retries", "int", "RAID sync poll retries", "retries × delay = max wait.",
          optional=True, default=60),
        F("raid_sync_delay", "int", "RAID sync poll delay (s)", optional=True, default=30),
    ])

EXCLUSIONS = S(
    "exclusions", "Device Exclusions",
    "Keep specific drives out of RAID. The boot / OS drive is ALWAYS excluded automatically — "
    "these lists are for anything else (reserved drives, slower models, a second tier). "
    "Docs: manual guide → NVMe Device Exclusion.",
    [
        F("nvme_exclude_devices", "list", "NVMe: by device name", "e.g. nvme0n1", default=[],
          placeholder="nvme0n1"),
        F("nvme_exclude_paths", "list", "NVMe: by path", "e.g. /dev/nvme0n1", default=[]),
        F("nvme_exclude_serials", "list", "NVMe: by serial", "Stable across reboots — the most "
          "reliable choice.", default=[]),
        F("nvme_exclude_models", "list", "NVMe: by model", 'e.g. "Samsung SSD 980 PRO"', default=[]),
        F("nvme_exclude_numa_nodes", "intlist", "NVMe: by NUMA node", "Excludes every NVMe on "
          "these NUMA nodes.", default=[]),
        F("nvme_exclude_pcie_addresses", "list", "NVMe: by PCIe address", "e.g. 0000:03:00.0",
          default=[]),
        F("nvme_exclude_pcie_prefixes", "list", "NVMe: by PCIe bus prefix",
          "e.g. 0000:03 (bus 03) or 0000:e (buses e1–ef).", default=[]),
        F("scsi_exclude_devices", "list", "SCSI/SATA: by device name", "Used when storage_type "
          "is hdd/ssd/scsi/all. e.g. sda", default=[]),
        F("scsi_exclude_paths", "list", "SCSI/SATA: by path", default=[]),
        F("scsi_exclude_serials", "list", "SCSI/SATA: by serial", default=[]),
        F("scsi_exclude_models", "list", "SCSI/SATA: by model", 'e.g. "VIRTUAL-DISK"', default=[]),
    ])

HS_API = S(
    "hs-api", "Hammerspace API",
    "Connection to the Anvil REST API. Set hammerspace_api_host once — hammerspace_nodes, "
    "network_test_targets and the DI cluster IP derive from it. The password itself lives in "
    "Credentials (vars/vault.yml) or Azure Key Vault. Docs: manual guide → Hammerspace API Integration.",
    [
        F("hammerspace_api_host", "str", "Anvil management IP / host",
          "Enables registration. The API preflight fails in <30 s if it is unreachable or the "
          "password is wrong.", default="10.0.10.15", placeholder="10.1.2.3"),
        F("hammerspace_api_user", "str", "API user", "Admin role required.", default="admin"),
        F("hammerspace_api_password", "str", "API password (reference)",
          "Normally the vault reference {{ vault_hammerspace_api_password }}. Set the actual "
          "password under Credentials. Avoid putting a plaintext password here.",
          default="{{ vault_hammerspace_api_password }}"),
        F("hammerspace_api_port", "int", "API port", optional=True, default=8443),
        F("hammerspace_api_validate_certs", "bool", "Validate TLS certificates",
          "Keep false for self-signed Anvil certificates.", default=False),
        F("hammerspace_api_method", "enum", "HTTP method",
          "auto: uri module on Python < 3.13, curl otherwise. curl: always (required on 3.13+). "
          "uri: always (faster, broken on 3.13+).", options=["auto", "curl", "uri"], default="auto"),
        F("hammerspace_node_name", "str", "Node name override",
          "Defaults to the inventory hostname. Use an AZ prefix for AZ deployments: "
          "AZ1:tier0-node01. Usually set per host in inventory instead.", optional=True,
          default="{{ inventory_hostname }}"),
        F("hammerspace_node_ip", "str", "Node IP override",
          "Defaults to ansible_default_ipv4.address.", optional=True,
          default="{{ ansible_default_ipv4.address }}"),
        F("hammerspace_debug", "bool", "Debug API calls", optional=True, default=False),
        F("hammerspace_keyvault_uri", "str", "Azure Key Vault URI (opt-in)",
          "Pull the admin password from Key Vault instead of vars/vault.yml. Runs once on the "
          "controller. The identity needs 'Key Vault Secrets User'.", optional=True,
          placeholder="https://my-vault.vault.azure.net"),
        F("hammerspace_keyvault_secret_name", "str", "Key Vault secret name", optional=True,
          placeholder="hammerspace-admin-password"),
        F("hammerspace_keyvault_auth_source", "enum", "Key Vault auth source",
          "msi is usually right on a CycleCloud scheduler with a managed identity.",
          options=["auto", "cli", "credential_file", "env", "msi"], default="auto"),
    ])

VOLUMES_AZ = S(
    "volumes-az", "Volumes & Availability Zones",
    "How each mount becomes a Hammerspace storage volume, and how nodes/volumes get their "
    "AZ. Docs: manual guide → Volume Settings, Hammerspace Volume AZ Prefix Configuration, "
    "Availability Zones.",
    [
        F("hammerspace_add_volumes", "bool", "Add volumes automatically", default=True),
        F("hammerspace_volume_high_threshold", "float", "High threshold",
          "utilizationThreshold — triggers evacuation. Decimal: 0.98 = 98%.", default=0.98,
          min=0, max=1, step=0.01),
        F("hammerspace_volume_low_threshold", "float", "Low threshold",
          "utilizationEvacuationThreshold — target after evacuation. 0.90 = 90%.", default=0.90,
          min=0, max=1, step=0.01),
        F("hammerspace_volume_max_suspected_seconds", "int", "Max suspected time (s)",
          "--max-suspected-time: seconds from suspected to unavailable. 0 = immediate.", default=0),
        F("hammerspace_volume_unavailable_multiplier", "enum", "Availability drop",
          "0 = availability drops to 0 when unavailable (--availability-drop-enabled). "
          "1 = unchanged (--availability-drop-disabled). See set_availability_drop.py for RMAs.",
          options=[0, 1], default=0),
        F("hammerspace_volume_availability", "int", "Target availability", default=2),
        F("hammerspace_volume_durability", "int", "Target durability", default=3),
        F("hammerspace_create_placement_objectives", "bool", "Create placement objectives",
          default=True),
        F("hammerspace_skip_performance_test", "bool", "Skip performance test on add",
          "Faster deployment.", default=True),
        F("hammerspace_additional_ips", "list", "Additional volume access IPs", optional=True),
        F("hammerspace_excluded_ips", "list", "Excluded volume access IPs", optional=True),
        F("hammerspace_volume_az_prefix_enabled", "bool", "AZ prefix in volume names",
          "AZ1:node::/hammerspace/hsvol0. Also auto-detects AZ<N> inventory groups (on-prem).",
          default=False),
        F("hammerspace_volume_az_prefix_mode", "str", "AZ prefix mode",
          '"" none, "auto" (from the cloud placement: OCI fault domain / Azure zone), or a '
          'static prefix such as "AZ1:".', default="", show_if={"hammerspace_volume_az_prefix_enabled": True}),
        F("hammerspace_volume_az_prefix", "str", "AZ prefix override",
          "Direct override — ignores the two settings above when set.", optional=True,
          placeholder="WEST:"),
        F("hammerspace_enable_az_mapping", "bool", "AZ mapping for nodes",
          "Detect each node's AZ (explicit hammerspace_node_az > OCI fault domain > Azure zone > "
          "Azure fault domain > name prefix > AZ<N> group > default).", default=False),
        F("hammerspace_apply_az_labels", "bool", "Apply AZ labels via API", default=False),
        F("hammerspace_default_az", "str", "Default AZ", "Used when nothing else matches.",
          default="AZ1"),
        F("hammerspace_azure_imds_az", "bool", "Azure IMDS AZ detection",
          "REQUIRED for non-zonal Azure VMs (availability sets): reads platformFaultDomain "
          "from IMDS. Harmless elsewhere.", default=False),
        F("azure_imds_api_version", "str", "Azure IMDS API version", default="2021-02-01"),
        F("azure_imds_timeout", "int", "Azure IMDS timeout (s)", default=5),
        F("hammerspace_gpu_fabric_az", "bool", "OCI GPU-fabric AZ detection",
          "Instances sharing a GPU memory fabric get the same AZ; learns existing mappings from "
          "Hammerspace volume names and gpu_fabric_data.txt.", default=False),
        F("hammerspace_gpu_fabric_az_update_file", "bool", "Update gpu_fabric_data.txt",
          default=True, show_if={"hammerspace_gpu_fabric_az": True}),
    ])

GROUPS_SHARES = S(
    "groups-shares", "Volume Groups, Shares & Objectives",
    "Organize volumes and publish shares. Docs: manual guide → Creating Shares via API; "
    "VARIABLE_REFERENCE → Volume Groups / Shares / Share Objectives.",
    [
        F("hammerspace_volume_group", "str", "Join existing volume group",
          "Every volume this run creates is APPENDED to this existing group (never created "
          "here — the run fails if it doesn't exist). Existing members are preserved and "
          "membership re-verified. Leave empty to skip.", default="", placeholder="tier0-nvme"),
        F("hammerspace_create_volume_groups", "bool", "Create volume groups", default=False),
        F("hammerspace_volume_groups", "yaml", "Volume groups to create", optional=True,
          show_if={"hammerspace_create_volume_groups": True},
          example='- name: "tier0-az1-volumes"\n  location_pattern: "AZ1:*"\n'
                  '- name: "tier0-az2-volumes"\n  location_pattern: "AZ2:*"\n'),
        F("hammerspace_volume_group_members", "yaml", "Add volumes to existing groups",
          optional=True,
          example='- volume_name: "AZ1:tier0-node-01::/hammerspace/hsvol0"\n'
                  '  group_name: "tier0-az1-volumes"\n'),
        F("hammerspace_create_shares", "bool", "Create shares", default=False),
        F("hammerspace_update_existing_shares", "bool", "Update existing shares", optional=True,
          default=False, show_if={"hammerspace_create_shares": True}),
        F("hammerspace_shares", "yaml", "Shares", optional=True,
          show_if={"hammerspace_create_shares": True},
          example='- name: checkpoints\n  path: /checkpoints\n  export_options:\n'
                  '    - subnet: "10.200.104.0/24"\n      accessPermissions: "RW"\n'
                  '      rootSquash: true\n  objective: "availability-3-nines"\n'),
        F("hammerspace_apply_share_objectives", "bool", "Apply share objectives", default=False),
        F("hammerspace_share_objectives", "yaml", "Share objectives",
          "Objectives: availability-3-nines, availability-4-nines, performance-high, "
          "durability-high.", optional=True, show_if={"hammerspace_apply_share_objectives": True},
          example='- share_name: "checkpoints"\n  objective: "availability-3-nines"\n'
                  '  applicability: "TRUE"\n'),
    ])

S3 = S(
    "s3", "S3 / Object Storage",
    "Add S3 nodes and buckets as Hammerspace volumes, and the internal S3 server. Keep keys "
    "in Credentials and reference them as {{ vault_... }}.",
    [
        F("hammerspace_add_s3_nodes", "bool", "Add S3 nodes", default=False),
        F("hammerspace_s3_nodes", "yaml", "S3 nodes", optional=True,
          show_if={"hammerspace_add_s3_nodes": True},
          example='- name: "aws-archive"\n  region: "us-west-2"\n'
                  '  access_key_id: "{{ vault_s3_access_key }}"\n'
                  '  secret_access_key: "{{ vault_s3_secret_key }}"\n'
                  '  # endpoint: "s3.us-west-2.amazonaws.com"\n'),
        F("hammerspace_add_object_volumes", "bool", "Add object-storage volumes", default=False),
        F("hammerspace_object_volumes", "yaml", "Object-storage volumes", optional=True,
          show_if={"hammerspace_add_object_volumes": True},
          example='- name: "archive-vol"\n  node_name: "aws-archive"\n'
                  '  bucket_name: "my-archive-bucket"\n  prefix: "hammerspace/"\n'
                  '  shared: true\n  compression: "NO_COMPRESSION"\n'),
        F("hammerspace_create_s3_server", "bool", "Create S3 server", default=False),
        F("hammerspace_s3_server", "yaml", "S3 server", optional=True,
          show_if={"hammerspace_create_s3_server": True},
          example='name: "internal-s3"\nport: 9000\ntls_enabled: false\nenabled: true\n'),
        F("hammerspace_create_s3_users", "bool", "Create S3 users", default=False),
        F("hammerspace_s3_users", "yaml", "S3 users", optional=True,
          show_if={"hammerspace_create_s3_users": True},
          example='- name: "app-user"\n  access_key_id: "{{ vault_app_s3_key }}"\n'
                  '  secret_access_key: "{{ vault_app_s3_secret }}"\n  enabled: true\n'),
    ])

CLUSTER = S(
    "cluster", "Cluster Settings",
    "Optional one-time cluster configuration through the API: DNS, Active Directory, site "
    "name, physical location and Prometheus.",
    [
        F("hammerspace_update_dns", "bool", "Update DNS", default=False),
        F("hammerspace_dns_config", "yaml", "DNS", optional=True,
          show_if={"hammerspace_update_dns": True},
          example='primary_server: "10.0.0.53"\nsecondary_server: "10.0.0.54"\n'
                  'search_domains:\n  - "example.com"\n'),
        F("hammerspace_join_ad", "bool", "Join Active Directory", default=False),
        F("hammerspace_ad_config", "yaml", "Active Directory", optional=True,
          show_if={"hammerspace_join_ad": True},
          example='domain: "corp.example.com"\nusername: "{{ vault_ad_username }}"\n'
                  'password: "{{ vault_ad_password }}"\nou: "OU=Servers,DC=corp,DC=example,DC=com"\n'),
        F("hammerspace_update_site_name", "bool", "Update site name", default=False),
        F("hammerspace_site_config", "yaml", "Site", optional=True,
          show_if={"hammerspace_update_site_name": True}, example='name: "datacenter-east"\n'),
        F("hammerspace_set_location", "bool", "Set physical location", default=False),
        F("hammerspace_location_config", "yaml", "Location", optional=True,
          show_if={"hammerspace_set_location": True},
          example='datacenter: "DC-EAST"\nroom: "Server Room A"\nrow: "R1"\nrack: "Rack-01"\n'
                  'position: "U1"\n'),
        F("hammerspace_enable_prometheus", "bool", "Enable Prometheus", default=False),
        F("hammerspace_prometheus_config", "yaml", "Prometheus", optional=True,
          show_if={"hammerspace_enable_prometheus": True}, example="port: 9100\n"),
    ])

THROTTLE = S(
    "throttle", "API Throttling & Timeouts",
    "Protect Anvil during large deployments. Docs: manual guide → Anvil Overload Protection.",
    [
        F("hammerspace_serial", "int", "Serial batch size",
          "Run hammerspace_integration N nodes at a time; all volumes of a batch finish "
          "before the next. 0 = all in parallel. Recommended 2 for 10+ nodes.", default=0, min=0),
        F("hammerspace_task_queue_throttle", "bool", "Task-queue throttling", default=True),
        F("hammerspace_min_queued_tasks", "int", "Resume below N queued tasks", default=5),
        F("hammerspace_max_queued_tasks", "int", "Pause above N queued tasks", default=10),
        F("hammerspace_task_queue_retries", "int", "Queue wait retries", default=100),
        F("hammerspace_task_queue_delay", "int", "Queue wait delay (s)", default=10),
        F("hammerspace_monitor_executing_tasks", "bool", "Count executing tasks too",
          optional=True, default=False),
        F("hammerspace_wait_for_executing", "bool", "Wait for executing tasks", optional=True,
          default=False),
        F("hammerspace_api_curl_timeout", "int", "Per-request timeout (s)", optional=True, default=60),
        F("hammerspace_api_volume_add_timeout", "int", "Volume-add POST timeout (s)",
          "Volume adds can take 15–20+ min on busy clusters (avoids curl rc 28).",
          optional=True, default=1800),
        F("hammerspace_node_add_retries", "int", "Node-add poll retries", optional=True, default=30),
        F("hammerspace_node_add_delay", "int", "Node-add poll delay (s)", optional=True, default=10),
        F("hammerspace_volume_add_retries", "int", "Volume-add poll retries", optional=True,
          default=180),
        F("hammerspace_volume_add_delay", "int", "Volume-add poll delay (s)", optional=True,
          default=10),
    ])

NFS = S(
    "nfs", "NFS Server & Exports",
    "The /etc/nfs.conf and /etc/exports the playbook writes. Hammerspace nodes and movers "
    "get no_root_squash, clients get root_squash; the 'mp' option prevents exporting an "
    "empty directory if a filesystem is not mounted. Docs: manual guide → NFS Settings, Export Options.",
    [
        F("nfs_threads", "int", "nfsd threads", "Hammerspace recommends 128.", default=128),
        F("nfs_vers3", "enum", "NFSv3", options=["y", "n"], default="y"),
        F("nfs_vers4_0", "enum", "NFSv4.0", options=["y", "n"], default="n"),
        F("nfs_vers4_1", "enum", "NFSv4.1", options=["y", "n"], default="n"),
        F("nfs_vers4_2", "enum", "NFSv4.2", options=["y", "n"], default="y"),
        F("nfs_rdma_enabled", "enum", "NFS over RDMA", options=["y", "n"], default="y"),
        F("nfs_rdma_port", "int", "RDMA port", default=20049),
        F("hammerspace_nodes", "list", "Hammerspace node IPs (no_root_squash)",
          "Defaults to the primary Anvil via {{ hammerspace_api_host }}. Add secondary Anvil "
          "and DSX IPs.", default=["{{ hammerspace_api_host }}"]),
        F("mover_nodes", "list", "Mover / DI IPs (no_root_squash)",
          "DSX nodes and standalone DI servers. With deploy_di + di_auto_export, di_nodes IPs "
          "are added automatically.", default=[]),
        F("client_subnets", "list", "Client subnets (root_squash)", default=["0.0.0.0/0"]),
        F("hammerspace_export_opts", "str", "Hammerspace export options",
          default="rw,no_root_squash,sync,secure,mp,no_subtree_check"),
        F("client_export_opts", "str", "Client export options",
          default="rw,root_squash,sync,secure,mp,no_subtree_check"),
        F("nfs_exports", "yaml", "Manual exports", "Only when dynamic discovery is off.",
          optional=True, show_if={"use_dynamic_discovery": False},
          example='- path: /hammerspace/hsvol0\n  clients:\n    - host: "10.1.2.3"\n'
                  '      options: "rw,sync,no_subtree_check,no_root_squash,secure,mp"\n'),
    ])

PERF = S(
    "perf", "Performance Tuning",
    "Hammerspace performance BKMs (perf_tuning role). Each block is independent. These are "
    "also exactly the settings an OS upgrade most often resets — check them with OS Upgrade "
    "Check. Run only this part with --tags perf.",
    [
        F("perf_tuning_enabled", "bool", "Enable perf_tuning role", default=True),
        F("perf_sunrpc_tunables_enabled", "bool", "sunrpc slot tables", default=True),
        F("perf_sunrpc_tcp_slot_table_entries", "int", "tcp_slot_table_entries",
          "Kernel default 2; Hammerspace recommends 128.", default=128),
        F("perf_sunrpc_tcp_max_slot_table_entries", "int", "tcp_max_slot_table_entries",
          default=65536),
        F("perf_nfsd_tunables_enabled", "bool", "nfsd tunables", default=True),
        F("perf_nfsd_max_block_size", "int", "nfsd max_block_size (bytes)", "1 MB is the kernel max.",
          default=1048576),
        F("perf_nfsd_direct_io", "bool", "nfsd direct I/O", "Kernel-version dependent; warns if "
          "unsupported.", default=True),
        F("perf_readahead_enabled", "bool", "Block-device read-ahead", default=True),
        F("perf_readahead_kb", "int", "Read-ahead (KB)", "1024–16384 for sequential workloads.",
          default=4096),
        F("perf_irq_pinning_enabled", "bool", "NIC IRQ pinning (NUMA-local)",
          "Required for line-rate 100/200 Gbps.", default=True),
        F("perf_irq_pin_interfaces", "list", "Interfaces to pin",
          "Empty = auto-detect NICs ≥ 25 Gbps.", default=[]),
        F("perf_irq_pinning_stop_irqbalance", "bool", "Stop irqbalance", "irqbalance fights "
          "manual pinning.", optional=True, default=True),
        F("perf_tcp_sysctl_enabled", "bool", "High-throughput TCP sysctl", default=True),
        F("perf_tcp_sysctl", "yaml", "TCP sysctl overrides", "Replaces the role default map.",
          optional=True,
          example='net.core.rmem_max: 268435456\nnet.core.wmem_max: 268435456\n'
                  'net.ipv4.tcp_rmem: "4096 87380 268435456"\nnet.ipv4.tcp_wmem: "4096 65536 268435456"\n'
                  'net.core.netdev_max_backlog: 250000\nnet.ipv4.tcp_mtu_probing: 1\n'),
    ])

FIREWALL = S(
    "firewall", "Firewall",
    "NFS ports (incl. RDMA 20049). firewalld, UFW or iptables is auto-detected.",
    [
        F("configure_firewall", "bool", "Configure the firewall", "Off if it's managed externally.",
          default=True),
        F("flush_iptables", "bool", "Flush iptables at start",
          "Clears REJECT rules that block NFS (INPUT/FORWARD flushed, SSH re-allowed).",
          default=True),
        F("enable_ufw", "bool", "Enable UFW (Debian/Ubuntu)", "Make sure SSH is allowed first.",
          optional=True, default=False),
        F("nfs_ports", "yaml", "NFS ports",
          default=None, example="- port: 2049\n  proto: tcp\n  service: nfs\n"),
        F("firewalld_services", "list", "firewalld services (RHEL family)",
          default=["nfs", "rpc-bind", "mountd"]),
    ])

PROTECTION = S(
    "protection", "Mount Protection",
    "systemd guard services keep each mount busy (umount fails) and a watchdog remounts "
    "anything that disappears. Docs: manual guide → Mount Point Protection.",
    [
        F("hammerspace_mount_protection", "bool", "Enable mount protection", default=True),
        F("hammerspace_mount_guard_enabled", "bool", "Guard services", default=True,
          show_if={"hammerspace_mount_protection": True}),
        F("hammerspace_remount_watchdog_enabled", "bool", "Auto-remount watchdog", default=True,
          show_if={"hammerspace_mount_protection": True}),
        F("hammerspace_remount_watchdog_interval", "str", "Watchdog interval",
          "systemd time span, e.g. 1min.", default="1min",
          show_if={"hammerspace_mount_protection": True}),
        F("hammerspace_automount_timeout", "int", "Device timeout (s)",
          "x-systemd.device-timeout.", default=10, show_if={"hammerspace_mount_protection": True}),
    ])

PRECHECK = S(
    "precheck", "Pre-checks",
    "Validation that runs before anything is changed (--tags precheck). Docs: manual guide → "
    "Pre-Setup Validation.",
    [
        F("expected_nvme_count", "int", "Expected NVMe count (incl. boot)", "Unset to skip.",
          optional=True, placeholder="9"),
        F("expected_scsi_count", "int", "Expected SCSI/SATA count", optional=True),
        F("enforce_drive_count", "bool", "Fail on count mismatch", default=False),
        F("fail_on_drives_in_use", "bool", "Fail if drives are in use",
          "Set false when re-running after RAID exists.", default=False),
        F("warn_on_numa_imbalance", "bool", "Warn on NUMA imbalance", default=True),
        F("expected_sector_size", "int", "Expected sector size", default=4096),
        F("require_4k_sectors", "bool", "Require 4K sectors", default=False),
        F("expected_mtu", "int", "Expected MTU", "Hosts 1500–9000; switches should be 9216.",
          default=9000),
        F("mtu_ping_size", "int", "MTU ping payload", "MTU − 28.", default=8972),
        F("enforce_mtu_test", "bool", "Fail on MTU test failure", default=False),
        F("network_test_targets", "list", "Jumbo-frame test targets",
          "Defaults to the primary Anvil; add the other Tier 0/LSS/DSX IPs.",
          default=["{{ hammerspace_api_host }}"]),
        F("iperf_test_enabled", "bool", "iperf bandwidth test",
          "Start servers first: iperf3 -s -D (5201) / iperf -s -D (5001).", default=False),
        F("iperf_version", "enum", "iperf version", options=["iperf3", "iperf"], default="iperf3",
          show_if={"iperf_test_enabled": True}),
        F("iperf_test_targets", "list", "iperf servers", default=[],
          show_if={"iperf_test_enabled": True}),
        F("iperf_test_duration", "int", "Duration (s)", default=10, show_if={"iperf_test_enabled": True}),
        F("iperf_test_parallel", "int", "Parallel streams", "64 for 200 Gbps.", default=64,
          show_if={"iperf_test_enabled": True}),
        F("iperf_min_bandwidth_mbps", "int", "Minimum bandwidth (Mbit/s)", default=40000,
          show_if={"iperf_test_enabled": True}),
        F("iperf_enforce_bandwidth", "bool", "Fail below minimum", default=False,
          show_if={"iperf_test_enabled": True}),
        F("fail_on_missing_packages", "bool", "Fail on missing packages",
          "mdadm, xfsprogs, nvme-cli, ...", default=True),
    ])

SAFETY = S(
    "safety", "Safety Switches",
    "Destructive overrides. Leave everything here at its default unless you are rebuilding a "
    "node on purpose.",
    [
        F("force_raid_recreate", "bool", "Force RAID recreation", default=False,
          danger="DESTRUCTIVE: destroys existing arrays (overrides the same-name and md127 "
                 "membership skips)."),
        F("force_fs_recreate", "bool", "Force filesystem recreation", default=False,
          danger="DESTRUCTIVE: runs mkfs over existing data."),
        F("format_nvme_to_4k", "bool", "Format NVMe to 4K sectors", default=False,
          danger="DESTRUCTIVE: erases every non-boot NVMe. Requires the confirmation string below."),
        F("nvme_format_confirm", "str", "4K format confirmation",
          'Must be exactly "YES_I_UNDERSTAND_THIS_IS_DESTRUCTIVE".', optional=True,
          show_if={"format_nvme_to_4k": True}),
        F("allow_empty_protected_disks", "bool", "Allow empty protected-disk list",
          "Only for diskless/netboot hosts. On a normal host an empty list is a detection bug — "
          "read the failure's topology dump instead.", default=False,
          danger="DANGEROUS: disables the last guard against mkfs on the OS disk."),
        F("skip_confirmation", "bool", "Skip confirmation prompts", default=False,
          danger="Removes interactive confirmations."),
    ])

DI_DEPLOY = S(
    "di-deploy", "DI: Deployment",
    "Data Instantiator (NFS mover) on the di_nodes group — host RPM, container "
    "(podman/docker) or Kubernetes DaemonSet. Docs: manual guide → Data Instantiator.",
    [
        F("deploy_di", "bool", "Deploy DI", "Master switch (or -e deploy_di=true).", default=False),
        F("di_activate", "bool", "Activate", "false = pre-stage only (install, no services, no "
          "registration); activate later with --tags di-activate.", default=True),
        F("di_auto_export", "bool", "Auto-export DI IPs on Tier 0",
          "Adds di_nodes IPs to /etc/exports with no_root_squash.", default=True),
        F("di_deployment_type", "enum", "Deployment mode", options=["host", "container", "kubernetes"],
          default="container"),
        F("di_container_runtime", "enum", "Container runtime", options=["podman", "docker"],
          default="podman", show_if={"di_deployment_type": "container"}),
        F("di_container_base_image", "str", "Base image", "Must be el9.", default="rockylinux:9",
          show_if={"di_deployment_type": ["container", "kubernetes"]}),
        F("di_container_image", "str", "Image name", default="hammerspace-di:local",
          show_if={"di_deployment_type": "container"}),
        F("di_container_name", "str", "Container name", default="pd-di",
          show_if={"di_deployment_type": "container"}),
        F("di_container_build_dir", "str", "Build dir on target", default="/opt/hammerspace/di-container",
          show_if={"di_deployment_type": "container"}),
        F("di_container_privileged", "bool", "Privileged", "Required for NFS + LTTng.", default=True,
          show_if={"di_deployment_type": "container"}),
        F("di_container_host_network", "bool", "Host network", "Required for 9095/9096.",
          default=True, show_if={"di_deployment_type": "container"}),
        F("di_container_restart_policy", "str", "Restart policy", default="unless-stopped",
          show_if={"di_deployment_type": "container"}),
        F("di_container_memory", "str", "Memory limit", "e.g. 4g. Empty = unlimited.", default="4g",
          show_if={"di_deployment_type": "container"}),
        F("di_container_cpus", "str", "CPU limit", "Empty = unlimited.", default="",
          show_if={"di_deployment_type": "container"}),
        F("di_image_source", "enum", "Image source",
          "build: on each target. local: tar from the controller (build_di_image.yml). url: "
          "download tar. registry: pull.", options=["build", "local", "url", "registry"],
          default="build", show_if={"di_deployment_type": "container"}),
        F("di_image_local_path", "str", "Image tar path (controller)", optional=True,
          default="{{ repo_root }}/payload/hammerspace-di-{{ ansible_architecture }}.tar",
          show_if={"di_image_source": "local"}),
        F("di_image_url", "str", "Image tar URL", optional=True, show_if={"di_image_source": "url"}),
        F("di_image_registry_ref", "str", "Registry reference", optional=True,
          show_if={"di_image_source": "registry"}),
        F("hammerspace_cluster_mgmt_ip", "str", "Anvil IP for DI", default="{{ hammerspace_api_host }}"),
        F("hammerspace_cluster_hostname", "str", "Cluster hostname (/etc/hosts)", default="data-cluster"),
    ])

DI_PACKAGES = S(
    "di-packages", "DI: Packages",
    "Where pd-di and its dependencies come from. x86_64 and aarch64 RPMs can share payload/ — "
    "the right arch is picked per node. Remember git lfs pull for payload/.",
    [
        F("di_rpm_source", "enum", "Package source", options=["directory", "url", "local"],
          default="directory"),
        F("di_payload_local_dir", "str", "Payload directory", default="{{ repo_root }}/payload",
          show_if={"di_rpm_source": "directory"}),
        F("di_tarball_url", "str", "Components tarball URL", show_if={"di_rpm_source": "url"}),
        F("di_tarball_local_path", "str", "Components tarball path (controller)", optional=True,
          show_if={"di_rpm_source": "local"}),
        F("babeltrace_rpm_url", "str", "babeltrace RPM URL (RedHat)"),
        F("di_add_node_script_url", "str", "add_node.py fallback URL"),
        F("di_tmp_dir", "str", "Extraction temp dir", default="/tmp/di_install"),
    ])

DI_NODE = S(
    "di-node", "DI: Node, AZ & Host Settings",
    "Registration, AZ spread, firewall and SELinux for DI nodes. Per-host di_node_ip / "
    "di_node_name / di_node_az belong in the inventory.",
    [
        F("di_node_name", "str", "Node name template",
          "Default appends -mover to avoid colliding with a co-located Tier 0 node.",
          default="{{ inventory_hostname }}-mover"),
        F("di_node_ip", "str", "Default node IP", "Falls back to ansible_default_ipv4.address.",
          optional=True),
        F("di_node_netmask_prefix", "int", "Netmask prefix (add_node.py -l)", default=24),
        F("di_registration_method", "enum", "Registration method", options=["script"], default="script"),
        F("di_ignore_ip_conflicts", "bool", "Allow IP shared with Tier 0", "Co-located deployments.",
          default=True),
        F("di_min_az_count", "int", "Minimum AZs", "1 = no requirement.", default=2, min=1),
        F("di_enforce_az_distribution", "bool", "Fail if below minimum AZs", default=False),
        F("di_firewall_ports", "yaml", "DI firewall ports",
          example="- port: 9095\n  proto: tcp\n- port: 9096\n  proto: tcp\n"),
        F("di_disable_selinux", "bool", "Disable SELinux (RedHat)", default=True),
        F("di_selinux_reboot", "bool", "Reboot after SELinux change", default=True),
    ])

DI_K8S = S(
    "di-k8s", "DI: Kubernetes Mode",
    "Only used when di_deployment_type is kubernetes: build/push the image, label nodes, "
    "apply namespace + secrets + DaemonSet, register, then set hammerspace.io/di-status. "
    "Registry credentials belong in Credentials.",
    [
        F("di_k8s_kubeconfig", "str", "Kubeconfig", optional=True, default="~/.kube/config"),
        F("di_k8s_namespace", "str", "Namespace", default="hammerspace-di"),
        F("di_k8s_psa_level", "str", "PodSecurityAdmission level", optional=True, default="privileged"),
        F("di_k8s_node_selector_label", "str", "Node selector label", default="hammerspace.io/di"),
        F("di_k8s_node_selector_value", "str", "Node selector value", default="true"),
        F("di_k8s_target_nodes", "list", "Target k8s nodes", "Empty = di_nodes hostnames.",
          optional=True),
        F("di_k8s_tolerations", "yaml", "Tolerations", optional=True,
          example='- {key: "nvidia.com/gpu", operator: "Exists", effect: "NoSchedule"}\n'),
        F("di_k8s_priority_class", "str", "Priority class", "system-node-critical recommended.",
          optional=True),
        F("di_k8s_status_label", "str", "Status label", default="hammerspace.io/di-status"),
        F("di_k8s_registry", "str", "Registry host", default="{{ oci_region | default('us-ashburn-1') }}.ocir.io"),
        F("di_k8s_registry_namespace", "str", "Registry namespace (REQUIRED)", optional=True,
          placeholder="axyz123"),
        F("di_k8s_image_repository", "str", "Image repository", default="hammerspace-di"),
        F("di_k8s_image_tag", "str", "Image tag", default="{{ pd_di_version | default('5.1.41-452') }}"),
        F("di_k8s_image_arch_suffix", "bool", "Append -amd64/-arm64", default=True),
        F("di_k8s_image_pull_secret", "str", "Pull secret name", default="hammerspace-di-pull"),
        F("di_k8s_registry_username", "str", "Registry username", "Prefer setting this in Credentials.",
          optional=True),
        F("di_k8s_registry_password", "str", "Registry password (reference)",
          "e.g. {{ vault_oci_registry_token }}.", optional=True),
        F("di_k8s_registry_email", "str", "Registry email", default="noreply@hammerspace.com"),
        F("di_k8s_api_credentials_secret", "str", "API credentials Secret",
          default="hammerspace-api-credentials"),
        F("di_k8s_build_push", "bool", "Build + push image", default=True),
        F("di_k8s_build_tool", "enum", "Build tool", options=["auto", "podman", "docker", "buildah", "nerdctl"],
          default="auto"),
        F("di_k8s_build_multiarch_manifest", "bool", "Multi-arch manifest (buildah)", default=False),
        F("di_k8s_rollout_timeout", "int", "Rollout timeout (s)", default=300),
        F("di_k8s_rollout_poll_delay", "int", "Rollout poll delay (s)", default=5),
        F("di_k8s_status_retries", "int", "Status retries", default=30),
        F("di_k8s_status_delay", "int", "Status delay (s)", default=5),
        F("di_k8s_auto_install_collection", "bool", "Auto-install kubernetes.core", default=True),
    ], show_if={"di_deployment_type": "kubernetes"})

DI_DECOM = S(
    "di-decom", "DI: Decommission",
    "Used by decommission_di.yml.",
    [
        F("di_decommission_evacuate_data", "bool", "Evacuate data first", default=True),
        F("di_decommission_stop_services", "bool", "Stop services", default=True),
        F("di_decommission_cleanup", "bool", "Remove local install", "RPMs, containers, /opt/pd.",
          default=True),
        F("di_decommission_poll_retries", "int", "Poll retries", default=180),
        F("di_decommission_poll_delay", "int", "Poll delay (s)", default=10),
        F("di_k8s_decommission_delete_namespace", "bool", "Delete k8s namespace", optional=True,
          default=False),
    ])

# ---------------------------------------------------------------------------
# vars/vault.yml
# ---------------------------------------------------------------------------
VAULT_SECTION = S(
    "credentials", "Credentials (vars/vault.yml)",
    "Secrets referenced from vars/main.yml as {{ vault_... }}. Encrypt this file with "
    "ansible-vault before committing or copying it anywhere.",
    [
        F("vault_hammerspace_api_password", "secret", "Hammerspace API password"),
        F("vault_become_password", "secret", "Sudo password for the hosts",
          "Only needed when the SSH user must type a password for sudo (no NOPASSWD). Used by "
          "the GUI's runs when 'Use saved sudo password' is ticked; from a terminal pass "
          "-e @vars/vault.yml -e 'ansible_become_password={{ vault_become_password }}', or use -K.",
          optional=True),
        F("vault_ad_username", "str", "AD join username", optional=True),
        F("vault_ad_password", "secret", "AD join password", optional=True),
        F("vault_s3_access_key", "secret", "S3 access key ID", optional=True),
        F("vault_s3_secret_key", "secret", "S3 secret key", optional=True),
        F("vault_app_s3_key", "secret", "S3 user access key", optional=True),
        F("vault_app_s3_secret", "secret", "S3 user secret key", optional=True),
        F("vault_oci_registry_token", "secret", "OCI registry auth token (DI k8s)", optional=True),
        F("di_k8s_registry_username", "str", "Registry username (DI k8s)", optional=True),
        F("di_k8s_registry_password", "secret", "Registry password (DI k8s)", optional=True),
    ], file=VAULT)

# ---------------------------------------------------------------------------
# ansible.cfg  (fields carry an INI section)
# ---------------------------------------------------------------------------
ANSIBLE_CFG = S(
    "ansible-cfg", "Ansible Settings (ansible.cfg)",
    "Controller-side defaults. The inventory chosen here is what every playbook uses unless "
    "-i is given (the Run menu always passes -i explicitly). Don't hard-code a private key "
    "path unless every controller has it — prefer ~/.ssh/config or ssh-agent.",
    [
        F("defaults.inventory", "str", "Default inventory",
          "Comma-separate several to combine clouds.", default="inventory.oci.yml", ini=True),
        F("defaults.private_key_file", "str", "SSH private key", optional=True, ini=True,
          placeholder="~/.ssh/id_ed25519"),
        F("defaults.remote_user", "str", "Remote user", optional=True, ini=True),
        F("defaults.forks", "int", "Forks", "Parallel hosts (Ansible default 5).", optional=True,
          ini=True),
        F("defaults.timeout", "int", "SSH connect timeout (s)", optional=True, ini=True),
        F("defaults.host_key_checking", "enum", "Host key checking",
          "False suits ephemeral cloud nodes that reuse IPs.", options=["False", "True"],
          default="False", ini=True),
        F("defaults.log_path", "str", "Log file", "scripts/run.sh already logs to ~/logs.",
          optional=True, ini=True),
        F("privilege_escalation.become", "enum", "Become (sudo)", options=["True", "False"],
          default="True", ini=True),
        F("privilege_escalation.become_method", "str", "Become method", default="sudo", ini=True),
        F("ssh_connection.pipelining", "enum", "SSH pipelining", options=["True", "False"],
          default="True", ini=True),
    ], file="ansible.cfg")

# ---------------------------------------------------------------------------
# Cloud dynamic inventories (top-level keys only; edited surgically)
# ---------------------------------------------------------------------------
CLOUD_OCI = S(
    "inv-oci", "OCI Dynamic Inventory", "inventory.oci.yml — oracle.oci.oci plugin. Requires "
    "pip3 install oci and ~/.oci/config. Verify with ansible-inventory --graph before deploying: "
    "whatever lands in storage_servers gets RAIDed.",
    [
        F("regions", "list", "Regions", default=["us-sanjose-1"]),
        F("fetch_hosts_from_subcompartments", "bool", "Include sub-compartments", default=True),
        F("include_host_filters", "list", "Host filters (Jinja2)",
          "e.g. lifecycle_state == 'RUNNING' and shape == 'BM.GPU.GB200-v3.4'."),
        F("groups", "yaml", "Groups", "storage_servers expression selects the Tier 0 nodes.",
          example="storage_servers: \"shape == 'BM.GPU.GB200-v3.4'\"\n"),
        F("hostname_format_preferences", "list", "Hostname preference"),
    ], file="inventory.oci.yml")

CLOUD_AWS = S(
    "inv-aws", "AWS Dynamic Inventory", "inventory.aws.yml — amazon.aws.aws_ec2. Requires "
    "boto3/botocore and AWS credentials.",
    [
        F("regions", "list", "Regions", default=["us-west-2"]),
        F("filters", "yaml", "EC2 filters", example="instance-state-name: running\ntag:Role: storage\n"),
        F("groups", "yaml", "Groups", example="storage_servers: \"'tier0' in (tags.Role | default(''))\"\n"),
        F("hostname_format_preferences", "list", "Hostname preference"),
    ], file="inventory.aws.yml")

CLOUD_GCP = S(
    "inv-gcp", "GCP Dynamic Inventory", "inventory.gcp.yml — google.cloud.gcp_compute. "
    "Requires google-auth and a service account or gcloud ADC.",
    [
        F("projects", "list", "Projects (REQUIRED)"),
        F("zones", "list", "Zones"),
        F("auth_kind", "enum", "Auth kind", options=["application", "serviceaccount", "machineaccount"],
          default="application"),
        F("filters", "list", "Filters", "e.g. status = RUNNING"),
        F("groups", "yaml", "Groups", example="storage_servers: \"'tier0' in (labels.role | default(''))\"\n"),
    ], file="inventory.gcp.yml")

CLOUD_AZURE = S(
    "inv-azure", "Azure Dynamic Inventory", "inventory.azure.yml — azure.azcollection.azure_rm. "
    "The file name must end in azure.yml / azure_rm.yml. See DEPLOYMENT_GUIDE_AZURE.md §4.",
    [
        F("include_vm_resource_groups", "list", "Resource groups",
          "Remove to scan the whole subscription (slow)."),
        F("plain_host_names", "bool", "Plain host names", default=True),
        F("auth_source", "enum", "Auth source", options=["auto", "cli", "credential_file", "env", "msi"],
          default="auto"),
        F("conditional_groups", "yaml", "Conditional groups",
          "This filter is destructive if too broad — everything in storage_servers is RAIDed.",
          example="storage_servers: >-\n  virtual_machine_size is defined and\n"
                  "  virtual_machine_size in ['Standard_L16s_v3', 'Standard_L32s_v3']\n"),
        F("exclude_host_filters", "list", "Exclude filters"),
        F("strict", "bool", "Strict (debug group expressions)", default=False),
        F("cache", "bool", "Cache", default=True),
        F("cache_timeout", "int", "Cache timeout (s)", default=300),
    ], file="inventory.azure.yml")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
MENUS = [
    {"title": "Getting Started", "items": [
        {"id": "overview", "title": "Overview", "kind": "overview"},
    ]},
    {"title": "Inventory", "items": [
        {"id": "inventory", "title": "Static Hosts", "kind": "inventory"},
        {"id": "inv-oci", "title": "OCI (dynamic)", "kind": "form"},
        {"id": "inv-aws", "title": "AWS (dynamic)", "kind": "form"},
        {"id": "inv-gcp", "title": "GCP (dynamic)", "kind": "form"},
        {"id": "inv-azure", "title": "Azure (dynamic)", "kind": "form"},
    ]},
    {"title": "Tier 0 Configuration", "items": [
        {"id": s["id"], "title": s["title"], "kind": "form"} for s in
        (STORAGE_LAYOUT, RAID_TUNING, EXCLUSIONS, NFS, PERF, PROTECTION, FIREWALL, PRECHECK, SAFETY)
    ]},
    {"title": "Hammerspace", "items": [
        {"id": s["id"], "title": s["title"], "kind": "form"} for s in
        (HS_API, VOLUMES_AZ, GROUPS_SHARES, THROTTLE, CLUSTER, S3)
    ]},
    {"title": "Data Instantiator", "items": [
        {"id": s["id"], "title": s["title"].replace("DI: ", ""), "kind": "form"} for s in
        (DI_DEPLOY, DI_PACKAGES, DI_NODE, DI_K8S, DI_DECOM)
    ]},
    {"title": "Controller", "items": [
        {"id": "credentials", "title": "Credentials", "kind": "vault"},
        {"id": "ansible-cfg", "title": "Ansible Settings", "kind": "form"},
        {"id": "files", "title": "Raw File Editor", "kind": "raw"},
    ]},
    {"title": "Operate", "items": [
        {"id": "run", "title": "Run Playbooks", "kind": "run"},
        {"id": "utilities", "title": "Hammerspace Utilities", "kind": "utilities"},
        {"id": "upgrade", "title": "OS Upgrade Check", "kind": "upgrade"},
        {"id": "jobs", "title": "Job History", "kind": "jobs"},
        {"id": "logs", "title": "Logs & Reports", "kind": "logs"},
    ]},
    {"title": "Reference", "items": [
        {"id": "docs", "title": "Documentation", "kind": "docs"},
    ]},
]

SECTIONS = [STORAGE_LAYOUT, RAID_TUNING, EXCLUSIONS, HS_API, VOLUMES_AZ, GROUPS_SHARES, S3,
            CLUSTER, THROTTLE, NFS, PERF, FIREWALL, PROTECTION, PRECHECK, SAFETY,
            DI_DEPLOY, DI_PACKAGES, DI_NODE, DI_K8S, DI_DECOM,
            VAULT_SECTION, ANSIBLE_CFG, CLOUD_OCI, CLOUD_AWS, CLOUD_GCP, CLOUD_AZURE]

SECTIONS_BY_ID = {s["id"]: s for s in SECTIONS}

# Keys in vars/main.yml that are internal plumbing, not user settings.
INTERNAL_VARS_KEYS = {"repo_root", "_cloud_az_detected"}

# Files the raw editor may open (relative to the repo root).
EDITABLE_FILES = [
    "vars/main.yml", "vars/vault.yml", "inventory.yml", "inventory.az.yml",
    "inventory.oci.yml", "inventory.aws.yml", "inventory.gcp.yml", "inventory.azure.yml",
    "ansible.cfg", "requirements.yml", "tier0_instances_limit",
]


def neighbors_for(section_id):
    """Ordered keys of a section (used to place newly inserted keys)."""
    return [f["key"] for f in SECTIONS_BY_ID[section_id]["fields"]]


def vars_main_fields():
    keys = set()
    for s in SECTIONS:
        if s["file"] == VARS:
            keys.update(f["key"] for f in s["fields"])
    return keys
