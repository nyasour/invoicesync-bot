# services/ocr.py
import logging
import re
import io # Needed for PyPDF2
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, ValidationError, AliasChoices
from mistralai import Mistral # Corrected import path
from PyPDF2 import PdfReader # Import PdfReader

import config # Use loaded config

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO) # Basic logging config for now

# --- Pydantic Models for Structured Output ---
class LineItem(BaseModel):
    description: Optional[str] = None
    amount: Optional[float] = None
    quantity: Optional[int] = None # Optional
    unit_price: Optional[float] = None # Added unit_price

class ExtractedInvoiceData(BaseModel):
    vendor_name: str # Changed from Optional to required based on typical need
    vendor_address: Optional[str] = None # Added vendor address
    invoice_number: Optional[str] = Field(None, validation_alias=AliasChoices('invoice_number', 'invoice_id', 'invoice #')) 
    issue_date: Optional[str] = None 
    due_date: Optional[str] = None # Added due date
    total_amount: float # Changed from Optional to required
    currency: Optional[str] = None # Added currency
    line_items: List[LineItem] # List of line items

# --- OCR Service Interface ---
class OCRService(ABC):
    @abstractmethod
    def extract(self, file_content: bytes, filename: str) -> Optional[ExtractedInvoiceData]:
        """
        Extracts structured data from invoice file content.

        Args:
            file_content: The byte content of the invoice file (e.g., PDF).
            filename: The original filename, potentially useful for some services.

        Returns:
            An ExtractedInvoiceData object or None if extraction fails.
        """
        pass

# --- Mistral OCR Implementation ---
class MistralOCR(OCRService):
    def __init__(self, api_key: Optional[str] = None):
        # Fetch API key from config if not provided explicitly
        effective_api_key = api_key
        if effective_api_key is None:
            try:
                # Instantiate settings to access the value
                settings = config.Settings()
                effective_api_key = settings.MISTRAL_API_KEY
            except ValidationError:
                 logger.critical("Configuration validation failed. Cannot determine Mistral API key.")
                 raise ValueError("Configuration validation failed or Mistral API key missing.")
            except AttributeError:
                logger.critical("'MISTRAL_API_KEY' not found in configuration settings.")
                raise ValueError("Mistral API key not found in configuration.")

        if not effective_api_key:
            # Log critical error and raise exception if API key is missing
            logger.critical("Mistral API key is not configured. OCR service cannot be initialized.")
            raise ValueError("Mistral API key is not configured.")
            
        self.client = Mistral(api_key=effective_api_key) # Use the determined key
        # Define the expected JSON structure for Mistral
        self.extraction_prompt_template = (
            "Extract the key information from the following invoice text and provide it ONLY as a valid JSON object. "
            "Ensure all monetary values are represented as numbers (float or int), not strings. \n"
            "Use the EXACT JSON key names specified below (e.g., 'vendor_name', 'invoice_number', 'issue_date').\n"
            "Required fields:\n"
            "- vendor_name (string, required): The name of the company issuing the invoice.\n"
            "- vendor_address (string, optional): The full address of the vendor.\n"
            "- invoice_number (string, optional): The unique identifier for the invoice.\n"
            "- issue_date (string, optional): The date the invoice was issued (YYYY-MM-DD format preferred).\n"
            "- due_date (string, optional): The date the invoice payment is due (YYYY-MM-DD format preferred).\n"
            "- total_amount (float, required): The final total amount due on the invoice.\n"
            "- currency (string, optional): The currency code (e.g., USD, GBP, EUR).\n"
            "- line_items (array of objects, required): A list of items or services billed. Each object MUST contain:\n"
            "    - description (string, required): Description of the line item.\n"
            "    - quantity (float, optional): The quantity of the item.\n"
            "    - unit_price (float, optional): The price per unit of the item.\n"
            "    - amount (float, required): The total amount for the line item (quantity * unit_price).\n\n"
            "Example JSON structure (use these keys):\n"
            "```json\n"
            "{{\n"
            "  \"vendor_name\": \"Example Corp\",\n"
            "  \"vendor_address\": \"123 Example St, Example City, EX 12345\",\n"
            "  \"invoice_number\": \"INV-123\",\n"
            "  \"issue_date\": \"2024-01-15\",\n"
            "  \"due_date\": \"2024-01-30\",\n"
            "  \"total_amount\": 150.75,\n"
            "  \"currency\": \"USD\",\n"
            "  \"line_items\": [\n"
            "    {{\n"
            "      \"description\": \"Product A\",\n"
            "      \"quantity\": 2,\n"
            "      \"unit_price\": 50.00,\n"
            "      \"amount\": 100.00\n"
            "    }},\n"
            "    {{\n"
            "      \"description\": \"Service B\",\n"
            "      \"quantity\": null,\n"
            "      \"unit_price\": null,\n"
            "      \"amount\": 50.75\n"
            "    }}\n"
            "  ]\n"
            "}}\n"
            "```\n\n"
            "Here is the invoice text:\n"
            "---------------------\n"
            "{invoice_text}\n"
            "---------------------\n"
            "JSON Output Only (using specified keys):"
        )

    def _extract_text_from_pdf(self, pdf_content: bytes, filename: str) -> Optional[str]:
        """Extracts text from PDF content using PyPDF2."""
        try:
            reader = PdfReader(io.BytesIO(pdf_content))
            text = ""
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text: # Check if text extraction returned something
                    text += page_text + "\n" # Add newline between pages
            if not text:
                 logger.warning(f"PyPDF2 extracted no text from {filename}. It might be image-based or corrupted.")
                 return None # Indicate no text could be extracted
            logger.info(f"Successfully extracted text from {filename} using PyPDF2.")
            # Limit text length to avoid excessive token usage with Mistral
            max_chars = 15000 # Adjust as needed based on token limits/cost
            if len(text) > max_chars:
                logger.warning(f"Extracted text truncated to {max_chars} characters for {filename}.")
                text = text[:max_chars]
            return text
        except Exception as e:
            logger.error(f"PyPDF2 failed to process {filename}: {e}")
            return None


    def _parse_response(self, response_content: str, filename: str) -> Optional[ExtractedInvoiceData]:
        """Attempts to parse the LLM response into the Pydantic model."""
        try:
            # Clean up potential markdown code blocks
            response_content = response_content.strip()
            if response_content.startswith("```json"):
                response_content = response_content[7:]
            if response_content.endswith("```"):
                response_content = response_content[:-3]
            response_content = response_content.strip()

            # --- Add this line ---
            # Remove ASCII control characters (0-31) except \n, \r, \t
            response_content = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', response_content)
            # --------------------

            # Handle potential variations if model doesn't strictly follow JSON format
            # Basic check if it looks like JSON
            if not response_content.startswith("{") or not response_content.endswith("}"):
                logger.warning(f"Mistral response for {filename} does not appear to be valid JSON: {response_content[:100]}...")
                # Attempt parsing anyway, might fail

            data = ExtractedInvoiceData.model_validate_json(response_content)
            logger.info(f"Successfully parsed Mistral OCR response for {filename}: {data.model_dump(exclude_none=True)}")
            return data
        except ValidationError as e:
            logger.error(f"Failed to validate Mistral OCR JSON response for {filename}: {e}")
            logger.debug(f"Raw response content for {filename}: {response_content}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error parsing Mistral response for {filename}: {e}")
            logger.debug(f"Raw response content for {filename}: {response_content}")
            return None

    def extract(self, file_content: bytes, filename: str) -> Optional[ExtractedInvoiceData]:
        """
        Extracts data using Mistral's API after text extraction via PyPDF2.
        """
        logger.info(f"Starting Mistral OCR extraction process for: {filename}")

        # Step 1: Extract text from PDF
        invoice_text = self._extract_text_from_pdf(file_content, filename)
        if not invoice_text:
             logger.error(f"Failed to extract text from {filename}. Cannot proceed with Mistral OCR.")
             return None

        # Step 2: Prepare prompt for Mistral
        prompt = self.extraction_prompt_template.format(invoice_text=invoice_text)

        # Step 3: Call Mistral API
        try:
            logger.info(f"Sending request to Mistral API for {filename}...")
            # Updated API call: Use chat.complete and pass messages as dicts
            chat_response = self.client.chat.complete(
                model="mistral-large-latest", # Confirm this is the best model choice
                messages=[{"role": "user", "content": prompt}], # Pass message as dict
                temperature=0.1, # Lower temperature for more deterministic extraction
                response_format={"type": "json_object"} # Added to enforce JSON output
            )

            if chat_response.choices and chat_response.choices[0].message:
                response_content = chat_response.choices[0].message.content
                logger.info(f"Received Mistral response for {filename}.")
                # Step 4: Parse the response
                return self._parse_response(response_content, filename)
            else:
                logger.error(f"Mistral API returned no choices or message content for {filename}.")
                # Log relevant details from the response if available
                # logger.debug(f"Mistral API full response: {chat_response}")
                return None

        except Exception as e:
            logger.error(f"Error calling Mistral API for {filename}: {e}", exc_info=True) # Log traceback
            return None

# --- Factory Function ---
def get_ocr_service() -> Optional[OCRService]:
    """Returns an instance of the configured OCR service, or None if config fails."""
    service_name = config.OCR_SERVICE
    logger.info(f"Attempting to initialize OCR service: {service_name}")
    try:
        if service_name == "mistral":
            return MistralOCR()
        # Add other services here with elif blocks
        # elif service_name == "azure":
        #     return AzureOCR(...)
        else:
            logger.error(f"Unsupported OCR service configured: {service_name}")
            return None
    except ValueError as e: # Catch config errors like missing API keys
         logger.error(f"Failed to initialize OCR service '{service_name}': {e}")
         return None
    except Exception as e:
        logger.error(f"Unexpected error initializing OCR service '{service_name}': {e}", exc_info=True)
        return None
