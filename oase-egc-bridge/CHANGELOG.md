# Changelog

## 0.1.16

- Fix: a broken controller connection now rebuilds the session. Before, every read error was caught and logged at DEBUG only, including `[Errno 32] Broken pipe`; the bridge kept polling a dead connection, published nothing and never reconnected, so Home Assistant kept stale values and later showed them as unknown until the add-on was restarted (seen 2026-11-18/19, about 35 h).
- Read timeouts still skip only the affected value. Connection errors (broken pipe, reset, TLS errors) end the session at once; three polls in a row without any value also end it. In both cases `Online` goes to OFF and the bridge reconnects after 10 s.
- New WARNING "No values from controller (n/3)" for polls without any value.

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
