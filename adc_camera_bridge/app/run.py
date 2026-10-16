#!/usr/bin/env python3
"""
App entrypoint: find the Alarm.com cameras in Home Assistant, write a go2rtc
config with one on-demand stream per camera, then hand over to go2rtc.
"""

import json
import os
import sys
import time
import urllib.request

OPTIONS_FILE = os.environ.get("ADC_OPTIONS", "/data/options.json")
GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", "/tmp/go2rtc.yaml")
APP_DIR = os.environ.get("ADC_APP_DIR", "/app")
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

    listen = f":{port}" if lan else f"127.0.0.1:{port}"
    lines = [
        "log:",
        "  level: info",
        "  exec: debug",  # our streamer's messages arrive as exec debug lines
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
    prefix = "env ADC_DEBUG=1 " if debug else ""  # go2rtc runs commands without a shell
    log(f"Found {len(cams)} Alarm.com camera(s):")
    for eid, attrs in cams:
        name = eid.split(".", 1)[1]
        cmd = f"exec:{prefix}python3 -u {APP_DIR}/adc_stream.py {eid}#killsignal=15#killtimeout=5"
        lines.append(f"  {name}: {yaml_str(cmd)}")
        friendly = attrs.get("friendly_name", name)
        log(f"  {friendly}: rtsp://127.0.0.1:{port}/{name}")
        log(f"    webrtc_config: {describe_config(attrs.get('webrtc_config'))}")
    if lan:
        log(f"RTSP is also available on your network at rtsp://<home-assistant-ip>:{port}/<name> "
            f"(username {user})")
    else:
        log("RTSP is local to Home Assistant only (turn on lan_access to use it from other devices)")

    with open(GO2RTC_CONFIG, "w") as f:
        f.write("\n".join(lines) + "\n")
    log("Starting go2rtc. Cameras connect only while something is watching.")
    os.execvp("go2rtc", ["go2rtc", "-config", GO2RTC_CONFIG])


if __name__ == "__main__":
    main()
