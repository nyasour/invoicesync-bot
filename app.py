import os
import shutil
import tempfile
import logging
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from pydantic import BaseModel

from services.ocr import MistralOCR, ExtractedInvoiceData
from services.categorization import InvoiceCategorizer, CategorizationResult
from config import settings # Ensure settings are loaded

import requests # Import requests
import aiohttp # Add aiohttp import

# Import Slack Bolt components
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp

from services.xero_service import get_xero_service, XeroService, XeroApiException  # Added for Xero
# from services.xero_models import XeroApiException # Removed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Invoice Processing API")

class ProcessedInvoiceResponse(BaseModel):
    ocr_result: Optional[ExtractedInvoiceData] = None
    categorization_result: Optional[CategorizationResult] = None
    error: Optional[str] = None

# Initialize services (consider dependency injection for larger apps)
try:
    ocr_service = MistralOCR()
except Exception as e:
    logger.error(f"Failed to initialize OCR service: {e}")
    ocr_service = None

# --- Initialize Categorization Service ---
logger.info("Attempting to initialize InvoiceCategorizer...")
categorization_service = None # Initialize to None
try:
    categorization_service = InvoiceCategorizer()
    if categorization_service:
        logger.info("InvoiceCategorizer initialized successfully.")
    else:
        # This case shouldn't happen if InvoiceCategorizer() constructor doesn't return None
        logger.warning("InvoiceCategorizer() returned None without raising exception?")
except Exception as e:
    # Log exception type and message more explicitly
    logger.error("!!! EXCEPTION DURING InvoiceCategorizer INITIALIZATION !!!")
    logger.error(f"Exception Type: {type(e).__name__}")
    logger.error(f"Exception Args: {e.args}")
    logger.exception("Full traceback for Categorization service initialization failure:")
    categorization_service = None # Ensure it's None on failure

# --- Initialize Xero Service ---
logger.info("Attempting to initialize XeroService...")
xero_service: Optional[XeroService] = None # Initialize to None
try:
    # Check required config *before* attempting to create
    if settings.XERO_CLIENT_ID and settings.XERO_CLIENT_SECRET and settings.XERO_REDIRECT_URI:
        xero_service = create_xero_service()
        if xero_service:
             logger.info("XeroService initialized successfully.")
        else:
            logger.warning("create_xero_service() returned None without raising exception?")
    else:
        logger.warning("Missing required Xero configuration (Client ID, Secret, Redirect URI). Xero service disabled.")
except Exception as e:
    logger.error("!!! EXCEPTION DURING XeroService INITIALIZATION !!!")
    logger.error(f"Exception Type: {type(e).__name__}")
    logger.error(f"Exception Args: {e.args}")
    logger.exception("Full traceback for Xero service initialization failure:")
    xero_service = None # Ensure it's None on failure

# --- Initialize Slack Bolt App ---
# Ensure SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET are loaded via settings
if not settings.SLACK_BOT_TOKEN or not settings.SLACK_SIGNING_SECRET:
    logger.warning("SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET not found. Slack integration will be disabled.")
    bolt_app = None
    app_handler = None
else:
    try:
        bolt_app = AsyncApp(
            token=settings.SLACK_BOT_TOKEN,
            signing_secret=settings.SLACK_SIGNING_SECRET
        )
        app_handler = AsyncSlackRequestHandler(bolt_app)
        logger.info("Slack Bolt app initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing Slack Bolt app: {e}", exc_info=True)
        bolt_app = None
        app_handler = None

# --- Slack Event Handlers ---
if bolt_app:
    @bolt_app.event("file_shared")
    async def handle_file_shared(body, say, logger):
        """Handles the file_shared event when a file is uploaded to Slack."""
        file_info = body.get('event', {}).get('file', {})
        file_id = file_info.get('id')
        file_name = file_info.get('name')
        user_id = body.get('event', {}).get('user_id')
        channel_id = body.get('event', {}).get('channel_id')
        # Use event_ts as the primary identifier for threading if available, fallback to ts
        thread_ts = body.get('event', {}).get('event_ts', body.get('event', {}).get('ts'))

        logger.info(f"Received file_shared event: File ID={file_id}, Name={file_name}, User={user_id}, Channel={channel_id}, ThreadTS={thread_ts}")

        # Define target channel ID from settings
        target_channel_id = settings.SLACK_TARGET_CHANNEL_ID

        # Validate services are available
        if not ocr_service:
            logger.error("OCR Service not available. Cannot process file.")
            await say(text="Sorry, the OCR service is currently unavailable.", thread_ts=thread_ts)
            return
        if not categorization_service:
            logger.error("Categorization Service not available. Cannot process file.")
            await say(text="Sorry, the categorization service is currently unavailable.", thread_ts=thread_ts)
            return
        # Xero service is optional for processing, but log if unavailable for this flow
        if not xero_service:
             logger.warning("Xero Service not available. Will skip Xero integration.")
             # Optionally inform user if Xero step is expected
             # await say(text="Note: Xero integration is currently unavailable.", thread_ts=thread_ts)


        # --- Start Invoice Processing ---
        temp_dir = None
        file_path = None
        ocr_data: Optional[ExtractedInvoiceData] = None
        categorization_data: Optional[CategorizationResult] = None
        xero_result_message: Optional[str] = None

        try:
            # 1. Get file info using the client from the 'say' utility
            # Use the bot token associated with the bolt app instance
            file_info_resp = await bolt_app.client.files_info(file=file_id)
            if not file_info_resp.get("ok"):
                logger.error(f"Failed to get file info for {file_id}: {file_info_resp.get('error')}")
                await say(text=f"Sorry, I couldn't get the details for file ID `{file_id}`. Error: `{file_info_resp.get('error')}`", thread_ts=thread_ts)
                return

            file_data = file_info_resp.get("file")
            download_url = file_data.get("url_private_download")
            original_filename = file_data.get("name", "downloaded_file") # Use original filename

            if not download_url:
                logger.error(f"No download URL found for file {file_id}")
                await say(text=f"Sorry, I couldn't find a download URL for file `{file_id}`.", thread_ts=thread_ts)
                return

            # 2. Download the file using aiohttp
            temp_dir = tempfile.mkdtemp()
            # Use the original filename for the temporary file
            file_path = os.path.join(temp_dir, original_filename)
            logger.info(f"Attempting to download file to: {file_path}")

            headers = {"Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}"}
            async with aiohttp.ClientSession() as session:
                async with session.get(download_url, headers=headers) as resp:
                    if resp.status == 200:
                        with open(file_path, 'wb') as f:
                            while True:
                                chunk = await resp.content.read(1024)
                                if not chunk:
                                    break
                                f.write(chunk)
                        logger.info(f"Successfully downloaded file {file_id} to {file_path}")
                    else:
                        error_content = await resp.text()
                        logger.error(f"Failed to download file {file_id}. Status: {resp.status}. Response: {error_content}")
                        await say(text=f"Sorry, I couldn't download the file `{original_filename}` (Status: {resp.status}).", thread_ts=thread_ts)
                        return # Stop processing if download fails

            # 3. Perform OCR
            logger.info(f"Starting OCR process for {file_path}")
            with open(file_path, "rb") as f:
                file_content = f.read()

            # Call OCR service extract method
            ocr_data = ocr_service.extract(file_content=file_content, filename=original_filename)

            if not ocr_data or not ocr_data.vendor_name: # Basic check if OCR yielded *something*
                 logger.warning(f"OCR extraction yielded minimal or no data for {original_filename}.")
                 await say(text=f"I couldn't extract much information from `{original_filename}` using OCR.", thread_ts=thread_ts)
                 # Decide if you want to stop or continue to categorization attempt
                 # return # Optional: Stop if OCR fails significantly

            # 4. Perform Categorization
            if ocr_data: # Only categorize if we have some OCR data
                 logger.info(f"Starting categorization for {original_filename}...")
                 categorization_data = categorization_service.categorize(ocr_data)
                 logger.info(f"Categorization Result for {original_filename}: {categorization_data}")
            else:
                 logger.warning(f"Skipping categorization for {original_filename} due to lack of OCR data.")


            # 5. --- Xero Integration (Optional based on availability and results) ---
            if xero_service and categorization_data and categorization_data.status == 'matched' and categorization_data.assigned_category:
                logger.info(f"Attempting Xero integration for {original_filename}...")
                assigned_category = categorization_data.assigned_category
                account_code = settings.XERO_ACCOUNT_CODE_MAP.get(assigned_category)

                if account_code:
                    logger.info(f"Found Xero account code '{account_code}' for category '{assigned_category}'.")
                    try:
                        xero_result = await xero_service.create_draft_bill(
                            invoice_data=ocr_data,
                            account_code=account_code,
                            contact_name=ocr_data.vendor_name # Use vendor name as contact
                        )
                        if xero_result and xero_result.get("Id"):
                            bill_id = xero_result["Id"]
                            # Construct deep link (adjust URL based on actual Xero structure if needed)
                            # Assuming a standard pattern, but might need verification
                            deep_link_url = f"https://go.xero.com/organisationlogin/default.aspx?shortcode=!2Account&redirecturl=/AccountsPayable/Edit.aspx?InvoiceID={bill_id}"
                            xero_result_message = f"✅ Successfully created draft bill in Xero: {deep_link_url}"
                            logger.info(f"Successfully created draft bill in Xero with ID: {bill_id}")
                        else:
                            xero_result_message = "⚠️ Created bill in Xero, but couldn't confirm details or get ID."
                            logger.warning(f"Xero draft bill created for {original_filename}, but response format was unexpected: {xero_result}")

                    except XeroApiException as xe:
                        logger.error(f"Xero API Error creating draft bill for {original_filename}: {xe.message} (Status: {xe.status_code}, Details: {xe.details})", exc_info=True)
                        xero_result_message = f"❌ Failed to create draft bill in Xero: {xe.message}"
                        # Optionally provide more details based on xe.details if safe
                    except Exception as e:
                        logger.exception(f"Unexpected error during Xero draft bill creation for {original_filename}: {e}")
                        xero_result_message = f"❌ An unexpected error occurred while creating the Xero draft bill."
                else:
                    logger.warning(f"No Xero account code found in map for category '{assigned_category}'. Skipping Xero bill creation.")
                    xero_result_message = f"ℹ️ Category '{assigned_category}' found, but no matching Xero account code is configured."


            # 6. Construct and Send Final Slack Message
            # Post results to the target channel, in a thread under the original file share message
            if target_channel_id:
                message_blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"📄 Processed Invoice: *{original_filename}*"}}
                ]
                # Add OCR details if available
                if ocr_data:
                    ocr_details = f"*Vendor:* {ocr_data.vendor_name or '_Not Found_'}\n" \
                                  f"*Amount:* {ocr_data.total_amount or '_Not Found_'}\n" \
                                  f"*Date:* {ocr_data.invoice_date or '_Not Found_'}\n" \
                                  f"*Due Date:* {ocr_data.due_date or '_Not Found_'}"
                    message_blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": ocr_details}})
                else:
                     message_blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_OCR failed to extract details._"}})

                # Add Categorization details if available
                if categorization_data:
                    cat_status_emoji = {
                        "matched": "✅",
                        "not_matched": "❓",
                        "error": "❌"
                    }.get(categorization_data.status, "❓")
                    cat_details = f"{cat_status_emoji} *Category:* {categorization_data.assigned_category or '_Not Assigned_'}\n" \
                                  f"*Notes:* {categorization_data.notes or '_None_'}"
                    message_blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": cat_details}})
                else:
                     message_blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_Categorization was not performed._"}})

                # Add Xero result message if available
                if xero_result_message:
                    message_blocks.append({"type": "divider"})
                    message_blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": xero_result_message}})


                # Send the message to the target channel, threaded to the original upload
                try:
                    await bolt_app.client.chat_postMessage(
                        channel=target_channel_id,
                        blocks=message_blocks,
                        text=f"Invoice Processed: {original_filename}", # Fallback text
                        thread_ts=thread_ts # Thread the reply under the original file share event
                    )
                    logger.info(f"Posted processing results for {original_filename} to channel {target_channel_id} in thread {thread_ts}")
                except Exception as slack_err:
                    logger.error(f"Failed to post results message to Slack channel {target_channel_id}: {slack_err}", exc_info=True)
                    # Maybe try a simpler message as fallback?
                    await say(text=f"Finished processing {original_filename}, but couldn't post detailed results to the target channel.", thread_ts=thread_ts)

            else:
                logger.warning("SLACK_TARGET_CHANNEL_ID not configured. Cannot post results.")
                # Optionally, reply in the original channel if no target is set, though this might be noisy.
                # await say(text=f"Processed {original_filename}. Configure SLACK_TARGET_CHANNEL_ID to see detailed results.", thread_ts=thread_ts)


        except aiohttp.ClientError as e:
             logger.error(f"Network error downloading file {file_id}: {e}", exc_info=True)
             await say(text=f"Sorry, there was a network error trying to download `{original_filename}`.", thread_ts=thread_ts)
        except Exception as e:
            logger.exception(f"Unhandled error processing file {file_id} ({original_filename}): {e}")
            await say(text=f"Sorry, an unexpected error occurred while processing `{original_filename}`. Please check the logs.", thread_ts=thread_ts)

        finally:
            # Clean up the temporary file and directory
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info(f"Removed temporary file: {file_path}")
                except OSError as e:
                    logger.error(f"Error removing temporary file {file_path}: {e}")
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Removed temporary directory: {temp_dir}")
                except OSError as e:
                    logger.error(f"Error removing temporary directory {temp_dir}: {e}")

        # No ack() needed for events API

# --- FastAPI Endpoints ---
@app.post("/slack/events")
async def slack_events_endpoint(req: Request):
    """Endpoint to receive events from Slack."""
    if not app_handler:
        logger.error("Slack app handler not initialized. Cannot process Slack event.")
        raise HTTPException(status_code=500, detail="Slack integration not configured")
    return await app_handler.handle(req)

@app.post("/process-invoice", response_model=ProcessedInvoiceResponse)
async def process_invoice(file: UploadFile = File(...)):
    """
    Accepts an invoice file (PDF), performs OCR, categorizes the extracted data,
    and returns both results.
    """
    if not ocr_service:
        raise HTTPException(status_code=500, detail="OCR Service not available")
    if not categorization_service:
        raise HTTPException(status_code=500, detail="Categorization Service not available")

    # Ensure temp directory exists
    temp_dir = tempfile.mkdtemp()
    file_path = os.path.join(temp_dir, file.filename)
    response = ProcessedInvoiceResponse()

    try:
        # Save uploaded file temporarily
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.info(f"Temporarily saved invoice to: {file_path}")

        # 1. Perform OCR
        logger.info("Starting OCR extraction...")
        # Read file content as bytes
        with open(file_path, "rb") as f:
            file_content = f.read()

        # Call extract with both content and filename
        response.ocr_result = ocr_service.extract(file_content=file_content, filename=file.filename)

        if not response.ocr_result:
            logger.warning(f"OCR extraction returned no result for {file.filename}")
        if not response.ocr_result or not response.ocr_result.vendor_name: # Check if OCR yielded data
             logger.warning("OCR did not extract sufficient data.")
             response.error = "OCR failed to extract sufficient data from the invoice."
             # Decide if you want to stop here or still try categorization
             # For now, let's attempt categorization even with partial data

        # 2. Perform Categorization (only if OCR was somewhat successful)
        if response.ocr_result:
            logger.info("Starting categorization...")
            response.categorization_result = categorization_service.categorize(response.ocr_result)
            logger.info(f"Categorization Result: {response.categorization_result}")
        else:
             logger.warning("Skipping categorization due to lack of OCR data.")
             if not response.error:
                response.error = "Skipping categorization due to lack of OCR data."

    except Exception as e:
        logger.exception(f"Error processing invoice {file.filename}: {e}")
        # Use HTTPException for client/server errors, keep generic error for unexpected ones
        response.error = f"An unexpected error occurred: {str(e)}"
        # Consider re-raising HTTPException for specific known errors

    finally:
        # Clean up the temporary file and directory
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Removed temporary file: {file_path}")
        if os.path.exists(temp_dir):
            os.rmdir(temp_dir) # Only removes if empty
            logger.info(f"Removed temporary directory: {temp_dir}")
        # Ensure file handle is closed
        await file.close()

    return response

@app.get("/health")
def health_check():
    """Basic health check endpoint."""
    return {"status": "ok",
            "ocr_service_available": ocr_service is not None,
            "categorization_service_available": categorization_service is not None,
            "xero_service_available": xero_service is not None # Added Xero status
            }

if __name__ == "__main__":
    import uvicorn
    # Note: Running directly like this is mainly for simple testing.
    # Production deployments usually use Gunicorn + Uvicorn workers.
    uvicorn.run(app, host="127.0.0.1", port=8000)
