#!/usr/bin/env python3
"""
OASE EGC Bridge - Home Assistant add-on
Local ONet connection of the OASE EGC pond pump controller to Home Assistant via MQTT.

Two operating modes (option `mode`):
  - bridge  : connects to the controller, polls state and sensors and
              publishes them via MQTT discovery; accepts control commands
              (speed, on/off) from HA (only if enable_control=true).
  - capture : poses once as the controller towards the OASE app and records
              the device password; stores it in /data/key.bin, switches the
              mode back to `bridge` and restarts the add-on.

The bridge keeps its OWN connection to the controller. The OASE app remains
usable in parallel (the controller accepts several clients); no exclusive mode
is set, so the app and firmware updates keep working.

Protocol basis: decompiled OASE Control app 5.2.0.
"""
import os, sys, socket, ssl, struct, time, json, threading, queue, tempfile, subprocess, signal, logging, urllib.request, warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)

# ------------------------------------------------------------------ Config
IP_RAW      = os.environ.get("CONTROLLER_IP", "172.17.30.5")
IP          = IP_RAW  # resolved via DNS in connect()
def resolve_host(h):
    try: return socket.gethostbyname(h)
    except Exception as e: log.debug("DNS %s: %s", h, e); return h
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

CERT_BACKDATE_S=3600

def make_server_ctx():
    # Fresh certificate per connect using the CURRENT clock. Validity ends before
    # 2050 so notAfter is encoded as UTCTime (the controller's embedded TLS stack
    # does not understand GeneralizedTime from 2050 on and reports "expired").
    # notBefore lies one hour back: the controller rejected a certificate created
    # at the same second once with "certificate expired" (2026-11-19, its clock is
    # presumably slightly behind). -not_before/-not_after need OpenSSL >= 3.4 (the
    # 3.24 base image ships 3.5); older versions fall back to -days.
    d=tempfile.mkdtemp(); c=os.path.join(d,"c.pem"); k=os.path.join(d,"k.pem")
    base=["openssl","req","-x509","-newkey","rsa:2048","-keyout",k,"-out",c,
          "-nodes","-subj","/CN=com.oase.easycontrol"]
    now=time.time(); fmt="%y%m%d%H%M%SZ"  # UTCTime, notAfter stays before 2050
    nb=time.strftime(fmt, time.gmtime(now-CERT_BACKDATE_S))
    na=time.strftime(fmt, time.gmtime(now+3650*86400))
    try:
        subprocess.run(base+["-not_before",nb,"-not_after",na], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        log.debug("openssl without -not_before - certificate starts now")
        subprocess.run(base+["-days","3650"], check=True, capture_output=True)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(c,k)
    try: ctx.minimum_version=ssl.TLSVersion.TLSv1
    except Exception: pass
    for cs in ("DEFAULT:@SECLEVEL=0","ALL:@SECLEVEL=0"):
        try: ctx.set_ciphers(cs); break
        except Exception: pass
    return ctx

def make_client_ctx():
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    try: ctx.minimum_version=ssl.TLSVersion.TLSv1
    except Exception: pass
    for cs in ("DEFAULT:@SECLEVEL=0","ALL:@SECLEVEL=0"):
        try: ctx.set_ciphers(cs); break
        except Exception: pass
    return ctx

# ------------------------------------------------------------------ ONet-Session
class OnetSession:
    """Holds the TLS connection opened by the controller and performs request/reply."""
    def __init__(self, ip, password):
        self.ip=ip; self.password=password; self.sock=None; self.lsock=None
        self.txn=0; self._buf=b""; self._lock=threading.Lock(); self.last_speed=128; self.last_mode=1

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
        self.ip=resolve_host(IP_RAW)
        if self.ip!=IP_RAW: log.info("Controller %s -> %s", IP_RAW, self.ip)
        th=threading.Thread(target=trig,daemon=True); th.start()
        try:
            raw,addr=self.lsock.accept()
        except socket.timeout:
            stop.set(); self.lsock.close()
            raise TimeoutError("Controller did not connect back (check firewall/IP)")
        stop.set()
        log.info("Controller connected from %s", addr[0])
        self.sock=make_server_ctx().wrap_socket(raw, server_side=True); self.sock.settimeout(8)
        self._buf=b""
        self.request(PT_ALIVE, b"", PT_ALIVE_R)
        if self.password:
            rep=self.request(PT_PW, self.password[:64].ljust(64,b"\x00"), PT_PW_R)
            if not (rep and rep[0]==1):
                raise PermissionError("CheckDevicePassword INCORRECT - check device_password")
            log.info("Authenticated (CheckDevicePassword correct)")
        else:
            log.warning("No device_password set - reads/writes will fail")

    def request(self, pt, payload, want, timeout=8, match=None):
        """Send one request and return the payload of the matching reply.

        A reply is accepted only if its packet type is `want` and, when given,
        `match(payload)` is true. Anything else - in particular late replies to
        earlier requests that timed out - is discarded. Without this check a late
        reply is taken as the answer to the next request of the same type and
        every following read is shifted (seen 2026-11-16: speed in the water
        temperature field, pump reported off while running).
        """
        with self._lock:
            self.sock.sendall(onet(pt,payload,self._nt()))
            end=time.time()+timeout
            while time.time()<end:
                while True:
                    pr=onet_parse(self._buf)
                    if not pr: break
                    rpt,_,rpl,consumed=pr
                    self._buf=self._buf[consumed:]
                    if rpt!=want: continue
                    if match is None or match(rpl): return rpl
                    log.debug("Discarding stale reply 0x%04X: %s", rpt, rpl[:32].hex())
                try: chunk=self.sock.recv(4096)
                except socket.timeout: break
                if not chunk: raise ConnectionError("Connection closed")
                self._buf+=chunk
            raise TimeoutError(f"no reply 0x{want:04X}")

    def close(self):
        for s in (self.sock,self.lsock):
            try: s.close()
            except Exception: pass

    # --- Pump functions -------------------------------------------------
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
        def match(rep):
            # RDM reply: PID at bytes 21-22; for parameter PIDs with an index
            # (0x0201) the controller echoes the index as first value byte.
            if len(rep)<24 or rep[0]!=0xCC: return False
            if struct.unpack(">H",rep[21:23])[0]!=pid: return False
            if data:
                val=rdm_value(rep)
                if not val or val[0]!=data[0]: return False
            return True
        try:
            rep=self.request(PT_RDM, rdm_build(manu,dev,pid,0x20,data=data), PT_RDM_R, match=match)
        except TimeoutError:
            log.warning("No reply for PID 0x%04X%s", pid, f" index {data[0]}" if data else "")
            raise
        return rdm_value(rep)

    def _read(self, name, fn):
        """Run one read. A missing or unusable reply only skips this value;
        a broken connection (OSError other than a timeout, e.g. BrokenPipe,
        ConnectionReset, SSL errors) is passed on so the session reconnects.
        Before 0.1.16 every error was swallowed at DEBUG: after the connection
        broke (2026-11-18 03:03) the bridge kept polling an empty session for
        ~35 h, HA kept stale values and then showed unknown."""
        try:
            return fn()
        except TimeoutError as e:
            log.debug("%s: %s", name, e)
        except OSError:
            raise
        except Exception as e:
            log.debug("%s: %s", name, e)
        return None

    def read_state(self, pump):
        m,d=pump["manu"],pump["dev"]; st={}
        def idx(i): return lambda: s16(self.rdm_get(m,d,0x0201,bytes([i])),1)
        for key,i in (("rpm",1),("el_temp",4),("water_temp",5),("power_w",10)):
            v=self._read(key, idx(i))
            if v is not None: st[key]=v
        sp=self._read("sp", lambda: self.rdm_get(m,d,0x8039))
        if sp: st["speed_raw"]=sp[0]; st["speed_pct"]=round(sp[0]*100/255)
        if st.get("speed_raw") and st["speed_raw"]>0: self.last_speed=st["speed_raw"]
        # SFC automatic (0x8038): 0 = off (manual), otherwise on
        md=self._read("sfc", lambda: self.rdm_get(m,d,0x8038))
        if md is not None and len(md)>=1: st["sfc"]=md[0]!=0
        lv=self._read("sfclvl", idx(9))
        if lv is not None: st["sfc_level"]={0:"Max",1:"Med",2:"Min"}.get(lv, str(lv))
        pw=self._read("power", lambda: self.rdm_get(m,d,0x1010))
        if pw is not None and len(pw)>=1: st["on"]=pw[0]==0xFF
        if st.get("power_w") is not None and st["power_w"]<0:
            st["power_w"]=0
        return st

    def set_power(self, pump, on):
        # Power = RDM OnOff 0x1010: 0xFF = on, 0x02 = standby (off)
        self._pidrdm_set(pump["index"], 0x1010, bytes([0xFF if on else 0x02]))
        log.info("Power %s (0x1010 <- %s)", "ON" if on else "OFF", "FF" if on else "02")

    def set_sfc(self, pump, on):
        self._pidrdm_set(pump["index"], 0x8038, bytes([1 if on else 0]))
        log.info("SFC automatic %s", "ON" if on else "OFF")

    def set_speed_pct(self, pump, pct):
        # Manual speed only applies without SFC -> switch SFC off first, then set 0x8039
        self._pidrdm_set(pump["index"], 0x8038, bytes([0]))
        val=max(0,min(255,round(pct*255/100)))
        self._pidrdm_set(pump["index"], 0x8039, bytes([val]))
        log.info("Manual power %d%% -> speed %d (SFC off)", round(pct), val)
        return val

    def _pidrdm_set(self, table_index, pid, data):
        payload=struct.pack("<I",table_index)+struct.pack("<H",pid)+bytes([1])+bytes([len(data)])+data
        self.request(PT_PIDRDM, payload, PT_PIDRDM_R)
        log.info("SET PID 0x%04X <- %s (index %d)", pid, data.hex(), table_index)

# ------------------------------------------------------------------ Capture
def run_capture():
    import ssl as _ssl
    log.info("Capture mode: learning controller identity from %s ...", IP)
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
        log.error("No DiscoveryReply from controller - powered on and reachable?"); return
    log.info("Identity learned. Now open the OASE app on the phone (power-cycle the controller if needed).")
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
                        log.warning("KEY CAPTURED (%d B)", len(pwb))
                        c.sendall(onet(PT_PW_R,bytes([1]),txn))
                        captured["key"]=pwb; captured["done"]=True; return
            c.close()
        except Exception as e: log.error("play: %s", e)
    us=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); us.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
    us.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try: us.bind(("",PORT_UDP))
    except OSError as e: log.error("UDP 5959 in use: %s", e); return
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
            log.info("RequestTcpConnection from phone %s (port %d)", addr[0], port)
            threading.Thread(target=play,args=(addr[0],port),daemon=True).start()
    if captured.get("key"):
        adopt_key_and_switch_to_bridge(captured["key"])
    else:
        log.error("Nothing captured. Check firewall (add-on host -> phone) and that the app was opened.")

# ------------------------------------------------------------------ Supervisor API
def _sup(method, path, body=None):
    tok=os.environ.get("SUPERVISOR_TOKEN","")
    if not tok: raise RuntimeError("no SUPERVISOR_TOKEN (hassio_api enabled?)")
    req=urllib.request.Request("http://supervisor"+path, method=method,
        headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=15) as r: return r.read()

def adopt_key_and_switch_to_bridge(pwb):
    """Store the key persistently, switch mode to bridge, restart the add-on."""
    try:
        with open(KEY_FILE,"wb") as f: f.write(pwb)
        log.warning("Key stored -> %s (%d B)", KEY_FILE, len(pwb))
    except Exception as e:
        log.error("Storing key failed: %s", e); return
    try:
        opts={}
        try: opts=json.load(open("/data/options.json"))
        except Exception: pass
        opts["mode"]="bridge"
        _sup("POST","/addons/self/options", {"options":opts})
        log.warning("Mode set to 'bridge' automatically. Restarting add-on ...")
        _sup("POST","/addons/self/restart")
    except Exception as e:
        log.error("Automatic switch via Supervisor failed (%s). Key is in %s, set mode to bridge manually.", e, KEY_FILE)

# ------------------------------------------------------------------ MQTT-Bridge
import paho.mqtt.client as mqtt

DISC="homeassistant"; NODE="oase_egc"
AVAIL=f"{NODE}/availability"
CMD_SPEED=f"{NODE}/speed/set"; CMD_SFC=f"{NODE}/sfc/set"; CMD_POWER=f"{NODE}/power/set"
cmd_q=queue.Queue()
MAX_EMPTY_POLLS=3  # polls without any value before the session is rebuilt

def mqtt_connect():
    c=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oase-egc-bridge")
    if MQTT_USER: c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.will_set(AVAIL,"offline",retain=True)
    def on_connect(cl,u,f,rc,props=None):
        cl.subscribe(CMD_SPEED); cl.subscribe(CMD_SFC); cl.subscribe(CMD_POWER); cl.publish(AVAIL,"online",retain=True)
        log.info("MQTT connected (%s:%s)", MQTT_HOST, MQTT_PORT)
    def on_msg(cl,u,m):
        cmd_q.put((m.topic, m.payload.decode(errors="replace")))
    c.on_connect=on_connect; c.on_message=on_msg
    c.connect(MQTT_HOST, MQTT_PORT, 60); c.loop_start(); return c

def publish_discovery(c, pump):
    dev={"identifiers":[f"oase_egc_{pump['dev']:08x}"],"name":"OASE EGC Pond Pump",
         "manufacturer":"OASE","model":f"AquaMax Eco Expert (article {pump['article']})"}
    def cfg(comp,obj,payload):
        payload.update({"availability_topic":AVAIL,"device":dev,"unique_id":f"{NODE}_{obj}"})
        c.publish(f"{DISC}/{comp}/{NODE}/{obj}/config", json.dumps(payload), retain=True)
    cfg("sensor","rpm",{"name":"Speed","state_topic":f"{NODE}/rpm","unit_of_measurement":"rpm","icon":"mdi:fan","state_class":"measurement"})
    cfg("sensor","water_temp",{"name":"Water temperature","state_topic":f"{NODE}/water_temp","unit_of_measurement":"°C","device_class":"temperature","state_class":"measurement"})
    cfg("sensor","el_temp",{"name":"Electronics temperature","state_topic":f"{NODE}/el_temp","unit_of_measurement":"°C","device_class":"temperature","state_class":"measurement"})
    cfg("sensor","power_w",{"name":"Power consumption","state_topic":f"{NODE}/power_w","unit_of_measurement":"W","device_class":"power","state_class":"measurement"})
    cfg("sensor","speed_setpoint",{"name":"Speed setpoint","state_topic":f"{NODE}/speed_raw","unit_of_measurement":"/255","icon":"mdi:speedometer","state_class":"measurement"})
    cfg("number","speed",{"name":"Manual power","command_topic":CMD_SPEED,"state_topic":f"{NODE}/speed_pct","min":0,"max":100,"step":1,"unit_of_measurement":"%","icon":"mdi:fan"})
    cfg("switch","power",{"name":"Pump","command_topic":CMD_POWER,"state_topic":f"{NODE}/power_state","payload_on":"ON","payload_off":"OFF","icon":"mdi:water-pump"})
    cfg("switch","sfc",{"name":"SFC automatic","command_topic":CMD_SFC,"state_topic":f"{NODE}/sfc","payload_on":"ON","payload_off":"OFF","icon":"mdi:thermostat"})
    cfg("sensor","sfc_level",{"name":"SFC level","state_topic":f"{NODE}/sfc_level","device_class":"enum","options":["Max","Med","Min"],"entity_category":"diagnostic","icon":"mdi:stairs"})
    cfg("binary_sensor","online",{"name":"Online","state_topic":f"{NODE}/online","payload_on":"ON","payload_off":"OFF","device_class":"connectivity","entity_category":"diagnostic"})

def publish_state(c, st):
    if not st: return
    for k,topic in (("rpm","rpm"),("water_temp","water_temp"),("el_temp","el_temp"),
                    ("power_w","power_w"),("speed_raw","speed_raw"),("speed_pct","speed_pct"),
                    ("sfc_level","sfc_level")):
        if st.get(k) is not None: c.publish(f"{NODE}/{topic}", st[k])
    if "sfc" in st:
        c.publish(f"{NODE}/sfc", "ON" if st["sfc"] else "OFF")
    if "on" in st:
        c.publish(f"{NODE}/power_state", "ON" if st["on"] else "OFF")
    c.publish(f"{NODE}/online","ON")

def run_bridge():
    if not MQTT_HOST:
        log.error("No MQTT broker configured. Set in the add-on options "
                  "mqtt_host (name or IP), mqtt_port, mqtt_user, mqtt_pass. Waiting 30 s ...")
        time.sleep(30); return
    mc=mqtt_connect()
    while True:
        sess=OnetSession(IP, PASSWORD)
        try:
            sess.connect(wait=30)
            pump=sess.find_pump()
            if not pump:
                log.error("No pump found in the device table."); sess.close(); time.sleep(10); continue
            log.info("Pump: article %s UID %04X:%08X DMX %d (index %d)",
                     pump["article"],pump["manu"],pump["dev"],pump["dmx"],pump["index"])
            publish_discovery(mc, pump)
            empty=0
            while True:
                # Process control commands (serialised with polling)
                while not cmd_q.empty():
                    topic,val=cmd_q.get()
                    if not ENABLE_CTRL:
                        log.warning("Control disabled (enable_control=false) - ignoring %s=%s",topic,val); continue
                    try:
                        if topic==CMD_SPEED: sess.set_speed_pct(pump, float(val))
                        elif topic==CMD_SFC: sess.set_sfc(pump, val.strip().upper()=="ON")
                        elif topic==CMD_POWER: sess.set_power(pump, val.strip().upper()=="ON")
                    except Exception as e: log.error("Control command %s=%s: %s",topic,val,e)
                st=sess.read_state(pump)
                log.debug("state=%s", st)
                if st:
                    empty=0; publish_state(mc, st)
                else:
                    empty+=1
                    log.warning("No values from controller (%d/%d)", empty, MAX_EMPTY_POLLS)
                    if empty>=MAX_EMPTY_POLLS:
                        raise ConnectionError(f"no values for {empty} polls")
                time.sleep(max(2,POLL))
        except Exception as e:
            log.warning("Session error: %s - reconnecting in 10 s", e)
            try: mc.publish(f"{NODE}/online","OFF")
            except Exception: pass
            sess.close(); time.sleep(10)

# ------------------------------------------------------------------ main
def wait_for_clock(max_wait=60):
    import datetime as _dt
    waited=0
    while _dt.datetime.now().year < 2024 and waited < max_wait:
        log.warning("System clock not yet synchronised (%s) - waiting for NTP", _dt.datetime.now())
        time.sleep(3); waited+=3

def main():
    log.info("OASE EGC Bridge start (mode=%s, control=%s, poll=%ss)", MODE, ENABLE_CTRL, POLL)
    wait_for_clock()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if MODE=="capture": run_capture()
    else: run_bridge()

if __name__=="__main__":
    main()
