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

def parse_telegram_target(raw_input):
    """
    Parses any token, link, or URL into target type, bot/channel, and id/payload.
    Supports:
      - https://t.me/xkaf_bot?start=XYZ
      - https://t.me/xKAF_dl_bot?start=XYZ
      - https://t.me/xkaforg/123
      - /d/XYZ or plain token string
    """
    if not raw_input:
        return None, None, None
    s = str(raw_input).strip()

    # 1. Channel post: t.me/xkaforg/123
    m_channel = re.search(r"t\.me/([a-zA-Z0-9_\-]+)/(\d+)", s)
    if m_channel:
        return "channel", m_channel.group(1), int(m_channel.group(2))

    # 2. Bot start link: t.me/<bot>?start=<token>
    m_bot = re.search(r"t\.me/([a-zA-Z0-9_\-]+)\?(?:start|startgroup)=([a-zA-Z0-9_\-]+)", s)
    if m_bot:
        return "bot", m_bot.group(1), m_bot.group(2)

    # 3. Direct download path: /d/<token>
    m_path = re.search(r"/d/([a-zA-Z0-9_\-]+)", s)
    if m_path:
        return "bot", "xkaf_bot", m_path.group(1)

    # 4. Query param start=XYZ
    m_start = re.search(r"start=([a-zA-Z0-9_\-]+)", s)
    if m_start:
        return "bot", "xkaf_bot", m_start.group(1)

    # Plain token: default to active xkaf_bot
    clean = s.split("&")[0].split("?")[0].strip()
    return "bot", "xkaf_bot", clean

async def handle_download(request):
    client = request.app.get("telegram_client")
    if not client or not await client.is_user_authorized():
        return web.Response(
            text=json.dumps({"error": "Telegram client not authorized. Please configure TELEGRAM_STRING_SESSION."}),
            status=401,
            content_type="application/json"
        )

    title = request.query.get("title", "book")
    ext = request.query.get("format", "epub").lower()
    raw_input = request.query.get("url") or request.query.get("link") or request.query.get("token")
    msg_id = request.query.get("msg_id") or request.query.get("telegram_msg_id")
    channel = request.query.get("channel", "xkaforg")

    # If explicit msg_id provided, fetch directly from channel
    if msg_id:
        try:
            mid = int(str(msg_id).strip())
            print(f"[*] Direct Telegram fetch: channel '{channel}', msg_id {mid} for '{title}' ({ext})")
            msg = await client.get_messages(channel, ids=mid)
            if msg and msg.file:
                print(f"[+] Found document in {channel}/{mid}: {msg.file.name}")
                file_bytes = await client.download_media(msg, file=bytes)
                if file_bytes:
                    file_name = msg.file.name or f"{title}.{ext}"
                    print(f"[+] Streamed '{file_name}' ({len(file_bytes)} bytes) directly to client.")
                    response = web.Response(body=file_bytes)
                    response.content_type = "application/epub+zip" if ext == "epub" else "application/octet-stream"
                    response.headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
                    response.headers["Content-Length"] = str(len(file_bytes))
                    return response
            else:
                print(f"[!] Message {mid} in {channel} has no document attached.")
        except Exception as e:
            print(f"[!] Error fetching direct message {msg_id}: {e}")

    # Parse input link or token
    target_type, target_name, payload = parse_telegram_target(raw_input)

    if not target_type or not payload:
        return web.Response(
            text=json.dumps({"error": "Missing valid Telegram link, token, or message ID."}),
            status=400,
            content_type="application/json"
        )

    # If the link is a direct channel message (e.g. t.me/xkaforg/123)
    if target_type == "channel":
        try:
            print(f"[*] Fetching from channel '{target_name}', msg_id {payload}...")
            msg = await client.get_messages(target_name, ids=int(payload))
            if msg and msg.file:
                file_bytes = await client.download_media(msg, file=bytes)
                if file_bytes:
                    file_name = msg.file.name or f"{title}.{ext}"
                    response = web.Response(body=file_bytes)
                    response.content_type = "application/epub+zip" if ext == "epub" else "application/octet-stream"
                    response.headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
                    response.headers["Content-Length"] = str(len(file_bytes))
                    return response
        except Exception as e:
            print(f"[!] Error fetching channel message: {e}")

    # If the target is a bot (@xkaf_bot or @xKAF_dl_bot)
    bot_username = target_name or "xkaf_bot"
    token = str(payload).strip()
    print(f"[*] Received download request for: '{title}' ({ext}) via @{bot_username} with token: {token}")

    loop = asyncio.get_running_loop()
    doc_future = loop.create_future()

    # Listen for replies from active xkaf_bot as well as legacy xKAF_dl_bot
    allowed_chats = list(set([bot_username, "xkaf_bot", "xKAF_dl_bot"]))

    @client.on(events.NewMessage(chats=allowed_chats))
    async def handler(event):
        if event.message and event.message.file:
            print(f"[+] Received book file from Telegram: {event.message.file.name}")
            if not doc_future.done():
                doc_future.set_result(event.message)

    try:
        print(f"[*] Sending '/start {token}' to @{bot_username}...")
        await client.send_message(bot_username, f"/start {token}")

        # Wait for file with 45s timeout
        msg = await asyncio.wait_for(doc_future, timeout=45.0)
    except asyncio.TimeoutError:
        print(f"[!] Timeout waiting for @{bot_username} to deliver file.")
        return web.Response(
            text=json.dumps({"error": f"Timeout waiting for @{bot_username} to respond."}),
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

async def handle_notify(request):
    client = request.app.get("telegram_client")
    if not client or not await client.is_user_authorized():
        return web.json_response({"error": "Telegram client not authorized."}, status=401)

    try:
        if request.can_read_body:
            body = await request.json()
        else:
            body = {}
    except Exception:
        body = {}

    text = body.get("message") or body.get("text") or request.query.get("text")
    if not text:
        return web.json_response({"error": "Missing message text"}, status=400)

    target = body.get("target") or request.query.get("target") or "me"

    try:
        sent = await client.send_message(target, text)
        return web.json_response({
            "status": "success",
            "message_id": sent.id,
            "target": str(target),
            "recipient": "Saved Messages" if target == "me" else str(target),
        })
    except Exception as e:
        print(f"[!] Error sending notification: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def init_app():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/download", handle_download)
    app.router.add_post("/notify", handle_notify)
    app.router.add_get("/notify", handle_notify)
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
