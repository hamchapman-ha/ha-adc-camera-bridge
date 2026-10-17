"""
Session manager: one camera session per camera, shared by every viewer.

go2rtc runs adc_client.py for each viewer (a dashboard, the Home app, a
snapshot). The client connects to this manager over a local socket. The
manager starts the camera's session (adc_stream.py) on the first viewer, sends
each new viewer the latest keyframe straight away, fans the live video out to
everyone, and keeps the session open for `linger` seconds after the last
viewer leaves, so the Home app's snapshot every ~10s reuses one warm session
instead of opening a new one each time.
"""

import asyncio
import logging
import os
import signal
import struct

log = logging.getLogger("bridge")

MAX_GOP_BYTES = 8 * 1024 * 1024       # cap on the cached keyframe + following frames
MAX_CLIENT_BUFFER = 8 * 1024 * 1024   # drop a viewer that falls this far behind


def has_idr(au):
    """True if an Annex-B access unit contains an IDR slice (NAL type 5)."""
    i = 0
    while True:
        i = au.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(au):
            return False
        if au[i + 3] & 0x1F == 5:
            return True
        i += 3


class Camera:
    def __init__(self, name, entity, app_dir, linger, debug):
        self.name = name
        self.entity = entity
        self.app_dir = app_dir
        self.linger = linger
        self.debug = debug
        self.clients = set()
        self.gop = []
        self.gop_bytes = 0
        self.proc = None
        self.pump_task = None
        self.linger_task = None
        self.stopping = False
        self.start_lock = asyncio.Lock()

    # -- viewers ---------------------------------------------------------

    async def handle_client(self, reader, writer):
        self._cancel_linger()
        async with self.start_lock:
            if self.proc is None:
                await self._start()
        # Send the cached keyframe + frames, then join the live fan-out. No
        # await in between, so no live frame can slip in ahead of the cache.
        for au in self.gop:
            writer.write(au)
        self.clients.add(writer)
        log.debug("%s: viewer joined (%d watching)", self.name, len(self.clients))
        try:
            await writer.drain()
            while await reader.read(4096):
                pass  # clients never send anything; EOF means they left
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._drop(writer)

    def _drop(self, writer):
        if writer in self.clients:
            self.clients.discard(writer)
            log.debug("%s: viewer left (%d watching)", self.name, len(self.clients))
        try:
            writer.close()
        except Exception:
            pass
        if not self.clients and self.proc is not None and self.linger_task is None:
            self.linger_task = asyncio.create_task(self._linger_then_stop())

    def _cancel_linger(self):
        if self.linger_task is not None:
            self.linger_task.cancel()
            self.linger_task = None

    async def _linger_then_stop(self):
        try:
            await asyncio.sleep(self.linger)
        except asyncio.CancelledError:
            return
        self.linger_task = None
        if not self.clients:
            log.info("%s: no viewers for %ds, closing camera session", self.name, self.linger)
            await self.stop()

    # -- camera session process ------------------------------------------

    async def _start(self):
        env = dict(os.environ, ADC_FRAMED="1")
        if self.debug:
            env["ADC_DEBUG"] = "1"
        self.gop, self.gop_bytes = [], 0
        self.proc = await asyncio.create_subprocess_exec(
            "python3", "-u", os.path.join(self.app_dir, "adc_stream.py"), self.entity,
            stdout=asyncio.subprocess.PIPE, env=env,
        )
        self.pump_task = asyncio.create_task(self._pump(self.proc))

    async def _pump(self, proc):
        try:
            while True:
                header = await proc.stdout.readexactly(4)
                au = await proc.stdout.readexactly(struct.unpack(">I", header)[0])
                if has_idr(au):
                    self.gop, self.gop_bytes = [au], len(au)
                elif self.gop:
                    self.gop.append(au)
                    self.gop_bytes += len(au)
                    if self.gop_bytes > MAX_GOP_BYTES:
                        self.gop, self.gop_bytes = [], 0
                for w in list(self.clients):
                    if w.transport.get_write_buffer_size() > MAX_CLIENT_BUFFER:
                        log.warning("%s: a viewer fell too far behind; disconnecting it", self.name)
                        self._drop(w)
                    else:
                        w.write(au)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        await proc.wait()
        if proc is not self.proc:
            return
        self.proc = None
        self.gop, self.gop_bytes = [], 0
        if self.clients and not self.stopping:
            log.warning("%s: camera session process exited (code %s); restarting", self.name, proc.returncode)
            await asyncio.sleep(2)
            if self.clients and self.proc is None:
                await self._start()
        elif self.clients:
            for w in list(self.clients):
                self._drop(w)

    async def stop(self):
        proc, self.proc = self.proc, None
        self._cancel_linger()
        if proc is None or proc.returncode is not None:
            return
        self.stopping = True
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=6)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        finally:
            self.stopping = False
            self.gop, self.gop_bytes = [], 0


async def serve(cameras, sock_dir):
    """Start one unix socket per camera. Returns the servers."""
    os.makedirs(sock_dir, exist_ok=True)
    servers = []
    for cam in cameras:
        path = os.path.join(sock_dir, f"{cam.name}.sock")
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        servers.append(await asyncio.start_unix_server(cam.handle_client, path=path))
    return servers
