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

## Steuerung (ab 0.1.8)

- **Pumpe (Power):** echter Ein/Aus-Schalter über den ONet-Betriebsmodus
  (`SetOperationMode`), wirkt **modusunabhängig** – schaltet die Pumpe auch
  aus laufender SFC-/Manuell-Regelung komplett ab.
- **SFC-Automatik:** temperaturbasierte Automatik (Seasonal Flow Control) an/aus.
- **Manuelle Leistung %:** feste Drehzahl; deaktiviert dazu automatisch SFC.

## FAQ

**Controller per Name?** `controller_ip` akzeptiert auch einen Hostnamen
(z. B. `oase-egc.iot.lan`); er wird beim Verbinden per DNS aufgelöst.

**Wird `device_password` benötigt?** Ja – es ist der Auth-Key, den der
Controller bei `CheckDevicePassword` verlangt (im Router-Modus ein in der App
erzeugter Zufalls-Key). Der Capture-Modus trägt ihn automatisch ein; du kannst
ihn hier auch ändern oder leeren. Ohne gültigen Key schlagen Lese-/Schreib-
zugriffe fehl. Ist das Feld leer, fällt das Add-on auf `/data/key.bin` zurück.
