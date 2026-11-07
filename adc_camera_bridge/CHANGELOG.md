# Changelog

## 0.4.3

- Janus: the first relay mountpoint for a camera that's been idle often never
  sends an offer, while a fresh one a moment later offers straight away. The
  app now gives a mountpoint 8 seconds to offer, then immediately starts a
  fresh one (up to three quick retries) instead of waiting. Replaces 0.4.2's
  longer wait, which didn't help.

## 0.4.2

- Janus: waited up to 45 seconds for the first picture (superseded by 0.4.3).

## 0.4.1

- Janus: completes the relay's bare H.264 offer (packetization-mode and
  profile) so it can be accepted, and holds on to ICE candidates the relay
  sends before its offer instead of dropping them.

## 0.4.0

- Supports cameras that Alarm.com streams through its Janus relay servers
  instead of end-to-end WebRTC. Alarm.com can switch a camera between the two
  at any time; before this, a switch to Janus stopped every stream with
  "uses Alarm.com's Janus streaming, which this app doesn't support yet".
  The app follows the same steps as the integration's own camera card: it
  creates a private mountpoint that pulls from the camera's proxy address,
  answers the relay's offer and keeps the relay session alive.

## 0.3.5

- Faster live view. The app now checks the Alarm.com login every 10 minutes
  in the background (it says hello to Alarm.com's signalling server without
  asking for video, so cameras aren't woken) and reloads the Alarm.com
  integration as soon as the login has gone stale, so opening a camera no
  longer waits for that first.
- When a session does hit a stale login, it retries about 1 second after the
  reload instead of a fixed 15, and the other cameras retry as soon as the
  reload has had time to finish (about 8 seconds) instead of backing off.

## 0.3.4

- Fixes cameras that stop working after Home Assistant has been running a
  while. The Alarm.com integration can hold on to a stale login for hours: it
  still gets stream tokens, but Alarm.com rejects them ("Invalid token!"), so
  every session ended after 0s with no video until something made the
  integration log in again. Now, when Alarm.com rejects the tokens, the app
  reloads the Alarm.com integration (at most once every 10 minutes, shared by
  all cameras) and tries again about 15 seconds later.

## 0.3.3

- Logs any signalling message from Alarm.com that isn't part of the normal
  WebRTC exchange (its keys, and the text of status or error fields only), so
  the reason Alarm.com gives for refusing a session shows up in the log.

## 0.3.2

- When Alarm.com closes a camera's signalling connection straight away, the
  log now says how it closed (close code, how long after connecting) and which
  kinds of signalling message arrived first, to diagnose sessions that end
  after 0s with no video. Message contents and tokens are never logged.

## 0.3.1

- Works with Alarm.com integration 2026.10.28 and later. That version stopped
  putting camera stream tokens in the camera entities' state (a security
  change), which broke every stream with "has no webrtc_config yet". The app
  now asks the integration for tokens through its alarmdotcom/webrtc_config
  websocket command, and finds cameras through the entity registry. Older
  integration versions still work the old way.

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
