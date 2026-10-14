#!/usr/bin/env bash
# =============================================================================
# tier0_config_snapshot.sh — READ-ONLY configuration snapshot of a Tier 0 node
# =============================================================================
#
# Prints a sectioned, normalized text dump of everything this repository
# configures on a Tier 0 / LSS node, plus the OS-level state an operating
# system upgrade can silently change (kernel, packages, module parameters,
# NIC naming, initramfs contents, sysctl, systemd enablement, ...).
#
# Run it before and after an OS upgrade and compare the two files with
# scripts/tier0_snapshot_diff.py. The os_upgrade_snapshot.yml playbook and the
# GUI ("OS Upgrade Check" menu) drive exactly that workflow fleet-wide.
#
# Standalone usage (no Ansible needed):
#   sudo bash tier0_config_snapshot.sh > pre-upgrade.txt
#   ...upgrade the OS, reboot...
#   sudo bash tier0_config_snapshot.sh > post-upgrade.txt
#   python3 tier0_snapshot_diff.py pre-upgrade.txt post-upgrade.txt
#
# Guarantees:
#   * Never writes, restarts, loads or unloads anything. Every command is a
#     query. (initramfs listing reads the image; it does not rebuild it.)
#   * Missing tools are reported as "(not available: <tool>)", never fatal —
#     the same script runs on RHEL/Rocky/Alma and Debian/Ubuntu.
#   * Volatile values (counters, timestamps, PIDs, IRQ numbers, autofs fds,
#     resync progress, free space) are stripped or normalized so the diff
#     shows configuration changes, not noise.
#
# Output format (consumed by tier0_snapshot_diff.py — keep it stable):
#   #! key: value                 header metadata, ignored by the diff
#   ===== SECTION: <name> =====   start of a section
#   ...section body...
#
# Section names are "<category>.<item>". Categories drive severity in the
# diff report — see SECTION_RULES in tier0_snapshot_diff.py.
# =============================================================================

set -u
export LC_ALL=C
export LANG=C
umask 077

SNAPSHOT_FORMAT_VERSION=1
MOUNT_ROOT="${TIER0_MOUNT_ROOT:-/hammerspace}"
CMD_TIMEOUT="${TIER0_SNAPSHOT_TIMEOUT:-30}"

have() { command -v "$1" >/dev/null 2>&1; }

# Run a command with a timeout when `timeout` exists. Never fails the script.
q() {
    if have timeout; then
        timeout "${CMD_TIMEOUT}" "$@" 2>/dev/null
    else
        "$@" 2>/dev/null
    fi
    return 0
}

# "active" / "inactive" / "failed" / "n/a" — never an empty string.
unit_active() {
    local st=""
    have systemctl && st="$(q systemctl is-active "$1")"
    printf '%s' "${st:-n/a}"
}

# Number of paths matching a glob (0 when nothing matches).
count_glob() {
    local n=0 f
    for f in $1; do [[ -e "$f" ]] && n=$((n + 1)); done
    echo "$n"
}

section() { printf '\n===== SECTION: %s =====\n' "$1"; }
na()      { printf '(not available: %s)\n' "$1"; }

# Print a file with a header line; mark missing files explicitly so a file
# that DISAPPEARS after an upgrade shows up as a change.
show_file() {
    local f="$1"
    if [[ -r "$f" ]]; then
        printf -- '--- %s\n' "$f"
        # Strip trailing whitespace so editor noise doesn't show as change.
        sed -e 's/[[:space:]]*$//' "$f"
    elif [[ -e "$f" ]]; then
        printf -- '--- %s (exists, unreadable)\n' "$f"
    else
        printf -- '--- %s (absent)\n' "$f"
    fi
}

# Same as show_file but expands a glob; prints "(none)" when it matches nothing.
show_glob() {
    local pattern="$1" matched=0 f
    for f in $pattern; do
        [[ -e "$f" ]] || continue
        matched=1
        show_file "$f"
    done
    (( matched )) || printf -- '--- %s (none)\n' "$pattern"
}

# Hash a file instead of printing it (scripts / binaries).
show_hash() {
    local f="$1"
    if [[ -r "$f" ]] && have sha256sum; then
        printf '%s  %s\n' "$(sha256sum "$f" | cut -d' ' -f1)" "$f"
    elif [[ -e "$f" ]]; then
        printf 'present  %s\n' "$f"
    else
        printf 'absent  %s\n' "$f"
    fi
}

# "name=value" for every readable file in a sysfs/procfs directory.
dump_params() {
    local dir="$1" p v
    [[ -d "$dir" ]] || { printf '%s (absent)\n' "$dir"; return 0; }
    for p in "$dir"/*; do
        [[ -f "$p" && -r "$p" ]] || continue
        v="$(head -c 512 "$p" 2>/dev/null | tr '\n' ' ' | sed -e 's/[[:space:]]*$//')"
        printf '%s/%s=%s\n' "${dir##*/module/}" "${p##*/}" "$v"
    done
}

# Header -----------------------------------------------------------------------
printf '#! tier0-config-snapshot format=%s\n' "${SNAPSHOT_FORMAT_VERSION}"
printf '#! host: %s\n' "$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null)"
printf '#! captured_utc: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '#! kernel: %s\n' "$(uname -r)"
printf '#! uptime_seconds: %s\n' "$(cut -d. -f1 /proc/uptime 2>/dev/null)"
printf '#! boot_id: %s\n' "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
printf '#! running_as_root: %s\n' "$([[ ${EUID:-$(id -u)} -eq 0 ]] && echo yes || echo no)"
printf '#! mount_root: %s\n' "${MOUNT_ROOT}"

# =============================================================================
# SUMMARY — one key=value per line; rendered as a before/after table
# =============================================================================
section "summary.key_facts"
{
    # shellcheck source=/dev/null
    os_pretty="$( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
    echo "os=${os_pretty}"
    echo "kernel=$(uname -r)"
    echo "arch=$(uname -m)"
    echo "selinux=$(have getenforce && getenforce 2>/dev/null || echo n/a)"
    echo "nvme_controllers=$(count_glob '/sys/class/nvme/nvme*')"
    echo "md_arrays_active=$(grep -c '^md' /proc/mdstat 2>/dev/null || echo 0)"
    echo "tier0_mounts=$(awk -v r="${MOUNT_ROOT}/" 'index($2, r) == 1 && $3 != "autofs"' /proc/mounts 2>/dev/null | wc -l)"
    echo "nfs_exports=$(q exportfs -v | grep -c '^/' )"
    echo "nfsd_threads=$(cat /proc/fs/nfsd/threads 2>/dev/null || echo n/a)"
    echo "nfsd_versions=$(cat /proc/fs/nfsd/versions 2>/dev/null || echo n/a)"
    echo "nfsd_max_block_size=$(cat /proc/fs/nfsd/max_block_size 2>/dev/null || echo n/a)"
    echo "sunrpc_tcp_slot_table_entries=$(cat /proc/sys/sunrpc/tcp_slot_table_entries 2>/dev/null || echo n/a)"
    echo "sunrpc_tcp_max_slot_table_entries=$(cat /proc/sys/sunrpc/tcp_max_slot_table_entries 2>/dev/null || echo n/a)"
    echo "nfsd_direct_io=$(cat /sys/module/nfsd/parameters/nfsd_direct_io 2>/dev/null || echo n/a)"
    echo "irqbalance=$(unit_active irqbalance)"
    nfs_state="$(unit_active nfs-server)"
    [[ "$nfs_state" == "active" ]] || nfs_state="$(unit_active nfs-kernel-server)"
    echo "nfs_server=${nfs_state}"
    echo "failed_units=$(q systemctl list-units --state=failed --no-legend --plain | wc -l)"
} | sed -e 's/[[:space:]]*$//'

# =============================================================================
# OS / KERNEL
# =============================================================================
section "os.release"
show_file /etc/os-release
[[ -r /etc/redhat-release ]] && show_file /etc/redhat-release
[[ -r /etc/debian_version ]] && show_file /etc/debian_version

section "kernel.version"
echo "release=$(uname -r)"
echo "machine=$(uname -m)"
echo "version=$(uname -v)"
if have grubby; then
    echo "grub_default_kernel=$(q grubby --default-kernel)"
fi

section "kernel.cmdline"
# Split into one parameter per line, sorted, so a single added/removed
# argument is a single-line diff. BOOT_IMAGE/initrd are kept on purpose:
# they confirm which kernel actually booted.
tr ' ' '\n' < /proc/cmdline | sed '/^$/d' | sort

section "kernel.installed"
# Kernels present on disk (boot entries). Order normalized.
for f in /boot/vmlinuz* /boot/initramfs* /boot/initrd*; do
    [[ -e "$f" ]] && printf '%s\n' "${f##*/}"
done | sort

section "kernel.modules_loaded"
# Names only — sizes and use counts are volatile.
if [[ -r /proc/modules ]]; then cut -d' ' -f1 /proc/modules | sort; else na /proc/modules; fi

section "kernel.module_params"
# Parameters of the modules Tier 0 tunes or depends on.
for m in nfsd sunrpc nfs nfsv4 rpcrdma svcrdma xprtrdma nvme nvme_core md_mod raid0 raid1 raid456 raid10 xfs mlx5_core ib_core; do
    [[ -d "/sys/module/$m/parameters" ]] && dump_params "/sys/module/$m/parameters"
done
for f in /etc/modprobe.d/*.conf; do
    [[ -e "$f" ]] || continue
    grep -Hv '^[[:space:]]*\(#\|$\)' "$f" 2>/dev/null
done | sort

section "kernel.sysctl"
# Full sysctl, minus keys that change on every boot or every second.
if have sysctl; then
    q sysctl -a | grep -Ev \
        -e '^kernel\.random\.' \
        -e '^kernel\.(ns_last_pid|pty\.nr|perf_event_max_sample_rate|sched_domain\.)' \
        -e '^fs\.(dentry-state|inode-nr|inode-state|file-nr|aio-nr|quota\.)' \
        -e '^net\.netfilter\.nf_conntrack_count' \
        -e '^kernel\.(tainted|hostname)\b' \
        -e '^dev\.cdrom\.info' \
        -e '^vm\.(stat_refresh)' \
        -e '^net\.ipv6\.conf\.(veth|cni|podman|docker|br-|virbr)' \
        -e '^net\.ipv4\.conf\.(veth|cni|podman|docker|br-|virbr)' \
        -e '^net\.ipv[46]\.neigh\.(veth|cni|podman|docker|br-|virbr)' \
        | sort
else
    na sysctl
fi

section "kernel.sysctl_files"
for f in /etc/sysctl.conf /etc/sysctl.d/*.conf; do
    [[ -e "$f" ]] || continue
    grep -Hv '^[[:space:]]*\(#\|;\|$\)' "$f" 2>/dev/null
done | sort

section "kernel.tainted"
echo "tainted=$(cat /proc/sys/kernel/tainted 2>/dev/null)"

# =============================================================================
# PACKAGES
# =============================================================================
section "packages.manager"
if have rpm; then echo "manager=rpm"; elif have dpkg-query; then echo "manager=dpkg"; else echo "manager=unknown"; fi

section "packages.installed"
# One "name.arch version" per line. The diff tool turns this into an
# upgraded / added / removed table rather than a raw text diff.
if have rpm; then
    q rpm -qa --qf '%{NAME}.%{ARCH} %{EPOCHNUM}:%{VERSION}-%{RELEASE}\n' | grep -v '^gpg-pubkey' | sort
elif have dpkg-query; then
    # shellcheck disable=SC2016  # dpkg-query format string, not a shell expansion
    q dpkg-query -W -f='${db:Status-Abbrev} ${Package}.${Architecture} ${Version}\n' \
        | awk '$1 ~ /^ii/ {print $2, $3}' | sort
else
    na "rpm/dpkg"
fi

section "packages.repositories"
if [[ -d /etc/yum.repos.d ]]; then
    for f in /etc/yum.repos.d/*.repo; do
        [[ -e "$f" ]] || continue
        awk -v f="${f##*/}" '/^\[/{s=$0} /^enabled[[:space:]]*=/{gsub(/[[:space:]]/,""); print f, s, $0}' "$f"
    done | sort
fi
if [[ -d /etc/apt ]]; then
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do
        [[ -f "$f" ]] || continue
        grep -Hv '^[[:space:]]*\(#\|$\)' "$f" 2>/dev/null
    done | sort
fi

section "packages.config_leftovers"
# Package managers leave these when an upgrade could not merge a config file
# you modified (e.g. /etc/nfs.conf.rpmnew). Each one means "the package
# shipped a new default you have NOT picked up" — or, for .rpmsave /
# .dpkg-old, "your file was moved aside and replaced".
find /etc -xdev \( -name '*.rpmnew' -o -name '*.rpmsave' -o -name '*.dpkg-new' \
    -o -name '*.dpkg-old' -o -name '*.dpkg-dist' -o -name '*.ucf-dist' \) 2>/dev/null | sort
echo "(end of list)"

# =============================================================================
# HARDWARE / STORAGE DEVICES
# =============================================================================
section "hardware.cpu"
if have lscpu; then
    lscpu 2>/dev/null | grep -E '^(Architecture|CPU\(s\)|On-line CPU|Thread|Core|Socket|NUMA node|Vendor ID|Model name|Flags)' \
        | sed -e 's/[[:space:]]\{2,\}/ /g'
else
    na lscpu
fi
echo "governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a)"
echo "transparent_hugepage=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo n/a)"
have tuned-adm && echo "tuned=$(q tuned-adm active | sed 's/^Current active profile: //')"

section "storage.nvme_controllers"
# Keyed by SERIAL so a controller that gets renumbered after a kernel upgrade
# (nvme0 <-> nvme3) shows up as a name change on the same serial.
if ls -d /sys/class/nvme/nvme* >/dev/null 2>&1; then
    for c in /sys/class/nvme/nvme*; do
        ser="$(tr -d ' ' < "$c/serial" 2>/dev/null)"
        mdl="$(sed 's/[[:space:]]*$//' "$c/model" 2>/dev/null)"
        fw="$(tr -d ' ' < "$c/firmware_rev" 2>/dev/null)"
        numa="$(cat "$c/device/numa_node" 2>/dev/null || cat "$c/numa_node" 2>/dev/null || echo ?)"
        addr="$(cat "$c/address" 2>/dev/null || basename "$(readlink -f "$c/device" 2>/dev/null)")"
        printf 'serial=%s ctrl=%s model=%s fw=%s numa=%s pci=%s\n' "${ser:-?}" "${c##*/}" "${mdl:-?}" "${fw:-?}" "$numa" "${addr:-?}"
    done | sort
else
    echo "(no NVMe controllers)"
fi

section "storage.block_devices"
# Disks and partitions with stable identity columns only (no usage).
if have lsblk; then
    q lsblk -P -e 7,11 -o NAME,TYPE,SIZE,ROTA,LOG-SEC,PHY-SEC,MODEL,SERIAL,FSTYPE,UUID,LABEL,MOUNTPOINT \
        | sed -e 's/[[:space:]]*"/"/g' | sort
else
    na lsblk
fi

section "storage.queue_settings"
# The tunables perf_tuning / cpu-optimized RAID set: scheduler, read-ahead,
# queue depth. Kernel upgrades frequently reset these when udev rules or
# set-readahead.service stop applying.
for b in /sys/block/*; do
    n="${b##*/}"
    case "$n" in nvme*n*|md*|sd*|dm-*) ;; *) continue ;; esac
    sch="$(cat "$b/queue/scheduler" 2>/dev/null)"
    ra="$(cat "$b/queue/read_ahead_kb" 2>/dev/null)"
    nr="$(cat "$b/queue/nr_requests" 2>/dev/null)"
    ms="$(cat "$b/queue/max_sectors_kb" 2>/dev/null)"
    printf '%s scheduler=[%s] read_ahead_kb=%s nr_requests=%s max_sectors_kb=%s\n' "$n" "${sch:-?}" "${ra:-?}" "${nr:-?}" "${ms:-?}"
done | sort

# =============================================================================
# RAID (mdadm)
# =============================================================================
section "raid.mdstat"
if [[ -r /proc/mdstat ]]; then
    # Drop resync/recovery progress bars and bitmap page counts.
    grep -Ev '(resync|recovery|reshape|check)[[:space:]]*=|^[[:space:]]*bitmap:|\[[=>.]+\]' /proc/mdstat \
        | sed -e 's/[[:space:]]*$//'
else
    na /proc/mdstat
fi

section "raid.detail"
if have mdadm; then
    for md in /dev/md[0-9]* /dev/md/*; do
        [[ -b "$md" ]] || continue
        printf -- '--- %s\n' "$md"
        q mdadm --detail "$md" | grep -Ev '^[[:space:]]*(Update Time|Events|Resync Status|Rebuild Status|Reshape Status|Check Status) :' \
            | sed -e 's/[[:space:]]*$//'
    done
    echo "--- mdadm --detail --scan"
    q mdadm --detail --scan | sort
else
    na mdadm
fi

section "raid.config_files"
show_file /etc/mdadm.conf
show_file /etc/mdadm/mdadm.conf

section "raid.initramfs"
# Does the initramfs for the RUNNING kernel still carry mdadm + its config?
# A new kernel whose initramfs was built without them is the classic cause of
# "arrays assemble as md127 / don't assemble at boot" after an upgrade.
krel="$(uname -r)"
img=""
for cand in "/boot/initramfs-${krel}.img" "/boot/initrd.img-${krel}" "/boot/initrd-${krel}"; do
    [[ -r "$cand" ]] && { img="$cand"; break; }
done
if [[ -z "$img" ]]; then
    echo "initramfs for ${krel}: (not found)"
elif have lsinitrd; then
    echo "initramfs=${img}"
    q lsinitrd "$img" | grep -oE '(etc/mdadm[^ ]*|sbin/mdadm|usr/sbin/mdadm|raid0\.ko[^ ]*|raid1\.ko[^ ]*|raid456\.ko[^ ]*|md-mod\.ko[^ ]*|md_mod\.ko[^ ]*)' | sort -u
elif have lsinitramfs; then
    echo "initramfs=${img}"
    q lsinitramfs "$img" | grep -oE '(etc/mdadm[^ ]*|sbin/mdadm|raid0\.ko[^ ]*|raid1\.ko[^ ]*|raid456\.ko[^ ]*|md-mod\.ko[^ ]*|md_mod\.ko[^ ]*)' | sort -u
else
    echo "initramfs=${img} (no lsinitrd/lsinitramfs to inspect it)"
fi

# =============================================================================
# FILESYSTEMS / MOUNTS
# =============================================================================
section "fs.fstab"
show_file /etc/fstab

section "fs.mounts"
# Real filesystems only; autofs options normalized (fd / pipe_ino / pgrp
# change on every boot for x-systemd.automount mounts).
awk '$3 !~ /^(proc|sysfs|cgroup2?|devtmpfs|securityfs|pstore|bpf|debugfs|tracefs|configfs|fusectl|mqueue|hugetlbfs|overlay|squashfs|nsfs|devpts|binfmt_misc|efivarfs|ramfs|fuse\.gvfsd-fuse|fuse\.portal|selinuxfs)$/' /proc/mounts 2>/dev/null \
    | awk '$2 !~ /^\/(run\/user|run\/credentials|var\/lib\/(containers|docker|kubelet)|snap)\//' \
    | awk '!($3 == "tmpfs" && $2 ~ /^\/run/)' \
    | sed -E 's/(fd|pipe_ino|pgrp|timeout)=[0-9]+/\1=N/g' \
    | sort

section "fs.tier0_mounts"
# For each Tier 0 data mount: device, xfs geometry, root perms, and whether
# the Hammerspace data layout exists (a missing PrimaryData after an upgrade
# means the volume will go "suspected").
found=0
while read -r dev mnt fstype _rest; do
    case "$mnt" in "${MOUNT_ROOT}"/*) ;; *) continue ;; esac
    [[ "$fstype" == "autofs" ]] && continue
    found=1
    printf -- '--- %s\n' "$mnt"
    echo "device=$dev fstype=$fstype"
    echo "mode_owner=$(stat -c '%a %U:%G' "$mnt" 2>/dev/null)"
    if [[ -d "$mnt/PrimaryData" ]]; then echo "PrimaryData=present"; else echo "PrimaryData=absent"; fi
    if have df; then
        echo "size_bytes=$(df -B1 --output=size "$mnt" 2>/dev/null | tail -1 | tr -d ' ')"
    fi
    if [[ "$fstype" == "xfs" ]] && have xfs_info; then
        q xfs_info "$mnt" | sed -e 's/[[:space:]]\{2,\}/ /g'
    fi
done < /proc/mounts
(( found )) || echo "(no filesystems mounted under ${MOUNT_ROOT})"

section "fs.mount_dirs"
# Expected mountpoints that exist on disk (mounted or not).
if [[ -d "${MOUNT_ROOT}" ]]; then
    find "${MOUNT_ROOT}" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null | sort
else
    echo "${MOUNT_ROOT} (absent)"
fi

# =============================================================================
# NFS SERVER
# =============================================================================
section "nfs.config_files"
show_file /etc/nfs.conf
show_glob "/etc/nfs.conf.d/*.conf"
show_file /etc/default/nfs-kernel-server
show_file /etc/sysconfig/nfs
show_file /etc/idmapd.conf

section "nfs.exports_file"
show_file /etc/exports
show_glob "/etc/exports.d/*.exports"

section "nfs.exports_active"
if have exportfs; then
    # exportfs -v wraps long entries over two lines; join them.
    q exportfs -v | awk 'NF==1 && /^\//{p=$0; next} p!=""{print p, $0; p=""; next} {print}' \
        | sed -E 's/[[:space:]]+/ /g' | sort
else
    na exportfs
fi

section "nfs.runtime"
for f in threads versions max_block_size portlist; do
    if [[ -r "/proc/fs/nfsd/$f" ]]; then
        printf '%s=%s\n' "$f" "$(tr '\n' ' ' < "/proc/fs/nfsd/$f" | sed 's/[[:space:]]*$//')"
    else
        printf '%s=(unavailable)\n' "$f"
    fi
done
for f in tcp_slot_table_entries tcp_max_slot_table_entries udp_slot_table_entries; do
    [[ -r "/proc/sys/sunrpc/$f" ]] && printf 'sunrpc.%s=%s\n' "$f" "$(cat "/proc/sys/sunrpc/$f")"
done

section "nfs.rpc_services"
if have rpcinfo; then
    q rpcinfo -p localhost | awk 'NR>1 {print $5, "v"$2, $3, $4}' | sort -u
else
    na rpcinfo
fi

# =============================================================================
# PERFORMANCE TUNING ARTIFACTS (perf_tuning / nfs_setup roles)
# =============================================================================
section "perf.files"
show_file /etc/modprobe.d/sunrpc.conf
show_file /etc/modprobe.d/sunrpc-slots.conf
show_file /etc/modprobe.d/nfsd-direct.conf
show_file /etc/sysctl.d/99-hammerspace-sunrpc.conf
show_file /etc/sysctl.d/99-hammerspace-tcp.conf
show_file /etc/tmpfiles.d/hammerspace-nfsd.conf
show_file /etc/udev/rules.d/99-hammerspace-readahead.rules
show_file /etc/udev/rules.d/99-nfs.rules

section "perf.scripts"
show_hash /usr/local/sbin/hammerspace-irq-pin.sh
show_hash /usr/local/bin/set-readahead.sh
show_hash /usr/local/bin/hammerspace-remount-check.sh
show_hash /usr/local/bin/add_node.py

section "perf.irq_affinity"
# NIC queue IRQ -> CPU list, keyed by interface + queue ORDINAL (IRQ numbers
# themselves are reassigned on boot, so they are not printed).
any=0
for nd in /sys/class/net/*; do
    n="${nd##*/}"
    [[ -d "$nd/device/msi_irqs" ]] || continue
    any=1
    i=0
    for irq in $(for f in "$nd"/device/msi_irqs/*; do [[ -e "$f" ]] && echo "${f##*/}"; done | sort -n); do
        aff="$(cat "/proc/irq/$irq/smp_affinity_list" 2>/dev/null || echo ?)"
        printf '%s q%03d cpus=%s\n' "$n" "$i" "$aff"
        i=$((i + 1))
    done
done
(( any )) || echo "(no MSI-capable NICs found)"

# =============================================================================
# NETWORK
# =============================================================================
section "net.links"
# Interface name, MTU, state, MAC. Leading ifindex stripped (renumbers).
if have ip; then
    q ip -o link show | sed -E 's/^[0-9]+: //; s/\\.*link\/[a-z]+ ([0-9a-f:]+).*/ mac=\1/' \
        | grep -Ev '^(veth|cni|flannel|cali|docker|podman|virbr|br-)' \
        | sed -E 's/ qlen [0-9]+//; s/ (qdisc|group|mode) [^ ]+//g' | sort
else
    na ip
fi

section "net.addresses"
if have ip; then
    q ip -br addr show | grep -Ev '^(veth|cni|flannel|cali|docker|podman|virbr|br-)' \
        | sed -E 's/[[:space:]]+/ /g' | sort
fi

section "net.routes"
have ip && q ip route show | sed -E 's/ (proto|metric) [^ ]+//g; s/[[:space:]]+/ /g' | grep -Ev '(docker|podman|cni|veth|virbr)' | sort

section "net.nic_drivers"
# Driver / firmware per physical NIC — an OS upgrade swapping the driver
# (inbox vs OFED) is one of the most impactful silent changes for RDMA.
if have ethtool; then
    for nd in /sys/class/net/*; do
        n="${nd##*/}"; [[ -e "$nd/device" ]] || continue
        printf -- '--- %s\n' "$n"
        q ethtool -i "$n" | grep -E '^(driver|version|firmware-version|bus-info):'
        echo "speed=$(cat "$nd/speed" 2>/dev/null || echo ?)"
        q ethtool -l "$n" | awk '/Current hardware settings/{c=1} c && /Combined/{print "channels_combined=" $2; exit}'
        q ethtool -g "$n" | awk '/Current hardware settings/{c=1} c && /^RX:/{print "ring_rx=" $2} c && /^TX:/{print "ring_tx=" $2}'
    done
else
    na ethtool
fi

section "net.offloads"
if have ethtool; then
    for nd in /sys/class/net/*; do
        n="${nd##*/}"; [[ -e "$nd/device" ]] || continue
        q ethtool -k "$n" | grep -v '\[fixed\]' | grep ':' | grep -v '^Features' | sed "s/^[[:space:]]*/$n /"
    done | sort
fi

section "net.rdma"
if have ofed_info; then echo "ofed=$(q ofed_info -s)"; else echo "ofed=(not installed)"; fi
if have rdma; then
    q rdma link show | sed -E 's/^link //; s/ (lid|sm_lid|lmc) [^ ]+//g' | sort
elif [[ -d /sys/class/infiniband ]]; then
    for f in /sys/class/infiniband/*; do echo "${f##*/}"; done | sort
else
    echo "(no RDMA devices)"
fi

section "net.hosts"
show_file /etc/hosts

# =============================================================================
# SERVICES
# =============================================================================
section "services.key_units"
# is-enabled / is-active for every unit Tier 0 and DI depend on.
units="nfs-server nfs-kernel-server nfs-mountd rpcbind rpc-statd nfs-idmapd mdmonitor mdmonitor-oneshot.timer
irqbalance firewalld ufw tuned chronyd chrony systemd-timesyncd
hammerspace-guards.target hammerspace-remount.timer hammerspace-remount.service hammerspace-irq-pin.service
set-readahead.service pd-di lttng-sessiond pd-di-lttng-recorder podman docker containerd kubelet"
for u in $units; do
    en="$(q systemctl is-enabled "$u")"; ac="$(q systemctl is-active "$u")"
    [[ -z "$en" && "$ac" == "inactive" ]] && en="not-found"
    printf '%s enabled=%s active=%s\n' "$u" "${en:-not-found}" "${ac:-unknown}"
done
for u in $(q systemctl list-unit-files 'hammerspace-*' --no-legend | awk '{print $1}'); do
    printf '%s enabled=%s active=%s\n' "$u" "$(q systemctl is-enabled "$u")" "$(q systemctl is-active "$u")"
done | sort -u

section "services.enabled_unit_files"
if have systemctl; then
    q systemctl list-unit-files --state=enabled --no-legend | awk '{print $1}' | sort
else
    na systemctl
fi

section "services.failed"
if have systemctl; then
    out="$(q systemctl list-units --state=failed --no-legend --plain | awk '{print $1}' | sort)"
    echo "${out:-(none)}"
fi

section "services.tier0_unit_files"
show_glob "/etc/systemd/system/hammerspace-*"
show_file /etc/systemd/system/set-readahead.service

# =============================================================================
# SECURITY / FIREWALL
# =============================================================================
section "security.selinux_apparmor"
have getenforce && echo "selinux_mode=$(getenforce 2>/dev/null)"
[[ -r /etc/selinux/config ]] && grep -E '^SELINUX(TYPE)?=' /etc/selinux/config
have aa-enabled && echo "apparmor=$(q aa-enabled)"
true

section "firewall.firewalld"
if have firewall-cmd && [[ "$(q systemctl is-active firewalld)" == "active" ]]; then
    echo "default_zone=$(q firewall-cmd --get-default-zone)"
    q firewall-cmd --list-all | sed -e 's/[[:space:]]*$//'
else
    echo "(firewalld not running)"
fi

section "firewall.ufw"
if have ufw; then q ufw status verbose; else echo "(ufw not installed)"; fi

section "firewall.iptables"
if have iptables-save; then
    # Strip packet/byte counters and generated-on comments.
    q iptables-save | grep -v '^#' | sed -E 's/\[[0-9]+:[0-9]+\]/[0:0]/g' \
        | grep -Ev '(DOCKER|CNI-|KUBE-|cali-|PODMAN|NETAVARK)'
else
    na iptables-save
fi

# =============================================================================
# DATA INSTANTIATOR (DI) — present only on di_nodes, harmless elsewhere
# =============================================================================
section "di.state"
if have rpm; then
    for p in pd-di hs-s3 lttng-tools lttng-ust jemalloc babeltrace libtirpc; do
        printf '%s=%s\n' "$p" "$(q rpm -q "$p" | head -1)"
    done
fi
[[ -d /opt/pd ]] && echo "/opt/pd=present" || echo "/opt/pd=absent"
for rt in podman docker; do
    if have "$rt"; then
        q "$rt" ps -a --format '{{.Names}} {{.Image}} {{.State}}' | sed "s/^/$rt /" | sort
    fi
done

section "time.sync"
if have timedatectl; then
    q timedatectl show 2>/dev/null | grep -E '^(Timezone|NTP|NTPSynchronized)=' | sort
fi

printf '\n===== END =====\n'
exit 0
