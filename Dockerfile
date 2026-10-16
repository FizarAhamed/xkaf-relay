FROM python:3.11-slim

WORKDIR /app

# Prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install dependencies directly
RUN pip install --no-cache-dir telethon aiohttp

# Copy application script
COPY xkaf_telegram_relay.py .

EXPOSE 8080

CMD ["python", "xkaf_telegram_relay.py"]
