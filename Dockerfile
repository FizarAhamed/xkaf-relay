FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir telethon aiohttp

COPY xkaf_telegram_relay.py .
COPY xkaf_relay_config.json* .
COPY xkaf_user.session* .

EXPOSE 8080

CMD ["python", "xkaf_telegram_relay.py"]
