import requests
import time
import os
import threading
import json
from datetime import datetime
from collections import deque
from flask import Flask, request

# ==================== CONFIGURACIÓN ====================
os.environ['TZ'] = 'America/Caracas'
try:
    time.tzset()
except AttributeError:
    pass  

TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not TOKEN or not ADMIN_ID:
    print("ERROR: TELEGRAM_TOKEN o ADMIN_ID no configurados")
    exit(1)

ADMIN_ID = int(ADMIN_ID)
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"

# ID de tu grupo o canal privado para restringir acceso (debe empezar con -100)
ID_CANAL_O_GRUPO = int(os.environ.get('ID_CANAL_O_GRUPO', '-1001234567890'))

# URL de Render para Self-Ping
RENDER_URL = "https://telegram-usdt-bot-vf5t.onrender.com"

# Caché en memoria para control de acceso (TTL: 5 minutos)
CACHE_USUARIOS = {}
TIEMPO_CACHE_USUARIOS = 300

# Lock para guardado seguro de config.json
config_lock = threading.Lock()

app = Flask(__name__)

# ==================== CONSTANTES FINANCIERAS ====================
COMISION_TARJETA_PCT = 0.015  # 1.5% Comisión Tarjeta Internacional Banesco
COMISION_BPAY_PCT = 0.041     # 4.1% Comisión Procesamiento Bpay

# ==================== PERSISTENCIA DE CONFIGURACIÓN ====================
CONFIG_FILE = "config.json"
TASA_SOLES_TARIFARIO = 3.80

def cargar_configuracion():
    global TASA_SOLES_TARIFARIO
    with config_lock:
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    TASA_SOLES_TARIFARIO = data.get('tasa_soles', 3.80)
        except Exception as e:
            print(f"Error al cargar config.json: {e}")

def guardar_configuracion():
    with config_lock:
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump({'tasa_soles': TASA_SOLES_TARIFARIO}, f)
        except Exception as e:
            print(f"Error al guardar config.json: {e}")

# ==================== ALERTAS DE PRECIO FINANCIERO ====================
UMBRALES = {
    'VES': 0.50,
    'COP': 30.0,  
    'PEN': 0.03    
}

FLUCTUACION_UMBRAL = 0.8
ultimos_precios = {'VES': None, 'COP': None, 'PEN': None}

# ==================== CONTROL DE ACCESO (MEJORADO CON CACHÉ) ====================
def usuario_esta_en_grupo(user_id):
    """
    Verifica si el usuario pertenece al grupo/canal autorizado mediante la API de Telegram.
    Aplica caché de 5 minutos y siempre autoriza al ADMIN_ID.
    """
    if user_id == ADMIN_ID:
        return True

    ahora = time.time()
    if user_id in CACHE_USUARIOS:
        es_valido, timestamp = CACHE_USUARIOS[user_id]
        if ahora - timestamp < TIEMPO_CACHE_USUARIOS:
            return es_valido

    try:
        url = f"{URL_TELEGRAM}getChatMember"
        response = requests.post(url, json={"chat_id": ID_CANAL_O_GRUPO, "user_id": user_id}, timeout=8)
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                status = data.get("result", {}).get("status", "")
                es_miembro = status in ['creator', 'administrator', 'member']
                CACHE_USUARIOS[user_id] = (es_miembro, ahora)
                return es_miembro
    except Exception as e:
        print(f"Error verificando acceso para usuario {user_id}: {e}")

    return False  # Fail-closed por seguridad

usuarios_activos = set([ADMIN_ID])
def obtener_usuarios():
    return list(usuarios_activos)
def guardar_usuario(chat_id):
    if chat_id not in usuarios_activos:
        usuarios_activos.add(chat_id)

# ==================== CACHÉ DE PRECIOS ====================
cache_precios = {}
cache_tiempo = {}
CACHE_DURACION = 30

# ==================== HISTORIAL ====================
historial_ves = deque(maxlen=1440)

# ==================== ESTADOS DE ENTRADA ====================
usuario_esperando_calculo = {} 
usuario_esperando_cruzado = {}  
usuario_configurando_soles = {}  

def limpiar_estados_usuario(chat_id):
    usuario_esperando_calculo.pop(chat_id, None)
    usuario_esperando_cruzado.pop(chat_id, None)
    usuario_configurando_soles.pop(chat_id, None)

# ==================== INTERFACES DE TECLADOS MEJORADAS ====================

def crear_teclado_principal(chat_id):
    teclado = [
        ["📈 Comparativa P2P vs BCV"],
        ["🧮 Conversor USD / Bs"], 
        ["📊 Calculadora de Margen"],
        ["📈 Historial de brecha VES"]
    ]

    if chat_id == ADMIN_ID:
        teclado.append(["💼 Panel de Operaciones"])

    teclado.append(["⚙️ Mercado P2P"])
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_remesas(chat_id):
    teclado = [
        ["💱 Conversor de Remesas"],
        ["📋 Tarifario USD"],
        ["📋 Tarifario Soles"],
        ["⚙️ Ajustar Tasa"],
        ["🌐 Tasas Cruzadas"],
        ["⬅️ Volver al Menú"]
    ]
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_opciones(chat_id):
    teclado = [
        ["📊 P2P Multidivisa"],
        ["🇻🇪 Tasa VES"],
        ["🇨🇴 Tasa COP"],
        ["🇵🇪 Tasa PEN"]
    ]

    if chat_id == ADMIN_ID:
        teclado.append(["👥 Usuarios Registrados"])

    teclado.append(["⬅️ Volver al Menú"])
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_cruzado_rapido(chat_id):
    teclado = [
        ["20 S/", "50 S/", "100 S/"],
        ["5000 Bs", "10000 Bs", "20000 Bs"],
        ["⬅️ Volver al Menú"]
    ]
    return {"keyboard": teclado, "resize_keyboard": True}

def enviar_mensaje(chat_id, texto, teclado=None):
    try:
        url = URL_TELEGRAM + "sendMessage"
        data = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}
        if teclado:
            data["reply_markup"] = teclado
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Error al enviar mensaje Telegram: {e}")
        return False

# ==================== OBTENCIÓN P2P Y BCV (DOLARAPI INTEGRADAS) ====================

def obtener_precios_con_cache(fiat):
    global cache_precios, cache_tiempo
    ahora = time.time()

    if fiat in cache_precios and fiat in cache_tiempo:
        if ahora - cache_tiempo[fiat] < CACHE_DURACION:
            return cache_precios[fiat]['compra'], cache_precios[fiat]['venta']

    compra, venta = obtener_precios_p2p_reales(fiat)
    if compra and venta:
        cache_precios[fiat] = {'compra': compra, 'venta': venta}
        cache_tiempo[fiat] = ahora

    return compra, venta

def obtener_precios_p2p_reales(fiat):
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

        data = {"asset": "USDT", "fiat": fiat, "tradeType": "SELL", "page": 1, "rows": 10, "payTypes": []}
        compra = None
        try:
            r = requests.post(url, json=data, headers=headers, timeout=10)
            if r.status_code == 200:
                result = r.json()
                if result.get('data'):
                    precios = []
                    for a in result['data']:
                        p = float(a['adv']['price'])
                        if 1 < p < 100000:
                            precios.append(p)
                    if precios:
                        compra = min(precios)
        except Exception as e:
            print(f"Error P2P SELL ({fiat}): {e}")

        data = {"asset": "USDT", "fiat": fiat, "tradeType": "BUY", "page": 1, "rows": 10, "payTypes": []}
        venta = None
        try:
            r = requests.post(url, json=data, headers=headers, timeout=10)
            if r.status_code == 200:
                result = r.json()
                if result.get('data'):
                    precios = []
                    for a in result['data']:
                        p = float(a['adv']['price'])
                        if 1 < p < 100000:
                            precios.append(p)
                    if precios:
                        venta = max(precios)
        except Exception as e:
            print(f"Error P2P BUY ({fiat}): {e}")

        if compra is None or venta is None:
            return None, None
        if compra < venta:
            compra, venta = venta, compra
        return compra, venta
    except Exception as e:
        print(f"Error general P2P ({fiat}): {e}")
        return None, None

def obtener_tasa_bcv_actual():
    tasas = obtener_tasas_bcv()
    if tasas and tasas.get('usd'):
        return tasas['usd']
    return 45.00 

def obtener_tasas_bcv():
    """
    Consulta las tasas oficiales vigentes (USD y EUR) usando DolarApi.
    Mantiene fallback secundario a ExchangeRate-API.
    """
    tasas = {'usd': 0.0, 'eur': 0.0, 'fecha': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    try:
        r_usd = requests.get("https://ve.dolarapi.com/v1/dolares/oficial", timeout=8)
        if r_usd.status_code == 200:
            tasas['usd'] = float(r_usd.json().get("promedio", 0))

        r_eur = requests.get("https://ve.dolarapi.com/v1/euros/oficial", timeout=8)
        if r_eur.status_code == 200:
            tasas['eur'] = float(r_eur.json().get("promedio", 0))

        if tasas['usd'] > 0:
            return tasas
    except Exception as e:
        print(f"Error consultando DolarApi: {e}")

    # Fallback si DolarApi no responde
    try:
        url = "https://api.exchangerate-api.com/v4/latest/USD"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            usd = data.get('rates', {}).get('VES', 0)
            eur = data.get('rates', {}).get('EUR', 0)
            if usd > 0:
                return {
                    'usd': usd,
                    'eur': usd * eur if eur > 0 else usd * 0.92,
                    'fecha': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
    except Exception as e:
        print(f"Error consultando ExchangeRate-API: {e}")
    return None

# ==================== MOSTRAR TARIFARIOS EN TEXTO ====================

def mostrar_tarifario_usd(chat_id):
    tasa_bcv = obtener_tasa_bcv_actual()
    dolares_lista = [10, 20, 30, 50, 100, 150, 200, 250, 300, 500]

    mensaje = f"📋 *TARIFARIO EN USD*\n🕐 Tasa BCV: {tasa_bcv:.2f} Bs | Perú - Ven Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n```\n{'Dólares'.ljust(9)}|{'Recibes (Bs)'.ljust(14)}|{'Equivalente'.ljust(12)}\n---------------------------------\n"
    for usd in dolares_lista:
        recibes_val = usd * tasa_bcv
        equiv_soles = recibes_val / TASA_SOLES_TARIFARIO if TASA_SOLES_TARIFARIO > 0 else 0
        mensaje += f"{f'{usd}$'.ljust(9)}|{f'{recibes_val:,.2f}'.ljust(14)}|{f'{equiv_soles:,.2f} S/'.ljust(12)}\n"
    mensaje += "```"
    enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))

def mostrar_tarifario_soles(chat_id):
    tasa_bcv = obtener_tasa_bcv_actual()
    soles_lista = [10, 20, 30, 50, 100, 150, 200, 300, 500, 1000]

    mensaje = f"📋 *TARIFARIO EN SOLES A BOLÍVARES*\n🕐 Tasa BCV: {tasa_bcv:.2f} Bs | Perú - Ven Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n```\n{'Enviado'.ljust(10)}|{'Recibes (Bs)'.ljust(14)}|{'Equivalente'.ljust(12)}\n---------------------------------\n"
    for soles in soles_lista:
        recibes_val = soles * TASA_SOLES_TARIFARIO
        equiv_usd = recibes_val / tasa_bcv if tasa_bcv > 0 else 0
        mensaje += f"{f'{soles} S/'.ljust(10)}|{f'{recibes_val:,.2f}'.ljust(14)}|{f'{equiv_usd:,.2f}$'.ljust(12)}\n"
    mensaje += "```"
    enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))

# ==================== TASAS CRUZADAS ====================

def calcular_tasas_cruzadas():
    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    compra_cop, venta_cop = obtener_precios_con_cache('COP')
    compra_pen, venta_pen = obtener_precios_con_cache('PEN')

    if not all([compra_ves, venta_ves, compra_cop, venta_cop, compra_pen, venta_pen]):
        return None

    tasas = {}

    # PERÚ (PEN)
    tasas['Perú → Venezuela'] = (venta_ves / compra_pen) * 0.95
    tasas['Venezuela → Perú'] = tasas['Perú → Venezuela'] + 15
    if compra_pen and venta_cop:
        tasas['Perú → Colombia'] = (1 / (compra_pen / venta_cop)) * 0.95
    else:
        tasas['Perú → Colombia'] = 0
    tasas['Colombia → Perú'] = (compra_cop / venta_pen) * 1.06

    # COLOMBIA (COP)
    tasas['Colombia → Venezuela'] = (compra_cop / venta_ves) * 1.06
    if compra_ves and venta_cop:
        tasas['Venezuela → Colombia'] = (1 / (compra_ves / venta_cop)) * 0.95
    else:
        tasas['Venezuela → Colombia'] = 0
    tasas['Colombia → Brasil'] = (compra_cop / 5.10) * 1.06

    # VENEZUELA (VES)
    tasas['Venezuela → Brasil'] = (compra_ves / 5.10) * 1.05

    return tasas

def mostrar_tasas_cambio(chat_id):
    tasas = calcular_tasas_cruzadas()

    if not tasas:
        mensaje = "❌ No se pudieron obtener los datos para calcular las tasas"
        enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))
        return

    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    compra_cop, venta_cop = obtener_precios_con_cache('COP')
    compra_pen, venta_pen = obtener_precios_con_cache('PEN')

    mensaje = f"🌐 *TASAS DE CAMBIO CRUZADAS*\n"
    mensaje += f"🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"

    mensaje += f"📊 *Precios de referencia:*\n"
    mensaje += f"  🇻🇪 VES: Compra {compra_ves:.2f} | Venta {venta_ves:.2f}\n"
    mensaje += f"  🇨🇴 COP: Compra {compra_cop:.2f} | Venta {venta_cop:.2f}\n"
    mensaje += f"  🇵🇪 PEN: Compra {compra_pen:.2f} | Venta {venta_pen:.2f}\n\n"

    mensaje += f"━━━━━━━━━━━━━━━━━━━━\n"
    mensaje += f"🇵🇪 *PERÚ (PEN)*\n"
    mensaje += f"━━━━━━━━━━━━━━━━━━━━\n"
    mensaje += f"  → 🇻🇪 Venezuela: {tasas['Perú → Venezuela']:.2f} Bs\n"
    mensaje += f"  → 🇨🇴 Colombia: {tasas['Perú → Colombia']:.2f} COP\n\n"

    mensaje += f"━━━━━━━━━━━━━━━━━━━━\n"
    mensaje += f"🇨🇴 *COLOMBIA (COP)*\n"
    mensaje += f"━━━━━━━━━━━━━━━━━━━━\n"
    mensaje += f"  → 🇻🇪 Venezuela: {tasas['Colombia → Venezuela']:.2f} Bs\n"
    mensaje += f"  → 🇵🇪 Perú: {tasas['Colombia → Perú']:.2f} PEN\n"
    mensaje += f"  → 🇧🇷 Brasil: {tasas['Colombia → Brasil']:.2f} BRL\n\n"

    mensaje += f"━━━━━━━━━━━━━━━━━━━━\n"
    mensaje += f"🇻🇪 *VENEZUELA (VES)*\n"
    mensaje += f"━━━━━━━━━━━━━━━━━━━━\n"
    mensaje += f"  → 🇵🇪 Perú: {tasas['Venezuela → Perú']:.2f} PEN\n"
    mensaje += f"  → 🇨🇴 Colombia: {tasas['Venezuela → Colombia']:.2f} COP\n"
    mensaje += f"  → 🇧🇷 Brasil: {tasas['Venezuela → Brasil']:.2f} BRL"

    enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))

# ==================== CONVERSOR DE REMESAS ====================

def calcular_conversion_tasas_cruzadas(chat_id, texto_monto):
    tasas = obtener_tasas_bcv()
    if not tasas:
        enviar_mensaje(chat_id, "⏳ No se pudo obtener la tasa BCV oficial en este momento.", crear_teclado_remesas(chat_id))
        return

    tasa_bcv = tasas['usd']
    texto_limpio = texto_monto.strip().lower()

    tasa_peru_ven = TASA_SOLES_TARIFARIO
    tasa_ven_peru = TASA_SOLES_TARIFARIO + 15

    try:
        if 's/' in texto_limpio or 'soles' in texto_limpio or 'sol' in texto_limpio:
            monto_str = texto_limpio.replace('s/', '').replace('soles', '').replace('sol', '').replace(',', '.').strip()
            monto_soles = float(monto_str)

            resultado_bs_pv = monto_soles * tasa_peru_ven
            resultado_usd_pv = resultado_bs_pv / tasa_bcv

            resultado_bs_vp = monto_soles * tasa_ven_peru
            resultado_usd_vp = resultado_bs_vp / tasa_bcv

            mensaje = f"""📊 *PROCESAMIENTO DINÁMICO DE SOLES*

*Tasa BCV:* {tasa_bcv:.2f} Bs
*Tasa Perú - Venezuela:* {tasa_peru_ven:.2f}
*Tasa Venezuela - Perú:* {tasa_ven_peru:.2f}
━━━━━━━━━━━━━━━━━━━━
🇵🇪 ➔ 🇻🇪 *Operación Perú - Venezuela:*
• {monto_soles:,.2f} Soles, Equivalente a *{resultado_bs_pv:,.2f} Bs*, *{resultado_usd_pv:,.2f}$* a tasa BCV

🇻🇪 ➔ 🇵🇪 *Operación Venezuela - Perú:*
• Para que lleguen {monto_soles:,.2f} Soles se necesita *{resultado_bs_vp:,.2f} Bs*, equivalente a *{resultado_usd_vp:,.2f}$* a tasa BCV
━━━━━━━━━━━━━━━━━━━━
🕐 {datetime.now().strftime('%H:%M:%S')} (Caracas)"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_cruzado_rapido(chat_id))

        elif 'bs' in texto_limpio:
            monto_str = texto_limpio.replace('bs', '').replace(',', '.').strip()
            monto_bs = float(monto_str)

            resultado_usd_bcv = monto_bs / tasa_bcv
            resultado_soles_pv = monto_bs / tasa_peru_ven if tasa_peru_ven > 0 else 0
            resultado_soles_vp = monto_bs / tasa_ven_peru if tasa_ven_peru > 0 else 0

            mensaje = f"""⚖️ *CALCULADORA DE TASAS CRUZADAS (TASA MANUAL)*

*Tasa BCV:* {tasa_bcv:.2f} Bs
*Tasa Perú - Venezuela (Manual):* {tasa_peru_ven:.2f} | *Tasa Venezuela - Perú:* {tasa_ven_peru:.2f}
━━━━━━━━━━━━━━━━━━━━
🇵🇪 ➔ 🇻🇪 *Fórmula Perú - Venezuela:*
• {monto_bs:,.2f} Bs, *${resultado_usd_bcv:,.2f}$* a tasa BCV, son *{resultado_soles_pv:,.2f} Soles*

🇻🇪 ➔ 🇵🇪 *Fórmula Venezuela - Perú:*
• Por {monto_bs:,.2f} Bs equivalente a *${resultado_usd_bcv:,.2f}$* a tasa BCV, llegan *{resultado_soles_vp:,.2f} Soles*
━━━━━━━━━━━━━━━━━━━━
🕐 {datetime.now().strftime('%H:%M:%S')} (Caracas)"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_cruzado_rapido(chat_id))
        else:
            enviar_mensaje(chat_id, "⚠️ Para cálculos cruzados indica la cantidad añadiendo *S/* o *Bs* al final (ejemplo: `100 S/` o `25000 Bs`).", crear_teclado_cruzado_rapido(chat_id))

    except ValueError:
        enviar_mensaje(chat_id, "❌ Error al realizar la conversión cruzada. Verifica la cantidad escrita.", crear_teclado_cruzado_rapido(chat_id))

# ==================== FLUCTUACIÓN E HISTORIAL ====================

ultimas_tasas_cruzadas = {}

def guardar_tasas_anteriores():
    try:
        with open("tasas_anteriores.json", 'w') as f: json.dump(ultimas_tasas_cruzadas, f)
    except Exception as e:
        print(f"Error al guardar tasas anteriores: {e}")

def cargar_tasas_anteriores():
    global ultimas_tasas_cruzadas
    try:
        if os.path.exists("tasas_anteriores.json"):
            with open("tasas_anteriores.json", 'r') as f: ultimas_tasas_cruzadas = json.load(f)
    except Exception as e:
        print(f"Error al cargar tasas anteriores: {e}")

def verificar_fluctuacion_tasas():
    global ultimas_tasas_cruzadas
    tasas_actuales = calcular_tasas_cruzadas()
    if not tasas_actuales:
        return
    if not ultimas_tasas_cruzadas:
        ultimas_tasas_cruzadas = tasas_actuales.copy()
        guardar_tasas_anteriores()
        return

    mensaje = "⚠️ *ALERTA DE FLUCTUACIÓN DE TASAS* ⚠️\n"
    mensaje += f"🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    hubo_fluctuacion = False

    for clave, valor_actual in tasas_actuales.items():
        if clave in ultimas_tasas_cruzadas:
            valor_anterior = ultimas_tasas_cruzadas[clave]
            if valor_anterior > 0:
                fluctuacion = abs((valor_actual - valor_anterior) / valor_anterior) * 100
                if fluctuacion >= FLUCTUACION_UMBRAL:
                    direccion = "📈 SUBIÓ" if valor_actual > valor_anterior else "📉 BAJÓ"
                    mensaje += f"• *{clave}*: {direccion} en {fluctuacion:.2f}%\n"
                    mensaje += f"  Anterior: {valor_anterior:.4f} → Actual: {valor_actual:.4f}\n\n"
                    hubo_fluctuacion = True

    if hubo_fluctuacion:
        for usuario in obtener_usuarios():
            try:
                enviar_mensaje(usuario, mensaje)
                time.sleep(0.05)
            except Exception as e:
                print(f"Error enviando alerta fluctuación a {usuario}: {e}")
    ultimas_tasas_cruzadas = tasas_actuales.copy()
    guardar_tasas_anteriores()

def guardar_historial_ves(precio):
    historial_ves.append(precio)

def obtener_analisis_ves():
    if not historial_ves: return None
    precios = list(historial_ves)
    if len(precios) < 2: return None
    precio_actual = precios[-1]
    precio_inicio = precios[0]
    cambio = precio_actual - precio_inicio
    cambio_porcentaje = (cambio / precio_inicio) * 100 if precio_inicio != 0 else 0
    precio_max = max(precios)
    precio_min = min(precios)
    tendencia = "↗️ Alcista" if len(precios) > 10 and precios[-1] > precios[-10] else "↘️ Bajista"
    if len(precios) > 10 and abs(precios[-1] - precios[-10]) < 0.01: tendencia = "➡️ Lateral"
    return {
        'actual': precio_actual, 'apertura': precio_inicio, 'cambio': cambio,
        'cambio_porcentaje': cambio_porcentaje, 'maximo': precio_max, 'minimo': precio_min,
        'tendencia': tendencia, 'muestras': len(precios)
    }

# ==================== VERIFICAR ALERTAS ACUMULATIVAS ====================

def verificar_alertas(precios):
    global ultimos_precios
    if not precios: 
        return

    usuarios = obtener_usuarios()
    if not usuarios: 
        return

    for moneda in ['VES', 'COP', 'PEN']:
        if moneda not in precios or not precios[moneda]: 
            continue

        precio_actual = precios[moneda]['compra']

        if ultimos_precios[moneda] is None:
            ultimos_precios[moneda] = precio_actual
            continue

        cambio = abs(precio_actual - ultimos_precios[moneda])
        umbral = UMBRALES.get(moneda, 0)

        if cambio >= umbral:
            direccion = "📈 SUBIÓ" if precio_actual > ultimos_precios[moneda] else "📉 BAJÓ"
            emoji = "🟢" if precio_actual > ultimos_precios[moneda] else "🔴"
            signo = "+" if precio_actual > ultimos_precios[moneda] else ""

            cambio_porcentaje = ((precio_actual - ultimos_precios[moneda]) / ultimos_precios[moneda] * 100) if ultimos_precios[moneda] != 0 else 0

            mensaje = (
                f"\n{emoji} *🔔 ALERTA {moneda}* {emoji}\n\n"
                f"{direccion} en {signo}{cambio:.2f}\n\n"
                f"📊 *Detalles:*\n"
                f"• Referencia Anterior: {ultimos_precios[moneda]:.2f}\n"
                f"• Precio Actual: {precio_actual:.2f}\n"
                f"• Variación: {signo}{cambio:.2f} ({signo}{cambio_porcentaje:.2f}%)\n\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
            )

            for usuario in usuarios:
                try:
                    enviar_mensaje(usuario, mensaje)
                    time.sleep(0.05)
                except Exception as e:
                    print(f"Error al enviar alerta a usuario {usuario}: {e}")

            ultimos_precios[moneda] = precio_actual

def mostrar_precios_usdt(chat_id):
    precios = {}
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_con_cache(m)
        if compra and venta: precios[m] = {'compra': compra, 'venta': venta}
    if not precios:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado_opciones(chat_id))
        return
    mensaje = f"💰 *PRECIOS USDT P2P*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    for m, datos in precios.items():
        mensaje += f"*{m}*\n  🟢 COMPRA: {datos['compra']:.2f}\n  🔴 VENTA: {datos['venta']:.2f}\n  📊 Spread: {datos['compra']-datos['venta']:.2f}\n\n"
    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

def mostrar_precio_individual(chat_id, moneda):
    compra, venta = obtener_precios_con_cache(moneda)
    if not compra or not venta:
        enviar_mensaje(chat_id, f"⏳ Obteniendo precio {moneda}...", crear_teclado_opciones(chat_id))
        return
    mensaje = f"💰 *PRECIO {moneda}*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"🟢 COMPRA: {compra:.2f}\n🔴 VENTA: {venta:.2f}\n📊 Spread: {compra-venta:.2f}\n"
    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

# ==================== COMPARATIVA P2P VS BCV ====================

def mostrar_tether_vs_bcv(chat_id):
    compra, venta = obtener_precios_con_cache('VES')
    tasas = obtener_tasas_bcv()

    if not compra or not venta or not tasas:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios del mercado...", crear_teclado_principal(chat_id))
        return

    # Tasa BCV Oficial e Intervención (+0.50% de comisión)
    tasa_bcv_oficial = tasas['usd']
    tasa_intervencion = tasa_bcv_oficial * 1.005
    media = (compra + venta) / 2.0

    analisis = obtener_analisis_ves()
    if analisis:
        max_24h = analisis['maximo']
        min_24h = analisis['minimo']
        var_pct = analisis['cambio_porcentaje']
        tendencia_str = analisis['tendencia']
    else:
        max_24h = compra
        min_24h = compra
        var_pct = 0.0
        tendencia_str = "➡️ Lateral"

    # Cálculos de brechas
    brecha_compra_bcv = compra - tasa_bcv_oficial
    pct_compra_bcv = (brecha_compra_bcv / tasa_bcv_oficial) * 100 if tasa_bcv_oficial > 0 else 0.0

    brecha_venta_bcv = venta - tasa_bcv_oficial
    pct_venta_bcv = (brecha_venta_bcv / tasa_bcv_oficial) * 100 if tasa_bcv_oficial > 0 else 0.0

    diferencial_bruto = venta - tasa_intervencion
    margen_bruto_pct = (diferencial_bruto / tasa_intervencion) * 100 if tasa_intervencion > 0 else 0.0

    fecha_hora_str = datetime.now().strftime('%d/%m %H:%M')

    mensaje = f"""📊 *MERCADO USDT / BCV — {fecha_hora_str}*

🟢 *Binance P2P (USDT)*
• Venta: *{venta:.2f} Bs*
• Compra: *{compra:.2f} Bs*
• Media: *{media:.2f} Bs*
• Tendencia (24h): {tendencia_str} (Máx {max_24h:.2f} | Mín {min_24h:.2f} | {var_pct:+.2f}%)

🏦 *Tasa Oficial (BCV)*
• BCV Oficial: *{tasa_bcv_oficial:.2f} Bs*
• BCV + 0.50%: *{tasa_intervencion:.2f} Bs*

📐 *Análisis de Brecha*
• P2P Compra vs BCV: *+{brecha_compra_bcv:.2f} Bs* ({pct_compra_bcv:+.2f}%)
• P2P Venta vs BCV: *+{brecha_venta_bcv:.2f} Bs* ({pct_venta_bcv:+.2f}%)
• Margen Operación Intervención: *~{margen_bruto_pct:.2f}%* ({diferencial_bruto:.2f} Bs)

💡 *Nota:* Si ejecutas la venta de USDT a {venta:.2f} Bs y recompras USD por intervención bancaria a {tasa_intervencion:.2f} Bs, el retorno bruto estimado ronda el {margen_bruto_pct:.1f}% antes de comisiones bancarias locales."""

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== CALCULADORA DE MARGEN ====================

def calcular_ganancia_neta(chat_id, monto=100.0):
    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    tasas = obtener_tasas_bcv()

    if not venta_ves or not tasas:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios del mercado...", crear_teclado_principal(chat_id))
        return

    # 1. Tasa BCV Oficial y Costo de Intervención
    tasa_bcv = tasas['usd']
    bcv_mas_medio = tasa_bcv * 1.005
    costo_bcv_monto = monto * bcv_mas_medio

    # 2. Comisiones Banesco mediante constantes globales
    comision_tarjeta = monto * COMISION_TARJETA_PCT
    comision_bpay = monto * COMISION_BPAY_PCT
    total_comisiones = comision_tarjeta + comision_bpay

    # 3. Liquidación Final
    usdt_neto = monto - total_comisiones

    # 4. Retorno en P2P
    total_retornado_bs = usdt_neto * venta_ves
    equivalente_usd_bcv = total_retornado_bs / tasa_bcv if tasa_bcv > 0 else 0.0

    # 5. Resultados de Ganancia
    ganancia_bs = total_retornado_bs - costo_bcv_monto
    ganancia_usd = equivalente_usd_bcv - monto
    rendimiento_pct = (ganancia_usd / monto) * 100 if monto > 0 else 0.0
    ganancia_por_dolar = ganancia_usd / monto if monto > 0 else 0.0

    mensaje = f"""🏦 *ANÁLISIS DE MARGEN Y LIQUIDACIÓN*
💰 Capital Invertido: *${monto:,.2f} USD*
📊 Tasa Oficial BCV: *{tasa_bcv:.2f} Bs*

━━━━━━━━━━━━━━━━━━━━━━━━━━
1️⃣ *COSTO DE INTERVENCIÓN (Egreso)*
• BCV Oficial: *{tasa_bcv:.2f} Bs*
• BCV + 0.50%: *{bcv_mas_medio:.2f} Bs*
• Total Invertido: *${monto:,.2f} USD* → *{costo_bcv_monto:,.2f} Bs*

━━━━━━━━━━━━━━━━━━━━━━━━━━
2️⃣ *ESTRUCTURA DE COMISIONES BANESCO*
• Tarjeta Int. ({COMISION_TARJETA_PCT * 100:.1f}%): *${comision_tarjeta:,.2f} USD*
• Bpay ({COMISION_BPAY_PCT * 100:.1f}%): *${comision_bpay:,.2f} USD*
• TOTAL COMISIONES: *${total_comisiones:,.2f} USD*

━━━━━━━━━━━━━━━━━━━━━━━━━━
3️⃣ *LIQUIDACIÓN NETA (USDT)*
• Capital bruto: *{monto:,.2f} USDT*
• Comisiones: *-{total_comisiones:,.2f} USDT*
• USDT neto obtenido: *{usdt_neto:,.2f} USDT*

━━━━━━━━━━━━━━━━━━━━━━━━━━
4️⃣ *RETORNO EN MERCADO P2P*
• Tasa de Venta USDT: *{venta_ves:.2f} Bs*
• Total Retornado: *{total_retornado_bs:,.2f} Bs*
• Equivalente USD (BCV): *${equivalente_usd_bcv:,.2f}*

━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 *GANANCIA NETA FINAL*
• En Bolívares: *{ganancia_bs:+,.2f} Bs*
• En USD Equivalente: *${ganancia_usd:+,.2f}*
• Rendimiento Operativo: *{rendimiento_pct:+.2f}%*

• 💵 Ganancia por dólar: *${ganancia_por_dolar:+.3f}* por cada $1"""

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

def calcular_conversion_bcv_medio(chat_id, texto_monto):
    tasas = obtener_tasas_bcv()
    if not tasas: return
    tasa_bcv = tasas['usd']
    bcv_mas_medio = tasa_bcv * 1.005
    texto_limpio = texto_monto.strip().lower()

    try:
        if 'bs' in texto_limpio:
            monto_bs = float(texto_limpio.replace('bs', '').replace(',', '.').strip())

            usd_oficial = monto_bs / tasa_bcv if tasa_bcv > 0 else 0
            usd_mas_medio = monto_bs / bcv_mas_medio if bcv_mas_medio > 0 else 0

            mensaje = f"""⚖️ *CONVERSOR USD / BS*

📊 *Tasa BCV Oficial:* {tasa_bcv:.2f} Bs
━━━━━━━━━━━━━━━━━━━━
✍️ *Operación (Bs ➔ $):* {monto_bs:,.2f} Bs
🇺🇸 *Total equivalente:* *${usd_oficial:,.2f} USD*
━━━━━━━━━━━━━━━━━━━━

📊 *Tasa BCV Oficial:* {tasa_bcv:.2f} Bs + 0.50%
━━━━━━━━━━━━━━━━━━━━
✍️ *Operación (Bs ➔ $):* {monto_bs:,.2f} Bs
🇺🇸 *Total equivalente:* *${usd_mas_medio:,.2f} USD*
━━━━━━━━━━━━━━━━━━━━"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

        elif '$' in texto_limpio or 'usd' in texto_limpio:
            monto_usd = float(texto_limpio.replace('$', '').replace('usd', '').replace(',', '.').strip())

            bs_oficial = monto_usd * tasa_bcv
            bs_mas_medio = monto_usd * bcv_mas_medio

            mensaje = f"""⚖️ *CONVERSOR USD / BS*

📊 *Tasa BCV Oficial:* {tasa_bcv:.2f} Bs
━━━━━━━━━━━━━━━━━━━━
✍️ *Operación ($ ➔ Bs):* ${monto_usd:,.2f} USD
🇻🇪 *Total equivalente:* *{bs_oficial:,.2f} Bs*
━━━━━━━━━━━━━━━━━━━━

📊 *Tasa BCV Oficial:* {tasa_bcv:.2f} Bs + 0.50%
━━━━━━━━━━━━━━━━━━━━
✍️ *Operación ($ ➔ Bs):* ${monto_usd:,.2f} USD
🇻🇪 *Total equivalente:* *{bs_mas_medio:,.2f} Bs*
━━━━━━━━━━━━━━━━━━━━"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))
    except Exception as e:
        print(f"Error en conversión BCV medio: {e}")

def mostrar_historial_ves(chat_id):
    analisis = obtener_analisis_ves()
    if not analisis:
        enviar_mensaje(chat_id, "📈 *HISTORIAL DE BRECHA VES*\n⏳ Sin datos suficientes aún", crear_teclado_principal(chat_id))
        return
    mensaje = f"📈 *HISTORIAL DE BRECHA VES (24h)*\n📊 *Apertura:* {analisis['apertura']:.2f} Bs\n📊 *Actual:* {analisis['actual']:.2f} Bs\n*Cambio:* {analisis['cambio']:+.2f} Bs ({analisis['cambio_porcentaje']:+.1f}%)\n📈 *Máximo:* {analisis['maximo']:.2f} Bs\n📉 *Mínimo:* {analisis['minimo']:.2f} Bs\n🧭 *Tendencia:* {analisis['tendencia']}"
    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== PROCESAR MENSAJES CON PROMPTS Y COMPATIBILIDAD ====================

def procesar_mensaje(chat_id, user_id, texto):
    global usuario_esperando_calculo, usuario_esperando_cruzado, usuario_configurando_soles
    global TASA_SOLES_TARIFARIO

    # Validación de acceso restringido por grupo/canal verificando a la persona que escribe (user_id)
    if not usuario_esta_en_grupo(user_id):
        enviar_mensaje(chat_id, "⛔ *Acceso Denegado*\n\nDebes ser miembro del grupo o canal autorizado para utilizar este bot.")
        return

    guardar_usuario(user_id)

    # Configuración de tasa por el admin
    if user_id == ADMIN_ID and usuario_configurando_soles.get(user_id):
        try:
            TASA_SOLES_TARIFARIO = float(texto.replace(',', '.'))
            guardar_configuracion()
            usuario_configurando_soles[user_id] = False  
            enviar_mensaje(chat_id, f"✅ *Tasa Soles actualizada con éxito:* `{TASA_SOLES_TARIFARIO:.2f}`", crear_teclado_remesas(user_id))
            return
        except ValueError: 
            pass  

    # Detección de entradas numéricas
    if any(char.isdigit() for char in texto):
        if usuario_esperando_cruzado.get(user_id) or 's/' in texto.lower() or 'soles' in texto.lower():
            calcular_conversion_tasas_cruzadas(chat_id, texto)
            return
        elif usuario_esperando_calculo.get(user_id) or 'bs' in texto.lower() or '$' in texto or 'usd' in texto.lower():
            calcular_conversion_bcv_medio(chat_id, texto)
            usuario_esperando_calculo[user_id] = False
            return

    # Comandos y Navegación
    if texto == '/start':
        limpiar_estados_usuario(user_id)
        msg = "👋 *¡Bienvenido al Terminal Financiero P2P & Remesas!*\n\nUsa el menú inferior para realizar conversiones, monitorear la brecha cambiaria o calcular tus márgenes operativos en tiempo real."
        enviar_mensaje(chat_id, msg, crear_teclado_principal(user_id))

    elif texto in ['📈 Comparativa P2P vs BCV', 'Tether + BCV']:
        mostrar_tether_vs_bcv(chat_id)

    elif texto in ['🧮 Conversor USD / Bs', '¿Cuánto Es?']:
        usuario_esperando_calculo[user_id] = True
        usuario_esperando_cruzado[user_id] = False
        msg = (
            "🧮 *CONVERSOR OFICIAL DE DIVISAS (USD / VES)*\n\n"
            "Ingresa el monto a consultar especificando la moneda al final:\n\n"
            "• Ejemplo en Bolívares: `5000 Bs`\n"
            "• Ejemplo en Dólares: `100 USD` o `100 $`\n\n"
            "💡 _Calcula automáticamente la tasa oficial BCV y la tasa con intervención (+0.50%)._"
        )
        enviar_mensaje(chat_id, msg, crear_teclado_principal(user_id))

    elif texto in ['📊 Calculadora de Margen', '¿Cuánto Gané?']:
        msg = (
            "📊 *SIMULADOR DE MARGEN Y LIQUIDACIÓN P2P*\n\n"
            "Ingresa el capital de inversión en *USD* para proyectar comisiones bancarias y retorno neto:\n\n"
            "• Ejemplo: `100` o `500`"
        )
        enviar_mensaje(chat_id, msg, crear_teclado_principal(user_id))

    elif texto == '📈 Historial de brecha VES':
        mostrar_historial_ves(chat_id)

    elif texto in ['💼 Panel de Operaciones', 'Remesas 💼']:
        if user_id == ADMIN_ID:
            enviar_mensaje(chat_id, "💼 *PANEL DE OPERACIONES & REMESAS*", crear_teclado_remesas(user_id))
        else:
            enviar_mensaje(chat_id, "❌ Acción restringida.", crear_teclado_principal(user_id))

    elif texto in ['💱 Conversor de Remesas', '¿Cuánto es Cruzado?']:
        if user_id == ADMIN_ID:
            usuario_esperando_calculo[user_id] = False
            usuario_esperando_cruzado[user_id] = True
            msg = (
                "💱 *CONVERSOR DINÁMICO DE REMESAS (PEN / VES)*\n\n"
                "Ingresa la cantidad indicando la divisa de origen:\n\n"
                "• Desde Perú: `100 S/` o `100 soles`\n"
                "• Desde Venezuela: `5000 Bs`"
            )
            enviar_mensaje(chat_id, msg, crear_teclado_cruzado_rapido(user_id))
        else:
            enviar_mensaje(chat_id, "❌ Acción restringida.", crear_teclado_principal(user_id))

    elif texto == '📋 Tarifario USD':
        if user_id == ADMIN_ID: mostrar_tarifario_usd(chat_id)

    elif texto == '📋 Tarifario Soles':
        if user_id == ADMIN_ID: mostrar_tarifario_soles(chat_id)

    elif texto == '⚙️ Ajustar Tasa':
        if user_id == ADMIN_ID:
            usuario_configurando_soles[user_id] = True
            msg = (
                f"⚙️ *CONFIGURACIÓN DE TASA OPERATIVA (PEN/VES)*\n\n"
                f"• Tasa actual registrada: `{TASA_SOLES_TARIFARIO:.2f}`\n\n"
                f"✍️ Envía el nuevo valor de referencia (ejemplo: `3.85`)."
            )
            enviar_mensaje(chat_id, msg, crear_teclado_remesas(user_id))

    elif texto in ['🌐 Tasas Cruzadas', 'Tasas Cruzadas']:
        if user_id == ADMIN_ID:
            mostrar_tasas_cambio(chat_id)

    elif texto in ['⚙️ Mercado P2P', '+ Opciones']:
        msg = "⚙️ *MONITOREO DE MERCADOS P2P*\n\nSelecciona una opción para consultar las cotizaciones en tiempo real:"
        enviar_mensaje(chat_id, msg, crear_teclado_opciones(user_id))

    elif texto in ['📊 P2P Multidivisa', 'Precio USDT']: mostrar_precios_usdt(chat_id)
    elif texto in ['🇻🇪 Tasa VES', 'Precio VES']: mostrar_precio_individual(chat_id, 'VES')
    elif texto in ['🇨🇴 Tasa COP', 'Precio COP']: mostrar_precio_individual(chat_id, 'COP')
    elif texto in ['🇵🇪 Tasa PEN', 'Precio PEN']: mostrar_precio_individual(chat_id, 'PEN')

    elif texto in ['👥 Usuarios Registrados', 'Usuarios Registrados']:
        if user_id == ADMIN_ID:
            usuarios = obtener_usuarios()
            mensaje = f"👥 *Usuarios activos registrados:* {len(usuarios)}"
            for uid in usuarios: mensaje += f"\n• `{uid}`"
            enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(user_id))

    elif texto in ['⬅️ Volver al Menú', 'Volver al menú anterior']:
        limpiar_estados_usuario(user_id)
        enviar_mensaje(chat_id, "🏠 *Menú Principal*", crear_teclado_principal(user_id))

    else:
        try:
            monto_usuario = float(texto.replace(',', '.'))
            if monto_usuario > 0: calcular_ganancia_neta(chat_id, monto_usuario)
        except ValueError:
            enviar_mensaje(chat_id, "⚠️ Comando o formato no reconocido. Por favor, selecciona una opción del menú.", crear_teclado_principal(user_id))

# ==================== RUTAS FLASK Y WEBHOOK ====================

@app.route('/', methods=['GET'])
def home():
    return f"Bot activo 24/7 | Muestras: {len(historial_ves)}", 200

@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = json.loads(json_string)

        message = update.get('message')
        if message:
            chat_id = message.get('chat', {}).get('id')
            user_id = message.get('from', {}).get('id')  # Se obtiene el ID del usuario real
            texto = message.get('text', '')

            if chat_id and user_id and texto:
                threading.Thread(target=procesar_mensaje, args=(chat_id, user_id, texto)).start()

        return 'OK', 200
    return 'Forbidden', 403

def configurar_webhook():
    url_app = RENDER_URL
    url_set = f"{URL_TELEGRAM}setWebhook?url={url_app}/{TOKEN}"
    try:
        r = requests.get(url_set, timeout=10)
        print("Webhook configurado:", r.json())
    except Exception as e:
        print("Error configurando Webhook:", e)

# ==================== HILO SELF-PING PARA RENDER ====================

def iniciar_self_ping():
    """
    Realiza peticiones HTTP periódicas a la app en Render cada 4.5 minutos (270s)
    para impedir el paso a modo suspensión (Sleep Mode).
    """
    def ping():
        time.sleep(10)  # Breve espera inicial mientras sube el servidor Flask
        while True:
            try:
                res = requests.get(RENDER_URL, timeout=10)
                print(f"[Self-Ping] Status: {res.status_code}")
            except Exception as e:
                print(f"[Self-Ping Error]: {e}")
            time.sleep(270)

    threading.Thread(target=ping, daemon=True).start()

def actualizar_precios():
    global cache_precios, cache_tiempo, ultimos_precios
    while True:
        try:
            precios = {}
            for m in ['VES', 'COP', 'PEN']:
                compra, venta = obtener_precios_p2p_reales(m)
                if compra and venta:
                    precios[m] = {'compra': compra, 'venta': venta}
                    cache_precios[m] = {'compra': compra, 'venta': venta}
                    cache_tiempo[m] = time.time()
                    if m == 'VES': guardar_historial_ves(compra)
            if precios:
                verificar_alertas(precios)
                verificar_fluctuacion_tasas()
            time.sleep(60)
        except Exception as e:
            print(f"Error en hilo de actualizar_precios: {e}")
            time.sleep(60)

if __name__ == "__main__":
    cargar_configuracion()
    cargar_tasas_anteriores()
    configurar_webhook()

    # Iniciar hilos en segundo plano
    iniciar_self_ping()
    threading.Thread(target=actualizar_precios, daemon=True).start()

    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
