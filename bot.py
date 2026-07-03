import requests
import time
import threading
import os
from datetime import datetime
from flask import Flask, request

# ==================== CONFIGURACIÓN ====================
TOKEN = os.environ.get('TELEGRAM_TOKEN')
ID_ADMIN = int(os.environ.get('ADMIN_ID', 0))

if not TOKEN or not ID_ADMIN:
    print("❌ ERROR: Configura TELEGRAM_TOKEN y ADMIN_ID")
    exit(1)

URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"
ultimo_update_id = 0

app = Flask(__name__)

# ==================== PRECIOS P2P REALES ====================

def obtener_precios_p2p_reales(fiat):
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Content-Type': 'application/json'
        }
        
        # COMPRA
        data = {
            "asset": "USDT",
            "fiat": fiat,
            "tradeType": "SELL",
            "page": 1,
            "rows": 10,
            "payTypes": []
        }
        precio_compra = None
        try:
            response = requests.post(url, json=data, headers=headers, timeout=10)
            if response.status_code == 200:
                result = response.json()
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
                        precio_compra = min(precios)
        except:
            pass
        
        # VENTA
        data = {
            "asset": "USDT",
            "fiat": fiat,
            "tradeType": "BUY",
            "page": 1,
            "rows": 10,
            "payTypes": []
        }
        precio_venta = None
        try:
            response = requests.post(url, json=data, headers=headers, timeout=10)
            if response.status_code == 200:
                result = response.json()
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
                        precio_venta = max(precios)
        except:
            pass
        
        if precio_compra is None or precio_venta is None:
            return None, None
        
        if precio_compra < precio_venta:
            precio_compra, precio_venta = precio_venta, precio_compra
        
        return precio_compra, precio_venta
    except:
        return None, None

# ==================== TASAS DE CAMBIO ====================

def obtener_tasas():
    try:
        url = "https://api.exchangerate-api.com/v4/latest/USD"
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            usd_to_ves = data.get('rates', {}).get('VES', 0)
            eur_to_usd = data.get('rates', {}).get('EUR', 0)
            eur_to_ves = usd_to_ves * eur_to_usd if usd_to_ves > 0 else 0
            
            if usd_to_ves > 0:
                return {
                    'usd': usd_to_ves,
                    'eur': eur_to_ves,
                    'fecha': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'fuente': 'ExchangeRate API'
                }
    except:
        pass
    
    return None

# ==================== TELEGRAM ====================

def enviar_mensaje(chat_id, texto, teclado=None):
    try:
        url = URL_TELEGRAM + "sendMessage"
        data = {
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "Markdown"
        }
        if teclado:
            data["reply_markup"] = teclado
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    except:
        return False

def crear_teclado():
    teclado = {
        "keyboard": [
            ["💰 Precio USDT"],
            ["🇻🇪 Precio VES", "🇨🇴 Precio COP"],
            ["🇵🇪 Precio PEN", "📊 vs BCV"]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    return teclado

def mostrar_precios(chat_id, moneda=None):
    precios = {}
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_p2p_reales(m)
        if compra and venta:
            precios[m] = {'compra': compra, 'venta': venta}
    
    if not precios:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado())
        return
    
    tasas = obtener_tasas()
    
    if moneda == 'USDT' or moneda is None:
        mensaje = f"💰 *PRECIOS USDT P2P BINANCE*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        
        for m, datos in precios.items():
            mensaje += f"*{m}*\n"
            mensaje += f"  🟢 COMPRA: {datos['compra']:.2f}\n"
            mensaje += f"  🔴 VENTA: {datos['venta']:.2f}\n"
            mensaje += f"  📊 Spread: {datos['compra']-datos['venta']:.2f}\n\n"
        
        if tasas:
            mensaje += f"🏦 *TASA DE CAMBIO*\n"
            mensaje += f"  💵 USD: {tasas['usd']:.2f} Bs\n"
            mensaje += f"  💶 EUR: {tasas['eur']:.2f} Bs\n"
            mensaje += f"  📌 {tasas['fuente']}"
        
        enviar_mensaje(chat_id, mensaje, crear_teclado())
    
    elif moneda in precios:
        datos = precios[moneda]
        mensaje = f"💰 *PRECIO {moneda}* 🏦\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        mensaje += f"🟢 *COMPRA:* {datos['compra']:.2f}\n"
        mensaje += f"🔴 *VENTA:* {datos['venta']:.2f}\n"
        mensaje += f"📊 *Spread:* {datos['compra']-datos['venta']:.2f}\n"
        
        if moneda == 'VES' and tasas:
            diff = datos['compra'] - tasas['usd']
            pct = (diff / tasas['usd']) * 100 if tasas['usd'] > 0 else 0
            mensaje += f"\n📊 *vs BCV:*\n"
            mensaje += f"  Diferencia: {diff:+.2f} Bs\n"
            mensaje += f"  Porcentaje: {pct:+.1f}%"
        
        enviar_mensaje(chat_id, mensaje, crear_teclado())
    
    else:
        enviar_mensaje(chat_id, "❌ Moneda no disponible", crear_teclado())

def mostrar_vs_bcv(chat_id):
    compra, venta = obtener_precios_p2p_reales('VES')
    if not compra or not venta:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado())
        return
    
    tasas = obtener_tasas()
    if not tasas:
        enviar_mensaje(chat_id, "⏳ Obteniendo tasas...", crear_teclado())
        return
    
    diff_compra = compra - tasas['usd']
    diff_venta = venta - tasas['usd']
    pct_compra = (diff_compra / tasas['usd']) * 100 if tasas['usd'] > 0 else 0
    pct_venta = (diff_venta / tasas['usd']) * 100 if tasas['usd'] > 0 else 0
    
    mensaje = f"📊 *USDT vs BCV* 🏦\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"💵 *BCV USD:* {tasas['usd']:.2f} Bs\n\n"
    
    mensaje += f"🟢 *COMPRA USDT:* {compra:.2f} Bs\n"
    mensaje += f"  Diferencia: {diff_compra:+.2f} Bs\n"
    mensaje += f"  Porcentaje: {pct_compra:+.1f}%\n\n"
    
    mensaje += f"🔴 *VENTA USDT:* {venta:.2f} Bs\n"
    mensaje += f"  Diferencia: {diff_venta:+.2f} Bs\n"
    mensaje += f"  Porcentaje: {pct_venta:+.1f}%\n"
    
    mensaje += f"\n📊 *Spread USDT:* {compra-venta:.2f} Bs"
    
    enviar_mensaje(chat_id, mensaje, crear_teclado())

# ==================== WEBHOOK ====================

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        if data and 'message' in data:
            chat_id = data['message']['chat']['id']
            texto = data['message'].get('text', '')
            
            if chat_id and texto:
                # Procesar en segundo plano
                threading.Thread(target=procesar_mensaje, args=(chat_id, texto)).start()
        
        return "OK", 200
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return "Error", 500

@app.route('/')
def home():
    return "🤖 Bot USDT P2P + BCV - Activo 24/7"

# ==================== PROCESAR MENSAJES ====================

def procesar_mensaje(chat_id, texto):
    print(f"📩 {texto}")
    
    texto_limpio = texto.replace("💰", "").replace("🇻🇪", "").replace("🇨🇴", "").replace("🇵🇪", "").replace("📊", "").strip()
    
    if texto == '/start':
        mensaje = """
🤖 *BOT USDT P2P + BCV* 🚀

📊 *Precios en tiempo real:*
💰 Precios EXACTOS de Binance P2P
🏦 Tasas de cambio actualizadas

📱 *Usa los botones:*
• Precio USDT - Todas las monedas
• Precio VES/COP/PEN - Cada moneda
• vs BCV - Comparación con BCV
"""
        enviar_mensaje(chat_id, mensaje, crear_teclado())
    
    elif texto == '💰 Precio USDT' or texto == '/precios':
        mostrar_precios(chat_id, 'USDT')
    
    elif texto == '🇻🇪 Precio VES' or texto == '/ves':
        mostrar_precios(chat_id, 'VES')
    
    elif texto == '🇨🇴 Precio COP' or texto == '/cop':
        mostrar_precios(chat_id, 'COP')
    
    elif texto == '🇵🇪 Precio PEN' or texto == '/pen':
        mostrar_precios(chat_id, 'PEN')
    
    elif texto == '📊 vs BCV' or texto == '/bcv':
        mostrar_vs_bcv(chat_id)
    
    else:
        enviar_mensaje(chat_id, "❓ Usa los botones o /start", crear_teclado())

# ==================== MAIN ====================

if __name__ == "__main__":
    print("🚀 Bot iniciando en Railway...")
    print("=" * 40)
    print(f"✅ TOKEN: {'Configurado' if TOKEN else 'FALTANTE'}")
    print(f"✅ ADMIN_ID: {ID_ADMIN if ID_ADMIN else 'FALTANTE'}")
    
    # Probar precios
    print("\n📊 Probando conexión a Binance...")
    for moneda in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_p2p_reales(moneda)
        if compra and venta:
            print(f"  ✅ {moneda}: {compra:.2f} / {venta:.2f}")
        else:
            print(f"  ❌ {moneda}: No disponible")
    
    print("\n✅ Bot listo en Railway")
    print("📱 Botones disponibles en Telegram")
    print("=" * 40)
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)