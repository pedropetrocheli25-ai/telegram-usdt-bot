import requests
import time
import os
import threading
from datetime import datetime
from collections import deque
from flask import Flask

# ==================== CONFIGURACIÓN DE ZONA HORARIA ====================
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

# ==================== ALERTAS (SOLO ADMIN) ====================
UMBRALES = {
    'VES': 1.0,    # Alerta si cambia ±1.0 Bs
    'COP': 50.0,   # Alerta si cambia ±50 COP
    'PEN': 0.10    # Alerta si cambia ±0.10 PEN
}

precios_base = {'VES': None, 'COP': None, 'PEN': None}
ultimos_precios = {'VES': None, 'COP': None, 'PEN': None}
usuarios_activos = set()
ARCHIVO_USUARIOS = "usuarios.txt"

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

# ==================== FUNCIONES ENVIAR ====================

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

def enviar_logo_tether(chat_id, texto_adicional=""):
    """Envía el logo de Tether con fondo verde + texto"""
    url_logo = "https://upload.wikimedia.org/wikipedia/commons/thumb/7/70/Tether_%28USDT%29_Logo.svg/200px-Tether_%28USDT%29_Logo.svg.png"
    try:
        url = URL_TELEGRAM + "sendPhoto"
        data = {
            "chat_id": chat_id,
            "photo": url_logo,
            "caption": texto_adicional,
            "parse_mode": "Markdown",
            "reply_markup": crear_teclado()
        }
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    except:
        return False

def crear_teclado():
    teclado = [
        ["💰 Precio USDT"],
        ["🇻🇪 Precio VES", "🇨🇴 Precio COP"],
        ["🇵🇪 Precio PEN", "🪙 Tether USDT vs BCV"],
        ["📈 Historial VES"]
    ]
    return {"keyboard": teclado, "resize_keyboard": True}

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

# ==================== CONSULTA SOLO BCV USD ====================

def obtener_bcv_usd_oficial():
    """Obtiene única y exclusivamente el precio oficial del BCV USD de DolarApi"""
    try:
        url = "https://ve.dolarapi.com/v1/dolares/oficial"
        r = requests.get(url, timeout=6)
        if r.status_code == 200:
            val = float(r.json().get('promedio', 0))
            if val > 0:
                return val
    except:
        pass
    
    # Respaldo alternativo por si falla el primer endpoint
    try:
        url_alt = "https://ve.dolarapi.com/v1/dolares"
        r = requests.get(url_alt, timeout=6)
        if r.status_code == 200:
            for item in r.json():
                if item.get('id') == 'oficial':
                    return float(item.get('promedio', 0))
    except:
        pass
    return None

# ==================== HISTORIAL (SOLO VES) ====================

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

# ==================== ALERTAS (SOLO ADMIN) ====================

def verificar_alertas(precios):
    global precios_base, ultimos_precios
    
    if not precios:
        return
    
    for moneda in ['VES', 'COP', 'PEN']:
        if moneda not in precios or not precios[moneda]:
            continue
        
        precio_actual = precios[moneda]['compra']
        
        if precios_base[moneda] is None:
            precios_base[moneda] = precio_actual
            ultimos_precios[moneda] = precio_actual
            print(f"📊 Precio base {moneda}: {precio_actual:.2f}")
            continue
        
        cambio_absoluto = precio_actual - precios_base[moneda]
        cambio_porcentaje = (cambio_absoluto / precios_base[moneda]) * 100 if precios_base[moneda] != 0 else 0
        
        umbral = UMBRALES.get(moneda, 0)
        if abs(cambio_absoluto) >= umbral:
            direccion = "📈 SUBIÓ" if cambio_absoluto > 0 else "📉 BAJÓ"
            emoji = "🟢" if cambio_absoluto > 0 else "🔴"
            signo = "+" if cambio_absoluto > 0 else ""
            
            mensaje = f"""
{emoji} *🔔 ALERTA {moneda}* {emoji}

{direccion} desde el precio base

📊 *Detalles:*
• Precio Base: {precios_base[moneda]:.2f}
• Precio Actual: {precio_actual:.2f}
• Cambio: {signo}{cambio_absoluto:.2f} ({signo}{cambio_porcentaje:.1f}%)

🕐 {datetime.now().strftime('%H:%M:%S')}
"""
            enviar_mensaje(ADMIN_ID, mensaje)
            print(f"🔔 Alerta {moneda} enviada al ADMIN")
            
            precios_base[moneda] = precio_actual
        
        ultimos_precios[moneda] = precio_actual

# ==================== MOSTRAR PRECIOS (SOLO BINANCE P2P) ====================

def mostrar_precios(chat_id, moneda=None):
    if moneda == 'USDT' or moneda is None:
        precios = {}
        for m in ['VES', 'COP', 'PEN']:
            compra, venta = obtener_precios_p2p_reales(m)
            if compra and venta:
                precios[m] = {'compra': compra, 'venta': venta}
                
        if not precios:
            enviar_mensaje(chat_id, "⏳ Error al obtener los precios P2P de Binance...", crear_teclado())
            return
            
        mensaje = f"💰 *PRECIOS USDT BINANCE P2P*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        for m in ['VES', 'COP', 'PEN']:
            if m in precios:
                datos = precios[m]
                mensaje += f"*{m}*\n"
                mensaje += f"  🟢 COMPRA: {datos['compra']:.2f}\n"
                mensaje += f"  🔴 VENTA: {datos['venta']:.2f}\n"
                mensaje += f"  📊 Spread: {datos['compra']-datos['venta']:.2f}\n\n"
                
        enviar_logo_tether(chat_id, mensaje)
        return

    compra, venta = obtener_precios_p2p_reales(moneda)
    if compra and venta:
        mensaje = f"💰 *PRECIO {moneda}*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        mensaje += f"🟢 COMPRA: {compra:.2f}\n"
        mensaje += f"🔴 VENTA: {venta:.2f}\n"
        mensaje += f"📊 Spread: {compra-venta:.2f}\n"
        enviar_mensaje(chat_id, mensaje, crear_teclado())
    else:
        enviar_mensaje(chat_id, f"⏳ Error al obtener el precio de {moneda} de Binance...", crear_teclado())

# ==================== TETHER USDT VS BCV CORREGIDO ====================

def mostrar_tether_vs_bcv(chat_id):
    # 1. Obtener precio P2P de Binance de Venezuela
    compra, venta = obtener_precios_p2p_reales('VES')
    if not compra or not venta:
        enviar_mensaje(chat_id, "⏳ Error al obtener precio de Binance P2P...", crear_teclado())
        return
        
    # 2. Obtener tasa BCV USD oficial
    bcv_usd = obtener_bcv_usd_oficial()
    if not bcv_usd:
        enviar_mensaje(chat_id, "⚠️ Servidor de tasas BCV no disponible. Intente de nuevo.", crear_teclado())
        return
        
    # 3. Calcular: BCV USD + 0.50%
    bcv_con_porcentaje = bcv_usd * 1.005
    
    # 4. Calcular diferenciales matemáticos solicitados
    diff_venta = venta - bcv_con_porcentaje
    pct_venta = (diff_venta / bcv_con_porcentaje) * 100 if bcv_con_porcentaje > 0 else 0
    
    # 5. Estructurar mensaje analítico limpio
    mensaje = f"🪙 *TETHER USDT vs BCV*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"🏦 *BCV USD Oficial:* {bcv_usd:.2f} Bs\n"
    mensaje += f"📈 *BCV USD + 0.50%:* {bcv_con_porcentaje:.2f} Bs\n"
    mensaje += f"🔴 *VENTA USDT P2P:* {venta:.2f} Bs\n\n"
    
    signo = "+" if diff_venta > 0 else ""
    mensaje += f"📊 *Diferencial Analítico:*\n"
    mensaje += f"• Diferencia: {signo}{diff_venta:.2f} Bs\n"
    mensaje += f"• Porcentaje: {signo}{pct_venta:.2f}%\n"
    
    enviar_logo_tether(chat_id, mensaje)

# ==================== HISTORIAL VES ====================

def mostrar_historial_ves(chat_id):
    analisis = obtener_analisis_ves()
    if not analisis:
        mensaje = "📈 *HISTORIAL VES*\n⏳ Sin datos suficientes aún"
        enviar_mensaje(chat_id, mensaje, crear_teclado())
        return
    
    mensaje = f"📈 *HISTORIAL VES (24h)*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n"
    mensaje += f"📅 {datetime.now().strftime('%d/%m/%Y')}\n\n"
    mensaje += f"📊 *Apertura:* {analisis['apertura']:.2f} Bs\n"
    mensaje += f"📊 *Actual:* {analisis['actual']:.2f} Bs\n"
    emoji = "📈" if analisis['cambio'] > 0 else "📉" if analisis['cambio'] < 0 else "➡️"
    mensaje += f"{emoji} *Cambio:* {analisis['cambio']:+.2f} Bs ({analisis['cambio_porcentaje']:+.1f}%)\n"
    mensaje += f"📈 *Máximo:* {analisis['maximo']:.2f} Bs\n"
    mensaje += f"📉 *Mínimo:* {analisis['minimo']:.2f} Bs\n"
    mensaje += f"🧭 *Tendencia:* {analisis['tendencia']}\n"
    mensaje += f"📊 *Muestras:* {analisis['muestras']}\n"
    
    enviar_mensaje(chat_id, mensaje, crear_teclado())

# ==================== PROCESAR MENSAJES ====================

def procesar_mensaje(chat_id, texto):
    print(f"📩 {texto}")
    guardar_usuario(chat_id)
    
    if texto == '/start':
        mensaje = f"""
🤖 *BOT USDT P2P* 🚀

🔔 *Alertas SOLO para el ADMIN*
• VES: ±{UMBRALES['VES']:.1f} Bs desde precio base
• COP: ±{UMBRALES['COP']:.0f} COP desde precio base
• PEN: ±{UMBRALES['PEN']:.2f} PEN desde precio base

👥 {len(usuarios_activos)} usuarios registrados

📱 *Botones:*
💰 Precio USDT - Todas las monedas (VES, COP, PEN)
🇻🇪 Precio VES - Solo VES
🇨🇴 Precio COP - Solo COP
🇵🇪 Precio PEN - Solo PEN
🪙 Tether USDT vs BCV - Analítico BCV USD + 0.50%
📈 Historial VES - Analítica VES (24h)

🕐 *Hora de Caracas (UTC -4)*
"""
        enviar_logo_tether(chat_id, mensaje)
    
    elif texto == '💰 Precio USDT' or texto == '/precios':
        mostrar_precios(chat_id, 'USDT')
    
    elif texto == '🇻🇪 Precio VES' or texto == '/ves':
        mostrar_precios(chat_id, 'VES')
    
    elif texto == '🇨🇴 Precio COP' or texto == '/cop':
        mostrar_precios(chat_id, 'COP')
    
    elif texto == '🇵🇪 Precio PEN' or texto == '/pen':
        mostrar_precios(chat_id, 'PEN')
        
    elif texto == '🪙 Tether USDT vs BCV' or texto == '/tether':
        mostrar_tether_vs_bcv(chat_id)
    
    elif texto == '📈 Historial VES' or texto == '/historial':
        mostrar_historial_ves(chat_id)
    
    else:
        enviar_mensaje(chat_id, "Usa /start", crear_teclado())

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
                    if moneda == 'VES':
                        guardar_historial_ves(compra)
            
            if precios:
                verificar_alertas(precios)
                print(f"  ✅ VES:
