import json
import random
import time
import paho.mqtt.client as mqtt

print("[INFO]: Rulează în mod SIMULARE completă (fără hardware fizic conectat).")

# Clase de simulare simplificate (nu apelează deloc gpiozero)
class MockOutputDevice:
    def __init__(self, pin, **kwargs):
        self.pin = pin
        self.is_active = False
    def on(self):
        self.is_active = True
        print(f"[SIMULARE] Releul / Pinul {self.pin} -> PORNIT (ON)")
    def off(self):
        self.is_active = False
        print(f"[SIMULARE] Releul / Pinul {self.pin} -> OPRIT (OFF)")

releu = MockOutputDevice(21)
buzzer = MockOutputDevice(18)

def citeste_senzori():
    """Generează date simulate plauzibile pentru parcul fotovoltaic."""
    temp = round(22.0 + random.uniform(-0.5, 2.0), 2)
    hum = round(45.0 + random.uniform(-2.0, 2.0), 1)
    lux = round(55000.0 + random.uniform(-1000.0, 1000.0), 1)
    return temp, hum, lux

def on_connect(client, userdata, flags, rc):
    print("Conectat cu succes la brokerul MQTT local!")
    client.subscribe("scada/comanda/ventilator")

def on_message(client, userdata, msg):
    cmd = msg.payload.decode("utf-8")
    if cmd == "true":
        releu.on()
    elif cmd == "false":
        releu.off()

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect("localhost", 1883)
client.loop_start()

print("Sistemul de simulare PV a pornit. Se transmit datele spre Node-RED...\n")

try:
    while True:
        temp, hum, lux = citeste_senzori()
        payload = {"temp": temp, "hum": hum, "lux": lux}
        
        client.publish("scada/pv/data", json.dumps(payload))
        print(f"[Trimis MQTT] Temp: {temp} °C | Umiditate: {hum}% | Lux: {lux}")
        
        time.sleep(2)
except KeyboardInterrupt:
    client.loop_stop()
    client.disconnect()
    print("\nProgram oprit.")