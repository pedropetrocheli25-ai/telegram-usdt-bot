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

# ==================== ALERTAS ====================
UMBRALES = {
    'VES': 1.0,
    'COP': 0.60,
    'PEN': 0.10
}

FLUCTUACION_UMBRAL = 0.8

ultimos_precios = {'VES': None, 'COP': None, 'PEN': None}
usuarios_activos = set()
ARCHIVO_USUARIOS = "usuarios.txt"
ARCHIVO_ALERTAS = "alertas_usuarios.json"

# ==================== ALERTAS POR USUARIO ====================
alertas_usuarios = {}

def cargar_alertas():
    global alertas_usuarios
    try:
        if os.path.exists(ARCHIVO_ALERTAS):
            with open(ARCHIVO_ALERTAS, 'r') as f:
                alertas_usuarios = json.load(f)
            print(f"✅ Alertas de {len(alertas_usuarios)} usuarios cargadas")
    except:
        print("📝 No hay alertas guardadas")

def guardar_alertas():
    try:
        with open(ARCHIVO_ALERTAS, 'w') as f:
            json.dump(alertas_usuarios, f)
    except:
        pass

def get_alertas_usuario(chat_id):
    chat_id_str = str(chat_id)
    if chat_id_str not in alertas_usuarios:
        alertas_usuarios[chat_id_str] = {
            'activa': False,
            'monedas': ['VES', 'COP', 'PEN'],
            'umbral_ves': 1.0,
            'umbral_cop': 0.60,
            'umbral_pen': 0.10,
            'fluctuacion': 0.8
        }
        guardar_alertas()
    return alertas_usuarios[chat_id_str]

def actualizar_alerta_usuario(chat_id, **kwargs):
    chat_id_str = str(chat_id)
    if chat_id_str not in alertas_usuarios:
        get_alertas_usuario(chat_id)
    for key, value in kwargs.items():
        alertas_usuarios[chat_id_str][key] = value
    guardar_alertas()

# ==================== CACHÉ DE PRECIOS ====================
cache_precios = {}
cache_tiempo = {}
CACHE_DURACION = 30

# ==================== HISTORIAL (SOLO VES) ====================
historial_ves = deque(maxlen=1440)
precio_apertura_ves = None

# ==================== GESTIÓN DE USUARIOS ====================

def cargar_usuarios():
    global usuarios_activos
    try:
        if os.path.exists(ARCHIVO_USUARIOS):
            with open(ARCHIVO_USUARIOS, 'r') as f:
                for linea in f:
                    try:
                        usuarios_activos.add(int(linea.strip()))
                    except:
                        pass
            print(f"✅ {len(usuarios_activos)} usuarios cargados")
    except:
        print("📝 No hay usuarios guardados")

def guardar_usuario(chat_id):
    global usuarios_activos
    if chat_id not in usuarios_activos:
        usuarios_activos.add(chat_id)
        try:
            with open(ARCHIVO_USUARIOS, 'a') as f:
                f.write(f"{chat_id}\n")
            print(f"✅ Nuevo usuario: {chat_id}")
        except:
            pass

def obtener_usuarios():
    return list(usuarios_activos)

# ==================== FUNCIONES ====================

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

def crear_teclado_principal():
    teclado = [
        ["💰 Precio USDT"],
        ["🪙 Tether USDT vs BCV"],
        ["📈 Historial VES"],
        ["⚙️ Configurar Alertas"],
        ["📋 Otras Opciones"]
    ]
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_alertas(chat_id):
    alerta = get_alertas_usuario(chat_id)
    estado = "🔴 Desactivar" if alerta.get('activa', False) else "🟢 Activar"
    
    teclado = [
        [f"{estado} Alertas"],
        ["📋 Ver Configuración"],
        ["🔙 Volver al menú principal"]
    ]
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_opciones(chat_id):
    teclado = [
        ["🇻🇪 Precio VES"],
        ["🇨🇴 Precio COP"],
        ["🇵🇪 Precio PEN"],
        ["🔙 Volver al menú principal"]
    ]
    
    if chat_id == ADMIN_ID:
        teclado.insert(3, ["👥 Usuarios Registrados"])
        teclado.insert(4, ["🏦 Tasas de Cambio"])
    
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
        enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))
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
    
    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

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
            alerta = get_alertas_usuario(usuario)
            if alerta.get('activa', False):
                try:
                    enviar_mensaje(usuario, mensaje)
                    time.sleep(0.05)
                except:
                    pass
        print(f"🔔 Alerta de fluctuación enviada a usuarios con alertas activas")
    
    ultimas_tasas_cruzadas = tasas_actuales.copy()
    guardar_tasas_anteriores()

# ==================== HISTORIAL ====================

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
        alerta = get_alertas_usuario(usuario)
        if not alerta.get('activa', False):
            continue
        
        monedas_usuario = alerta.get('monedas', ['VES', 'COP', 'PEN'])
        
        for moneda in monedas_usuario:
            if moneda not in precios or not precios[moneda]:
                continue
            
            precio_actual = precios[moneda]['compra']
            
            if ultimos_precios[moneda] is None:
                ultimos_precios[moneda] = precio_actual
                continue
            
            cambio = abs(precio_actual - ultimos_precios[moneda])
            
            if moneda == 'VES':
                umbral = alerta.get('umbral_ves', 1.0)
            elif moneda == 'COP':
                umbral = alerta.get('umbral_cop', 0.60)
            elif moneda == 'PEN':
                umbral = alerta.get('umbral_pen', 0.10)
            else:
                umbral = 0
            
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
                try:
                    enviar_mensaje(usuario, mensaje)
                    time.sleep(0.05)
                except:
                    pass
        
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
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado_principal())
        return
    
    mensaje = f"💰 *PRECIOS USDT P2P*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    for m, datos in precios.items():
        mensaje += f"*{m}*\n"
        mensaje += f"  🟢 COMPRA: {datos['compra']:.2f}\n"
        mensaje += f"  🔴 VENTA: {datos['venta']:.2f}\n"
        mensaje += f"  📊 Spread: {datos['compra']-datos['venta']:.2f}\n\n"
    
    enviar_mensaje(chat_id, mensaje, crear_teclado_principal())

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
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado_principal())
        return
    
    tasas = obtener_tasas_bcv()
    if not tasas:
        enviar_mensaje(chat_id, "⏳ Obteniendo tasas...", crear_teclado_principal())
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
    
    enviar_mensaje(chat_id, mensaje, crear_teclado_principal())

# ==================== CONFIGURAR ALERTAS (CON BOTONES) ====================

def mostrar_configurar_alertas(chat_id):
    alerta = get_alertas_usuario(chat_id)
    
    estado = "🟢 ACTIVADA" if alerta.get('activa', False) else "🔴 DESACTIVADA"
    monedas = ", ".join(alerta.get('monedas', ['VES', 'COP', 'PEN']))
    
    mensaje = f"⚙️ *CONFIGURAR ALERTAS*\n\n"
    mensaje += f"📊 *Estado:* {estado}\n"
    mensaje += f"📊 *Monedas:* {monedas}\n"
    mensaje += f"⚡ *Umbral VES:* {alerta.get('umbral_ves', 1.0):.2f} Bs\n"
    mensaje += f"⚡ *Umbral COP:* {alerta.get('umbral_cop', 0.60):.2f} COP\n"
    mensaje += f"⚡ *Umbral PEN:* {alerta.get('umbral_pen', 0.10):.2f} PEN\n"
    mensaje += f"📈 *Fluctuación Cruzada:* {alerta.get('fluctuacion', 0.8):.1f}%\n\n"
    
    mensaje += f"📝 *Comandos:*\n"
    mensaje += f"🔘 /alertas_on - Activar alertas\n"
    mensaje += f"🔘 /alertas_off - Desactivar alertas\n"
    mensaje += f"🔘 /alertas_monedas VES COP - Elegir monedas\n"
    mensaje += f"🔘 /alertas_umbral VES 1.5 - Cambiar umbral\n"
    mensaje += f"🔘 /alertas_fluctuacion 0.8 - Cambiar % fluctuación\n\n"
    mensaje += f"📌 *O usa los botones abajo*"
    
    enviar_mensaje(chat_id, mensaje, crear_teclado_alertas(chat_id))

# ==================== PROCESAR MENSAJES ====================

def procesar_mensaje(chat_id, texto):
    print(f"📩 {texto}")
    guardar_usuario(chat_id)
    
    # ==================== MENÚ PRINCIPAL ====================
    if texto == '/start':
        mensaje = f"""
🤖 *BOT CASA DE CAMBIO* 🚀

🔔 *Alertas personalizadas por usuario*
👥 {len(usuarios_activos)} usuarios registrados

📱 *Botones:*
💰 Precio USDT - Todas las monedas
🪙 Tether USDT vs BCV - BCV + 0.50%
📈 Historial VES - 24h
⚙️ Configurar Alertas - Activa/Desactiva
📋 Otras Opciones - VES, COP, PEN, Tasas, Usuarios

⚡ *Umbrales por defecto:* VES: 1 Bs | COP: 0.60 | PEN: 0.10
🕐 *Hora de Caracas (UTC -4)*
"""
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal())
    
    elif texto == '💰 Precio USDT' or texto == '/precios':
        mostrar_precios_usdt(chat_id)
    
    elif texto == '🪙 Tether USDT vs BCV' or texto == '/tether':
        mostrar_tether_vs_bcv(chat_id)
    
    elif texto == '📈 Historial VES' or texto == '/historial':
        mostrar_historial_ves(chat_id)
    
    elif texto == '⚙️ Configurar Alertas' or texto == '/configurar':
        mostrar_configurar_alertas(chat_id)
    
    # ==================== BOTONES DE ALERTAS ====================
    elif texto == '🟢 Activar Alertas' or texto == '/alertas_on':
        actualizar_alerta_usuario(chat_id, activa=True)
        enviar_mensaje(chat_id, "✅ Alertas activadas correctamente", crear_teclado_principal())
    
    elif texto == '🔴 Desactivar Alertas' or texto == '/alertas_off':
        actualizar_alerta_usuario(chat_id, activa=False)
        enviar_mensaje(chat_id, "❌ Alertas desactivadas correctamente", crear_teclado_principal())
    
    elif texto == '📋 Ver Configuración':
        mostrar_configurar_alertas(chat_id)
    
    # ==================== OTRAS OPCIONES ====================
    elif texto == '📋 Otras Opciones':
        mensaje = "📋 *OTRAS OPCIONES*\n\nSelecciona una opción:"
        enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))
    
    elif texto == '🔙 Volver al menú principal':
        mensaje = "🏠 *Volviendo al menú principal*"
        enviar_mensaje(chat_id, mensaje, crear_teclado_principal())
    
    elif texto == '🇻🇪 Precio VES' or texto == '/ves':
        mostrar_precio_individual(chat_id, 'VES')
    
    elif texto == '🇨🇴 Precio COP' or texto == '/cop':
        mostrar_precio_individual(chat_id, 'COP')
    
    elif texto == '🇵🇪 Precio PEN' or texto == '/pen':
        mostrar_precio_individual(chat_id, 'PEN')
    
    # ==================== SOLO ADMIN ====================
    elif texto == '👥 Usuarios Registrados' or texto == '/usuarios':
        if chat_id == ADMIN_ID:
            usuarios = obtener_usuarios()
            if usuarios:
                mensaje = f"👥 *USUARIOS REGISTRADOS*\n\nTotal: {len(usuarios)}\n\n"
                for uid in usuarios:
                    alerta = get_alertas_usuario(uid)
                    estado = "🟢 Activa" if alerta.get('activa', False) else "🔴 Inactiva"
                    mensaje += f"• `{uid}` - {estado}\n"
            else:
                mensaje = "📝 No hay usuarios registrados"
            enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))
    
    elif texto == '🏦 Tasas de Cambio' or texto == '/tasas':
        if chat_id == ADMIN_ID:
            mostrar_tasas_cambio(chat_id)
    
    # ==================== COMANDOS DE ALERTAS ====================
    elif texto.startswith('/alertas_on'):
        actualizar_alerta_usuario(chat_id, activa=True)
        enviar_mensaje(chat_id, "✅ Alertas activadas correctamente", crear_teclado_principal())
    
    elif texto.startswith('/alertas_off'):
        actualizar_alerta_usuario(chat_id, activa=False)
        enviar_mensaje(chat_id, "❌ Alertas desactivadas correctamente", crear_teclado_principal())
    
    elif texto.startswith('/alertas_monedas'):
        partes = texto.split(maxsplit=1)
        valor = partes[1] if len(partes) > 1 else None
        if valor:
            monedas = [m.strip().upper() for m in valor.split() if m.strip().upper() in ['VES', 'COP', 'PEN']]
            if monedas:
                actualizar_alerta_usuario(chat_id, monedas=monedas)
                enviar_mensaje(chat_id, f"✅ Monedas actualizadas: {', '.join(monedas)}", crear_teclado_principal())
            else:
                enviar_mensaje(chat_id, "❌ Monedas inválidas. Usa: VES, COP, PEN", crear_teclado_principal())
    
    elif texto.startswith('/alertas_umbral'):
        partes = texto.split(maxsplit=1)
        valor = partes[1] if len(partes) > 1 else None
        if valor:
            partes2 = valor.split()
            if len(partes2) >= 2:
                moneda = partes2[0].upper()
                try:
                    umbral = float(partes2[1])
                    if moneda == 'VES':
                        actualizar_alerta_usuario(chat_id, umbral_ves=umbral)
                        enviar_mensaje(chat_id, f"✅ Umbral VES actualizado a {umbral:.2f} Bs", crear_teclado_principal())
                    elif moneda == 'COP':
                        actualizar_alerta_usuario(chat_id, umbral_cop=umbral)
                        enviar_mensaje(chat_id, f"✅ Umbral COP actualizado a {umbral:.2f} COP", crear_teclado_principal())
                    elif moneda == 'PEN':
                        actualizar_alerta_usuario(chat_id, umbral_pen=umbral)
                        enviar_mensaje(chat_id, f"✅ Umbral PEN actualizado a {umbral:.2f} PEN", crear_teclado_principal())
                    else:
                        enviar_mensaje(chat_id, "❌ Moneda inválida. Usa: VES, COP, PEN", crear_teclado_principal())
                except:
                    enviar_mensaje(chat_id, "❌ Valor inválido. Usa: /alertas_umbral VES 1.5", crear_teclado_principal())
    
    elif texto.startswith('/alertas_fluctuacion'):
        partes = texto.split(maxsplit=1)
        valor = partes[1] if len(partes) > 1 else None
        if valor:
            try:
                fluctuacion = float(valor)
                if 0.1 <= fluctuacion <= 10:
                    actualizar_alerta_usuario(chat_id, fluctuacion=fluctuacion)
                    enviar_mensaje(chat_id, f"✅ Fluctuación actualizada a {fluctuacion:.1f}%", crear_teclado_principal())
                else:
                    enviar_mensaje(chat_id, "❌ Valor inválido. Usa entre 0.1 y 10", crear_teclado_principal())
            except:
                enviar_mensaje(chat_id, "❌ Valor inválido. Usa: /alertas_fluctuacion 0.8", crear_teclado_principal())
    
    else:
        enviar_mensaje(chat_id, "Usa /start", crear_teclado_principal())

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

# ==================== ACTUALIZACIÓN CONTINUA ====================

def actualizar_precios():
    while True:
        try:
            print(f"\n🔄 Actualizando... {datetime.now().strftime('%H:%M:%S')}")
            print(f"👥 Usuarios: {len(usuarios_activos)}")
            
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
            else:
                print("  ❌ No se obtuvieron precios")
            
            time.sleep(60)
            
        except Exception as e:
            print(f"  ❌ Error: {e}")
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
    return f"✅ Bot activo 24/7\n👥 {len(usuarios_activos)} usuarios\n📊 {len(historial_ves)} muestras VES\n🕐 Hora: {datetime.now().strftime('%H:%M:%S')} (Caracas)"

# ==================== MAIN ====================

if __name__ == "__main__":
    print("🚀 Bot iniciando en Railway...")
    print(f"✅ TOKEN: {'Configurado' if TOKEN else 'FALTANTE'}")
    print(f"✅ ADMIN_ID: {ADMIN_ID if ADMIN_ID else 'FALTANTE'}")
    print(f"🕐 Zona horaria: Caracas (UTC -4)")
    
    cargar_usuarios()
    cargar_alertas()
    cargar_tasas_anteriores()
    print(f"👥 {len(usuarios_activos)} usuarios en memoria")
    print(f"🔔 {len(alertas_usuarios)} configuraciones de alertas")
    
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
    
    print("\n📋 MENÚ PRINCIPAL:")
    print("  - Precio USDT")
    print("  - Tether USDT vs BCV")
    print("  - Historial VES")
    print("  - Configurar Alertas (CON BOTONES)")
    print("  - Otras Opciones")
    
    threading.Thread(target=recibir_mensajes, daemon=True).start()
    threading.Thread(target=actualizar_precios, daemon=True).start()
    threading.Thread(target=mantener_activo, daemon=True).start()
    
    print("\n✅ Bot listo!")
    print("=" * 40)
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)