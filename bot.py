import requests
import time
import os
from datetime import datetime
from flask import Flask, request

TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not TOKEN or not ADMIN_ID:
    print("❌ FALTAN VARIABLES")
    print("TELEGRAM_TOKEN:", "✅" if TOKEN else "❌")
    print("ADMIN_ID:", "✅" if ADMIN_ID else "❌")
    exit(1)

ADMIN_ID = int(ADMIN_ID)
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"
app = Flask(__name__)

def enviar_mensaje(chat_id, texto):
    try:
        url = URL_TELEGRAM + "sendMessage"
        data = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}
        requests.post(url, json=data, timeout=10)
        return True
    except:
        return False

def obtener_precios(fiat):
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        
        data = {"asset": "USDT", "fiat": fiat, "tradeType": "SELL", "page": 1, "rows": 5, "payTypes": []}
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
        
        data = {"asset": "USDT", "fiat": fiat, "tradeType": "BUY", "page": 1, "rows": 5, "payTypes": []}
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

def procesar(chat_id, texto):
    print(f"📩 {texto}")
    
    if texto == '/start':
        enviar_mensaje(chat_id, "🤖 Bot activo!\n\n/comandos:\n/precios - Ver precios")
    
    elif texto == '/precios':
        mensaje = f"💰 *PRECIOS USDT*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        for m in ['VES', 'COP', 'PEN']:
            compra, venta = obtener_precios(m)
            if compra and venta:
                mensaje += f"*{m}*\n  🟢 COMPRA: {compra:.2f}\n  🔴 VENTA: {venta:.2f}\n\n"
            else:
                mensaje += f"*{m}*: ❌ No disponible\n\n"
        enviar_mensaje(chat_id, mensaje)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        if data and 'message' in data:
            chat_id = data['message']['chat']['id']
            texto = data['message'].get('text', '')
            if chat_id and texto:
                procesar(chat_id, texto)
        return "OK", 200
    except:
        return "Error", 500

@app.route('/')
def home():
    return "✅ Bot activo"

if __name__ == "__main__":
    print("🚀 Bot iniciando...")
    print(f"TOKEN: {'✅' if TOKEN else '❌'}")
    print(f"ADMIN_ID: {'✅' if ADMIN_ID else '❌'}")
    
    # Probar una vez
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios(m)
        if compra and venta:
            print(f"✅ {m}: {compra:.2f} / {venta:.2f}")
        else:
            print(f"❌ {m}: No disponible")
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)