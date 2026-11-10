#!/usr/bin/env python3
"""
OASE EGC Bridge - Home Assistant Add-on
Lokale ONet-Anbindung der OASE EGC Teichpumpensteuerung an HA via MQTT.

Zwei Betriebsarten (Option `mode`):
  - bridge  : verbindet sich zum Controller, pollt Zustand/Sensoren und
              veroeffentlicht sie per MQTT-Discovery; nimmt Steuerbefehle
              (Speed, Ein/Aus) aus HA entgegen (nur wenn enable_control=true).
  - capture : gibt sich einmalig als Controller gegenueber der OASE-App aus
              und liest das Geraetepasswort mit; schreibt es nach
              /data/captured_key.txt und ins Log. Danach den Key in die
              Add-on-Optionen (device_password) uebernehmen und auf `bridge`
              zurueckstellen.

Die Bridge haelt eine EIGENE Verbindung zum Controller. Die OASE-App bleibt
parallel nutzbar (der Controller erlaubt mehrere Clients); es wird kein
Exklusivmodus gesetzt, damit App und ggf. Firmware-Updates weiter funktionieren.

Protokollgrundlage: App-Dekompilat OASE Control 5.2.0 (siehe Repo infra, D04).
"""
import os, sys, socket, ssl, struct, time, json, threading, queue, tempfile, subprocess, signal, logging, urllib.request

# ------------------------------------------------------------------ Konfig
IP          = os.environ.get("CONTROLLER_IP", "172.17.30.5")
KEY_FILE    = "/data/key.bin"
def _resolve_key():
    env=os.environ.get("DEVICE_PASSWORD","")
    if env: return env.encode("latin1","ignore")
    try:
        with open(KEY_FILE,"rb") as f: return f.read().rstrip(b"\r\n")
    except FileNotFoundError: return b""
PASSWORD    = _resolve_key()
POLL        = int(os.environ.get("POLL_INTERVAL", "10"))
ENABLE_CTRL = os.environ.get("ENABLE_CONTROL", "false").lower() == "true"
MODE        = os.environ.get("MODE", "bridge")
LOGLEVEL    = os.environ.get("LOG_LEVEL", "info").upper()
MQTT_HOST   = os.environ.get("MQTT_HOST", "")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER   = os.environ.get("MQTT_USER", "")
MQTT_PASS   = os.environ.get("MQTT_PASS", "")

logging.basicConfig(level=getattr(logging, LOGLEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oase")

# ------------------------------------------------------------------ ONet/RDM
ONET_START = b"\x5c\x23\x4f\x41"
PORT_TCP, PORT_UDP = 5999, 5959
PT_REQTCP   = 0x1400
PT_ALIVE,  PT_ALIVE_R  = 0x1100, 0x11FF
PT_PW,     PT_PW_R     = 0x9F00, 0x9FFF
PT_DEVTAB, PT_DEVTAB_R = 0x4000, 0x40FF
PT_PUMP,   PT_PUMP_R   = 0x5100, 0x51FF
PT_RDM,    PT_RDM_R    = 0x7100, 0x71FF
PT_PIDRDM, PT_PIDRDM_R = 0x5500, 0x55FF
PT_FDISC,  PT_FDISC_R  = 0x1000, 0x10FF
RDM_SRC = bytes([0,0,0,0,0,1])

def onet(pt, payload=b"", txn=0):
    return ONET_START+struct.pack("<I",len(payload))+bytes([2,txn&0xFF])+struct.pack("<H",pt)+bytes([1,0,0,0])+payload

def onet_parse(d):
    if len(d)<16 or d[:4]!=ONET_START: return None
    ln=struct.unpack("<I",d[4:8])[0]
    return struct.unpack("<H",d[10:12])[0], d[9], d[16:16+ln], 16+ln

def rdm_build(manu,dev,pid,cc=0x20,sub=0,data=b"",txn=1):
    b=bytes([0xCC,0x01,24+len(data)])+struct.pack(">H",manu)+struct.pack(">I",dev)+RDM_SRC
    b+=bytes([txn&0xFF,0x01,0x00])+struct.pack(">H",sub)+bytes([cc])+struct.pack(">H",pid)+bytes([len(data)])+data
    return b+struct.pack(">H",sum(b)&0xFFFF)

def rdm_value(payload):
    if len(payload)<26 or payload[0]!=0xCC: return None
    pdl=payload[23]; return payload[24:24+pdl]

def s16(b, off=0):
    return struct.unpack(">h", b[off:off+2])[0] if len(b)>=off+2 else None

def make_server_ctx():
    d=tempfile.mkdtemp(); c=os.path.join(d,"c.pem"); k=os.path.join(d,"k.pem")
    subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-keyout",k,"-out",c,
                    "-days","3650","-nodes","-subj","/CN=com.oase.easycontrol"],
                   check=True, capture_output=True)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(c,k)
    try: ctx.minimum_version=ssl.TLSVersion.TLSv1_2
    except Exception: pass
    return ctx

def make_client_ctx():
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    try: ctx.minimum_version=ssl.TLSVersion.TLSv1_2
    except Exception: pass
    return ctx

# ------------------------------------------------------------------ ONet-Session
class OnetSession:
    """Haelt die vom Controller aufgebaute TLS-Verbindung und macht Request/Reply."""
    def __init__(self, ip, password):
        self.ip=ip; self.password=password; self.sock=None; self.lsock=None
        self.txn=0; self._buf=b""; self._lock=threading.Lock()

    def _nt(self):
        self.txn=(self.txn+1)&0xFF; return self.txn

    def connect(self, wait=30):
        self.lsock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self.lsock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self.lsock.bind(("0.0.0.0",PORT_TCP)); self.lsock.listen(1); self.lsock.settimeout(wait)
        stop=threading.Event()
        def trig():
            u=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
            pl=bytes([1])+struct.pack("<H",PORT_TCP)+struct.pack("<I",int(time.time()))
            while not stop.is_set():
                try: u.sendto(onet(PT_REQTCP,pl),(self.ip,PORT_UDP))
                except Exception as e: log.debug("trigger: %s", e)
                stop.wait(3)
            u.close()
        th=threading.Thread(target=trig,daemon=True); th.start()
        try:
            raw,addr=self.lsock.accept()
        except socket.timeout:
            stop.set(); self.lsock.close()
            raise TimeoutError("Controller hat nicht zurueckverbunden (Firewall/IP pruefen)")
        stop.set()
        log.info("Controller verbunden von %s", addr[0])
        self.sock=make_server_ctx().wrap_socket(raw, server_side=True); self.sock.settimeout(8)
        self._buf=b""
        self.request(PT_ALIVE, b"", PT_ALIVE_R)
        if self.password:
            rep=self.request(PT_PW, self.password[:64].ljust(64,b"\x00"), PT_PW_R)
            if not (rep and rep[0]==1):
                raise PermissionError("CheckDevicePassword INCORRECT - device_password pruefen")
            log.info("Authentifiziert (CheckDevicePassword Correct)")
        else:
            log.warning("Kein device_password gesetzt - Reads/Writes werden scheitern")

    def request(self, pt, payload, want, timeout=8):
        with self._lock:
            self.sock.sendall(onet(pt,payload,self._nt()))
            end=time.time()+timeout
            while time.time()<end:
                while True:
                    pr=onet_parse(self._buf)
                    if not pr: break
                    rpt,_,rpl,consumed=pr
                    self._buf=self._buf[consumed:]
                    if rpt==want: return rpl
                try: chunk=self.sock.recv(4096)
                except socket.timeout: break
                if not chunk: raise ConnectionError("Verbindung geschlossen")
                self._buf+=chunk
            raise TimeoutError(f"keine Antwort 0x{want:04X}")

    def close(self):
        for s in (self.sock,self.lsock):
            try: s.close()
            except Exception: pass

    # --- Fachfunktionen -------------------------------------------------
    def find_pump(self):
        for idx in range(8):
            rep=self.request(PT_DEVTAB, struct.pack("<I",idx)+struct.pack("<H",0), PT_DEVTAB_R)
            if len(rep)<24: continue
            article=struct.unpack("<I",rep[8:12])[0]; devid=struct.unpack("<I",rep[12:16])[0]
            manu=struct.unpack("<H",rep[16:18])[0]; dmx=struct.unpack("<H",rep[20:22])[0]
            if article and devid!=0xFFFFFFFF:
                return {"index":idx,"article":article,"manu":manu,"dev":devid,"dmx":dmx}
        return None

    def rdm_get(self, manu, dev, pid, data=b""):
        rep=self.request(PT_RDM, rdm_build(manu,dev,pid,0x20,data=data), PT_RDM_R)
        return rdm_value(rep)

    def read_state(self, pump):
        m,d=pump["manu"],pump["dev"]; st={}
        try: st["rpm"]=s16(self.rdm_get(m,d,0x0201,bytes([1])),1)
        except Exception as e: log.debug("rpm: %s",e)
        try: st["el_temp"]=s16(self.rdm_get(m,d,0x0201,bytes([4])),1)
        except Exception as e: log.debug("el:%s",e)
        try: st["water_temp"]=s16(self.rdm_get(m,d,0x0201,bytes([5])),1)
        except Exception as e: log.debug("wt:%s",e)
        try: st["power_w"]=s16(self.rdm_get(m,d,0x0201,bytes([10])),1)
        except Exception as e: log.debug("pw:%s",e)
        try:
            sp=self.rdm_get(m,d,0x8039)
            if sp: st["speed_raw"]=sp[0]; st["speed_pct"]=round(sp[0]*100/255)
        except Exception as e: log.debug("sp:%s",e)
        try:
            on=self.rdm_get(m,d,0x1010)
            if on is not None and len(on)>=1: st["on"]=bool(on[0])
        except Exception as e: log.debug("on:%s",e)
        if "on" not in st and st.get("rpm") is not None:
            st["on"]=st["rpm"]>50
        return st

    def set_speed_pct(self, pump, pct):
        val=max(0,min(255,round(pct*255/100)))
        self._pidrdm_set(pump["index"], 0x8039, bytes([val]))
        return val

    def set_power(self, pump, on):
        self._pidrdm_set(pump["index"], 0x1010, bytes([1 if on else 0]))

    def _pidrdm_set(self, table_index, pid, data):
        payload=struct.pack("<I",table_index)+struct.pack("<H",pid)+bytes([1])+bytes([len(data)])+data
        self.request(PT_PIDRDM, payload, PT_PIDRDM_R)
        log.info("SET PID 0x%04X <- %s (index %d)", pid, data.hex(), table_index)

# ------------------------------------------------------------------ Capture
def run_capture():
    import ssl as _ssl
    log.info("Capture-Modus: lerne Controller-Kennung von %s ...", IP)
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
    s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try: s.bind(("",PORT_UDP))
    except OSError: s.bind(("",0))
    s.settimeout(8); reply=None; end=time.time()+8
    while time.time()<end and not reply:
        s.sendto(onet(PT_FDISC,b""),(IP,PORT_UDP))
        try:
            d,addr=s.recvfrom(2048)
            if addr[0]==IP:
                pr=onet_parse(d)
                if pr and pr[0]==PT_FDISC_R and len(pr[2])>=300: reply=pr[2]
        except socket.timeout: pass
    s.close()
    if not reply:
        log.error("Keine DiscoveryReply vom Controller - an/erreichbar?"); return
    log.info("Kennung gelernt. Oeffne jetzt die OASE-App auf dem Handy (ggf. Controller kurz stromlos).")
    captured={"done":False}
    def play(phone,port):
        try:
            ctx=make_client_ctx(); raw=socket.create_connection((phone,port),timeout=8)
            c=ctx.wrap_socket(raw,server_hostname=phone); c.settimeout(10); buf=b""
            while not captured["done"]:
                data=c.recv(4096)
                if not data: break
                buf+=data
                while True:
                    pr=onet_parse(buf)
                    if not pr: break
                    pt,txn,pl,consumed=pr; buf=buf[consumed:]
                    if pt==PT_ALIVE: c.sendall(onet(PT_ALIVE_R,b"",txn))
                    elif pt==PT_PW:
                        pwb=pl[:64].split(b"\x00")[0]
                        log.warning("KEY ABGEFANGEN (%d B)", len(pwb))
                        c.sendall(onet(PT_PW_R,bytes([1]),txn))
                        captured["key"]=pwb; captured["done"]=True; return
            c.close()
        except Exception as e: log.error("play: %s", e)
    us=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); us.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
    us.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try: us.bind(("",PORT_UDP))
    except OSError as e: log.error("UDP 5959 belegt: %s", e); return
    us.settimeout(1); end=time.time()+300
    while time.time()<end and not captured["done"]:
        try: d,addr=us.recvfrom(2048)
        except socket.timeout: continue
        if addr[0]==IP: continue
        pr=onet_parse(d)
        if not pr: continue
        pt,txn,pl,_=pr
        if pt==PT_FDISC: us.sendto(onet(PT_FDISC_R,reply,txn),addr)
        elif pt==PT_REQTCP and len(pl)>=3:
            port=struct.unpack("<H",pl[1:3])[0]
            log.info("RequestTcpConnection vom Handy %s (Port %d)", addr[0], port)
            threading.Thread(target=play,args=(addr[0],port),daemon=True).start()
    if captured.get("key"):
        adopt_key_and_switch_to_bridge(captured["key"])
    else:
        log.error("Nichts abgefangen. Firewall (Add-on-Host -> Handy) und App-Oeffnen pruefen.")

# ------------------------------------------------------------------ Supervisor-API
def _sup(method, path, body=None):
    tok=os.environ.get("SUPERVISOR_TOKEN","")
    if not tok: raise RuntimeError("kein SUPERVISOR_TOKEN (hassio_api aktiv?)")
    req=urllib.request.Request("http://supervisor"+path, method=method,
        headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=15) as r: return r.read()

def adopt_key_and_switch_to_bridge(pwb):
    """Key persistent speichern, Modus automatisch auf bridge, Add-on neu starten."""
    try:
        with open(KEY_FILE,"wb") as f: f.write(pwb)
        log.warning("Key uebernommen -> %s (%d B)", KEY_FILE, len(pwb))
    except Exception as e:
        log.error("Key speichern fehlgeschlagen: %s", e); return
    try:
        opts={}
        try: opts=json.load(open("/data/options.json"))
        except Exception: pass
        opts["mode"]="bridge"
        _sup("POST","/addons/self/options", {"options":opts})
        log.warning("Modus automatisch auf 'bridge' gesetzt. Starte Add-on neu ...")
        _sup("POST","/addons/self/restart")
    except Exception as e:
        log.error("Auto-Uebernahme via Supervisor fehlgeschlagen (%s). Key liegt in %s, Modus manuell auf bridge stellen.", e, KEY_FILE)

# ------------------------------------------------------------------ MQTT-Bridge
import paho.mqtt.client as mqtt

DISC="homeassistant"; NODE="oase_egc"
AVAIL=f"{NODE}/availability"
CMD_SPEED=f"{NODE}/speed/set"; CMD_POWER=f"{NODE}/power/set"
cmd_q=queue.Queue()

def mqtt_connect():
    c=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oase-egc-bridge")
    if MQTT_USER: c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.will_set(AVAIL,"offline",retain=True)
    def on_connect(cl,u,f,rc,props=None):
        cl.subscribe(CMD_SPEED); cl.subscribe(CMD_POWER); cl.publish(AVAIL,"online",retain=True)
        log.info("MQTT verbunden (%s:%s)", MQTT_HOST, MQTT_PORT)
    def on_msg(cl,u,m):
        cmd_q.put((m.topic, m.payload.decode(errors="replace")))
    c.on_connect=on_connect; c.on_message=on_msg
    c.connect(MQTT_HOST, MQTT_PORT, 60); c.loop_start(); return c

def publish_discovery(c, pump):
    dev={"identifiers":[f"oase_egc_{pump['dev']:08x}"],"name":"OASE EGC Teichpumpe",
         "manufacturer":"OASE","model":f"AquaMax Eco Expert (Art {pump['article']})"}
    def cfg(comp,obj,payload):
        payload.update({"availability_topic":AVAIL,"device":dev,"unique_id":f"{NODE}_{obj}"})
        c.publish(f"{DISC}/{comp}/{NODE}/{obj}/config", json.dumps(payload), retain=True)
    cfg("sensor","rpm",{"name":"Drehzahl","state_topic":f"{NODE}/rpm","unit_of_measurement":"rpm","icon":"mdi:fan","state_class":"measurement"})
    cfg("sensor","water_temp",{"name":"Wassertemperatur","state_topic":f"{NODE}/water_temp","unit_of_measurement":"°C","device_class":"temperature","state_class":"measurement"})
    cfg("sensor","el_temp",{"name":"Elektroniktemperatur","state_topic":f"{NODE}/el_temp","unit_of_measurement":"°C","device_class":"temperature","state_class":"measurement"})
    cfg("sensor","power_w",{"name":"Leistung","state_topic":f"{NODE}/power_w","unit_of_measurement":"W","device_class":"power","state_class":"measurement"})
    cfg("sensor","speed_setpoint",{"name":"Speed-Sollwert","state_topic":f"{NODE}/speed_raw","unit_of_measurement":"/255","icon":"mdi:speedometer"})
    cfg("number","speed",{"name":"Leistung","command_topic":CMD_SPEED,"state_topic":f"{NODE}/speed_pct","min":0,"max":100,"step":1,"unit_of_measurement":"%","icon":"mdi:fan"})
    cfg("switch","power",{"name":"Pumpe","command_topic":CMD_POWER,"state_topic":f"{NODE}/power_state","payload_on":"ON","payload_off":"OFF","icon":"mdi:water-pump"})
    cfg("binary_sensor","online",{"name":"Online","state_topic":f"{NODE}/online","payload_on":"ON","payload_off":"OFF","device_class":"connectivity","entity_category":"diagnostic"})

def publish_state(c, st):
    for k,topic in (("rpm","rpm"),("water_temp","water_temp"),("el_temp","el_temp"),
                    ("power_w","power_w"),("speed_raw","speed_raw"),("speed_pct","speed_pct")):
        if st.get(k) is not None: c.publish(f"{NODE}/{topic}", st[k])
    if "on" in st:
        c.publish(f"{NODE}/power_state", "ON" if st["on"] else "OFF")
    c.publish(f"{NODE}/online","ON")

def run_bridge():
    if not MQTT_HOST:
        log.error("Kein MQTT-Broker (Supervisor-Service fehlt?)."); return
    mc=mqtt_connect()
    while True:
        sess=OnetSession(IP, PASSWORD)
        try:
            sess.connect(wait=30)
            pump=sess.find_pump()
            if not pump:
                log.error("Keine Pumpe in der Geraetetabelle gefunden."); sess.close(); time.sleep(10); continue
            log.info("Pumpe: Art %s UID %04X:%08X DMX %d (Index %d)",
                     pump["article"],pump["manu"],pump["dev"],pump["dmx"],pump["index"])
            publish_discovery(mc, pump)
            while True:
                # Steuerbefehle abarbeiten (serialisiert mit dem Poll)
                while not cmd_q.empty():
                    topic,val=cmd_q.get()
                    if not ENABLE_CTRL:
                        log.warning("Steuerung deaktiviert (enable_control=false) - ignoriere %s=%s",topic,val); continue
                    try:
                        if topic==CMD_SPEED: sess.set_speed_pct(pump, float(val))
                        elif topic==CMD_POWER: sess.set_power(pump, val.strip().upper()=="ON")
                    except Exception as e: log.error("Steuerbefehl %s=%s: %s",topic,val,e)
                st=sess.read_state(pump)
                publish_state(mc, st)
                log.debug("state=%s", st)
                time.sleep(max(2,POLL))
        except Exception as e:
            log.warning("Session-Fehler: %s - neu aufbauen in 10s", e)
            try: mc.publish(f"{NODE}/online","OFF")
            except Exception: pass
            sess.close(); time.sleep(10)

# ------------------------------------------------------------------ main
def main():
    log.info("OASE EGC Bridge start (mode=%s, control=%s, poll=%ss)", MODE, ENABLE_CTRL, POLL)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if MODE=="capture": run_capture()
    else: run_bridge()

if __name__=="__main__":
    main()
