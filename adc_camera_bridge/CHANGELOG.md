# Changelog

## 0.3.0

- HD: each session now asks the camera for 1280x720, exactly as Alarm.com's
  web player does, so streams switch up from the default 640x360 a couple of
  seconds after starting. New option "Ask cameras for HD" (on by default).
- Sends Alarm.com's signalling keepalive about once a minute, like the
  official player.
- The log shows when a camera switches resolution, and each session's stop
  line includes the resolution it was streaming.

## 0.2.1

- Log lines now start with the date and time.

## 0.2.0

- One shared camera session per camera, kept open for 60 seconds after the
  last viewer leaves (new option: "Keep camera sessions open"). The Home app's
  preview refreshes no longer open a new Alarm.com session every 10 seconds.
- New viewers get the latest picture immediately, so previews and live view
  start faster.
- Quieter log: harmless relay errors are no longer printed as tracebacks.
- Each session's stop line now includes how long the first picture took.

## 0.1.0

- First version: on-demand RTSP streams for Alarm.com cameras via go2rtc.
