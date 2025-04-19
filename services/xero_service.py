import logging
import os
import sys
import json
import base64
from abc import ABC, abstractmethod
from typing import Dict, Any, Tuple, Optional

# Assuming config setup similar to other services
from config import settings
from .ocr import ExtractedInvoiceData

# --- Define Custom Xero Exceptions --- START
class XeroConfigurationError(Exception):
    """Indicates an error with Xero configuration settings."""
    pass

class XeroApiException(Exception):
    """Indicates a general error during a Xero API call."""
    pass

class TokenExpiredError(XeroApiException):
    """Indicates that the OAuth token has expired."""
    pass
# --- Define Custom Xero Exceptions --- END

# OAuth and Xero Client libraries
from requests_oauthlib import OAuth2Session
from oauthlib.oauth2 import TokenExpiredError

# --- Use official xero-python SDK --- 
from xero_python.accounting import AccountingApi, Account, Accounts, Invoice, Invoices, Contact, Contacts
from xero_python.api_client import ApiClient, Configuration
from xero_python.api_client.oauth2 import OAuth2Token
from xero_python.exceptions import AccountingBadRequestException, ApiException
# --- End xero-python imports --- 

logger = logging.getLogger(__name__)

# Xero uses specific token URLs
XERO_AUTH_URL = 'https://login.xero.com/identity/connect/authorize'
XERO_TOKEN_URL = 'https://identity.xero.com/connect/token'
XERO_CONNECTIONS_URL = 'https://api.xero.com/connections'

class XeroService(ABC):
    """Interface for interacting with the Xero API."""

    @abstractmethod
    def get_authorization_url(self) -> tuple[str, str]:
        """Generate the Xero authorization URL and state."""
        pass

    @abstractmethod
    def fetch_token(self, authorization_response_url: str, state: str) -> Dict[str, Any]:
        """Fetch the OAuth token using the callback response."""
        pass

    @abstractmethod
    def get_tenant_id(self) -> Optional[str]:
        """Get the active Xero Tenant ID."""
        pass

    @abstractmethod
    def create_draft_bill(self, invoice_data: ExtractedInvoiceData, category: str) -> Optional[Dict[str, Any]]:
        """Create a draft bill in Xero."""
        pass

    @abstractmethod
    def refresh_oauth_token(self) -> Optional[Dict[str, Any]]:
        """Refresh the OAuth access token using the refresh token."""
        pass


class XeroPythonService(XeroService):
    """Implementation of XeroService using xero-python and requests-oauthlib."""

    def __init__(self):
        self.client_id = settings.XERO_CLIENT_ID
        self.client_secret = settings.XERO_CLIENT_SECRET
        self.redirect_uri = settings.XERO_REDIRECT_URI
        self.scopes = settings.XERO_SCOPES.split() # Should be a list
        # Store tokens securely (e.g., database, Secret Manager) in a real app
        # For now, we might load from config/env or store in memory temporarily
        # TODO: Implement secure loading/saving of token data (especially refresh_token and tenant_id)
        self._refresh_token = settings.XERO_REFRESH_TOKEN
        self._tenant_id = settings.XERO_TENANT_ID
        self._access_token_data: Optional[Dict[str, Any]] = None # To hold the full token dict {access_token, refresh_token, expires_at, ...}

        if not all([self.client_id, self.client_secret, self.redirect_uri, self.scopes]):
            logger.error("Xero credentials (ID, Secret, Redirect URI, Scopes) not fully configured.")
            # Depending on use case, might raise an error or just log

        logger.info("XeroPythonService initialized.")
        # Attempt to load existing token data if possible (e.g., from env/config for simplicity here)
        if self._refresh_token: 
             # If we have a refresh token, we might be able to construct a partial token dict
             # to enable refreshing later. Ideally, we'd load the full last known token.
             # For now, just having the refresh token is the key starting point.
             logger.info("Found existing Xero refresh token in config.")
             # We need expires_at to properly check for expiry before making calls
             # Without it, we'll have to rely on catching TokenExpiredError
             # Placeholder structure:
             self._access_token_data = {
                 'refresh_token': self._refresh_token,
                 'expires_at': 0 # Assume expired if only refresh token known
             }

    def _get_oauth_session(self, state: Optional[str] = None, token: Optional[Dict[str, Any]] = None) -> OAuth2Session:
        """Creates a requests_oauthlib session."""
        # If we have a token, create session with it
        if token:
            return OAuth2Session(self.client_id, token=token)
        # Otherwise, create session for initiating auth flow
        return OAuth2Session(self.client_id, redirect_uri=self.redirect_uri, scope=self.scopes, state=state)

    def get_authorization_url(self) -> tuple[str, str]:
        """Generate the Xero authorization URL and state."""
        session = self._get_oauth_session()
        authorization_url, state = session.authorization_url(XERO_AUTH_URL)
        logger.info(f"Generated Xero authorization URL with state: {state}")
        # TODO: Store the 'state' temporarily (e.g., in user session, cache, or db) to verify on callback
        return authorization_url, state

    def fetch_token(self, authorization_response_url: str, state: str) -> Dict[str, Any]:
        """Fetch the OAuth token using the callback response."""

        # TODO: Retrieve the stored 'state' and verify it matches the input 'state' parameter
        
        session = self._get_oauth_session(state=state)
        try:
            # Fetch token. client_secret is needed here.
            token = session.fetch_token(
                XERO_TOKEN_URL,
                client_secret=self.client_secret,
                authorization_response=authorization_response_url
            )
            logger.info("Successfully fetched Xero OAuth token.")
            self._access_token_data = token
            self._refresh_token = token.get('refresh_token')
            self._tenant_id = None # Reset tenant ID, needs fetching with new token
            # TODO: Persist the new full token dict (self._access_token_data) securely!
            logger.debug(f"New Token Data: {self._access_token_data}")
            # Fetch and store tenant ID immediately after getting token
            self.get_tenant_id() # Fetch and potentially store tenant ID
            return token
        except Exception as e:
            logger.exception(f"Error fetching Xero OAuth token: {e}")
            raise # Re-raise the exception

    def refresh_oauth_token(self) -> Optional[Dict[str, Any]]:
        """Refresh the OAuth access token using the refresh token."""
        if not self._refresh_token:
            logger.error("Cannot refresh token: No refresh token available.")
            # raise ValueError("Missing refresh token") # Or return None
            return None

        # Create a basic session for refreshing
        session = OAuth2Session(self.client_id, token=self._access_token_data) 

        try:
            logger.info("Attempting to refresh Xero OAuth token...")
            # Use requests-oauthlib's refresh mechanism
            new_token = session.refresh_token(
                XERO_TOKEN_URL,
                refresh_token=self._refresh_token,
                client_id=self.client_id,
                client_secret=self.client_secret
            )
            logger.info("Successfully refreshed Xero OAuth token.")
            self._access_token_data = new_token
            self._refresh_token = new_token.get('refresh_token')
            # Tenant ID should remain the same, but clear just in case if needed
            # self._tenant_id = None 
            # TODO: Persist the refreshed token securely!
            logger.debug(f"Refreshed Token Data: {self._access_token_data}")
            return new_token
        except Exception as e:
            logger.exception(f"Error refreshing Xero OAuth token: {e}")
            # Clear potentially invalid token data on failure?
            # self._access_token_data = None
            # self._refresh_token = None # Be careful not to lose the refresh token if it might still work
            return None # Indicate failure

    def _ensure_token_valid(self) -> bool:
        """Checks if the token exists and attempts refresh if expired. Returns True if valid/refreshed, False otherwise."""
        if not self._access_token_data:
            logger.warning("No Xero access token data available.")
            # Can we get one using a refresh token?
            if self._refresh_token:
                logger.info("Attempting initial token refresh using stored refresh token.")
                refreshed_token = self.refresh_oauth_token()
                return refreshed_token is not None
            else:
                logger.error("Authentication needed: No access token and no refresh token.")
                return False
        
        # Check expiry time (add buffer, e.g., 60 seconds)
        expires_at = self._access_token_data.get('expires_at', 0)
        if time.time() > expires_at - 60:
            logger.info("Xero access token expired or nearing expiry, attempting refresh.")
            refreshed_token = self.refresh_oauth_token()
            if not refreshed_token:
                logger.error("Token refresh failed.")
                return False
            else:
                 logger.info("Token refresh successful.")

        return True # Token exists and is likely valid (or was just refreshed)

    def get_tenant_id(self) -> Optional[str]:
        """Get the active Xero Tenant ID using the connections endpoint."""
        # Return cached ID if we have it
        if self._tenant_id:
            return self._tenant_id

        # Ensure token is valid before making API call
        if not self._ensure_token_valid():
            logger.error("Cannot fetch tenant ID: Invalid or missing token.")
            return None

        # Use requests-oauthlib session to make the call
        session = self._get_oauth_session(token=self._access_token_data)
        try:
            logger.info("Fetching Xero connections to get tenant ID...")
            response = session.get(XERO_CONNECTIONS_URL)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            connections = response.json()
            logger.debug(f"Xero Connections Response: {connections}")
            if connections and isinstance(connections, list) and len(connections) > 0:
                # Assuming the first connection is the desired one
                tenant_id = connections[0].get('tenantId')
                if tenant_id:
                    self._tenant_id = tenant_id
                    logger.info(f"Fetched and cached Xero Tenant ID: {self._tenant_id}")
                    # TODO: Persist the tenant ID if needed
                    return self._tenant_id
                else:
                    logger.warning("Tenant ID not found in the first connection.")
                    return None
            else:
                logger.warning(f"Could not determine Tenant ID from connections response: {connections}")
                return None
        except Exception as e:
            logger.exception(f"Error fetching Xero connections: {e}")
            return None

    def _get_xero_api_client(self) -> Optional[AccountingApi]:
        """Initializes and returns the xero-python AccountingApi client."""
        if not self._ensure_token_valid():
            logger.error("Cannot create Xero API client: Invalid or missing token.")
            return None
            
        tenant_id = self.get_tenant_id() # Fetch/get cached tenant ID
        if not tenant_id:
            logger.error("Cannot create Xero API client: Missing Tenant ID.")
            return None

        try:
            # Create OAuth2Token object expected by xero-python
            oauth2_token = OAuth2Token(client_id=self.client_id, token=self._access_token_data)
            
            # Create ApiClient
            api_client = ApiClient(
                Configuration( 
                    host = "https://api.xero.com/api.xro/2.0", 
                    oauth2_token=oauth2_token
                ),
                oauth2_token=oauth2_token, # Pass it here too for potential internal use
                pool_threads=1 # Or adjust as needed
            )
            
            # Attach a token refresher callback (optional but recommended)
            # This allows the SDK to attempt refreshing automatically
            @api_client.oauth2_token_getter
            def get_token():
                return self._access_token_data # Return current token data

            @api_client.oauth2_token_saver
            def save_token(token_dict):
                logger.info("xero-python SDK internal token saver called.")
                self._access_token_data = token_dict
                self._refresh_token = token_dict.get('refresh_token')
                # TODO: Persist the token securely immediately!
                logger.debug(f"SDK Saved Token: {self._access_token_data}")

            # Return the specific API we need (Accounting)
            return AccountingApi(api_client)
        except Exception as e:
            logger.exception(f"Failed to initialize Xero API client: {e}")
            return None

    def _find_contact(self, accounting_api: AccountingApi, tenant_id: str, name: str) -> Optional[str]:
        """Finds a Xero contact by name using xero-python, returns ContactID."""
        try:
            logger.info(f"Searching for Xero contact with name: '{name}'")
            # Use where clause for filtering
            where_filter = f'Name=="{name}"'
            contacts_response = accounting_api.get_contacts(tenant_id, where=where_filter)
            
            if contacts_response and contacts_response.contacts and len(contacts_response.contacts) > 0:
                contact_id = contacts_response.contacts[0].contact_id
                logger.info(f"Found existing Xero contact '{name}' with ID: {contact_id}")
                return str(contact_id) # Return as string
            else:
                logger.info(f"No existing Xero contact found for '{name}'.")
                return None
        except ApiException as e:
             # Handle specific API errors, e.g., 404 Not Found might be expected if contact doesn't exist
            if e.status == 404:
                 logger.info(f"No existing Xero contact found for '{name}' (API 404).")
                 return None
            logger.exception(f"API Error searching for Xero contact '{name}': Status {e.status}, Body: {e.body}")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error searching for Xero contact '{name}': {e}")
            return None

    def _create_contact(self, accounting_api: AccountingApi, tenant_id: str, name: str) -> Optional[str]:
        """Creates a new Xero contact using xero-python, returns ContactID."""
        try:
            logger.info(f"Creating new Xero contact with name: '{name}'")
            new_contact = Contact(name=name)
            contacts_to_create = Contacts(contacts=[new_contact])
            created_contacts_response = accounting_api.create_contacts(tenant_id, contacts=contacts_to_create)
            
            if created_contacts_response and created_contacts_response.contacts and len(created_contacts_response.contacts) > 0:
                # Check for errors within the contact response items if needed
                contact_id = created_contacts_response.contacts[0].contact_id
                if contact_id:
                    logger.info(f"Successfully created new Xero contact '{name}' with ID: {contact_id}")
                    return str(contact_id)
                else:
                    logger.error(f"Failed to create Xero contact '{name}'. Response item lacked ID: {created_contacts_response.contacts[0]}")
                    return None
            else:
                logger.error(f"Failed to create Xero contact '{name}'. Response: {created_contacts_response}")
                return None
        except AccountingBadRequestException as e:
            logger.exception(f"Bad Request Error creating Xero contact '{name}': Status {e.status}, Body: {e.body}")
            # Parse e.body which might contain specific validation errors
            return None
        except ApiException as e:
            logger.exception(f"API Error creating Xero contact '{name}': Status {e.status}, Body: {e.body}")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error creating Xero contact '{name}': {e}")
            return None

    def _get_account_code(self, accounting_api: AccountingApi, tenant_id: str, category_name: str) -> Optional[str]:
        """Maps internal category name to Xero Account Code using config or Xero data."""
        # Option 1: Simple mapping from config (less flexible, requires maintenance)
        account_map_str = settings.XERO_ACCOUNT_CODE_MAP
        account_map = {}
        if account_map_str:
            try:
                account_map = json.loads(account_map_str)
            except json.JSONDecodeError:
                logger.error("Failed to parse XERO_ACCOUNT_CODE_MAP from settings. Ensure it's valid JSON.")
        
        code = account_map.get(category_name)
        if code:
            logger.info(f"Mapped category '{category_name}' to Xero Account Code: {code} using config map.")
            return str(code)
        else:
            logger.warning(f"Category '{category_name}' not found in XERO_ACCOUNT_CODE_MAP.")
            # Option 2: Fallback - Query Xero Chart of Accounts (more robust, slower)
            # try:
            #     logger.info(f"Querying Xero Chart of Accounts for category '{category_name}'...")
            #     # Search for an EXPENSE account matching the category name
            #     where_filter = f'Type=="EXPENSE" AND Name=="{category_name}"'
            #     accounts_response = accounting_api.get_accounts(tenant_id, where=where_filter)
            #     if accounts_response and accounts_response.accounts and len(accounts_response.accounts) > 0:
            #         account_code = accounts_response.accounts[0].code
            #         logger.info(f"Found matching Xero Account Code: {account_code} via API lookup.")
            #         return str(account_code)
            #     else:
            #         logger.warning(f"No Xero EXPENSE account found matching name '{category_name}'.")
            #         return None
            # except Exception as e:
            #     logger.exception(f"Error querying Xero accounts for category '{category_name}': {e}")
            #     return None
            return None # Return None if not found in map (and API lookup is disabled/failed)

    def _format_date(self, date_input: Optional[str]) -> Optional[str]:
        """Attempts to parse and format date string to YYYY-MM-DD."""
        if not date_input:
            return None
        try:
            # Attempt common formats
            parsed_date = None
            formats_to_try = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%Y%m%d"]
            for fmt in formats_to_try:
                 try:
                     parsed_date = datetime.strptime(date_input, fmt)
                     break
                 except ValueError:
                     continue
            
            if parsed_date:
                return parsed_date.strftime("%Y-%m-%d")
            else:
                 logger.warning(f"Could not parse date string: {date_input}")
                 return None # Or return original string if Xero might handle it?
        except Exception as e:
            logger.exception(f"Error formatting date '{date_input}': {e}")
            return None

    def create_draft_bill(self, invoice_data: ExtractedInvoiceData, category: str) -> Optional[Dict[str, Any]]:
        """Create a draft bill (Accounts Payable Invoice) in Xero."""
        accounting_api = self._get_xero_api_client()
        tenant_id = self.get_tenant_id() # Should be cached now

        if not accounting_api or not tenant_id:
            logger.error("Cannot create bill: Xero API client or Tenant ID not available.")
            return None

        try:
            # 1. Find or Create Contact
            if not invoice_data.vendor_name:
                logger.error("Cannot create bill: Vendor name is missing from extracted data.")
                return None
                
            contact_id = self._find_contact(accounting_api, tenant_id, invoice_data.vendor_name)
            if not contact_id:
                contact_id = self._create_contact(accounting_api, tenant_id, invoice_data.vendor_name)
            
            if not contact_id:
                logger.error(f"Failed to find or create Xero contact for '{invoice_data.vendor_name}'. Cannot create bill.")
                return None # Cannot proceed without a contact

            # 2. Map Category to Account Code
            account_code = self._get_account_code(accounting_api, tenant_id, category)
            if not account_code:
                 logger.warning(f"Proceeding without account code for category '{category}'. Bill line item will need manual coding.")

            # 3. Prepare Line Items using xero-python models
            line_items_payload = []
            total_from_lines = 0.0
            
            if invoice_data.line_items:
                 for item in invoice_data.line_items:
                     line_amount = item.amount or 0.0
                     # Use UnitAmount * Quantity if available, otherwise fallback to LineAmount
                     unit_amount = item.unit_price if item.unit_price is not None else (line_amount if (item.quantity or 0) <= 0 else line_amount / (item.quantity or 1))
                     quantity = item.quantity if item.quantity is not None and item.quantity > 0 else 1
                     
                     line_items_payload.append({
                         "Description": item.description or f"Item from {invoice_data.vendor_name}",
                         "Quantity": quantity,
                         "UnitAmount": unit_amount,
                         "AccountCode": account_code, # May be None
                         # "TaxType": "NONE", # TODO: Determine tax type based on rules/config
                         # "LineAmount": line_amount # Xero calculates this: Qty * UnitAmount
                     })
                     total_from_lines += (quantity * unit_amount)
            else: # Create a single line item if no detailed lines extracted
                logger.info("No detailed line items found, creating single summary line.")
                total = invoice_data.total_amount or 0.0
                line_items_payload.append({
                    "Description": f"Invoice {invoice_data.invoice_number or 'N/A'} from {invoice_data.vendor_name}",
                    "Quantity": 1,
                    "UnitAmount": total,
                    "AccountCode": account_code,
                })
                total_from_lines = total
                
            # Check if extracted total matches sum of lines (if both exist)
            if invoice_data.total_amount is not None and abs(invoice_data.total_amount - total_from_lines) > 0.01:
                 logger.warning(f"Extracted total ({invoice_data.total_amount}) does not match sum of lines ({total_from_lines}). Using extracted total if available.")

            # 4. Construct Invoice Payload using xero-python models
            invoice_payload = {
                "Type": "ACCPAY", # Accounts Payable Bill
                "Contact": {"ContactID": contact_id},
                "DateString": self._format_date(invoice_data.issue_date) or date.today().isoformat(),
                "DueDateString": self._format_date(invoice_data.due_date),
                "LineItems": line_items_payload,
                "InvoiceNumber": invoice_data.invoice_number,
                "CurrencyCode": invoice_data.currency, # Defaults based on Xero Org settings if None
                "Status": "DRAFT",
                "Reference": f"Slack Upload: Inv {invoice_data.invoice_number or 'N/A'}",
                 # Let Xero calculate Total if possible
                # "Total": invoice_data.total_amount if invoice_data.total_amount is not None else None 
            }
            # Clean payload (remove keys with None values that Xero might reject)
            cleaned_payload = {k: v for k, v in invoice_payload.items() if v is not None}
            
            # Need to structure as Invoice object for SDK
            invoice_object = Invoice(**cleaned_payload)
            invoices_to_create = Invoices(invoices=[invoice_object])

            logger.info(f"Submitting draft bill to Xero...")
            logger.debug(f"Xero Invoice Payload: {invoices_to_create.to_dict()}")

            # 5. Create the Bill using the API
            created_invoices_response = accounting_api.create_invoices(tenant_id, invoices=invoices_to_create)

            if created_invoices_response and created_invoices_response.invoices and len(created_invoices_response.invoices) > 0:
                # Check for errors within the invoice response items
                created_invoice = created_invoices_response.invoices[0]
                if created_invoice.invoice_id and not created_invoice.has_errors:
                    bill_id = str(created_invoice.invoice_id)
                    logger.info(f"Successfully created draft bill in Xero with ID: {bill_id}")
                    # TODO: Attach the original PDF to the bill
                    return created_invoice.to_dict() # Return the created invoice details
                else:
                    logger.error(f"Failed to create draft bill in Xero. Response indicates errors: {created_invoice.validation_errors}")
                    return None
            else:
                logger.error(f"Failed to create draft bill in Xero. Unexpected response: {created_invoices_response}")
                return None

        except AccountingBadRequestException as e:
             logger.exception(f"Bad Request Error creating Xero bill: Status {e.status}, Body: {e.body}")
             # Try to log specific validation errors if possible
             try:
                 error_details = json.loads(e.body)
                 logger.error(f"Xero Validation Errors: {error_details.get('Elements', [])}")
             except:
                 pass
             return None
        except ApiException as e:
            # Handle potential token expiry error not caught by pre-check/refresh
            if e.status == 401: # Unauthorized
                 logger.warning(f"Xero API returned 401 Unauthorized. Token might be invalid or expired despite checks. Status: {e.status}")
                 # Optionally attempt one more refresh? Or just fail.
            else:
                 logger.exception(f"API Error creating Xero bill: Status {e.status}, Body: {e.body}")
            return None
        except TokenExpiredError: # Should be caught by _ensure_token_valid ideally
             logger.warning("Xero token expired during operation (TokenExpiredError caught).")
             return None # Rely on the next call to trigger refresh
        except Exception as e:
            logger.exception(f"An unexpected error occurred creating Xero draft bill: {e}")
            return None

# --- Service Factory (similar to other services) ---
def get_xero_service() -> XeroService:
    """Factory function to get the configured Xero service implementation."""
    # Add logic here if supporting multiple Xero service types in future
    # For now, directly instantiate XeroPythonService
    
    # Perform initial checks for essential config
    if not settings.XERO_CLIENT_ID or not settings.XERO_CLIENT_SECRET or not settings.XERO_REDIRECT_URI or not settings.XERO_SCOPES:
        logger.warning("Core Xero OAuth credentials (ID, Secret, Redirect URI, Scopes) not configured. XeroService will likely fail.")
        # Return a dummy/null service or raise ConfigurationError?
        # For now, let it proceed but methods will likely fail.
        pass # Allow init, it will log error
        
    return XeroPythonService()
