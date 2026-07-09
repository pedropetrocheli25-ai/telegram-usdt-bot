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

# ==================== ALERTAS (PARA TODOS LOS USUARIOS) ====================
UMBRALES = {
    'VES': 3.0,
    'COP': 60.0,
    'PEN': 0.60
}

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

def crear_teclado(chat_id):
    """Crea teclado personalizado según si es ADMIN o no"""
    teclado = [
        ["💰 Precio USDT"],
        ["🇻🇪 Precio VES", "🇨🇴 Precio COP"],
        ["🇵🇪 Precio PEN", "🪙 Tether USDT vs BCV"],
        ["📈 Historial VES"]
    ]
    
    # Botones SOLO para ADMIN
    if chat_id == ADMIN_ID:
        teclado.append(["👥 Usuarios Registrados"])
        teclado.append(["🏦 Tasas de Cambio"])
    
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

def obtener_tasas():
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

# ==================== MOSTRAR TASAS (SOLO ADMIN) ====================

def mostrar_tasas_admin(chat_id):
    """Muestra las tasas de cambio SOLO al ADMIN"""
    tasas = obtener_tasas()
    if not tasas:
        mensaje = "❌ No se pudieron obtener las tasas de cambio"
        enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))
        return
    
    mensaje = f"🏦 *TASAS DE CAMBIO* 🏦\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"💵 *Dólar (USD):* {tasas['usd']:.2f} Bs\n"
    mensaje += f"💶 *Euro (EUR):* {tasas['eur']:.2f} Bs\n"
    mensaje += f"📅 {tasas['fecha']}"
    
    enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))

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

# ==================== ALERTAS (PARA TODOS LOS USUARIOS) ====================

def verificar_alertas(precios):
    global ultimos_precios
    if not precios:
        return
    
    usuarios = obtener_usuarios()
    if not usuarios:
        print("⚠️ No hay usuarios registrados para enviar alertas")
        return
    
    for moneda in ['VES', 'COP', 'PEN']:
        if moneda not in precios or not precios[moneda]:
            continue
        precio_actual = precios[moneda]['compra']
        if ultimos_precios[moneda] is None:
            ultimos_precios[moneda] = precio_actual
            print(f"📊 {moneda}: Precio inicial {precio_actual:.2f}")
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
            # Enviar a TODOS los usuarios registrados
            enviados = 0
            for usuario in usuarios:
                try:
                    enviar_mensaje(usuario, mensaje)
                    enviados += 1
                    time.sleep(0.05)
                except:
                    pass
            
            print(f"🔔 Alerta {moneda} enviada a {enviados} usuarios")
            ultimos_precios[moneda] = precio_actual

# ==================== MOSTRAR PRECIOS ====================

def mostrar_precios(chat_id, moneda=None):
    precios = {}
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_p2p_reales(m)
        if compra and venta:
            precios[m] = {'compra': compra, 'venta': venta}
    if not precios:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado(chat_id))
        return
    
    if moneda == 'USDT' or moneda is None:
        mensaje = f"💰 *PRECIOS USDT P2P*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        for m, datos in precios.items():
            mensaje += f"*{m}*\n"
            mensaje += f"  🟢 COMPRA: {datos['compra']:.2f}\n"
            mensaje += f"  🔴 VENTA: {datos['venta']:.2f}\n"
            mensaje += f"  📊 Spread: {datos['compra']-datos['venta']:.2f}\n\n"
        enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))
    
    elif moneda in precios:
        datos = precios[moneda]
        mensaje = f"💰 *PRECIO {moneda}*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        mensaje += f"🟢 COMPRA: {datos['compra']:.2f}\n"
        mensaje += f"🔴 VENTA: {datos['venta']:.2f}\n"
        mensaje += f"📊 Spread: {datos['compra']-datos['venta']:.2f}\n"
        enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))

# ==================== TETHER USDT VS BCV ====================

def mostrar_tether_vs_bcv(chat_id):
    compra, venta = obtener_precios_p2p_reales('VES')
    if not compra or not venta:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado(chat_id))
        return
    
    tasas = obtener_tasas()
    if not tasas:
        enviar_mensaje(chat_id, "⏳ Obteniendo tasas...", crear_teclado(chat_id))
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
    
    enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))

# ==================== HISTORIAL VES ====================

def mostrar_historial_ves(chat_id):
    analisis = obtener_analisis_ves()
    if not analisis:
        mensaje = "📈 *HISTORIAL VES*\n⏳ Sin datos suficientes aún"
        enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))
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
    
    enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))

# ==================== PROCESAR MENSAJES ====================

def procesar_mensaje(chat_id, texto):
    print(f"📩 {texto}")
    guardar_usuario(chat_id)
    
    if texto == '/start':
        mensaje = f"""
🤖 *BOT USDT P2P* 🚀

🔔 *Alertas para TODOS los usuarios*
👥 {len(usuarios_activos)} usuarios registrados

📱 *Botones:*
💰 Precio USDT - Todas las monedas
🇻🇪 Precio VES - Solo VES
🇨🇴 Precio COP - Solo COP
🇵🇪 Precio PEN - Solo PEN
🪙 Tether USDT vs BCV - BCV + 0.50%
📈 Historial VES - Solo VES (24h)

⚡ *Umbrales de alerta:* VES: 3 Bs | COP: 60 | PEN: 0.60
🕐 *Hora de Caracas (UTC -4)*
"""
        enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))
    
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
    
    # ==================== SOLO ADMIN ====================
    elif texto == '👥 Usuarios Registrados' or texto == '/usuarios':
        if chat_id == ADMIN_ID:
            usuarios = obtener_usuarios()
            if usuarios:
                mensaje = f"👥 *USUARIOS REGISTRADOS*\n\nTotal: {len(usuarios)}\n\n"
                for uid in usuarios:
                    mensaje += f"• `{uid}`\n"
            else:
                mensaje = "📝 No hay usuarios registrados"
            enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))
    
    elif texto == '🏦 Tasas de Cambio' or texto == '/tasas':
        if chat_id == ADMIN_ID:
            mostrar_tasas_admin(chat_id)
    
    else:
        enviar_mensaje(chat_id, "Usa /start", crear_teclado(chat_id))

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
    print(f"🕐 Hora actual: {datetime.now().strftime('%H:%M:%S')}")
    
    cargar_usuarios()
    print(f"👥 {len(usuarios_activos)} usuarios en memoria")
    
    print("\n📊 Probando conexión a Binance...")
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_p2p_reales(m)
        if compra and venta:
            print(f"  ✅ {m}: {compra:.2f} / {venta:.2f}")
            ultimos_precios[m] = compra
            if m == 'VES':
                guardar_historial_ves(compra)
        else:
            print(f"  ❌ {m}: No disponible")
    
    print("\n🔔 ALERTAS ACTIVADAS PARA TODOS LOS USUARIOS")
    print(f"  VES: ±{UMBRALES['VES']} Bs")
    print(f"  COP: ±{UMBRALES['COP']} COP")
    print(f"  PEN: ±{UMBRALES['PEN']} PEN")
    print(f"  👥 {len(usuarios_activos)} usuarios recibirán alertas")
    
    print("\n📱 Botones para TODOS:")
    print("  - Precio USDT, VES, COP, PEN")
    print("  - Tether USDT vs BCV (BCV + 0.50%)")
    print("  - Historial VES (solo VES)")
    
    print("\n🔒 Botones SOLO para ADMIN:")
    print("  - Usuarios Registrados")
    print("  - Tasas de Cambio")
    
    threading.Thread(target=recibir_mensajes, daemon=True).start()
    threading.Thread(target=actualizar_precios, daemon=True).start()
    threading.Thread(target=mantener_activo, daemon=True).start()
    
    print("\n✅ Bot listo!")
    print("🕐 Hora de Caracas (UTC -4)")
    print("=" * 40)
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)