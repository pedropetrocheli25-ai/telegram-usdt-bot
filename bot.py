import requests
import time
import os
import threading
from datetime import datetime
from flask import Flask

TOKEN = os.environ.get('TELEGRAM_TOKEN')

if not TOKEN:
    print("ERROR: TELEGRAM_TOKEN no configurado")
    exit(1)

URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"
ultimo_update_id = 0

app = Flask(__name__)

def enviar_mensaje(chat_id, texto):
    try:
        url = URL_TELEGRAM + "sendMessage"
        data = {"chat_id": chat_id, "text": texto}
        response = requests.post(url, json=data, timeout=10)
        print(f"✅ Mensaje enviado a {chat_id}")
        return True
    except Exception as e:
        print(f"❌ Error enviando: {e}")
        return False

def obtener_precios(fiat):
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        
        data = {"asset": "USDT", "fiat": fiat, "tradeType": "SELL", "page": 1, "rows": 3, "payTypes": []}
        compra = None
        try:
            r = requests.post(url, json=data, headers=headers, timeout=10)
            if r.status_code == 200:
                result = r.json()
                if result.get('data') and len(result['data']) > 0:
                    compra = float(result['data'][0]['adv']['price'])
        except:
            pass
        
        data = {"asset": "USDT", "fiat": fiat, "tradeType": "BUY", "page": 1, "rows": 3, "payTypes": []}
        venta = None
        try:
            r = requests.post(url, json=data, headers=headers, timeout=10)
            if r.status_code == 200:
                result = r.json()
                if result.get('data') and len(result['data']) > 0:
                    venta = float(result['data'][0]['adv']['price'])
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
            if usd > 0:
                return usd
    except:
        pass
    return None

def procesar_comando(chat_id, texto):
    print(f"📩 Mensaje recibido: {texto}")
    
    if texto == '/start':
        mensaje = "🤖 BOT USDT ACTIVO!\n\nComandos:\n/precios - Ver precios\n/tasas - Ver tasa BCV"
        enviar_mensaje(chat_id, mensaje)
    
    elif texto == '/precios':
        mensaje = f"💰 PRECIOS USDT P2P\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        for m in ['VES', 'COP', 'PEN']:
            compra, venta = obtener_precios(m)
            if compra and venta:
                mensaje += f"*{m}*\n  COMPRA: {compra:.2f}\n  VENTA: {venta:.2f}\n\n"
            else:
                mensaje += f"*{m}*: ❌ No disponible\n\n"
        
        tasa = obtener_tasas()
        if tasa:
            mensaje += f"🏦 TASA BCV\nUSD: {tasa:.2f} Bs"
        
        enviar_mensaje(chat_id, mensaje)
    
    elif texto == '/tasas':
        tasa = obtener_tasas()
        if tasa:
            mensaje = f"🏦 TASA BCV\nUSD: {tasa:.2f} Bs"
        else:
            mensaje = "❌ Tasa no disponible"
        enviar_mensaje(chat_id, mensaje)
    
    else:
        enviar_mensaje(chat_id, "❓ Usa /start para comandos")

def polling():
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
                                print(f"📨 Procesando: {texto}")
                                threading.Thread(target=procesar_comando, args=(chat_id, texto)).start()
            
            time.sleep(1)
            
        except Exception as e:
            print(f"❌ Error polling: {e}")
            time.sleep(5)

@app.route('/')
def home():
    return "Bot activo"

if __name__ == "__main__":
    print("🚀 Bot iniciando en Railway...")
    print(f"✅ TOKEN: {'Configurado' if TOKEN else 'FALTANTE'}")
    
    print("\n📊 Probando conexión a Binance...")
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios(m)
        if compra and venta:
            print(f"  ✅ {m}: {compra:.2f} / {venta:.2f}")
        else:
            print(f"  ❌ {m}: No disponible")
    
    print("\n🔄 Iniciando polling...")
    threading.Thread(target=polling, daemon=True).start()
    
    print("✅ Bot listo! Esperando mensajes...")
    print("📱 Envía /start a tu bot en Telegram")
    print("=" * 40)
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)