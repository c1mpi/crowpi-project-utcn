import json
import time

import paho.mqtt.client as mqtt
from gpiozero import OutputDevice
from smbus2 import SMBus

# ---------------------------------------------------------------
# Hardware CrowPi (neschimbat): releu GPIO 21, buzzer GPIO 18
# ---------------------------------------------------------------
releu = OutputDevice(21, active_high=True, initial_value=False)
buzzer = OutputDevice(18, active_high=True, initial_value=False)

I2C_BUS = 1
DHT20_ADDR = 0x38
# Senzorul de lumină: pe CrowPi 2 este de obicei un BH1750 (0x23 sau 0x5C).
# Verifică cu:  i2cdetect -y 1   și ajustează adresa dacă e nevoie.
LIGHT_ADDR = 0x23

bus = SMBus(I2C_BUS)


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
# MQTT
# ---------------------------------------------------------------
def on_message(client, userdata, msg):
    comanda = msg.payload.decode("utf-8")
    if comanda == "true":
        releu.on()
        print("\n>>> [TELECOMANDĂ] RELEU CUPLAT <<<")
    elif comanda == "false":
        releu.off()
        print("\n>>> [TELECOMANDĂ] RELEU DECUPLAT <<<")


def on_connect(client, userdata, flags, rc):
    print("Conectat cu succes la brokerul MQTT local!")
    client.subscribe("scada/comanda/ventilator")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect("localhost", 1883)
client.loop_start()

print("Sistem PV pornit! Se citesc temperatură, umiditate și lumină...\n")

try:
    while True:
        temp, umid = citeste_dht20()
        lux = citeste_lux()

        if temp is not None:
            # Date brute pentru dashboardul fotovoltaic (calculul se face în pagină)
            payload = {"temp": temp, "hum": umid, "lux": lux if lux is not None else 0}
            client.publish("scada/pv/data", json.dumps(payload))

            # Compatibilitate cu fluxul vechi (demo transformator)
            temp_trafo = round(65.0 + (temp - 23.0) * 5.0, 1)
            client.publish("scada/senzor/temp", temp_trafo)

            if temp_trafo >= 88.0:
                buzzer.on()
                time.sleep(0.1)
                buzzer.off()

            print(f"[DHT20] {temp} °C  {umid} %   [Lumină] {lux} lux")

        time.sleep(2)

except KeyboardInterrupt:
    print("\nOprire sistem...")
    releu.off()
    buzzer.off()
    bus.close()
    client.loop_stop()
    client.disconnect()
