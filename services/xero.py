# services/xero.py
import logging
import datetime
from typing import Optional, Dict, Any

# Xero API library and OAuth2 handling
from xero_python.accounting import AccountingApi, Contact, Account, Bill, LineItem as XeroLineItem, CurrencyCode
from xero_python.api_client import ApiClient, Configuration
from xero_python.api_client.oauth2 import OAuth2Credentials
from xero_python.exceptions import AccountingBadRequestException, ApiException

# Import project config and data models
import config
from services.categorize import CategorizedInvoiceData # Use categorized data

logger = logging.getLogger(__name__)

class XeroService:
    def __init__(self):
        self._credentials = None
        self._accounting_api = None
        self._tenant_id = config.XERO_TENANT_ID

        if not all([config.XERO_CLIENT_ID, config.XERO_CLIENT_SECRET, config.XERO_REFRESH_TOKEN, config.XERO_TENANT_ID]):
            logger.critical("Xero configuration (Client ID, Secret, Refresh Token, Tenant ID) is incomplete. Xero service cannot be initialized.")
            raise ValueError("Xero configuration incomplete.")

        self._setup_credentials()
        self._setup_api_client()

    def _setup_credentials(self):
        """Sets up OAuth2 credentials for Xero API."""
        try:
            self._credentials = OAuth2Credentials(
                client_id=config.XERO_CLIENT_ID,
                client_secret=config.XERO_CLIENT_SECRET,
                # Grant type is implicitly refresh_token when refresh_token is provided
            )
            # Set the refresh token obtained during initial authorization
            self._credentials.set_raw_refresh_token(config.XERO_REFRESH_TOKEN)
            # Note: Access token will be fetched/refreshed automatically by the library when needed
            logger.info("Xero OAuth2 credentials configured.")
        except Exception as e:
            logger.error(f"Failed to configure Xero credentials: {e}", exc_info=True)
            raise

    def _setup_api_client(self):
        """Sets up the Xero API client with configured credentials."""
        if not self._credentials:
            logger.error("Cannot setup Xero API client: Credentials not configured.")
            raise ValueError("Xero credentials not configured.")
        try:
            api_client = ApiClient(
                Configuration(oauth2_credentials=self._credentials),
                pool_threads=1 # Recommended for serverless environments
            )
            self._accounting_api = AccountingApi(api_client)
            logger.info("Xero Accounting API client initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Xero API client: {e}", exc_info=True)
            raise

    def _get_tenant_id(self) -> Optional[str]:
        """Returns the configured Tenant ID."""
        # In a multi-tenant scenario, this might involve fetching connections
        # For MVP, we use the one from config.
        if not self._tenant_id:
             logger.error("Xero Tenant ID is not configured.")
             return None
        return self._tenant_id

    def _find_or_create_contact(self, vendor_name: str) -> Optional[Contact]:
        """Finds an existing contact by name or creates a new one."""
        tenant_id = self._get_tenant_id()
        if not tenant_id or not vendor_name:
            return None

        try:
            # Try finding by name (case-insensitive exact match for simplicity)
            where_filter = f'Name.ToLower() == "{vendor_name.lower()}"'
            contacts = self._accounting_api.get_contacts(tenant_id, where=where_filter)

            if contacts and contacts.contacts:
                logger.info(f"Found existing Xero contact for '{vendor_name}'.")
                return contacts.contacts[0]
            else:
                # Contact not found, create a new one
                logger.info(f"Xero contact for '{vendor_name}' not found. Creating new contact.")
                new_contact = Contact(name=vendor_name)
                created_contacts = self._accounting_api.create_contacts(tenant_id, contacts={"contacts": [new_contact]})
                if created_contacts and created_contacts.contacts:
                    logger.info(f"Successfully created new Xero contact for '{vendor_name}'.")
                    return created_contacts.contacts[0]
                else:
                    logger.error(f"Failed to create Xero contact for '{vendor_name}'. API response empty.")
                    return None
        except AccountingBadRequestException as e:
             logger.error(f"Xero API Bad Request finding/creating contact '{vendor_name}': {e.body}", exc_info=True)
             return None
        except ApiException as e:
            logger.error(f"Xero API error finding/creating contact '{vendor_name}': {e}", exc_info=True)
            return None

    def create_draft_expense(self, invoice_data: CategorizedInvoiceData, pdf_content: bytes, pdf_filename: str) -> Optional[str]:
        """
        Creates a draft Bill (expense) in Xero with PDF attachment.

        Args:
            invoice_data: The categorized invoice data.
            pdf_content: The raw byte content of the original PDF invoice.
            pdf_filename: The original filename of the PDF.

        Returns:
            The ID of the created Xero Bill, or None if creation fails.
        """
        tenant_id = self._get_tenant_id()
        if not tenant_id or not invoice_data or not invoice_data.vendor_name:
            logger.error("Cannot create Xero expense: Missing tenant ID or invoice data/vendor name.")
            return None

        contact = self._find_or_create_contact(invoice_data.vendor_name)
        if not contact or not contact.contact_id:
            logger.error(f"Failed to find or create Xero contact for vendor '{invoice_data.vendor_name}'. Cannot create Bill.")
            return None

        # Map category to Xero Account Code
        account_code = config.XERO_ACCOUNT_CODES.get(invoice_data.category, config.XERO_ACCOUNT_CODES.get("Other"))
        if not account_code:
             logger.error(f"Could not find Xero account code for category '{invoice_data.category}' or 'Other'. Check config.")
             return None # Or potentially raise an error


        # Prepare Line Item(s) for the Bill
        # For MVP, create a single line item using the category and total amount.
        # A more advanced version could try to use OCR'd line items if available and reliable.
        line_items = [
            XeroLineItem(
                description=f"Invoice {invoice_data.invoice_number or 'N/A'} from {invoice_data.vendor_name}",
                quantity=1.0,
                unit_amount=invoice_data.total_amount or 0.0, # Use total amount for single line
                account_code=account_code,
                # tax_type=... # Determine based on configuration or rules if needed
            )
        ]

        # Prepare the Bill object
        bill_to_create = Bill(
            type="ACCPAY", # Accounts Payable Bill
            contact=contact,
            date=datetime.datetime.strptime(invoice_data.issue_date, '%Y-%m-%d').date() if invoice_data.issue_date else datetime.date.today(),
            due_date=None, # Optional: Calculate based on terms?
            reference=invoice_data.invoice_number or None,
            status="DRAFT", # Create as Draft as per requirements
            line_items=line_items,
            # total=invoice_data.total_amount # Usually calculated from line items
            # currency_code=CurrencyCode.USD # Set appropriate currency code if needed
        )

        try:
            logger.info(f"Attempting to create draft Bill in Xero for vendor '{invoice_data.vendor_name}'...")
            created_bills = self._accounting_api.create_bills(
                tenant_id,
                bills={"bills": [bill_to_create]},
                unitdp=4 # Decimal places for line item amounts
            )

            if not created_bills or not created_bills.bills:
                logger.error("Failed to create Bill in Xero: API response was empty.")
                return None

            created_bill = created_bills.bills[0]
            bill_id = created_bill.bill_id
            logger.info(f"Successfully created draft Bill in Xero with ID: {bill_id}")

            # Attach the PDF
            try:
                logger.info(f"Attempting to attach PDF '{pdf_filename}' to Bill ID: {bill_id}")
                self._accounting_api.create_bill_attachment_by_file_name(
                    tenant_id,
                    bill_id,
                    file_name=pdf_filename,
                    body=pdf_content,
                    # include_online=True # Optional: Make attachment viewable online
                    _headers={'Content-Type': 'application/pdf'} # Important header
                )
                logger.info(f"Successfully attached PDF '{pdf_filename}' to Bill ID: {bill_id}")
            except AccountingBadRequestException as e:
                logger.error(f"Xero API Bad Request attaching PDF to Bill {bill_id}: {e.body}", exc_info=True)
                # Continue even if attachment fails? For MVP, maybe log and return bill ID.
            except ApiException as e:
                logger.error(f"Xero API error attaching PDF to Bill {bill_id}: {e}", exc_info=True)
                 # Continue even if attachment fails? For MVP, maybe log and return bill ID.


            return bill_id

        except AccountingBadRequestException as e:
             logger.error(f"Xero API Bad Request creating Bill for '{invoice_data.vendor_name}': {e.body}", exc_info=True)
             # Try to parse specific validation errors if possible from e.body
             return None
        except ApiException as e:
            logger.error(f"Xero API error creating Bill for '{invoice_data.vendor_name}': {e}", exc_info=True)
            return None
        except Exception as e:
             logger.error(f"Unexpected error creating Xero Bill: {e}", exc_info=True)
             return None

# --- Factory Function ---
def get_xero_service() -> Optional[XeroService]:
    """Returns an instance of the Xero service, or None if config fails."""
    logger.info("Attempting to initialize Xero service...")
    try:
        # Check if essential configs are present before initializing
        if not all([config.XERO_CLIENT_ID, config.XERO_CLIENT_SECRET, config.XERO_REFRESH_TOKEN, config.XERO_TENANT_ID]):
             logger.error("Cannot initialize Xero service due to missing configuration.")
             return None
        return XeroService()
    except ValueError as e: # Catch config errors
        logger.error(f"Failed to initialize Xero service: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error initializing Xero service: {e}", exc_info=True)
        return None
