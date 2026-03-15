FROM python:3.12-slim

LABEL maintainer="shoutcast-relay"
LABEL description="Shoutcast to Icecast 2 stream relay with web UI"

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY relay.py .

# Persistent config volume
VOLUME ["/app/data"]
ENV RELAY_CONFIG=/app/data/config.json

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status')" || exit 1

CMD ["python", "-u", "relay.py"]
