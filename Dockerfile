# Stop Loss Guardian - Capital Protection Service
# The platform's #1 job is keeping losses small.

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY stop_loss_guardian/ ./stop_loss_guardian/

# Run the guardian
CMD ["python", "-m", "stop_loss_guardian.main"]
