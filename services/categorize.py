import logging
import json
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from pydantic import BaseModel, ValidationError

from openai import OpenAI, OpenAIError # Import OpenAI client and errors
from config import settings # Ensure settings is imported
import openai # Ensure openai is imported

# Import the OCR data model and config
from services.ocr import ExtractedInvoiceData
import config

logger = logging.getLogger(__name__)

# --- Pydantic Model for Categorized Data ---
# We can extend the OCR data model or create a new one. Let's extend for simplicity.
class CategorizedInvoiceData(ExtractedInvoiceData):
    category: Optional[str] = None # Add the category field

# --- Categorization Service Interface ---
class CategorizationService(ABC):
    @abstractmethod
    def categorize(self, invoice_data: ExtractedInvoiceData) -> Optional[CategorizedInvoiceData]:
        """
        Categorizes an invoice based on extracted data.

        Args:
            invoice_data: The structured data extracted by the OCR service.

        Returns:
            CategorizedInvoiceData object with the added category, or None if categorization fails.
        """
        pass

# --- OpenAI Categorization Implementation ---
class OpenAICategorizer(CategorizationService):
    """Categorizes invoices using the OpenAI API."""

    def __init__(self, api_key: Optional[str] = None): # Default to None
        """
        Initializes the OpenAICategorizer.

        Args:
            api_key: The OpenAI API key. If None, it's fetched from settings.
        """
        self.logger = logging.getLogger(__name__)

        # If no API key is passed in, try to get it from settings
        if api_key is None:
            api_key = settings.OPENAI_API_KEY
            self.logger.info("Attempting to use OpenAI API key from settings.")

        # Raise error if still no API key
        if not api_key:
            self.logger.error("OpenAI API key is required but not provided or found in settings.")
            raise ValueError("OpenAI API key is required but not provided or found in settings.")

        self.api_key = api_key
        try:
            # Initialize the OpenAI client with the resolved API key
            self.client = openai.OpenAI(api_key=self.api_key)
            self.logger.info("OpenAI client initialized successfully.")
        except Exception as e:
            self.logger.error(f"Failed to initialize OpenAI client: {e}")
            self.client = None # Ensure client is None if initialization fails
            raise ConnectionError(f"Failed to initialize OpenAI client: {e}") from e

        self.model = "gpt-4-turbo-preview" # Or "gpt-4" - check availability/cost
        # Define allowed categories based on config/requirements
        self.allowed_categories = list(config.XERO_ACCOUNT_CODES.keys())
        self.system_prompt = f"""
You are an AI assistant helping categorize business invoices. Based on the provided invoice details (vendor name, line items), determine the most appropriate expense category.
The allowed categories are: {', '.join(self.allowed_categories)}.
Respond ONLY with the single most fitting category name from the allowed list. Do not add explanations or any other text. If no category fits well, respond with "Other".
"""

    def categorize(self, invoice_data: ExtractedInvoiceData) -> Optional[CategorizedInvoiceData]:
        """Categorizes using OpenAI's ChatCompletion API."""
        if not invoice_data:
             logger.warning("Cannot categorize invoice: No input data provided.")
             return None

        # Create a concise representation of the invoice data for the prompt
        prompt_data = f"Vendor: {invoice_data.vendor_name}\n"
        prompt_data += f"Total Amount: {invoice_data.total_amount}\n"
        if invoice_data.line_items:
            items_str = "; ".join([
                f"{item.description} ({item.amount})"
                for item in invoice_data.line_items
                if item.description or item.amount # Only include items with some info
            ])
            if items_str: # Add line items only if they contain useful info
                 prompt_data += f"Line Items: {items_str}"

        logger.info(f"Requesting categorization for invoice data: {prompt_data[:200]}...") # Log snippet

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt_data}
                ],
                temperature=0.2, # Low temperature for consistency
                max_tokens=20, # Category name should be short
                n=1, # We only need one category suggestion
                stop=None # No specific stop sequence needed
            )

            if response.choices and response.choices[0].message:
                category = response.choices[0].message.content.strip()
                logger.info(f"OpenAI suggested category: {category}")

                # Validate the category against the allowed list
                if category not in self.allowed_categories:
                    logger.warning(f"OpenAI returned an invalid category '{category}'. Defaulting to 'Other'.")
                    category = "Other" # Fallback to default category

                # Create the output object by copying input and adding the category
                categorized_data = CategorizedInvoiceData(
                    **invoice_data.model_dump(), # Copy fields from input
                    category=category
                )
                return categorized_data
            else:
                logger.error("OpenAI API returned no choices or message content for categorization.")
                return None

        except OpenAIError as e:
            logger.error(f"OpenAI API error during categorization: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error during categorization: {e}", exc_info=True)
            return None

# --- Factory Function ---
def get_categorization_service() -> Optional[CategorizationService]:
    """Returns an instance of the configured categorization service."""
    service_name = config.CATEGORIZATION_SERVICE
    logger.info(f"Attempting to initialize Categorization service: {service_name}")
    try:
        if service_name == "openai":
            return OpenAICategorizer()
        # Add other services here with elif blocks
        # elif service_name == "custom":
        #     return CustomCategorizer(...)
        else:
            logger.error(f"Unsupported Categorization service configured: {service_name}")
            return None
    except ValueError as e: # Catch config errors like missing API keys
        logger.error(f"Failed to initialize Categorization service '{service_name}': {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error initializing Categorization service '{service_name}': {e}", exc_info=True)
        return None
