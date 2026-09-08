FROM python:3.11-slim

# Install system dependencies required for headless Firefox
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 libx11-6 \
    libxcomposite1 libxdamage1 libxext6 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Camoufox browser using python module invocation
RUN python -m camoufox fetch

COPY . .

EXPOSE 10000

# Use standard uvicorn module execution
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
