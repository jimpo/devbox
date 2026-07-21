# Migrating from cfleet to devbox

cfleet (the standalone TypeScript CLI for AWS EC2 dev instances) has been folded
into devbox: one tool now manages development machines on **libvirt** (local VMs)
and **AWS** (EC2), with a shared config, provisioning script, and jj-sync model.
The cfleet repo is retired — use `devbox` for everything it did.

## Command parity

Every cfleet command maps onto a devbox one. On AWS, cloud instances always
carry an explicit `--name`; the project's `[aws]` section supplies what cfleet's
"instance template" used to.

| cfleet | devbox | Notes |
|--------|--------|-------|
| `cfleet launch <n> --template T` | `devbox create --provider aws --name <n>` | The "template" (instance type / arch / AMI / spot) is the project's `[aws]` section. |
| `cfleet launch … --spot` / `--on-demand` | `devbox create … --spot` / `--on-demand` | Same override of the configured spot default. |
| `cfleet list` | `devbox list` | Unions libvirt + AWS; AWS rows show instance type + a `($)` cost flag. |
| `cfleet ssh <n>` | `devbox --name <n> ssh` | |
| `cfleet exec <n> -- CMD` | `devbox --name <n> ssh -- CMD` | `devbox ssh` passes trailing args straight to `ssh`. |
| `cfleet stop <n>` | `devbox --name <n> stop` | |
| `cfleet start <n>` | `devbox --name <n> up` | |
| `cfleet terminate <n>` | `devbox --name <n> destroy` | |
| `cfleet sync <n>` | `devbox --name <n> sync` | Same jj-remote model (fetch bookmarked work). |
| `cfleet templates list/show` | — | Templates are per-project `[aws]` config now; there's no separate command. |

Known ergonomic gaps (not blockers; can be added later if missed):

- `cfleet list --json` — `devbox list` is a human table only.
- `cfleet sync --watch` — `devbox sync` is one-shot; loop it externally if you
  want a watcher.
- cfleet let you pick any of several global templates per launch; devbox ties
  `[aws]` launch defaults to the project. Use separate projects (or override
  `--spot`/`--on-demand`) for different shapes.

## Config migration

cfleet's `~/.cfleet/config.yaml` splits into devbox's **global** config
(`~/.config/devbox/config.toml`) and each project's **`[aws]`** section
(`.devbox/config.toml`). cfleet instance *templates* (`~/.cfleet/templates/*.yaml`)
become that per-project `[aws]` section.

### Global: `~/.cfleet/config.yaml` → `~/.config/devbox/config.toml`

| cfleet (YAML) | devbox (TOML) |
|---------------|---------------|
| `aws.profile` | `[aws] profile` |
| `aws.region` | `[aws] region` |
| `aws.default_security_groups` | `[aws] default_security_groups` |
| `aws.default_subnets` | `[aws] default_subnets` |
| `defaults.prefer_spot` | `[aws] spot` |
| `ssh.public_key` (path to `.pub`) | `[ssh] pubkey_path` |
| `ssh.user` | per-project `user` (defaults to `$USER`) |
| `ssh.key_path` (private key) | not needed — devbox uses your normal `ssh` client/agent |
| `defaults.default_template` | no global default — set `[aws]` per project |
| `sync.git_remote` | not needed — devbox's jj-sync fetches from the instance directly |

Example:

```yaml
# ~/.cfleet/config.yaml
aws:
  profile: your-aws-profile
  region: us-east-1
  default_security_groups: [sg-xxxx]
defaults:
  prefer_spot: true
ssh:
  public_key: "ssh-ed25519 AAAA... you@host"
```

becomes

```toml
# ~/.config/devbox/config.toml
[ssh]
pubkey_path = "~/.ssh/id_ed25519.pub"

[aws]
profile = "your-aws-profile"
region = "us-east-1"
default_security_groups = ["sg-xxxx"]
spot = true
```

### Template: `~/.cfleet/templates/rust-x86.yaml` → project `[aws]`

| cfleet template | devbox project `[aws]` |
|-----------------|------------------------|
| `instance.instance_type` | `instance_type` |
| `instance.architecture` | `architecture` |
| `storage.volume_size_gb` | project `disk_gb` (the EBS root size) |
| `ami.filters.name` | `ami_name_filter` |
| `ami.filters.owner` | `ami_owner` |
| `spot.enabled` | `spot` |
| `spot.interruption_behavior` | `interruption_behavior` |
| `setup` | provisioning is the shared `provision-common.sh` + the project's optional `.devbox/cloud-config.yaml` — no per-template setup file |

Example:

```yaml
# ~/.cfleet/templates/rust-x86.yaml
instance: { instance_type: c7i.8xlarge, architecture: x86_64 }
storage: { volume_size_gb: 64 }
ami:
  filters: { name: "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*", owner: "099720109477" }
spot: { enabled: true, interruption_behavior: stop }
```

becomes

```toml
# <project>/.devbox/config.toml
provider = "aws"
disk_gb = 64

[aws]
instance_type = "c7i.8xlarge"
architecture = "x86_64"
spot = true
# ami_name_filter / ami_owner default to Canonical's Ubuntu 24.04 image for the
# architecture; set them (or `ami = "ami-..."`) to override.
```

## Retiring the cfleet install

- Remove the global binary if you `npm link`ed it: `npm rm -g cfleet` (or
  `npm unlink -g cfleet`) in the cfleet checkout.
- Archive/deprecate the GitHub repo (its README now points here).
