# Changelog

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
