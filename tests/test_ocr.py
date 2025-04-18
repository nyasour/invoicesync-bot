import pytest
from unittest.mock import patch, MagicMock
import json

from pydantic import ValidationError

from services.ocr import MistralOCR, ExtractedInvoiceData, LineItem
import config

# --- Test Data --- 
DUMMY_PDF_CONTENT = b"dummy pdf bytes"
DUMMY_FILENAME = "invoice.pdf"
DUMMY_API_KEY = "test-mistral-key"

# Sample extracted text (can be modified per test)
SAMPLE_EXTRACTED_TEXT = "Vendor: Test Vendor\nInvoice ID: INV-123\nDate: 2024-01-15\nTotal: 150.75\nItem 1: Product A - 100.50\nItem 2: Service B - 50.25"

# Sample successful Mistral API response content (JSON string)
SAMPLE_MISTRAL_RESPONSE_JSON = json.dumps({
    "vendor_name": "Test Vendor",
    "invoice_number": "INV-123",
    "issue_date": "2024-01-15",
    "total_amount": 150.75,
    "line_items": [
        {"description": "Product A", "amount": 100.50},
        {"description": "Service B", "amount": 50.25}
    ]
})

# Expected Pydantic object for the successful response
EXPECTED_INVOICE_DATA = ExtractedInvoiceData(
    vendor_name="Test Vendor",
    invoice_number="INV-123",
    issue_date="2024-01-15",
    total_amount=150.75,
    line_items=[
        LineItem(description="Product A", amount=100.50),
        LineItem(description="Service B", amount=50.25),
    ]
)

# --- Fixtures --- 
@pytest.fixture
def mistral_ocr_instance():
    """Provides a MistralOCR instance, mocking config loading to provide a dummy API key."""
    # Patch the Settings class instantiation within the scope of the fixture
    mock_settings = MagicMock(spec=config.Settings)
    mock_settings.MISTRAL_API_KEY = DUMMY_API_KEY
    
    with patch('config.Settings', return_value=mock_settings) as mock_settings_cls:
        ocr_service = MistralOCR() # Now __init__ will call the mocked Settings()
        yield ocr_service # Use yield if you need teardown, otherwise return is fine

# --- Test Cases --- 

def test_mistral_ocr_initialization_success(mistral_ocr_instance):
    """Test that MistralOCR initializes correctly with an API key."""
    assert mistral_ocr_instance is not None
    assert mistral_ocr_instance.client is not None
    # We can mock MistralClient instantiation if needed to check the key passed

def test_mistral_ocr_initialization_missing_key():
    """Test that MistralOCR raises ValueError if API key is missing in Settings."""
    # Patch Settings to simulate missing key
    mock_settings = MagicMock(spec=config.Settings)
    # Simulate AttributeError when accessing MISTRAL_API_KEY
    del mock_settings.MISTRAL_API_KEY # Or set it to None and check the error message
    
    with patch('config.Settings', return_value=mock_settings):
        with pytest.raises(ValueError, match="Mistral API key not found in configuration|Mistral API key is not configured"):
            MistralOCR()

@patch('services.ocr.PdfReader') # Mock PyPDF2 PdfReader
@patch('services.ocr.Mistral') # Mock Mistral class
@patch('config.Settings') # Also mock Settings here for consistency inside extract if needed
def test_extract_happy_path(mock_settings_cls, mock_mistral_cls, mock_pdf_reader_cls):
    """Test the full extract process with successful text extraction and API call."""
    # --- Mock PdfReader --- 
    mock_pdf_page = MagicMock()
    mock_pdf_page.extract_text.return_value = SAMPLE_EXTRACTED_TEXT
    mock_pdf_reader_instance = MagicMock()
    mock_pdf_reader_instance.pages = [mock_pdf_page]
    mock_pdf_reader_cls.return_value = mock_pdf_reader_instance
    
    # --- Configure the Mock Mistral instance returned by the class --- 
    mock_mistral_instance = MagicMock()
    mock_chat_message = MagicMock()
    mock_chat_message.message.content = SAMPLE_MISTRAL_RESPONSE_JSON
    mock_chat_response = MagicMock()
    mock_chat_response.choices = [mock_chat_message]
    mock_mistral_instance.chat.complete.return_value = mock_chat_response
    mock_mistral_cls.return_value = mock_mistral_instance # When Mistral() is called, return this configured mock

    # --- Re-initialize OCR instance to use the fully mocked Mistral --- 
    # We need to ensure the MistralOCR instance under test uses the mock we just configured.
    # The fixture `mistral_ocr_instance` might have already created an instance before this patch was fully set up.
    # It's safer to create the instance *after* setting up the mock return value.
    with patch('config.Settings', mock_settings_cls): # Ensure settings are patched during re-init
        ocr_service = MistralOCR()

    # --- Call the method under test --- 
    result = ocr_service.extract(DUMMY_PDF_CONTENT, DUMMY_FILENAME)

    # --- Assertions --- 
    assert isinstance(result, ExtractedInvoiceData)
    assert result == EXPECTED_INVOICE_DATA

    # Assert PdfReader was called correctly
    mock_pdf_reader_cls.assert_called_once()
    assert mock_pdf_reader_instance.pages[0].extract_text.call_count == 1

    # Assert MistralClient.chat was called correctly
    # Check the mock instance we configured
    assert mock_mistral_instance.chat.complete.call_count == 1 
    call_args, call_kwargs = mock_mistral_instance.chat.complete.call_args
    assert call_kwargs['model'] == 'mistral-large-latest' # Or whichever model is used
    # Check that the prompt contains the extracted text using dictionary access
    assert SAMPLE_EXTRACTED_TEXT in call_kwargs['messages'][0]['content']

@patch('services.ocr.PdfReader') # Mock PyPDF2 PdfReader
@patch('services.ocr.Mistral') # Mock Mistral class
@patch('config.Settings')
def test_extract_pdf_text_extraction_failure(mock_settings_cls, mock_mistral_cls, mock_pdf_reader_cls):
    """Test extract when PdfReader fails to extract text."""
    # --- Mock PdfReader to return no text --- 
    mock_pdf_page = MagicMock()
    mock_pdf_page.extract_text.return_value = "" # Simulate no text extracted
    mock_pdf_reader_instance = MagicMock()
    mock_pdf_reader_instance.pages = [mock_pdf_page]
    mock_pdf_reader_cls.return_value = mock_pdf_reader_instance
    
    # --- Configure Mock Mistral instance (though it shouldn't be called) ---
    mock_mistral_instance = MagicMock()
    mock_mistral_cls.return_value = mock_mistral_instance
    
    # --- Re-initialize OCR instance --- 
    with patch('config.Settings', mock_settings_cls):
        ocr_service = MistralOCR()
        
    # --- Call the method under test --- 
    result = ocr_service.extract(DUMMY_PDF_CONTENT, DUMMY_FILENAME)

    # --- Assertions --- 
    assert result is None
    mock_pdf_reader_cls.assert_called_once()
    # Check that Mistral's chat.complete was not called
    mock_mistral_instance.chat.complete.assert_not_called()

@patch('services.ocr.PdfReader') # Mock PyPDF2 PdfReader
@patch('services.ocr.Mistral') # Mock Mistral class
@patch('config.Settings')
def test_extract_mistral_api_error(mock_settings_cls, mock_mistral_cls, mock_pdf_reader_cls):
    """Test extract when the Mistral API call raises an exception."""
    # --- Mock PdfReader --- 
    mock_pdf_page = MagicMock()
    mock_pdf_page.extract_text.return_value = SAMPLE_EXTRACTED_TEXT
    mock_pdf_reader_instance = MagicMock()
    mock_pdf_reader_instance.pages = [mock_pdf_page]
    mock_pdf_reader_cls.return_value = mock_pdf_reader_instance
    
    # --- Configure Mock Mistral instance to raise error --- 
    mock_mistral_instance = MagicMock()
    mock_mistral_instance.chat.complete.side_effect = Exception("Mistral API Down")
    mock_mistral_cls.return_value = mock_mistral_instance

    # --- Re-initialize OCR instance --- 
    with patch('config.Settings', mock_settings_cls):
        ocr_service = MistralOCR()

    # --- Call the method under test --- 
    result = ocr_service.extract(DUMMY_PDF_CONTENT, DUMMY_FILENAME)

    # --- Assertions --- 
    assert result is None
    mock_pdf_reader_cls.assert_called_once()
    mock_mistral_instance.chat.complete.assert_called_once() # API was called

@patch('services.ocr.PdfReader') # Mock PyPDF2 PdfReader
@patch('services.ocr.Mistral') # Mock Mistral class
@patch('config.Settings')
def test_extract_mistral_empty_response(mock_settings_cls, mock_mistral_cls, mock_pdf_reader_cls):
    """Test extract when the Mistral API returns an empty or non-standard response."""
    # --- Mock PdfReader --- 
    mock_pdf_page = MagicMock()
    mock_pdf_page.extract_text.return_value = SAMPLE_EXTRACTED_TEXT
    mock_pdf_reader_instance = MagicMock()
    mock_pdf_reader_instance.pages = [mock_pdf_page]
    mock_pdf_reader_cls.return_value = mock_pdf_reader_instance
    
    # --- Configure Mock Mistral instance with empty response --- 
    mock_mistral_instance = MagicMock()
    mock_chat_response = MagicMock()
    mock_chat_response.choices = [] # No choices in response
    mock_mistral_instance.chat.complete.return_value = mock_chat_response
    mock_mistral_cls.return_value = mock_mistral_instance

    # --- Re-initialize OCR instance --- 
    with patch('config.Settings', mock_settings_cls):
        ocr_service = MistralOCR()

    # --- Call the method under test --- 
    result = ocr_service.extract(DUMMY_PDF_CONTENT, DUMMY_FILENAME)

    # --- Assertions --- 
    assert result is None
    mock_pdf_reader_cls.assert_called_once()
    mock_mistral_instance.chat.complete.assert_called_once() 

@patch('services.ocr.PdfReader') # Mock PyPDF2 PdfReader
@patch('services.ocr.Mistral') # Mock Mistral class
@patch('config.Settings')
def test_extract_mistral_invalid_json_response(mock_settings_cls, mock_mistral_cls, mock_pdf_reader_cls):
    """Test extract when the Mistral API returns invalid JSON."""
    # --- Mock PdfReader --- 
    mock_pdf_page = MagicMock()
    mock_pdf_page.extract_text.return_value = SAMPLE_EXTRACTED_TEXT
    mock_pdf_reader_instance = MagicMock()
    mock_pdf_reader_instance.pages = [mock_pdf_page]
    mock_pdf_reader_cls.return_value = mock_pdf_reader_instance
    
    # --- Configure Mock Mistral instance with invalid JSON content --- 
    mock_mistral_instance = MagicMock()
    mock_chat_message = MagicMock()
    mock_chat_message.message.content = "This is not JSON { definitely not }"
    mock_chat_response = MagicMock()
    mock_chat_response.choices = [mock_chat_message]
    mock_mistral_instance.chat.complete.return_value = mock_chat_response
    mock_mistral_cls.return_value = mock_mistral_instance

    # --- Re-initialize OCR instance --- 
    with patch('config.Settings', mock_settings_cls):
        ocr_service = MistralOCR()

    # --- Call the method under test --- 
    result = ocr_service.extract(DUMMY_PDF_CONTENT, DUMMY_FILENAME)

    # --- Assertions --- 
    assert result is None # Should fail parsing
    mock_pdf_reader_cls.assert_called_once()
    mock_mistral_instance.chat.complete.assert_called_once() 

@patch('services.ocr.PdfReader') # Mock PyPDF2 PdfReader
@patch('services.ocr.Mistral') # Mock Mistral class
@patch('config.Settings')
def test_extract_mistral_validation_error(mock_settings_cls, mock_mistral_cls, mock_pdf_reader_cls):
    """Test extract when Mistral response is JSON but fails Pydantic validation."""
    # --- Mock PdfReader --- 
    mock_pdf_page = MagicMock()
    mock_pdf_page.extract_text.return_value = SAMPLE_EXTRACTED_TEXT
    mock_pdf_reader_instance = MagicMock()
    mock_pdf_reader_instance.pages = [mock_pdf_page]
    mock_pdf_reader_cls.return_value = mock_pdf_reader_instance
    
    # --- Configure Mock Mistral instance with validation-failing JSON --- 
    mock_mistral_instance = MagicMock()
    # Pydantic model expects total_amount as float, provide string using alias 'total'
    invalid_json_content = json.dumps({
        "vendor": "Test Vendor",         # Use alias 'vendor'
        "total": "one hundred fifty",    # Use alias 'total' (Incorrect type)
        "date": "2024-01-15"           # Use alias 'date'
        # Missing invoice_id (alias for invoice_number), line_items are optional
    })
    mock_chat_message = MagicMock()
    mock_chat_message.message.content = invalid_json_content
    mock_chat_response = MagicMock()
    mock_chat_response.choices = [mock_chat_message]
    mock_mistral_instance.chat.complete.return_value = mock_chat_response
    mock_mistral_cls.return_value = mock_mistral_instance

    # --- Re-initialize OCR instance --- 
    with patch('config.Settings', mock_settings_cls):
        ocr_service = MistralOCR()

    # --- Call the method under test --- 
    result = ocr_service.extract(DUMMY_PDF_CONTENT, DUMMY_FILENAME)

    # --- Assertions --- 
    assert result is None
    mock_pdf_reader_cls.assert_called_once()
    mock_mistral_instance.chat.complete.assert_called_once()

# TODO: Add tests for _extract_text_from_pdf specifically (e.g., multiple pages, empty PDF)
# TODO: Add tests for _parse_response specifically (e.g., null values in JSON)
