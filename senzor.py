import paho.mqtt.client as mqtt
import time
from smbus2 import SMBus
from gpiozero import OutputDevice
#test

# 1. Configurare Hardware CrowPi (Releu pe GPIO 21, Buzzer pe GPIO 18)
releu = OutputDevice(21, active_high=True, initial_value=False)
buzzer = OutputDevice(18, active_high=True, initial_value=False)

# 2. Configurare Senzor DHT20 pe magistrala I2C-1 (Adresa 0x38)
I2C_BUS = 1
DHT20_ADDR = 0x38
bus = SMBus(I2C_BUS)

def citeste_dht20():
    """Citește temperatura și umiditatea reală direct din senzorul DHT20."""
    try:
        # Trimitem comanda de inițiere a măsurătorii către DHT20
        bus.write_i2c_block_data(DHT20_ADDR, 0xAC, [0x33, 0x00])
        time.sleep(0.08)  # Așteptăm 80ms conversia senzorului
        
        # Citim cei 7 octeți de date returnați
        data = bus.read_i2c_block_data(DHT20_ADDR, 0x71, 7)
        
        # Extragem biții pentru umiditate și temperatură (conform datasheet DHT20)
        umiditate_raw = ((data[1] << 16) | (data[2] << 8) | data[3]) >> 4
        temp_raw = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]
        
        umiditate = round((umiditate_raw / 1048576.0) * 100.0, 1)
        temperatura = round(((temp_raw / 1048576.0) * 200.0) - 50.0, 2)
        
        return temperatura, umiditate
    except Exception as e:
        print(f"[Eroare I2C DHT20]: {e}")
        return None, None

# 3. Funcțiile MQTT pentru telecomandă (Node-RED -> Placa fizică)
def on_message(client, userdata, msg):
    comanda = msg.payload.decode("utf-8")
    
    if comanda == "true":
        releu.on()  # Cuplează fizic releul pe placă (se aude CLICK și se aprinde LED-ul)
        print("\n>>> [TELECOMANDĂ] RELEU CUPLAT: Instalația de răcire a PORNIT! <<<")
    elif comanda == "false":
        releu.off() # Decuplează fizic releul
        print("\n>>> [TELECOMANDĂ] RELEU DECUPLAT: Instalația de răcire s-a OPRIT! <<<")

def on_connect(client, userdata, flags, rc):
    print("Conectat cu succes la brokerul MQTT local!")
    client.subscribe("scada/comanda/ventilator")

# Inițializare client MQTT (compatibil paho-mqtt 2.x)
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message

client.connect("localhost", 1883)
client.loop_start()

print("Sistem SCADA Hardware pornit! Se citesc datele de pe placa CrowPi...\n")

try:
    while True:
        temp_ambiant, umiditate = citeste_dht20()
        
        if temp_ambiant is not None:
            # --- SCALARE PENTRU MODELUL TRANSFORMATORULUI ---
            # La 23°C în laborator -> 65.0°C în SCADA
            # Dacă pui degetul pe senzor și urcă la 28°C -> 90.0°C în SCADA (declanșează la 85°C)
            temp_trafo = round(65.0 + (temp_ambiant - 23.0) * 5.0, 1)
            
            # Publicăm temperatura în Node-RED
            client.publish("scada/senzor/temp", temp_trafo)
            
            # Alarmă acustică scurtă pe placă dacă trece de 88°C
            if temp_trafo >= 88.0:
                buzzer.on()
                time.sleep(0.1)
                buzzer.off()
                
            print(f"[Senzor DHT20] Real: {temp_ambiant} °C (Umid: {umiditate}%)  --->  [SCADA Trafo]: {temp_trafo} °C")
        
        time.sleep(2)

except KeyboardInterrupt:
    print("\nOprire sistem și decuplare pini hardware...")
    releu.off()
    buzzer.off()
    bus.close()
    client.loop_stop()
    client.disconnect()
