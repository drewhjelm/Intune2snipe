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

# Optional: Load .env file if present (for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()  # Load .env file from current directory
except ImportError:
    pass  # python-dotenv not installed, skip

# ─── CONSTANTS ──────────────────────────────────────────────────────────────────
# HTTP timeout: (connect timeout, read timeout) in seconds
DEFAULT_TIMEOUT = (3.05, 30)
# Maximum length of response body to include in error logs (to prevent log spam)
MAX_RESPONSE_LOG_LENGTH = 200
SCOPE = ["https://graph.microsoft.com/.default"]
# UUID pattern for validating Azure AD object IDs
UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)
# ────────────────────────────────────────────────────────────────────────────────

# Configure logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

def sanitize_log(value, max_length=200):
    """Sanitize a value for safe logging by removing newlines and control characters."""
    if value is None:
        return "None"
    s = str(value)[:max_length]
    # Remove newlines and control characters that could enable log injection
    s = re.sub(r'[\r\n\x00-\x1f\x7f]', '', s)
    return s

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

def load_from_aws_secrets_manager(secret_name, region_name="us-east-1"):
    """
    Load secrets from AWS Secrets Manager.
    Returns a dictionary with the secret key-value pairs.
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        logger.error("boto3 is required for AWS Secrets Manager. Install with: pip install boto3")
        sys.exit(1)
    
    logger.info(f"Loading secrets from AWS Secrets Manager: {secret_name} (region: {region_name})")
    try:
        client = boto3.client('secretsmanager', region_name=region_name)
        response = client.get_secret_value(SecretId=secret_name)
        
        if 'SecretString' in response:
            return json.loads(response['SecretString'])
        else:
            logger.error("Binary secrets are not supported")
            sys.exit(1)
    except ClientError as e:
        logger.error(f"Failed to retrieve secret from AWS Secrets Manager: {e}")
        sys.exit(1)

def load_from_vault(vault_addr, vault_path, vault_token=None):
    """
    Load secrets from HashiCorp Vault.
    Returns a dictionary with the secret key-value pairs.
    """
    try:
        import hvac
    except ImportError:
        logger.error("hvac is required for HashiCorp Vault. Install with: pip install hvac")
        sys.exit(1)
    
    if vault_token is None:
        vault_token = os.getenv("VAULT_TOKEN")
    
    if not vault_token:
        logger.error("VAULT_TOKEN environment variable is required for HashiCorp Vault")
        sys.exit(1)
    
    logger.info(f"Loading secrets from HashiCorp Vault: {vault_addr} -> {vault_path}")
    try:
        client = hvac.Client(url=vault_addr, token=vault_token)
        
        if not client.is_authenticated():
            logger.error("Failed to authenticate with HashiCorp Vault")
            sys.exit(1)
        
        # Try KV v2 first, then fall back to v1
        try:
            response = client.secrets.kv.v2.read_secret_version(path=vault_path)
            return response['data']['data']
        except Exception as e:
            logger.info(f"KV v2 read failed, trying v1: {sanitize_log(e)}")
            response = client.secrets.kv.v1.read_secret(path=vault_path)
            return response['data']
    except Exception as e:
        logger.error(f"Failed to retrieve secret from HashiCorp Vault: {e}")
        sys.exit(1)

def load_from_azure_keyvault(vault_url, credential=None):
    """
    Load secrets from Azure Key Vault.
    Returns a dictionary with the secret key-value pairs.
    Secret names in Key Vault should match environment variable names.
    """
    try:
        from azure.keyvault.secrets import SecretClient
        from azure.identity import DefaultAzureCredential
    except ImportError:
        logger.error("azure-keyvault-secrets is required for Azure Key Vault. Install with: pip install azure-keyvault-secrets")
        sys.exit(1)
    
    if credential is None:
        credential = DefaultAzureCredential()
    
    logger.info(f"Loading secrets from Azure Key Vault: {vault_url}")
    try:
        client = SecretClient(vault_url=vault_url, credential=credential)
        
        # Define the secret names we need
        secret_names = [
            "AZURE-TENANT-ID",
            "AZURE-CLIENT-ID",
            "AZURE-COMBINED-CERT-KEY",    # Combined cert+key
            "AZURE-CERTIFICATE-PEM",      # Certificate only (alternative)
            "AZURE-PRIVATE-KEY-PEM",      # Private key only (alternative)
            "SNIPEIT-URL",
            "SNIPEIT-API-TOKEN",
            "SNIPEIT-DEFAULT-STATUS",
            "AZURE-GROUP-IDS"
        ]
        
        secrets = {}
        for secret_name in secret_names:
            try:
                secret = client.get_secret(secret_name)
                # Convert Key Vault naming (hyphens) to env var naming (underscores)
                env_var_name = secret_name.replace("-", "_")
                secrets[env_var_name] = secret.value
            except Exception:
                # Secret not found, skip it (might be optional)
                pass
        
        return secrets
    except Exception as e:
        logger.error(f"Failed to retrieve secrets from Azure Key Vault: {e}")
        sys.exit(1)

def apply_secrets_to_env(secrets):
    """
    Apply secrets from a dictionary to environment variables.
    Only sets variables that are not already set.
    """
    for key, value in secrets.items():
        if not os.getenv(key):
            os.environ[key] = str(value)

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
    # Support both combined and separate certificate/key variables
    cert_pem = os.getenv("AZURE_COMBINED_CERT_KEY")  # Combined cert+key
    cert_only = os.getenv("AZURE_CERTIFICATE_PEM")    # Certificate only
    key_only = os.getenv("AZURE_PRIVATE_KEY_PEM")     # Private key only
    
    # If separate cert and key are provided, combine them
    if cert_only and key_only:
        logger.info("Combining separate certificate and private key variables...")
        cert_pem = f"{cert_only.strip()}\n{key_only.strip()}"
    
    client_secret = os.getenv("AZURE_CLIENT_SECRET")
    
    # Check if cert PEM is valid (not empty and not a placeholder)
    has_valid_cert = cert_pem and not cert_pem.startswith("your-")
    # Check if secret is valid (not empty and not a placeholder)
    has_valid_secret = client_secret and not client_secret.startswith("your-")
    
    if not has_valid_cert and not has_valid_secret:
        logger.error("Authentication credentials missing:")
        logger.error("  Either AZURE_COMBINED_CERT_KEY (or AZURE_CERTIFICATE_PEM + AZURE_PRIVATE_KEY_PEM) or AZURE_CLIENT_SECRET must be set")
        logger.error("  (and not a placeholder like 'your-...')")
        sys.exit(1)
    
    # Read and validate configuration
    TENANT_ID = os.getenv("AZURE_TENANT_ID")
    CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
    CLIENT_SECRET = client_secret if has_valid_secret else None
    SNIPEIT_URL = os.getenv("SNIPEIT_URL")
    SNIPEIT_API_TOKEN = os.getenv("SNIPEIT_API_TOKEN")
    
    # Validate SNIPEIT_URL is a proper HTTPS URL (prevent SSRF)
    from urllib.parse import urlparse
    parsed_url = urlparse(SNIPEIT_URL)
    if parsed_url.scheme != "https":
        logger.error(f"SNIPEIT_URL must use HTTPS (got: {sanitize_log(parsed_url.scheme)})")
        sys.exit(1)
    if not parsed_url.hostname:
        logger.error("SNIPEIT_URL has no valid hostname")
        sys.exit(1)
    # Block private/metadata IPs
    import ipaddress
    try:
        ip = ipaddress.ip_address(parsed_url.hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            logger.error("SNIPEIT_URL must not point to a private/internal IP address")
            sys.exit(1)
    except ValueError:
        pass  # hostname is a domain name, not an IP - that's fine
    
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
    session.mount("https://", adapter)
    
    # Do NOT mount http:// adapter - all requests must use HTTPS
    
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
            
            # Get token using the same scope as MSAL flow
            # Note: get_token() expects scope strings, not a list
            token_result = credential.get_token("https://graph.microsoft.com/.default")
            access_token = token_result.token
            logger.info("Successfully acquired Microsoft Graph access token using certificate")
        except Exception as e:
            # Catch any authentication failure for fallback behavior
            # Log error with details for debugging
            logger.error("Certificate authentication failed")
            logger.error(f"Error details: {str(e)}")
            logger.error("Common causes:")
            logger.error("  1. AZURE_COMBINED_CERT_KEY must contain BOTH the certificate AND private key")
            logger.error("     OR use separate AZURE_CERTIFICATE_PEM and AZURE_PRIVATE_KEY_PEM variables")
            logger.error("  2. Invalid PEM format or missing BEGIN/END markers")
            logger.error("  3. Certificate expired or not yet valid")
            logger.error("  4. Wrong tenant ID or client ID")
            logger.error("  5. Certificate not uploaded to Azure AD App Registration")
            
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
        # Validate group_id is a valid UUID to prevent path injection
        if not UUID_PATTERN.match(group_id):
            logger.error(f"Invalid group ID format (expected UUID): {sanitize_log(group_id)}")
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


def find_existing_asset(serial):
    """
    Search for an existing asset in Snipe-IT by serial number.
    Returns the full asset dict if found, None otherwise.
    """
    if not serial or serial == "UNKNOWN":
        return None
    
    try:
        r = session.get(
            f"{SNIPEIT_URL}/hardware",
            headers=headers_snipeit,
            params={"search": serial, "limit": 5},
            timeout=DEFAULT_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()
        
        if data.get("total") > 0 and data.get("rows"):
            # Check if the serial matches exactly (not just partial match)
            for asset in data["rows"]:
                if asset.get("serial") == serial:
                    return asset
        return None
    except Exception as e:
        logger.warning("Error searching for existing asset with serial %s: %s", sanitize_log(serial), e)
        return None


def asset_needs_update(existing_asset, payload):
    """
    Compare existing Snipe-IT asset with the desired payload.
    Returns True if any field differs and an update is needed.
    """
    # Map payload fields to how Snipe-IT returns them in GET responses
    # Snipe-IT nests some fields as objects with 'id' keys
    checks = {
        "name": existing_asset.get("name"),
        "serial": existing_asset.get("serial"),
        "asset_tag": existing_asset.get("asset_tag"),
    }
    
    for field, existing_val in checks.items():
        if payload.get(field) != existing_val:
            return True
    
    # Check nested ID fields (Snipe-IT returns these as {"id": N, "name": "..."})
    id_fields = {
        "manufacturer_id": existing_asset.get("manufacturer", {}),
        "model_id": existing_asset.get("model", {}),
        "status_id": existing_asset.get("status_label", {}),
    }
    
    for field, existing_obj in id_fields.items():
        existing_id = existing_obj.get("id") if isinstance(existing_obj, dict) else None
        if payload.get(field) != existing_id:
            return True
    
    return False


def send_to_snipeit(device, category_id, status_id, deployed_status_id, dry_run=False):
    raw_upn = device.get("userPrincipalName")
    upn = normalize_upn(raw_upn)
    snipe_user_id = get_snipeit_user_id(upn)

    # Use "Deployed" status if device has an assigned user, otherwise default status
    effective_status_id = deployed_status_id if snipe_user_id and deployed_status_id else status_id

    man_name = device.get("manufacturer")
    mod_number = device.get("model")
    man_id = get_or_create_manufacturer(man_name)
    mod_id = get_or_create_model(mod_number, man_id, category_id) if man_id else None

    if mod_id is None:
        logger.warning("Skipping device: model_id unavailable (device=%s)", sanitize_log(device.get('deviceName')))
        return

    serial = device.get("serialNumber") or "UNKNOWN"
    device_name = device.get("deviceName") or "Unknown Device"
    safe_name = sanitize_log(device_name)
    safe_serial = sanitize_log(serial)
    
    # Use serial number as asset tag (required by Snipe-IT and must be unique)
    # If no serial, use device name with timestamp to ensure uniqueness
    asset_tag = serial if serial != "UNKNOWN" else f"{device_name}-{device.get('id', 'NOID')}"
    
    payload = {
        "asset_tag": asset_tag,
        "name": device_name,
        "serial": serial,
        "manufacturer_id": man_id,
        "model_id": mod_id,
        "status_id": effective_status_id,
        "notes": f"Imported from Intune: {man_name} {mod_number}"
    }

    if dry_run:
        existing = find_existing_asset(serial)
        action = "UPDATE" if existing else "CREATE"
        logger.info("[DRY RUN] %s %s (serial=%s) → checkout to user_id %s", action, safe_name, safe_serial, snipe_user_id)
        return

    # Check if asset already exists
    existing_asset = find_existing_asset(serial)
    
    if existing_asset:
        existing_asset_id = existing_asset.get("id")
        
        # Check if anything actually changed
        if not asset_needs_update(existing_asset, payload):
            logger.info("Unchanged: %s → asset ID %s (skipping update)", safe_name, existing_asset_id)
            # Still check checkout status below
            asset_id = existing_asset_id
        else:
            # Update existing asset
            try:
                r = session.patch(
                    f"{SNIPEIT_URL}/hardware/{existing_asset_id}",
                    headers=headers_snipeit,
                    json=payload,
                    timeout=DEFAULT_TIMEOUT
                )
                resp = r.json()
            except requests.exceptions.Timeout:
                logger.error("Timeout updating asset for '%s'", safe_name)
                return
            except requests.exceptions.RequestException as e:
                logger.error("Error updating asset for '%s': %s", safe_name, e)
                return
            except ValueError:
                logger.error("Invalid JSON response when updating asset for '%s': status=%s", safe_name, r.status_code)
                return
            
            if r.status_code not in (200, 201) or resp.get("status") != "success":
                logger.error("Failed to update '%s': status=%s, response=%s", safe_name, r.status_code, r.text[:MAX_RESPONSE_LOG_LENGTH])
                return
            
            asset_id = existing_asset_id
            logger.info("Updated: %s → asset ID %s", safe_name, asset_id)
    else:
        # Create new asset
        try:
            r = session.post(
                f"{SNIPEIT_URL}/hardware",
                headers=headers_snipeit,
                json=payload,
                timeout=DEFAULT_TIMEOUT
            )
            resp = r.json()
        except requests.exceptions.Timeout:
            logger.error("Timeout creating asset for '%s'", safe_name)
            return
        except requests.exceptions.RequestException as e:
            logger.error("Error creating asset for '%s': %s", safe_name, e)
            return
        except ValueError:
            logger.error("Invalid JSON response when creating asset for '%s': status=%s", safe_name, r.status_code)
            return
        
        if r.status_code not in (200, 201) or resp.get("status") != "success":
            logger.error("Failed to create '%s': status=%s, response=%s", safe_name, r.status_code, r.text[:MAX_RESPONSE_LOG_LENGTH])
            return

        asset_id = resp["payload"]["id"]
        logger.info("Created: %s → asset ID %s", safe_name, asset_id)

    if snipe_user_id:
        # Check if asset is already checked out (to avoid "not available for checkout" errors)
        try:
            asset_info = session.get(
                f"{SNIPEIT_URL}/hardware/{asset_id}",
                headers=headers_snipeit,
                timeout=DEFAULT_TIMEOUT
            )
            asset_data = asset_info.json()
            assigned_to = asset_data.get("assigned_to")
            if assigned_to and assigned_to.get("id") == snipe_user_id:
                logger.info("Asset %s already checked out to user_id %s — skipping", asset_id, snipe_user_id)
                return
            elif assigned_to:
                logger.warning("Asset %s already checked out to user_id %s (expected %s) — skipping", asset_id, assigned_to.get("id"), snipe_user_id)
                return
        except Exception:
            pass  # If we can't check, attempt checkout anyway

        try:
            co = session.post(
                f"{SNIPEIT_URL}/hardware/{asset_id}/checkout",
                headers=headers_snipeit,
                json={"assigned_user": snipe_user_id, "checkout_to_type": "user"},
                timeout=DEFAULT_TIMEOUT
            )
            co_resp = co.json()
            if co.status_code in (200, 201) and co_resp.get("status") == "success":
                logger.info("Checked out asset %s to user_id %s", asset_id, snipe_user_id)
            else:
                logger.error("Checkout failed for asset %s: status=%s, response=%s", asset_id, co.status_code, co.text[:MAX_RESPONSE_LOG_LENGTH])
        except requests.exceptions.Timeout:
            logger.error("Timeout checking out asset %s", asset_id)
        except requests.exceptions.RequestException as e:
            logger.error("Error checking out asset %s: %s", asset_id, e)
        except ValueError:
            logger.error("Invalid JSON response when checking out asset %s", asset_id)


def main(dry_run, platform, group_ids=None, limit=None):
    devices = fetch_managed_devices(platform, group_ids=group_ids)
    filter_info = f"platform '{platform}'"
    if group_ids:
        filter_info += f" and {len(group_ids)} group(s)"
    logger.info(f"Found {len(devices)} Intune devices matching {filter_info}")
    if limit:
        logger.info(f"Limiting to first {limit} device(s)")
        devices = devices[:limit]
    category_id = get_or_create_category("Intune")
    status_id = get_status_id(DEFAULT_STATUS_NAME)
    if status_id is None:
        sys.exit(1)
    # Look up "Deployed" status for devices with assigned users
    deployed_status_id = get_status_id("Deployed")
    if deployed_status_id is None:
        logger.warning("'Deployed' status label not found in Snipe-IT — assigned devices will use '%s' instead", DEFAULT_STATUS_NAME)
        deployed_status_id = status_id
    logger.info(f"Using category_id={category_id}, status_id={status_id} (unassigned), deployed_status_id={deployed_status_id} (assigned)")
    for d in devices:
        send_to_snipeit(d, category_id=category_id, status_id=status_id, deployed_status_id=deployed_status_id, dry_run=dry_run)


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
    parser.add_argument(
        "--secret-store", type=str, default=None,
        choices=["aws-secrets-manager", "vault", "azure-keyvault"],
        help="Load secrets from an external secret store instead of environment variables"
    )
    parser.add_argument(
        "--secret-name", type=str, default=None,
        help="Secret name/path for the secret store (required when using --secret-store)"
    )
    parser.add_argument(
        "--aws-region", type=str, default="us-east-1",
        help="AWS region for Secrets Manager (default: us-east-1)"
    )
    parser.add_argument(
        "--vault-addr", type=str, default=None,
        help="HashiCorp Vault address (e.g., https://vault.example.com:8200)"
    )
    parser.add_argument(
        "--vault-path", type=str, default=None,
        help="HashiCorp Vault secret path (e.g., secret/intune2snipe)"
    )
    parser.add_argument(
        "--keyvault-url", type=str, default=None,
        help="Azure Key Vault URL (e.g., https://myvault.vault.azure.net/)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N devices (useful for testing)"
    )
    args = parser.parse_args()
    
    # Load secrets from external store if specified
    if args.secret_store:
        if args.secret_store == "aws-secrets-manager":
            if not args.secret_name:
                logger.error("--secret-name is required when using AWS Secrets Manager")
                sys.exit(1)
            secrets = load_from_aws_secrets_manager(args.secret_name, args.aws_region)
            apply_secrets_to_env(secrets)
        
        elif args.secret_store == "vault":
            vault_addr = args.vault_addr or os.getenv("VAULT_ADDR")
            vault_path = args.vault_path or args.secret_name
            
            if not vault_addr:
                logger.error("--vault-addr or VAULT_ADDR environment variable is required")
                sys.exit(1)
            if not vault_path:
                logger.error("--vault-path or --secret-name is required for HashiCorp Vault")
                sys.exit(1)
            
            secrets = load_from_vault(vault_addr, vault_path)
            apply_secrets_to_env(secrets)
        
        elif args.secret_store == "azure-keyvault":
            keyvault_url = args.keyvault_url or os.getenv("AZURE_KEYVAULT_URL")
            
            if not keyvault_url:
                logger.error("--keyvault-url or AZURE_KEYVAULT_URL environment variable is required")
                sys.exit(1)
            
            secrets = load_from_azure_keyvault(keyvault_url)
            apply_secrets_to_env(secrets)
    
    # Validate configuration and initialize session/headers
    validate_and_init_config()
    
    # Use --groups argument if provided, otherwise fall back to environment variable
    group_ids = None
    if args.groups:
        group_ids = [gid.strip() for gid in args.groups.split(",") if gid.strip()]
    elif AZURE_GROUP_IDS:
        group_ids = AZURE_GROUP_IDS
    
    main(dry_run=args.dry_run, platform=args.platform, group_ids=group_ids, limit=args.limit)

