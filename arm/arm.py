"""
OCI Always Free Instance Spawner - ARM (VM.Standard.A1.Flex)
Spawns Ampere A1 Flex instances (up to 4 OCPUs / 24 GB RAM free tier limit).
Continuously retries when capacity is available with safe backoff.
"""

import datetime
import email.utils
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path

import oci
import telebot

# ==============================================================================
# User Configuration
# ==============================================================================
# Leave as "REPLACE_WITH_..." or empty to enable auto-discovery where supported.

# Availability Domain(s) - e.g., ["xx:AP-MUMBAI-1-AD-1"].
# If left as placeholder or empty, all ADs in the compartment will be auto-detected.
availabilityDomains = ["REPLACE_WITH_AVAILABILITY_DOMAIN"]

# Name of the instance to create
displayName = "REPLACE_WITH_INSTANCE_NAME"

# Compartment OCID. If left as placeholder, the tenancy OCID from config is used.
compartmentId = "REPLACE_WITH_COMPARTMENT_OCID"

# Subnet OCID. If left as placeholder, the first available public subnet in the compartment is auto-detected.
subnetId = "REPLACE_WITH_SUBNET_OCID"

# Public SSH key string (e.g., "ssh-rsa AAAAB3NzaC1yc2E...").
ssh_authorized_keys = "REPLACE_WITH_SSH_PUBLIC_KEY"

# Image OCID. If left as placeholder, the latest Canonical Ubuntu image for ARM is auto-detected.
imageId = "REPLACE_WITH_IMAGE_OCID"

# Boot volume size in GB (min 47 GB for Always Free, total 200 GB across all volumes).
# Leave as placeholder or set to an integer (e.g. 50, 100) or None for default.
boot_volume_size_in_gbs = "REPLACE_WITH_BOOT_VOLUME_SIZE"

# Existing boot volume OCID to boot from (optional, overrides imageId if set).
boot_volume_id = "REPLACE_WITH_BOOT_VOLUME_ID"

# Telegram Bot configuration (optional, set to "xxxx" or leave placeholder to disable).
bot_token = "REPLACE_WITH_BOT_TOKEN"
uid = "REPLACE_WITH_UID"

# ARM Ampere resource allocation (Always Free tier allows up to 4 OCPUs and 24 GB RAM total).
ocpus = 2
memory_in_gbs = 12

# Retry interval in seconds (60-90s recommended to prevent OCI API rate limits and bans).
retry_interval = 60

# Check host capacity report before launching (set to False to disable capacity report checks).
check_capacity_first = True


# ==============================================================================
# Helper Functions
# ==============================================================================


def is_set(val) -> bool:
    """Check if a config variable contains a real value rather than a placeholder."""
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return True
    if isinstance(val, str):
        v = val.strip()
        if not v or v.lower() in ("xxxx", "none"):
            return False
        if v.startswith("REPLACE_WITH_"):
            return False
        return True
    if isinstance(val, (list, tuple)):
        return any(is_set(item) for item in val)
    return bool(val)


def parse_int_or_none(val):
    """Safely parse an integer from string or int, returning None on failure."""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            return int(s)
    return None


def check_system_clock_skew(max_skew_seconds=120):
    """
    Check if local system clock is synchronized with public time servers.
    OCI request signing uses timestamps that must be within 300 seconds of server time.
    Returns (is_synced: bool, diff_seconds: float or None)
    """
    for url in ("https://cloud.oracle.com", "https://www.oracle.com"):
        try:
            req = urllib.request.Request(
                url,
                method="HEAD",
                headers={"User-Agent": "oci-spawner/1.0"},
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                date_hdr = resp.headers.get("Date")
                if date_hdr:
                    server_ts = email.utils.parsedate_to_datetime(date_hdr).timestamp()
                    diff = server_ts - time.time()
                    return (abs(diff) <= max_skew_seconds, diff)
        except Exception:
            pass
    return (True, None)


def load_oci_config(config_file=None, profile_name="DEFAULT"):
    """
    Locate and load OCI configuration file.
    Searches:
    1. Explicit config_file parameter / OCI_CONFIG_FILE env var
    2. ./config in the script's directory
    3. ./config in the current working directory
    4. ./config in the parent directory (repo root)
    5. ~/.oci/config (OCI default)
    """
    resolved_path = None
    if config_file:
        p = Path(config_file)
        if not p.is_file():
            logging.critical(f"Specified OCI config file not found: {config_file}")
            sys.exit(1)
        resolved_path = p.resolve()
    elif os.environ.get("OCI_CONFIG_FILE"):
        p = Path(os.environ["OCI_CONFIG_FILE"])
        if not p.is_file():
            logging.critical(f"OCI_CONFIG_FILE not found: {os.environ['OCI_CONFIG_FILE']}")
            sys.exit(1)
        resolved_path = p.resolve()
    else:
        script_dir = Path(__file__).resolve().parent
        candidates = [
            script_dir / "config",
            Path.cwd() / "config",
            script_dir.parent / "config",
            Path(oci.config.DEFAULT_LOCATION),
        ]
        for cand in candidates:
            try:
                if cand.is_file():
                    resolved_path = cand.resolve()
                    break
            except Exception:
                pass

        if not resolved_path:
            logging.critical(
                "OCI config file not found! Checked locations:\n" + "\n".join(f" - {c}" for c in candidates)
            )
            sys.exit(1)

    logging.info(f"Loading OCI config from: {resolved_path}")
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(str(resolved_path)):
        logging.critical(f"Could not parse OCI config file at: {resolved_path}")
        sys.exit(1)

    if profile_name not in parser:
        logging.critical(f"Profile '{profile_name}' not found in OCI config file: {resolved_path}")
        sys.exit(1)

    cfg = dict(oci.config.DEFAULT_CONFIG)
    cfg.update(parser[profile_name])
    cfg["log_requests"] = oci.config._as_bool(cfg.get("log_requests", False))

    # Resolve relative key_file path relative to config directory, cwd, or repo root
    if "key_file" in cfg:
        raw_key = os.path.expanduser(cfg["key_file"])
        key_path = Path(raw_key)
        if not key_path.is_absolute() and not key_path.exists():
            cand_dirs = [
                resolved_path.parent,
                Path.cwd(),
                Path(__file__).resolve().parent.parent,
                Path(__file__).resolve().parent,
            ]
            found = False
            for d in cand_dirs:
                cand_k = d / key_path
                if cand_k.exists():
                    cfg["key_file"] = str(cand_k.resolve())
                    found = True
                    break
            if not found:
                cfg["key_file"] = str((resolved_path.parent / key_path).resolve())
        elif key_path.exists():
            cfg["key_file"] = str(key_path.resolve())

    return cfg


def send_telegram_msg(bot, chat_id, text, delete_msg_id=None, max_retries=3):
    """Send a Telegram message with finite retries to avoid hanging on failure."""
    if not bot or not is_set(chat_id):
        return None
    for attempt in range(1, max_retries + 1):
        try:
            if delete_msg_id is not None:
                try:
                    bot.delete_message(chat_id, delete_msg_id)
                except Exception:
                    pass
            sent = bot.send_message(chat_id, text)
            return getattr(sent, "message_id", getattr(sent, "id", None))
        except Exception as e:
            logging.warning(f"Telegram send_message attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(3)
    return None


def update_telegram_status(bot, chat_id, text, current_msg_id):
    """Update or send status message in Telegram, catching errors and recreating deleted messages."""
    if not bot or not is_set(chat_id):
        return current_msg_id
    try:
        if current_msg_id is not None:
            bot.edit_message_text(text, chat_id=chat_id, message_id=current_msg_id)
            return current_msg_id
    except Exception as e:
        err_msg = str(e).lower()
        if "not modified" in err_msg:
            return current_msg_id
        logging.warning(f"Telegram status update error: {e}. Sending replacement status message...")
        current_msg_id = None

    try:
        sent = bot.send_message(chat_id, text)
        return getattr(sent, "message_id", getattr(sent, "id", None))
    except Exception as e:
        logging.warning(f"Telegram send_message failed: {e}")
        return current_msg_id


def resolve_availability_domains(identity_client, compartment_id, configured_ads):
    """Return valid configured ADs or auto-detect all available ADs in the compartment."""
    valid = [ad for ad in configured_ads if is_set(ad)]
    if valid:
        return valid
    try:
        logging.info("Auto-detecting Availability Domains in compartment...")
        ads = identity_client.list_availability_domains(compartment_id=compartment_id).data
        detected = [ad.name for ad in ads if getattr(ad, "name", None)]
        if detected:
            logging.info(f"Auto-detected Availability Domain(s): {detected}")
            return detected
    except Exception as e:
        logging.warning(f"Failed to auto-detect Availability Domains: {e}")
    logging.critical("No Availability Domains found or configured! **SCRIPT STOPPED**")
    sys.exit(1)


def resolve_image_id(compute_client, compartment_id, shape, configured_image_id):
    """Return configured image OCID or auto-detect latest available Canonical Ubuntu image."""
    if is_set(configured_image_id):
        return configured_image_id
    try:
        logging.info(f"Auto-detecting latest Canonical Ubuntu image for shape {shape}...")
        images = compute_client.list_images(
            compartment_id=compartment_id,
            operating_system="Canonical Ubuntu",
            shape=shape,
            sort_by="TIMECREATED",
            sort_order="DESC",
        ).data
        if images:
            active = [img for img in images if getattr(img, "lifecycle_state", "AVAILABLE") == "AVAILABLE"]
            chosen = active[0] if active else images[0]
            logging.info(f"Auto-detected image: {chosen.display_name} ({chosen.id})")
            return chosen.id
    except Exception as e:
        logging.warning(f"Failed to auto-detect Ubuntu image for {shape}: {e}")
    logging.critical("No valid imageId configured or auto-detected! **SCRIPT STOPPED**")
    sys.exit(1)


def resolve_subnet_id(vnc_client, compartment_id, configured_subnet_id):
    """Return configured subnet OCID or auto-detect first available public subnet in compartment."""
    if is_set(configured_subnet_id):
        return configured_subnet_id
    try:
        logging.info("Auto-detecting subnets in compartment...")
        subnets = vnc_client.list_subnets(compartment_id=compartment_id).data
        active = [s for s in subnets if s.lifecycle_state not in ("TERMINATING", "TERMINATED")]
        if active:
            # Prefer public subnets (where prohibit_public_ip_on_vnic is False)
            public_subnets = [s for s in active if not getattr(s, "prohibit_public_ip_on_vnic", False)]
            chosen = public_subnets[0] if public_subnets else active[0]
            logging.info(
                f"Auto-detected subnet: {chosen.display_name} ({chosen.id}) "
                f"[Public IP allowed: {not getattr(chosen, 'prohibit_public_ip_on_vnic', False)}]"
            )
            return chosen.id
    except Exception as e:
        logging.warning(f"Failed to auto-detect subnet: {e}")
    logging.critical("No valid subnetId configured or auto-detected! **SCRIPT STOPPED**")
    sys.exit(1)


def check_instance_spawned(compute_client, compartment_id, display_name):
    """Check if an instance with displayName is already active in the compartment."""
    try:
        instances = compute_client.list_instances(compartment_id=compartment_id).data
        if instances:
            for inst in instances:
                if inst.display_name == display_name and inst.lifecycle_state not in ("TERMINATING", "TERMINATED"):
                    return inst
    except Exception as e:
        logging.warning(f"Could not check existing instances by display name: {e}")
    return None


def check_ad_capacity(compute_client, compartment_id, ad, shape, ocpus=None, memory_in_gbs=None):
    """
    Query OCI Compute Capacity Report for host capacity.
    Returns:
        True: Host capacity is AVAILABLE
        False: OUT_OF_HOST_CAPACITY
        None: Inconclusive, permission denied, or error -> fallback to launch_instance
    """
    try:
        if "Flex" in shape and ocpus is not None and memory_in_gbs is not None:
            shape_cfg = oci.core.models.CapacityReportInstanceShapeConfig(
                ocpus=float(ocpus),
                memory_in_gbs=float(memory_in_gbs),
            )
        else:
            shape_cfg = None

        shape_avail = oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
            instance_shape=shape,
            instance_shape_config=shape_cfg,
        )
        report_details = oci.core.models.CreateComputeCapacityReportDetails(
            compartment_id=compartment_id,
            availability_domain=ad,
            shape_availabilities=[shape_avail],
        )
        report = compute_client.create_compute_capacity_report(report_details).data
        if report and getattr(report, "shape_availabilities", None):
            status = getattr(report.shape_availabilities[0], "availability_status", "")
            if status == "AVAILABLE":
                return True
            elif status == "OUT_OF_HOST_CAPACITY":
                return False
    except Exception as e:
        logging.debug(f"Compute capacity report check failed for {ad} ({e}). Falling back to launch.")
    return None


def resolve_instance_public_ip(
    compute_client,
    vnc_client,
    compartment_id,
    subnet_id,
    instance_id,
    max_retries=18,
    delay=10,
):
    """
    Poll for VNIC attachment and Public IP assignment after launch.
    Does NOT fail or crash if IP is not immediately available.
    """
    logging.info("Polling for VNIC attachment and Public IP assignment...")
    for attempt in range(1, max_retries + 1):
        try:
            attachments = compute_client.list_vnic_attachments(
                compartment_id=compartment_id,
                instance_id=instance_id,
            ).data
            if attachments:
                for att in attachments:
                    vnic_id = getattr(att, "vnic_id", None)
                    if not vnic_id:
                        continue
                    try:
                        vnic = vnc_client.get_vnic(vnic_id=vnic_id).data
                        if getattr(vnic, "public_ip", None):
                            logging.info(f"Public IP resolved: {vnic.public_ip}")
                            return vnic.public_ip
                    except Exception:
                        pass
                    try:
                        private_ips = vnc_client.list_private_ips(subnet_id=subnet_id, vnic_id=vnic_id).data
                        if private_ips:
                            pip_id = getattr(private_ips[0], "id", None)
                            if pip_id:
                                pub_details = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=pip_id)
                                pub_resp = vnc_client.get_public_ip_by_private_ip_id(pub_details)
                                if getattr(pub_resp.data, "ip_address", None):
                                    logging.info(f"Public IP resolved: {pub_resp.data.ip_address}")
                                    return pub_resp.data.ip_address
                    except Exception:
                        pass
        except Exception as e:
            logging.debug(f"IP resolution attempt {attempt} error: {e}")
        time.sleep(delay)
    logging.warning("Public IP could not be resolved within timeout. Please check OCI Console.")
    return "Pending (Check OCI Console)"


# ==============================================================================
# Main Execution Logic
# ==============================================================================


def main():
    global displayName, compartmentId, subnetId, imageId, availabilityDomains

    LOG_FORMAT = "[%(levelname)s] %(asctime)s - %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    target_shape = "VM.Standard.A1.Flex"
    logging.info("#####################################################")
    logging.info(f"Script to spawn {target_shape} instance")

    # Check clock skew against Oracle Cloud servers before API operations
    clock_ok, skew_diff = check_system_clock_skew()
    if skew_diff is not None:
        if not clock_ok:
            logging.warning(
                f"⚠️ System clock drift detected: local clock is off by {skew_diff:.1f}s. "
                "OCI API rejects signatures when drift > 300s (causing HTTP 401 NotAuthenticated). "
                "Please synchronize clock with NTP (e.g. 'sudo systemctl restart systemd-timesyncd')."
            )
        else:
            logging.info(f"System clock check passed: drift is {skew_diff:.2f}s (well within 300s limit).")

    if not is_set(displayName):
        displayName = "instance-a1-flex"
        logging.info(f"Using default display name: '{displayName}'")

    config = load_oci_config()

    to_launch_instance = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)
    vnc_client = oci.core.VirtualNetworkClient(config)
    volume_client = oci.core.BlockstorageClient(config)

    # Resolve root compartment / tenancy OCID
    tenancy_id = config.get("tenancy")
    if not is_set(compartmentId):
        if tenancy_id:
            compartmentId = tenancy_id
            logging.info(f"Using tenancy OCID as default compartmentId: {compartmentId}")
        else:
            logging.critical("compartmentId is not set and tenancy OCID missing in config. **SCRIPT STOPPED**")
            sys.exit(1)

    # Fetch tenancy display name
    cloud_name = "N/A"
    try:
        cloud_name = identity_client.get_tenancy(tenancy_id=tenancy_id or compartmentId).data.name
    except Exception as e:
        logging.warning(f"Could not retrieve tenancy name: {e}")
        cloud_name = tenancy_id or "Unknown"

    # Fetch user email / username safely (IAM permissions might be restricted)
    email = "N/A"
    try:
        user_id = config.get("user")
        if user_id and is_set(user_id):
            try:
                user_data = identity_client.get_user(user_id=user_id).data
                email = getattr(user_data, "email", None) or getattr(user_data, "name", None) or user_id
            except Exception:
                email = user_id
        if email == "N/A" and tenancy_id:
            users = identity_client.list_users(compartment_id=tenancy_id).data
            if users:
                email = getattr(users[0], "email", None) or getattr(users[0], "name", None) or "N/A"
    except Exception as e:
        logging.warning(f"Could not retrieve user info (IAM permissions restricted): {e}")

    logging.info(f"Target Instance: {target_shape} - {ocpus} OCPUs - {memory_in_gbs} GB RAM")
    logging.info(f"Cloud Account: {cloud_name} | User/Email: {email}")

    # Resolve Availability Domains, Subnet, and Image
    availability_domains = resolve_availability_domains(identity_client, compartmentId, availabilityDomains)
    subnet_id = resolve_subnet_id(vnc_client, compartmentId, subnetId)

    # Initialize Telegram Bot if configured
    bot = None
    if is_set(bot_token) and is_set(uid):
        try:
            bot = telebot.TeleBot(bot_token)
        except Exception as e:
            logging.warning(f"Failed to initialize Telegram Bot: {e}")

    # Precheck Storage Availability
    logging.info("Checking available storage in account...")
    target_boot_size = parse_int_or_none(boot_volume_size_in_gbs)
    if is_set(boot_volume_id):
        required_storage = 0
        logging.info(f"Using existing boot volume ({boot_volume_id}); 0 GB additional storage required.")
    else:
        required_storage = target_boot_size if target_boot_size is not None else 47
        if target_boot_size is not None and target_boot_size < 47:
            logging.warning(
                f"Boot volume size ({target_boot_size} GB) is below OCI minimum (47 GB). Adjusting to 47 GB."
            )
            target_boot_size = 47
            required_storage = 47

    total_volume_size = 0
    try:
        list_volumes = volume_client.list_volumes(compartment_id=compartmentId).data
        if list_volumes:
            for b_vol in list_volumes:
                if b_vol.lifecycle_state not in ("TERMINATING", "TERMINATED"):
                    total_volume_size += int(b_vol.size_in_gbs or 0)

        # Query all availability domains in compartment/tenancy for complete storage accounting
        all_ads_to_check = set(availability_domains)
        try:
            detected_all_ads = identity_client.list_availability_domains(compartment_id=compartmentId).data
            for ad_item in detected_all_ads:
                if getattr(ad_item, "name", None):
                    all_ads_to_check.add(ad_item.name)
        except Exception:
            pass

        for ad in all_ads_to_check:
            try:
                list_boot = volume_client.list_boot_volumes(availability_domain=ad, compartment_id=compartmentId).data
                if list_boot:
                    for b_vol in list_boot:
                        if b_vol.lifecycle_state not in ("TERMINATING", "TERMINATED"):
                            total_volume_size += int(b_vol.size_in_gbs or 0)
            except Exception:
                pass
    except Exception as e:
        logging.warning(f"Could not inspect existing storage volumes: {e}")

    free_storage = 200 - total_volume_size
    logging.info(f"Storage: {total_volume_size} GB used, {free_storage} GB free out of 200 GB Always Free tier.")

    if free_storage < required_storage:
        logging.critical(
            f"Insufficient storage! Only {free_storage} GB out of 200 GB left in your Oracle Cloud account. "
            f"{required_storage} GB storage required to create this VPS. "
            f"Please delete unused boot or block volumes. **SCRIPT STOPPED**"
        )
        sys.exit(1)

    # Precheck Existing Instances & Resource Limits
    logging.info("Checking current instances in account...")
    active_instances_resp = to_launch_instance.list_instances(compartment_id=compartmentId).data
    total_ocpus = 0
    total_memory = 0
    active_a1_count = 0
    active_instance_names = set()

    if active_instances_resp:
        logging.info(f"{len(active_instances_resp)} total instance(s) found in compartment:")
        for inst in active_instances_resp:
            is_terminated = inst.lifecycle_state in ("TERMINATING", "TERMINATED")
            shape_cfg = getattr(inst, "shape_config", None)
            inst_ocpus = (
                int(shape_cfg.ocpus)
                if (shape_cfg and shape_cfg.ocpus is not None)
                else (1 if "Micro" in (inst.shape or "") else 0)
            )
            inst_mem = (
                float(shape_cfg.memory_in_gbs)
                if (shape_cfg and shape_cfg.memory_in_gbs is not None)
                else (1.0 if "Micro" in (inst.shape or "") else 0.0)
            )

            logging.info(
                f"- {inst.display_name} | {inst.shape} | {inst_ocpus} OCPUs, {inst_mem} GB RAM | State: {inst.lifecycle_state}"
            )
            if not is_terminated:
                active_instance_names.add(inst.display_name)
                if inst.shape == target_shape:
                    active_a1_count += 1
                    total_ocpus += inst_ocpus
                    total_memory += inst_mem

    logging.info(f"Active {target_shape} instances: {active_a1_count}")
    logging.info(
        f"Used: {total_ocpus} OCPUs, {total_memory} GB RAM || Available: {4 - total_ocpus} OCPUs, {24 - total_memory} GB RAM"
    )

    if total_ocpus + ocpus > 4 or total_memory + memory_in_gbs > 24:
        logging.critical(
            f"Resource limit exceeded! Always Free tier allows max 4 OCPUs and 24 GB RAM total for A1.Flex.\n"
            f"Currently active: {total_ocpus} OCPUs, {total_memory} GB RAM. "
            f"Requested: +{ocpus} OCPUs, +{memory_in_gbs} GB RAM. **SCRIPT STOPPED**"
        )
        sys.exit(1)

    if displayName in active_instance_names:
        logging.critical(
            f"An active instance with display name '{displayName}' already exists! Change displayName. **SCRIPT STOPPED**"
        )
        sys.exit(1)

    # Configure Source Details (Image vs Boot Volume)
    if is_set(boot_volume_id):
        logging.info(f"Using existing boot volume: {boot_volume_id}")
        source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(
            source_type="bootVolume",
            boot_volume_id=boot_volume_id,
        )
    else:
        resolved_img_id = resolve_image_id(to_launch_instance, compartmentId, target_shape, imageId)
        logging.info(f"Using image source: {resolved_img_id}")
        img_kwargs = {
            "source_type": "image",
            "image_id": resolved_img_id,
        }
        if target_boot_size is not None:
            img_kwargs["boot_volume_size_in_gbs"] = target_boot_size
        source_details = oci.core.models.InstanceSourceViaImageDetails(**img_kwargs)

    # Warn if SSH key missing
    if not is_set(ssh_authorized_keys):
        logging.warning("⚠️ No SSH public key configured! You will not be able to SSH into the instance.")

    # Shape config details (A1.Flex is flexible)
    shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
        ocpus=float(ocpus),
        memory_in_gbs=float(memory_in_gbs),
    )

    msg_id = None
    if bot and is_set(uid):
        startup_msg = (
            f"🚀 OCI Spawner Started\n\n"
            f"Cloud Account: {cloud_name}\n"
            f"Email: {email}\n"
            f"Shape: {target_shape} ({ocpus} OCPUs / {memory_in_gbs} GB RAM)\n"
            f"Status: Polling for capacity...\n"
            f"Started (UTC): {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        msg_id = send_telegram_msg(bot, uid, startup_msg)

    wait_s_for_retry = max(int(retry_interval), 30)
    total_count = 0
    cycle_count = 0
    last_tg_update_time = time.time()
    launched_instance = None

    logging.info(f"Pre-check passed! Ready to create {target_shape} ('{displayName}').")
    logging.info(
        f"Starting launch loop with safe retry interval of {wait_s_for_retry}s "
        f"({len(availability_domains)} AD(s) per cycle)..."
    )

    try:
        while launched_instance is None:
            cycle_count += 1

            # Safe state verification before cycle: check if instance was already spawned
            spawned = check_instance_spawned(to_launch_instance, compartmentId, displayName)
            if spawned:
                launched_instance = spawned
                logging.info(f"🎉 Instance '{displayName}' is already active (OCID: {launched_instance.id})!")
                break

            cycle_rate_limited = False
            for ad in availability_domains:
                total_count += 1
                now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                logging.info(f"[Attempt #{total_count} | Cycle #{cycle_count}] Target AD: {ad} at {now_utc} UTC...")

                # Lightweight Capacity Report Pre-check if enabled
                if check_capacity_first:
                    capacity_status = check_ad_capacity(
                        compute_client=to_launch_instance,
                        compartment_id=compartmentId,
                        ad=ad,
                        shape=target_shape,
                        ocpus=ocpus,
                        memory_in_gbs=memory_in_gbs,
                    )
                    if capacity_status is False:
                        logging.info(f"Capacity report for {ad}: OUT_OF_HOST_CAPACITY. Checking next candidate...")
                        if len(availability_domains) > 1:
                            time.sleep(2)
                        continue
                    elif capacity_status is True:
                        logging.info(f"Capacity report for {ad}: AVAILABLE! Initiating launch...")

                launch_kwargs = {
                    "availability_domain": ad,
                    "compartment_id": compartmentId,
                    "display_name": displayName,
                    "shape": target_shape,
                    "source_details": source_details,
                    "create_vnic_details": oci.core.models.CreateVnicDetails(
                        assign_public_ip=True,
                        subnet_id=subnet_id,
                    ),
                    "is_pv_encryption_in_transit_enabled": True,
                    "shape_config": shape_config,
                }
                if is_set(ssh_authorized_keys):
                    launch_kwargs["metadata"] = {"ssh_authorized_keys": ssh_authorized_keys}

                instance_detail = oci.core.models.LaunchInstanceDetails(**launch_kwargs)
                retry_token = f"spawn-{displayName[:20]}-{total_count}"

                try:
                    launch_response = to_launch_instance.launch_instance(
                        instance_detail,
                        opc_retry_token=retry_token,
                    )
                    launched_instance = launch_response.data
                    logging.info(f"🎉 SUCCESS! Instance launched! OCID: {launched_instance.id}")
                    break  # Break out of AD loop immediately on launch success!

                except oci.exceptions.ServiceError as e:
                    if e.status == 429:
                        cycle_rate_limited = True
                        retry_after = None
                        if getattr(e, "headers", None):
                            hdr = e.headers.get("retry-after") or e.headers.get("Retry-After")
                            if hdr and str(hdr).isdigit():
                                retry_after = int(hdr)

                        if retry_after:
                            wait_s_for_retry = max(retry_after, 60)
                        else:
                            wait_s_for_retry = min(max(wait_s_for_retry * 2, 120), 300)

                        logging.warning(
                            f"HTTP 429 TooManyRequests: OCI rate limit hit. Backing off for {wait_s_for_retry}s to avoid ban. "
                            f"Message: {e.message}"
                        )
                        break  # Stop checking other ADs in this cycle when rate-limited

                    elif e.status == 401:
                        logging.critical(
                            f"HTTP 401 NotAuthenticated: {e.code} - {e.message}\n"
                            "Note: System clock skew exceeding 5 minutes can cause 401 errors. "
                            "Check your system NTP clock synchronization and API credentials. **SCRIPT STOPPED**"
                        )
                        if bot and is_set(uid):
                            send_telegram_msg(
                                bot,
                                uid,
                                f"🚨 OCI Spawner Stopped!\n\nAuthentication Failed (401): {e.message}\n"
                                "Check your API keys and verify system clock (NTP) sync.",
                            )
                        sys.exit(1)

                    elif 400 <= e.status < 500 and e.status != 408:
                        logging.critical(
                            f"Fatal OCI Client Error ({e.status} {e.code}): {e.message}\n"
                            "Stopping script to prevent API ban due to invalid request parameters. **SCRIPT STOPPED**"
                        )
                        if bot and is_set(uid):
                            send_telegram_msg(
                                bot,
                                uid,
                                f"🚨 OCI Spawner Stopped!\n\nFatal Error: {e.status} - {e.code}\nDetail: {e.message}",
                            )
                        sys.exit(1)

                    else:
                        # 500 Out of host capacity, 408 timeout, or transient server error
                        logging.info(f"{e.status} - {e.code}: {e.message}. (Attempt #{total_count})")
                        if len(availability_domains) > 1:
                            time.sleep(2)

                except Exception as e:
                    logging.warning(f"Connection or unexpected network error: {e}. Checking if instance spawned...")
                    spawned = check_instance_spawned(to_launch_instance, compartmentId, displayName)
                    if spawned:
                        launched_instance = spawned
                        logging.info(f"🎉 Instance was successfully launched before disconnect! OCID: {spawned.id}")
                        break
                    break  # Sleep cycle cooldown

            if launched_instance is not None:
                break

            # Periodic Telegram status update (at most every 5 minutes to avoid rate limits)
            if bot and is_set(uid) and (time.time() - last_tg_update_time >= 300):
                last_tg_update_time = time.time()
                status_text = (
                    f"Cloud Account: {cloud_name}\n"
                    f"Email: {email}\n"
                    f"Target Shape: {target_shape}\n"
                    f"Attempts: {total_count} (Cycles: {cycle_count})\n"
                    f"Bot Status: Polling for capacity...\n"
                    f"Last Checked (UTC): {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
                )
                msg_id = update_telegram_status(bot, uid, status_text, msg_id)

            # Gradually decay back to retry_interval if elevated backoff occurred
            if not cycle_rate_limited and wait_s_for_retry > max(int(retry_interval), 30):
                wait_s_for_retry = max(max(int(retry_interval), 30), wait_s_for_retry - 15)

            logging.info(f"Cycle #{cycle_count} completed. Sleeping {wait_s_for_retry}s before next cycle...")
            time.sleep(wait_s_for_retry)

    except KeyboardInterrupt:
        logging.info("Process interrupted by user. Exiting...")
        sys.exit(0)

    # Post-launch IP resolution phase (completely separated from launch loop to prevent duplicate launches)
    if launched_instance:
        logging.info(f"Provisioning instance '{displayName}' (OCID: {launched_instance.id})...")
        public_ip = resolve_instance_public_ip(
            compute_client=to_launch_instance,
            vnc_client=vnc_client,
            compartment_id=compartmentId,
            subnet_id=subnet_id,
            instance_id=launched_instance.id,
        )

        summary = (
            f"🎉 VPS '{displayName}' spawned successfully!\n"
            f"   OCID: {launched_instance.id}\n"
            f"   Public IP: {public_ip}\n"
            f"   Cloud Account: {cloud_name}\n"
            f"   Email: {email}\n"
            f"   Total Attempts: {total_count}"
        )
        logging.info(summary)

        if bot and is_set(uid):
            success_msg = (
                f'🎉 "{displayName}" VPS created successfully!\n\n'
                f"Shape: {target_shape} ({ocpus} OCPUs / {memory_in_gbs} GB RAM)\n"
                f"Public IP: {public_ip}\n"
                f"Cloud Account: {cloud_name}\n"
                f"Email: {email}\n"
                f"Total Attempts: {total_count}"
            )
            send_telegram_msg(bot, uid, success_msg, delete_msg_id=msg_id, max_retries=3)

    sys.exit(0)


if __name__ == "__main__":
    main()
