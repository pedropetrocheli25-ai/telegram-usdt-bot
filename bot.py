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
    pass  # Evita errores si se prueba localmente en Windows

TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not TOKEN or not ADMIN_ID:
    print("ERROR: TELEGRAM_TOKEN o ADMIN_ID no configurados")
    exit(1)

ADMIN_ID = int(ADMIN_ID)
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"
ultimo_update_id = 0

app = Flask(__name__)

# ==================== TASAS PARA TARIFARIOS ====================
TASA_SOLES_TARIFARIO = 3.80

# ==================== ALERTAS DE PRECIO FINANCIERO ====================
UMBRALES = {
    'VES': 1.0,    # 1.00 VES neto
    'COP': 100.0,  # 100.00 COP neto
    'PEN': 0.10    # 0.10 PEN neto
}

FLUCTUACION_UMBRAL = 0.8

ultimos_precios = {'VES': None, 'COP': None, 'PEN': None}

# ==================== CONTROL DE ACCESO AUTOMÁTICO por GRUPO ====================
GRUPO_AUTORIZADO_ID = -5370892602  

def usuario_esta_en_grupo(user_id):
    """Acceso temporalmente abierto para todos los usuarios (Evita Acceso Denegado)"""
    return True

# Mantener compatibilidad con funciones previas de mensajería masiva
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

# ==================== HISTORIAL (SOLO VES) ====================
historial_ves = deque(maxlen=1440)
precio_apertura_ves = None

# ==================== ESTADOS DE ENTRADA DE USUARIOS ====================
usuario_esperando_calculo = {} 
usuario_configurando_soles = {}  # Estado específico para que no interfiera con otros cálculos

# ==================== FUNCIONES BASE DE TELEGRAM ====================

def crear_teclado_principal(chat_id):
    """Genera el menú de inicio con la nueva estructura de botones solicitada"""
    teclado = [
        ["Tether + BCV"],
        ["¿Cuánto es?", "¿Cuánto Gané?"],
        ["📋 Tarifario USD", "📋 Tarifario Soles"],
        ["📈 Historial de brecha VES"]
    ]

    # Botones exclusivos del Administrador ocultos al público
    if chat_id == ADMIN_ID:
        teclado.append(["🏦 Tasas de Cambio", "⚙️ Ajustar Tasas"])

    teclado.append(["+ Opciones"])

    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_opciones(chat_id):
    """Genera el menú de opciones secundarias con la nueva estructura solicitada"""
    teclado = [
        ["Precio USDT"],
        ["Precio VES"],
        ["Precio COP"],
        ["Precio PEN"]
    ]

    if chat_id == ADMIN_ID:
        teclado.append(["Usuarios Registrados"])

    teclado.append(["Volver al menú principal"])

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

# ==================== PRECIOS CON CACHÉ ====================

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
                        try:
                            p = float(a['adv']['price'])
                            if 1 < p < 100000:
                                precios.append(p)
                        except:
                            pass
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
                        try:
                            p = float(a['adv']['price'])
                            if 1 < p < 100000:
                                precios.append(p)
                        except:
                            pass
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

# ==================== GENERADOR DE TARIFARIOS CORREGIDOS ====================

def obtener_tasa_bcv_actual():
    tasas = obtener_tasas_bcv()
    if tasas and tasas.get('usd'):
        return tasas['usd']
    return 45.00 

def mostrar_tarifario_usd(chat_id):
    tasa_bcv = obtener_tasa_bcv_actual()
    mensaje = f"📋 *TARIFARIO EN USD REAL*\n"
    mensaje += f"🕐 Tasa BCV (API): {tasa_bcv:.2f} Bs | Soles Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n"
    
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
    enviar_mensaje(chat_id, mensaje + tabla, crear_teclado_principal(chat_id))

def mostrar_tarifario_soles(chat_id):
    tasa_bcv = obtener_tasa_bcv_actual()
    mensaje = f"📋 *TARIFARIO EN SOLES A BOLÍVARES*\n"
    mensaje += f"🕐 Tasa BCV (API): {tasa_bcv:.2f} Bs | Soles Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n"
    
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
    enviar_mensaje(chat_id, mensaje + tabla, crear_teclado_principal(chat_id))

# ==================== TASAS CRUZADAS MODIFICADAS ====================

def calcular_tasas_cruzadas():
    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    compra_cop, venta_cop = obtener_precios_con_cache('COP')
    compra_pen, venta_pen = obtener_precios_con_cache('PEN')

    if not all([compra_ves, venta_ves, compra_cop, venta_cop, compra_pen, venta_pen]):
        return None

    tasas = {}

    # PERÚ (PEN) -> MODIFICADO: Mantiene el 5% original y descuenta 0.50 adicionales al resultado
    tasas['Perú → Venezuela'] = ((venta_ves / compra_pen) * 0.95) - 0.50
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
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))
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

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== ALERTA DE FLUCTUACIÓN ====================

ultimas_tasas_cruzadas = {}
TASAS_ANTERIORES_ARCHIVO = "tasas_anteriores.json"

def guardar_tasas_anteriores():
    try:
        with open(TASAS_ANTERIORES_ARCHIVO, 'w') as f:
            json.dump(ultimas_tasas_cruzadas, f)
    except:
        pass

def cargar_tasas_anteriores():
    global ultimas_tasas_cruzadas
    try:
        if os.path.exists(TASAS_ANTERIORES_ARCHIVO):
            with open(TASAS_ANTERIORES_ARCHIVO, 'r') as f:
                ultimas_tasas_cruzadas = json.load(f)
    except:
        pass

def verificar_fluctuacion_tasas():
    global ultimas_tasas_cruzadas

    tasas_actuales = calcular_tasas_cruzadas()
    if not tasas_actuales:
        return

    if not ultimas_tasas_cruzadas:
        using_tasas = tasas_actuales.copy()
        ultimas_tasas_cruzadas = using_tasas
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
        print(f"🔔 Alerta de fluctuación enviada a la lista de control.")

    ultimas_tasas_cruzadas = tasas_actuales.copy()
    guardar_tasas_anteriores()

# ==================== HISTORIAL ====================

def guardar_historial_ves(precio):
    global precio_apertura_ves
    historial_ves.append(precio)
    if precio_apertura_ves is None:
        precio_apertura_ves = precio
    print(f"📊 Historial VES: {len(historial_ves)} muestras")

def obtener_analisis_ves():
    if not historial_ves:
        return None
    precios = list(historial_ves)
    if len(precios) < 2:
        return None
    precio_actual = precios[-1]
    precio_inicio = precios[0]
    cambio = precio_actual - precio_inicio
    cambio_porcentaje = (cambio / precio_inicio) * 100 if precio_inicio != 0 else 0
    precio_max = max(precios)
    precio_min = min(precios)
    tendencia = "↗️ Alcista" if len(precios) > 10 and precios[-1] > precios[-10] else "↘️ Bajista"
    if len(precios) > 10 and abs(precios[-1] - precios[-10]) < 0.01:
        tendencia = "➡️ Lateral"
    return {
        'actual': precio_actual,
        'apertura': precio_inicio,
        'cambio': cambio,
        'cambio_porcentaje': cambio_porcentaje,
        'maximo': precio_max,
        'minimo': precio_min,
        'tendencia': tendencia,
        'muestras': len(precios)
    }

# ==================== ALERTAS DE PRECIO ====================

def verificar_alertas(precios):
    global ultimos_precios
    if not precios:
        return

    usuarios = obtener_usuarios()
    if not usuarios:
        return

    for usuario in usuarios:
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

                mensaje = f"""
{emoji} *🔔 ALERTA {moneda}* {emoji}

{direccion} en {signo}{cambio:.2f}

📊 *Detalles:*
• Anterior: {ultimos_precios[moneda]:.2f}
• Actual: {precio_actual:.2f}
• Cambio: {signo}{cambio:.2f} ({signo}{cambio_porcentaje:.2f}%)

🕐 {datetime.now().strftime('%H:%M:%S')}
"""
                enviar_mensaje(usuario, mensaje)
                time.sleep(0.05)

                if moneda in ['COP', 'PEN']:
                    enviar_mensaje(ADMIN_ID, f"📨 *Alerta {moneda} processed con éxito.*")

        for moneda in ['VES', 'COP', 'PEN']:
            if moneda in precios and precios[moneda]:
                ultimos_precios[moneda] = precios[moneda]['compra']

# ==================== MOSTRAR PRECIOS ====================

def mostrar_precios_usdt(chat_id):
    precios = {}
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_con_cache(m)
        if compra and venta:
            precios[m] = {'compra': compra, 'venta': venta}
    if not precios:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado_opciones(chat_id))
        return

    mensaje = f"💰 *PRECIOS USDT P2P*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    for m, datos in precios.items():
        mensaje += f"*{m}*\n"
        mensaje += f"  🟢 COMPRA: {datos['compra']:.2f}\n"
        mensaje += f"  🔴 VENTA: {datos['venta']:.2f}\n"
        mensaje += f"  📊 Spread: {datos['compra']-datos['venta']:.2f}\n\n"

    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

def mostrar_precio_individual(chat_id, moneda):
    compra, venta = obtener_precios_con_cache(moneda)
    if not compra or not venta:
        enviar_mensaje(chat_id, f"⏳ Obteniendo precio {moneda}...", crear_teclado_opciones(chat_id))
        return

    mensaje = f"💰 *PRECIO {moneda}*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"🟢 COMPRA: {compra:.2f}\n"
    mensaje += f"🔴 VENTA: {venta:.2f}\n"
    mensaje += f"📊 Spread: {compra-venta:.2f}\n"

    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

# ==================== TETHER + BCV ====================

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

def mostrar_tether_vs_bcv(chat_id):
    compra, venta = obtener_precios_con_cache('VES')
    if not compra or not venta:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado_principal(chat_id))
        return

    tasas = obtener_tasas_bcv()
    if not tasas:
        enviar_mensaje(chat_id, "⏳ Obteniendo tasas...", crear_teclado_principal(chat_id))
        return

    bcv_con_porcentaje = tasas['usd'] * 1.005

    diff_compra = compra - bcv_con_porcentaje
    pct_compra = (diff_compra / bcv_con_porcentaje) * 100 if bcv_con_porcentaje > 0 else 0

    mensaje = f"🪙 *TETHER + BCV (+0.50%)*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    
    mensaje += f"🏦 *BCV Oficial:* {tasas['usd']:.2f} Bs\n"
    mensaje += f"📈 *BCV + 0.50%:* {bcv_con_porcentaje:.2f} Bs\n\n"
    
    mensaje += f"🇻🇪 *PRECIO VES EN EL MOMENTO (Binance P2P):*\n"
    mensaje += f"  🟢 COMPRA (Tasa): {compra:.2f} Bs\n"
    mensaje += f"  🔴 VENTA: {venta:.2f} Bs\n"
    mensaje += f"  📊 Spread: {compra-venta:.2f} Bs\n\n"
    
    mensaje += f"⚖️ *Diferencia vs BCV+0.50%:*\n"
    mensaje += f"  Diferencia: {diff_compra:+.2f} Bs\n"
    mensaje += f"  Porcentaje: {pct_compra:+.1f}%\n"

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== CALCULO ¿CUÁNTO GANÉ? ====================

def calcular_ganancia_neta(chat_id, monto=100.0):
    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    tasas = obtener_tasas_bcv()

    if not compra_ves or not tasas:
        enviar_mensaje(chat_id, "⏳ No se pudieron cargar los precios de Binance P2P o del BCV. Intenta de nuevo.", crear_teclado_principal(chat_id))
        return

    bcv_mas_medio = tasas['usd'] * 1.005
    costo_bcv_monto = bcv_mas_medio * monto

    usdt_neto_tarjeta = monto * (1 - 0.015)  
    usdt_final = usdt_neto_tarjeta * (1 - 0.041)  

    retorno_ves = usdt_final * venta_ves

    ganancia_neta_ves = retorno_ves - costo_bcv_monto
    ganancia_porcentaje = (ganancia_neta_ves / costo_bcv_monto) * 100 if costo_bcv_monto > 0 else 0

    emoji_resultado = "💵" if ganancia_neta_ves >= 0 else "⚠️"
    
    mensaje = f"""{emoji_resultado} *CALCULADORA DE RETORNO NETO*

Análisis financiero detallado basado en un capital de *${monto:,.2f} USD*:

*1. Costo de Intervención (Egreso):*
• BCV Oficial: {tasas['usd']:.2f} Bs
• BCV + 0.50%: {bcv_mas_medio:.2f} Bs
• Total Invertido ({monto:.2f}$): *{costo_bcv_monto:,.2f} Bs*

*2. Liquidación y Comisiones:*
• Capital base: {monto:.2f} USDT
• Tarjeta (-1.5%): {usdt_neto_tarjeta:,.2f} USDT
• Bpay (-4.1%): {usdt_final:,.4f} USDT (Monto a liquidar)

*3. Retorno en P2P (Venta VES):*
• Tasa de Venta: {venta_ves:.2f} Bs
• Total Retornado: *{retorno_ves:,.2f} Bs*

━━━━━━━━━━━━━━━━━━━━
📊 *GANANCIA NETA TOTAL:*
• Retorno Neto: *{ganancia_neta_ves:+,.2f} Bs* ({ganancia_porcentaje:+.2f}%)
━━━━━━━━━━━━━━━━━━━━

🕐 {datetime.now().strftime('%H:%M:%S')} (Caracas)"""

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== CALCULO ¿CUÁNTO ES? MODIFICADO Y AMPLIADO ====================

def calcular_conversion_bcv_medio(chat_id, texto_monto):
    tasas = obtener_tasas_bcv()
    if not tasas:
        enviar_mensaje(chat_id, "⏳ No se pudo obtener la tasa BCV oficial en este momento.", crear_teclado_principal(chat_id))
        return

    tasa_bcv = tasas['usd']
    bcv_mas_medio = tasa_bcv * 1.005
    texto_limpio = texto_monto.strip().lower()

    # Cargar Tasas Cruzadas para los cálculos avanzados de Soles/Bolívares
    tasas_cruzadas = calcular_tasas_cruzadas()
    tasa_peru_ven = tasas_cruzadas['Perú → Venezuela'] if tasas_cruzadas else 250.00
    tasa_ven_peru = tasas_cruzadas['Venezuela → Perú'] if tasas_cruzadas else 265.00

    try:
        # --- FLUJO CON SOLES (S/ o soles) ---
        if 's/' in texto_limpio or 'soles' in texto_limpio or 'sol' in texto_limpio:
            monto_str = texto_limpio.replace('s/', '').replace('soles', '').replace('sol', '').replace(',', '.').strip()
            monto_soles = float(monto_str)

            # 1. Entrada de Soles usando Perú → Venezuela
            resultado_bs_pv = monto_soles * tasa_peru_ven
            resultado_usd_pv = resultado_bs_pv / tasa_bcv

            # 2. Entrada de Soles simulando salida (Venezuela → Perú)
            resultado_bs_vp = monto_soles * tasa_ven_peru
            resultado_usd_vp = resultado_bs_vp / tasa_bcv

            mensaje = f"""📊 *PROCESAMIENTO DINÁMICO DE SOLES*

*Tasa BCV:* {tasa_bcv:.2f} Bs
*Tasa Perú → Venezuela:* {tasa_peru_ven:.2f}
*Tasa Venezuela → Perú:* {tasa_ven_peru:.2f}
━━━━━━━━━━━━━━━━━━━━
🇵🇪 ➔ 🇻🇪 *Operación Perú → Venezuela:*
• {monto_soles:,.2f} Soles, Equivalente a *{resultado_bs_pv:,.2f} Bs*, *{resultado_usd_pv:,.2f}$* a tasa BCV

🇻🇪 ➔ 🇵🇪 *Operación Venezuela → Perú:*
• Para que lleguen {monto_soles:,.2f} Soles se necesita *{resultado_bs_vp:,.2f} Bs*, equivalente a *{resultado_usd_vp:,.2f}$* a tasa BCV
━━━━━━━━━━━━━━━━━━━━
🕐 {datetime.now().strftime('%H:%M:%S')} (Caracas)"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

        # --- FLUJO CON BOLÍVARES (Bs) ---
        elif 'bs' in texto_limpio:
            monto_str = texto_limpio.replace('bs', '').replace(',', '.').strip()
            monto_bs = float(monto_str)
            
            # Cálculo estándar BCV+0.50%
            resultado_usd_std = monto_bs / bcv_mas_medio

            # Cálculos avanzados usando tasas cruzadas y BCV solicitados
            resultado_usd_bcv = monto_bs / tasa_bcv
            resultado_soles_pv = monto_bs / tasa_peru_ven
            resultado_soles_vp = monto_bs / tasa_ven_peru

            mensaje = f"""⚖️ *CALCULADORA MULTI-CONVERSIÓN*

*Tasa BCV:* {tasa_bcv:.2f} Bs | *BCV + 0.50%:* {bcv_mas_medio:.2f} Bs
*Tasa Perú → Venezuela:* {tasa_peru_ven:.2f} | *Tasa Venezuela → Perú:* {tasa_ven_peru:.2f}
━━━━━━━━━━━━━━━━━━━━
✍️ *Conversión Estándar:*
• {monto_bs:,.2f} Bs ➔ *${resultado_usd_std:,.2f} USD* (A tasa BCV + 0.50%)

🇵🇪 ➔ 🇻🇪 *Fórmula Perú → Venezuela:*
• {monto_bs:,.2f} Bs, *${resultado_usd_bcv:,.2f}$* a tasa BCV, son *{resultado_soles_pv:,.2f} Soles*

🇻🇪 ➔ 🇵🇪 *Fórmula Venezuela → Perú:*
• Por {monto_bs:,.2f} Bs  equivalente a *${resultado_usd_bcv:,.2f} $* (tasa BCV), llegan *{resultado_soles_vp:,.2f} Soles*
━━━━━━━━━━━━━━━━━━━━
🕐 {datetime.now().strftime('%H:%M:%S')} (Caracas)"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

        # --- FLUJO CON DÓLARES ($ o usd) ---
        elif '$' in texto_limpio or 'usd' in texto_limpio:
            monto_str = texto_limpio.replace('$', '').replace('usd', '').replace(',', '.').strip()
            monto_usd = float(monto_str)
            resultado_bs = monto_usd * bcv_mas_medio
            
            mensaje = f"""⚖️ *CALCULADORA DE CONVERSIÓN*

📊 *Tasa de Referencia:*
• BCV + 0.50%: *{bcv_mas_medio:.2f} Bs*

━━━━━━━━━━━━━━━━━━━━
✍️ *Operación ($ ➔ Bs):*
• Monto ingresado: *${monto_usd:,.2f} USD*
• Cálculo: Multiplicado por tasa BCV + 0.50%

🇻🇪 *Total equivalente:* *{resultado_bs:,.2f} Bs*
━━━━━━━━━━━━━━━━━━━━
🕐 {datetime.now().strftime('%H:%M:%S')} (Caracas)"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

        else:
            enviar_mensaje(chat_id, "⚠️ Por favor, especifica el tipo de moneda añadiendo *Bs*, *S/* o *$* al final de la cantidad (ejemplo: `100 S/`, `200000 Bs` o `100 $`).", crear_teclado_principal(chat_id))

    except ValueError:
        enviar_mensaje(chat_id, "❌ Error al leer la cantidad. Asegúrate de escribir solo números y añadir su identificador al final.", crear_teclado_principal(chat_id))

# ==================== HISTORIAL VES ====================

def mostrar_historial_ves(chat_id):
    analisis = obtener_analisis_ves()
    if not analisis:
        mensaje = "📈 *HISTORIAL DE BRECHA VES*\n⏳ Sin datos suficientes aún\n\nEspera al menos 2 minutos después de iniciar el bot."
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))
        return

    mensaje = f"📈 *HISTORIAL DE BRECHA VES (24h)*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n"
    mensaje += f"📅 {datetime.now().strftime('%d/%m/%Y')}\n\n"
    mensaje += f"📊 *Apertura:* {analisis['apertura']:.2f} Bs\n"
    mensaje += f"📊 *Actual:* {analisis['actual']:.2f} Bs\n"
    emoji = "📈" if analisis['cambio'] > 0 else "📉" if analisis['cambio'] < 0 else "➡️"
    mensaje += f"{emoji} *Cambio:* {analisis['cambio']:+.2f} Bs ({analisis['cambio_porcentaje']:+.1f}%)\n"
    mensaje += f"📈 *Máximo:* {analisis['maximo']:.2f} Bs\n"
    mensaje += f"📉 *Mínimo:* {analisis['minimo']:.2f} Bs\n"
    mensaje += f"🧭 *Tendencia:* {analisis['tendencia']}\n"
    mensaje += f"📊 *Muestras:* {analisis['muestras']}\n"

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== PROCESAR MENSAJES ====================

def procesar_mensaje(chat_id, texto):
    global usuario_esperando_calculo, usuario_configurando_soles
    global TASA_SOLES_TARIFARIO
    
    if not usuario_esta_en_grupo(chat_id):
        mensaje_bloqueo = (
            "❌ *Acceso Denegado*\n\n"
            "Este bot es privado y exclusivo para miembros de nuestra comunidad.\n\n"
            "⚠️ Para poder usarlo, debes pertenecer a nuestro grupo oficial. "
            "Una vez dentro del grupo, vuelve aquí y presiona /start."
        )
        enviar_mensaje(chat_id, mensaje_bloqueo)
        return

    print(f"📩 {texto}")
    guardar_usuario(chat_id)

    # CAPTURA DE TASA SOLES: Única y exclusivamente si el administrador está en el estado de configuración
    if chat_id == ADMIN_ID and usuario_configurando_soles.get(chat_id):
        try:
            monto_limpio = texto.replace(',', '.')
            TASA_SOLES_TARIFARIO = float(monto_limpio)
            usuario_configurando_soles[chat_id] = False  
            enviar_mensaje(chat_id, f"✅ *Tasa Soles configurada con éxito:* {TASA_SOLES_TARIFARIO:.2f}\n\nLos tarifarios ya están usando este nuevo valor.", crear_teclado_principal(chat_id))
            return
        except ValueError:
            pass  

    # Identificación rápida de conversiones directas con sufijos de moneda
    if ('bs' in texto.lower() or '$' in texto or 'usd' in texto.lower() or 's/' in texto.lower() or 'soles' in texto.lower()) and any(char.isdigit() for char in texto):
        calcular_conversion_bcv_medio(chat_id, texto)
        if chat_id in usuario_esperando_calculo:
            del usuario_esperando_calculo[chat_id]
        return

    if texto == '/start':
        usuario_configurando_soles[chat_id] = False
        mensaje = """
Bienvenido a TetherPrueba

Soy tu asistente diseñado para facilitarte la información sobre las tasas del momento de VES, COP y PEN del P2P de Binance.

Herramientas disponibles en los menús para consultas rápidas.
"""
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

    elif texto == 'Tether + BCV' or texto == '/tether':
        usuario_configurando_soles[chat_id] = False
        mostrar_tether_vs_bcv(chat_id)

    elif texto == '📋 Tarifario USD':
        usuario_configurando_soles[chat_id] = False
        mostrar_tarifario_usd(chat_id)

    elif texto == '📋 Tarifario Soles':
        usuario_configurando_soles[chat_id] = False
        mostrar_tarifario_soles(chat_id)

    elif texto == '⚙️ Ajustar Tasas' and chat_id == ADMIN_ID:
        usuario_configurando_soles[chat_id] = True
        tasa_bcv = obtener_tasa_bcv_actual()
        mensaje = f"⚙️ *PANEL DE CONFIGURACIÓN DE TARIFARIOS*\n\n" \
                  f"Tasa BCV del Día (Automática API): *{tasa_bcv:.2f} Bs*\n" \
                  f"Tasa Soles Actual: *{TASA_SOLES_TARIFARIO:.2f}*\n\n" \
                  f"💡 *Ajuste Automático:* Envía directamente el número en el siguiente mensaje para reconfigurar la Tasa Soles (Ejemplo: `3.85` o `200`)."
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

    elif texto == '¿Cuánto es?':
        usuario_configurando_soles[chat_id] = False
        usuario_esperando_calculo[chat_id] = True
        mensaje = "✍️ *Calculadora de Conversión Dinámica Avanzada*\n\nEscribe directamente la cantidad y su moneda al final para realizar las operaciones automáticas de Tasas Cruzadas y BCV.\n\nEjemplos:\n• `100 S/` o `100 Soles` (Calcula Soles a Bs y su equivalente en $)\n• `25000 Bs` (Desglosa cálculos en Soles a Tasa P→V, V→P y dólares)\n• `100 $` (Multiplicará estándar por BCV + 0.50%)"
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

    elif texto == '¿Cuánto Gané?' or texto == '/cuantogane':
        usuario_configurando_soles[chat_id] = False
        mensaje = "✍️ *Calculadora de Ganancias Inteligente*\n\nPor favor, escribe directamente en el chat el monto en *USD* que deseas calcular (ejemplo: `50` o `150.50`) y te daré el desglose de tu ganancia al instante."
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

    elif texto == '📈 Historial de brecha VES' or texto == '/historial':
        usuario_configurando_soles[chat_id] = False
        mostrar_historial_ves(chat_id)

    elif texto == '🏦 Tasas de Cambio' or texto == '/tasas':
        usuario_configurando_soles[chat_id] = False
        if chat_id == ADMIN_ID:
            mostrar_tasas_cambio(chat_id)
        else:
            enviar_mensaje(chat_id, "❌ Solo el administrador puede usar este comando", crear_teclado_principal(chat_id))

    elif texto == '+ Opciones':
        usuario_configurando_soles[chat_id] = False
        mensaje = "📋 *OPCIONES SECUNDARIAS*\n\nSelecciona una opción del menú:"
        enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

    elif texto == 'Volver al menú principal':
        usuario_configurando_soles[chat_id] = False
        mensaje = "🏠 *Volviendo al menú principal*"
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

    elif texto == 'Precio USDT':
        mostrar_precios_usdt(chat_id)

    elif texto == 'Precio VES' or texto == '/ves':
        mostrar_precio_individual(chat_id, 'VES')

    elif texto == 'Precio COP' or texto == '/cop':
        mostrar_precio_individual(chat_id, 'COP')

    elif texto == 'Precio PEN' or texto == '/pen':
        mostrar_precio_individual(chat_id, 'PEN')

    elif texto == 'Usuarios Registrados' or texto == '/usuarios':
        if chat_id == ADMIN_ID:
            usuarios = obtener_usuarios()
            if usuarios:
                mensaje = f"👥 *SISTEMA AUTOMÁTICO ACTIVO*\n\nTotal interactuando: {len(usuarios)}\n\nEl bot verifica accesos en tiempo real mediante Rose."
                for uid in usuarios:
                    mensaje += f"\n• `{uid}`"
            else:
                mensaje = "📝 No hay usuarios registrados"
            enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))
        else:
            enviar_mensaje(chat_id, "❌ Solo el administrador puede ver esto", crear_teclado_opciones(chat_id))

    else:
        # CÁLCULO DE GANANCIAS POR DEFECTO
        try:
            monto_limpio = texto.replace(',', '.')
            monto_usuario = float(monto_limpio)
            
            if monto_usuario > 0:
                calcular_ganancia_neta(chat_id, monto_usuario)
            else:
                enviar_mensaje(chat_id, "⚠️ El monto debe ser un número mayor a cero.", crear_teclado_principal(chat_id))
        except ValueError:
            enviar_mensaje(chat_id, "Usa /start o selecciona una opción de los menús.", crear_teclado_principal(chat_id))

# ==================== POLLING ====================

def recibir_mensajes():
    global ultimo_update_id
    print("🔄 Polling iniciado...")
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

        except Exception as e:
            print(f"❌ Error polling: {e}")
            time.sleep(5)

# ==================== ACTUALIZACIÓN CONTINUA Y ALERTAS ====================

def actualizar_precios():
    global mantener_activo, cache_precios, cache_tiempo, ultimos_precios
    
    while True:
        try:
            print(f"\n🔄 Actualizando... {datetime.now().strftime('%H:%M:%S')}")

            precios = {}
            for moneda in ['VES', 'COP', 'PEN']:
                compra, venta = obtener_precios_p2p_reales(moneda)
                if compra and venta:
                    precios[moneda] = {'compra': compra, 'venta': venta}
                    cache_precios[moneda] = {'compra': compra, 'venta': venta}
                    cache_tiempo[moneda] = time.time()
                    if moneda == 'VES':
                        guardar_historial_ves(compra)

            if precios:
                verificar_alertas(precios)
                verificar_fluctuacion_tasas()

                print(f"  ✅ VES: {precios.get('VES', {}).get('compra', 0):.2f}")
                print(f"  📊 Historial VES: {len(historial_ves)} muestras")
            else:
                print("  ❌ No se obtuvieron precios")

            time.sleep(60)

        except Exception as e:
            print(f"  ❌ Error en bucle principal: {e}")
            time.sleep(60)

# ==================== MANTENER ACTIVO ====================

def mantener_activo():
    while True:
        try:
            url = "https://telegram-usdt-bot-vf5t.onrender.com/"
            requests.get(url, timeout=10)
            print(f"💓 Keep alive: {datetime.now().strftime('%H:%M:%S')}")
        except:
            pass
        time.sleep(300)

# ==================== FLASK ====================

@app.route('/')
def home():
    return f"✅ Bot activo 24/7\n🔒 Canal/Grupo Vinculado: {GRUPO_AUTORIZADO_ID}\n📊 {len(historial_ves)} muestras VES\n🕐 Hora: {datetime.now().strftime('%H:%M:%S')} (Caracas)"

# ==================== MAIN ====================

if __name__ == "__main__":
    print("🚀 Bot iniciando en Render...")
    print(f"✅ TOKEN: {'Configurado' if TOKEN else 'FALTANTE'}")
    print(f"✅ ADMIN_ID: {ADMIN_ID if ADMIN_ID else 'FALTANTE'}")
    print(f"🔒 ID GRUPO VINCULADO: {GRUPO_AUTORIZADO_ID}")
    print(f"🕐 Zona horaria: Caracas (UTC -4)")

    cargar_tasas_anteriores()

    print("\n📊 Probando conexión a Binance...")
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_p2p_reales(m)
        if compra and venta:
            print(f"  ✅ {m}: {compra:.2f} / {venta:.2f}")
            grid = compra
            ultimos_precios[m] = grid
            cache_precios[m] = {'compra': compra, 'venta': venta}
            cache_tiempo[m] = time.time()
            if m == 'VES':
                guardar_historial_ves(compra)
        else:
            print(f"  ❌ {m}: No disponible")

    print(f"\n📊 Historial VES inicial: {len(historial_ves)} muestras")

    threading.Thread(target=recibir_mensajes, daemon=True).start()
    threading.Thread(target=actualizar_precios, daemon=True).start()
    threading.Thread(target=mantener_activo, daemon=True).start()

    print("\n✅ Bot listo!")
    print("=" * 40)

    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
