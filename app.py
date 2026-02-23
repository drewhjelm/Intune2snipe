#!/usr/bin/env python3
import os
import re
import sys
import json
import logging
import argparse
import requests
from msal import ConfidentialClientApplication
from azure.identity import CertificateCredential
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── CONSTANTS ──────────────────────────────────────────────────────────────────
# HTTP timeout: (connect timeout, read timeout) in seconds
DEFAULT_TIMEOUT = (3.05, 30)
# Maximum length of response body to include in error logs (to prevent log spam)
MAX_RESPONSE_LOG_LENGTH = 200
SCOPE = ["https://graph.microsoft.com/.default"]
# ────────────────────────────────────────────────────────────────────────────────

# Configure logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ─── CONFIG (no defaults for required vars) ────────────────────────────────────
TENANT_ID = None
CLIENT_ID = None
CLIENT_SECRET = None
SNIPEIT_URL = None
SNIPEIT_API_TOKEN = None
DEFAULT_STATUS_NAME = os.getenv("SNIPEIT_DEFAULT_STATUS", "Ready to Deploy")
AZURE_GROUP_IDS = []

# Session and headers (initialized in validate_and_init_config)
session = None
headers_graph = None
headers_snipeit = None
# ────────────────────────────────────────────────────────────────────────────────

def validate_and_init_config():
    """
    Validate required environment variables and initialize configuration.
    Exits with non-zero status if validation fails.
    """
    global TENANT_ID, CLIENT_ID, CLIENT_SECRET, SNIPEIT_URL, SNIPEIT_API_TOKEN
    global AZURE_GROUP_IDS, session, headers_graph, headers_snipeit
    
    # Required environment variables (except auth credentials which are validated separately)
    required_vars = {
        "AZURE_TENANT_ID": "Azure tenant ID",
        "AZURE_CLIENT_ID": "Azure client ID",
        "SNIPEIT_URL": "Snipe-IT API URL",
        "SNIPEIT_API_TOKEN": "Snipe-IT API token"
    }
    
    missing_vars = []
    for var_name, description in required_vars.items():
        value = os.getenv(var_name)
        if not value or value.startswith("your-"):
            missing_vars.append(f"  - {var_name}: {description}")
    
    if missing_vars:
        logger.error("Missing required environment variables:")
        for var in missing_vars:
            logger.error(var)
        logger.error("\nPlease set all required environment variables before running.")
        sys.exit(1)
    
    # Validate authentication credentials: require either certificate OR secret
    cert_pem = os.getenv("AZURE_CLIENT_CERT_PEM")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")
    
    # Check if cert PEM is valid (not empty and not a placeholder)
    has_valid_cert = cert_pem and not cert_pem.startswith("your-")
    # Check if secret is valid (not empty and not a placeholder)
    has_valid_secret = client_secret and not client_secret.startswith("your-")
    
    if not has_valid_cert and not has_valid_secret:
        logger.error("Authentication credentials missing:")
        logger.error("  Either AZURE_CLIENT_CERT_PEM or AZURE_CLIENT_SECRET must be set")
        logger.error("  (and not a placeholder like 'your-...')")
        sys.exit(1)
    
    # Read and validate configuration
    TENANT_ID = os.getenv("AZURE_TENANT_ID")
    CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
    CLIENT_SECRET = client_secret if has_valid_secret else None
    SNIPEIT_URL = os.getenv("SNIPEIT_URL")
    SNIPEIT_API_TOKEN = os.getenv("SNIPEIT_API_TOKEN")
    
    # Validate and normalize SNIPEIT_URL
    if not SNIPEIT_URL.endswith("/api/v1"):
        # Try to normalize it
        SNIPEIT_URL = SNIPEIT_URL.rstrip("/")
        if SNIPEIT_URL.endswith("/api"):
            SNIPEIT_URL += "/v1"
        elif not SNIPEIT_URL.endswith("/api/v1"):
            SNIPEIT_URL += "/api/v1"
        logger.warning(f"SNIPEIT_URL normalized to: {SNIPEIT_URL}")
    
    # Parse Azure Group IDs
    azure_groups_str = os.getenv("AZURE_GROUP_IDS", "")
    if azure_groups_str:
        AZURE_GROUP_IDS = [gid.strip() for gid in azure_groups_str.split(",") if gid.strip()]
    
    # Initialize requests session with retry logic
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # Acquire access token - prefer certificate, fallback to secret
    access_token = None
    
    if has_valid_cert:
        logger.info("Attempting to acquire Microsoft Graph access token using certificate...")
        try:
            # Environment variables are always strings, so encode to bytes
            cert_data = cert_pem.encode('utf-8')
            
            credential = CertificateCredential(
                tenant_id=TENANT_ID,
                client_id=CLIENT_ID,
                certificate_data=cert_data
            )
            
            token_result = credential.get_token("https://graph.microsoft.com/.default")
            access_token = token_result.token
            logger.info("Successfully acquired Microsoft Graph access token using certificate")
        except Exception as e:
            logger.error(f"Certificate authentication failed: {e}")
            logger.error("Common causes: invalid PEM format, certificate expired, wrong tenant/client ID, or insufficient permissions")
            
            # If certificate auth failed but we have a secret, try to fall back
            if has_valid_secret:
                logger.warning("Falling back to client secret authentication...")
            else:
                logger.error("No client secret available for fallback. Exiting.")
                sys.exit(1)
    
    # Use MSAL client secret if certificate didn't succeed
    if access_token is None and has_valid_secret:
        logger.info("Acquiring Microsoft Graph access token using client secret...")
        authority = f"https://login.microsoftonline.com/{TENANT_ID}"
        auth_app = ConfidentialClientApplication(
            client_id=CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=authority
        )
        
        token = auth_app.acquire_token_for_client(scopes=SCOPE)
        
        if "access_token" not in token:
            error = token.get("error", "unknown_error")
            error_desc = token.get("error_description", "No description provided")
            correlation_id = token.get("correlation_id", "N/A")
            logger.error(f"Failed to acquire Graph access token")
            logger.error(f"Error: {error}")
            logger.error(f"Description: {error_desc}")
            logger.error(f"Correlation ID: {correlation_id}")
            sys.exit(1)
        
        access_token = token['access_token']
        logger.info("Successfully acquired Microsoft Graph access token using client secret")
    
    # Set up headers
    headers_graph = {"Authorization": f"Bearer {access_token}"}
    headers_snipeit = {
        "Authorization": f"Bearer {SNIPEIT_API_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    
    return headers_graph, headers_snipeit


# Regex to strip Android-Enterprise GUID prefixes from UPNs
GUID_PREFIX = re.compile(r'^[0-9a-f]{32}')

def normalize_upn(upn_raw):
    if not upn_raw:
        return None
    m = GUID_PREFIX.match(upn_raw)
    return upn_raw[m.end():] if m else upn_raw

# ─── SNIPE-IT LOOKUPS & CREATORS ─────────────────────────────────────────────

def get_or_create_category(name):
    if not name:
        return None
    try:
        r = session.get(
            f"{SNIPEIT_URL}/categories",
            headers=headers_snipeit,
            params={"search": name},
            timeout=DEFAULT_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching category '{name}'")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching category '{name}': {e}")
        return None
    except ValueError:
        logger.error(f"Invalid JSON response when fetching category '{name}'")
        return None
    
    rows = data.get("rows", [])
    if rows:
        return rows[0]["id"]
    
    payload = {"name": name, "category_type": "asset"}
    try:
        c = session.post(
            f"{SNIPEIT_URL}/categories",
            headers=headers_snipeit,
            json=payload,
            timeout=DEFAULT_TIMEOUT
        )
        if c.status_code in (200, 201):
            resp_data = c.json()
            if resp_data.get("payload"):
                return resp_data["payload"]["id"]
        logger.warning(f"Could not create category '{name}': status={c.status_code}, response={c.text[:MAX_RESPONSE_LOG_LENGTH]}")
    except requests.exceptions.Timeout:
        logger.error(f"Timeout creating category '{name}'")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error creating category '{name}': {e}")
    except ValueError:
        logger.error(f"Invalid JSON response when creating category '{name}'")
    return None


def get_or_create_manufacturer(name):
    if not name:
        return None
    try:
        r = session.get(
            f"{SNIPEIT_URL}/manufacturers",
            headers=headers_snipeit,
            params={"search": name},
            timeout=DEFAULT_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching manufacturer '{name}'")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching manufacturer '{name}': {e}")
        return None
    except ValueError:
        logger.error(f"Invalid JSON response when fetching manufacturer '{name}'")
        return None
    
    rows = data.get("rows", [])
    if rows:
        return rows[0]["id"]
    
    payload = {"name": name}
    try:
        c = session.post(
            f"{SNIPEIT_URL}/manufacturers",
            headers=headers_snipeit,
            json=payload,
            timeout=DEFAULT_TIMEOUT
        )
        if c.status_code in (200, 201):
            resp_data = c.json()
            if resp_data.get("payload"):
                return resp_data["payload"]["id"]
        logger.warning(f"Could not create manufacturer '{name}': status={c.status_code}, response={c.text[:MAX_RESPONSE_LOG_LENGTH]}")
    except requests.exceptions.Timeout:
        logger.error(f"Timeout creating manufacturer '{name}'")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error creating manufacturer '{name}': {e}")
    except ValueError:
        logger.error(f"Invalid JSON response when creating manufacturer '{name}'")
    return None


def get_or_create_model(model_number, manufacturer_id, category_id):
    if not model_number:
        return None
    try:
        r = session.get(
            f"{SNIPEIT_URL}/models",
            headers=headers_snipeit,
            params={"search": model_number},
            timeout=DEFAULT_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching model '{model_number}'")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching model '{model_number}': {e}")
        return None
    except ValueError:
        logger.error(f"Invalid JSON response when fetching model '{model_number}'")
        return None
    
    rows = data.get("rows", [])
    for row in rows:
        if row.get("model_number") == model_number or row.get("name") == model_number:
            return row["id"]
    
    payload = {
        "name": model_number,
        "model_number": model_number,
        "manufacturer_id": manufacturer_id,
        "category_id": category_id
    }
    try:
        c = session.post(
            f"{SNIPEIT_URL}/models",
            headers=headers_snipeit,
            json=payload,
            timeout=DEFAULT_TIMEOUT
        )
        if c.status_code in (200, 201):
            resp_data = c.json()
            if resp_data.get("payload"):
                return resp_data["payload"]["id"]
        logger.warning(f"Could not create model '{model_number}': status={c.status_code}, response={c.text[:MAX_RESPONSE_LOG_LENGTH]}")
    except requests.exceptions.Timeout:
        logger.error(f"Timeout creating model '{model_number}'")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error creating model '{model_number}': {e}")
    except ValueError:
        logger.error(f"Invalid JSON response when creating model '{model_number}'")
    return None


def get_status_id(name):
    try:
        r = session.get(
            f"{SNIPEIT_URL}/statuslabels",
            headers=headers_snipeit,
            timeout=DEFAULT_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.Timeout:
        logger.error("Timeout fetching status labels")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Unable to fetch status labels: {e}")
        return None
    except ValueError:
        logger.error("Invalid JSON response when fetching status labels")
        return None
    
    rows = data.get("rows", [])
    for sl in rows:
        if sl.get("name") == name:
            return sl.get("id")
    logger.error(f"Status label '{name}' not found. Available: {[sl.get('name') for sl in rows]}")
    return None


def get_snipeit_user_id(upn):
    if not upn:
        return None
    try:
        r = session.get(
            f"{SNIPEIT_URL}/users",
            headers=headers_snipeit,
            params={"search": upn},
            timeout=DEFAULT_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()
        rows = data.get("rows", [])
        return rows[0]["id"] if rows else None
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching user for UPN '{upn}'")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching user for UPN '{upn}': {e}")
        return None
    except ValueError:
        logger.error(f"Invalid JSON response when fetching user for UPN '{upn}'")
        return None
# ────────────────────────────────────────────────────────────────────────────────


def fetch_azure_ad_device_ids_from_groups(group_ids):
    """
    Fetch Azure AD device IDs from the specified groups.
    Returns a set of Azure AD device object IDs.
    """
    if not group_ids:
        return None  # None means no filtering
    
    device_ids = set()
    for group_id in group_ids:
        if not group_id:
            continue
        url = f"https://graph.microsoft.com/v1.0/groups/{group_id}/members/microsoft.graph.device"
        while url:
            try:
                r = session.get(url, headers=headers_graph, timeout=DEFAULT_TIMEOUT)
                if r.status_code == 404:
                    logger.warning(f"Group {group_id} not found or not accessible")
                    break
                if r.status_code == 403:
                    raise RuntimeError(f"403 Forbidden accessing group {group_id}: check Group.Read.All permission")
                r.raise_for_status()
                data = r.json()
                for device in data.get("value", []):
                    device_ids.add(device.get("id"))
                url = data.get("@odata.nextLink")
            except requests.exceptions.Timeout:
                logger.error(f"Timeout fetching devices from group {group_id}")
                break
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to fetch devices from group {group_id}: {e}")
                break
            except ValueError:
                logger.error(f"Invalid JSON response when fetching devices from group {group_id}")
                break
    
    logger.info(f"Found {len(device_ids)} Azure AD devices from {len(group_ids)} group(s)")
    return device_ids


def fetch_managed_devices(platform, group_ids=None):
    """
    Fetch Intune managed devices, optionally filtering by operatingSystem and group membership.
    
    Args:
        platform: OS platform filter ('windows', 'android', 'ios', 'macos', 'all')
        group_ids: List of Azure AD group IDs to filter by. If None or empty, no group filtering is applied.
    
    Returns:
        List of managed devices that match the platform and group filters.
    """
    # Fetch Azure AD device IDs from groups if group filtering is enabled
    azure_ad_device_ids = None
    if group_ids:
        azure_ad_device_ids = fetch_azure_ad_device_ids_from_groups(group_ids)
        if azure_ad_device_ids is not None and len(azure_ad_device_ids) == 0:
            logger.warning("No devices found in specified groups, no devices will be synced")
            return []
    
    url = "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices"
    devices = []
    while url:
        try:
            r = session.get(url, headers=headers_graph, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 403:
                raise RuntimeError("403 Forbidden fetching devices: check permissions")
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.Timeout:
            logger.error("Timeout fetching managed devices")
            break
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching managed devices: {e}")
            break
        except ValueError:
            logger.error("Invalid JSON response when fetching managed devices")
            break
        
        for dev in data.get("value", []):
            # Filter by platform
            os_val = dev.get("operatingSystem", "").lower()
            platform_match = (
                platform == 'all' or
                (platform == 'windows' and os_val.startswith('windows')) or
                (platform == 'android' and 'android' in os_val) or
                (platform == 'ios' and 'ios' in os_val) or
                (platform == 'macos' and 'mac' in os_val)
            )
            
            if not platform_match:
                continue
            
            # Filter by group membership if groups are specified
            if azure_ad_device_ids is not None:
                azure_ad_device_id = dev.get("azureActiveDeviceId") or dev.get("azureADDeviceId")
                if azure_ad_device_id not in azure_ad_device_ids:
                    continue  # Skip devices not in the specified groups
            
            devices.append(dev)
        url = data.get("@odata.nextLink")
    return devices


def send_to_snipeit(device, category_id, status_id, dry_run=False):
    raw_upn = device.get("userPrincipalName")
    upn = normalize_upn(raw_upn)
    snipe_user_id = get_snipeit_user_id(upn)

    man_name = device.get("manufacturer")
    mod_number = device.get("model")
    man_id = get_or_create_manufacturer(man_name)
    mod_id = get_or_create_model(mod_number, man_id, category_id) if man_id else None

    if mod_id is None:
        logger.warning(f"Skipping '{device.get('deviceName')}': model_id unavailable")
        return

    payload = {
        "name": device.get("deviceName"),
        "serial": device.get("serialNumber"),
        "manufacturer_id": man_id,
        "model_id": mod_id,
        "status_id": status_id,
        "notes": f"Imported from Intune: {man_name} {mod_number}"
    }

    if dry_run:
        logger.info(f"[DRY RUN] {json.dumps(payload)} → checkout to user_id {snipe_user_id}")
        return

    try:
        r = session.post(
            f"{SNIPEIT_URL}/hardware",
            headers=headers_snipeit,
            json=payload,
            timeout=DEFAULT_TIMEOUT
        )
        resp = r.json()
    except requests.exceptions.Timeout:
        logger.error(f"Timeout creating asset for '{device.get('deviceName')}'")
        return
    except requests.exceptions.RequestException as e:
        logger.error(f"Error creating asset for '{device.get('deviceName')}': {e}")
        return
    except ValueError:
        logger.error(f"Invalid JSON response when creating asset for '{device.get('deviceName')}': status={r.status_code}, response={r.text[:MAX_RESPONSE_LOG_LENGTH]}")
        return
    
    if r.status_code not in (200, 201) or resp.get("status") != "success":
        logger.error(f"Failed to create '{device.get('deviceName')}': status={r.status_code}, response={r.text[:MAX_RESPONSE_LOG_LENGTH]}")
        return

    asset_id = resp["payload"]["id"]
    logger.info(f"Imported: {device.get('deviceName')} → asset ID {asset_id}")

    if snipe_user_id:
        try:
            co = session.post(
                f"{SNIPEIT_URL}/hardware/{asset_id}/checkout",
                headers=headers_snipeit,
                json={"user_id": snipe_user_id},
                timeout=DEFAULT_TIMEOUT
            )
            co_resp = co.json()
            if co.status_code in (200, 201) and co_resp.get("status") == "success":
                logger.info(f"Checked out asset {asset_id} to user_id {snipe_user_id}")
            else:
                logger.error(f"Checkout failed for asset {asset_id}: status={co.status_code}, response={co.text[:MAX_RESPONSE_LOG_LENGTH]}")
        except requests.exceptions.Timeout:
            logger.error(f"Timeout checking out asset {asset_id}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error checking out asset {asset_id}: {e}")
        except ValueError:
            logger.error(f"Invalid JSON response when checking out asset {asset_id}")


def main(dry_run, platform, group_ids=None):
    devices = fetch_managed_devices(platform, group_ids=group_ids)
    filter_info = f"platform '{platform}'"
    if group_ids:
        filter_info += f" and {len(group_ids)} group(s)"
    logger.info(f"Found {len(devices)} Intune devices matching {filter_info}")
    category_id = get_or_create_category("Intune")
    status_id = get_status_id(DEFAULT_STATUS_NAME)
    if status_id is None:
        sys.exit(1)
    logger.info(f"Using category_id={category_id}, status_id={status_id}")
    for d in devices:
        send_to_snipeit(d, category_id=category_id, status_id=status_id, dry_run=dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Intune → Snipe-IT")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing to Snipe-IT")
    parser.add_argument(
        "--platform", choices=["windows", "android", "ios", "macos", "all"],
        default="all", help="Filter devices by OS"
    )
    parser.add_argument(
        "--groups", type=str, default=None,
        help="Comma-separated list of Azure AD group object IDs to filter devices by membership. "
             "Alternatively, use AZURE_GROUP_IDS environment variable."
    )
    args = parser.parse_args()
    
    # Validate configuration and initialize session/headers
    validate_and_init_config()
    
    # Use --groups argument if provided, otherwise fall back to environment variable
    group_ids = None
    if args.groups:
        group_ids = [gid.strip() for gid in args.groups.split(",") if gid.strip()]
    elif AZURE_GROUP_IDS:
        group_ids = AZURE_GROUP_IDS
    
    main(dry_run=args.dry_run, platform=args.platform, group_ids=group_ids)

