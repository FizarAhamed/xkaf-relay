#!/usr/bin/env python3
"""
xKAF Telegram MTProto Relay & 24/7 Session Keeper for KOReader
=============================================================
1. Connects personal Telegram account to @xkaf_bot / @xKAF_dl_bot via MTProto (Telethon).
2. Maintains 24/7 authenticated xKAF session via automatic refresh token rotation (every 7 minutes).
3. Mints signed download tokens for Curated & 324k Global Archive books on demand.
4. Correctly routes Global Archive downloads to @xKAF_dl_bot and Curated to @xkaf_bot.
5. Ignores welcome photos/banners and ONLY captures real book documents (.epub, .pdf, .mobi).
6. Streams binary book files directly back to Kindle over Wi-Fi.
"""

import os
import sys
import json
import time
import asyncio
import re
from aiohttp import web, ClientSession
from telethon import TelegramClient, events
from telethon.sessions import StringSession

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xkaf_relay_config.json")
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xkaf_user")
SESSION_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xkaf_session_cache.json")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
API_BASE = "https://api.xkaf.org"

# In-memory xKAF session store
xkaf_state = {
    "rt": None,
    "at": None,
    "last_refresh": 0,
    "status": "uninitialized",
    "last_error": None,
    "user_info": None,
}

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
    if len(s) > 1 and s.startswith("1"):
        payload = s[1:]
        missing = len(payload) % 4
        if missing:
            payload += "=" * (4 - missing)
        return s[0] + payload
    return s

def load_cached_session():
    # 1. Environment variable takes highest initial priority
    env_rt = os.environ.get("XKAF_REFRESH_TOKEN")
    if env_rt:
        xkaf_state["rt"] = env_rt.strip()
        xkaf_state["status"] = "configured_via_env"
        print("[*] Loaded xKAF refresh token from environment variable XKAF_REFRESH_TOKEN.")

    # 2. Check cached file on disk (persists across rotations)
    if os.path.exists(SESSION_CACHE_FILE):
        try:
            with open(SESSION_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("rt"):
                    xkaf_state["rt"] = data["rt"]
                if data.get("at"):
                    xkaf_state["at"] = data["at"]
                if data.get("last_refresh"):
                    xkaf_state["last_refresh"] = data["last_refresh"]
                if data.get("status"):
                    xkaf_state["status"] = data["status"]
                print("[*] Loaded cached xKAF session from disk.")
        except Exception as e:
            print(f"[!] Error reading session cache: {e}")

def save_cached_session():
    try:
        with open(SESSION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "rt": xkaf_state["rt"],
                "at": xkaf_state["at"],
                "last_refresh": xkaf_state["last_refresh"],
                "status": xkaf_state["status"],
            }, f, indent=2)
    except Exception as e:
        print(f"[!] Error saving session cache: {e}")

async def refresh_xkaf_session():
    """Refreshes xkaf_at and rotates xkaf_rt using https://api.xkaf.org/api/auth/refresh"""
    rt = xkaf_state.get("rt")
    if not rt:
        xkaf_state["status"] = "no_token"
        return False, "No refresh token configured."

    print(f"[*] Attempting xKAF token refresh (current rt: {rt[:8]}...)...")
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://xkaf.org/",
        "Origin": "https://xkaf.org",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Cookie": f"xkaf_rt={rt}",
    }

    try:
        async with ClientSession() as session:
            async with session.post(f"{API_BASE}/api/auth/refresh", headers=headers) as resp:
                status = resp.status
                raw_text = await resp.text()

                if status == 200:
                    new_at = None
                    new_rt = None

                    if "xkaf_at" in resp.cookies:
                        new_at = resp.cookies["xkaf_at"].value
                    if "xkaf_rt" in resp.cookies:
                        new_rt = resp.cookies["xkaf_rt"].value

                    for header_val in resp.headers.getall("Set-Cookie", []):
                        m_at = re.search(r"xkaf_at=([^;,\s]+)", header_val)
                        if m_at and m_at.group(1) != "deleted":
                            new_at = m_at.group(1)
                        m_rt = re.search(r"xkaf_rt=([^;,\s]+)", header_val)
                        if m_rt and m_rt.group(1) != "deleted":
                            new_rt = m_rt.group(1)

                    if new_at or new_rt:
                        if new_at:
                            xkaf_state["at"] = new_at
                        if new_rt:
                            xkaf_state["rt"] = new_rt
                        xkaf_state["last_refresh"] = time.time()
                        xkaf_state["status"] = "active"
                        xkaf_state["last_error"] = None
                        save_cached_session()
                        print(f"[+] xKAF session refreshed successfully! Next rotation in 7m.")
                        return True, "Session refreshed."
                    else:
                        print("[!] Warning: 200 OK received but no Set-Cookie headers found.")
                        return True, "OK (no new cookies)"
                elif status == 401:
                    xkaf_state["status"] = "expired"
                    xkaf_state["last_error"] = "HTTP 401 Unauthorized"
                    print(f"[!] xKAF refresh failed with 401: token expired or invalidated.")
                    return False, "Token expired (HTTP 401)"
                else:
                    xkaf_state["last_error"] = f"HTTP {status}"
                    print(f"[!] xKAF refresh failed with HTTP {status}: {raw_text[:200]}")
                    return False, f"HTTP {status}"
    except Exception as e:
        xkaf_state["last_error"] = str(e)
        print(f"[!] Network error during xKAF refresh: {e}")
        return False, str(e)

async def mint_xkaf_token(raw_id=None, slug=None, msg_id=None, ext="epub"):
    """Mints a signed 5-minute download token for raw or curated books."""
    if not xkaf_state.get("at"):
        ok, _ = await refresh_xkaf_session()
        if not ok or not xkaf_state.get("at"):
            return None, "xKAF session not active or expired. Please visit /set-session?rt=..."

    payload = {}
    if raw_id:
        try:
            payload["rawId"] = int(raw_id)
        except ValueError:
            payload["rawId"] = raw_id
    else:
        payload["bookSlug"] = slug or ""
        try:
            payload["telegramMsgId"] = int(msg_id or 0)
        except ValueError:
            payload["telegramMsgId"] = 0
        payload["format"] = (ext or "epub").lower()

    cookie_parts = []
    if xkaf_state.get("at"):
        cookie_parts.append(f"xkaf_at={xkaf_state['at']}")
    if xkaf_state.get("rt"):
        cookie_parts.append(f"xkaf_rt={xkaf_state['rt']}")

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://xkaf.org/",
        "Origin": "https://xkaf.org",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
        "Cookie": "; ".join(cookie_parts),
    }

    url = f"{API_BASE}/api/downloads/mint-token"
    print(f"[*] Minting download token: {payload}...")

    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            status = resp.status
            if status == 401:
                print("[!] Mint token returned 401. Refreshing session and retrying...")
                ok, _ = await refresh_xkaf_session()
                if ok:
                    cookie_parts = []
                    if xkaf_state.get("at"):
                        cookie_parts.append(f"xkaf_at={xkaf_state['at']}")
                    if xkaf_state.get("rt"):
                        cookie_parts.append(f"xkaf_rt={xkaf_state['rt']}")
                    headers["Cookie"] = "; ".join(cookie_parts)
                    async with session.post(url, headers=headers, json=payload) as retry_resp:
                        if retry_resp.status == 200:
                            data = await retry_resp.json()
                            return data.get("url"), None
                        else:
                            return None, f"Retry failed with HTTP {retry_resp.status}"
                return None, "Session unauthorized"

            if status == 200:
                data = await resp.json()
                bot_url = data.get("url")
                print(f"[+] Successfully minted token URL: {bot_url}")
                return bot_url, None
            else:
                raw = await resp.text()
                return None, f"Mint token failed (HTTP {status}): {raw[:200]}"

async def session_keeper_loop():
    """Background task running every 7 minutes to continuously rotate xkaf_rt."""
    print("[*] Started xKAF 24/7 background session keeper loop (7m interval).")
    while True:
        try:
            if xkaf_state.get("rt"):
                await refresh_xkaf_session()
            else:
                xkaf_state["status"] = "waiting_for_token"
        except Exception as e:
            print(f"[!] Session keeper error: {e}")
        await asyncio.sleep(420)

def parse_telegram_target(raw_input, default_bot="xkaf_bot"):
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
        bot_target = m_bot.group(1)
        if bot_target.lower() == "xkaf_bot":
            bot_target = "xKAF_dl_bot"
        return "bot", bot_target, m_bot.group(2)

    # 3. Direct download path: /d/<token>
    m_path = re.search(r"/d/([a-zA-Z0-9_\-]+)", s)
    if m_path:
        return "bot", default_bot, m_path.group(1)

    # 4. Query param start=XYZ
    m_start = re.search(r"start=([a-zA-Z0-9_\-]+)", s)
    if m_start:
        return "bot", default_bot, m_start.group(1)

    # Plain token: use default_bot
    clean = s.split("&")[0].split("?")[0].strip()
    return "bot", default_bot, clean

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

    now = time.time()
    last_ref = xkaf_state["last_refresh"]
    age_seconds = int(now - last_ref) if last_ref > 0 else None

    return web.json_response({
        "status": "ok" if is_auth else "warning",
        "telegram": {
            "authorized": is_auth,
            "user": me.first_name if me else None,
            "username": me.username if me else None,
            "phone": me.phone if me else None,
        },
        "xkaf_session": {
            "status": xkaf_state["status"],
            "has_token": bool(xkaf_state["rt"]),
            "last_refresh_ago": f"{age_seconds}s" if age_seconds is not None else "never",
            "last_error": xkaf_state["last_error"],
        },
        "bots": {
            "all_downloads": "@xKAF_dl_bot",
        },
        "version": "1.3.0-dlbot-fix",
        "service": "xkaf-telegram-relay",
        "notice": None if is_auth else "Telegram client not yet authorized. Please verify TELEGRAM_STRING_SESSION.",
    })

async def handle_set_session(request):
    """Allows one-time or manual configuration of xkaf_rt."""
    rt = request.query.get("rt") or request.query.get("token")
    if not rt and request.can_read_body:
        try:
            body = await request.json()
            rt = body.get("rt") or body.get("token") or body.get("xkaf_rt")
        except Exception:
            pass

    if not rt:
        return web.json_response({
            "error": "Missing 'rt' parameter. Usage: /set-session?rt=YOUR_XKAF_RT",
            "current_status": xkaf_state["status"]
        }, status=400)

    clean_rt = rt.strip().strip('"\'')
    if "xkaf_rt=" in clean_rt:
        m = re.search(r"xkaf_rt=([^;,\s]+)", clean_rt)
        if m:
            clean_rt = m.group(1)

    xkaf_state["rt"] = clean_rt
    save_cached_session()
    print(f"[*] Updated xKAF refresh token via /set-session. Testing refresh...")

    ok, msg = await refresh_xkaf_session()
    return web.json_response({
        "status": "success" if ok else "warning",
        "session_active": ok,
        "message": msg,
        "rotation_interval": "every 7 minutes automatically",
    })

async def handle_test_mint(request):
    """Diagnostic endpoint to inspect mint-token responses."""
    raw_id = request.query.get("raw_id")
    slug = request.query.get("slug")
    msg_id = request.query.get("msg_id", 0)
    ext = request.query.get("format", "epub")

    url, err = await mint_xkaf_token(raw_id=raw_id, slug=slug, msg_id=msg_id, ext=ext)
    return web.json_response({
        "raw_id": raw_id,
        "slug": slug,
        "minted_url": url,
        "error": err,
    })

async def handle_recent_messages(request):
    """Inspects recent messages in bot chats for debugging."""
    client = request.app.get("telegram_client")
    if not client or not await client.is_user_authorized():
        return web.json_response({"error": "Telegram client not authorized."}, status=401)

    bot = request.query.get("bot", "xKAF_dl_bot")
    try:
        msgs = await client.get_messages(bot, limit=5)
        res = []
        for m in msgs:
            res.append({
                "id": m.id,
                "date": str(m.date),
                "is_photo": bool(m.photo),
                "is_document": bool(m.document),
                "file_name": m.file.name if m.file else None,
                "file_size": m.file.size if m.file else None,
                "mime_type": getattr(m.file, "mime_type", None) if m.file else None,
                "text": m.text,
            })
        return web.json_response({"bot": bot, "messages": res})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

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
    raw_id = request.query.get("raw_id") or request.query.get("rawId")
    msg_id = request.query.get("msg_id") or request.query.get("telegram_msg_id")
    slug = request.query.get("slug")
    channel = request.query.get("channel", "xkaforg")

    is_raw_book = bool(raw_id)
    # @xKAF_dl_bot is the real download delivery bot for both Curated and Global Archive
    default_bot = "xKAF_dl_bot"

    # 1. Direct channel message fetch if msg_id provided and not raw_id and not raw_input
    # (Curated library has 1,280 books in channel @xkaforg - 100% session-free!)
    if msg_id and not raw_id and not raw_input:
        try:
            mid = int(str(msg_id).strip())
            if mid > 0:
                print(f"[*] Direct Telegram fetch: channel '{channel}', msg_id {mid} for '{title}' ({ext})")
                msg = await client.get_messages(channel, ids=mid)
                if msg and msg.file and msg.document:
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
                    print(f"[*] Direct channel msg {mid} had no document, falling back...")
        except Exception as e:
            print(f"[!] Direct channel fetch fallback: {e}")

    # 2. If raw_id is passed, mint token using our 24/7 server session (Global Archive)
    if raw_id:
        print(f"[*] Download request for Global Archive rawId: {raw_id} ('{title}')")
        minted_url, err = await mint_xkaf_token(raw_id=raw_id, ext=ext)
        if minted_url:
            raw_input = minted_url
            print(f"[+] Global Archive minted URL: {minted_url}")
        else:
            return web.Response(
                text=json.dumps({"error": f"Failed to mint download token for rawId #{raw_id}: {err}"}),
                status=502,
                content_type="application/json"
            )

    # 3. If slug is passed without explicit token/url (fallback for curated books if direct fetch didn't return a doc)
    elif slug and not raw_input:
        print(f"[*] Download request for curated slug: {slug} ('{title}')")
        minted_url, err = await mint_xkaf_token(slug=slug, msg_id=msg_id, ext=ext)
        if minted_url:
            raw_input = minted_url
            print(f"[+] Curated book minted URL: {minted_url}")
        else:
            return web.Response(
                text=json.dumps({"error": f"Failed to mint download token for slug '{slug}': {err}"}),
                status=502,
                content_type="application/json"
            )

    # Parse input link or token with appropriate default bot
    target_type, target_name, payload = parse_telegram_target(raw_input, default_bot=default_bot)

    if not target_type or not payload:
        return web.Response(
            text=json.dumps({"error": "Missing valid Telegram link, token, raw_id, or book slug."}),
            status=400,
            content_type="application/json"
        )

    # Determine primary bot (always xKAF_dl_bot for file downloads)
    bot_username = target_name or default_bot
    if bot_username.lower() == "xkaf_bot":
        bot_username = "xKAF_dl_bot"
    token = str(payload).strip()
    print(f"[*] Forwarding to Telegram: '{title}' ({ext}) via @{bot_username} (is_raw={is_raw_book}) with token: {token}")

    loop = asyncio.get_running_loop()
    doc_future = loop.create_future()
    last_bot_text = []

    # Listen for replies from BOTH bots
    allowed_chats = ["xKAF_dl_bot", "xkaf_bot"]

    @client.on(events.NewMessage(chats=allowed_chats))
    async def handler(event):
        if not event.message:
            return

        sender_chat = getattr(event.chat, "username", "") or str(event.chat_id)
        if event.message.text:
            print(f"[*] Bot @{sender_chat} sent text: {event.message.text[:120]}")
            last_bot_text.append(event.message.text)

        # CRITICAL FIX: Only accept DOCUMENTS (not welcome photos, banners, or avatars!)
        if event.message.document:
            mime = getattr(event.message.file, "mime_type", "") or ""
            file_name = event.message.file.name or ""
            # Reject image/jpeg or image/png banner documents unless filename is explicit book format
            is_image = mime.startswith("image/") and not file_name.lower().endswith((".epub", ".pdf", ".mobi", ".azw3", ".cbz"))
            if not is_image:
                print(f"[+] Received valid book document from @{sender_chat}: '{file_name}' ({mime}, {event.message.file.size} bytes)")
                if not doc_future.done():
                    doc_future.set_result(event.message)
            else:
                print(f"[-] Ignored banner image document from @{sender_chat}: '{file_name}' ({mime})")
        elif event.message.photo:
            print(f"[-] Ignored bot welcome photo from @{sender_chat} ({event.message.file.size if event.message.file else 0} bytes). Waiting for book document...")

    try:
        # Send /start {token} directly to @xKAF_dl_bot (the real book downloader)
        print(f"[*] Sending '/start {token}' to @xKAF_dl_bot...")
        await client.send_message("xKAF_dl_bot", f"/start {token}")

        if bot_username and bot_username != "xKAF_dl_bot":
            print(f"[*] Also sending '/start {token}' to @{bot_username}...")
            await client.send_message(bot_username, f"/start {token}")

        # Wait for file with 45s timeout
        msg = await asyncio.wait_for(doc_future, timeout=45.0)
    except asyncio.TimeoutError:
        print(f"[!] Timeout waiting for @{bot_username} to deliver book document.")
        err_msg = f"Timeout waiting for book document from @{bot_username}."
        if last_bot_text:
            err_msg += f" Bot response: {last_bot_text[-1][:150]}"
        return web.Response(
            text=json.dumps({"error": err_msg}),
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
    print(f"[*] Downloading book bytes from Telegram ({msg.file.size} bytes)...")
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
    app.router.add_get("/set-session", handle_set_session)
    app.router.add_post("/set-session", handle_set_session)
    app.router.add_get("/test-mint", handle_test_mint)
    app.router.add_get("/recent-messages", handle_recent_messages)
    app.router.add_post("/notify", handle_notify)
    app.router.add_get("/notify", handle_notify)
    return app

def register_telegram_commands(client):
    """Registers automated bot command handlers in the user's private Telegram Saved Messages."""
    if not client:
        return

    @client.on(events.NewMessage(chats="me"))
    async def handle_saved_messages(event):
        text = (event.message.text or "").strip()
        if not text:
            return

        # 1. /rt <token> or /rt=<token> or rt=<token>
        match = re.match(r"(?i)^(?:/rt|rt)(?:\s+|=)(.+)$", text)
        if match:
            raw_token = match.group(1).strip()
            # Clean possible cookie wrapper e.g. xkaf_rt=...
            m_rt = re.search(r"xkaf_rt=([^;\s]+)", raw_token)
            clean_rt = m_rt.group(1) if m_rt else raw_token.split(";")[0].strip()

            xkaf_state["rt"] = clean_rt
            save_cached_session()
            print(f"[*] Updated xKAF refresh token via Telegram command: {clean_rt[:10]}...")

            ok, err = await refresh_xkaf_session()
            if ok:
                user = xkaf_state.get("user_info") or "Active Member"
                await event.reply(
                    f"✅ **xKAF Session Updated Successfully!**\n\n"
                    f"• **Status:** Active & Verified\n"
                    f"• **Member:** {user}\n"
                    f"• **Token:** `{clean_rt[:8]}...{clean_rt[-6:]}`\n"
                    f"• **Auto-Renewal:** Every 7 minutes 🔄\n\n"
                    f"You can now download from Global Archive on your Kindle!",
                    parse_mode="md"
                )
            else:
                await event.reply(
                    f"❌ **Failed to verify token with xKAF:**\n`{err}`\n\n"
                    f"Please make sure you are logged in on xkaf.org and copied a fresh `xkaf_rt`.",
                    parse_mode="md"
                )
            return

        # 2. /status or /session or /ping
        if text.lower() in ["/status", "/session", "/ping"]:
            rt_disp = f"`{xkaf_state['rt'][:8]}...`" if xkaf_state.get("rt") else "Not configured"
            at_disp = f"`{xkaf_state['at'][:8]}...`" if xkaf_state.get("at") else "Not configured"
            last_ref = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(xkaf_state['last_refresh'])) if xkaf_state.get('last_refresh') else "Never"
            status_emoji = "🟢" if xkaf_state.get("status") == "active" else "🔴"

            await event.reply(
                f"📊 **xKAF Relay Server Status**\n\n"
                f"• **Session:** {status_emoji} `{xkaf_state.get('status')}`\n"
                f"• **Last Error:** `{xkaf_state.get('last_error') or 'None'}`\n"
                f"• **Last Refresh:** {last_ref}\n"
                f"• **RT Token:** {rt_disp}\n"
                f"• **AT Token:** {at_disp}\n\n"
                f"💡 Send `/rt <token>` anytime to update your session.\n"
                f"📚 *Curated books (1,280 books) download directly from the channel without login!*",
                parse_mode="md"
            )
            return

        # 3. /help or /start
        if text.lower() in ["/help", "/start"]:
            await event.reply(
                "🤖 **xKAF Kindle Relay Commands**\n\n"
                "• `/rt <token>` — Update your xKAF refresh token\n"
                "• `/status` — Check 24/7 session status & health\n\n"
                "✨ *Curated books (1,280 books) download directly from the channel with 0 login required!*",
                parse_mode="md"
            )
            return

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

    load_cached_session()

    # 1. Start the HTTP web server FIRST
    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print("\n" + "="*60)
    print(f"  xKAF Telegram Relay & Session Keeper listening on port {port}!")
    print(f"  Health Check: http://0.0.0.0:{port}/health")
    print(f"  Set Session:  http://0.0.0.0:{port}/set-session?rt=...")
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

    if client:
        try:
            if await client.is_user_authorized():
                register_telegram_commands(client)
                print("[+] Telegram command listener registered for Saved Messages (/rt, /status, /help).")
        except Exception as e:
            print(f"[!] Could not register Telegram command listener: {e}")

    # 3. Start 24/7 background session keeper loop
    asyncio.create_task(session_keeper_loop())

    # Keep server running forever
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Server stopped.")
