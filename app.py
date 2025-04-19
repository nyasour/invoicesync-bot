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
        # TODO: Implement actual file processing logic here
        file_info = body.get('event', {}).get('file', {})
        file_id = file_info.get('id')
        file_name = file_info.get('name')
        user_id = body.get('event', {}).get('user_id')
        channel_id = body.get('event', {}).get('channel_id')
        thread_ts = body.get('event', {}).get('thread_ts')

        logger.info(f"Received file_shared event: File ID={file_id}, Name={file_name}, User={user_id}, Channel={channel_id}")

        # --- Start Invoice Processing --- 
        try:
            # 1. Get file info using the client from the 'say' utility
            file_info_resp = await say.client.files_info(file=file_id)
            if not file_info_resp.get("ok"): 
                logger.error(f"Failed to get file info for {file_id}: {file_info_resp.get('error')}")
                await say(f"Sorry, I couldn't get the details for file ID {file_id}. Error: {file_info_resp.get('error')}")
                return
            
            file_data = file_info_resp.get("file")
            file_url = file_data.get("url_private_download")
            file_type = file_data.get("filetype")
            original_filename = file_data.get("name", f"{file_id}_upload") # Use original name or default

            logger.info(f"File Info: URL={file_url}, Type={file_type}, Original Name={original_filename}")

            # 2. Check file type 
            supported_types = ["pdf", "png", "jpg", "jpeg"]
            if file_type not in supported_types:
                logger.info(f"Ignoring file {file_id} ({original_filename}) - unsupported type: {file_type}")
                # Optionally send a message back?
                # await say(f"I can only process PDF, PNG, or JPG files right now. '{original_filename}' is a {file_type}.")
                return
            
            # 3. Download the file content using aiohttp
            bot_token = say.client.token # Get the token associated with the client
            headers = {'Authorization': f'Bearer {bot_token}'}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url, headers=headers) as response:
                    response.raise_for_status() # Raise an exception for bad status codes
                    file_content = await response.read() # Read file content as bytes
                    logger.info(f"Successfully downloaded {len(file_content)} bytes for file {file_id} ({original_filename}).")

                # --- Process with OCR --- 
                ocr_result = None
                with tempfile.NamedTemporaryFile(delete=True, suffix=os.path.splitext(original_filename)[1]) as temp_file:
                    temp_file.write(file_content)
                    temp_file.flush() # Ensure content is written to disk
                    temp_file_path = temp_file.name
                    logger.info(f"Saved downloaded content to temporary file: {temp_file_path}")

                    # Call OCR Service within its own try-except block
                    try:
                        logger.info(f"Starting OCR processing for {original_filename}...")
                        # Read content from temp file as bytes
                        with open(temp_file_path, "rb") as f_in:
                            file_content_for_ocr = f_in.read()
                        
                        # Pass file content and filename to extract method (it's synchronous)
                        ocr_result = ocr_service.extract(file_content=file_content_for_ocr, filename=original_filename)
                        
                        if ocr_result:
                            logger.info(f"OCR processing successful for {original_filename}. Extracted: {ocr_result.dict(exclude_none=True)}") # Exclude None fields
                    except Exception as ocr_error:
                        logger.exception(f"Error during OCR processing for {original_filename}: {ocr_error}")
                        await say(f"Sorry, I encountered an error during OCR for '{original_filename}'. Details: {ocr_error}", thread_ts=thread_ts)
                        # Stop processing if OCR failed
                        return 
                    # temp_file is automatically deleted when 'with' block exits

                # --- Categorize and Respond --- (Ensure this is INSIDE the main try block)
                if ocr_result:
                    categorization_result = None
                    try:
                        logger.info(f"Starting categorization for {original_filename}...")
                        # Call categorization service (synchronous)
                        categorization_result = categorization_service.categorize(ocr_result)
                        
                        # Check for assigned_category when status is 'matched'
                        if categorization_result and categorization_result.status == 'matched' and categorization_result.assigned_category:
                            logger.info(f"Categorization successful for {original_filename}: {categorization_result.dict()}")
                            # Build a more informative message
                            details = [
                                f"Vendor: `{ocr_result.vendor_name or 'N/A'}`",
                                f"Total: `{ocr_result.total_amount or 'N/A'}`",
                                f"Inv #: `{ocr_result.invoice_number or 'N/A'}`",
                                f"Date: `{ocr_result.issue_date or 'N/A'}`"
                            ]
                            final_message = (
                                f"Processed '{original_filename}' (ID: `{file_id}`):\n"
                                f"> {' | '.join(details)}\n" # Join details neatly
                                # Use assigned_category here
                                f"> *Suggested Category: {categorization_result.assigned_category}*"
                            )
                            await say(final_message, thread_ts=thread_ts)
                        else:
                            reason = categorization_result.reason if categorization_result else 'Service error'
                            logger.warning(f"Categorization returned no category for {original_filename}. Reason: {reason}")
                            await say(f"Finished OCR for '{original_filename}', but couldn't determine a category. Reason: `{reason}`", thread_ts=thread_ts)
                    except Exception as cat_error:
                        logger.exception(f"Error during categorization for {original_filename}: {cat_error}")
                        await say(f"Finished OCR for '{original_filename}', but encountered an error during categorization. Details: {cat_error}", thread_ts=thread_ts)
                    else:
                        pass
                # Implicit else: if ocr_result was None, we already sent a message and returned.

        # Outer exception handlers - Restored
        except aiohttp.ClientResponseError as http_err: # Specific handling for HTTP errors during download
             logger.error(f"HTTP error downloading file {file_id} ({original_filename}): Status={http_err.status}, Message='{http_err.message}'")
             await say(f"Sorry <@{user_id}>, I couldn't download '{original_filename}'. Received status {http_err.status} from Slack.", thread_ts=thread_ts)
        except aiohttp.ClientError as e: # Catch other client errors (connection issues, etc.)
            logger.error(f"Network/Client error processing file {file_id} ({original_filename}): {e}")
            await say(f"Sorry <@{user_id}>, I encountered a network issue while trying to process '{original_filename}'. Details: `{e}`", thread_ts=thread_ts)
        except Exception as e: # Generic catch-all for unexpected errors
            # Log user_id and original_filename if available, otherwise use file_id
            user_mention = f"<@{user_id}>" if 'user_id' in locals() and user_id else "User"
            file_desc = f"'{original_filename}' ({file_id})" if 'original_filename' in locals() else f"file ID {file_id}"
            logger.exception(f"An unexpected error occurred processing {file_desc}: {e}") # Log full traceback
            # Avoid using potentially undefined variables directly in say
            await say(f"Sorry {user_mention}, an unexpected internal error occurred while trying to process {file_desc}. Please check the application logs.", thread_ts=thread_ts)

    @bolt_app.event("message")
    async def handle_message_events(body, logger):
        # We can add more specific handling later if needed, e.g., filter by subtype
        event = body.get("event", {})
        logger.info(f"Received message event: {event.get('type')}/{event.get('subtype')}")
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
            "categorization_service_available": categorization_service is not None}


if __name__ == "__main__":
    import uvicorn
    # Note: Running directly like this is mainly for simple testing.
    # Production deployments usually use Gunicorn + Uvicorn workers.
    uvicorn.run(app, host="127.0.0.1", port=8000)
