import os
import shutil
import tempfile
import logging
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException
from pydantic import BaseModel

from services.ocr import MistralOCR, ExtractedInvoiceData
from services.categorization import InvoiceCategorizer, CategorizationResult
from config import settings # Ensure settings are loaded

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
