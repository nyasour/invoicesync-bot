"""
Service responsible for categorizing invoices based on extracted data.
"""

import logging
from typing import Optional, Literal
import json

from pydantic import BaseModel, ValidationError
import openai

from .ocr import ExtractedInvoiceData
from config import settings

logger = logging.getLogger(__name__)


class CategorizationResult(BaseModel):
    """Represents the outcome of the LLM categorization process."""
    status: Literal['matched', 'not_matched', 'error']
    assigned_category: Optional[str] = None # Set if status is 'matched'
    suggested_new_category: Optional[str] = None # Potentially set if status is 'not_matched'
    # Add other relevant categorization fields, e.g., GL code, department
    notes: Optional[str] = None # General notes or error details


class InvoiceCategorizer:
    """Categorizes invoices using predefined rules or potentially an LLM."""

    def __init__(self):
        """Initializes the categorizer based on configuration."""
        self.provider = settings.CATEGORIZATION_PROVIDER
        self.client = None
        self.allowed_categories = settings.ALLOWED_CATEGORIES
        self.company_context = settings.COMPANY_CONTEXT

        if self.provider == "openai":
            if not settings.OPENAI_API_KEY:
                logger.error("OpenAI API key is missing. OpenAI categorizer disabled.")
            else:
                try:
                    self.client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
                    logger.info("OpenAI client initialized for categorization.")
                except Exception as e:
                    logger.error(f"Failed to initialize OpenAI client: {e}")
        elif self.provider == "rules": # Example for future expansion
            logger.info("Using rule-based categorization (placeholder). ")
            # Load rules here if implementing rule-based logic
        else:
            logger.warning(f"Unsupported categorization provider: {self.provider}. Categorization disabled.")

    def _build_openai_prompt(self, invoice_data: ExtractedInvoiceData) -> str:
        """Builds the prompt for the OpenAI API call."""
        invoice_details = f"Vendor: {invoice_data.vendor_name}\n" \
                          f"Invoice Number: {invoice_data.invoice_number}\n" \
                          f"Issue Date: {invoice_data.issue_date}\n" \
                          f"Total Amount: {invoice_data.total_amount}\n"
        if invoice_data.line_items:
            invoice_details += "Line Items:\n"
            for item in invoice_data.line_items:
                invoice_details += f"  - Description: {item.description}, Quantity: {item.quantity}, Unit Price: {item.unit_price}, Amount: {item.amount}\n"

        allowed_categories_str = ", ".join(self.allowed_categories)

        prompt = f"""\
You are an accounts payable assistant for '{self.company_context}'.
Your task is to categorize the following invoice data based on the provided list of allowed expense categories.

Allowed Expense Categories:
{allowed_categories_str}

Invoice Data:
{invoice_details}

Please analyze the invoice data and respond ONLY with a JSON object containing the categorization result. The JSON object must have the following structure:
{{
  "status": "<status>",                // Required. Must be 'matched', 'not_matched', or 'error'.
  "assigned_category": "<category>",    // Required if status is 'matched', otherwise null. Must be EXACTLY one of the allowed categories listed above.
  "suggested_new_category": "<text>", // Optional. Suggest a new category if status is 'not_matched' and you have a suggestion, otherwise null.
  "notes": "<text>"                   // Optional. Add brief notes or explanation for the categorization or error.
}}

Instructions:
- If the invoice clearly matches one of the allowed categories, set status to 'matched' and assigned_category to the EXACT category name.
- If the invoice does not clearly match any allowed category, set status to 'not_matched' and assigned_category to null. You may suggest a new category in suggested_new_category if appropriate.
- If you encounter an error processing the request, set status to 'error' and provide details in notes.
- Do NOT include any text outside the JSON object in your response.
"""
        return prompt

    def categorize(self, invoice_data: ExtractedInvoiceData) -> CategorizationResult:
        """Determines the expense category for the given invoice data using the configured provider."""
        logger.info(f"Starting categorization for vendor: {invoice_data.vendor_name} using provider: {self.provider}")

        if self.provider != "openai" or not self.client:
            logger.warning(f"Categorization skipped: Provider is '{self.provider}' or client not initialized.")
            return CategorizationResult(status='error', notes=f"Categorization provider '{self.provider}' not supported or not initialized.")

        prompt = self._build_openai_prompt(invoice_data)

        try:
            logger.debug(f"Sending prompt to OpenAI: {prompt}")
            # Consider using gpt-3.5-turbo for cost/speed if sufficient
            # Use response_format for guaranteed JSON if using compatible models (e.g., gpt-4-turbo-preview)
            completion = self.client.chat.completions.create(
                model="gpt-4o", # Or another suitable model like gpt-4-turbo
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2, # Lower temperature for more deterministic results
                # response_format={ "type": "json_object" } # Enable if model supports it
            )

            response_content = completion.choices[0].message.content
            logger.debug(f"Received raw response from OpenAI: {response_content}")

            if not response_content:
                 logger.error("OpenAI returned an empty response.")
                 return CategorizationResult(status='error', notes="LLM returned empty response.")
            
            # Attempt to parse the JSON (LLM might sometimes add extra text/markdown)
            try:
                # Basic cleanup: strip leading/trailing whitespace and potential markdown code fences
                response_content = response_content.strip()
                if response_content.startswith("```json"):
                    response_content = response_content[7:]
                if response_content.endswith("```"):
                    response_content = response_content[:-3]
                response_content = response_content.strip()

                parsed_json = json.loads(response_content)
            except json.JSONDecodeError as json_e:
                logger.error(f"Failed to decode JSON response from OpenAI: {json_e}")
                logger.debug(f"Non-JSON response content: {response_content}")
                return CategorizationResult(status='error', notes=f"LLM response was not valid JSON: {response_content[:100]}...")

            # Validate the parsed JSON against our Pydantic model
            try:
                result = CategorizationResult.model_validate(parsed_json)
                logger.info(f"Successfully parsed and validated LLM response: Status='{result.status}', Category='{result.assigned_category}'")

                # Additional check: If matched, ensure category is allowed
                if result.status == 'matched':
                    if result.assigned_category not in self.allowed_categories:
                        logger.warning(f"LLM assigned category '{result.assigned_category}' which is not in the allowed list: {self.allowed_categories}. Treating as 'not_matched'.")
                        result.notes = f"LLM suggested invalid category '{result.assigned_category}'. Original Notes: {result.notes}"
                        result.assigned_category = None
                        result.status = 'not_matched'
                        # Optionally try to capture the bad category as a suggestion?
                        # result.suggested_new_category = result.assigned_category 
                        
                return result
                
            except ValidationError as e:
                logger.error(f"LLM response failed Pydantic validation: {e}")
                logger.debug(f"Invalid JSON structure received: {parsed_json}")
                return CategorizationResult(status='error', notes=f"LLM response structure invalid: {e}")

        except openai.APIError as e:
            logger.error(f"OpenAI API returned an API Error: {e}")
            return CategorizationResult(status='error', notes=f"OpenAI API Error: {e}")
        except openai.APIConnectionError as e:
            logger.error(f"Failed to connect to OpenAI API: {e}")
            return CategorizationResult(status='error', notes=f"OpenAI Connection Error: {e}")
        except openai.RateLimitError as e:
            logger.error(f"OpenAI API request exceeded rate limit: {e}")
            return CategorizationResult(status='error', notes=f"OpenAI Rate Limit Error: {e}")
        except Exception as e:
            logger.exception(f"An unexpected error occurred during categorization: {e}") # Use logger.exception to include traceback
            return CategorizationResult(status='error', notes=f"Unexpected error: {e}")
