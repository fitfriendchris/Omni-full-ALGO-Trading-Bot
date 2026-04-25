FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY python/ ./python/
COPY server.py .
COPY config.example.json ./config.example.json

# Create directories
RUN mkdir -p logs shared pine

# Default: run watchdog (overridden per service in docker-compose)
CMD ["python", "python/watchdog.py"]
