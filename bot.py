import requests
import time
import os
import threading
import json
from datetime import datetime
from collections import deque
from flask import Flask

TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not TOKEN or not ADMIN_ID:
    print("ERROR: Faltan variables")
    exit(1)

ADMIN_ID = int(ADMIN_ID)
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"
ultimo_update_id = 0

app = Flask(__name__)

UMBRALES = {
    'VES': 1.0,
    'COP': 20.0,
    'PEN': 0.20
}

ultimos_precios = {'VES': None, 'COP': None, 'PEN': None}
usuarios_activos = set()

def cargar_usuarios():
    global usuarios_activos
    try:
        usuarios_json = os.environ.get('USUARIOS', '[]')
        usuarios_lista = json.loads(usuarios_json)
        usuarios_activos = set(usuarios_lista)
        print(f"Usuarios cargados: {len(usuarios_activos)}")
    except:
        print("No hay usuarios guardados")

def guardar_usuario(chat_id):
    global usuarios_activos
    if chat_id not in usuarios_activos:
        usuarios_activos.add(chat_id)
        print(f"Nuevo usuario: {chat_id}")

def obtener_usuarios():
    return list(usuarios_activos)

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
    teclado = [
        ["Precio USDT"],
        ["Precio VES", "Precio COP"],
        ["Precio PEN", "vs BCV"],
        ["Historial del dia"]
    ]
    if chat_id == ADMIN_ID:
        teclado.append(["Usuarios"])
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

historial = {
    'VES': deque(maxlen=1440),
    'COP': deque(maxlen=1440),
    'PEN': deque(maxlen=1440)
}
precio_apertura = {'VES': None, 'COP': None, 'PEN': None}

def guardar_historial(moneda, precio):
    historial[moneda].append(precio)
    if precio_apertura[moneda] is None:
        precio_apertura[moneda] = precio

def obtener_analisis_dia(moneda):
    if not historial[moneda]:
        return None
    precios = list(historial[moneda])
    if len(precios) < 2:
        return None
    precio_actual = precios[-1]
    precio_inicio = precios[0]
    cambio = precio_actual - precio_inicio
    cambio_porcentaje = (cambio / precio_inicio) * 100 if precio_inicio != 0 else 0
    precio_max = max(precios)
    precio_min = min(precios)
    tendencia = "Alcista" if len(precios) > 10 and precios[-1] > precios[-10] else "Bajista"
    if len(precios) > 10 and abs(precios[-1] - precios[-10]) < 0.01:
        tendencia = "Lateral"
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
            direccion = "SUBIÓ" if precio_actual > ultimos_precios[moneda] else "BAJÓ"
            signo = "+" if precio_actual > ultimos_precios[moneda] else ""
            mensaje = f"""
ALERTA {moneda}

{direccion} en {signo}{cambio:.2f}

Anterior: {ultimos_precios[moneda]:.2f}
Actual: {precio_actual:.2f}
Cambio: {signo}{cambio:.2f}

{datetime.now().strftime('%H:%M:%S')}
"""
            for usuario in usuarios:
                try:
                    enviar_mensaje(usuario, mensaje)
                    time.sleep(0.05)
                except:
                    pass
            print(f"Alerta {moneda} enviada")
            ultimos_precios[moneda] = precio_actual

def test_alerta(chat_id):
    usuarios = obtener_usuarios()
    if not usuarios:
        enviar_mensaje(chat_id, "No hay usuarios registrados")
        return
    mensaje = f"""
ALERTA DE PRUEBA

Esta es una alerta de prueba
Enviada a {len(usuarios)} usuarios
{datetime.now().strftime('%H:%M:%S')}
"""
    enviados = 0
    for usuario in usuarios:
        try:
            enviar_mensaje(usuario, mensaje)
            enviados += 1
            time.sleep(0.05)
        except:
            pass
    enviar_mensaje(chat_id, f"Alerta enviada a {enviados} usuarios")

def mostrar_precios(chat_id, moneda=None):
    precios = {}
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_p2p_reales(m)
        if compra and venta:
            precios[m] = {'compra': compra, 'venta': venta}
    if not precios:
        enviar_mensaje(chat_id, "Obteniendo precios...", crear_teclado(chat_id))
        return
    tasas = obtener_tasas()
    if moneda == 'USDT' or moneda is None:
        mensaje = f"PRECIOS USDT P2P\n{datetime.now().strftime('%H:%M:%S')}\n\n"
        for m, datos in precios.items():
            mensaje += f"{m}\n"
            mensaje += f"  COMPRA: {datos['compra']:.2f}\n"
            mensaje += f"  VENTA: {datos['venta']:.2f}\n"
            mensaje += f"  Spread: {datos['compra']-datos['venta']:.2f}\n\n"
        if tasas:
            mensaje += f"TASA DE CAMBIO\n"
            mensaje += f"  USD: {tasas['usd']:.2f} Bs\n"
            mensaje += f"  EUR: {tasas['eur']:.2f} Bs\n"
        enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))
    elif moneda in precios:
        datos = precios[moneda]
        mensaje = f"PRECIO {moneda}\n{datetime.now().strftime('%H:%M:%S')}\n\n"
        mensaje += f"COMPRA: {datos['compra']:.2f}\n"
        mensaje += f"VENTA: {datos['venta']:.2f}\n"
        mensaje += f"Spread: {datos['compra']-datos['venta']:.2f}\n"
        analisis = obtener_analisis_dia(moneda)
        if analisis:
            mensaje += f"\nAnalisis del dia:\n"
            mensaje += f"  Apertura: {analisis['apertura']:.2f}\n"
            mensaje += f"  Cambio: {analisis['cambio']:+.2f}\n"
            mensaje += f"  Tendencia: {analisis['tendencia']}\n"
        if moneda == 'VES' and tasas:
            diff = datos['compra'] - tasas['usd']
            pct = (diff / tasas['usd']) * 100 if tasas['usd'] > 0 else 0
            mensaje += f"\nvs BCV: {diff:+.2f} Bs ({pct:+.1f}%)"
        enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))

def mostrar_vs_bcv(chat_id):
    compra, venta = obtener_precios_p2p_reales('VES')
    if not compra or not venta:
        enviar_mensaje(chat_id, "Obteniendo precios...", crear_teclado(chat_id))
        return
    tasas = obtener_tasas()
    if not tasas:
        enviar_mensaje(chat_id, "Obteniendo tasas...", crear_teclado(chat_id))
        return
    diff_compra = compra - tasas['usd']
    diff_venta = venta - tasas['usd']
    pct_compra = (diff_compra / tasas['usd']) * 100 if tasas['usd'] > 0 else 0
    pct_venta = (diff_venta / tasas['usd']) * 100 if tasas['usd'] > 0 else 0
    mensaje = f"USDT vs BCV\n{datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"BCV USD: {tasas['usd']:.2f} Bs\n\n"
    mensaje += f"COMPRA USDT: {compra:.2f} Bs\n"
    mensaje += f"  Diferencia: {diff_compra:+.2f} Bs\n\n"
    mensaje += f"VENTA USDT: {venta:.2f} Bs\n"
    mensaje += f"  Diferencia: {diff_venta:+.2f} Bs\n"
    analisis = obtener_analisis_dia('VES')
    if analisis:
        mensaje += f"\nDia: {analisis['cambio']:+.2f} Bs"
    enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))

def mostrar_historial(chat_id):
    mensaje = f"HISTORIAL DEL DIA\n{datetime.now().strftime('%H:%M:%S')}\n{datetime.now().strftime('%d/%m/%Y')}\n\n"
    for moneda in ['VES', 'COP', 'PEN']:
        analisis = obtener_analisis_dia(moneda)
        if analisis:
            mensaje += f"{moneda}\n"
            mensaje += f"  Apertura: {analisis['apertura']:.2f}\n"
            mensaje += f"  Actual: {analisis['actual']:.2f}\n"
            mensaje += f"  Cambio: {analisis['cambio']:+.2f}\n"
            mensaje += f"  Maximo: {analisis['maximo']:.2f}\n"
            mensaje += f"  Minimo: {analisis['minimo']:.2f}\n"
            mensaje += f"  Tendencia: {analisis['tendencia']}\n\n"
        else:
            mensaje += f"{moneda}: Sin datos\n\n"
    enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))

def procesar_mensaje(chat_id, texto):
    print(f"Mensaje: {texto}")
    guardar_usuario(chat_id)
    
    if texto == '/start':
        mensaje = f"""
BOT USDT P2P

Alertas para todos
Usuarios: {len(usuarios_activos)}

Botones:
Precio USDT - Todas las monedas
Precio VES - Solo VES
Precio COP - Solo COP
Precio PEN - Solo PEN
vs BCV - Comparacion
Historial del dia - Analisis completo
"""
        enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))
    
    elif texto == '/test_alerta':
        if chat_id == ADMIN_ID:
            test_alerta(chat_id)
        else:
            enviar_mensaje(chat_id, "Solo administrador")
    
    elif texto == 'Precio USDT' or texto == '/precios':
        mostrar_precios(chat_id, 'USDT')
    
    elif texto == 'Precio VES' or texto == '/ves':
        mostrar_precios(chat_id, 'VES')
    
    elif texto == 'Precio COP' or texto == '/cop':
        mostrar_precios(chat_id, 'COP')
    
    elif texto == 'Precio PEN' or texto == '/pen':
        mostrar_precios(chat_id, 'PEN')
    
    elif texto == 'vs BCV' or texto == '/bcv':
        mostrar_vs_bcv(chat_id)
    
    elif texto == 'Historial del dia' or texto == '/historial':
        mostrar_historial(chat_id)
    
    elif texto == 'Usuarios' or texto == '/usuarios':
        if chat_id == ADMIN_ID:
            usuarios = obtener_usuarios()
            if usuarios:
                mensaje = f"USUARIOS REGISTRADOS\n\nTotal: {len(usuarios)}\n\n"
                for uid in usuarios:
                    mensaje += f"- {uid}\n"
            else:
                mensaje = "No hay usuarios"
            enviar_mensaje(chat_id, mensaje, crear_teclado(chat_id))
    
    else:
        enviar_mensaje(chat_id, "Usa /start", crear_teclado(chat_id))

def recibir_mensajes():
    global ultimo_update_id
    while True:
        try:
            url = URL_TELEGRAM + "getUpdates"
            params = {'offset': ultimo_update_id + 1, 'timeout': 10}
            response = requests.get(url, params=params, timeout=15)
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
            print(f"Error: {e}")
            time.sleep(5)

def actualizar_precios():
    while True:
        try:
            print(f"Actualizando... {datetime.now().strftime('%H:%M:%S')}")
            precios = {}
            for moneda in ['VES', 'COP', 'PEN']:
                compra, venta = obtener_precios_p2p_reales(moneda)
                if compra and venta:
                    precios[moneda] = {'compra': compra, 'venta': venta}
                    guardar_historial(moneda, compra)
            if precios:
                verificar_alertas(precios)
            time.sleep(60)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

@app.route('/')
def home():
    return f"Bot activo\nUsuarios: {len(usuarios_activos)}"

if __name__ == "__main__":
    print("Bot iniciando en Railway...")
    cargar_usuarios()
    print(f"Usuarios: {len(usuarios_activos)}")
    
    threading.Thread(target=recibir_mensajes, daemon=True).start()
    threading.Thread(target=actualizar_precios, daemon=True).start()
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)