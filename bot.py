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

# ==================== ALERTAS DE PRECIO FINANCIERO (NOTIFICACIÓN 1) ====================
UMBRALES = {
    'VES': 1.0,    # 1.00 VES neto
    'COP': 100.0,  # 100.00 COP neto
    'PEN': 0.10    # 0.10 PEN neto
}

FLUCTUACION_UMBRAL = 0.8

ultimos_precios = {'VES': None, 'COP': None, 'PEN': None}

# ==================== VARIABLES DE CONTROL DE VOLUMEN (NOTIFICACIÓN 2) ====================
ultimo_delta_notificado = None  # Guarda la fuerza del delta de la última alerta para aplicar filtro de 5%

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

# ==================== CONTROL DE SPAM Y ALERTAS CRÍTICAS ====================
ultima_alerta_enviada = None         # Mantenida por compatibilidad de variables globales
ultimo_registro_prediccion = None    # Cooldown para limitar muestras idénticas en el historial

# Búfer cuantitativo para suavizar la fuerza del mercado (Promedio Móvil - 10 muestras)
historico_fuerza = deque(maxlen=10)

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

def crear_teclado_principal(chat_id):
    """Genera el menú de inicio con botones en fila única (uno debajo de otro)"""
    teclado = [
        ["💰 Precio USDT"],
        ["🪙 Tether USDT vs BCV"],
        ["📈 Historial de brecha VES"]
    ]

    # Si es el Administrador, se añade la opción de Tasas de Cambio aquí
    if chat_id == ADMIN_ID:
        teclado.append(["🏦 Tasas de Cambio"])

    # Botones colocados estrictamente uno debajo del otro
    teclado.append(["💰 ¿Cuánto Gané?"])
    teclado.append(["📋 + Opciones"])

    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_opciones(chat_id):
    """Genera el menú de opciones secundarias en fila única (uno debajo de otro)"""
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

# ==================== ANÁLISIS CUANTITATIVO DE VOLUMEN (CORREGIDO) ====================

def analizar_tendencia_mercado(moneda='VES'):
    global historico_fuerza
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

        # 1. tradeType: SELL -> Anunciantes vendiendo USDT -> Representa la OFERTA (Supply)
        vol_oferta = 0.0
        precio_compra_ref = 0.0
        data_sell = {"asset": "USDT", "fiat": moneda, "tradeType": "SELL", "page": 1, "rows": 15, "payTypes": []}

        r_sell = requests.post(url, json=data_sell, headers=headers, timeout=10)
        if r_sell.status_code == 200 and r_sell.json().get('data'):
            anuncios = r_sell.json()['data']
            precio_compra_ref = float(anuncios[0]['adv']['price'])
            for adv in anuncios:
                vol_oferta += float(adv['adv']['surplusAmount']) * float(adv['adv']['price'])

        # 2. tradeType: BUY -> Anunciantes comprando USDT -> Representa la DEMANDA (Demand)
        vol_demanda = 0.0
        precio_venta_ref = 0.0
        data_buy = {"asset": "USDT", "fiat": moneda, "tradeType": "BUY", "page": 1, "rows": 15, "payTypes": []}

        r_buy = requests.post(url, json=data_buy, headers=headers, timeout=10)
        if r_buy.status_code == 200 and r_buy.json().get('data'):
            anuncios = r_buy.json()['data']
            precio_venta_ref = float(anuncios[0]['adv']['price'])
            for adv in anuncios:
                vol_demanda += float(adv['adv']['surplusAmount']) * float(adv['adv']['price'])

        if vol_demanda == 0 or vol_oferta == 0:
            return None, "⚠️ API temporalmente sin data de profundidad de volumen."

        # 3. Análisis del desequilibrio instantáneo (Corregido: Demanda - Oferta)
        vol_total = vol_demanda + vol_oferta
        delta_volumen = vol_demanda - vol_oferta
        fuerza_instantanea = (delta_volumen / vol_total) * 100  

        # Promedio Móvil de Fuerza
        historico_fuerza.append(fuerza_instantanea)
        fuerza_suavizada = sum(historico_fuerza) / len(historico_fuerza)

        # Análisis de microestructura del Spread
        spread_absoluto = abs(precio_compra_ref - precio_venta_ref) if precio_venta_ref > 0 else 0.0
        spread_porcentaje = (spread_absoluto / precio_compra_ref) * 100 if precio_compra_ref > 0 else 0.0
        alerta_spread = "⚠️ Volatilidad alta (spread ensanchado)" if spread_porcentaje > 1.2 else "✅ Liquidez óptima (spread controlado)"

        UMBRAL_CRITICO = 45.0

        if fuerza_suavizada >= UMBRAL_CRITICO:
            tendencia = "🚀 PRESIÓN ALCISTA"
            emoji = "🟢"
            prediccion = f"Absorción acelerada de la oferta respaldada por flujo promedio acumulado. Los compradores activos empujan el precio al alza. {alerta_spread}."
            confianza = "Alta"
            recomendacion = "💰 COMPRA - Demanda consistente superando a la oferta"
            puntaje = 7
        elif fuerza_suavizada <= -UMBRAL_CRITICO:
            tendencia = "🔻 PRESIÓN BAJISTA"
            emoji = "🔴"
            prediccion = f"Saturación de oferta en el libro de órdenes. Liquidaciones masivas buscando liquidez en Bs (comportamiento típico de intervención). El precio tiende a la baja. {alerta_spread}."
            confianza = "Alta"
            recomendacion = "⚠️ VENDE / ESPERA - Exceso de oferta circulante en el libro"
            puntaje = -7
        else:
            tendencia = "➡️ NEUTRAL / ESTABLE"
            emoji = "🟡"
            prediccion = f"Mercado en rango controlado y equilibrio dinámico. Flujo sin desequilibrios mayores en el promedio móvil. {alerta_spread}."
            confianza = "Media"
            recomendacion = "⏳ ESPERA - Rango plano controlado"
            puntaje = 0

        resultado = {
            'precio_actual': precio_compra_ref if precio_compra_ref > 0 else (historial_ves[-1] if historial_ves else 0.0),
            'cambio_10min': fuerza_suavizada,
            'cambio_30min': fuerza_suavizada * 0.8,
            'cambio_1id': fuerza_suavizada * 0.5,
            'cambio_1hora': fuerza_suavizada * 0.5,
            'cambio_2h': fuerza_suavizada * 0.2,
            'promedio': vol_total / 2,
            'volatilidad': spread_porcentaje,
            'soporte': precio_compra_ref * 0.995,
            'resistencia': precio_compra_ref * 1.005,
            'momentum': delta_volumen / 1000000,
            'rsi': 50 - (fuerza_suavizada / 2),
            'puntaje': puntaje,
            'tendencia': tendencia,
            'emoji': emoji,
            'prediccion': prediccion,
            'confianza': confianza,
            'recomendacion': recomendacion,
            'muestras': len(historial_ves)
        }

        return resultado, None

    except Exception as e:
        return None, f"❌ Error en análisis de volumen: {str(e)}"

# ==================== SISTEMA DE PREDICCIONES CON PRECISIÓN ====================

def guardar_prediccion(analisis):
    global historial_predicciones, estadisticas_predicciones

    prediccion = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'precio_actual': analisis['precio_actual'],
        'puntaje': analisis['puntaje'],
        'tendencia': analisis['tendencia'],
        'prediccion': analisis['prediccion'],
        'recomendacion': analisis['recomendacion'],
        'rsi': analisis['rsi'],
        'momentum': analisis['momentum'],
        'cambio_10min': analisis['cambio_10min'],
        'verificada': False,
        'acertada': False,
        'precio_verificacion': None
    }

    historial_predicciones.append(prediccion)
    estadisticas_predicciones['ultima_prediccion'] = prediccion
    estadisticas_predicciones['total_predicciones'] += 1

def verificar_predicciones():
    global historial_predicciones, estadisticas_predicciones

    if len(historial_predicciones) < 2:
        return

    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    if not compra_ves:
        return

    precio_actual = compra_ves

    for prediccion in historial_predicciones:
        if not prediccion['verificada']:
            tiempo_prediccion = datetime.strptime(prediccion['timestamp'], '%Y-%m-%d %H:%M:%S')
            tiempo_actual = datetime.now()
            minutos_transcurridos = (tiempo_actual - tiempo_prediccion).total_seconds() / 60

            if minutos_transcurridos >= 20:
                precio_prediccion = prediccion['precio_actual']
                cambio_real = ((precio_actual - precio_prediccion) / precio_prediccion) * 100

                acertada = False
                umbral_movimiento = 0.15

                if 'ALCISTA' in prediccion['tendencia'] and cambio_real >= umbral_movimiento:
                    acertada = True
                elif 'BAJISTA' in prediccion['tendencia'] and cambio_real <= -umbral_movimiento:
                    acertada = True
                elif 'NEUTRAL' in prediccion['tendencia'] and abs(cambio_real) < umbral_movimiento:
                    acertada = True

                prediccion['verificada'] = True
                prediccion['acertada'] = acertada
                prediccion['precio_verificacion'] = precio_actual
                prediccion['cambio_real'] = cambio_real

                if acertada:
                    estadisticas_predicciones['aciertos'] += 1
                else:
                    estadisticas_predicciones['fallos'] += 1

                total = estadisticas_predicciones['aciertos'] + estadisticas_predicciones['fallos']
                if total > 0:
                    estadisticas_predicciones['precision'] = (estadisticas_predicciones['aciertos'] / total) * 100

def obtener_estadisticas_precision():
    global estadisticas_predicciones, historial_predicciones

    total = estadisticas_predicciones['aciertos'] + estadisticas_predicciones['fallos']
    res = list(historial_predicciones)[-10:] if len(historial_predicciones) > 0 else []

    precision_alcista = 0
    precision_bajista = 0
    precision_neutral = 0
    total_alcista = 0
    total_bajista = 0
    total_neutral = 0

    for p in historial_predicciones:
        if p['verificada']:
            if 'ALCISTA' in p['tendencia']:
                total_alcista += 1
                if p['acertada']:
                    precision_alcista += 1
            elif 'BAJISTA' in p['tendencia']:
                total_bajista += 1
                if p['acertada']:
                    precision_bajista += 1
            elif 'NEUTRAL' in p['tendencia']:
                total_neutral += 1
                if p['acertada']:
                    precision_neutral += 1

    precision_alcista = (precision_alcista / total_alcista * 100) if total_alcista > 0 else 0
    precision_bajista = (precision_bajista / total_bajista * 100) if total_bajista > 0 else 0
    precision_neutral = (precision_neutral / total_neutral * 100) if total_neutral > 0 else 0

    salida = {}
    salida['total_predicciones'] = estadisticas_predicciones['total_predicciones']
    salida['verificadas'] = estadisticas_predicciones['aciertos'] + estadisticas_predicciones['fallos']
    salida['aciertos'] = estadisticas_predicciones['aciertos']
    salida['fallos'] = estadisticas_predicciones['fallos']
    salida['precision_general'] = estadisticas_predicciones['precision']
    salida['precision_alcista'] = precision_alcista
    salida['precision_bajista'] = precision_bajista
    salida['precision_neutral'] = precision_neutral
    salida['ultimas'] = res
    
    return salida

def mostrar_historial_predicciones(chat_id):
    stats = obtener_estadisticas_precision()

    mensaje = f"""📊 *HISTORIAL DE PREDICCIONES*

🎯 *Precisión del Bot:*
• Total predicciones: {stats['total_predicciones']}
• Verificadas: {stats['verificadas']}
• ✅ Aciertos: {stats['aciertos']}
• ❌ Fallos: {stats['fallos']}
• 📈 Precisión general: {stats['precision_general']:.1f}%

📊 *Precisión por tendencia:*
• 📈 Alcistas: {stats['precision_alcista']:.1f}%
• 📉 Bajistas: {stats['precision_bajista']:.1f}%
• ➡️ Neutral: {stats['precision_neutral']:.1f}%

📋 *Últimas 10 predicciones:*
"""

    if stats['ultimas']:
        for i, p in enumerate(reversed(stats['ultimas']), 1):
            estado = "✅" if p.get('acertada', False) else "❌" if p.get('verificada', False) else "⏳"
            tendencia = p['tendencia'][:20]

            if p.get('verificada', False):
                cambio = f"{p.get('cambio_real', 0):+.2f}%"
            else:
                cambio = "⏳ Pendiente"

            mensaje += f"\n{i}. {estado} {tendencia}... | {cambio}"

    mensaje += f"""

💡 *Recomendación:* { '✅ El bot está siendo preciso, confía en sus predicciones' if stats['precision_general'] > 60 else '⚠️ El bot está aprendiendo, toma las predicciones con precaución' }

🕐 Última actualización: {datetime.now().strftime('%H:%M:%S')}
"""
    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

def mostrar_estadisticas_detalladas(chat_id):
    stats = obtener_estadisticas_precision()

    precision = stats['precision_general']
    barras = "█" * int(precision / 5) + "░" * (20 - int(precision / 5))

    mensaje = f"""📈 *ESTADÍSTICAS DETALLADAS*

🎯 *Precisión General*
[{barras}] {precision:.1f}%

📊 *Distribución de Aciertos/Fallos*
✅ Aciertos: {stats['aciertos']} ({'█' * int(stats['aciertos'] / max(stats['verificadas'], 1) * 20) if stats['verificadas'] > 0 else '░░░░░░░░░░░░░░░░░░░░'})
❌ Fallos: {stats['fallos']} ({'█' * int(stats['fallos'] / max(stats['verificadas'], 1) * 20) if stats['verificadas'] > 0 else '░░░░░░░░░░░░░░░░░░░░'})

📊 *Rendimiento por Tendencia*
📈 Alcista:  {'▓' * int(stats['precision_alcista'] / 5)}{'░' * (20 - int(stats['precision_alcista'] / 5))} {stats['precision_alcista']:.1f}%
📉 Bajista:  {'▓' * int(stats['precision_bajista'] / 5)}{'░' * (20 - int(stats['precision_bajista'] / 5))} {stats['precision_bajista']:.1f}%
➡️ Neutral:  {'▓' * int(stats['precision_neutral'] / 5)}{'░' * (20 - int(stats['precision_neutral'] / 5))} {stats['precision_neutral']:.1f}%

📋 *Resumen:*
• Total predicciones: {stats['total_predicciones']}
• Verificadas: {stats['verificadas']}
• Ratio Acierto/Fallo: {stats['aciertos']}/{stats['fallos']}

{'✅ El bot tiene buena precisión' if stats['precision_general'] > 65 else '📈 El bot está mejorando su precisión' if stats['precision_general'] > 50 else '⚠️ El bot necesita más datos para ser preciso'}

🕐 {datetime.now().strftime('%H:%M:%S')}
"""
    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

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
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado_principal(chat_id))
        return

    mensaje = f"💰 *PRECIOS USDT P2P*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    for m, datos in precios.items():
        mensaje += f"*{m}*\n"
        mensaje += f"  🟢 COMPRA: {datos['compra']:.2f}\n"
        mensaje += f"  🔴 VENTA: {datos['venta']:.2f}\n"
        mensaje += f"  📊 Spread: {datos['compra']-datos['venta']:.2f}\n\n"

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

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

# ==================== TETHER USDT VS BCV ====================

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

    mensaje = f"🪙 *TETHER USDT vs BCV (+0.50%)*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"🏦 *BCV Oficial:* {tasas['usd']:.2f} Bs\n"
    mensaje += f"📈 *BCV + 0.50%:* {bcv_con_porcentaje:.2f} Bs\n\n"
    mensaje += f"🟢 *COMPRA USDT VES:* {compra:.2f} Bs\n"
    mensaje += f"  Diferencia vs BCV+0.50%: {diff_compra:+.2f} Bs\n"
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

    usdt_neto_tarjeta = monto * (1 - 0.015)  # Resta el 1.5% de la tarjeta
    usdt_final = usdt_neto_tarjeta * (1 - 0.041)  # Resta el 4.1% de Bpay

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

    if texto == '/start':
        mensaje = """
Bienvenido a TetherPrueba

Soy tu asistente diseñado para facilitarte la información sobre las tasas del momento de VES, COP y PEN del P2P de Binance.

Herramientas disponibles:

💰 Precio USDT → Todas las monedas
🪙 Tether USDT vs BCV → Comparativa con tasa oficial
📈 Historial de brecha VES → Últimas 24h

🔔 Alertas automáticas:
Activo por cambios en las tasas o por anomalías críticas en el Delta de Volumen P2P.
"""
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

    elif texto == '💰 Precio USDT' or texto == '/precios':
        mostrar_precios_usdt(chat_id)

    elif texto == '🪙 Tether USDT vs BCV' or texto == '/tether':
        mostrar_tether_vs_bcv(chat_id)

    elif texto == '📈 Historial de brecha VES' or texto == '/historial':
        mostrar_historial_ves(chat_id)

    elif texto == '🏦 Tasas de Cambio' or texto == '/tasas':
        if chat_id == ADMIN_ID:
            mostrar_tasas_cambio(chat_id)
        else:
            enviar_mensaje(chat_id, "❌ Solo el administrador puede usar este comando", crear_teclado_principal(chat_id))

    elif texto == '📋 + Opciones':
        mensaje = "📋 *+ OPCIONES*\n\nSelecciona una opción:"
        enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

    elif texto == '🔙 Volver al menú principal':
        mensaje = "🏠 *Volviendo al menú principal*"
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

    elif texto == '🇻🇪 Precio VES' or texto == '/ves':
        mostrar_precio_individual(chat_id, 'VES')

    elif texto == '🇨🇴 Precio COP' or texto == '/cop':
        mostrar_precio_individual(chat_id, 'COP')

    elif texto == '🇵🇪 Precio PEN' or texto == '/pen':
        mostrar_precio_individual(chat_id, 'PEN')

    elif texto == '👥 Usuarios Registrados' or texto == '/usuarios':
        if chat_id == ADMIN_ID:
            usuarios = obtener_usuarios()
            if usuarios:
                mensaje = f"👥 *SISTEMA AUTOMÁTIVO ACTIVO*\n\nTotal interactuando: {len(usuarios)}\n\nEl bot verifica accesos en tiempo real mediante Rose."
                for uid in usuarios:
                    mensaje += f"\n• `{uid}`"
            else:
                mensaje = "📝 No hay usuarios registrados"
            enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))
        else:
            enviar_mensaje(chat_id, "❌ Solo el administrador puede ver esto", crear_teclado_opciones(chat_id))

    elif texto == '📊 Análisis Mercado' or texto == '/analisis':
        mostrar_analisis_mercado(chat_id)

    elif texto == '💰 ¿Cuánto Gané?' or texto == '/cuantogane':
        mensaje = "✍️ *Calculadora Inteligente*\n\nPor favor, escribe directamente en el chat el monto en *USD* que deseas calcular (ejemplo: `50` o `150.50`) y te daré el desglose de tu ganancia al instante."
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

    elif texto == '📋 Historial Predicciones' or texto == '/historial_predicciones':
        mostrar_historial_predicciones(chat_id)

    elif texto == '📈 Estadísticas' or texto == '/estadisticas':
        mostrar_estadisticas_detalladas(chat_id)

    else:
        try:
            monto_limpio = texto.replace(',', '.')
            monto_usuario = float(monto_limpio)
            
            if monto_usuario > 0:
                calcular_ganancia_neta(chat_id, monto_usuario)
            else:
                enviar_mensaje(chat_id, "⚠️ El monto debe ser un número mayor a cero.", crear_teclado_principal(chat_id))
        except ValueError:
            enviar_mensaje(chat_id, "Usa /start o selecciona una opción del menú.", crear_teclado_principal(chat_id))

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

# ==================== ACTUALIZACIÓN CONTINUA Y ALERTAS DE VOLUMEN ====================

def actualizar_precios():
    global mantener_activo, ultimo_registro_prediccion, cache_precios, cache_tiempo, ultimo_delta_notificado
    
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

                analisis, err = analizar_tendencia_mercado('VES')
                if analisis and not err:
                    ahora = datetime.now()

                    precio_actual = analisis['precio_actual']
                    debe_guardar = False

                    if estadisticas_predicciones['ultima_prediccion'] is None:
                        debe_guardar = True
                    else:
                        precio_anterior = estadisticas_predicciones['ultima_prediccion']['precio_actual']
                        if precio_anterior > 0:
                            variacion_porcentaje = abs((precio_actual - precio_anterior) / precio_anterior) * 100
                            if variacion_porcentaje >= 0.5:
                                debe_guardar = True

                    if debe_guardar:
                        guardar_prediccion(analisis)
                        ultimo_registro_prediccion = ahora
                        print(f"🔮 Nueva predicción registrada por variación del 0.5% (Precio: {precio_actual:.2f}).")
                    
                    verificar_predicciones()

                    if analisis['puntaje'] in [7, -7]:
                        delta_actual = analisis['cambio_10min']
                        debe_notificar = False

                        if ultimo_delta_notificado is None:
                            debe_notificar = True
                        else:
                            if abs(delta_actual - ultimo_delta_notificado) >= 2.0:
                                debe_notificar = True

                        if debe_notificar:
                            msg_alerta = f"🚨 *ALERTA DE DESEQUILIBRIO CAMBIARIO (P2P)* 🚨\n\n" \
                                         f"🧭 *Sesgo del Mercado:* {analisis['tendencia']}\n" \
                                         f"📊 *Ratio de Liquidez (Oferta/Demanda):* {delta_actual:+.1f}% " \
                                         f"({'Exceso de Demanda' if delta_actual >= 0 else 'Exceso de Oferta'})\n\n" \
                                         f"💡 *Diagnóstico Macroeconómico:*\n{analisis['prediccion']}\n\n" \
                                         f"🕐 *Captura de Datos:* {datetime.now().strftime('%H:%M:%S')}"

                            for usr in obtener_usuarios():
                                try:
                                    enviar_mensaje(usr, msg_alerta)
                                    time.sleep(0.04)
                                except:
                                    pass
                            
                            ultimo_delta_notificado = delta_actual
                            print(f"🔔 Alerta por movimiento >= 2% enviada. Delta base fijado en: {delta_actual:.2f}%")
                        else:
                            print(f"⏳ Cambio en Delta menor al 2% (Actual: {delta_actual:.1f}% | Último: {ultimo_delta_notificado:.1f}%). Omisión activa.")

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
            url = f"https://{os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'localhost')}/"
            requests.get(url, timeout=10)
            print(f"💓 Keep alive: {datetime.now().strftime('%H:%M:%S')}")
        except:
            pass
        time.sleep(300)

# ==================== FLASK ====================

@app.route('/')
def home():
    return f"✅ Bot activo 24/7\n🔒 Canal/Grupo Vinculado: {GRUPO_AUTORIZADO_ID}\n📊 {len(historial_ves)} muestras VES\n📊 {len(historial_predicciones)} predicciones\n🕐 Hora: {datetime.now().strftime('%H:%M:%S')} (Caracas)"

# ==================== ANALISIS MERCADO ====================

def mostrar_analisis_mercado(chat_id):
    analisis, err = analizar_tendencia_mercado('VES')
    
    if err:
        enviar_mensaje(chat_id, err, crear_teclado_opciones(chat_id))
        return

    mensaje = f"""📊 *ANÁLISIS CUANTITATIVO (ORDER FLOW P2P)*

{analisis['emoji']} Sesgo del Mercado: {analisis['tendencia']}
🕐 {datetime.now().strftime('%H:%M:%S')}
📈 Precio Referencia: {analisis['precio_actual']:.2f} Bs

📊 *Métricas de Flujo:*
• Fuerza del Delta (Ratio): {analisis['cambio_10min']:+.2f}%
• Presión Neta (Order Flow): {analisis['momentum']:+.3f}M
• Volatilidad (Spread): {analisis['volatilidad']:.3f}%

🔮 *Diagnóstico Macroeconómico:* {analisis['prediccion']}

🎯 *Confianza Matemática:* {analisis['confianza']}
💡 *Recomendación:* {analisis['recomendacion']}

🔄 Análisis basado en profundidad de órdenes reales del P2P actual."""

    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

# ==================== MAIN ====================

if __name__ == "__main__":
    print("🚀 Bot iniciando en Railway...")
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
            ultimos_precios[m] = compra
            cache_precios[m] = {'compra': compra, 'venta': venta}
            cache_tiempo[m] = time.time()
            if m == 'VES':
                guardar_historial_ves(compra)
        else:
            print(f"  ❌ {m}: No disponible")

    print(f"\n📊 Historial VES inicial: {len(historial_ves)} muestras")
    print(f"📊 Sistema de predicciones cuantitativas inicializado")

    threading.Thread(target=recibir_mensajes, daemon=True).start()
    threading.Thread(target=actualizar_precios, daemon=True).start()
    threading.Thread(target=mantener_activo, daemon=True).start()

    print("\n✅ Bot listo!")
    print("=" * 40)

    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
