FROM python:3.11-slim

# Install system dependencies required for headless Firefox & Camoufox (GTK, X11, Display drivers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg \
    libgtk-3-0 libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 libx11-6 \
    libxcomposite1 libxdamage1 libxext6 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Camoufox browser binaries and dependencies
RUN python -m playwright install-deps firefox || true
RUN python -m camoufox fetch

# Copy the rest of the application code
COPY . .

# Expose default port
EXPOSE 10000

# Start Uvicorn pointing to app:app and reading Render's PORT variable
CMD ["sh", "-c", "python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
