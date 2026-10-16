#!/usr/bin/env python3
"""
xKAF Telegram MTProto Relay for KOReader
========================================
This server connects your personal Telegram user account directly to @xKAF_dl_bot
via Telegram's MTProto protocol (using Telethon).

When KOReader on your Kindle requests a book download:
1. KOReader calls this relay: https://<back4app-url>/download?token=<TOKEN>
2. The relay sends `/start <TOKEN>` to @xKAF_dl_bot from your Telegram account.
3. @xKAF_dl_bot sends back the EPUB/MOBI file.
4. The relay streams the binary file directly back to your Kindle!
"""

import os
import sys
import json
import asyncio
import re
from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xkaf_relay_config.json")
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xkaf_user")

def load_config():
    cfg = {
        "api_id": 2040,
        "api_hash": "b1844f7ea357e827e322493d1b02dd59",
        "port": 8080,
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[!] Error reading config file: {e}")
    # Environment variables override config file
    if os.environ.get("TELEGRAM_API_ID"):
        try:
            cfg["api_id"] = int(os.environ["TELEGRAM_API_ID"])
        except ValueError:
            pass
    if os.environ.get("TELEGRAM_API_HASH"):
        cfg["api_hash"] = os.environ["TELEGRAM_API_HASH"].strip().strip('"\'')
    if os.environ.get("TELEGRAM_STRING_SESSION"):
        cfg["string_session"] = os.environ["TELEGRAM_STRING_SESSION"]
    if os.environ.get("PORT"):
        try:
            cfg["port"] = int(os.environ["PORT"])
        except ValueError:
            pass
    return cfg

def sanitize_string_session(s):
    if not s:
        return None
    s = s.strip().strip('"\'').strip()
    if not s:
        return None
    # Fix base64 padding if needed
    if len(s) > 1 and s.startswith("1"):
        payload = s[1:]
        missing = len(payload) % 4
        if missing:
            payload += "=" * (4 - missing)
        return s[0] + payload
    return s

async def handle_health(request):
    client = request.app.get("telegram_client")
    is_auth = False
    me = None
    if client:
        try:
            is_auth = await client.is_user_authorized()
            if is_auth:
                me = await client.get_me()
        except Exception as e:
            print(f"[!] Error checking client authorization: {e}")

    return web.json_response({
        "status": "ok" if is_auth else "warning",
        "authorized": is_auth,
        "user": me.first_name if me else None,
        "username": me.username if me else None,
        "phone": me.phone if me else None,
        "service": "xkaf-telegram-relay",
        "notice": None if is_auth else "Telegram client not yet authorized. Please verify TELEGRAM_STRING_SESSION.",
    })

def clean_token(raw_token):
    if not raw_token:
        return None
    # If URL like https://api.xkaf.org/d/XYZ or https://t.me/xKAF_dl_bot?start=XYZ
    match = re.search(r"start=([a-zA-Z0-9_\-]+)", raw_token)
    if match:
        return match.group(1)
    match = re.search(r"/d/([a-zA-Z0-9_\-]+)", raw_token)
    if match:
        return match.group(1)
    # Strip any extra query params or whitespace
    raw_token = raw_token.strip().split("&")[0].split("?")[0]
    return raw_token

async def handle_download(request):
    client = request.app.get("telegram_client")
    if not client or not await client.is_user_authorized():
        return web.Response(
            text=json.dumps({"error": "Telegram client not authorized. Please configure TELEGRAM_STRING_SESSION."}),
            status=401,
            content_type="application/json"
        )

    raw_token = request.query.get("token")
    title = request.query.get("title", "book")
    ext = request.query.get("format", "epub").lower()

    token = clean_token(raw_token)
    if not token:
        return web.Response(
            text=json.dumps({"error": "Missing or invalid download token."}),
            status=400,
            content_type="application/json"
        )

    print(f"[*] Received download request for: '{title}' ({ext}) with token: {token}")

    # Set up future for incoming document from @xKAF_dl_bot
    loop = asyncio.get_running_loop()
    doc_future = loop.create_future()

    @client.on(events.NewMessage(chats="xKAF_dl_bot"))
    async def handler(event):
        if event.message and event.message.file:
            print(f"[+] Received book file from @xKAF_dl_bot: {event.message.file.name}")
            if not doc_future.done():
                doc_future.set_result(event.message)

    try:
        # Send /start <token> to @xKAF_dl_bot
        print(f"[*] Sending '/start {token}' to @xKAF_dl_bot...")
        await client.send_message("xKAF_dl_bot", f"/start {token}")

        # Wait for file with 45s timeout
        msg = await asyncio.wait_for(doc_future, timeout=45.0)
    except asyncio.TimeoutError:
        print("[!] Timeout waiting for @xKAF_dl_bot to deliver file.")
        return web.Response(
            text=json.dumps({"error": "Timeout waiting for @xKAF_dl_bot to respond."}),
            status=504,
            content_type="application/json"
        )
    except Exception as e:
        print(f"[!] Error during Telegram interaction: {e}")
        return web.Response(
            text=json.dumps({"error": str(e)}),
            status=500,
            content_type="application/json"
        )
    finally:
        client.remove_event_handler(handler)

    # Download file bytes into buffer
    print("[*] Downloading file bytes from Telegram...")
    file_bytes = await client.download_media(msg, file=bytes)
    if not file_bytes:
        return web.Response(
            text=json.dumps({"error": "Failed to download media bytes."}),
            status=500,
            content_type="application/json"
        )

    file_name = msg.file.name or f"{title}.{ext}"
    print(f"[+] Successfully retrieved '{file_name}' ({len(file_bytes)} bytes). Streaming to Kindle...")

    # Stream back directly to Kindle
    response = web.Response(body=file_bytes)
    response.content_type = "application/epub+zip" if ext == "epub" else "application/octet-stream"
    response.headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
    response.headers["Content-Length"] = str(len(file_bytes))
    return response

async def init_app():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/download", handle_download)
    return app

async def interactive_login(client):
    await client.connect()
    if not await client.is_user_authorized():
        print("\n" + "="*50)
        print("  xKAF Telegram Relay - First Time Login")
        print("="*50)
        phone = input("Enter your phone number (with country code, e.g. +88017...): ").strip()
        await client.send_code_request(phone)
        code = input("Enter the login code sent to your Telegram app: ").strip()
        try:
            await client.sign_in(phone, code)
        except Exception as e:
            if "SessionPasswordNeededError" in str(type(e)):
                pwd = input("Enter your Two-Factor Authentication (2FA) password: ").strip()
                await client.sign_in(password=pwd)
            else:
                raise e
        print("[+] Logged in successfully! Session saved.")
    else:
        me = await client.get_me()
        print(f"[+] Authorized as {me.first_name} (@{me.username or me.phone})")

async def main():
    cfg = load_config()
    port = int(cfg.get("port", 8080))

    # 1. Start the HTTP web server FIRST so container health checks always pass
    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print("\n" + "="*60)
    print(f"  xKAF Telegram Relay web server listening on port {port}!")
    print(f"  Health Check: http://0.0.0.0:{port}/health")
    print("="*60 + "\n")

    # 2. Connect Telegram Client
    client = None
    string_sess = cfg.get("string_session")
    if string_sess:
        sanitized = sanitize_string_session(string_sess)
        try:
            print("[*] Initializing Telegram client via TELEGRAM_STRING_SESSION...")
            session = StringSession(sanitized)
            client = TelegramClient(session, int(cfg.get("api_id", 2040)), str(cfg.get("api_hash", "b1844f7ea357e827e322493d1b02dd59")))
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"[+] Authorized as {me.first_name} (@{me.username or me.phone})")
            else:
                print("[!] Warning: StringSession connected, but user is not authorized. Please check your session token.")
        except Exception as e:
            print(f"[!] Warning: Could not initialize Telegram client: {e}")
            client = None
    elif sys.stdin and sys.stdin.isatty():
        try:
            session = SESSION_FILE
            client = TelegramClient(session, int(cfg.get("api_id", 2040)), str(cfg.get("api_hash", "b1844f7ea357e827e322493d1b02dd59")))
            await interactive_login(client)
        except Exception as e:
            print(f"[!] Interactive login failed: {e}")
            client = None
    else:
        print("[!] No TELEGRAM_STRING_SESSION provided in environment variables.")

    app["telegram_client"] = client

    # Keep server running forever
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Server stopped.")
