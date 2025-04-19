# InvoiceSync Bot

## Overview

InvoiceSync Bot is an application designed to automate the processing of PDF invoices received via Slack. It extracts key information using OCR, categorizes the expense using AI, (planned) creates a draft bill in Xero with the PDF attached, and notifies the user in Slack.

This streamlines the accounts payable workflow by reducing manual data entry and categorization effort.

## Features

*   **Slack Integration:** Listens for `file_shared` events in Slack channels where the bot is present.
*   **File Processing:** Filters for supported file types (PDF, PNG, JPG, etc.) shared in Slack, downloads them securely.
*   **OCR Data Extraction:** Uses Mistral AI (via `MistralOCR` service) to extract data like vendor name, invoice number, issue date, and total amount from the file content.
*   **AI-Powered Categorization:** Uses OpenAI's GPT models (via `OpenAICategorizer` service) to suggest an expense category based on the extracted invoice data and a predefined list of allowed categories.
*   **User Notifications:** Provides feedback to the user in Slack about the processing status (starting, success with details, failure).
*   **(Planned) Xero Integration:** Creates a draft "Bill" (Accounts Payable) in Xero (via `XeroService`) containing the extracted details, category (mapped to a Xero Account Code), and attaches the original PDF.
*   **Web API:** Includes a FastAPI endpoint (`/process-invoice`) for direct invoice processing via HTTP POST.
*   **Modular Design:** Core functionalities (OCR, Categorization, Xero) are implemented as swappable services with clear interfaces, making it easier to adapt or extend.
*   **Configuration Management:** Uses environment variables (`.env` file for local development) and optionally Google Secret Manager for sensitive credentials and settings.
*   **(Planned) Serverless Deployment:** Designed to be deployed to Google Cloud Run.

## Architecture

The bot follows a modular architecture built with FastAPI and Slack Bolt:

1.  **Web Server (`app.py`):** Handles incoming Slack events using `slack-bolt` integrated with FastAPI. Downloads files, orchestrates the workflow, and exposes API endpoints.
2.  **Configuration (`config.py`):** Loads settings from environment variables or Google Secret Manager.
3.  **Services (`services/`):**
    *   `ocr.py`: Defines the `OCRService` interface and `MistralOCR` implementation.
    *   `categorize.py`: Defines the `CategorizationService` interface and `OpenAICategorizer` implementation.
    *   `(Planned) xero.py`: Defines the `XeroService` implementation for interacting with the Xero API.
4.  **Deployment (Planned):** Designed for containerization and deployment to Google Cloud Run.

## Directory Structure

```
invoicesync-bot/
├── .env               # Local environment variables (Add to .gitignore!)
├── .env.sample        # Example environment variables file
├── .gitignore         # Files/directories to ignore in Git
├── app.py             # Main application logic, FastAPI server, Slack event handlers
├── config.py          # Configuration loading and validation
├── requirements.txt   # Production dependencies
├── services/          # Core service implementations
│   ├── __init__.py
│   ├── ocr.py         # OCR service interface and implementation
│   ├── categorize.py  # Categorization service interface and implementation
│   └── (planned) xero.py # Xero integration service
└── (planned) tests/   # Unit tests
    ├── __init__.py
    # Test files like test_config.py, test_ocr.py etc. will go here
```

## Project Status (As of 2025-04-19)

*   **Implemented:**
    *   Core FastAPI application setup.
    *   Configuration loading (`config.py`).
    *   OCR service using Mistral AI (`services/ocr.py`).
    *   Categorization service using OpenAI (`services/categorization.py`).
    *   `/process-invoice` API endpoint for direct uploads.
    *   Slack event handler (`file_shared`) for processing invoices uploaded to Slack channels.
    *   Secure file downloading from Slack.
    *   Sending results back to the originating Slack thread.
    *   Basic logging.
*   **Next Steps:**
    *   Xero Integration.
    *   Deployment to GCP Cloud Run.

## Roadmap

### Phase 1: Xero Integration

*   **Goal:** Automatically create a Draft Bill in Xero upon successful invoice processing.
*   **Tasks:**
    *   Implement OAuth 2.0 authentication flow for Xero.
    *   Securely store and manage Xero API tokens (using GCP Secret Manager in deployment).
    *   Add Xero client library (`pyxero` or similar) to `requirements.txt`.
    *   Develop `services/xero_service.py`:
        *   Find/Create Xero Contacts based on vendor name.
        *   Map internal categories to Xero Account Codes (requires configuration).
        *   Implement `create_draft_bill` function.
        *   Handle token refresh.
    *   Integrate `create_draft_bill` call into `app.py` workflow.
    *   Update Slack notifications with Xero status.
    *   Add robust error handling for Xero API interactions.

### Phase 2: GCP Cloud Run Deployment

*   **Goal:** Deploy the application reliably and securely on Google Cloud.
*   **Tasks:**
    *   Create `Dockerfile` to containerize the application.
    *   Configure application to run via Uvicorn, listening on `0.0.0.0:$PORT`.
    *   Set up GCP Artifact Registry for container images.
    *   Set up GCP Secret Manager for all sensitive credentials.
    *   Grant Cloud Run service account access to Secret Manager.
    *   Create `cloudbuild.yaml` for automated build & deploy pipeline triggered by Git.
    *   Update Slack App Request URL to point to the Cloud Run service URL.
    *   Configure Cloud Logging and Monitoring.

## Setup and Installation

1.  **Clone the Repository:** (Assuming you have the code)
2.  **Navigate to the Directory:**
    ```bash
    cd path/to/accounting-ai/invoicesync-bot
    ```
3.  **Create Virtual Environment:**
    ```bash
    python3 -m venv venv
    ```
4.  **Activate Virtual Environment:**
    ```bash
    source venv/bin/activate
    ```
    *(On Windows use: `venv\Scripts\activate`)*
5.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
6.  **Configure Environment Variables:**
    *   Copy `.env.sample` to a new file named `.env`.
    *   **Fill in the required values** in the `.env` file (API keys for Slack, Mistral, OpenAI; `SLACK_TARGET_CHANNEL_ID`, etc.).
    *   **Important:** Ensure the `.env` file is listed in your `.gitignore`.

## Running Locally

1.  **Ensure `.env` is configured.**
2.  **Activate the virtual environment (`source venv/bin/activate`).**
3.  **Run the FastAPI server using Uvicorn:**
    ```bash
    # Example using port 8003 with auto-reload
    uvicorn app:app --reload --port 8003
    ```
4.  **Use ngrok (or similar) to expose the local server:** The Uvicorn server runs locally (e.g., `http://127.0.0.1:8003`). You need a tool like `ngrok` to create a public HTTPS URL that tunnels to your local port.
    ```bash
    ngrok http 8003
    ```
5.  **Update Slack App Configuration:** In your Slack App settings (Features -> Event Subscriptions), set the Request URL to the public HTTPS URL provided by ngrok (e.g., `https://<your-ngrok-subdomain>.ngrok.io/slack/events`).
6.  **Test:** Upload a supported file to a channel where the bot is present.

## Deployment (Planned - GCP Cloud Run)

Deployment will use Google Cloud Run. Key steps are outlined in the Roadmap section and involve containerization, Secret Manager, Artifact Registry, and Cloud Build.

## Environment Variables

Refer to the `.env.sample` file for a detailed explanation of each environment variable required for configuration and authentication.

## Testing (Planned)

Unit tests are planned and will be located in the `tests/` directory. They will likely utilize `pytest` and `pytest-mock`.
