"""
Keeping the Alarm.com integration's login usable.

The alarmdotcom integration can hold on to a stale Alarm.com login: it still
gets camera stream tokens, but Alarm.com's signalling server rejects them
("Invalid token!") and nothing tells the integration to log in again.
Reloading the integration fixes it.

Two things use this module:
  * each camera session, when Alarm.com rejects its tokens (reload, then retry)
  * the app itself, which checks the login every few minutes in the background
    and reloads before anyone opens a camera, so live view starts straight away

Reloads are shared by every process through a stamp file, so at most one
happens per RELOAD_EVERY seconds however many cameras notice at once.
"""

import asyncio
import fcntl
import json
import logging
import os
import time

import aiohttp
import ha_ws

log = logging.getLogger("bridge")

WEBRTC_COMMAND = "alarmdotcom/webrtc_config"
STAMP = os.path.join(os.environ.get("ADC_SOCK_DIR", "/tmp/adc"), "relogin.stamp")
RELOAD_EVERY = 600       # at most one reload per 10 minutes
RELOAD_TAKES = 8         # seconds a reload is given before other cameras retry
CHECK_EVERY = int(os.environ.get("ADC_LOGIN_CHECK_SECS", "600"))


def _rest():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        return "http://supervisor/core", token
    return os.environ.get("HA_URL", "http://homeassistant.local").rstrip("/"), os.environ["HA_TOKEN"]


def last_reload():
    """Seconds since the last reload by any camera (None if there hasn't been one)."""
    try:
        if os.path.getsize(STAMP):
            return time.time() - os.path.getmtime(STAMP)
    except OSError:
        pass
    return None


async def reload_integration(http, entity):
    """Reload the integration that owns `entity`, unless one happened recently.

    Returns True if this call did the reload.
    """
    os.makedirs(os.path.dirname(STAMP), exist_ok=True)
    with open(STAMP, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            age = last_reload()
            if age is not None and age < RELOAD_EVERY:
                return False
            f.write(".")
            f.flush()
            os.utime(STAMP)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    base, token = _rest()
    async with http.post(
        f"{base}/api/services/homeassistant/reload_config_entry",
        headers={"Authorization": f"Bearer {token}"},
        json={"entity_id": entity},
        timeout=aiohttp.ClientTimeout(total=60),
    ) as r:
        r.raise_for_status()
    return True


async def wait_for_other_reload():
    """If another camera started a reload moments ago, wait until it's had time to finish."""
    age = last_reload()
    if age is not None and age < RELOAD_TAKES:
        await asyncio.sleep(RELOAD_TAKES - age)


async def tokens_rejected(http, entity, timeout=8):
    """Does Alarm.com reject the integration's current stream tokens?

    Connects to the signalling server and says HELLO, the way a session starts,
    but never asks for video, so the camera isn't woken. True if Alarm.com
    answers "Invalid token!", False if it answers normally, None if unsure.
    """
    cfg = await ha_ws.command(http, {"type": WEBRTC_COMMAND, "entity_id": entity})
    if not isinstance(cfg, dict) or not cfg.get("signallingServerUrl") or not cfg.get("signallingServerToken"):
        return None
    url = f"{cfg['signallingServerUrl'].rstrip('/')}/{cfg['signallingServerToken']}"
    async with http.ws_connect(url, timeout=aiohttp.ClientWSTimeout(ws_close=timeout)) as ws:
        await ws.send_str("HELLO 2.0.1")
        try:
            msg = await ws.receive(timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None
        if msg.data.startswith("HELLO"):
            return False
        try:
            data = json.loads(msg.data)
        except ValueError:
            return None
        return isinstance(data, dict) and "invalid token" in str(data.get("error", "")).lower()


async def keep_fresh(entities, interval=CHECK_EVERY):
    """Every `interval` seconds, check the login with one camera; reload if it's stale."""
    if interval <= 0 or not entities:
        return
    entity = sorted(entities)[0]
    await asyncio.sleep(30)  # let Home Assistant settle after the app starts
    async with aiohttp.ClientSession() as http:
        while True:
            try:
                if await tokens_rejected(http, entity):
                    if await reload_integration(http, entity):
                        log.info("Alarm.com login had gone stale; reloaded the Alarm.com integration "
                                 "so the cameras are ready when you open them")
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.debug("login check failed: %s", err)
            await asyncio.sleep(interval)
