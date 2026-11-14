# Changelog

## 0.1.14

- Base image pinned in the Dockerfile (`ghcr.io/home-assistant/base:3.24`); deprecated `build.yaml` removed.
- armv7 dropped: the 3.24 base image is published for amd64 and aarch64 only.
- Source comments, log messages and MQTT entity names in English. Entity IDs stay the same (unique IDs unchanged); only the displayed names change, e.g. "Drehzahl" becomes "Speed".

## 0.1.13

- Documentation and add-on description in English, MIT license.
- No functional changes.
