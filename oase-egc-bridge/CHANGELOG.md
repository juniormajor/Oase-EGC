# Changelog

## 0.1.15

- Fix: replies are matched to their request by RDM PID and, for indexed values, the echoed index. Late replies to timed-out requests are discarded instead of being taken as the answer to the next read; before, a late reply shifted all following values (speed shown as water temperature, pump reported off and SFC on while the pump kept running) until the next control command.
- A read without reply is logged as WARNING (was DEBUG).

## 0.1.14

- Base image pinned in the Dockerfile (`ghcr.io/home-assistant/base:3.24`); deprecated `build.yaml` removed.
- armv7 dropped: the 3.24 base image is published for amd64 and aarch64 only.
- Source comments, log messages and MQTT entity names in English. Entity IDs stay the same (unique IDs unchanged); only the displayed names change, e.g. "Drehzahl" becomes "Speed".

## 0.1.13

- Documentation and add-on description in English, MIT license.
- No functional changes.
