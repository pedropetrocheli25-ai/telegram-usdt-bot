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
time.tzset()

TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not TOKEN or not ADMIN_ID:
    print("ERROR: TELEGRAM_TOKEN o ADMIN_ID no configurados")
    exit(1)

ADMIN_ID = int(ADMIN_ID)
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"
ultimo_update_id = 0

app = Flask(__name__)

# ==================== NOTIFICACIÓN 1: UMBRALES DE DINERO FIJO ====================
UMBRALES_FIJOS = {
    'VES': 1.0,    # 1.00 Bolívar neto
    'COP': 100.0,  # 100.00 Pesos Colombianos neto
    'PEN': 0.10    # 0.10 Soles Peruanos neto
}

FLUCTUACION_UMBRAL = 0.8
ultimos_precios = {'VES': None, 'COP': None, 'PEN': None}

# ==================== CONTROL DE ACCESO AUTOMÁTICO por GRUPO ====================
GRUPO_AUTORIZADO_ID = -5370892602  

def usuario_esta_en_grupo(user_id):
    if user_id == ADMIN_ID:
        return True
    try:
        url = URL_TELEGRAM + "getChatMember"
        params = {"chat_id": GRUPO_AUTORIZADO_ID, "user_id": user_id}
        response = requests.get(url, params=params, timeout=8)
        if response.status_code == 200:
            resultado = response.json()
            if resultado.get("ok"):
                status = resultado["result"].get("status")
                if status in ["creator", "administrator", "member"]:
                    return True
        return False
    except Exception as e:
        print(f"⚠️ Error al verificar miembro en Telegram: {e}")
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

# ==================== HISTORIAL (SOLO VES) ====================
historial_ves = deque(maxlen=1440)
precio_apertura_ves = None

# ==================== NOTIFICACIÓN 2: CONTROL DE SPAM POR VOLUMEN ====================
ultima_alerta_enviada = None         
ultimo_delta_notificado = None       # Guarda el Delta de Volumen anterior para el filtro del 5%
ultimo_registro_prediccion = None    

# ==================== HISTORIAL DE PREDICCIONES ====================
historial_predicciones = deque(maxlen=100)
estadisticas_predicciones = {
    'aciertos': 0,
    'fallos': 0,
    'total_predicciones': 0,
    'precision': 0,
    'ultima_prediccion': None
}

# ==================== FUNCIONES BASE DE TELEGRAM ====================

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

def crear_teclado_principal(chat_id):
    teclado = [["💰 Precio USDT"], ["🪙 Tether USDT vs BCV"], ["📈 Historial de brecha VES"]]
    if chat_id == ADMIN_ID:
        teclado.append(["🏦 Tasas de Cambio"])
    teclado.append(["📋 + Opciones"])
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_opciones(chat_id):
    teclado = [["🇻🇪 Precio VES"], ["🇨🇴 Precio COP"], ["🇵🇪 Precio PEN"]]
    if chat_id == ADMIN_ID:
        teclado.append(["👥 Usuarios Registrados"])
    teclado.append(["📊 Análisis Mercado"], ["📋 Historial Predicciones"], ["📈 Estadísticas"], ["🔙 Volver al menú principal"])
    return {"keyboard": teclado, "resize_keyboard": True}

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
            if r.status_code == 200 and r.json().get('data'):
                precios = [float(a['adv']['price']) for a in r.json()['data'] if 1 < float(a['adv']['price']) < 100000]
                if precios: compra = min(precios)
        except: pass

        data = {"asset": "USDT", "fiat": fiat, "tradeType": "BUY", "page": 1, "rows": 10, "payTypes": []}
        venta = None
        try:
            r = requests.post(url, json=data, headers=headers, timeout=10)
            if r.status_code == 200 and r.json().get('data'):
                precios = [float(a['adv']['price']) for a in r.json()['data'] if 1 < float(a['adv']['price']) < 100000]
                if precios: venta = max(precios)
        except: pass

        if compra is None or venta is None: return None, None
        if compra < venta: compra, venta = venta, compra
        return compra, venta
    except:
        return None, None

# ==================== TASAS CRUZADAS ====================

def calcular_tasas_cruzadas():
    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    compra_cop, venta_cop = obtener_precios_con_cache('COP')
    compra_pen, venta_pen = obtener_precios_con_cache('PEN')
    if not all([compra_ves, venta_ves, compra_cop, venta_cop, compra_pen, venta_pen]): return None

    tasas = {}
    tasas['Perú → Venezuela'] = (venta_ves / compra_pen) * 0.95
    tasas['Venezuela → Perú'] = tasas['Perú → Venezuela'] + 15
    tasas['Perú → Colombia'] = (1 / (compra_pen / venta_cop)) * 0.95 if compra_pen else 0
    tasas['Colombia → Perú'] = (compra_cop / venta_pen) * 1.06
    tasas['Colombia → Venezuela'] = (compra_cop / venta_ves) * 1.06
    tasas['Venezuela → Colombia'] = (1 / (compra_ves / venta_cop)) * 0.95 if compra_ves else 0
    tasas['Colombia → Brasil'] = (compra_cop / 5.10) * 1.06
    tasas['Venezuela → Brasil'] = (compra_ves / 5.10) * 1.05
    return tasas

def mostrar_tasas_cambio(chat_id):
    tasas = calcular_tasas_cruzadas()
    if not tasas:
        enviar_mensaje(chat_id, "❌ No se pudieron obtener los datos para calcular las tasas", crear_teclado_principal(chat_id))
        return
    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    compra_cop, venta_cop = obtener_precios_con_cache('COP')
    compra_pen, venta_pen = obtener_precios_con_cache('PEN')

    mensaje = f"🏦 *TASAS DE CAMBIO CRUZADAS*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"📊 *Precios de referencia:*\n  🇻🇪 VES: Compra {compra_ves:.2f}\n  🇨🇴 COP: Compra {compra_cop:.2f}\n  🇵🇪 PEN: Compra {compra_pen:.2f}\n\n"
    mensaje += f"🇵🇪 *PERÚ (PEN)*\n  → 🇻🇪 Venezuela: {tasas['Perú → Venezuela']:.2f} Bs\n  → 🇨🇴 Colombia: {tasas['Perú → Colombia']:.2f} COP\n\n"
    mensaje += f"🇨🇴 *COLOMBIA (COP)*\n  → 🇻🇪 Venezuela: {tasas['Colombia → Venezuela']:.2f} Bs\n  → 🇵🇪 Perú: {tasas['Colombia → Perú']:.2f} PEN\n\n"
    mensaje += f"🇻🇪 *VENEZUELA (VES)*\n  → 🇵🇪 Perú: {tasas['Venezuela → Perú']:.2f} PEN\n  → 🇨🇴 Colombia: {tasas['Venezuela → Colombia']:.2f} COP"
    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== ALERTA DE FLUCTUACIÓN ====================
ultimas_tasas_cruzadas = {}
TASAS_ANTERIORES_ARCHIVO = "tasas_anteriores.json"

def guardar_tasas_anteriores():
    try:
        with open(TASAS_ANTERIORES_ARCHIVO, 'w') as f: json.dump(ultimas_tasas_cruzadas, f)
    except: pass

def cargar_tasas_anteriores():
    global ultimas_tasas_cruzadas
    try:
        if os.path.exists(TASAS_ANTERIORES_ARCHIVO):
            with open(TASAS_ANTERIORES_ARCHIVO, 'r') as f: ultimas_tasas_cruzadas = json.load(f)
    except: pass

def verificar_fluctuacion_tasas():
    global ultimas_tasas_cruzadas
    tasas_actuales = calcular_tasas_cruzadas()
    if not tasas_actuales: return

    if not ultimas_tasas_cruzadas:
        ultimas_tasas_cruzadas = tasas_actuales.copy()
        guardar_tasas_anteriores()
        return

    mensaje = "⚠️ *ALERTA DE FLUCTUACIÓN DE TASAS* ⚠️\n\n"
    hubo_fluctuacion = False
    for clave, valor_actual in tasas_actuales.items():
        if clave in ultimas_tasas_cruzadas:
            valor_anterior = ultimas_tasas_cruzadas[clave]
            if valor_anterior > 0:
                fluctuacion = abs((valor_actual - valor_anterior) / valor_anterior) * 100
                if fluctuacion >= FLUCTUACION_UMBRAL:
                    direccion = "📈 SUBIÓ" if valor_actual > valor_anterior else "📉 BAJÓ"
                    mensaje += f"• *{clave}*: {direccion} en {fluctuacion:.2f}%\n  Anterior: {valor_anterior:.4f} → Actual: {valor_actual:.4f}\n\n"
                    hubo_fluctuacion = True

    if hubo_fluctuacion:
        for usuario in obtener_usuarios():
            try: enviar_mensaje(usuario, mensaje); time.sleep(0.05)
            except: pass
    ultimas_tasas_cruzadas = tasas_actuales.copy()
    guardar_tasas_anteriores()

# ==================== HISTORIAL ====================
def guardar_historial_ves(precio):
    global precio_apertura_ves
    historial_ves.append(precio)
    if precio_apertura_ves is None: precio_apertura_ves = precio

def obtener_analisis_ves():
    if not historial_ves or len(historial_ves) < 2: return None
    precios = list(historial_ves)
    cambio = precios[-1] - precios[0]
    return {
        'actual': precios[-1], 'apertura': precios[0], 'cambio': cambio,
        'cambio_porcentaje': (cambio / precios[0]) * 100 if precios[0] != 0 else 0,
        'maximo': max(precios), 'minimo': min(precios),
        'tendencia': "↗️ Alcista" if precios[-1] > precios[-10] else "↘️ Bajista", 'muestras': len(precios)
    }

# ==================== ANÁLISIS CUANTITATIVO DE VOLUMEN (P2P ORDER FLOW) ====================

def analizar_tendencia_mercado(moneda='VES'):
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

        vol_demanda = 0.0
        precio_compra_ref = 0.0
        data_sell = {"asset": "USDT", "fiat": moneda, "tradeType": "SELL", "page": 1, "rows": 15, "payTypes": []}
        r_sell = requests.post(url, json=data_sell, headers=headers, timeout=10)
        if r_sell.status_code == 200 and r_sell.json().get('data'):
            anuncios = r_sell.json()['data']
            precio_compra_ref = float(anuncios[0]['adv']['price'])
            vol_demanda = sum(float(adv['adv']['surplusAmount']) * float(adv['adv']['price']) for adv in anuncios)

        vol_oferta = 0.0
        data_buy = {"asset": "USDT", "fiat": moneda, "tradeType": "BUY", "page": 1, "rows": 15, "payTypes": []}
        r_buy = requests.post(url, json=data_buy, headers=headers, timeout=10)
        if r_buy.status_code == 200 and r_buy.json().get('data'):
            vol_oferta = sum(float(adv['adv']['surplusAmount']) * float(adv['adv']['price']) for adv in r_buy.json()['data'])

        if vol_demanda == 0 or vol_oferta == 0: return None, "⚠️ API sin data de volumen."

        vol_total = vol_demanda + vol_oferta
        fuerza_mercado = ((vol_demanda - vol_oferta) / vol_total) * 100  

        if fuerza_mercado >= 12.0:
            tendencia, emoji, puntaje = "🚀 ALCISTA INMINENTE", "🟢", 7
            prediccion = "📈 Alta demanda absorbiendo liquidez. El precio tiende a SUBIR."
            recomendacion = "💰 COMPRA - Volumen presionando el libro"
        elif fuerza_mercado <= -12.0:
            tendencia, emoji, puntaje = "🔻 BAJISTA INMINENTE", "🔴", -7
            prediccion = "📉 Exceso de oferta inundando los anuncios. El precio tiende a BAJAR."
            recomendacion = "⚠️ VENDE - Acumulación agresiva en muros"
        else:
            tendencia, emoji, puntaje = "➡️ NEUTRAL / ESTABLE", "🟡", 0
            prediccion = "⏳ Oferta y demanda equilibradas en el P2P."
            recomendacion = "⏳ ESPERA - Rango lateral"

        resultado = {
            'precio_actual': precio_compra_ref if precio_compra_ref > 0 else (historial_ves[-1] if historial_ves else 0.0),
            'cambio_10min': fuerza_mercado, 'puntaje': puntaje, 'tendencia': tendencia, 'emoji': emoji,
            'prediccion': prediccion, 'confianza': "Alta" if puntaje != 0 else "Media", 'recomendacion': recommendation,
            'momentum': (vol_demanda - vol_oferta) / 1000000, 'rsi': 50 + (fuerza_mercado / 2)
        }
        return resultado, None
    except Exception as e:
        return None, f"❌ Error en análisis: {str(e)}"

# ==================== SISTEMA DE PREDICCIONES ====================

def guardar_prediccion(analisis):
    global historial_predicciones, estadisticas_predicciones
    prediccion = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'precio_actual': analisis['precio_actual'],
        'puntaje': analisis['puntaje'], 'tendencia': analisis['tendencia'], 'prediccion': analisis['prediccion'],
        'verificada': False, 'acertada': False, 'precio_verificacion': None
    }
    historial_predicciones.append(prediccion)
    estadisticas_predicciones['ultima_prediccion'] = prediccion
    estadisticas_predicciones['total_predicciones'] += 1

def verificar_predicciones():
    global historial_predicciones, estadisticas_predicciones
    if len(historial_predicciones) < 2: return
    compra_ves, _ = obtener_precios_con_cache('VES')
    if not compra_ves: return

    for p in historial_predicciones:
        if not p['verificada']:
            minutos = (datetime.now() - datetime.strptime(p['timestamp'], '%Y-%m-%d %H:%M:%S')).total_seconds() / 60
            if minutos >= 30:
                cambio_real = ((compra_ves - p['precio_actual']) / p['precio_actual']) * 100
                acertada = ('ALCISTA' in p['tendencia'] and cambio_real > 0.3) or ('BAJISTA' in p['tendencia'] and cambio_real < -0.3) or ('NEUTRAL' in p['tendencia'] and abs(cambio_real) <= 0.3)
                p.update({'verificada': True, 'acertada': acertada, 'precio_verificacion': compra_ves, 'cambio_real': cambio_real})
                if acertada: estadisticas_predicciones['aciertos'] += 1
                else: estadisticas_predicciones['fallos'] += 1
    total = estadisticas_predicciones['aciertos'] + estadisticas_predicciones['fallos']
    if total > 0: estadisticas_predicciones['precision'] = (estadisticas_predicciones['aciertos'] / total) * 100

def obtener_estadisticas_precision():
    global estadisticas_predicciones, historial_predicciones
    return {
        'total_predicciones': estadisticas_predicciones['total_predicciones'],
        'verificadas': estadisticas_predicciones['aciertos'] + estadisticas_predicciones['fallos'],
        'aciertos': estadisticas_predicciones['aciertos'], 'fallos': estadisticas_predicciones['fallos'],
        'precision_general': estadisticas_predicciones['precision'], 'ultimas': list(historial_predicciones)[-10:]
    }

# ==================== SECCIÓN CORREGIDA: INDEPENDIENTE (UMBRALES FIJOS) ====================

def verificar_alertas(precios):
    global ultimos_precios
    if not precios: return
    usuarios = obtener_usuarios()
    if not usuarios: return

    for moneda in ['VES', 'COP', 'PEN']:
        if moneda not in precios or not precios[moneda]: continue
        precio_actual = precios[moneda]['compra']

        if ultimos_precios[moneda] is None:
            ultimos_precios[moneda] = precio_actual
            continue

        cambio_absoluto = precio_actual - ultimos_precios[moneda]
        
        # Validación de umbral exacto de dinero
        if abs(cambio_absoluto) >= UMBRALES_FIJOS[moneda]:
            direccion = "📈 SUBIÓ" if cambio_absoluto > 0 else "📉 BAJÓ"
            emoji = "🟢" if cambio_absoluto > 0 else "🔴"
            signo = "+" if cambio_absoluto > 0 else ""
            cambio_porcentaje = (cambio_absoluto / ultimos_precios[moneda] * 100)

            mensaje = f"""
{emoji} *🔔 ALERTA DE PRECIO: {moneda}* {emoji}

El precio del mercado ha cambiado un valor neto de: *{signo}{cambio_absoluto:.2f} {moneda}* ({direccion})

📊 *Detalles:*
• Anterior: {ultimos_precios[moneda]:.2f}
• Actual: {precio_actual:.2f}
• Variación Porcentual: {signo}{cambio_porcentaje:.2f}%

🕐 {datetime.now().strftime('%H:%M:%S')}
"""
            for usuario in usuarios:
                try: enviar_mensaje(usuario, mensaje); time.sleep(0.04)
                except: pass

            ultimos_precios[moneda] = precio_actual
            print(f"🔔 Alerta por Umbral Fijo enviada para {moneda}.")

# ==================== MOSTRAR MENÚS DE PRECIOS ====================

def mostrar_precios_usdt(chat_id):
    precios = {m: {'compra': c, 'venta': v} for m in ['VES', 'COP', 'PEN'] for c, v in [obtener_precios_con_cache(m)] if c}
    if not precios: return
    mensaje = f"💰 *PRECIOS USDT P2P*\n\n"
    for m, d in precios.items():
        mensaje += f"*{m}*\n  🟢 COMPRA: {d['compra']:.2f}\n  🔴 VENTA: {d['venta']:.2f}\n  📊 Spread: {d['compra']-d['venta']:.2f}\n\n"
    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

def mostrar_precio_individual(chat_id, moneda):
    c, v = obtener_precios_con_cache(moneda)
    if c:
        enviar_mensaje(chat_id, f"💰 *PRECIO {moneda}*\n\n🟢 COMPRA: {c:.2f}\n🔴 VENTA: {v:.2f}", crear_teclado_opciones(chat_id))

def mostrar_tether_vs_bcv(chat_id):
    compra, _ = obtener_precios_con_cache('VES')
    tasas = obtener_tasas_bcv()
    if compra and tasas:
        bcv_05 = tasas['usd'] * 1.005
        enviar_mensaje(chat_id, f"🪙 *USDT vs BCV (+0.50%)*\n\n🏦 BCV: {tasas['usd']:.2f}\n📈 BCV+0.50%: {bcv_05:.2f}\n🟢 P2P: {compra:.2f}", crear_teclado_principal(chat_id))

def obtener_tasas_bcv():
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10)
        if r.status_code == 200: return {'usd': r.json().get('rates', {}).get('VES', 0)}
    except: pass
    return None

def mostrar_historial_ves(chat_id):
    analisis = obtener_analisis_ves()
    if analisis:
        enviar_mensaje(chat_id, f"📈 *BRECHA VES*\n\n📊 Actual: {analisis['actual']:.2f}\n📊 Apertura: {analisis['apertura']:.2f}\n🧭 Tendencia: {analisis['tendencia']}", crear_teclado_principal(chat_id))

# ==================== PROCESAR ENTRADAS TELEGRAM ====================

def procesar_mensaje(chat_id, texto):
    if not usuario_esta_en_grupo(chat_id):
        enviar_mensaje(chat_id, "❌ *Acceso Denegado*\nEste bot es exclusivo de nuestra comunidad.")
        return
    guardar_usuario(chat_id)
    if texto == '/start': enviar_mensaje(chat_id, "Bienvenido a TetherPrueba Bot", crear_teclado_principal(chat_id))
    elif texto == '💰 Precio USDT': mostrar_precios_usdt(chat_id)
    elif texto == '🪙 Tether USDT vs BCV': mostrar_tether_vs_bcv(chat_id)
    elif texto == '📈 Historial de brecha VES': mostrar_historial_ves(chat_id)
    elif texto == '📋 + Opciones': enviar_mensaje(chat_id, "Opciones avanzadas:", crear_teclado_opciones(chat_id))
    elif texto == '🔙 Volver al menú principal': enviar_mensaje(chat_id, "Menú principal", crear_teclado_principal(chat_id))
    elif texto == '🇻🇪 Precio VES': mostrar_precio_individual(chat_id, 'VES')
    elif texto == '🇨🇴 Precio COP': mostrar_precio_individual(chat_id, 'COP')
    elif texto == '🇵🇪 Precio PEN': mostrar_precio_individual(chat_id, 'PEN')
    elif texto == '📊 Análisis Mercado': mostrar_analisis_mercado(chat_id)
    elif texto == '📋 Historial Predicciones': mostrar_historial_predicciones(chat_id)
    elif texto == '📈 Estadísticas': mostrar_estadisticas_detalladas(chat_id)

def mostrar_analisis_mercado(chat_id):
    analisis, err = analizar_tendencia_mercado('VES')
    if analisis:
        enviar_mensaje(chat_id, f"📊 *ORDER FLOW P2P*\n\nTendencia: {analisis['tendencia']}\nDelta: {analisis['cambio_10min']:+.2f}%", crear_teclado_opciones(chat_id))

def mostrar_historial_predicciones(chat_id):
    stats = obtener_estadisticas_precision()
    enviar_mensaje(chat_id, f"📊 *PREDICCIONES*\n\nGeneral: {stats['precision_general']:.1f}%", crear_teclado_opciones(chat_id))

def mostrar_estadisticas_detalladas(chat_id):
    stats = obtener_estadisticas_precision()
    enviar_mensaje(chat_id, f"📈 *MÉTRICAS*\n\nAciertos: {stats['aciertos']} | Fallos: {stats['fallos']}", crear_teclado_opciones(chat_id))

# ==================== POLLING ====================

def recibir_mensajes():
    global ultimo_update_id
    while True:
        try:
            r = requests.get(URL_TELEGRAM + "getUpdates", params={'offset': ultimo_update_id + 1, 'timeout': 30}, timeout=35)
            if r.status_code == 200 and r.json().get('ok'):
                for u in r.json().get('result', []):
                    ultimo_update_id = u.get('update_id', 0)
                    msg = u.get('message')
                    if msg and msg.get('chat', {}).get('id') and msg.get('text', ''):
                        threading.Thread(target=procesar_mensaje, args=(msg['chat']['id'], msg['text'])).start()
            time.sleep(1)
        except: time.sleep(5)

# ==================== SECCIÓN CORREGIDA: INDEPENDIENTE (FILTRO DELTA 5%) ====================

def actualizar_precios():
    global cache_precios, cache_tiempo, ultima_alerta_enviada, ultimo_delta_notificado
    while True:
        try:
            precios = {}
            for moneda in ['VES', 'COP', 'PEN']:
                compra, venta = obtener_precios_p2p_reales(moneda)
                if compra and venta:
                    precios[moneda] = {'compra': compra, 'venta': venta}
                    cache_precios[moneda] = {'compra': compra, 'venta': venta}
                    cache_tiempo[moneda] = time.time()
                    if moneda == 'VES': guardar_historial_ves(compra)

            if precios:
                # Alertas por Umbrales fijos (1 VES, 100 COP, 0.10 PEN)
                verificar_alertas(precios)
                verificar_fluctuacion_tasas()

                # Alertas Cuantitativas de Volumen con filtro estricto del 5%
                analisis, err = analizar_tendencia_mercado('VES')
                if analisis and not err:
                    delta_actual = analisis['cambio_10min']
                    guardar_prediccion(analisis)

                    if analisis['puntaje'] in [7, -7]:
                        debe_notificar = False
                        if ultimo_delta_notificado is None:
                            debe_notificar = True
                        else:
                            # FILTRO DEL 5% EN LA VARIACIÓN DEL DELTA DE VOLUMEN
                            if abs(delta_actual - ultimo_delta_notificado) >= 5.0:
                                debe_notificar = True

                        if debe_notificar:
                            msg_alerta = f"🚨 *ALERTA DE ANÁLISIS DE MERCADO (Order Flow)* 🚨\n\n" \
                                         f"🧭 *Dirección Proyectada:* {analisis['tendencia']}\n" \
                                         f"📊 *Fuerza del Delta (Volumen):* {delta_actual:+.1f}%\n" \
                                         f"💡 *Predicción:* {analisis['prediccion']}\n" \
                                         f"💵 *Precio Ref:* {analisis['precio_actual']:.2f} Bs\n" \
                                         f"🕐 {datetime.now().strftime('%H:%M:%S')}"

                            for usr in obtener_usuarios():
                                try: enviar_mensaje(usr, msg_alerta); time.sleep(0.04)
                                except: pass
                            
                            ultima_alerta_enviada = datetime.now()
                            ultimo_delta_notificado = delta_actual
                            print(f"📢 Alerta de volumen enviada. Delta actual: {delta_actual:.2f}%")
                    verificar_predicciones()
            time.sleep(60)
        except Exception as e:
            print(f"❌ Error bucle: {e}"); time.sleep(60)

def mantener_activo():
    while True:
        try: requests.get(f"https://{os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'localhost')}/", timeout=10)
        except: pass
        time.sleep(300)

@app.route('/')
def home():
    return f"✅ Bot activo\n📊 {len(historial_ves)} muestras VES\n🕐 Hora: {datetime.now().strftime('%H:%M:%S')} (Caracas)"

# ==================== PRODUCCIÓN GITHUB / RAILWAY ====================

if __name__ == "__main__":
    cargar_tasas_anteriores()
    
    # Inicialización del entorno
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_p2p_reales(m)
        if compra:
            ultimos_precios[m] = compra
            cache_precios[m] = {'compra': compra, 'venta': venta}
            cache_tiempo[m] = time.time()

    threading.Thread(target=recibir_mensajes, daemon=True).start()
    threading.Thread(target=actualizar_precios, daemon=True).start()
    threading.Thread(target=mantener_activo, daemon=True).start()

    # Configuración del puerto dinámico para despliegues en la nube (Railway/Heroku)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
