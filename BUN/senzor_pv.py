import csv
import json
import math
import os
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from gpiozero import OutputDevice
from smbus2 import SMBus

# ---------------------------------------------------------------
# Hardware CrowPi
#   releu  GPIO 21 -> ventilatorul panoului, comandat din panoul admin
#   buzzer GPIO 18 -> DOAR la login cu RFID (bip scurt = acces permis)
#   RFID   MFRC522 pe SPI -> login admin
# ---------------------------------------------------------------
releu = OutputDevice(21, active_high=True, initial_value=False)
buzzer = OutputDevice(18, active_high=True, initial_value=False)

I2C_BUS = 1
DHT20_ADDR = 0x38
LIGHT_ADDR = 0x5C   # BH1750; adresa găsită cu:  i2cdetect -y 1

bus = SMBus(I2C_BUS)

# ---------------------------------------------------------------
# Admini RFID  (UID card -> nume)
# Scanează cardul: dacă nu e în listă, scriptul îi afișează UID-ul.
# Copiază UID-ul aici și repornește scriptul.
# ---------------------------------------------------------------
ADMINI = {
    508001937949: "Dispecer",
}
SESIUNE_ADMIN_S = 300   # cât rămâne deschis panoul admin după scanare (secunde)
RFID_SPI = (0, 0)       # (bus, device) al cititorului RFID; îl afișează test_rfid.py
JURNAL_ACCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acces_rfid.csv")

T_DATE = "scada/pv/data"                     # date senzori + starea admin -> pagină
T_ACCES = "scada/pv/acces"                   # evenimente de acces -> pagină
T_CMD_VENTILATOR = "scada/comanda/ventilator"
T_CMD_ADMIN = "scada/comanda/admin"


# ---------------------------------------------------------------
# Senzori
# ---------------------------------------------------------------
def citeste_dht20():
    """Temperatură (°C) și umiditate (%) de la DHT20."""
    try:
        bus.write_i2c_block_data(DHT20_ADDR, 0xAC, [0x33, 0x00])
        time.sleep(0.08)
        data = bus.read_i2c_block_data(DHT20_ADDR, 0x71, 7)
        umiditate_raw = ((data[1] << 16) | (data[2] << 8) | data[3]) >> 4
        temp_raw = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]
        umiditate = round((umiditate_raw / 1048576.0) * 100.0, 1)
        temperatura = round(((temp_raw / 1048576.0) * 200.0) - 50.0, 2)
        return temperatura, umiditate
    except Exception as e:
        print(f"[Eroare I2C DHT20]: {e}")
        return None, None


def citeste_lux():
    """Lumină în lux de la BH1750 (mod continuu, rezoluție înaltă).
    Returnează None dacă senzorul nu răspunde."""
    try:
        bus.write_byte(LIGHT_ADDR, 0x10)      # Continuous H-resolution mode
        time.sleep(0.18)                      # conversie ~120-180 ms
        d = bus.read_i2c_block_data(LIGHT_ADDR, 0x10, 2)
        return round(((d[0] << 8) | d[1]) / 1.2, 1)
    except Exception as e:
        print(f"[Eroare I2C lumină]: {e}")
        return None


# ---------------------------------------------------------------
# Buzzer (folosit doar la login RFID)
# ---------------------------------------------------------------
def bip(n=1, durata=0.12, pauza=0.08):
    for i in range(n):
        buzzer.on()
        time.sleep(durata)
        buzzer.off()
        if i < n - 1:
            time.sleep(pauza)


# ---------------------------------------------------------------
# Sesiune admin
# ---------------------------------------------------------------
sesiune = {"nume": None, "uid": None, "pana_la": 0.0}
lacat = threading.Lock()


def admin_activ():
    with lacat:
        return sesiune["nume"] is not None and time.time() < sesiune["pana_la"]


def stare_admin():
    """Starea panoului admin, trimisă paginii odată cu fiecare mesaj."""
    with lacat:
        activ = sesiune["nume"] is not None and time.time() < sesiune["pana_la"]
        return {
            "admin": sesiune["nume"] if activ else None,
            "admin_s": max(0, math.ceil(sesiune["pana_la"] - time.time())) if activ else 0,
            "releu": bool(releu.value),
        }


def eveniment(tip, nume=None, uid=None):
    """Trimite un eveniment de acces către pagină și îl scrie în acces_rfid.csv."""
    mesaj = {"eveniment": tip, "nume": nume, "uid": uid, **stare_admin()}
    client.publish(T_ACCES, json.dumps(mesaj))
    try:
        nou = not os.path.exists(JURNAL_ACCES)
        with open(JURNAL_ACCES, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nou:
                w.writerow(["data_ora", "eveniment", "nume", "uid"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), tip, nume or "", uid or ""])
    except OSError as e:
        print(f"[Jurnal acces]: {e}")


def card_scanat(uid):
    nume = ADMINI.get(uid)
    if nume is None:
        print(f"\n>>> [RFID] Card necunoscut, UID {uid}. Adaugă-l în ADMINI ca să devină admin. <<<")
        eveniment("respins", uid=uid)
        bip(3, durata=0.07, pauza=0.07)
        return
    with lacat:
        sesiune.update(nume=nume, uid=uid, pana_la=time.time() + SESIUNE_ADMIN_S)
    print(f"\n>>> [RFID] Admin logat: {nume} (UID {uid}) <<<")
    eveniment("login", nume=nume, uid=uid)
    bip(1, durata=0.15)


def iesire_admin(tip):
    with lacat:
        nume, uid = sesiune["nume"], sesiune["uid"]
        if nume is None:
            return
        sesiune.update(nume=None, uid=None, pana_la=0.0)
    print(f"\n>>> [ADMIN] {nume}: {tip} <<<")
    eveniment(tip, nume=nume, uid=uid)


def verifica_expirare():
    with lacat:
        expirat = sesiune["nume"] is not None and time.time() >= sesiune["pana_la"]
    if expirat:
        iesire_admin("expirat")


# ---------------------------------------------------------------
# RFID (MFRC522)
# ---------------------------------------------------------------
class CititorRFID:
    """Citește UID-ul cardurilor cu biblioteca mfrc522, pe portul SPI din RFID_SPI."""

    def __init__(self, bus, device):
        from mfrc522 import MFRC522
        self.r = MFRC522(bus=bus, device=device)

    def read_id_no_block(self):
        st, _ = self.r.MFRC522_Request(self.r.PICC_REQIDL)
        if st != self.r.MI_OK:
            return None
        st, uid = self.r.MFRC522_Anticoll()
        if st != self.r.MI_OK:
            return None
        n = 0
        for b in uid:
            n = n * 256 + b
        return n


def bucla_rfid(cititor):
    vazut = {}   # uid -> ultima dată când a fost citit
    while True:
        try:
            uid = cititor.read_id_no_block()
        except Exception as e:
            print(f"[Eroare RFID]: {e}")
            time.sleep(1)
            continue
        if uid:
            # Un card ținut pe cititor se citește intermitent; îl considerăm
            # scanare nouă doar dacă a lipsit cel puțin 2 secunde.
            if time.time() - vazut.get(uid, 0) > 2:
                card_scanat(uid)
            vazut[uid] = time.time()
        time.sleep(0.2)


def porneste_rfid():
    try:
        cititor = CititorRFID(*RFID_SPI)
    except Exception as e:
        print(f"[RFID indisponibil]: {e}")
        print("    Monitorizarea merge mai departe, dar fără login admin. Rulează test_rfid.py.")
        return
    threading.Thread(target=bucla_rfid, args=(cititor,), daemon=True).start()
    print(f"Cititor RFID pornit pe /dev/spidev{RFID_SPI[0]}.{RFID_SPI[1]}. Apropie cardul pentru login admin.")


# ---------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------
def on_message(client, userdata, msg):
    comanda = msg.payload.decode("utf-8").strip()

    if msg.topic == T_CMD_ADMIN:
        if comanda == "logout":
            iesire_admin("logout")
        return

    if msg.topic == T_CMD_VENTILATOR and comanda in ("true", "false"):
        if not admin_activ():
            print("\n>>> [ADMIN] Comandă ventilator respinsă: niciun admin logat <<<")
            eveniment("comanda_respinsa")
            return
        if comanda == "true":
            releu.on()
        else:
            releu.off()
        with lacat:
            nume = sesiune["nume"]
        print(f"\n>>> [ADMIN {nume}] Ventilator {'PORNIT' if comanda == 'true' else 'OPRIT'} <<<")
        eveniment("releu", nume=nume)


def on_connect(client, userdata, flags, rc):
    print("Conectat cu succes la brokerul MQTT local!")
    client.subscribe([(T_CMD_VENTILATOR, 0), (T_CMD_ADMIN, 0)])


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect("localhost", 1883)
client.loop_start()

porneste_rfid()
print("Sistem PV pornit! Se citesc temperatură, umiditate și lumină...\n")

try:
    while True:
        verifica_expirare()
        temp, umid = citeste_dht20()
        lux = citeste_lux()

        if temp is not None:
            # Date brute pentru dashboardul fotovoltaic (calculul se face în pagină)
            payload = {"temp": temp, "hum": umid, "lux": lux if lux is not None else 0, **stare_admin()}
            client.publish(T_DATE, json.dumps(payload))
            print(f"[DHT20] {temp} °C  {umid} %   [Lumină] {lux} lux")

        time.sleep(2)

except KeyboardInterrupt:
    print("\nOprire sistem...")
    releu.off()
    buzzer.off()
    bus.close()
    client.loop_stop()
    client.disconnect()
    try:
        import RPi.GPIO as GPIO   # folosit de biblioteca mfrc522
        GPIO.cleanup()
    except Exception:
        pass
