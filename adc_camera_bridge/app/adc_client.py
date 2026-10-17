#!/usr/bin/env python3
"""
go2rtc runs this for each viewer: it connects to the session manager's socket
for one camera and copies the camera's H.264 to stdout. go2rtc stops it
(SIGTERM) when the viewer leaves, which tells the manager the viewer has gone.
"""

import os
import signal
import socket
import sys

if len(sys.argv) != 2:
    sys.exit("usage: adc_client.py <camera name>")

signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
path = os.path.join(os.environ.get("ADC_SOCK_DIR", "/tmp/adc"), f"{sys.argv[1]}.sock")
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    sock.connect(path)
except OSError as err:
    sys.exit(f"adc_client: can't reach the session manager at {path}: {err}")

try:
    while True:
        data = sock.recv(262144)
        if not data:
            break
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]
except (BrokenPipeError, ConnectionError):
    pass
os._exit(0)
