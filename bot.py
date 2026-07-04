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
        print(f"❌ Error: {e}")
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

def procesar_mensaje(chat_id, texto):
    print(f"📩 Mensaje: {texto}")
    
    if texto == '/start':
        enviar_mensaje(chat_id, "🤖 Bot USDT Activo!\n\nComandos:\n/precios - Ver precios")
    
    elif texto == '/precios':
        mensaje = f"PRECIOS USDT P2P\n{datetime.now().strftime('%H:%M:%S')}\n\n"
        for m in ['VES', 'COP', 'PEN']:
            compra, venta = obtener_precios(m)
            if compra and venta:
                mensaje += f"{m}\n  COMPRA: {compra:.2f}\n  VENTA: {venta:.2f}\n\n"
            else:
                mensaje += f"{m}: No disponible\n\n"
        enviar_mensaje(chat_id, mensaje)
    
    else:
        enviar_mensaje(chat_id, "Usa /start para comandos")

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
            print(f"❌ Error: {e}")
            time.sleep(5)

@app.route('/')
def home():
    return "Bot activo"

if __name__ == "__main__":
    print("🚀 Bot iniciando...")
    print(f"✅ TOKEN: {'Configurado' if TOKEN else 'FALTANTE'}")
    
    print("\n📊 Probando conexión a Binance...")
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios(m)
        if compra and venta:
            print(f"  ✅ {m}: {compra:.2f} / {venta:.2f}")
        else:
            print(f"  ❌ {m}: No disponible")
    
    print("\n🔄 Iniciando polling...")
    threading.Thread(target=recibir_mensajes, daemon=True).start()
    
    print("✅ Bot listo!")
    print("📱 Envía /start a tu bot en Telegram")
    print("=" * 40)
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port) requests
import time
import os
from datetime import datetime
from flask import Flask, request

TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not TOKEN:
    print("ERROR: TELEGRAM_TOKEN no configurado")
    exit(1)

ADMIN_ID = int(ADMIN_ID) if ADMIN_ID else 0
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"
app = Flask(__name__)

def enviar_mensaje(chat_id, texto):
    try:
        url = URL_TELEGRAM + "sendMessage"
        data = {"chat_id": chat_id, "text": texto}
        response = requests.post(url, json=data, timeout=10)
        print(f"✅ Mensaje enviado a {chat_id}")
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Error: {e}")
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

def procesar_mensaje(chat_id, texto):
    print(f"📩 Mensaje: {texto} de {chat_id}")
    
    if texto == '/start':
        mensaje = "🤖 Bot USDT P2P Activo!\n\nComandos:\n/precios - Ver precios"
        enviar_mensaje(chat_id, mensaje)
    
    elif texto == '/precios':
        mensaje = f"💰 PRECIOS USDT P2P\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        for m in ['VES', 'COP', 'PEN']:
            compra, venta = obtener_precios(m)
            if compra and venta:
                mensaje += f"{m}\n  COMPRA: {compra:.2f}\n  VENTA: {venta:.2f}\n\n"
            else:
                mensaje += f"{m}: No disponible\n\n"
        enviar_mensaje(chat_id, mensaje)
    
    else:
        enviar_mensaje(chat_id, "Usa /start para comandos")

@app.route('/', methods=['GET'])
def home():
    return "Bot activo"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        if data and 'message' in data:
            chat_id = data['message']['chat']['id']
            texto = data['message'].get('text', '')
            if chat_id and texto:
                procesar_mensaje(chat_id, texto)
        return "OK", 200
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return "Error", 500

def configurar_webhook():
    """Configura el webhook en Telegram"""
    try:
        # Obtener la URL pública de Railway
        railway_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN')
        if not railway_url:
            # Si no está disponible, intentar con la URL por defecto
            railway_url = "https://telegram-usdt-bot.up.railway.app"
        
        webhook_url = f"{railway_url}/webhook"
        url = URL_TELEGRAM + "setWebhook"
        data = {"url": webhook_url}
        response = requests.post(url, json=data, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('ok'):
                print(f"✅ Webhook configurado: {webhook_url}")
                return True
            else:
                print(f"❌ Error configurando webhook: {result}")
                return False
        else:
            print(f"❌ Error HTTP: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Error configurando webhook: {e}")
        return False

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
    
    print("\n🔗 Configurando webhook...")
    configurar_webhook()
    
    print("✅ Bot listo! Esperando mensajes por webhook...")
    print("=" * 40)
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)