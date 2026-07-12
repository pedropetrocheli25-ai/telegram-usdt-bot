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

# ==================== ALERTAS DE PRECIO FINANCIERO ====================
UMBRALES = {
    'VES': 1.0,
    'COP': 50.0,
    'PEN': 0.10
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

# ==================== CONTROL DE SPAM MEMORIA INTELIGENTE ====================
ultima_alerta_enviada = None         
ultimo_registro_prediccion = None    
ultimo_porcentaje_alertado = 0.0     
ultima_tendencia_alertada = ""       

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
    teclado = [
        ["💰 Precio USDT"],
        ["🪙 Tether USDT vs BCV"],
        ["📈 Historial de brecha VES"]
    ]
    if chat_id == ADMIN_ID:
        teclado.append(["🏦 Tasas de Cambio"])
    teclado.append(["📋 + Opciones"])
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_opciones(chat_id):
    teclado = [
        ["🇻🇪 Precio VES"],
        ["🇨🇴 Precio COP"],
        ["🇵🇪 Precio PEN"]
    ]
    if chat_id == ADMIN_ID:
        teclado.append(["👥 Usuarios Registrados"])
    teclado.append(["📊 Análisis Mercado"])
    teclado.append(["📋 Historial Predicciones"])
    teclado.append(["📈 Estadísticas"])
    teclado.append(["🔙 Volver al menú principal"])
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

# ==================== TASAS CRUZADAS ====================

def calcular_tasas_cruzadas():
    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    compra_cop, venta_cop = obtener_precios_con_cache('COP')
    compra_pen, venta_pen = obtener_precios_con_cache('PEN')

    if not all([compra_ves, venta_ves, compra_cop, venta_cop, compra_pen, venta_pen]):
        return None

    tasas = {}
    tasas['Perú → Venezuela'] = (venta_ves / compra_pen) * 0.95
    tasas['Venezuela → Perú'] = tasas['Perú → Venezuela'] + 15
    if compra_pen and venta_cop:
        tasas['Perú → Colombia'] = (1 / (compra_pen / venta_cop)) * 0.95
    else:
        tasas['Perú → Colombia'] = 0
    tasas['Colombia → Perú'] = (compra_cop / venta_pen) * 1.06

    tasas['Colombia → Venezuela'] = (compra_cop / venta_ves) * 1.06
    if compra_ves and venta_cop:
        tasas['Venezuela → Colombia'] = (1 / (compra_ves / venta_cop)) * 0.95
    else:
        tasas['Venezuela → Colombia'] = 0
    tasas['Colombia → Brasil'] = (compra_cop / 5.10) * 1.06
    tasas['Venezuela → Brasil'] = (compra_ves / 5.10) * 1.05

    return tasas

def mostrar_tasas_cambio(chat_id):
    tasas = calcular_tasas_cruzadas()
    if not tasas:
        enviar_mensaje(chat_id, "❌ No se pudieron obtener los datos", crear_teclado_principal(chat_id))
        return

    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    compra_cop, venta_cop = obtener_precios_con_cache('COP')
    compra_pen, venta_pen = obtener_precios_con_cache('PEN')

    mensaje = f"🏦 *TASAS DE CAMBIO CRUZADAS*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"🇵🇪 *PERÚ (PEN)*\n  → 🇻🇪 Venezuela: {tasas['Perú → Venezuela']:.2f} Bs\n  → 🇨🇴 Colombia: {tasas['Perú → Colombia']:.2f} COP\n\n"
    mensaje += f"🇨🇴 *COLOMBIA (COP)*\n  → 🇻🇪 Venezuela: {tasas['Colombia → Venezuela']:.2f} Bs\n  → 🇵🇪 Perú: {tasas['Colombia → Perú']:.2f} PEN\n\n"
    mensaje += f"🇻🇪 *VENEZUELA (VES)*\n  → 🇵🇪 Perú: {tasas['Venezuela → Perú']:.2f} PEN\n  → 🇨🇴 Colombia: {tasas['Venezuela → Colombia']:.2f} COP"

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

ultimas_tasas_cruzadas = {}
def verificar_fluctuacion_tasas():
    global ultimas_tasas_cruzadas
    tasas_actuales = calcular_tasas_cruzadas()
    if not tasas_actuales:
        return
    if not ultimas_tasas_cruzadas:
        grid = tasas_actuales.copy()
        ultimas_tasas_cruzadas = grid
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
                    mensaje += f"• *{clave}*: {direccion} en {fluctuacion:.2f}%\n"
                    hubo_fluctuacion = True

    if hubo_fluctuacion:
        for usuario in obtener_usuarios():
            enviar_mensaje(usuario, mensaje)
    ultimas_tasas_cruzadas = tasas_actuales.copy()

# ==================== HISTORIAL VES ====================

def guardar_historial_ves(precio):
    global precio_apertura_ves
    historial_ves.append(precio)
    if precio_apertura_ves is None:
        precio_apertura_ves = precio

def obtener_analisis_ves():
    if not historial_ves:
        return None
    precios = list(historial_ves)
    if len(precios) < 2:
        return None
    return {
        'actual': precios[-1],
        'apertura': precios[0],
        'cambio': precios[-1] - precios[0],
        'cambio_porcentaje': ((precios[-1] - precios[0]) / precios[0]) * 100,
        'maximo': max(precios),
        'minimo': min(precios),
        'tendencia': "↗️ Alcista" if precios[-1] > precios[-2] else "↘️ Bajista",
        'muestras': len(precios)
    }

# ==================== ANÁLISIS CUANTITATIVO DE VOLUMEN (UMBRAL AL 40%) ====================

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
            for adv in anuncios:
                vol_demanda += float(adv['adv']['surplusAmount']) * float(adv['adv']['price'])

        vol_oferta = 0.0
        data_buy = {"asset": "USDT", "fiat": moneda, "tradeType": "BUY", "page": 1, "rows": 15, "payTypes": []}

        r_buy = requests.post(url, json=data_buy, headers=headers, timeout=10)
        if r_buy.status_code == 200 and r_buy.json().get('data'):
            anuncios = r_buy.json()['data']
            for adv in anuncios:
                vol_oferta += float(adv['adv']['surplusAmount']) * float(adv['adv']['price'])

        if vol_demanda == 0 or vol_oferta == 0:
            return None, "⚠️ API sin data."

        vol_total = vol_demanda + vol_oferta
        fuerza_mercado = ((vol_demanda - vol_oferta) / vol_total) * 100  

        porcentaje_umbral_alcista = 40.0  
        porcentaje_umbral_bajista = -40.0

        if fuerza_mercado >= porcentaje_umbral_alcista:
            tendencia = "🚀 ALCISTA INMINENTE"
            emoji = "🟢"
            prediccion = "📈 Muros de venta cediendo. El precio tiende a SUBIR."
            puntaje = 7
        elif fuerza_mercado <= porcentaje_umbral_bajista:
            tendencia = "🔻 BAJISTA INMINENTE"
            emoji = "🔴"
            prediccion = "📉 Exceso de oferta agresiva. El precio tiende a BAJAR."
            puntaje = -7
        else:
            tendencia = "➡️ NEUTRAL / ESTABLE"
            emoji = "🟡"
            prediccion = "⏳ Mercado tranquilo. Oferta y demanda equilibradas."
            puntaje = 0

        resultado = {
            'precio_actual': precio_compra_ref if precio_compra_ref > 0 else (historial_ves[-1] if historial_ves else 0.0),
            'cambio_10min': fuerza_mercado,
            'puntaje': puntaje,
            'tendencia': tendencia,
            'emoji': emoji,
            'prediccion': prediccion
        }
        return resultado, None
    except Exception as e:
        return None, str(e)

# ==================== SISTEMA DE PREDICCIONES Y PRECISIÓN ====================

def guardar_prediccion(analisis):
    global historial_predicciones, estadisticas_predicciones
    prediccion = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'precio_actual': analisis['precio_actual'],
        'tendencia': analisis['tendencia'],
        'verificada': False,
        'acertada': False
    }
    historial_predicciones.append(prediccion)
    estadisticas_predicciones['total_predicciones'] += 1

def verificar_predicciones():
    pass

def obtener_estadisticas_precision():
    global estadisticas_predicciones
    return {
        'total_predicciones': estadisticas_predicciones['total_predicciones'],
        'verificadas': 0, 'aciertos': 0, 'fallos': 0, 'precision_general': 0.0,
        'precision_alcista': 0.0, 'precision_bajista': 0.0, 'precision_neutral': 0.0, 'ultimas': []
    }


# ==================== PROCESAR MENSAJES ====================

def procesar_mensaje(chat_id, texto):
    if not usuario_esta_en_grupo(chat_id):
        enviar_mensaje(chat_id, "❌ *Acceso Denegado.* Debes estar en el grupo oficial.")
        return

    guardar_usuario(chat_id)
    if texto == '/start':
        enviar_mensaje(chat_id, "Bienvenido a TetherPrueba", crear_teclado_principal(chat_id))
    elif texto == '💰 Precio USDT':
        mostrar_precios_usdt(chat_id)
    elif texto == '🪙 Tether USDT vs BCV':
        mostrar_tether_vs_bcv(chat_id)
    elif texto == '📈 Historial de brecha VES':
        mostrar_historial_ves(chat_id)
    elif texto == '📋 + Opciones':
        enviar_mensaje(chat_id, "📋 *Opciones:*", crear_teclado_opciones(chat_id))
    elif texto == '🔙 Volver al menú principal':
        enviar_mensaje(chat_id, "🏠 Menú principal", crear_teclado_principal(chat_id))
    elif texto == '🇻🇪 Precio VES':
        mostrar_precio_individual(chat_id, 'VES')
    elif texto == '🇨🇴 Precio COP':
        mostrar_precio_individual(chat_id, 'COP')
    elif texto == '🇵🇪 Precio PEN':
        mostrar_precio_individual(chat_id, 'PEN')

def mostrar_precios_usdt(chat_id):
    c, v = obtener_precios_con_cache('VES')
    if c:
        enviar_mensaje(chat_id, f"🇻🇪 *VES:* Compra {c:.2f} | Venta {v:.2f}", crear_teclado_principal(chat_id))

def mostrar_precio_individual(chat_id, moneda):
    c, v = obtener_precios_con_cache(moneda)
    if c:
        enviar_mensaje(chat_id, f"🪙 *{moneda}:* Compra {c:.2f} | Venta {v:.2f}", crear_teclado_opciones(chat_id))

def mostrar_tether_vs_bcv(chat_id):
    c, v = obtener_precios_con_cache('VES')
    enviar_mensaje(chat_id, f"📊 *P2P Compra:* {c:.2f} Bs", crear_teclado_principal(chat_id))

def mostrar_historial_ves(chat_id):
    enviar_mensaje(chat_id, "📈 Historial activo.", crear_teclado_principal(chat_id))

# ==================== POLLING Y CONTROL DE ALERTAS INTELIGENTE ====================

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
        except:
            time.sleep(5)

def verificar_alertas(precios):
    global ultimos_precios
    for m in ['VES']:
        if m in precios and precios[m]:
            precio_actual = precios[m]['compra']
            if ultimos_precios[m] is not None:
                cambio = abs(precio_actual - ultimos_precios[m])
                if cambio >= UMBRALES[m]:
                    direccion = "📈 SUBIÓ" if precio_actual > ultimos_precios[m] else "📉 BAJÓ"
                    for usr in obtener_usuarios():
                        enviar_mensaje(usr, f"🔔 *Notificación {m}:* {direccion} a {precio_actual:.2f}")
            ultimos_precios[m] = precio_actual

def actualizar_precios():
    global ultima_alerta_enviada, ultimo_registro_prediccion, cache_precios, cache_tiempo
    global ultimo_porcentaje_alertado, ultima_tendencia_alertada
    
    while True:
        try:
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

                analisis, err = analizar_tendencia_mercado('VES')
                if analisis and not err:
                    ahora = datetime.now()

                    if ultimo_registro_prediccion is None or (ahora - ultimo_registro_prediccion).total_seconds() >= 900:
                        guardar_prediccion(analisis)
                        ultimo_registro_prediccion = ahora

                    if analisis['puntaje'] == 7 or analisis['puntaje'] == -7:
                        diferencia_porcentaje = abs(analisis['cambio_10min'] - ultimo_porcentaje_alertado)
                        cambio_de_tendencia = (analisis['tendencia'] != ultima_tendencia_alertada)

                        # MODIFICADO A 5.0% PARA MÁXIMA REACTIVIDAD DE TRADING EN TIEMPO REAL
                        if cambio_de_tendencia or diferencia_porcentaje >= 5.0:
                            if ultima_alerta_enviada is None or (ahora - ultima_alerta_enviada).total_seconds() >= 900:
                                
                                msg_alerta = f"🚨 *ALERTA DE VOLUMEN P2P CRÍTICA* 🚨\n\n"
                                msg_alerta += f"🧭 *Dirección Proyectada:* {analisis['tendencia']}\n"
                                msg_alerta += f"📊 *Desequilibrio de Órdenes:* {analisis['cambio_10min']:+.1f}%\n"
                                msg_alerta += f"💡 *Acción:* {analisis['prediccion']}\n"
                                msg_alerta += f"🕐 {datetime.now().strftime('%H:%M:%S')}"

                                for usr in obtener_usuarios():
                                    try:
                                        enviar_mensaje(usr, msg_alerta)
                                        time.sleep(0.04)
                                    except:
                                        pass
                                
                                ultima_alerta_enviada = ahora
                                ultimo_porcentaje_alertado = analisis['cambio_10min']
                                ultima_tendencia_alertada = analisis['tendencia']
                                print("🔔 Alerta enviada por cambio importante del 5% o más.")
                        else:
                            print("⏳ Alerta omitida: El cambio es menor al 5.0% fijado.")

            time.sleep(60)
        except Exception as e:
            time.sleep(60)

def mantener_activo():
    while True:
        try:
            requests.get(f"https://{os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'localhost')}/", timeout=10)
        except:
            pass
        time.sleep(300)

@app.route('/')
def home():
    return "✅ Bot Online"

if __name__ == "__main__":
    threading.Thread(target=recibir_mensajes, daemon=True).start()
    threading.Thread(target=actualizar_precios, daemon=True).start()
    threading.Thread(target=mantener_activo, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)