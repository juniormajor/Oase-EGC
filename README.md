# OASE EGC Bridge – Home Assistant Add-on repository

Local Home Assistant add-on for the OASE EGC pond pump controller
(AquaMax Eco Expert, ONet protocol) – no OASE cloud, no OASE account.
Pump state, speed, water and electronics temperature, power, SFC automatic and
manual power are exposed via MQTT discovery.

## Installation

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fjuniormajor%2FOase-EGC)

Or manually: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
`https://github.com/juniormajor/Oase-EGC`, then install **OASE EGC Bridge**.

Configuration and usage: see [oase-egc-bridge/DOCS.md](oase-egc-bridge/DOCS.md).

## Deutsch

Lokale Anbindung der OASE-EGC-Teichpumpensteuerung an Home Assistant ohne
Cloud und ohne OASE-Konto. Installation wie oben; die Dokumentation steht auf
Englisch in [DOCS.md](oase-egc-bridge/DOCS.md).

## License

MIT – see [LICENSE](LICENSE). Not affiliated with or endorsed by OASE GmbH.
