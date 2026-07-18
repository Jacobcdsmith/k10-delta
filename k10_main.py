# k10_main.py — MicroPython firmware for UNIHIKER K10 (ESP32-S3)
# Flash as main.py on the device.

import machine
import network
import usocket as socket
import ujson
import time
import gc
from machine import I2C, Pin, ADC, PWM

# ── WiFi / Host config (edit these) ───────────────────────────────────────────
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
HOST_IP = "192.168.1.100"   # IP of the machine running host.py
HOST_PORT = 5555
RECONNECT_DELAY_S = 5

# ── Hardware Setup ─────────────────────────────────────────────────────────────
# IMU (MPU-6050 or similar on I2C bus 0)
i2c = I2C(0, scl=Pin(22), sda=Pin(21), freq=400000)
IMU_ADDR = 0x68   # MPU-6050 default; change if AD0 is HIGH → 0x69

def _imu_read_word(reg):
    data = i2c.readfrom_mem(IMU_ADDR, reg, 2)
    val = (data[0] << 8) | data[1]
    return val - 65536 if val > 32767 else val

def imu_init():
    try:
        i2c.writeto_mem(IMU_ADDR, 0x6B, b'\x00')  # wake up
    except OSError:
        pass  # IMU not present; sensor reads will return zeros

def read_imu():
    try:
        ax = _imu_read_word(0x3B) / 16384.0
        ay = _imu_read_word(0x3D) / 16384.0
        az = _imu_read_word(0x3F) / 16384.0
        gx = _imu_read_word(0x43) / 131.0
        gy = _imu_read_word(0x45) / 131.0
        gz = _imu_read_word(0x47) / 131.0
        return [ax, ay, az], [gx, gy, gz]
    except OSError:
        return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]

# Microphone on ADC (GPIO34; adjust to your board)
mic_adc = ADC(Pin(34))
mic_adc.atten(ADC.ATTN_11DB)

def read_mic():
    s = 0
    for _ in range(10):
        s += mic_adc.read()
    return s // 10

# Buttons (active-low on GPIO0 and GPIO2)
btn_a = Pin(0, Pin.IN, Pin.PULL_UP)
btn_b = Pin(2, Pin.IN, Pin.PULL_UP)

# RGB LED via PWM (common-anode; duty=0 → full brightness, 1023 → off)
rgb_r = PWM(Pin(25), freq=1000, duty=1023)
rgb_g = PWM(Pin(26), freq=1000, duty=1023)
rgb_b = PWM(Pin(27), freq=1000, duty=1023)

# Speaker on GPIO14
speaker = PWM(Pin(14), freq=440, duty=0)

# Display — stub; replace with your board's LCD library
def show_display(text):
    pass  # e.g.: lcd.set_text(text)

# ── WiFi ───────────────────────────────────────────────────────────────────────
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi...")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        deadline = time.ticks_add(time.ticks_ms(), 15000)
        while not wlan.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                print("WiFi timeout — rebooting")
                machine.reset()
            time.sleep(0.2)
    print("WiFi:", wlan.ifconfig())

# ── TCP Client ─────────────────────────────────────────────────────────────────
sock = None

def tcp_connect():
    global sock
    try:
        if sock is not None:
            sock.close()
    except Exception:
        pass
    sock = socket.socket()
    sock.connect((HOST_IP, HOST_PORT))
    sock.settimeout(0.02)
    print("TCP connected to host")

def tcp_send(msg_dict):
    global sock
    try:
        sock.write(ujson.dumps(msg_dict) + "\n")
    except Exception as e:
        print("TCP send error:", e)
        time.sleep(RECONNECT_DELAY_S)
        tcp_connect()

# ── Actuators ──────────────────────────────────────────────────────────────────
def set_rgb(r, g, b):
    # r,g,b in 0–100; invert for common-anode
    rgb_r.duty(1023 - int(r * 10.23))
    rgb_g.duty(1023 - int(g * 10.23))
    rgb_b.duty(1023 - int(b * 10.23))

def set_tone(freq_hz):
    if freq_hz == 0:
        speaker.duty(0)
    else:
        speaker.freq(int(freq_hz))
        speaker.duty(512)

def actuate(msg):
    if "rgb" in msg:
        r, g, b = msg["rgb"]
        set_rgb(r, g, b)
    if "tone" in msg:
        set_tone(msg["tone"])
    if "display_str" in msg:
        show_display(msg["display_str"])

# ── OTA ────────────────────────────────────────────────────────────────────────
def handle_ota(msg):
    """Receive base64-encoded MicroPython firmware, verify hash, write and reboot."""
    import uhashlib
    import ubinascii

    mpy_b64 = msg.get("mpy_base64", "")
    expected_hash = msg.get("otahash", "")
    if not mpy_b64:
        print("OTA: no firmware data")
        return

    try:
        fw = ubinascii.a2b_base64(mpy_b64)
        h = uhashlib.sha256(fw).digest()
        actual_hash = ubinascii.hexlify(h).decode()
        if expected_hash and actual_hash != expected_hash:
            print("OTA hash mismatch! Aborting.")
            tcp_send({"t": "ota_error", "msg": "hash mismatch"})
            return

        # Write to alternate partition (simple approach: write to /flash/k10_ota.py)
        with open("/flash/k10_ota.py", "wb") as f:
            f.write(fw)
        print("OTA: firmware written, rebooting...")
        tcp_send({"t": "ota_ok"})
        time.sleep(0.5)
        machine.reset()
    except Exception as e:
        print("OTA error:", e)
        tcp_send({"t": "ota_error", "msg": str(e)})

# ── Main Loop ──────────────────────────────────────────────────────────────────
def main():
    imu_init()
    connect_wifi()
    tcp_connect()

    recv_buf = ""
    last_hb = time.ticks_ms()
    HB_INTERVAL = 5000  # ms

    while True:
        # Read and send sensors
        acc, gyro = read_imu()
        mic = read_mic()
        btn = int(btn_a.value() == 0 or btn_b.value() == 0)
        tcp_send({
            "t": "sensor",
            "ts": time.ticks_ms(),
            "acc": acc,
            "gyro": gyro,
            "mic": mic,
            "btn": btn,
            "light": 0  # stub; replace with BH1750 read
        })

        # Non-blocking receive
        try:
            chunk = sock.read(256)
            if chunk:
                recv_buf += chunk.decode("utf-8")
                while "\n" in recv_buf:
                    line, recv_buf = recv_buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = ujson.loads(line)
                        mtype = msg.get("t")
                        if mtype == "actuator":
                            actuate(msg)
                        elif mtype == "code_update":
                            handle_ota(msg)
                        elif mtype == "heartbeat_req":
                            tcp_send({"t": "heartbeat", "uptime": time.ticks_ms() // 1000, "free_mem": gc.mem_free()})
                    except ValueError:
                        pass
        except OSError:
            pass  # timeout

        # Periodic heartbeat
        now = time.ticks_ms()
        if time.ticks_diff(now, last_hb) >= HB_INTERVAL:
            tcp_send({"t": "heartbeat", "uptime": time.ticks_ms() // 1000, "free_mem": gc.mem_free()})
            last_hb = now

        time.sleep_ms(10)  # ~100 Hz

if __name__ == "__main__":
    main()
