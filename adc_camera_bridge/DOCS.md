# Alarm.com Camera Bridge

Turns each Alarm.com / ADT camera into an ordinary local RTSP stream, so
Home Assistant can treat it like any other camera, including showing it in
Apple Home through the HomeKit Bridge.

It needs the **Alarm.com** integration from HACS (the ibasebcast fork, which
supports cameras) set up and showing your cameras.

## How it works

- When something opens a camera stream (a dashboard, the Home app, a
  snapshot), the app asks the Alarm.com integration for fresh tokens,
  connects to the camera the same way the Alarm.com web player does, and
  passes the camera's own H.264 video through. Nothing is re-encoded.
- When nobody is watching, the camera connection is closed.
- If Alarm.com or the network ends a session, it reconnects automatically.
  Viewers see a short freeze, not a disconnect.

This is unofficial and depends on Alarm.com's private web interface, so an
Alarm.com change can break it. Don't rely on it for security.

## Stream addresses

The log lists every camera found when the app starts, for example:

```
Front Left Cam: rtsp://127.0.0.1:8554/front_left_cam
```

Use that address as the **Stream Source URL** of a **Generic Camera** in
Home Assistant (Settings → Devices & services → Add integration → Generic
Camera). Leave the still image URL empty and choose TCP as the RTSP
transport.

## Options

- **RTSP port**: default 8554.
- **Allow other devices on your network**: off by default, so only Home
  Assistant can reach the streams. Turn on, with a username and password, to
  watch from VLC or another device at `rtsp://user:pass@<home-assistant-ip>:8554/<name>`.
- **Cameras to skip**: comma-separated camera entity IDs.
- **Log level**: `debug` adds detail from the camera connections.

New cameras are picked up when the app restarts.

## Reading the log

Each camera connection logs lines like:

```
camera.front_left_cam: session 1: camera offers H.264 1280x720 (profile 77, level 3.1)
camera.front_left_cam: session 1 ended after 312s: Alarm.com closed the signalling connection. First picture after 4.2s; ...
```

The resolution line shows what Alarm.com chose for that session (it varies
between 640x360 and 1280x720).
