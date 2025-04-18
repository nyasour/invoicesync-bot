# main.py
import os
import logging
import tempfile
from slack_bolt import App
from slack_bolt.adapter.google_cloud_functions import SlackRequestHandler # For GCF deployment
from google.cloud import storage

# Import project modules
import config
from services.ocr import get_ocr_service
from services.categorize import get_categorization_service
from services.xero import get_xero_service

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Initialize Services ---
# Use factory functions which handle configuration checks internally
ocr_service = get_ocr_service()
categorization_service = get_categorization_service()
xero_service = get_xero_service()
storage_client = None
if config.TEMP_STORAGE_BUCKET_NAME:
    try:
        storage_client = storage.Client()
        logger.info(f"Google Cloud Storage client initialized for bucket: {config.TEMP_STORAGE_BUCKET_NAME}")
    except Exception as e:
        logger.error(f"Failed to initialize Google Cloud Storage client: {e}", exc_info=True)
        # Bot might still function if GCS isn't strictly required, but log critical error
        # Depending on requirements, might want to raise an exception here
else:
    logger.warning("TEMP_STORAGE_BUCKET_NAME not configured. File handling might be limited.")


# --- Initialize Slack App ---
# Use secrets loaded via config module
app = App(
    token=config.SLACK_BOT_TOKEN,
    signing_secret=config.SLACK_SIGNING_SECRET,
    process_before_response=True # Important for Function-as-a-Service environments
)

# --- Helper Functions ---
def download_file_from_slack(file_info: dict, token: str) -> Optional[bytes]:
    """Downloads file content from Slack given file info."""
    url_private = file_info.get('url_private_download')
    if not url_private:
        logger.error("File URL not found in event data.")
        return None

    headers = {'Authorization': f'Bearer {token}'}
    try:
        import requests # Add requests to requirements.txt
        response = requests.get(url_private, headers=headers, stream=True)
        response.raise_for_status() # Raise exception for bad status codes
        logger.info(f"Successfully initiated download for file: {file_info.get('name')}")
        return response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download file from Slack: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred during file download: {e}", exc_info=True)
        return None

def upload_to_gcs(bucket_name: str, file_content: bytes, destination_blob_name: str) -> Optional[str]:
    """Uploads file content to Google Cloud Storage."""
    if not storage_client or not bucket_name:
        logger.error("GCS client or bucket name not configured. Cannot upload.")
        return None
    try:
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(destination_blob_name)
        blob.upload_from_string(file_content, content_type='application/pdf')
        logger.info(f"File uploaded to gs://{bucket_name}/{destination_blob_name}")
        # Return the GCS URI
        return f"gs://{bucket_name}/{destination_blob_name}"
    except Exception as e:
        logger.error(f"Failed to upload file to GCS: {e}", exc_info=True)
        return None

def delete_from_gcs(bucket_name: str, blob_name: str):
    """Deletes a blob from Google Cloud Storage."""
    if not storage_client or not bucket_name:
        logger.warning("GCS client or bucket name not configured. Cannot delete.")
        return
    try:
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.delete()
        logger.info(f"File gs://{bucket_name}/{blob_name} deleted.")
    except Exception as e:
        logger.error(f"Failed to delete file from GCS: {e}", exc_info=True)


# --- Slack Event Handlers ---
@app.event("file_shared")
def handle_file_shared(body: dict, client, ack, say):
    """Handles file_shared events, processing PDF invoices."""
    ack() # Acknowledge the event within 3 seconds

    event = body.get('event', {})
    file_id = event.get('file_id')
    user_id = event.get('user_id') # User who shared the file
    channel_id = event.get('channel_id') # Channel where file was shared

    if not file_id or not user_id or not channel_id:
        logger.error("Received file_shared event with missing data.")
        return

    logger.info(f"Received file_shared event for file_id: {file_id} from user: {user_id} in channel: {channel_id}")

    # 1. Get File Info & Check Type
    try:
        file_info_response = client.files_info(file=file_id)
        if not file_info_response.get('ok'):
            logger.error(f"Failed to get file info for {file_id}: {file_info_response.get('error')}")
            say(text=f"Sorry <@{user_id}>, I couldn't get the details for that file.", channel=channel_id)
            return

        file_info = file_info_response.get('file')
        if not file_info:
             logger.error(f"File object missing in files.info response for {file_id}")
             say(text=f"Sorry <@{user_id}>, I couldn't get the file object details.", channel=channel_id)
             return

        # Ensure it's a PDF
        if file_info.get('filetype') != 'pdf':
            logger.info(f"Ignoring non-PDF file: {file_info.get('name')} ({file_info.get('filetype')})")
            # Optionally inform the user, but might be noisy if many files are shared.
            # say(text=f"<@{user_id}>, I can only process PDF invoices.", channel=channel_id)
            return

        file_name = file_info.get('name', 'unknown_invoice.pdf')
        logger.info(f"Processing PDF file: {file_name}")
        say(text=f"Processing invoice `{file_name}` for you <@{user_id}>...", channel=channel_id)

        # 2. Download File Content
        file_content = download_file_from_slack(file_info, client.token)
        if not file_content:
            say(text=f"Sorry <@{user_id}>, I couldn't download the file `{file_name}`.", channel=channel_id)
            return

        # 3. (Optional but Recommended) Upload to Temp Storage (GCS)
        gcs_blob_name = None
        if storage_client and config.TEMP_STORAGE_BUCKET_NAME:
            # Create a unique blob name, e.g., using file_id or timestamp
            gcs_blob_name = f"invoices/{user_id}/{file_id}-{file_name}"
            gcs_uri = upload_to_gcs(config.TEMP_STORAGE_BUCKET_NAME, file_content, gcs_blob_name)
            if not gcs_uri:
                say(text=f"Sorry <@{user_id}>, I encountered an issue storing the file temporarily.", channel=channel_id)
                # Decide whether to proceed without GCS or stop
                return # Stop processing if GCS upload fails

        # 4. OCR Extraction
        if not ocr_service:
            logger.critical("OCR Service is not available. Cannot process file.")
            say(text=f"Sorry <@{user_id}>, the OCR service isn't configured correctly. Please contact an admin.", channel=channel_id)
            # Clean up GCS file if it was uploaded
            if gcs_blob_name and config.TEMP_STORAGE_BUCKET_NAME:
                delete_from_gcs(config.TEMP_STORAGE_BUCKET_NAME, gcs_blob_name)
            return

        logger.info(f"Starting OCR extraction for '{file_name}'...")
        extracted_data = ocr_service.extract(file_content, file_name)
        if not extracted_data:
            logger.error(f"OCR extraction failed for file: {file_name}")
            say(text=f"Sorry <@{user_id}>, I couldn't extract data from `{file_name}`. The OCR process failed.", channel=channel_id)
            if gcs_blob_name and config.TEMP_STORAGE_BUCKET_NAME: delete_from_gcs(config.TEMP_STORAGE_BUCKET_NAME, gcs_blob_name)
            return
        logger.info(f"OCR extraction successful for '{file_name}'. Vendor: {extracted_data.vendor_name}")

        # 5. Categorization
        if not categorization_service:
            logger.critical("Categorization Service is not available. Cannot process file.")
            say(text=f"Sorry <@{user_id}>, the categorization service isn't configured correctly. Please contact an admin.", channel=channel_id)
            if gcs_blob_name and config.TEMP_STORAGE_BUCKET_NAME: delete_from_gcs(config.TEMP_STORAGE_BUCKET_NAME, gcs_blob_name)
            return

        logger.info(f"Starting categorization for '{file_name}'...")
        categorized_data = categorization_service.categorize(extracted_data)
        if not categorized_data:
            logger.error(f"Categorization failed for file: {file_name}")
            # Inform user, maybe include extracted data if helpful?
            say(text=f"Sorry <@{user_id}>, I couldn't categorize the invoice `{file_name}`.", channel=channel_id)
            if gcs_blob_name and config.TEMP_STORAGE_BUCKET_NAME: delete_from_gcs(config.TEMP_STORAGE_BUCKET_NAME, gcs_blob_name)
            return
        logger.info(f"Categorization successful for '{file_name}'. Category: {categorized_data.category}")

        # 6. Xero Integration
        if not xero_service:
            logger.critical("Xero Service is not available. Cannot create draft expense.")
            say(text=f"Sorry <@{user_id}>, the Xero service isn't configured correctly. Please contact an admin.", channel=channel_id)
            if gcs_blob_name and config.TEMP_STORAGE_BUCKET_NAME: delete_from_gcs(config.TEMP_STORAGE_BUCKET_NAME, gcs_blob_name)
            return

        logger.info(f"Creating draft expense in Xero for '{file_name}'...")
        bill_id = xero_service.create_draft_expense(categorized_data, file_content, file_name)
        if not bill_id:
            logger.error(f"Failed to create draft expense in Xero for file: {file_name}")
            say(text=f"Sorry <@{user_id}>, I couldn't create the draft expense in Xero for `{file_name}`.", channel=channel_id)
            if gcs_blob_name and config.TEMP_STORAGE_BUCKET_NAME: delete_from_gcs(config.TEMP_STORAGE_BUCKET_NAME, gcs_blob_name)
            return

        logger.info(f"Successfully created draft bill in Xero (ID: {bill_id}) for file: {file_name}")

        # 7. Report Success
        # Construct a success message, potentially with a link to the draft bill if possible
        # Basic success message:
        success_message = (
            f"Success! :tada: I've processed `{file_name}` for you <@{user_id}>.\n"
            f"- Vendor: `{categorized_data.vendor_name}`\n"
            f"- Amount: `{categorized_data.total_amount}` {categorized_data.currency or ''}\n"
            f"- Category: `{categorized_data.category}`\n"
            f"- A draft bill has been created in Xero (ID: `{bill_id}`). Please review and approve it."
        )
        say(text=success_message, channel=channel_id)

        # 8. Clean up temporary file from GCS
        if gcs_blob_name and config.TEMP_STORAGE_BUCKET_NAME:
            delete_from_gcs(config.TEMP_STORAGE_BUCKET_NAME, gcs_blob_name)

    except Exception as e:
        logger.error(f"An unexpected error occurred in handle_file_shared for file_id {file_id}: {e}", exc_info=True)
        try:
            # Try to notify the user about the unexpected error
            say(text=f"Sorry <@{user_id}>, an unexpected error occurred while processing `{file_info.get('name', 'the file')}`. Please check the logs or contact an admin.", channel=channel_id)
        except Exception as slack_err:
            logger.error(f"Failed to send error message to Slack: {slack_err}", exc_info=True)
        # Attempt cleanup if possible
        if 'gcs_blob_name' in locals() and gcs_blob_name and config.TEMP_STORAGE_BUCKET_NAME:
             delete_from_gcs(config.TEMP_STORAGE_BUCKET_NAME, gcs_blob_name)


# --- Google Cloud Functions Entrypoint ---
# Use SlackRequestHandler to adapt the Bolt app for GCF HTTP triggers
handler = SlackRequestHandler(app)

# Define the entry point function name expected by GCF (e.g., 'slack_events')
def slack_events(req):
    """Google Cloud Function HTTP Trigger entrypoint."""
    logger.info("Received request for GCF entrypoint 'slack_events'")
    return handler.handle(req)

# --- Local Development ---
# To run locally using Socket Mode (requires slack_bolt[socket_mode]):
# 1. Install: pip install slack_bolt[socket_mode]
# 2. Set SLACK_APP_TOKEN environment variable (starts with xapp-)
# 3. Uncomment and run this block:
# if __name__ == "__main__":
#     from slack_bolt.adapter.socket_mode import SocketModeHandler
#     # Ensure SLACK_APP_TOKEN is set as an environment variable
#     socket_handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
#     logger.info("Starting Socket Mode handler for local development...")
#     socket_handler.start()
