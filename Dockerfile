# Use an official Python runtime as a parent image.
FROM python:3.9-slim

# Install system dependencies, including Tesseract OCR and fonts.
RUN apt-get update && \
    apt-get install -y tesseract-ocr libtesseract-dev fonts-dejavu && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory in the container.
WORKDIR /app

# Copy the requirements file and install Python dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code.
COPY . .

# Expose any ports if needed (for example, if your bot uses webhooks).

# Define environment variables (optional) or use a .env file.
# ENV BOT_TOKEN=your_bot_token_here
# ENV API_ID=your_api_id_here
# ENV API_HASH=your_api_hash_here
# ENV TESSERACT_CMD=/usr/bin/tesseract

# Run the bot.
CMD ["python", "main.py"]
