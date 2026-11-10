# OASE EGC Bridge – Home Assistant Add-on

Bindet die OASE EGC Teichpumpensteuerung (AquaMax Eco Expert, ONet-Protokoll)
**lokal** in Home Assistant ein – ohne OASE-Cloud, ohne OASE-Konto. Reverse
Engineering und Protokolldoku: siehe Repo `infra`, Bereich D04.

## Installation
1. In HA: **Einstellungen → Add-ons → Add-on-Store → ⋮ → Repositories**
   und die URL dieses GitHub-Repos hinzufügen.
2. Add-on **OASE EGC Bridge** installieren.
3. MQTT-Integration in HA aktiv (Mosquitto-Broker-Add-on genügt).

## Schnellstart
1. **Key beschaffen** (einmalig): Option `mode: capture` setzen, starten,
   OASE-App am Handy öffnen. Das Add-on fängt den Key ab, **speichert ihn selbst**
   (persistent in `/data/key.bin`), stellt den Modus **automatisch auf `bridge`**
   und startet sich neu – kein manuelles Kopieren.
   Firewall: der Add-on-Host muss das Handy erreichen (wie der bisherige Testhost).
2. Danach läuft das Add-on als Bridge. Die Pumpe erscheint via MQTT-Discovery als
   Gerät „OASE EGC Teichpumpe" mit Sensoren (Drehzahl, Wassertemperatur, Leistung)
   und – wenn `enable_control: true` – Leistung (`number`) und Ein/Aus (`switch`).

**Key ändern:** erneut `mode: capture` laufen lassen (überschreibt automatisch),
oder `device_password` in den Optionen setzen – die Option hat Vorrang vor dem
automatisch gespeicherten Key.

## Optionen
| Option | Bedeutung |
|---|---|
| `controller_ip` | IP des EGC Controllers (z. B. 172.17.30.5) |
| `device_password` | Optionaler Geräte-Key (Vorrang); leer = automatisch abgefangener Key aus dem Capture |
| `poll_interval` | Abfrageintervall in Sekunden |
| `enable_control` | Schreiben (Leistung/Ein-Aus) freigeben |
| `mode` | `bridge` (Normalbetrieb) oder `capture` (Key abfangen) |

## Hinweise
- Die Bridge hält eine **eigene** Verbindung; die OASE-App und Firmware-Updates
  des Controllers bleiben parallel möglich (kein Exklusivmodus).
- Der Controller **verbindet sich zurück** auf TCP 5999. Läuft das Add-on in einem
  anderen Netz als der Controller, braucht es eine Firewall-Allow-Regel
  `Controller → Add-on-Host:5999` (host_network ist aktiv).
