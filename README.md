# InvoiceSync Bot

## Overview

InvoiceSync Bot is a serverless application designed to automate the processing of PDF invoices received via Slack. It extracts key information using OCR, categorizes the expense using AI, creates a draft bill in Xero with the PDF attached, and notifies the user in Slack.

This streamlines the accounts payable workflow by reducing manual data entry and categorization effort.

## Features

*   **Slack Integration:** Listens for `file_shared` events in Slack channels where the bot is present.
*   **PDF Processing:** Filters for PDF files shared in Slack.
*   **OCR Data Extraction:** Uses Mistral AI (via `MistralOCR` service) to extract data like vendor name, invoice number, issue date, and total amount from the PDF content.
*   **AI-Powered Categorization:** Uses OpenAI's GPT models (via `OpenAICategorizer` service) to suggest an expense category based on the extracted invoice data and a predefined list of allowed categories.
*   **Xero Integration:** Creates a draft "Bill" (Accounts Payable) in Xero (via `XeroService`) containing the extracted details, category (mapped to a Xero Account Code), and attaches the original PDF.
*   **User Notifications:** Provides feedback to the user in Slack about the processing status (starting, success with details, failure).
*   **Modular Design:** Core functionalities (OCR, Categorization, Xero) are implemented as swappable services with clear interfaces, making it easier to adapt or extend.
*   **Configuration Management:** Uses environment variables (`.env` file for local development) and optionally Google Secret Manager for sensitive credentials and settings.
*   **Serverless Deployment:** Designed to be deployed as a Google Cloud Function triggered by Slack events.

## Architecture

The bot follows a modular architecture:

1.  **Slack Interface (`main.py`):** Handles incoming Slack events using `slack-bolt`, downloads files, and orchestrates the workflow.
2.  **Configuration (`config.py`):** Loads settings from environment variables or Google Secret Manager.
3.  **Services (`services/`):**
    *   `ocr.py`: Defines the `OCRService` interface and `MistralOCR` implementation.
    *   `categorize.py`: Defines the `CategorizationService` interface and `OpenAICategorizer` implementation.
    *   `xero.py`: Defines the `XeroService` implementation for interacting with the Xero API.
4.  **Temporary Storage (Optional):** Uses Google Cloud Storage (`google-cloud-storage`) to temporarily store downloaded PDFs during processing, especially useful in serverless environments.
5.  **Deployment (`main.py`):** Includes an entry point (`slack_events`) compatible with Google Cloud Functions HTTP triggers, adapted using `SlackRequestHandler`.

## Directory Structure

```
invoicesync-bot/
├── .env               # Local environment variables (Add to .gitignore!)
├── .gitignore         # Files/directories to ignore in Git
├── main.py            # Main application logic, Slack event handler, GCF entrypoint
├── config.py          # Configuration loading and validation
├── requirements.txt   # Production dependencies
├── requirements-dev.txt # Development/testing dependencies
├── services/          # Core service implementations
│   ├── __init__.py
│   ├── ocr.py         # OCR service interface and implementation
│   ├── categorize.py  # Categorization service interface and implementation
│   └── xero.py        # Xero integration service
└── tests/             # Unit tests (structure setup, tests to be added)
    ├── __init__.py
    # Test files like test_config.py, test_ocr.py etc. will go here
```

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
    pip install -r requirements.txt -r requirements-dev.txt
    ```
6.  **Configure Environment Variables:**
    *   Copy the contents of `.env.example` (or the one created previously) to a new file named `.env`.
    *   **Fill in the required values** in the `.env` file, such as API keys (Slack, Mistral, OpenAI, Xero) and other configurations. Pay close attention to the comments explaining each variable.
    *   **Important:** Ensure the `.env` file is listed in your `.gitignore` to avoid committing secrets.

## Running Locally (Socket Mode)

For local development and testing without deploying, you can use Slack's Socket Mode:

1.  **Enable Socket Mode:** In your Slack App configuration dashboard, enable Socket Mode and generate an App-Level Token (starts with `xapp-`).
2.  **Add Token to `.env`:** Add the `SLACK_APP_TOKEN` to your `.env` file.
3.  **Uncomment Local Run Block:** In `main.py`, uncomment the `if __name__ == "__main__":` block at the end of the file.
4.  **Run the App:**
    ```bash
    python main.py
    ```
    The bot will connect to Slack via Socket Mode and start listening for events. You can then share PDF files in a channel where the bot is present to test the flow.

## Deployment (Google Cloud Functions)

The application is designed for deployment on Google Cloud Functions:

1.  **Prerequisites:**
    *   Google Cloud Project setup.
    *   `gcloud` CLI installed and configured.
    *   Secrets stored in Google Secret Manager (if `SECRET_MANAGER_ENABLED=true`).
    *   Service Account with necessary permissions (Secret Manager Accessor, Storage Object Admin if using GCS).
2.  **Deployment Command (Example):**
    ```bash
    gcloud functions deploy slack_events \
        --runtime python39 \
        --trigger-http \
        --entry-point slack_events \
        --source . \
        --region YOUR_GCP_REGION \
        --service-account YOUR_SERVICE_ACCOUNT_EMAIL \
        --set-env-vars GCP_PROJECT_ID=YOUR_GCP_PROJECT_ID,SECRET_MANAGER_ENABLED=true # Add other env vars if not using Secret Manager
        # Add --allow-unauthenticated if Slack needs to call the function directly without IAM auth
    ```
3.  **Slack Event Subscription:** Update your Slack App's Event Subscriptions Request URL to point to the deployed Cloud Function's trigger URL.

## Environment Variables

Refer to the comments in the `.env` file for a detailed explanation of each environment variable required for configuration and authentication. Key variables include API keys/secrets for Slack, Mistral, OpenAI, Xero, and configuration for GCS, Secret Manager, categories, and account codes.

## Testing

Unit tests are planned and will be located in the `tests/` directory. They utilize `pytest` and `pytest-mock`. To run tests (once implemented):

```bash
pytest
