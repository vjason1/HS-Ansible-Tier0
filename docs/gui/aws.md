# AWS EC2 — GUI Guide

For Tier 0 on EC2 instances with local NVMe instance storage (e.g. `p4d`, `p5`,
`i4i`), discovered automatically through the `amazon.aws.aws_ec2` dynamic
inventory. The [manual (CLI) version](../manual/inventory-aws.md) covers the same
setup by editing `inventory.aws.yml` by hand; for the rest of a cloud deployment
from the command line, AWS follows the [OCI deployment guide](../../DEPLOYMENT_GUIDE.md)
from section 5.

| Step | Where |
|------|-------|
| 1. Prepare the controller + AWS access | terminal (once) |
| 2. Start the GUI | terminal |
| 3. Select your instances | **Inventory → AWS (dynamic)** |
| 4. SSH access | your EC2 key pair on the controller |
| 5. Configure and deploy | [shared GUI steps](configure-and-deploy.md) |

## 1. Prepare the controller and AWS access

```bash
pip3 install ansible boto3 botocore
git clone https://github.com/vjason1/HS-Ansible-Tier0.git
cd HS-Ansible-Tier0
git lfs pull                                   # DI RPMs, only if you deploy DI
ansible-galaxy collection install -r requirements.yml   # includes amazon.aws

# Credentials — pick one
aws configure                                  # access key in ~/.aws/credentials
# or: export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=us-west-2
# or: run the controller on EC2 with an IAM instance role (nothing to configure)
```

The credentials need read access to EC2 (`ec2:DescribeInstances`, `ec2:DescribeRegions`).
Start the GUI from the same shell so it inherits any `AWS_*` variables.

## 2. Start the GUI

```bash
python3 gui/tier0_gui.py
```

Open the printed URL (headless controller: `ssh -L 8765:127.0.0.1:8765 <user>@<controller>` first).

## 3. Select your instances — Inventory → AWS (dynamic)

This menu edits `inventory.aws.yml`.

| Field | What to set |
|-------|-------------|
| **Regions** | e.g. `us-west-2` |
| **EC2 filters** | Server-side filters, e.g. `instance-state-name: running` and `tag:Role: storage` (or `instance-type: [p4d.24xlarge]`) |
| **Groups** | The `storage_servers` expression — **everything it matches gets its local drives RAIDed**, e.g. `storage_servers: "'tier0' in (tags.Role \| default(''))"` |
| **Hostname preference** | Which instance attribute names the host |

The connection settings under `compose:` — `ansible_host: private_ip_address`,
`ansible_user: "'ubuntu'"` (use `ec2-user` on Amazon Linux / RHEL AMIs) and the AZ
prefix expression — aren't in the form: click **Edit the whole file** to change them.

**Save to file**, then **Show resolved inventory** and check that exactly the intended
instances land in `storage_servers`. Each host also carries `aws_instance_id`,
`aws_instance_type`, `aws_availability_zone`, `aws_vpc_id` and `aws_private_ip`.

## 4. SSH access

Make your EC2 key pair available on the controller (`~/.ssh/config` `IdentityFile`,
`ssh-add`, or **Controller → Ansible Settings → SSH private key**). For instances
reachable only with a password, use **Static Hosts → Copy SSH key to hosts…**
([how](static.md#4-give-ansible-ssh-access--copy-ssh-key-to-hosts)).

## 5. Availability zones

The inventory's `compose:` derives the Hammerspace AZ prefix from the EC2
availability zone:

| AWS availability zone | Hammerspace AZ prefix |
|-----------------------|-----------------------|
| us-west-2a | `AZ1:` |
| us-west-2b | `AZ2:` |
| us-west-2c | `AZ3:` |

That `hammerspace_volume_az_prefix` host variable is used as-is. Turn on
**Volumes & Availability Zones → AZ mapping for nodes** and *Apply AZ labels via
API* if you also want AZ labels on the nodes.

## 6. Configure and deploy

Continue with the [shared GUI steps](configure-and-deploy.md). AWS specifics:

- **On the Run page, untick `inventory.yml` and tick `inventory.aws.yml`.**
- Security groups must allow SSH from the controller, NFS between the nodes and the
  Hammerspace cluster (111, 2049, 20048 TCP/UDP; 20049 TCP for RDMA), and access to
  the Anvil management IP on port 8443 for the API integration.
- Adding instances later: **Preflight: find new instances**, then deploy with
  *Limit* `@.new_instances_limit`.

## Related

- [Manual (CLI) AWS inventory](../manual/inventory-aws.md) · [GUI reference](../../gui/README.md)
- [OS Upgrade Configuration Check](../os-upgrade-check.md) · [Rack operations runbook](../../RACK_OPERATIONS_RUNBOOK.md)
