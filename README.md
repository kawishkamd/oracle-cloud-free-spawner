# OCI Instance Spawner

A clean automation tool to provision Oracle Cloud Infrastructure (OCI) Always Free instances (AMD and ARM Ampere) by continuously retrying when capacity is available.

## What is this?
Two lightweight Python scripts designed to automate OCI instance creation:
- **`amd/amd.py`**: Spawns `VM.Standard.E2.1.Micro` (AMD micro) instances.
- **`arm/arm.py`**: Spawns `VM.Standard.A1.Flex` (ARM Ampere) instances (up to 4 OCPUs / 24 GB RAM).

Both scripts run in a loop, handle OCI rate limits, and send status updates/success notifications via a Telegram Bot.

## Why is this?
Oracle Cloud's Always Free tier resources are highly sought after and frequently return **"Out of host capacity"** errors when trying to create them manually in the web console. These scripts poll the API continuously until the resources are successfully claimed.

## How to use it?

### 1. Install Dependencies
Ensure you have Python 3 installed, then run:
```bash
pip install -r amd/requirements.txt
```

### 2. Configure OCI Authentication
1. Edit the `./config` file inside either the `amd/` or `arm/` directory with your user OCID, tenancy OCID, region, and API fingerprint.
2. Replace the contents of the `oci_private_key.pem` file with your OCI private key.

### 3. Configure Spawner settings
Open `amd/amd.py` or `arm/arm.py` and configure the placeholder variables at the top:
*   `availabilityDomains` (e.g., `["xx:AP-MUMBAI-1-AD-1"]`)
*   `compartmentId` & `subnetId` (from your OCI console network configuration)
*   `ssh_authorized_keys` (your public SSH key)
*   `imageId` (OCI image OCID for your target OS)
*   `bot_token` & `uid` (Telegram Bot token and your Telegram User ID for updates)

> [!TIP]
> **Easily Finding OCI OCIDs & configurations (Browser inspect trick):**
> Finding image IDs, subnet OCIDs, and availability domains manually in the OCI Console can be tedious. Instead:
> 1. Go to the OCI Console and set up your instance creation options as you normally would.
> 2. Open your browser's Developer Tools (`F12` or right-click -> **Inspect**) and go to the **Network** tab.
> 3. Click **Create** in the OCI console.
> 4. The creation request will fail in the UI with "Out of capacity", but in the Network tab, find the failed API call (a `POST` request to the `instances` endpoint).
> 5. View the **Request Payload** (JSON body) of this request. It will contain all the exact IDs and configurations (like `compartmentId`, `subnetId`, `imageId`, `availabilityDomain`) ready to be copied directly into the python scripts.

### 4. Run in the Background
Run the spawner continuously in the background using `nohup`:
```bash
# To spawn AMD Instance
cd amd && nohup python3 amd.py > output.log 2>&1 &

# To spawn ARM Instance
cd arm && nohup python3 arm.py > output.log 2>&1 &
```