# OCI Instance Spawner

A clean, production-grade automation tool to provision Oracle Cloud Infrastructure (OCI) Always Free instances (AMD and ARM Ampere) by continuously retrying when capacity becomes available.

## What is this?
Two robust Python scripts designed to automate OCI instance creation:
- **`amd/amd.py`**: Spawns `VM.Standard.E2.1.Micro` (AMD micro) instances (Always Free allows up to 2 active instances).
- **`arm/arm.py`**: Spawns `VM.Standard.A1.Flex` (ARM Ampere) instances (Always Free allows up to 4 OCPUs and 24 GB RAM total).

Both scripts run in a loop, handle OCI rate limits with exponential backoff, auto-detect infrastructure components if left unconfigured, and optionally send status updates and success notifications via a Telegram Bot.

## Key Features & Safety Mechanisms
- **Safe Polling Cooldown**: Default 60-second cycle cooldown (configurable, minimum 30s) across all Availability Domains to prevent account throttling and API bans.
- **Lightweight Capacity Reporting**: Integrates OCI `create_compute_capacity_report` pre-checks (learned from `filipbob`), testing host capacity before triggering heavy `launch_instance` calls, with automatic fallback.
- **Safe State Verification & Double-Spawn Prevention**:
  - Immediately halts launch attempts upon success, cleanly decoupling provisioning from VNIC and Public IP resolution.
  - Automatically queries existing active instances if a network connection drops or times out during launch, ensuring no duplicate instances are created.
  - Generates idempotent `opc-retry-token` for launch requests.
- **Rate Limit (HTTP 429) Backoff**: Inspects `Retry-After` headers and applies exponential backoff (up to 300s) if OCI rate limiters are encountered.
- **Smart Infrastructure Auto-Discovery**:
  - Auto-detects all available Availability Domains in the compartment.
  - Auto-detects the latest official Canonical Ubuntu LTS image (ARM or x86_64) that is active (`AVAILABLE`).
  - Auto-detects subnets, automatically prioritizing public subnets so public IP assignment succeeds.
  - Auto-detects root compartment via Tenancy OCID from config.
- **Strict Pre-flight Checks**:
  - Storage validation prevents exceeding the 200 GB Always Free block/boot volume quota across all ADs (with 0 GB penalty when booting from an existing boot volume).
  - Quota verification ensures you do not exceed 4 OCPUs / 24 GB RAM (ARM) or 2 instances (AMD).
  - Terminated and terminating instances are excluded from duplicate name collisions.
  - Startup Clock Skew Detection: Checks local clock offset against Oracle Cloud servers, warning you before 401 authentication errors occur if system clock drifts.
- **Flexible Execution**: Run directly from the repository root or from the `amd/` / `arm/` subdirectories. Config files in either directory or the parent root are automatically discovered.

## How to use it?

### 1. Install Dependencies
Ensure you have Python 3.8+ installed, then install the dependencies:
```bash
pip install -r arm/requirements.txt
```

### 2. Configure OCI Authentication
1. Edit the `config` file inside `arm/` or `amd/` (or use standard `~/.oci/config`) with your user OCID, tenancy OCID, region, and API fingerprint.
2. Place your private API key in `oci_private_key.pem` in the same directory (or set `key_file` in config).

### 3. Configure Spawner settings
Open `arm/arm.py` or `amd/amd.py` and configure the settings at the top:
*   `ssh_authorized_keys`: Your public SSH key (e.g. `ssh-rsa AAAAB3Nza...`).
*   `displayName`: Name of the instance (default: `instance-a1-flex` / `instance-amd-micro`).
*   `retry_interval`: Seconds to wait between attempts (default: `60`).
*   `bot_token` & `uid`: *(Optional)* Telegram Bot token and your Telegram User ID for alerts. Set to `"xxxx"` or leave default to disable.
*   *Optional overrides:* `availabilityDomains`, `subnetId`, `imageId`, and `boot_volume_size_in_gbs` (auto-detected if left as default placeholders).

> [!TIP]
> **Zero-Friction Auto-Discovery:**
> If you leave `availabilityDomains`, `imageId`, and `subnetId` as their default `"REPLACE_WITH_..."` values, the script will automatically detect all active Availability Domains in your region, pick the latest Ubuntu LTS image for your shape, and use your default subnet.

### 4. Run the Spawner

Run from root or from within the directory:

```bash
# To spawn ARM Instance
nohup python3 arm/arm.py > arm.log 2>&1 &

# To spawn AMD Instance
nohup python3 amd/amd.py > amd.log 2>&1 &
```

Or from within the respective subdirectory:
```bash
cd arm && nohup python3 arm.py > output.log 2>&1 &
```

Monitor progress in real-time:
```bash
tail -f arm.log
```