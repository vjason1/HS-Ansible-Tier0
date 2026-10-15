# AWS EC2 Dynamic Inventory

> Part of the **manual (CLI) guide** — [back to the manual guide](README.md#2-configure-inventory).
> Using the GUI instead? See [GUI: AWS](../gui/aws.md).

Auto-discover instances from Amazon Web Services EC2.

## 1. Install AWS Ansible collection and Python SDK
```bash
# Install AWS Ansible collection
ansible-galaxy collection install amazon.aws

# Install AWS Python SDK
pip3 install boto3 botocore
```

## 2. Configure AWS authentication
```bash
# Option 1: AWS CLI configuration (recommended)
aws configure

# Option 2: Environment variables
export AWS_ACCESS_KEY_ID='your-access-key'
export AWS_SECRET_ACCESS_KEY='your-secret-key'
export AWS_REGION='us-west-2'

# Option 3: IAM instance role (when running on EC2)
# No configuration needed - uses instance metadata
```

## 3. Edit `inventory.aws.yml`
Update the regions and filters to match your environment:

```yaml
plugin: amazon.aws.aws_ec2
regions:
  - us-west-2

# Filter to only running instances with specific tags
filters:
  instance-state-name: running
  tag:Role: storage      # Adjust to your tagging convention
  # instance-type:
  #   - p4d.24xlarge     # GPU instances

# Create storage_servers group
groups:
  storage_servers: "'tier0' in (tags.Role | default(''))"

compose:
  ansible_host: private_ip_address
  ansible_user: "'ubuntu'"
  # Hammerspace AZ prefix from availability zone
  hammerspace_volume_az_prefix: >-
    "AZ" ~ ((placement.availability_zone[-1:] | lower | ord) - 96) ~ ":"
```

## 4. Update `ansible.cfg` to use AWS inventory
```ini
[defaults]
inventory = inventory.aws.yml
```

## 5. Test the inventory
```bash
# List discovered hosts
ansible-inventory -i inventory.aws.yml --list

# Show as graph
ansible-inventory -i inventory.aws.yml --graph

# Ping all storage servers
ansible storage_servers -m ping
```

**AWS Instance Metadata Exposed:**

| Variable | Description | Example |
|----------|-------------|---------|
| `aws_instance_id` | EC2 instance ID | "i-0abc123def456" |
| `aws_instance_type` | Instance type | "p4d.24xlarge" |
| `aws_availability_zone` | Availability zone | "us-west-2a" |
| `aws_vpc_id` | VPC ID | "vpc-12345678" |
| `aws_private_ip` | Private IP address | "10.0.1.100" |

**AWS AZ Mapping:**

| AWS Availability Zone | Hammerspace AZ Prefix |
|-----------------------|----------------------|
| us-west-2a | `AZ1:` |
| us-west-2b | `AZ2:` |
| us-west-2c | `AZ3:` |

---

Next: configure variables and run — [manual guide §3](README.md#3-configure-variables).
