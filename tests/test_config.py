import pytest
import os
import importlib
import json
import logging # Import logging for caplog levels
from unittest.mock import patch, MagicMock

# Keep top-level imports minimal

# --- Dummy Data Definition (can stay at top level) ---
# These values are used to SET environment variables or MOCK return values
DUMMY_SECRET_VALUES = {
    "SLACK_BOT_TOKEN": "dummy-slack-bot-token",
    "SLACK_SIGNING_SECRET": "dummy-slack-signing-secret",
    "MISTRAL_API_KEY": "dummy-mistral-api-key",
    "OPENAI_API_KEY": "dummy-openai-api-key",
    "XERO_CLIENT_ID": "dummy-xero-client-id",
    "XERO_CLIENT_SECRET": "dummy-xero-client-secret",
    "XERO_REFRESH_TOKEN": "dummy-xero-refresh-token",
}

OTHER_DUMMY_VALUES = {
    "ALLOWED_CATEGORIES": ["TestCat1", "TestCat2"],
    "XERO_ACCOUNT_CODES": {"TestCat1": "100", "TestCat2": "200"},
    "OCR_SERVICE": "DummyOCR",
    "CATEGORIZATION_SERVICE": "DummyCategorizer",
    "SLACK_TARGET_CHANNEL_ID": "C12345",
    "GCP_PROJECT_ID": "dummy-gcp-project",
    "GCP_REGION": "dummy-region",
    "TEMP_STORAGE_BUCKET_NAME": "dummy-bucket",
    "XERO_TENANT_ID": "dummy-tenant-id"
}


@pytest.fixture(autouse=True)
def clear_secret_cache():
    """Ensure the secret cache is cleared before each test."""
    # Attempt to import config to access cache, handle potential errors
    try:
        import config
        config._secret_cache.clear()
    except (NameError, AttributeError):
        pass # Module might not be loaded or in a weird state
    yield
    # Clear again after test
    try:
        import config
        config._secret_cache.clear()
    except (NameError, AttributeError):
        pass

def test_load_config_from_env(mocker, capsys, caplog): # Add caplog
    """Tests loading configuration purely from environment variables (Secret Manager disabled)."""
    env_vars = {
        "SECRET_MANAGER_ENABLED": "false",
        "TEST_SKIP_GCP": "False",
        **DUMMY_SECRET_VALUES,
        "ALLOWED_CATEGORIES": ",".join(OTHER_DUMMY_VALUES["ALLOWED_CATEGORIES"]),
        "XERO_ACCOUNT_CODES": json.dumps(OTHER_DUMMY_VALUES["XERO_ACCOUNT_CODES"]),
        "OCR_SERVICE": OTHER_DUMMY_VALUES["OCR_SERVICE"],
        "CATEGORIZATION_SERVICE": OTHER_DUMMY_VALUES["CATEGORIZATION_SERVICE"],
        "SLACK_TARGET_CHANNEL_ID": OTHER_DUMMY_VALUES["SLACK_TARGET_CHANNEL_ID"],
        "GCP_PROJECT_ID": OTHER_DUMMY_VALUES["GCP_PROJECT_ID"],
        "GCP_REGION": OTHER_DUMMY_VALUES["GCP_REGION"],
        "TEMP_STORAGE_BUCKET_NAME": OTHER_DUMMY_VALUES["TEMP_STORAGE_BUCKET_NAME"],
        "XERO_TENANT_ID": OTHER_DUMMY_VALUES["XERO_TENANT_ID"],
    }
    with patch.dict(os.environ, env_vars, clear=True):
        # Import config *after* setting env vars and reload if necessary
        import config
        importlib.reload(config) # Reload because SECRET_MANAGER_ENABLED is module level
        
        logging.getLogger().setLevel(logging.INFO) # Ensure INFO logs are processed
        test_settings = config.Settings()

        # --- Assertions ---
        assert config.SECRET_MANAGER_ENABLED is False # Check module-level constant
        assert test_settings.SLACK_BOT_TOKEN == DUMMY_SECRET_VALUES["SLACK_BOT_TOKEN"]
        assert test_settings.SLACK_SIGNING_SECRET == DUMMY_SECRET_VALUES["SLACK_SIGNING_SECRET"]
        # ... assert other values match DUMMY_SECRET_VALUES and OTHER_DUMMY_VALUES ...
        assert test_settings.ALLOWED_CATEGORIES == OTHER_DUMMY_VALUES["ALLOWED_CATEGORIES"]
        assert test_settings.XERO_ACCOUNT_CODES == OTHER_DUMMY_VALUES["XERO_ACCOUNT_CODES"]
        assert test_settings.OCR_SERVICE == OTHER_DUMMY_VALUES["OCR_SERVICE"].lower()
        assert test_settings.CATEGORIZATION_SERVICE == OTHER_DUMMY_VALUES["CATEGORIZATION_SERVICE"].lower()
        assert test_settings.SLACK_TARGET_CHANNEL_ID == OTHER_DUMMY_VALUES["SLACK_TARGET_CHANNEL_ID"]
        assert test_settings.GCP_PROJECT_ID == OTHER_DUMMY_VALUES["GCP_PROJECT_ID"]
        assert test_settings.GCP_REGION == OTHER_DUMMY_VALUES["GCP_REGION"]
        assert test_settings.TEMP_STORAGE_BUCKET_NAME == OTHER_DUMMY_VALUES["TEMP_STORAGE_BUCKET_NAME"]
        assert test_settings.XERO_TENANT_ID == OTHER_DUMMY_VALUES["XERO_TENANT_ID"]


        # Check logs using caplog
        assert "Using Environment Variables for secrets." in caplog.text
        assert "Using Google Secret Manager." not in caplog.text

def test_load_config_from_secret_manager(mocker, caplog): # Use caplog
    """Tests loading config using mocked Secret Manager (by patching get_secret)."""
    
    env_vars = {
        "SECRET_MANAGER_ENABLED": "true",
        "TEST_SKIP_GCP": "True", 
        "GCP_PROJECT_ID": OTHER_DUMMY_VALUES["GCP_PROJECT_ID"],
        "GCP_REGION": OTHER_DUMMY_VALUES["GCP_REGION"], 
        "OCR_SERVICE": OTHER_DUMMY_VALUES["OCR_SERVICE"],
        "CATEGORIZATION_SERVICE": OTHER_DUMMY_VALUES["CATEGORIZATION_SERVICE"],
        "ALLOWED_CATEGORIES": ",".join(OTHER_DUMMY_VALUES["ALLOWED_CATEGORIES"]),
        "XERO_ACCOUNT_CODES": json.dumps(OTHER_DUMMY_VALUES["XERO_ACCOUNT_CODES"]),
        "SLACK_TARGET_CHANNEL_ID": OTHER_DUMMY_VALUES["SLACK_TARGET_CHANNEL_ID"],
        "TEMP_STORAGE_BUCKET_NAME": OTHER_DUMMY_VALUES["TEMP_STORAGE_BUCKET_NAME"],
        "XERO_TENANT_ID": OTHER_DUMMY_VALUES["XERO_TENANT_ID"],
    }

    with patch.dict(os.environ, env_vars, clear=True):
        caplog.set_level(logging.INFO) # Explicitly set caplog level
        import config
        importlib.reload(config)
        
        # --- Setup Mock for get_secret using actual constant names ---
        dummy_secrets_dict = {
            config.SLACK_BOT_TOKEN_SECRET_NAME: DUMMY_SECRET_VALUES["SLACK_BOT_TOKEN"],
            config.SLACK_SIGNING_SECRET_SECRET_NAME: DUMMY_SECRET_VALUES["SLACK_SIGNING_SECRET"],
            config.MISTRAL_API_KEY_SECRET_NAME: DUMMY_SECRET_VALUES["MISTRAL_API_KEY"],
            config.OPENAI_API_KEY_SECRET_NAME: DUMMY_SECRET_VALUES["OPENAI_API_KEY"],
            config.XERO_CLIENT_ID_SECRET_NAME: DUMMY_SECRET_VALUES["XERO_CLIENT_ID"],
            config.XERO_CLIENT_SECRET_SECRET_NAME: DUMMY_SECRET_VALUES["XERO_CLIENT_SECRET"],
            config.XERO_REFRESH_TOKEN_SECRET_NAME: DUMMY_SECRET_VALUES["XERO_REFRESH_TOKEN"],
        }
        def get_secret_side_effect(secret_name, project_id=None):
            return dummy_secrets_dict.get(secret_name, None)

        # Patch the get_secret function *within the config module*
        with patch('config.get_secret', side_effect=get_secret_side_effect) as mock_get_secret:
            test_settings = config.Settings()
            
            # --- Assertions ---
            assert config.SECRET_MANAGER_ENABLED is True
            assert test_settings.SLACK_BOT_TOKEN == DUMMY_SECRET_VALUES["SLACK_BOT_TOKEN"]
            # ... assert other secrets ...
            assert test_settings.SLACK_SIGNING_SECRET == DUMMY_SECRET_VALUES["SLACK_SIGNING_SECRET"]
            assert test_settings.MISTRAL_API_KEY == DUMMY_SECRET_VALUES["MISTRAL_API_KEY"]
            assert test_settings.OPENAI_API_KEY == DUMMY_SECRET_VALUES["OPENAI_API_KEY"]
            assert test_settings.XERO_CLIENT_ID == DUMMY_SECRET_VALUES["XERO_CLIENT_ID"]
            assert test_settings.XERO_CLIENT_SECRET == DUMMY_SECRET_VALUES["XERO_CLIENT_SECRET"]
            assert test_settings.XERO_REFRESH_TOKEN == DUMMY_SECRET_VALUES["XERO_REFRESH_TOKEN"]
            # ... assert non-secrets ...
            assert test_settings.ALLOWED_CATEGORIES == OTHER_DUMMY_VALUES["ALLOWED_CATEGORIES"]
            assert test_settings.XERO_ACCOUNT_CODES == OTHER_DUMMY_VALUES["XERO_ACCOUNT_CODES"]
            assert test_settings.OCR_SERVICE == OTHER_DUMMY_VALUES["OCR_SERVICE"].lower()
            assert test_settings.CATEGORIZATION_SERVICE == OTHER_DUMMY_VALUES["CATEGORIZATION_SERVICE"].lower()
            assert test_settings.SLACK_TARGET_CHANNEL_ID == OTHER_DUMMY_VALUES["SLACK_TARGET_CHANNEL_ID"]
            assert test_settings.GCP_PROJECT_ID == OTHER_DUMMY_VALUES["GCP_PROJECT_ID"]
            assert test_settings.GCP_REGION == OTHER_DUMMY_VALUES["GCP_REGION"]
            assert test_settings.TEMP_STORAGE_BUCKET_NAME == OTHER_DUMMY_VALUES["TEMP_STORAGE_BUCKET_NAME"]
            assert test_settings.XERO_TENANT_ID == OTHER_DUMMY_VALUES["XERO_TENANT_ID"]

            # Ensure mock was called and correct logs appear
            mock_get_secret.assert_called()
            print(f"Captured logs (SM test): {caplog.text}")
            core_info_message = "Using Google Secret Manager."
            info_log_found = any(
                record.levelno == logging.INFO and core_info_message in record.getMessage()
                for record in caplog.records
            )
            assert info_log_found, f"Expected INFO log containing '{core_info_message}' not found in logs:\n{caplog.text}"

            assert "WARNING: Secret/Environment variable" not in caplog.text
            assert "CRITICAL: Missing required configuration(s)" not in caplog.text

def test_missing_required_secret_name(mocker, caplog): # Use caplog
    """Tests that missing a required secret when SM is enabled logs correctly."""
    caplog.set_level(logging.WARNING) # Ensure WARNING level logs are captured
    
    # Define env vars first
    env_vars = {
        "SECRET_MANAGER_ENABLED": "true",
        "TEST_SKIP_GCP": "True",
        "GCP_PROJECT_ID": OTHER_DUMMY_VALUES["GCP_PROJECT_ID"],
        "OCR_SERVICE": OTHER_DUMMY_VALUES["OCR_SERVICE"],
        "CATEGORIZATION_SERVICE": OTHER_DUMMY_VALUES["CATEGORIZATION_SERVICE"],
        "ALLOWED_CATEGORIES": ",".join(OTHER_DUMMY_VALUES["ALLOWED_CATEGORIES"]),
        "XERO_ACCOUNT_CODES": json.dumps(OTHER_DUMMY_VALUES["XERO_ACCOUNT_CODES"]),
        "SLACK_TARGET_CHANNEL_ID": OTHER_DUMMY_VALUES["SLACK_TARGET_CHANNEL_ID"],
        "TEMP_STORAGE_BUCKET_NAME": OTHER_DUMMY_VALUES["TEMP_STORAGE_BUCKET_NAME"],
        "GCP_REGION": OTHER_DUMMY_VALUES["GCP_REGION"],
        "XERO_TENANT_ID": OTHER_DUMMY_VALUES["XERO_TENANT_ID"],
        # DO NOT PROVIDE ENV VAR FOR SLACK_BOT_TOKEN HERE
    }

    with patch.dict(os.environ, env_vars, clear=True):
        caplog.set_level(logging.INFO) # Explicitly set caplog level for INFO and WARNING
        import config
        importlib.reload(config)

        # --- Setup Mock for get_secret --- 
        missing_secret_key = "SLACK_BOT_TOKEN" # Key used in REQUIRED_CONFIG map
        missing_secret_name_const = config.SLACK_BOT_TOKEN_SECRET_NAME # Actual secret name constant
        
        dummy_secrets_dict = {
            config.SLACK_SIGNING_SECRET_SECRET_NAME: DUMMY_SECRET_VALUES["SLACK_SIGNING_SECRET"],
            config.MISTRAL_API_KEY_SECRET_NAME: DUMMY_SECRET_VALUES["MISTRAL_API_KEY"],
            config.OPENAI_API_KEY_SECRET_NAME: DUMMY_SECRET_VALUES["OPENAI_API_KEY"],
            config.XERO_CLIENT_ID_SECRET_NAME: DUMMY_SECRET_VALUES["XERO_CLIENT_ID"],
            config.XERO_CLIENT_SECRET_SECRET_NAME: DUMMY_SECRET_VALUES["XERO_CLIENT_SECRET"],
            config.XERO_REFRESH_TOKEN_SECRET_NAME: DUMMY_SECRET_VALUES["XERO_REFRESH_TOKEN"],
        }
        def get_secret_side_effect(secret_name, project_id=None):
            if secret_name == missing_secret_name_const:
                 logging.warning(f"Secret/Environment variable '{secret_name}' not found.") # Add log here
                 return None # Mock SM miss
            return dummy_secrets_dict.get(secret_name)

        with patch('config.get_secret', side_effect=get_secret_side_effect) as mock_get_secret:
            test_settings = config.Settings()

            # --- Assertions ---
            assert test_settings.SLACK_BOT_TOKEN is None
            assert test_settings.SLACK_SIGNING_SECRET == DUMMY_SECRET_VALUES["SLACK_SIGNING_SECRET"]

            print(f"Captured logs (Missing Secret test):\n{caplog.text}")
            core_warning_message = f"Secret/Environment variable '{missing_secret_name_const}' not found."
            warning_log_found = any(
                record.levelno == logging.WARNING and core_warning_message in record.getMessage()
                for record in caplog.records
            )
            assert warning_log_found, f"Expected WARNING log containing '{core_warning_message}' not found in logs:\n{caplog.text}"

            core_critical_message = f"Missing required configuration(s): {missing_secret_key}"
            critical_log_found = any(
                record.levelno == logging.CRITICAL and core_critical_message in record.getMessage()
                for record in caplog.records
            )
            assert critical_log_found, f"Expected CRITICAL log containing '{core_critical_message}' not found in logs:\n{caplog.text}"

            mock_get_secret.assert_called()


def test_invalid_xero_account_codes_json(caplog): # Use caplog
    """Tests that invalid JSON in XERO_ACCOUNT_CODES logs a warning and uses an empty dict."""
    caplog.set_level(logging.WARNING)
    invalid_json_string = "{\"key\": \"value\"" # Missing closing brace
    
    # Define env vars first
    env_vars = {
        "SECRET_MANAGER_ENABLED": "false", 
        "XERO_ACCOUNT_CODES": invalid_json_string,
        # Add minimal other required env vars from DUMMY_SECRET_VALUES & OTHER_DUMMY_VALUES
        **DUMMY_SECRET_VALUES, 
        "SLACK_TARGET_CHANNEL_ID": OTHER_DUMMY_VALUES["SLACK_TARGET_CHANNEL_ID"],
        "TEMP_STORAGE_BUCKET_NAME": OTHER_DUMMY_VALUES["TEMP_STORAGE_BUCKET_NAME"],
        "GCP_PROJECT_ID": OTHER_DUMMY_VALUES["GCP_PROJECT_ID"],
    }

    with patch.dict(os.environ, env_vars, clear=True):
        import config 
        importlib.reload(config)
        
        logging.getLogger().setLevel(logging.WARNING) # Ensure WARNING logs are processed
        test_settings = config.Settings()

        assert test_settings.XERO_ACCOUNT_CODES == {}

        print(f"Captured logs (Invalid JSON test):\n{caplog.text}")
        # Looser check for the core warning message content
        expected_warning_substring = f"Failed to parse XERO_ACCOUNT_CODES JSON: {invalid_json_string}. Using empty map."
        assert expected_warning_substring in caplog.text
        assert any(record.levelno == logging.WARNING and expected_warning_substring in record.message for record in caplog.records)
