import requests
import time
import os
import threading
import json
from datetime import datetime
from collections import deque
from flask import Flask

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
ultimo_update_id = 0

app = Flask(__name__)

# ==================== TASAS PARA TARIFARIOS Y CONVERSIÓN MANUAL ====================
TASA_SOLES_TARIFARIO = 3.80

# ==================== ALERTAS DE PRECIO FINANCIERO ====================
UMBRALES = {
    'VES': 1.0,    
    'COP': 50.0,  
    'PEN': 0.05    
}

FLUCTUACION_UMBRAL = 0.8

ultimos_precios = {'VES': None, 'COP': None, 'PEN': None}

# ==================== CONTROL DE ACCESO ====================
GRUPO_AUTORIZADO_ID = -5370892602  

def usuario_esta_en_grupo(user_id):
    return True

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
precio_apertura_ves = None

# ==================== ESTADOS DE ENTRADA ====================
usuario_esperando_calculo = {} 
usuario_esperando_cruzado = {}  
usuario_configurando_soles = {}  

# ==================== INTERFACES DE TECLADOS REORGANIZADAS ====================

def crear_teclado_principal(chat_id):
    """Menú Principal reorganizado"""
    teclado = [
        ["Tether + BCV"],
        ["¿Cuánto Es?"], 
        ["¿Cuánto Gané?"],
        ["📈 Historial de brecha VES"]
    ]

    if chat_id == ADMIN_ID:
        teclado.append(["Remesas 💼"])

    teclado.append(["+ Opciones"])
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_remesas(chat_id):
    """Submenú Remesas"""
    teclado = [
        ["¿Cuánto es Cruzado?"],
        ["📋 Tarifario USD"],
        ["📋 Tarifario Soles"],
        ["⚙️ Ajustar Tasa"],
        ["Tasas Cruzadas"],
        ["Volver al menú anterior"]
    ]
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_opciones(chat_id):
    """Segundo Menú (+ Opciones)"""
    teclado = [
        ["Precio USDT"],
        ["Precio VES"],
        ["Precio COP"],
        ["Precio PEN"]
    ]

    if chat_id == ADMIN_ID:
        teclado.append(["Usuarios Registrados"])

    teclado.append(["Volver al menú anterior"])
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_cruzado_rapido(chat_id):
    teclado = [
        ["20 S/", "50 S/", "100 S/"],
        ["5000 Bs", "10000 Bs", "20000 Bs"],
        ["Volver al menú anterior"]
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
    except:
        return False

# ==================== OBTENCIÓN P2P Y BCV ====================

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
        except:
            pass

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
        except:
            pass

        if compra is None or venta is None:
            return None, None
        if compra < venta:
            compra, venta = venta, compra
        return compra, venta
    except:
        return None, None

def obtener_tasa_bcv_actual():
    tasas = obtener_tasas_bcv()
    if tasas and tasas.get('usd'):
        return tasas['usd']
    return 45.00 

def obtener_tasas_bcv():
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
    except:
        pass
    return None

# ==================== TARIFARIOS UNIFICADOS ====================

def mostrar_tarifario_usd(chat_id):
    tasa_bcv = obtener_tasa_bcv_actual()
    mensaje = f"📋 *TARIFARIO EN USD*\n"
    mensaje += f"🕐 Tasa BCV: {tasa_bcv:.2f} Bs | Perú - Ven Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n"

    tabla = f"```\n"
    tabla += f"{'Dólares'.ljust(9)}|{'Recibes (Bs)'.ljust(14)}|{'Equivalente'.ljust(12)}\n"
    tabla += f"---------------------------------\n"

    montos_usd = [10, 20, 30, 50, 100, 150, 200, 250, 300, 500]

    for usd in montos_usd:
        recibes_bs = usd * tasa_bcv
        equiv_soles = recibes_bs / TASA_SOLES_TARIFARIO if TASA_SOLES_TARIFARIO > 0 else 0

        col_usd = f"{usd}$".ljust(9)
        col_bs = f"{recibes_bs:,.2f}".ljust(14)
        col_soles = f"{equiv_soles:,.2f} S/".ljust(12)

        tabla += f"{col_usd}|{col_bs}|{col_soles}\n"

    tabla += f"```"
    enviar_mensaje(chat_id, mensaje + tabla, crear_teclado_remesas(chat_id))

def mostrar_tarifario_soles(chat_id):
    tasa_bcv = obtener_tasa_bcv_actual()
    mensaje = f"📋 *TARIFARIO EN SOLES A BOLÍVARES*\n"
    mensaje += f"🕐 Tasa BCV: {tasa_bcv:.2f} Bs | Perú - Ven Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n"

    tabla = f"```\n"
    tabla += f"{'Enviado'.ljust(10)}|{'Recibes (Bs)'.ljust(14)}|{'Equivalente'.ljust(12)}\n"
    tabla += f"---------------------------------\n"

    montos_soles = [10, 20, 30, 50, 100, 150, 200, 300, 500, 1000]

    for soles in montos_soles:
        recibes_bs = soles * TASA_SOLES_TARIFARIO
        equiv_usd = recibes_bs / tasa_bcv if tasa_bcv > 0 else 0

        col_soles = f"{soles} S/".ljust(10)
        col_bs = f"{recibes_bs:,.2f}".ljust(14)
        col_usd = f"{equiv_usd:,.2f}$".ljust(12)

        tabla += f"{col_soles}|{col_bs}|{col_usd}\n"

    tabla += f"```"
    enviar_mensaje(chat_id, mensaje + tabla, crear_teclado_remesas(chat_id))

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

    mensaje = f"🏦 *TASAS DE CAMBIO CRUZADAS*\n"
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

# ==================== ¿CUÁNTO ES CRUZADO? ====================

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

# ==================== RESTO DE FUNCIONES DE CÁLCULO E HISTORIAL ====================

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
            except:
                pass
    ultimas_tasas_cruzadas = tasas_actuales.copy()
    guardar_tasas_anteriores()

with open("tasas_anteriores.json", "w") as f: pass
ultimas_tasas_cruzadas = {}
def guardar_tasas_anteriores():
    try:
        with open("tasas_anteriores.json", 'w') as f: json.dump(ultimas_tasas_cruzadas, f)
    except: pass
def cargar_tasas_anteriores():
    global ultimas_tasas_cruzadas
    try:
        if os.path.exists("tasas_anteriores.json"):
            with open("tasas_anteriores.json", 'r') as f: ultimas_tasas_cruzadas = json.load(f)
    except: pass

def guardar_historial_ves(precio):
    global precio_apertura_ves
    historial_ves.append(precio)
    if precio_apertura_ves is None:
        precio_apertura_ves = precio

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

def verificar_alertas(precios):
    global ultimos_precios
    if not precios: return
    usuarios = obtener_usuarios()
    if not usuarios: return

    for usuario in usuarios:
        for moneda in ['VES', 'COP', 'PEN']:
            if moneda not in precios or not precios[moneda]: continue
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
                mensaje = f"\n{emoji} *🔔 ALERTA {moneda}* {emoji}\n\n{direccion} en {signo}{cambio:.2f}\n\n📊 *Detalles:*\n• Anterior: {ultimos_precios[moneda]:.2f}\n• Actual: {precio_actual:.2f}\n• Cambio: {signo}{cambio:.2f} ({signo}{cambio_porcentaje:.2f}%)\n\n🕐 {datetime.now().strftime('%H:%M:%S')}\n"
                enviar_mensaje(usuario, mensaje)
                time.sleep(0.05)
                if moneda in ['COP', 'PEN']:
                    enviar_mensaje(ADMIN_ID, f"📨 *Alerta {moneda} processed con éxito.*")
        for moneda in ['VES', 'COP', 'PEN']:
            if moneda in precios and precios[moneda]: ultimos_precios[moneda] = precios[moneda]['compra']

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

def mostrar_tether_vs_bcv(chat_id):
    compra, venta = obtener_precios_con_cache('VES')
    tasas = obtener_tasas_bcv()
    if not compra or not tasas:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado_principal(chat_id))
        return
    
    tasa_bcv = tasas['usd']
    bcv_con_porcentaje = tasa_bcv * 1.005
    diff_compra = compra - bcv_con_porcentaje
    pct_compra = (diff_compra / bcv_con_porcentaje) * 100 if bcv_con_porcentaje > 0 else 0

    # Operaciones matemáticas calculadas NETAMENTE a Tasa BCV Oficial
    cantidades_usd = [10, 20, 50, 100]
    cantidades_bs = [1000, 5000, 10000, 50000]

    operaciones_usd_bs = "\n".join([f"• {monto}$ = *{(monto * tasa_bcv):,.2f} Bs*" for monto in cantidades_usd])
    operaciones_bs_usd = "\n".join([f"• {monto:,.0f} Bs = *{(monto / tasa_bcv):,.2f}$*" for monto in cantidades_bs])

    mensaje = f"""🪙 *TETHER + BCV*
🕐 {datetime.now().strftime('%H:%M:%S')}

🏦 *BCV Oficial:* {tasa_bcv:.2f} Bs
📈 *BCV + 0.50%:* {bcv_con_porcentaje:.2f} Bs

🇻🇪 *PRECIO VES EN EL MOMENTO (Binance P2P):*
  🟢 COMPRA: {compra:.2f} Bs
  🔴 VENTA: {venta:.2f} Bs
  📊 Spread: {compra-venta:.2f} Bs

⚖️ *Diferencia vs BCV+0.50%:*:
  Diferencia: {diff_compra:+.2f} Bs
  Porcentaje: {pct_compra:+.1f}%

━━━━━━━━━━━━━━━━━━━━
💵 *CÁLCULOS DÓLARES A BOLÍVARES (Tasa BCV):*
{operaciones_usd_bs}

🇻🇪 *CÁLCULOS BOLÍVARES A DÓLARES (Tasa BCV):*
{operaciones_bs_usd}
━━━━━━━━━━━━━━━━━━━━
💡 _Para montos personalizados, usa la opción *"¿Cuánto Es?"* en el menú._"""

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

def calcular_ganancia_neta(chat_id, monto=100.0):
    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    tasas = obtener_tasas_bcv()
    if not compra_ves or not tasas: return
    tasa_bcv = tasas['usd']
    bcv_mas_medio = tasa_bcv * 1.005
    costo_bcv_monto = bcv_mas_medio * monto
    usdt_neto_tarjeta = monto * (1 - 0.015)  
    usdt_final = usdt_neto_tarjeta * (1 - 0.041)  
    retorno_ves = usdt_final * venta_ves
    ganancia_neta_ves = retorno_ves - costo_bcv_monto
    ganancia_porcentaje = (ganancia_neta_ves / costo_bcv_monto) * 100 if costo_bcv_monto > 0 else 0
    mensaje = f"💵 *CALCULADORA DE RETORNO NETO*\n\nAnálisis financiero detallado basado en un capital de *${monto:,.2f} USD*:\n\n*1. Costo de Intervención (Egreso):*\n• BCV Oficial: {tasa_bcv:.2f} Bs\n• BCV + 0.50%: {bcv_mas_medio:.2f} Bs\n• Total Invertido ({monto:.2f}$): *{costo_bcv_monto:,.2f} Bs*\n\n*2. Liquidación y Comisiones:*\n• Capital base: {monto:.2f} USDT\n• Tarjeta (-1.5%): {usdt_neto_tarjeta:,.2f} USDT\n• Bpay (-4.1%): {usdt_final:,.4f} USDT\n\n*3. Retorno en P2P (Venta VES):*\n• Tasa de Venta: {venta_ves:.2f} Bs\n• Total Retornado: *{retorno_ves:,.2f} Bs*\n\n━━━━━━━━━━━━━━━━━━━━\n📊 *GANANCIA NETA TOTAL:*\n• Retorno Neto: *{ganancia_neta_ves:+,.2f} Bs* ({ganancia_porcentaje:+.2f}%)\n━━━━━━━━━━━━━━━━━━━━"
    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

def calcular_conversion_bcv_medio(chat_id, texto_monto):
    tasas = obtener_tasas_bcv()
    if not tasas: return
    tasa_bcv = tasas['usd']
    texto_limpio = texto_monto.strip().lower()
    try:
        if 'bs' in texto_limpio:
            monto_bs = float(texto_limpio.replace('bs', '').replace(',', '.').strip())
            resultado_usd = monto_bs / tasa_bcv
            mensaje = f"⚖️ *CALCULADORA DE CONVERSIÓN*\n\n📊 *Tasa BCV Oficial:* *{tasa_bcv:.2f} Bs*\n━━━━━━━━━━━━━━━━━━━━\n✍️ *Operación (Bs ➔ $):* {monto_bs:,.2f} Bs\n🇺🇸 *Total equivalente:* *${resultado_usd:,.2f} USD*\n━━━━━━━━━━━━━━━━━━━━"
            enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))
        elif '$' in texto_limpio or 'usd' in texto_limpio:
            monto_usd = float(texto_limpio.replace('$', '').replace('usd', '').replace(',', '.').strip())
            resultado_bs = monto_usd * tasa_bcv
            mensaje = f"⚖️ *CALCULADORA DE CONVERSIÓN*\n\n📊 *Tasa BCV Oficial:* *{tasa_bcv:.2f} Bs*\n━━━━━━━━━━━━━━━━━━━━\n✍️ *Operación ($ ➔ Bs):* ${monto_usd:,.2f} USD\n🇻🇪 *Total equivalente:* *{resultado_bs:,.2f} Bs*\n━━━━━━━━━━━━━━━━━━━━"
            enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))
    except: pass

def mostrar_historial_ves(chat_id):
    analisis = obtener_analisis_ves()
    if not analisis:
        enviar_mensaje(chat_id, "📈 *HISTORIAL DE BRECHA VES*\n⏳ Sin datos suficientes aún", crear_teclado_principal(chat_id))
        return
    mensaje = f"📈 *HISTORIAL DE BRECHA VES (24h)*\n📊 *Apertura:* {analisis['apertura']:.2f} Bs\n📊 *Actual:* {analisis['actual']:.2f} Bs\n*Cambio:* {analisis['cambio']:+.2f} Bs ({analisis['cambio_porcentaje']:+.1f}%)\n📈 *Máximo:* {analisis['maximo']:.2f} Bs\n📉 *Mínimo:* {analisis['minimo']:.2f} Bs\n🧭 *Tendencia:* {analisis['tendencia']}"
    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== PROCESAR MENSAJES ====================

def procesar_mensaje(chat_id, texto):
    global usuario_esperando_calculo, usuario_esperando_cruzado, usuario_configurando_soles
    global TASA_SOLES_TARIFARIO

    if not usuario_esta_en_grupo(chat_id): return
    guardar_usuario(chat_id)

    # Captura de configuración de tasa soles manual
    if chat_id == ADMIN_ID and usuario_configurando_soles.get(chat_id):
        try:
            TASA_SOLES_TARIFARIO = float(texto.replace(',', '.'))
            usuario_configurando_soles[chat_id] = False  
            enviar_mensaje(chat_id, f"✅ *Tasa Soles configurada con éxito:* {TASA_SOLES_TARIFARIO:.2f}", crear_teclado_remesas(chat_id))
            return
        except ValueError: pass  

    # Gestión numérica en mensajes abiertos
    if any(char.isdigit() for char in texto):
        if usuario_esperando_cruzado.get(chat_id) or 's/' in texto.lower() or 'soles' in texto.lower():
            calcular_conversion_tasas_cruzadas(chat_id, texto)
            return
        elif usuario_esperando_calculo.get(chat_id) or 'bs' in texto.lower() or '$' in texto or 'usd' in texto.lower():
            calcular_conversion_bcv_medio(chat_id, texto)
            usuario_esperando_calculo[chat_id] = False
            return

    # --- ENRUTAMIENTO DE BOTONES Y ENTRADAS ---
    if texto == '/start':
        usuario_configurando_soles[chat_id] = False
        enviar_mensaje(chat_id, "Bienvenido a Asistente Remesas P2P.", crear_teclado_principal(chat_id))

    elif texto == 'Tether + BCV':
        mostrar_tether_vs_bcv(chat_id)

    elif texto == '¿Cuánto Es?':
        usuario_esperando_calculo[chat_id] = True
        usuario_esperando_cruzado[chat_id] = False
        enviar_mensaje(chat_id, "✍️ Escribe la cantidad seguida de *Bs* o *$*.", crear_teclado_principal(chat_id))

    elif texto == '¿Cuánto Gané?':
        enviar_mensaje(chat_id, "✍️ Escribe directamente el monto en *USD* que deseas calcular (Ej: `100`).", crear_teclado_principal(chat_id))

    elif texto == '📈 Historial de brecha VES':
        mostrar_historial_ves(chat_id)

    elif texto == 'Remesas 💼':
        if chat_id == ADMIN_ID:
            enviar_mensaje(chat_id, "💼 *SUBMENÚ REMESAS & TARIFARIOS MANUALEZ*", crear_teclado_remesas(chat_id))
        else:
            enviar_mensaje(chat_id, "❌ Acción restringida.", crear_teclado_principal(chat_id))

    elif texto == '¿Cuánto es Cruzado?':
        if chat_id == ADMIN_ID:
            usuario_esperando_calculo[chat_id] = False
            usuario_esperando_cruzado[chat_id] = True
            enviar_mensaje(chat_id, "✍️ Escribe el monto seguido de *S/* o *Bs*.", crear_teclado_cruzado_rapido(chat_id))
        else:
            enviar_mensaje(chat_id, "❌ Acción restringida.", crear_teclado_principal(chat_id))

    elif texto == '📋 Tarifario USD':
        if chat_id == ADMIN_ID: mostrar_tarifario_usd(chat_id)

    elif texto == '📋 Tarifario Soles':
        if chat_id == ADMIN_ID: mostrar_tarifario_soles(chat_id)

    elif texto == '⚙️ Ajustar Tasa':
        if chat_id == ADMIN_ID:
            usuario_configurando_soles[chat_id] = True
            enviar_mensaje(chat_id, f"⚙️ *Tasa Actual:* {TASA_SOLES_TARIFARIO:.2f}\n\n✍️ Envía el nuevo valor (Ej: `3.85`).", crear_teclado_remesas(chat_id))

    elif texto == 'Tasas Cruzadas':
        if chat_id == ADMIN_ID:
            mostrar_tasas_cambio(chat_id)

    elif texto == '+ Opciones':
        enviar_mensaje(chat_id, "📋 *SEGUNDO MENÚ (MERCADO P2P)*", crear_teclado_opciones(chat_id))

    elif texto == 'Precio USDT': mostrar_precios_usdt(chat_id)
    elif texto == 'Precio VES': mostrar_precio_individual(chat_id, 'VES')
    elif texto == 'Precio COP': mostrar_precio_individual(chat_id, 'COP')
    elif texto == 'Precio PEN': mostrar_precio_individual(chat_id, 'PEN')

    elif texto == 'Usuarios Registrados':
        if chat_id == ADMIN_ID:
            usuarios = obtener_usuarios()
            mensaje = f"👥 *Usuarios activos:* {len(usuarios)}"
            for uid in usuarios: mensaje += f"\n• `{uid}`"
            enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

    elif texto == 'Volver al menú anterior':
        usuario_configurando_soles[chat_id] = False
        usuario_esperando_calculo[chat_id] = False
        usuario_esperando_cruzado[chat_id] = False
        enviar_mensaje(chat_id, "🏠 *Regresando al menú principal*", crear_teclado_principal(chat_id))

    else:
        try:
            monto_usuario = float(texto.replace(',', '.'))
            if monto_usuario > 0: calcular_ganancia_neta(chat_id, monto_usuario)
        except ValueError:
            enviar_mensaje(chat_id, "Comando no reconocido.", crear_teclado_principal(chat_id))

# ==================== POLLING & BACKLOGS ====================

def recibir_mensajes():
    global ultimo_update_id
    while True:
        try:
            url = URL_TELEGRAM + "getUpdates"
            params = {'offset': ultimo_update_id + 1, 'timeout': 30}
            response = requests.get(url, params=params, timeout=35)
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    for update in data.get('result', []):
                        ultimo_update_id = update.get('update_id', 0)
                        message = update.get('message')
                        if message:
                            chat_id = message.get('chat', {}).get('id')
                            texto = message.get('text', '')
                            if chat_id and texto:
                                threading.Thread(target=procesar_mensaje, args=(chat_id, texto)).start()
            time.sleep(1)
        except: time.sleep(5)

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
        except: time.sleep(60)

def mantener_activo():
    while True:
        try:
            url = "https://telegram-usdt-bot-vf5t.onrender.com/"
            requests.get(url, timeout=10)
        except: pass
        time.sleep(300)

@app.route('/')
def home():
    return f"Bot activo 24/7 | Muestras: {len(historial_ves)}"
if __name__ == "__main__":
    cargar_tasas_anteriores()
    threading.Thread(target=recibir_mensajes, daemon=True).start()
    threading.Thread(target=actualizar_precios, daemon=True).start()
    threading.Thread(target=mantener_activo, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
