#!/usr/bin/env python3
"""
App entrypoint: find the Alarm.com cameras in Home Assistant, start the
session manager (one shared camera session per camera), and run go2rtc to
serve each camera as a local RTSP stream.
"""

import asyncio
import logging
import signal

from manager import Camera, serve

import json
import os
import sys
import time
import urllib.request

OPTIONS_FILE = os.environ.get("ADC_OPTIONS", "/data/options.json")
GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", "/tmp/go2rtc.yaml")
APP_DIR = os.environ.get("ADC_APP_DIR", "/app")
SOCK_DIR = os.environ.get("ADC_SOCK_DIR", "/tmp/adc")
SECRET_HINTS = ("token", "password", "credential", "secret", "key")


def log(msg):
    print(f"[bridge] {msg}", flush=True)


def load_options():
    try:
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def ha_states():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        base = "http://supervisor/core"
    else:
        base = os.environ.get("HA_URL", "http://homeassistant.local").rstrip("/")
        token = os.environ["HA_TOKEN"]
    req = urllib.request.Request(f"{base}/api/states", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def discover(include, exclude):
    """Camera entities that carry the alarmdotcom integration's webrtc_config."""
    for attempt in range(30):
        try:
            states = ha_states()
            break
        except Exception as err:
            log(f"waiting for Home Assistant ({err})")
            time.sleep(10)
    else:
        sys.exit("Home Assistant API not reachable")
    cams = []
    for st in states:
        eid = st.get("entity_id", "")
        attrs = st.get("attributes") or {}
        if not eid.startswith("camera.") or "webrtc_config" not in attrs:
            continue
        if include and eid not in include:
            continue
        if eid in exclude:
            continue
        cams.append((eid, attrs))
    return cams


def describe_config(cfg):
    """Key names (and harmless values) of webrtc_config, for the HD investigation."""
    if not isinstance(cfg, dict):
        return "not available yet"
    parts = []
    for k, v in sorted(cfg.items()):
        if any(h in k.lower() for h in SECRET_HINTS):
            parts.append(f"{k}=<hidden>")
        elif k == "iceServers":
            parts.append(f"iceServers=<{len(v or [])} servers>")
        elif isinstance(v, (str, int, float, bool)) or v is None:
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}=<{type(v).__name__}>")
    return ", ".join(parts)


def yaml_str(s):
    return json.dumps(s)  # JSON strings are valid YAML scalars


def main():
    opts = load_options()
    port = int(opts.get("rtsp_port", 8554))
    lan = bool(opts.get("lan_access", False))
    user = (opts.get("rtsp_username") or "").strip()
    password = opts.get("rtsp_password") or ""
    include = set()
    exclude = {e.strip() for e in (opts.get("exclude") or "").split(",") if e.strip()}
    debug = opts.get("log_level") == "debug"

    if lan and not (user and password):
        log("lan_access needs rtsp_username and rtsp_password; staying local-only")
        lan = False

    cams = discover(include, exclude)
    if not cams:
        log("No Alarm.com cameras found. Is the Alarm.com (alarmdotcom) integration set up with cameras?")
        log("Retrying in 5 minutes; restart the app after fixing.")
        time.sleep(300)
        sys.exit(1)

    linger = int(opts.get("linger_seconds", 60))
    listen = f":{port}" if lan else f"127.0.0.1:{port}"
    lines = [
        "log:",
        "  level: info",
        "  format: text",
        "api:",
        '  listen: "127.0.0.1:11985"',
        "rtsp:",
        f"  listen: {yaml_str(listen)}",
    ]
    if lan:
        lines += [f"  username: {yaml_str(user)}", f"  password: {yaml_str(password)}"]
    lines += [
        "webrtc:",
        '  listen: ""',
        "srtp:",
        '  listen: ""',
        "streams:",
    ]
    cameras = []
    log(f"Found {len(cams)} Alarm.com camera(s):")
    for eid, attrs in cams:
        name = eid.split(".", 1)[1]
        cmd = f"exec:python3 -u {APP_DIR}/adc_client.py {name}#killsignal=15#killtimeout=3"
        lines.append(f"  {name}: {yaml_str(cmd)}")
        cameras.append(Camera(name, eid, APP_DIR, linger, debug))
        friendly = attrs.get("friendly_name", name)
        log(f"  {friendly}: rtsp://127.0.0.1:{port}/{name}")
        log(f"    webrtc_config: {describe_config(attrs.get('webrtc_config'))}")
    if lan:
        log(f"RTSP is also available on your network at rtsp://<home-assistant-ip>:{port}/<name> "
            f"(username {user})")
    else:
        log("RTSP is local to Home Assistant only (turn on lan_access to use it from other devices)")
    log(f"Camera sessions stay open {linger}s after the last viewer leaves.")

    with open(GO2RTC_CONFIG, "w") as f:
        f.write("\n".join(lines) + "\n")

    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, format="[bridge] %(message)s")
    sys.exit(asyncio.run(run_services(cameras)))


async def run_services(cameras):
    """Run the session manager and go2rtc until either stops or we're told to stop."""
    await serve(cameras, SOCK_DIR)
    env = dict(os.environ, ADC_SOCK_DIR=SOCK_DIR)
    go2rtc = await asyncio.create_subprocess_exec("go2rtc", "-config", GO2RTC_CONFIG, env=env)
    log("Started go2rtc. Cameras connect only while something is watching.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    go2rtc_exit = asyncio.create_task(go2rtc.wait())
    await asyncio.wait([go2rtc_exit, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED)

    code = 0
    if go2rtc_exit.done():
        code = go2rtc.returncode or 1
        log(f"go2rtc exited unexpectedly (code {go2rtc.returncode}); stopping")
    else:
        go2rtc.terminate()
        try:
            await asyncio.wait_for(go2rtc.wait(), timeout=5)
        except asyncio.TimeoutError:
            go2rtc.kill()
    await asyncio.gather(*(c.stop() for c in cameras), return_exceptions=True)
    return code


if __name__ == "__main__":
    main()
