"""
Minimal Home Assistant websocket client: connect, authenticate, send one
command, return its result. Used for things the REST API doesn't offer: the
entity registry, and the alarmdotcom integration's on-demand camera tokens.

Inside the app the Supervisor token is used (ws://supervisor/core/websocket).
Elsewhere set HA_URL and HA_TOKEN.
"""

import os
import sys

import aiohttp


class HAWebsocketError(RuntimeError):
    """A websocket command came back with an error."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def connection():
    """(websocket URL, token) for this environment."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        return "ws://supervisor/core/websocket", token
    base = os.environ.get("HA_URL", "http://homeassistant.local").rstrip("/")
    token = os.environ.get("HA_TOKEN") or sys.exit("HA_TOKEN not set")
    if base.startswith("https://"):
        url = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        url = "ws://" + base[len("http://"):]
    else:
        url = base
    return f"{url}/api/websocket", token


async def command(http, payload, timeout=20):
    """Send one websocket command and return its result (raises HAWebsocketError)."""
    url, token = connection()
    async with http.ws_connect(url, timeout=aiohttp.ClientWSTimeout(ws_close=timeout), heartbeat=30) as ws:
        hello = await ws.receive_json(timeout=timeout)
        if hello.get("type") != "auth_required":
            raise HAWebsocketError("unexpected", f"expected auth_required, got {hello.get('type')}")
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json(timeout=timeout)
        if auth.get("type") != "auth_ok":
            raise HAWebsocketError("auth_failed", auth.get("message") or auth.get("type") or "unknown")
        await ws.send_json({"id": 1, **payload})
        while True:
            msg = await ws.receive_json(timeout=timeout)
            if msg.get("id") != 1 or msg.get("type") != "result":
                continue
            if msg.get("success"):
                return msg.get("result")
            err = msg.get("error") or {}
            raise HAWebsocketError(err.get("code", "unknown_error"), err.get("message", ""))
