import webview
import asyncio
import threading
from bleak import BleakScanner, BleakClient
import json
import os
import sys

# Ninebot / Xiaomi UUID Sets
UUID_SETS = [
    {
        "name": "Nordic UART",
        "service": "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
        "rx": "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
        "tx": "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
    },
    {
        "name": "Xiaomi/Ninebot Standard",
        "service": "0000ff01-0000-1000-8000-00805f9b34fb",
        "rx": "0000ff01-0000-1000-8000-00805f9b34fb",
        "tx": "0000ff01-0000-1000-8000-00805f9b34fb"
    }
]

class ScooterAPI:
    def __init__(self):
        self.client = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.window = None
        self.lock = asyncio.Lock()
        self.is_polling = False
        self.active_rx = None
        self.active_tx = None

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def set_window(self, window):
        self.window = window

    def log(self, msg):
        if self.window:
            try:
                safe_msg = str(msg).replace("'", "\\'").replace("\n", " ")
                # Sicherstellen, dass JS geladen ist
                self.window.evaluate_js(f"if(window.addLog) window.addLog('{safe_msg}')")
            except: pass

    def calc_crc(self, data):
        sum_val = sum(data[2:])
        crc = sum_val ^ 0xFFFF
        return [crc & 0xFF, (crc >> 8) & 0xFF]

    def build_packet(self, addr, cmd, payload):
        length = len(payload) + 2
        packet = [0x55, 0xAA, length, addr, cmd] + payload
        crc = self.calc_crc(packet)
        return bytes(packet + crc)

    def scan_and_connect(self):
        future = asyncio.run_coroutine_threadsafe(self._scan_and_connect(), self.loop)
        return future.result()

    async def _scan_and_connect(self):
        try:
            self.log("Nativer Bluetooth-Scan startet (V1.5)...")
            # Wir suchen ohne jegliche Namensfilter um ALLES zu finden
            devices = await BleakScanner.discover(timeout=10.0)
            self.log(f"Scan fertig. {len(devices)} Geräte in Reichweite.")
            
            scooter = None
            for d in devices:
                name = (d.name or "Unbekannt").upper()
                address = d.address
                self.log(f"Gefunden: {name} [{address}]")
                # Wir suchen nach allem was wie ein Scooter aussieht oder dein 58ata
                if any(x in name for x in ["NINEBOT", "MISCOOTER", "G30", "F40", "ES", "ATA", "SCOOTER", "58ATA", "F20"]):
                    scooter = d
                    break
            
            if not scooter:
                self.log("Kein Scooter automatisch erkannt. Bitte Gerätenamen prüfen!")
                return False

            self.log(f"Verbinde mit {scooter.name} ({scooter.address})...")
            if self.client and self.client.is_connected:
                try: await self.client.disconnect()
                except: pass

            self.client = BleakClient(scooter, timeout=20.0)
            await self.client.connect()
            self.log("VERBINDUNG HERGESTELLT!")
            
            # Rest der Logik (Services erkennen) bleibt gleich...
            services = await self.client.get_services()
            self.active_rx = None
            self.active_tx = None

            for u_set in UUID_SETS:
                for s in services:
                    if s.uuid.lower() == u_set["service"].lower():
                        self.log(f"Dienst: {u_set['name']}")
                        self.active_rx = u_set["rx"]
                        self.active_tx = u_set["tx"]
                        break
                if self.active_rx: break

            if not self.active_rx:
                for s in services:
                    for c in s.characteristics:
                        if "write" in c.properties or "write-without-response" in c.properties:
                            self.active_rx = c.uuid
                        if "notify" in c.properties:
                            self.active_tx = c.uuid
                        if self.active_rx and self.active_tx: break

            if self.active_tx:
                await self.client.start_notify(self.active_tx, self._notification_handler)
            
            if self.window:
                name_json = json.dumps({"name": scooter.name})
                self.window.evaluate_js(f"if(window.setDevice) window.setDevice({name_json})")
            
            self.is_polling = True
            self.loop.create_task(self._status_polling())
            return True
        except Exception as e:
            self.log(f"BT-Fehler: {str(e)}")
            return False

    async def _status_polling(self):
        while self.is_polling and self.client and self.client.is_connected:
            try:
                async with self.lock:
                    packet = self.build_packet(0x20, 0x01, [0x1A, 0x0A])
                    await self.client.write_gatt_char(self.active_rx, packet, response=False)
            except: pass
            await asyncio.sleep(1.0)

    def _notification_handler(self, sender, data):
        try:
            if len(data) >= 12 and data[0] == 0x55 and data[1] == 0xAA:
                speed_raw = data[6] | (data[7] << 8)
                speed = speed_raw / 100.0
                battery = data[10]
                if self.window:
                    stats_json = json.dumps({"speed": round(speed, 1), "battery": battery})
                    self.window.evaluate_js(f"if(window.setStats) window.setStats({stats_json})")
        except: pass

    def send_scooter_command(self, payload, addr=0x20, cmd=0x03):
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send_command(payload, addr, cmd), self.loop)

    async def _send_command(self, payload, addr, cmd):
        if not self.client or not self.client.is_connected or not self.active_rx:
            return
        
        try:
            async with self.lock:
                # Beep/Auth
                p = self.build_packet(0x20, 0x03, [0x3F, 0x01])
                await self.client.write_gatt_char(self.active_rx, p, response=False)
                await asyncio.sleep(0.1)
                
                # Main Command
                packet = self.build_packet(addr, cmd, payload)
                await self.client.write_gatt_char(self.active_rx, packet, response=False)
                self.log(f"Befehl {payload[0]:02x} gesendet!")
        except Exception as e:
            self.log(f"Sende-Fehler: {str(e)}")

    def disconnect(self):
        self.is_polling = False
        if self.client:
            asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop)
        self.log("Getrennt.")

def get_resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

if __name__ == '__main__':
    api = ScooterAPI()
    
    # EXTREMES CACHE-BUSTING
    import time
    import random
    # Wir hängen eine Zufallszahl an, damit Vercel JEDES MAL neu lädt
    url = f'https://atatuning-pro-yjf5.vercel.app/?cache={time.time()}_{random.randint(1000,9999)}'
    
    window = webview.create_window(
        'AtaTuning Pro (Native BT)', 
        url, 
        js_api=api,
        width=1200, 
        height=800,
        background_color='#000000'
    )
    api.set_window(window)
    # private_mode=True löscht alle Browser-Daten beim Start
    webview.start(private_mode=True, debug=True)
