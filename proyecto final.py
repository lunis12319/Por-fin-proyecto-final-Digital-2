from machine import Pin, PWM, SPI, I2C
from time import sleep_ms
import network
import time
import dht
import onewire
import ds18x20
import ujson
import gc
import socket
import urequests
from mfrc522 import MFRC522
from ssd1306 import SSD1306_I2C

# ============================================================
# CONFIGURACION
# ============================================================

WIFI_SSID = "Sara"
WIFI_PASS = "3008742602"

# Cambia esta URL por la IP que imprime Http_rapido.py en el PC.
SERVER_URL = "http://10.159.217.249:8000/api/datos"
NODE_ID = "nevera01"

# La ESP32 sigue leyendo sensores aunque el servidor no responda.
HTTP_INTERVAL = 3000
HTTP_BACKOFF = 15000
NET_TIMEOUT = 0.8

# Telegram queda apagado por defecto para que no frene los sensores.
# Activalo solo despues de crear un token nuevo en BotFather.
USAR_TELEGRAM = False
BOT_TOKEN = "PEGA_AQUI_TU_TOKEN_NUEVO"
CHAT_ID = "PEGA_AQUI_TU_CHAT_ID"
TELEGRAM_INTERVAL = 15000
TELEGRAM_START_RETRY = 60000

WIFI_RETRY_INTERVAL = 8000
DHT_INTERVAL = 2500
DS_INTERVAL = 2000
DS_CONVERSION_MS = 750
OLED_INTERVAL = 500
DEBUG_INTERVAL = 2000

PIN_DS18B20 = 4
PIN_DHT22 = 15
PIN_REED = 14
PIN_SERVO = 25
PIN_LED_VERDE = 26
PIN_LED_ROJO = 27
PIN_BUZZER = 32
PIN_LUZ = 33
PIN_RELE = 12

PIN_SDA = 21
PIN_SCL = 22

PIN_CS = 5
PIN_SCK = 18
PIN_MOSI = 23
PIN_MISO = 19

RIESGO_BAJO_VAL = 0.15
RIESGO_MEDIO_VAL = 0.50
RIESGO_ALTO_VAL = 0.90


def configurar_timeout_red():
    try:
        socket.setdefaulttimeout(NET_TIMEOUT)
    except Exception:
        pass


configurar_timeout_red()

# ============================================================
# HARDWARE
# ============================================================

led_verde = Pin(PIN_LED_VERDE, Pin.OUT)
led_rojo = Pin(PIN_LED_ROJO, Pin.OUT)

luz = Pin(PIN_LUZ, Pin.OUT)

rele = Pin(PIN_RELE, Pin.OUT)
rele.value(1)

buzzer = PWM(Pin(PIN_BUZZER))
buzzer.duty(0)

servo = PWM(Pin(PIN_SERVO), freq=50)
PUERTA_ABIERTA = 0
PUERTA_CERRADA = 180
puerta_estado = False

sensor_dht = dht.DHT22(Pin(PIN_DHT22))

ow = onewire.OneWire(Pin(PIN_DS18B20))
ds = ds18x20.DS18X20(ow)
roms = ds.scan()
print("DS18B20 encontrados:", roms)

i2c = I2C(0, sda=Pin(PIN_SDA), scl=Pin(PIN_SCL), freq=400000)
oled = SSD1306_I2C(128, 64, i2c)

spi = SPI(
    1,
    baudrate=1000000,
    polarity=0,
    phase=0,
    sck=Pin(PIN_SCK),
    mosi=Pin(PIN_MOSI),
    miso=Pin(PIN_MISO),
)
cs = Pin(PIN_CS, Pin.OUT)
rdr = MFRC522(spi, cs)

USUARIOS = {
    "44601507": "Sara",
    "99C92907": "Luna",
}

reed = Pin(PIN_REED, Pin.IN, Pin.PULL_UP)

# ============================================================
# VARIABLES
# ============================================================

temp_ds = 0.0
humedad = 0.0
ultimo_rfid = 0
tiempo_puerta_abierta = 0
puerta_abierta_desde = None
nivel_riesgo = 0.0
estado_sistema = "NORMAL"
ultimo_beep_alarm = 0
sistema_activo = True
ultimo_update_id = 0
ultimo_telegram_critico = 0
critico_notificado = False
alertas_registradas = []

ultimo_dht = None
ultimo_ds_inicio = None
ds_conversion_en_curso = False
ultimo_oled = 0
ultimo_debug = 0
ultimo_envio_http = 0
ultimo_polling_telegram = 0
http_fallos = 0
ultimo_error_http = 0

wlan = network.WLAN(network.STA_IF)
wifi_anunciado = False
ultimo_intento_wifi = None
telegram_preparado = False
ultimo_intento_inicio_telegram = None

# ============================================================
# BUZZER Y SERVO
# ============================================================


def beep_ok():
    buzzer.freq(2500)
    buzzer.duty(512)
    time.sleep_ms(100)
    buzzer.duty(0)


def beep_error():
    buzzer.freq(1200)
    buzzer.duty(512)
    time.sleep_ms(500)
    buzzer.duty(0)


def beep_alarm():
    buzzer.freq(3000)
    buzzer.duty(512)
    time.sleep_ms(120)
    buzzer.duty(0)


def mover_servo(grados):
    duty = int((grados / 180) * 75 + 25)
    servo.duty(duty)


def abrir_puerta():
    global puerta_estado
    mover_servo(PUERTA_ABIERTA)
    puerta_estado = True


def cerrar_puerta():
    global puerta_estado
    mover_servo(PUERTA_CERRADA)
    puerta_estado = False

# ============================================================
# WIFI
# ============================================================


def iniciar_wifi():
    wlan.active(True)
    mantener_wifi(force=True)


def red_disponible():
    return wlan is not None and wlan.active() and wlan.isconnected()


def mantener_wifi(force=False):
    global wifi_anunciado, ultimo_intento_wifi

    ahora = time.ticks_ms()

    if red_disponible():
        if not wifi_anunciado:
            print("WiFi OK:", wlan.ifconfig()[0])
            wifi_anunciado = True
        return True

    wifi_anunciado = False

    if (
        force
        or ultimo_intento_wifi is None
        or time.ticks_diff(ahora, ultimo_intento_wifi) > WIFI_RETRY_INTERVAL
    ):
        ultimo_intento_wifi = ahora
        try:
            if not wlan.active():
                wlan.active(True)
            print("Intentando WiFi...")
            wlan.connect(WIFI_SSID, WIFI_PASS)
        except Exception as e:
            print("WIFI ERROR:", e)

    return False

# ============================================================
# TELEGRAM
# ============================================================


def telegram(msg, chat_id=None):
    if not USAR_TELEGRAM or not red_disponible():
        return False

    gc.collect()
    r = None

    try:
        destino = CHAT_ID if chat_id is None else str(chat_id)
        payload = ujson.dumps({"chat_id": destino, "text": msg})
        url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"

        r = urequests.post(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        print("Telegram:", r.status_code)
        return True

    except Exception as e:
        print("TELEGRAM ERROR:", e)
        return False

    finally:
        try:
            if r:
                r.close()
        except Exception:
            pass
        gc.collect()


def preparar_telegram():
    global ultimo_update_id

    if not USAR_TELEGRAM or not red_disponible():
        return False

    gc.collect()
    r = None

    try:
        url = (
            "https://api.telegram.org/bot"
            + BOT_TOKEN
            + "/deleteWebhook?drop_pending_updates=true"
        )
        r = urequests.get(url)
        print("Delete webhook:", r.status_code)
        ultimo_update_id = 0
        return True

    except Exception as e:
        print("PREPARAR TELEGRAM ERROR:", e)
        return False

    finally:
        try:
            if r:
                r.close()
        except Exception:
            pass
        gc.collect()


def tarea_inicio_telegram():
    global telegram_preparado, ultimo_intento_inicio_telegram

    if not USAR_TELEGRAM or telegram_preparado or not red_disponible():
        return

    ahora = time.ticks_ms()

    if (
        ultimo_intento_inicio_telegram is not None
        and time.ticks_diff(ahora, ultimo_intento_inicio_telegram)
        < TELEGRAM_START_RETRY
    ):
        return

    ultimo_intento_inicio_telegram = ahora

    if preparar_telegram():
        telegram(
            "Sistema de nevera iniciado\n"
            "IP: {}\n"
            "Usa /estado, /alertas, /activar o /reset".format(wlan.ifconfig()[0])
        )
        telegram_preparado = True


def registrar_alerta(texto):
    alertas_registradas.append(texto)

    if len(alertas_registradas) > 8:
        alertas_registradas.pop(0)


def mensaje_estado():
    puerta = "CERRADA" if reed.value() == 0 else "ABIERTA"

    return (
        "ESTADO NEVERA\n"
        "Sistema: {}\n"
        "Temperatura: {:.1f} C\n"
        "Humedad: {:.1f} %\n"
        "Puerta: {}\n"
        "Tiempo abierta: {:.1f} s\n"
        "Riesgo difuso: {:.2f}\n"
        "Nivel: {}".format(
            "ACTIVO" if sistema_activo else "PAUSADO",
            temp_ds,
            humedad,
            puerta,
            tiempo_puerta_abierta,
            nivel_riesgo,
            estado_sistema,
        )
    )


def mensaje_alertas():
    actuales = []

    if estado_sistema != "NORMAL":
        actuales.append("Estado actual: " + estado_sistema)

    if temp_ds < 2:
        actuales.append("Temperatura critica baja: {:.1f} C".format(temp_ds))
    elif temp_ds > 8:
        actuales.append("Temperatura sobre rango normal: {:.1f} C".format(temp_ds))

    if humedad < 60:
        actuales.append("Humedad baja: {:.1f} %".format(humedad))
    elif humedad > 80:
        actuales.append("Humedad alta: {:.1f} %".format(humedad))

    if tiempo_puerta_abierta > 20:
        actuales.append("Puerta abierta por {:.1f} s".format(tiempo_puerta_abierta))

    if not actuales:
        actuales.append("No hay alertas activas")

    if alertas_registradas:
        historial = "\n".join(alertas_registradas[-5:])
        return (
            "ALERTAS ACTUALES\n"
            + "\n".join(actuales)
            + "\n\nULTIMAS ALERTAS\n"
            + historial
        )

    return "ALERTAS ACTUALES\n" + "\n".join(actuales)


def reset_alertas():
    global ultimo_telegram_critico, critico_notificado
    alertas_registradas[:] = []
    ultimo_telegram_critico = 0
    critico_notificado = False


def normalizar_comando(texto):
    cmd = texto.strip().lower()

    if not cmd:
        return ""

    cmd = cmd.split()[0]

    if "@" in cmd:
        cmd = cmd.split("@")[0]

    if not cmd.startswith("/"):
        cmd = "/" + cmd

    return cmd


def manejar_comando_telegram(texto, chat_id=None):
    global sistema_activo

    cmd = normalizar_comando(texto)
    print("Comando Telegram:", cmd)

    if cmd == "/estado":
        telegram(mensaje_estado(), chat_id)

    elif cmd == "/alertas":
        telegram(mensaje_alertas(), chat_id)

    elif cmd == "/activar":
        sistema_activo = True
        telegram("Sistema de monitoreo y alarmas ACTIVADO", chat_id)

    elif cmd == "/reset":
        reset_alertas()
        telegram("Alertas reiniciadas. " + mensaje_estado(), chat_id)

    elif cmd == "/ayuda" or cmd == "/start":
        telegram(
            "Comandos disponibles:\n"
            "/estado - consultar estado actual\n"
            "/alertas - consultar alertas\n"
            "/activar - activar monitoreo\n"
            "/reset - reiniciar alertas",
            chat_id,
        )

    elif cmd.startswith("/"):
        telegram(
            "Comando no reconocido.\n"
            "Usa /estado, /alertas, /activar o /reset",
            chat_id,
        )


def verificar_telegram():
    global ultimo_update_id

    if not USAR_TELEGRAM or not red_disponible():
        return

    gc.collect()
    r = None

    try:
        url = (
            "https://api.telegram.org/bot"
            + BOT_TOKEN
            + "/getUpdates?offset="
            + str(ultimo_update_id + 1)
            + "&timeout=0&limit=1&allowed_updates=%5B%22message%22%5D"
        )

        r = urequests.get(url)
        raw = r.text
        data = ujson.loads(raw)

        for upd in data.get("result", []):
            ultimo_update_id = upd["update_id"]
            msg = upd.get("message", {})
            chat = msg.get("chat", {})
            chat_id = chat.get("id", CHAT_ID)
            texto = msg.get("text", "")
            manejar_comando_telegram(texto, chat_id)

    except Exception as e:
        print("POLLING ERROR:", e)

    finally:
        try:
            if r:
                r.close()
        except Exception:
            pass
        gc.collect()


def notificar_si_critico():
    global ultimo_telegram_critico, critico_notificado

    if not sistema_activo or not USAR_TELEGRAM:
        return

    if estado_sistema != "CRITICO":
        critico_notificado = False
        return

    ahora = time.ticks_ms()

    if (
        not critico_notificado
        or ultimo_telegram_critico == 0
        or time.ticks_diff(ahora, ultimo_telegram_critico) > 60000
    ):
        msg = "ALERTA CRITICA\n" + mensaje_estado()
        registrar_alerta(msg)

        if telegram(msg):
            ultimo_telegram_critico = ahora
            critico_notificado = True

# ============================================================
# HTTP HACIA EL PC
# ============================================================


def parse_http_url(url):
    if not url.startswith("http://"):
        raise ValueError("Solo se soporta http:// para el servidor del PC")

    resto = url[7:]
    slash = resto.find("/")

    if slash == -1:
        host_port = resto
        path = "/"
    else:
        host_port = resto[:slash]
        path = resto[slash:]

    if ":" in host_port:
        host, port_str = host_port.split(":", 1)
        port = int(port_str)
    else:
        host = host_port
        port = 80

    return host, port, path


def socket_write_all(s, data):
    if isinstance(data, str):
        data = data.encode()

    total = 0

    while total < len(data):
        try:
            sent = s.write(data[total:])
        except AttributeError:
            sent = s.send(data[total:])

        if sent is None:
            return

        if sent == 0:
            raise OSError("Socket cerrado")

        total += sent


def http_post_json(url, datos):
    host, port, path = parse_http_url(url)
    body = ujson.dumps(datos).encode()
    host_header = host if port == 80 else "{}:{}".format(host, port)
    header = (
        "POST {} HTTP/1.1\r\n"
        "Host: {}\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: {}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).format(path, host_header, len(body)).encode()

    s = None

    try:
        addr = socket.getaddrinfo(host, port)[0][-1]
        s = socket.socket()

        try:
            s.settimeout(NET_TIMEOUT)
        except Exception:
            pass

        s.connect(addr)
        socket_write_all(s, header + body)

        try:
            resp = s.recv(64)
            return b" 200 " in resp or b" 201 " in resp
        except Exception:
            return True

    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def enviar_estado_http():
    global http_fallos, ultimo_error_http

    if not red_disponible():
        return False

    ahora = time.ticks_ms()

    if (
        http_fallos >= 3
        and time.ticks_diff(ahora, ultimo_error_http) < HTTP_BACKOFF
    ):
        return False

    gc.collect()

    try:
        puerta = "CERRADA" if reed.value() == 0 else "ABIERTA"

        datos = {
            "id": NODE_ID,
            "temperatura": temp_ds,
            "humedad": humedad,
            "puerta": puerta,
            "tiempo_puerta_abierta": tiempo_puerta_abierta,
            "riesgo": nivel_riesgo,
            "estado": estado_sistema,
        }

        ok = http_post_json(SERVER_URL, datos)

        if ok:
            http_fallos = 0
            print("HTTP PC: OK")
            return True

        http_fallos += 1
        ultimo_error_http = ahora
        print("HTTP PC: sin 200")
        return False

    except Exception as e:
        http_fallos += 1
        ultimo_error_http = ahora
        print("HTTP PC ERROR:", e)
        return False

    finally:
        gc.collect()

# ============================================================
# LOGICA DIFUSA
# ============================================================


def limitar(valor, minimo, maximo):
    if valor < minimo:
        return minimo

    if valor > maximo:
        return maximo

    return valor


def mf_temp_baja(t):
    if t < 2:
        return 1.0
    return 0.0


def mf_temp_ideal(t):
    if t < 2 or t >= 10:
        return 0.0

    if 2 <= t <= 8:
        return 1.0

    return (10 - t) / 2


def mf_temp_alta(t):
    if t <= 8:
        return 0.0

    if t < 12:
        return (t - 8) / 4

    return 1.0


def mf_hum_baja(h):
    if h <= 45:
        return 1.0

    if h < 60:
        return (60 - h) / 15

    return 0.0


def mf_hum_normal(h):
    if h <= 50 or h >= 90:
        return 0.0

    if 50 < h < 60:
        return (h - 50) / 10

    if 60 <= h <= 80:
        return 1.0

    return (90 - h) / 10


def mf_hum_alta(h):
    if h <= 80:
        return 0.0

    if h < 95:
        return (h - 80) / 15

    return 1.0


def mf_puerta_corta(segundos):
    if segundos <= 5:
        return 1.0

    if segundos < 15:
        return (15 - segundos) / 10

    return 0.0


def mf_puerta_media(segundos):
    if segundos <= 5 or segundos >= 35:
        return 0.0

    if 5 < segundos < 20:
        return (segundos - 5) / 15

    return (35 - segundos) / 15


def mf_puerta_larga(segundos):
    if segundos <= 20:
        return 0.0

    if segundos < 45:
        return (segundos - 20) / 25

    return 1.0


def promedio_ponderado(reglas):
    numerador = 0.0
    denominador = 0.0

    for activacion, peso in reglas:
        numerador += activacion * peso
        denominador += activacion

    if denominador == 0:
        return 0.0

    return numerador / denominador


def calcular_riesgo_difuso(temp, hum, puerta_seg):
    if temp < 2:
        return 1.0

    t_baja = mf_temp_baja(temp)
    t_ideal = mf_temp_ideal(temp)
    t_alta = mf_temp_alta(temp)
    h_baja = mf_hum_baja(hum)
    h_normal = mf_hum_normal(hum)
    h_alta = mf_hum_alta(hum)
    p_corta = mf_puerta_corta(puerta_seg)
    p_media = mf_puerta_media(puerta_seg)
    p_larga = mf_puerta_larga(puerta_seg)

    reglas = [
        (min(t_ideal, h_normal, p_corta), RIESGO_BAJO_VAL),
        (max(t_baja, h_baja), RIESGO_MEDIO_VAL),
        (max(t_alta, h_alta), RIESGO_ALTO_VAL),
        (p_media, RIESGO_MEDIO_VAL),
        (p_larga, RIESGO_ALTO_VAL),
        (min(t_alta, p_larga), 1.0),
        (min(h_alta, p_larga), RIESGO_ALTO_VAL),
    ]

    return limitar(promedio_ponderado(reglas), 0.0, 1.0)


def clasificar_estado(riesgo):
    if riesgo < 0.35:
        return "NORMAL"

    if riesgo < 0.70:
        return "ALERTA"

    return "CRITICO"


def actualizar_estado_sistema():
    global nivel_riesgo, estado_sistema, ultimo_beep_alarm

    nivel_riesgo = calcular_riesgo_difuso(temp_ds, humedad, tiempo_puerta_abierta)
    estado_sistema = clasificar_estado(nivel_riesgo)

    if estado_sistema == "NORMAL":
        led_verde.on()
        led_rojo.off()
        return

    led_verde.off()
    led_rojo.on()

    if estado_sistema == "CRITICO":
        ahora = time.ticks_ms()

        if time.ticks_diff(ahora, ultimo_beep_alarm) > 5000:
            beep_alarm()
            ultimo_beep_alarm = ahora

# ============================================================
# SENSORES Y ENTRADAS
# ============================================================


def actualizar_ds18b20():
    global temp_ds, ultimo_ds_inicio, ds_conversion_en_curso

    if not roms:
        return

    ahora = time.ticks_ms()

    if not ds_conversion_en_curso:
        if (
            ultimo_ds_inicio is None
            or time.ticks_diff(ahora, ultimo_ds_inicio) > DS_INTERVAL
        ):
            try:
                ds.convert_temp()
                ds_conversion_en_curso = True
                ultimo_ds_inicio = ahora
            except Exception as e:
                print("DS18B20 iniciar error:", e)
        return

    if time.ticks_diff(ahora, ultimo_ds_inicio) >= DS_CONVERSION_MS:
        try:
            temp_ds = ds.read_temp(roms[0])
        except Exception as e:
            print("DS18B20 leer error:", e)

        ds_conversion_en_curso = False


def actualizar_dht():
    global humedad, ultimo_dht

    ahora = time.ticks_ms()

    if ultimo_dht is not None and time.ticks_diff(ahora, ultimo_dht) < DHT_INTERVAL:
        return

    ultimo_dht = ahora

    try:
        sensor_dht.measure()
        humedad = sensor_dht.humidity()
    except Exception as e:
        print("DHT error:", e)


def verificar_puerta():
    global puerta_abierta_desde, tiempo_puerta_abierta

    ahora = time.ticks_ms()

    if reed.value() == 0:
        luz.on()
        rele.value(0)

        if puerta_abierta_desde is None:
            puerta_abierta_desde = ahora

        tiempo_puerta_abierta = time.ticks_diff(ahora, puerta_abierta_desde) / 1000
        return

    luz.off()
    rele.value(1)
    puerta_abierta_desde = None
    tiempo_puerta_abierta = 0


def verificar_rfid():
    global ultimo_rfid

    stat, tag_type = rdr.request(rdr.REQIDL)

    if stat != rdr.OK:
        return

    stat, raw_uid = rdr.anticoll()

    if stat != rdr.OK:
        return

    uid = "%02X%02X%02X%02X" % (
        raw_uid[0],
        raw_uid[1],
        raw_uid[2],
        raw_uid[3],
    )

    ahora = time.ticks_ms()

    if time.ticks_diff(ahora, ultimo_rfid) < 1500:
        return

    ultimo_rfid = ahora
    print("UID:", uid)

    oled.fill(0)

    if uid in USUARIOS:
        nombre = USUARIOS[uid]
        print("ACCESO OK")
        beep_ok()

        if puerta_estado:
            cerrar_puerta()
            oled.text("CERRANDO", 0, 0)
        else:
            abrir_puerta()
            oled.text("ABRIENDO", 0, 0)

        oled.text(nombre, 0, 20)

    else:
        print("DENEGADO")
        beep_error()
        oled.text("DENEGADO", 0, 0)

    oled.show()

# ============================================================
# OLED Y DEBUG
# ============================================================


def pantalla_principal():
    oled.fill(0)
    oled.text("Temp:{:.1f}C".format(temp_ds), 0, 0)
    oled.text("Hum:{:.1f}%".format(humedad), 0, 15)

    if reed.value() == 0:
        oled.text("Puerta:CE", 0, 30)
    else:
        oled.text("Puerta:AB", 0, 30)

    oled.text("R:{:.2f}".format(nivel_riesgo), 0, 45)
    oled.text(estado_sistema, 64, 45)
    oled.show()


def imprimir_estado():
    print(
        "Temp:",
        temp_ds,
        "Hum:",
        humedad,
        "Puerta_s:",
        tiempo_puerta_abierta,
        "Riesgo:",
        nivel_riesgo,
        "Estado:",
        estado_sistema,
        "WiFi:",
        red_disponible(),
        "HTTP fallos:",
        http_fallos,
    )

# ============================================================
# INICIO
# ============================================================


led_verde.on()
led_rojo.off()
cerrar_puerta()
iniciar_wifi()

# ============================================================
# LOOP PRINCIPAL
# ============================================================


while True:
    mantener_wifi()
    tarea_inicio_telegram()

    actualizar_ds18b20()
    actualizar_dht()
    verificar_puerta()
    actualizar_estado_sistema()
    notificar_si_critico()
    verificar_rfid()

    ahora = time.ticks_ms()

    if time.ticks_diff(ahora, ultimo_polling_telegram) > TELEGRAM_INTERVAL:
        verificar_telegram()
        ultimo_polling_telegram = ahora

    ahora = time.ticks_ms()

    if time.ticks_diff(ahora, ultimo_envio_http) > HTTP_INTERVAL:
        enviar_estado_http()
        ultimo_envio_http = ahora

    ahora = time.ticks_ms()

    if time.ticks_diff(ahora, ultimo_oled) > OLED_INTERVAL:
        pantalla_principal()
        ultimo_oled = ahora

    if time.ticks_diff(ahora, ultimo_debug) > DEBUG_INTERVAL:
        imprimir_estado()
        ultimo_debug = ahora

    sleep_ms(20)