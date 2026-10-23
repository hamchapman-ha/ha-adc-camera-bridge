#!/usr/bin/env python3
"""
Alarm.com camera -> H.264 on stdout.

The app's session manager starts this when a camera is first viewed and stops
it (SIGTERM) a while after the last viewer leaves. While running it:

  1. asks Home Assistant's alarmdotcom integration for fresh WebRTC tokens
     (camera.turn_on, then reads the camera entity's webrtc_config),
  2. connects to the camera over WebRTC exactly like the dashboard card,
  3. writes the camera's own H.264 to stdout - no decoding, no re-encoding,
  4. reconnects automatically if Alarm.com or the network ends the session.

With ADC_FRAMED=1 (as the manager runs it) each access unit is written as a
4-byte big-endian length followed by the bytes; otherwise raw H.264. Logs go
to stderr.

Usage: adc_stream.py camera.front_left_cam
Inside the app the Supervisor token is used. Elsewhere set HA_URL and HA_TOKEN.
"""

import asyncio
import json
import logging
import os
import re
import signal
import struct
import sys
import time

import aiohttp
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc import jitterbuffer as aiortc_jitterbuffer
from aiortc import rtcdtlstransport
from aiortc import rtcrtpreceiver as aiortc_rtcrtpreceiver
from aiortc.codecs import h264 as aiortc_h264
from aiortc.sdp import candidate_from_sdp

log = logging.getLogger("adc")

START = b"\x00\x00\x00\x01"
CONNECT_TIMEOUT = 20      # seconds from tokens to first keyframe
STALL_TIMEOUT = 10        # seconds without video before we reconnect
TOKEN_SETTLE = 0.3        # seconds after camera.turn_on before reading tokens
KEEPALIVE_EVERY = int(os.environ.get("ADC_KEEPALIVE_SECS", "50"))  # Alarm.com's player: ~1/min
HD_RESOLUTION = "1280x720"
REQUEST_HD = os.environ.get("ADC_REQUEST_HD", "1") != "0"

# ---------------------------------------------------------------------------
# aiortc adjustments that these cameras need (all found with adc_probe.py)
# ---------------------------------------------------------------------------

# 1. The camera's DTLS certificate is RSA; aiortc only offers ECDSA suites.
RSA_CIPHERS = (
    b"ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:"
    b"ECDHE-ECDSA-AES128-SHA:ECDHE-ECDSA-AES256-SHA:"
    b"ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:"
    b"ECDHE-RSA-CHACHA20-POLY1305:ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA"
)
_orig_ctx = rtcdtlstransport.RTCCertificate._create_ssl_context


def _ssl_context(self, srtp_profiles):
    ctx = _orig_ctx(self, srtp_profiles)
    ctx.set_cipher_list(RSA_CIPHERS)
    return ctx


rtcdtlstransport.RTCCertificate._create_ssl_context = _ssl_context


# 2. Keyframes (~170 KB at 720p) don't fit aiortc's 128-packet video buffer.
class BigVideoJitterBuffer(aiortc_jitterbuffer.JitterBuffer):
    def __init__(self, capacity, prefetch=0, is_video=False):
        super().__init__(2048 if is_video else capacity, prefetch=prefetch, is_video=is_video)


aiortc_rtcrtpreceiver.JitterBuffer = BigVideoJitterBuffer

# 3. aiortc only negotiates Baseline H.264; the camera offers Main. Rewriting the
#    offer's profile lets negotiation succeed; we never decode, so it's harmless.
PROFILE_RE = re.compile(r"profile-level-id=[0-9a-fA-F]{6}")


# ---------------------------------------------------------------------------
# H.264 helpers
# ---------------------------------------------------------------------------


def nal_types(data):
    return [n[0] & 0x1F for n in aiortc_h264.H264Encoder._split_bitstream(data) if n]


def parse_sprop(sdp):
    import base64

    m = re.search(r"sprop-parameter-sets=([^;\s]+)", sdp)
    sps = pps = None
    if m:
        for part in m.group(1).split(","):
            try:
                nal = base64.b64decode(part + "=" * (-len(part) % 4))
            except Exception:
                continue
            if nal and nal[0] & 0x1F == 7 and sps is None:
                sps = nal
            elif nal and nal[0] & 0x1F == 8 and pps is None:
                pps = nal
    return sps, pps


def sps_resolution(sps):
    """Return (width, height) from an H.264 SPS NAL, or None."""
    try:
        raw, zeros = bytearray(), 0
        for b in sps[1:]:
            if zeros >= 2 and b == 3:
                zeros = 0
                continue
            raw.append(b)
            zeros = zeros + 1 if b == 0 else 0
        bits = "".join(f"{b:08b}" for b in raw)
        pos = 0

        def u(n):
            nonlocal pos
            v = int(bits[pos:pos + n], 2) if n else 0
            pos += n
            return v

        def ue():
            nonlocal pos
            z = 0
            while bits[pos] == "0":
                z += 1
                pos += 1
            pos += 1
            return (1 << z) - 1 + u(z)

        def se():
            v = ue()
            return (v + 1) // 2 if v % 2 else -(v // 2)

        profile = u(8)
        u(16)  # constraint flags + level
        ue()  # sps id
        chroma = 1
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma = ue()
            if chroma == 3:
                u(1)
            ue()
            ue()
            u(1)
            if u(1):  # scaling matrix present
                for i in range(8 if chroma != 3 else 12):
                    if u(1):
                        last = nxt = 8
                        for _ in range(16 if i < 6 else 64):
                            if nxt:
                                nxt = (last + se() + 256) % 256
                            last = nxt or last
        ue()  # log2_max_frame_num
        poc = ue()
        if poc == 0:
            ue()
        elif poc == 1:
            u(1)
            se()
            se()
            for _ in range(ue()):
                se()
        ue()  # max refs
        u(1)
        w_mbs, h_map = ue() + 1, ue() + 1
        frame_mbs_only = u(1)
        if not frame_mbs_only:
            u(1)
        u(1)
        width, height = w_mbs * 16, h_map * 16 * (2 - frame_mbs_only)
        if u(1):  # cropping
            cl, cr, ct, cb = ue(), ue(), ue(), ue()
            cx = 2 if chroma in (1, 2) else 1
            cy = (2 if chroma == 1 else 1) * (2 - frame_mbs_only)
            width -= (cl + cr) * cx
            height -= (ct + cb) * cy
        return width, height
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Output: H.264 access units to stdout, starting at a keyframe each session
# ---------------------------------------------------------------------------


class Session:
    def __init__(self, number):
        self.number = number
        self.sps = None
        self.pps = None
        self.started = False
        self.frames = 0
        self.keyframes = 0
        self.broken = 0
        self.bytes = 0
        self.last_frame = None
        self.first_keyframe_at = None
        self.t0 = time.monotonic()
        self.resolution = None    # from the most recent SPS seen
        self.switched_at = None   # when the in-band resolution last changed


def _sps_pps_in(data):
    sps = pps = None
    for n in aiortc_h264.H264Encoder._split_bitstream(data):
        if n and n[0] & 0x1F == 7:
            sps = n
        elif n and n[0] & 0x1F == 8:
            pps = n
    return sps, pps


CURRENT = Session(0)
_stdout = sys.stdout.buffer
FRAMED = os.environ.get("ADC_FRAMED") == "1"


def _on_access_unit(self, encoded_frame):
    """Replaces aiortc's H264Decoder.decode: forward instead of decoding."""
    s = CURRENT
    data = encoded_frame.data
    if not (data.startswith(START) or data.startswith(b"\x00\x00\x01")):
        s.broken += 1  # leftover of a frame the jitter buffer had to drop
        return []
    types = nal_types(data)
    s.frames += 1
    s.last_frame = time.monotonic()
    if 7 in types:
        sps, pps = _sps_pps_in(data)
        if sps:
            res = sps_resolution(sps)
            if res and s.resolution and res != s.resolution:
                log.info("session %d: camera switched to %dx%d", s.number, res[0], res[1])
                s.switched_at = time.monotonic()
            s.resolution = res or s.resolution
            s.sps, s.pps = sps, pps or s.pps
    if 5 in types:
        s.keyframes += 1
        if not s.started:
            s.started = True
            s.first_keyframe_at = s.last_frame
        if 7 not in types and s.sps and s.pps:
            data = START + s.sps + START + s.pps + data
    if not s.started:
        return []
    try:
        if FRAMED:
            _stdout.write(struct.pack(">I", len(data)) + data)
        else:
            _stdout.write(data)
        _stdout.flush()
    except (BrokenPipeError, ValueError):
        os._exit(0)  # whoever was reading us has gone
    s.bytes += len(data)
    return []


aiortc_h264.H264Decoder.decode = _on_access_unit


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------


class HomeAssistant:
    def __init__(self):
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
        if supervisor_token:
            self.base, self.token = "http://supervisor/core", supervisor_token
        else:
            self.base = os.environ.get("HA_URL", "http://homeassistant.local").rstrip("/")
            self.token = os.environ.get("HA_TOKEN") or sys.exit("HA_TOKEN not set")
        self.headers = {"Authorization": f"Bearer {self.token}"}

    async def webrtc_config(self, http, entity):
        async with http.post(
            f"{self.base}/api/services/camera/turn_on",
            headers=self.headers,
            json={"entity_id": entity},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            r.raise_for_status()
        await asyncio.sleep(TOKEN_SETTLE)
        async with http.get(
            f"{self.base}/api/states/{entity}",
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            r.raise_for_status()
            state = await r.json()
        cfg = (state.get("attributes") or {}).get("webrtc_config")
        if not cfg:
            raise RuntimeError(f"{entity} has no webrtc_config yet (state {state.get('state')})")
        if cfg.get("streamType") == "janus" or cfg.get("janusGatewayUrl"):
            raise RuntimeError(f"{entity} uses Alarm.com's Janus streaming, which this app doesn't support yet")
        missing = [k for k in ("signallingServerUrl", "signallingServerToken", "cameraAuthToken") if not cfg.get(k)]
        if missing:
            raise RuntimeError(f"{entity} webrtc_config is missing {missing}")
        return cfg


def ice_servers(cfg):
    servers = []
    for s in cfg.get("iceServers") or []:
        urls = s.get("urls") or s.get("url")
        if urls:
            servers.append(RTCIceServer(urls=urls, username=s.get("username"), credential=s.get("credential")))
    return servers


def mids_from_sdp(sdp):
    mids, current = [], None
    for line in sdp.splitlines():
        if line.startswith("m="):
            if current is not None:
                mids.append(current)
            current = str(len(mids))
        elif line.startswith("a=mid:") and current is not None:
            current = line[6:].strip()
    if current is not None:
        mids.append(current)
    return mids


# ---------------------------------------------------------------------------
# One camera session
# ---------------------------------------------------------------------------


async def run_session(http, ha, entity, number):
    """Stream until the session ends. Returns the Session for reporting."""
    global CURRENT
    s = CURRENT = Session(number)
    cfg = await ha.webrtc_config(http, entity)
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers(cfg)))
    ended = asyncio.Event()
    reason = {"why": "unknown"}
    mids = []
    peers = {"camera": None, "us": None}  # signalling peer ids, as Alarm.com's player uses them

    def end(why):
        if not ended.is_set():
            reason["why"] = why
            ended.set()

    @pc.on("connectionstatechange")
    async def on_state():
        if pc.connectionState in ("failed", "closed"):
            end(f"connection {pc.connectionState}")

    async def pli_until_keyframe():
        while not s.started and not ended.is_set():
            await asyncio.sleep(1.0)
            for t in pc.getTransceivers():
                if t.kind == "video":
                    for src in t.receiver.getSynchronizationSources():
                        try:
                            await t.receiver._send_rtcp_pli(src.source)
                        except Exception:
                            pass

    async def keepalive(ws):
        while not ended.is_set():
            await asyncio.sleep(KEEPALIVE_EVERY)
            await ws.send_str("keepalive")

    async def request_hd(ws):
        """Ask for HD the way Alarm.com's web player does, once video is flowing."""
        if not REQUEST_HD:
            return
        while not s.started and not ended.is_set():
            await asyncio.sleep(0.2)
        if ended.is_set():
            return
        if s.resolution and f"{s.resolution[0]}x{s.resolution[1]}" == HD_RESOLUTION:
            return
        if not (peers["camera"] and peers["us"]):
            log.info("session %d: can't request HD (peer ids unknown)", number)
            return
        message = "CHANGE_STREAM " + json.dumps({
            "to": peers["camera"], "from": peers["us"],
            "stream": {"streamID": 0, "resolution": HD_RESOLUTION},
        })
        for attempt in (1, 2):
            await ws.send_str(message)
            for _ in range(30):  # up to 6 s for the camera to switch
                await asyncio.sleep(0.2)
                if s.switched_at or ended.is_set():
                    return
        log.info("session %d: camera stayed at %s after HD request", number,
                 f"{s.resolution[0]}x{s.resolution[1]}" if s.resolution else "unknown resolution")

    async def watchdog():
        t0 = time.monotonic()
        while not ended.is_set():
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if not s.started and now - t0 > CONNECT_TIMEOUT:
                end(f"no keyframe within {CONNECT_TIMEOUT}s (connection: {pc.connectionState})")
            elif s.started and now - (s.last_frame or now) > STALL_TIMEOUT:
                end(f"video stalled for {STALL_TIMEOUT}s")

    ws_url = f"{cfg['signallingServerUrl'].rstrip('/')}/{cfg['signallingServerToken']}"
    tasks = []
    try:
        async with http.ws_connect(ws_url, heartbeat=20) as ws:
            await ws.send_str("HELLO 2.0.1")

            async def reader():
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    text = msg.data
                    if text.startswith("HELLO"):
                        await ws.send_str(f"START_SESSION {cfg['cameraAuthToken']}")
                        continue
                    if text.startswith("SESSION_STARTED"):
                        try:
                            started = json.loads(text.split(" ", 1)[1])
                            peers["camera"] = started.get("publisher_peer_id")
                        except (IndexError, ValueError, AttributeError):
                            pass
                        continue
                    if text.startswith("keepalive"):
                        continue
                    try:
                        data = json.loads(text)
                    except ValueError:
                        log.info("signalling: %s", text[:200])
                        continue
                    sdp = data.get("sdp")
                    if isinstance(sdp, dict) and sdp.get("type") == "offer":
                        offer = sdp["sdp"]
                        peers["us"] = data.get("to")
                        peers["camera"] = peers["camera"] or data.get("from")
                        mids[:] = mids_from_sdp(offer)
                        s.sps, s.pps = parse_sprop(offer)
                        res = sps_resolution(s.sps) if s.sps else None
                        s.resolution = res
                        log.info(
                            "session %d: camera offers H.264 %s%s",
                            number,
                            f"{res[0]}x{res[1]}" if res else "(resolution unknown)",
                            f" (profile {s.sps[1]}, level {s.sps[3] / 10:.1f})" if s.sps else "",
                        )
                        await pc.setRemoteDescription(
                            RTCSessionDescription(sdp=PROFILE_RE.sub("profile-level-id=42e01f", offer), type="offer")
                        )
                        await pc.setLocalDescription(await pc.createAnswer())
                        await ws.send_str(json.dumps({
                            "to": peers["camera"],
                            "sdp": {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp},
                        }))
                        continue
                    ice = data.get("ice")
                    if isinstance(ice, dict):
                        cand = (ice.get("candidate") or "").strip()
                        if not cand:
                            continue
                        if cand.startswith("candidate:"):
                            cand = cand[10:]
                        try:
                            c = candidate_from_sdp(cand)
                        except Exception:
                            continue
                        idx = ice.get("sdpMLineIndex")
                        c.sdpMLineIndex = idx
                        c.sdpMid = ice.get("sdpMid") or (
                            mids[idx] if isinstance(idx, int) and idx < len(mids) else None
                        )
                        try:
                            await pc.addIceCandidate(c)
                        except Exception:
                            pass
                end("Alarm.com closed the signalling connection")

            async def guarded(coro):
                try:
                    await coro
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    end(f"error: {err!r}")

            tasks = [
                asyncio.create_task(guarded(c))
                for c in (reader(), pli_until_keyframe(), watchdog(), keepalive(ws), request_hd(ws))
            ]
            await ended.wait()
    finally:
        for t in tasks:
            t.cancel()
        await pc.close()
    s.end_reason = reason["why"]
    return s


async def main(entity):
    ha = HomeAssistant()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def quiet_turn_refusals(loop, context):
        # Alarm.com's relay refuses to forward to the camera's home-network
        # address ("403 Forbidden IP"). Harmless: we connect directly instead.
        if type(context.get("exception")).__name__ == "TransactionFailed":
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(quiet_turn_refusals)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def stream_forever():
        number, backoff = 0, 2
        async with aiohttp.ClientSession() as http:
            while True:
                number += 1
                t0 = time.monotonic()
                try:
                    s = await run_session(http, ha, entity, number)
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    log.warning("session %d failed to start: %s (retrying in %ds)", number, err, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                lasted = time.monotonic() - t0
                startup = (s.first_keyframe_at - t0) if s.first_keyframe_at else None
                log.info(
                    "session %d ended after %.0fs: %s. %s",
                    number, lasted, s.end_reason,
                    f"First picture after {startup:.1f}s; sent {s.frames} frames, {s.bytes // 1024} KB"
                    + (f", {s.broken} broken" if s.broken else "")
                    if s.started else "No video was received.",
                )
                if s.started:
                    backoff = 2
                    await asyncio.sleep(0.5)  # reconnect straight away
                else:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    runner = asyncio.create_task(stream_forever())
    await asyncio.wait([runner, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED)
    runner.cancel()
    try:
        await runner
    except (asyncio.CancelledError, Exception):
        pass


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("camera."):
        sys.exit("usage: adc_stream.py camera.<entity>")
    entity_id = sys.argv[1]
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if os.environ.get("ADC_DEBUG") else logging.INFO,
        format=f"%(asctime)s {entity_id}: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )
    for noisy in ("aioice", "aiortc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log.info("starting camera session")
    asyncio.run(main(entity_id))
    s = CURRENT
    detail = ""
    if s.number:
        detail = f" (session {s.number} had been running {time.monotonic() - s.t0:.0f}s"
        if s.first_keyframe_at:
            detail += f"; first picture after {s.first_keyframe_at - s.t0:.1f}s"
        if s.resolution:
            detail += f"; streaming {s.resolution[0]}x{s.resolution[1]}"
        detail += ")"
    log.info("camera session stopped%s", detail)
