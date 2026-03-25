FROM python:3.13-slim

# Install system dependencies for psycopg2
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY alert/ alert/
COPY scripts/ scripts/

# Create data directory
RUN mkdir -p alert/data

CMD ["python", "-m", "alert.main", "--daemon"]
