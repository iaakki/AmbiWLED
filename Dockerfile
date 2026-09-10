FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AMBIWLED_CONFIG_DIR=/config

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ambiwled/ ./ambiwled/
COPY probe.py .
COPY healthcheck.py .

VOLUME ["/config"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python healthcheck.py

CMD ["python", "-m", "ambiwled"]
