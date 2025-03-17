import os

# Bot credentials. Preferably, set these as environment variables in production.
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_bot_token_here")
API_ID = int(os.getenv("API_ID", "1234567"))
API_HASH = os.getenv("API_HASH", "your_api_hash_here")

# Path to the Tesseract executable.
# On a Linux VPS, this is usually installed at /usr/bin/tesseract.
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "/usr/bin/tesseract")
