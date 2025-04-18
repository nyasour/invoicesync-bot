# services/ocr.py
import logging
import io # Needed for PyPDF2
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, ValidationError
from mistralai.client import MistralClient
from mistralai.models.chat_completion import ChatMessage
from PyPDF2 import PdfReader # Import PdfReader

import config # Use loaded config

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO) # Basic logging config for now

# --- Pydantic Models for Structured Output ---
class LineItem(BaseModel):
    description: Optional[str] = None
    amount: Optional[float] = None
    quantity: Optional[int] = None # Optional

class ExtractedInvoiceData(BaseModel):
    vendor_name: Optional[str] = Field(None, alias="vendor")
    invoice_number: Optional[str] = Field(None, alias="invoice_id") # Alias based on common OCR outputs
    issue_date: Optional[str] = Field(None, alias="date") # Expecting YYYY-MM-DD format
    total_amount: Optional[float] = Field(None, alias="total")
    line_items: Optional[List[LineItem]] = [] # List of line items

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
    def __init__(self, api_key: Optional[str] = config.MISTRAL_API_KEY):
        if not api_key:
            # Log critical error and raise exception if API key is missing
            logger.critical("Mistral API key is not configured. OCR service cannot be initialized.")
            raise ValueError("Mistral API key is not configured.")
        self.client = MistralClient(api_key=api_key)
        # Define the expected JSON structure for Mistral
        self.extraction_prompt_template = """
Extract the following information from the provided invoice text:
- vendor_name: The name of the company issuing the invoice.
- invoice_number: The unique identifier for the invoice.
- issue_date: The date the invoice was issued (format YYYY-MM-DD). If multiple dates exist, prefer the main invoice date.
- total_amount: The final total amount due, including tax if specified. Must be a number.
- line_items: A list of items/services, including 'description' and 'amount' for each. If multiple items exist, list them all. If no line items are clearly listed, provide an empty list [].

Format the output STRICTLY as a JSON object with these exact keys: "vendor_name", "invoice_number", "issue_date", "total_amount", "line_items".
If a value is not found or cannot be determined, use null for that key (e.g., "invoice_number": null). Do not include explanations or apologies.

Invoice Text:
```
{invoice_text}
```

JSON Output:
"""

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
            chat_response = self.client.chat(
                model="mistral-large-latest", # Confirm this is the best model choice
                messages=[ChatMessage(role="user", content=prompt)],
                temperature=0.1, # Lower temperature for more deterministic extraction
                # response_format={"type": "json_object"} # Uncomment if supported and desired
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
