import os
from google.cloud import secretmanager
from dotenv import load_dotenv
import json
import logging

# Set up basic logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Load environment variables from .env file for local development
load_dotenv()

# --- Control flag for testing --- 
_UNDER_TEST_SKIP_GCP = os.environ.get('TEST_SKIP_GCP', 'False').lower() == 'true'

# --- Google Cloud Settings (Module Level - OK as they control get_secret behavior) ---
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
SECRET_MANAGER_ENABLED = os.getenv("SECRET_MANAGER_ENABLED", "false").lower() == "true"

# --- Secret Names (Constants) ---
SLACK_BOT_TOKEN_SECRET_NAME = "SLACK_BOT_TOKEN"
SLACK_SIGNING_SECRET_SECRET_NAME = "SLACK_SIGNING_SECRET"
MISTRAL_API_KEY_SECRET_NAME = "MISTRAL_API_KEY"
OPENAI_API_KEY_SECRET_NAME = "OPENAI_API_KEY"
XERO_CLIENT_ID_SECRET_NAME = "XERO_CLIENT_ID"
XERO_CLIENT_SECRET_SECRET_NAME = "XERO_CLIENT_SECRET"
XERO_REDIRECT_URI_SECRET_NAME = "XERO_REDIRECT_URI"
XERO_REFRESH_TOKEN_SECRET_NAME = "XERO_REFRESH_TOKEN"
XERO_TENANT_ID_SECRET_NAME = "XERO_TENANT_ID"
XERO_ACCOUNT_CODE_MAP_SECRET_NAME = "XERO_ACCOUNT_CODE_MAP"

# --- Helper Function to Get Secrets (Keep at module level) ---
_secret_cache = {} # Simple in-memory cache for secrets

def get_secret(secret_name: str, project_id: str = GCP_PROJECT_ID) -> str | None:
    """Retrieves a secret from Google Secret Manager or environment variables."""
    _secret_cache.clear() # Ensure cache is clear for each call in this pattern
    if secret_name in _secret_cache:
        return _secret_cache[secret_name]

    secret_value = None
    if SECRET_MANAGER_ENABLED:
        if not project_id:
            logging.warning("GCP_PROJECT_ID not set, cannot fetch from Secret Manager.")
            secret_value = os.getenv(secret_name) # Fallback to env var
        else:
            if not _UNDER_TEST_SKIP_GCP:
                try:
                    client = secretmanager.SecretManagerServiceClient()
                    secret_version_name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
                    response = client.access_secret_version(request={"name": secret_version_name})
                    secret_value = response.payload.data.decode("UTF-8")
                    logging.info(f"Successfully retrieved secret '{secret_name}' from Secret Manager.")
                except Exception as e:
                    logging.warning(f"Failed to retrieve secret '{secret_name}' from Secret Manager: {e}")
                    logging.warning("Falling back to environment variable.")
                    secret_value = os.getenv(secret_name)
            # else: If skipping GCP, secret_value remains None initially
            # We handle the fallback below
    else:
        # If Secret Manager is disabled, get directly from environment variables
        secret_value = os.getenv(secret_name)

    # Common fallback/caching logic
    if not secret_value:
        # If SM was enabled & skipped GCP, or SM disabled, or SM failed and fallback env var missing
        # Try getting from env var one last time (handles SM disabled case cleanly)
        secret_value = os.getenv(secret_name)

    if secret_value:
        _secret_cache[secret_name] = secret_value
    else:
         logging.warning(f"Secret/Environment variable '{secret_name}' not found.")

    return secret_value

# --- Settings Class --- 
class Settings:
    def __init__(self):
        # --- Google Cloud Settings ---
        self.GCP_PROJECT_ID = GCP_PROJECT_ID # Use module-level loaded value
        self.GCP_REGION = os.getenv("GCP_REGION", "us-central1") # Default region

        # --- Slack Settings ---
        self.SLACK_BOT_TOKEN = get_secret(SLACK_BOT_TOKEN_SECRET_NAME)
        self.SLACK_SIGNING_SECRET = get_secret(SLACK_SIGNING_SECRET_SECRET_NAME)
        self.SLACK_TARGET_CHANNEL_ID = os.getenv("SLACK_TARGET_CHANNEL_ID")

        # --- API Keys ---
        self.MISTRAL_API_KEY = get_secret(MISTRAL_API_KEY_SECRET_NAME)
        self.OPENAI_API_KEY = get_secret(OPENAI_API_KEY_SECRET_NAME)
        # --- Xero Credentials (Initial OAuth needs ID, Secret, Redirect, Scopes) ---
        self.XERO_CLIENT_ID = get_secret(XERO_CLIENT_ID_SECRET_NAME)
        self.XERO_CLIENT_SECRET = get_secret(XERO_CLIENT_SECRET_SECRET_NAME)
        # Redirect URI and Scopes might come from env var directly or secrets
        self.XERO_REDIRECT_URI = get_secret(XERO_REDIRECT_URI_SECRET_NAME) or os.getenv("XERO_REDIRECT_URI")
        self.XERO_SCOPES = os.getenv("XERO_SCOPES", "offline_access accounting.transactions accounting.contacts.read accounting.settings.read openid profile email")
        # Refresh token and Tenant ID are obtained *after* initial auth, load if available
        self.XERO_REFRESH_TOKEN = get_secret(XERO_REFRESH_TOKEN_SECRET_NAME)
        self.XERO_TENANT_ID = get_secret(XERO_TENANT_ID_SECRET_NAME)

        # --- Service Selection (Add back) ---
        self.OCR_SERVICE = os.getenv("OCR_SERVICE", "mistral").lower()
        self.CATEGORIZATION_SERVICE = os.getenv("CATEGORIZATION_SERVICE", "openai").lower()

        # --- Categorization Settings ---
        _allowed_cats_str = os.getenv("ALLOWED_CATEGORIES", '["Software & Subscriptions", "Office Supplies", "Travel", "Marketing & Advertising", "Meals & Entertainment", "Utilities", "Professional Services"]')
        try:
            self.ALLOWED_CATEGORIES = json.loads(_allowed_cats_str)
            if not isinstance(self.ALLOWED_CATEGORIES, list):
                logging.warning(f"ALLOWED_CATEGORIES was not a valid JSON list. Got: {_allowed_cats_str}. Using empty list.")
                self.ALLOWED_CATEGORIES = []
            # Ensure all items are strings
            self.ALLOWED_CATEGORIES = [str(item) for item in self.ALLOWED_CATEGORIES]
        except json.JSONDecodeError:
            logging.warning(f"Failed to parse ALLOWED_CATEGORIES JSON: {_allowed_cats_str}. Attempting comma-separated fallback.")
            # Fallback for simple comma-separated strings (optional, but might be useful)
            self.ALLOWED_CATEGORIES = [cat.strip() for cat in _allowed_cats_str.split(',') if cat.strip()]
            if not self.ALLOWED_CATEGORIES:
                 logging.warning(f"Could not parse ALLOWED_CATEGORIES as JSON or comma-separated. Using empty list.")

        # --- Company Context --- 
        self.COMPANY_CONTEXT = os.getenv("COMPANY_CONTEXT", "44pixels is a mobile app development studio focused on building utility apps. Key expense areas include software subscriptions, cloud services (AWS, GCP), and performance marketing (e.g., Facebook Ads, Google Ads).")

        # --- Storage Settings ---
        self.TEMP_STORAGE_BUCKET_NAME = os.getenv("TEMP_STORAGE_BUCKET_NAME", 
                                                 f"{self.GCP_PROJECT_ID}-invoices-temp" if self.GCP_PROJECT_ID else None)

        # --- Xero Account Code Map ---
        _xero_codes_json = get_secret(XERO_ACCOUNT_CODE_MAP_SECRET_NAME) or os.getenv("XERO_ACCOUNT_CODE_MAP", "{}")
        try:
            self.XERO_ACCOUNT_CODE_MAP = json.loads(_xero_codes_json)
            if not isinstance(self.XERO_ACCOUNT_CODE_MAP, dict):
                logging.warning(f"XERO_ACCOUNT_CODE_MAP was not a valid JSON dictionary. Got: {_xero_codes_json}. Using empty map.")
                self.XERO_ACCOUNT_CODE_MAP = {}
        except json.JSONDecodeError:
            logging.warning(f"Failed to parse XERO_ACCOUNT_CODE_MAP JSON: {_xero_codes_json}. Using empty map.")
            self.XERO_ACCOUNT_CODE_MAP = {}
            
        # --- Validation ---
        REQUIRED_CONFIG = {
            "GCP_PROJECT_ID": self.GCP_PROJECT_ID,
            "SLACK_BOT_TOKEN": self.SLACK_BOT_TOKEN,
            "SLACK_SIGNING_SECRET": self.SLACK_SIGNING_SECRET,
            "SLACK_TARGET_CHANNEL_ID": self.SLACK_TARGET_CHANNEL_ID,
            "TEMP_STORAGE_BUCKET_NAME": self.TEMP_STORAGE_BUCKET_NAME,
            "MISTRAL_API_KEY": self.MISTRAL_API_KEY,
            "OPENAI_API_KEY": self.OPENAI_API_KEY,
            "XERO_CLIENT_ID": self.XERO_CLIENT_ID, # Needed for OAuth flow
            "XERO_CLIENT_SECRET": self.XERO_CLIENT_SECRET, # Needed for OAuth flow
            "XERO_REDIRECT_URI": self.XERO_REDIRECT_URI, # Needed for OAuth flow
            "ALLOWED_CATEGORIES": self.ALLOWED_CATEGORIES,
            "COMPANY_CONTEXT": self.COMPANY_CONTEXT
            # XERO_REFRESH_TOKEN and XERO_TENANT_ID are not required initially
            # XERO_ACCOUNT_CODE_MAP is useful but might be empty initially
        }
        # COMPANY_CONTEXT is useful but not strictly required to run
        # Add other strictly required ones here
        if not SECRET_MANAGER_ENABLED:
            REQUIRED_CONFIG = {
                "SLACK_BOT_TOKEN",
                "SLACK_SIGNING_SECRET",
                "MISTRAL_API_KEY",
                "OPENAI_API_KEY",
                "ALLOWED_CATEGORIES",
                "COMPANY_CONTEXT",
                "XERO_CLIENT_ID", # Needed for OAuth flow
                "XERO_CLIENT_SECRET", # Needed for OAuth flow
                "XERO_REDIRECT_URI" # Needed for OAuth flow
            }
            missing_configs = {key for key in REQUIRED_CONFIG if not getattr(self, key)}
        else:
            missing_configs = [k for k, v in REQUIRED_CONFIG.items() if not v]
        if missing_configs:
            logging.critical(f"Missing required configuration(s): {', '.join(missing_configs)}")
        
        logging.info(f"Configuration loaded. OCR: {self.OCR_SERVICE}, Categorization: {self.CATEGORIZATION_SERVICE}")
        if SECRET_MANAGER_ENABLED:
            logging.info("Using Google Secret Manager.")
        else:
            logging.info("Using Environment Variables for secrets.")

# --- Instantiate Settings --- 
settings = Settings()
