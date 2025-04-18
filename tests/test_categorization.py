"""
Unit tests for the LLM-based Invoice Categorization service.
"""

import unittest
from unittest.mock import patch, MagicMock
import json
import openai # Import the openai library itself for error types

# Import the class and models to test
from services.categorization import InvoiceCategorizer, CategorizationResult
from services.ocr import ExtractedInvoiceData

# Dummy data for tests
ALLOWED_CATEGORIES = ["Software & Subscriptions", "Office Supplies", "Travel", "Marketing & Advertising"]
COMPANY_CONTEXT = "Test Company Context"
DUMMY_INVOICE_DATA = ExtractedInvoiceData.model_validate({
    "vendor": "Test Vendor",
    "total": 100.00,
    "date": "2024-01-01"
})

class TestInvoiceCategorizerLLM(unittest.TestCase):

    def _create_mock_openai_response(self, response_json: dict):
        """Helper to create a mock OpenAI completion object."""
        mock_completion = MagicMock()
        mock_message = MagicMock()
        mock_message.content = json.dumps(response_json)
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_completion.choices = [mock_choice]
        return mock_completion

    @patch('services.categorization.openai.OpenAI')
    @patch('services.categorization.settings')
    def test_categorize_successful_match(self, mock_settings, mock_openai_cls):
        """Test successful categorization with a matched category."""
        # Configure the mock_settings object BEFORE InvoiceCategorizer uses it
        mock_settings.CATEGORIZATION_PROVIDER = 'openai'
        mock_settings.OPENAI_API_KEY = 'fake-key'
        mock_settings.ALLOWED_CATEGORIES = ALLOWED_CATEGORIES
        mock_settings.COMPANY_CONTEXT = COMPANY_CONTEXT

        # Mock OpenAI Client and Response
        mock_openai_instance = MagicMock()
        response_payload = {
            "status": "matched",
            "assigned_category": "Software & Subscriptions",
            "suggested_new_category": None,
            "notes": "Matches software category."
        }
        mock_openai_instance.chat.completions.create.return_value = self._create_mock_openai_response(response_payload)
        mock_openai_cls.return_value = mock_openai_instance

        # Initialize categorizer (uses mocked settings and client)
        categorizer = InvoiceCategorizer()
        result = categorizer.categorize(DUMMY_INVOICE_DATA)

        # Assertions
        mock_openai_instance.chat.completions.create.assert_called_once()
        self.assertEqual(result.status, 'matched')
        self.assertEqual(result.assigned_category, 'Software & Subscriptions')
        self.assertIsNone(result.suggested_new_category)
        self.assertEqual(result.notes, "Matches software category.")

    @patch('services.categorization.openai.OpenAI')
    @patch('services.categorization.settings')
    def test_categorize_not_matched_no_suggestion(self, mock_settings, mock_openai_cls):
        """Test categorization when LLM cannot match and provides no suggestion."""
        # Configure the mock_settings object
        mock_settings.CATEGORIZATION_PROVIDER = 'openai'
        mock_settings.OPENAI_API_KEY = 'fake-key'
        mock_settings.ALLOWED_CATEGORIES = ALLOWED_CATEGORIES
        mock_settings.COMPANY_CONTEXT = COMPANY_CONTEXT

        mock_openai_instance = MagicMock()
        response_payload = {
            "status": "not_matched",
            "assigned_category": None,
            "suggested_new_category": None,
            "notes": "Could not match to any category."
        }
        mock_openai_instance.chat.completions.create.return_value = self._create_mock_openai_response(response_payload)
        mock_openai_cls.return_value = mock_openai_instance

        categorizer = InvoiceCategorizer()
        result = categorizer.categorize(DUMMY_INVOICE_DATA)

        self.assertEqual(result.status, 'not_matched')
        self.assertIsNone(result.assigned_category)
        self.assertIsNone(result.suggested_new_category)
        self.assertEqual(result.notes, "Could not match to any category.")

    @patch('services.categorization.openai.OpenAI')
    @patch('services.categorization.settings')
    def test_categorize_not_matched_with_suggestion(self, mock_settings, mock_openai_cls):
        """Test categorization when LLM cannot match but suggests a new category."""
        # Configure the mock_settings object
        mock_settings.CATEGORIZATION_PROVIDER = 'openai'
        mock_settings.OPENAI_API_KEY = 'fake-key'
        mock_settings.ALLOWED_CATEGORIES = ALLOWED_CATEGORIES
        mock_settings.COMPANY_CONTEXT = COMPANY_CONTEXT

        mock_openai_instance = MagicMock()
        response_payload = {
            "status": "not_matched",
            "assigned_category": None,
            "suggested_new_category": "Meals & Entertainment",
            "notes": "Appears to be a restaurant expense."
        }
        mock_openai_instance.chat.completions.create.return_value = self._create_mock_openai_response(response_payload)
        mock_openai_cls.return_value = mock_openai_instance

        categorizer = InvoiceCategorizer()
        result = categorizer.categorize(DUMMY_INVOICE_DATA)

        self.assertEqual(result.status, 'not_matched')
        self.assertIsNone(result.assigned_category)
        self.assertEqual(result.suggested_new_category, "Meals & Entertainment")
        self.assertEqual(result.notes, "Appears to be a restaurant expense.")

    @patch('services.categorization.openai.OpenAI')
    @patch('services.categorization.settings')
    def test_categorize_llm_suggests_invalid_category(self, mock_settings, mock_openai_cls):
        """Test when LLM returns status 'matched' but with a category not in the allowed list."""
        # Configure the mock_settings object
        mock_settings.CATEGORIZATION_PROVIDER = 'openai'
        mock_settings.OPENAI_API_KEY = 'fake-key'
        mock_settings.ALLOWED_CATEGORIES = ALLOWED_CATEGORIES
        mock_settings.COMPANY_CONTEXT = COMPANY_CONTEXT

        mock_openai_instance = MagicMock()
        invalid_category = "Invalid Category From LLM"
        response_payload = {
            "status": "matched",
            "assigned_category": invalid_category, # Not in ALLOWED_CATEGORIES
            "suggested_new_category": None,
            "notes": "LLM thinks it matched."
        }
        mock_openai_instance.chat.completions.create.return_value = self._create_mock_openai_response(response_payload)
        mock_openai_cls.return_value = mock_openai_instance

        categorizer = InvoiceCategorizer()
        result = categorizer.categorize(DUMMY_INVOICE_DATA)

        # Should be corrected to 'not_matched' by the validation logic
        self.assertEqual(result.status, 'not_matched')
        self.assertIsNone(result.assigned_category)
        self.assertIsNone(result.suggested_new_category) # Check if suggestion is captured later if needed
        expected_notes_substring = f"LLM suggested invalid category '{invalid_category}'. Original Notes: LLM thinks it matched."
        self.assertIn(expected_notes_substring, result.notes)

    @patch('services.categorization.openai.OpenAI')
    @patch('services.categorization.settings')
    def test_categorize_openai_api_error(self, mock_settings, mock_openai_cls):
        """Test handling of an OpenAI APIError."""
        # Configure the mock_settings object
        mock_settings.CATEGORIZATION_PROVIDER = 'openai'
        mock_settings.OPENAI_API_KEY = 'fake-key'
        mock_settings.ALLOWED_CATEGORIES = ALLOWED_CATEGORIES
        mock_settings.COMPANY_CONTEXT = COMPANY_CONTEXT

        mock_openai_instance = MagicMock()
        error_message = "Simulated API Error"
        mock_openai_instance.chat.completions.create.side_effect = openai.APIError(error_message, request=None, body=None)
        mock_openai_cls.return_value = mock_openai_instance

        categorizer = InvoiceCategorizer()
        result = categorizer.categorize(DUMMY_INVOICE_DATA)

        self.assertEqual(result.status, 'error')
        self.assertIsNone(result.assigned_category)
        self.assertIn(f"OpenAI API Error: {error_message}", result.notes)

    @patch('services.categorization.openai.OpenAI')
    @patch('services.categorization.settings')
    def test_categorize_invalid_json_response(self, mock_settings, mock_openai_cls):
        """Test handling when OpenAI returns non-JSON content."""
        # Configure the mock_settings object
        mock_settings.CATEGORIZATION_PROVIDER = 'openai'
        mock_settings.OPENAI_API_KEY = 'fake-key'
        mock_settings.ALLOWED_CATEGORIES = ALLOWED_CATEGORIES
        mock_settings.COMPANY_CONTEXT = COMPANY_CONTEXT

        mock_openai_instance = MagicMock()
        mock_completion = MagicMock()
        mock_message = MagicMock()
        mock_message.content = "This is not JSON { definitely not json" # Invalid JSON
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_completion.choices = [mock_choice]
        mock_openai_instance.chat.completions.create.return_value = mock_completion
        mock_openai_cls.return_value = mock_openai_instance

        categorizer = InvoiceCategorizer()
        result = categorizer.categorize(DUMMY_INVOICE_DATA)

        self.assertEqual(result.status, 'error')
        self.assertIn("LLM response was not valid JSON", result.notes)

    @patch('services.categorization.openai.OpenAI')
    @patch('services.categorization.settings')
    def test_categorize_pydantic_validation_error(self, mock_settings, mock_openai_cls):
        """Test handling when OpenAI returns JSON with incorrect structure/types."""
        # Configure the mock_settings object
        mock_settings.CATEGORIZATION_PROVIDER = 'openai'
        mock_settings.OPENAI_API_KEY = 'fake-key'
        mock_settings.ALLOWED_CATEGORIES = ALLOWED_CATEGORIES
        mock_settings.COMPANY_CONTEXT = COMPANY_CONTEXT

        mock_openai_instance = MagicMock()
        response_payload = {
            # Missing 'status' field required by CategorizationResult
            "assigned_category": "Software & Subscriptions",
            "notes": "Missing status"
        }
        mock_openai_instance.chat.completions.create.return_value = self._create_mock_openai_response(response_payload)
        mock_openai_cls.return_value = mock_openai_instance

        categorizer = InvoiceCategorizer()
        result = categorizer.categorize(DUMMY_INVOICE_DATA)

        self.assertEqual(result.status, 'error')
        self.assertIn("LLM response structure invalid", result.notes)

    @patch('services.categorization.settings')
    def test_categorize_initialization_failure_no_key(self, mock_settings):
        """Test categorization fails if OpenAI key is missing."""
        # Configure the mock_settings object
        mock_settings.CATEGORIZATION_PROVIDER = 'openai'
        mock_settings.OPENAI_API_KEY = None # No API Key
        # We also need ALLOWED_CATEGORIES and COMPANY_CONTEXT, even if API key is missing, 
        # because __init__ reads them before checking the key.
        mock_settings.ALLOWED_CATEGORIES = ALLOWED_CATEGORIES
        mock_settings.COMPANY_CONTEXT = COMPANY_CONTEXT

        # No need to mock OpenAI client as init should fail
        categorizer = InvoiceCategorizer()
        result = categorizer.categorize(DUMMY_INVOICE_DATA)

        self.assertEqual(result.status, 'error')
        self.assertIn("provider 'openai' not supported or not initialized", result.notes)

    @patch('services.categorization.settings')
    def test_categorize_unsupported_provider(self, mock_settings):
        """Test categorization fails gracefully if provider is not 'openai'."""
        # Configure the mock_settings object
        mock_settings.CATEGORIZATION_PROVIDER = 'mistral' # Unsupported
        mock_settings.OPENAI_API_KEY = 'fake-key'
        mock_settings.ALLOWED_CATEGORIES = ALLOWED_CATEGORIES
        mock_settings.COMPANY_CONTEXT = COMPANY_CONTEXT

        categorizer = InvoiceCategorizer()
        result = categorizer.categorize(DUMMY_INVOICE_DATA)

        self.assertEqual(result.status, 'error')
        self.assertIn("provider 'mistral' not supported or not initialized", result.notes)

if __name__ == '__main__':
    unittest.main()
