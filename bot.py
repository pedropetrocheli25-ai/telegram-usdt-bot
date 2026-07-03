import requests
import time
import os
import threading
from datetime import datetime
from flask import Flask

# ==================== CONFIGURACIÓN ====================
TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not TOKEN or not ADMIN_ID:
    print("❌ FALTAN VARIABLES")
    exit(1)

ADMIN_ID = int(ADMIN_ID)
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"
ultimo_update_id = 0

app = Flask(__name__)

# ==================== FUNCIONES ====================

def enviar_mensaje(chat_id, texto):
    try:
        url = URL_TELEGRAM + "sendMessage"
        data = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}
        requests.post(url, json=data, timeout=10)
        return True
    except Exception as e:
        print(f"❌ Error enviando: {e}")
        return False

def obtener_precios(fiat):
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        
        # COMPRA
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
        
        # VENTA
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
    print(f"📩 {texto}")
    
    if texto == '/start':
        mensaje = """
🤖 *BOT USDT P2P ACTIVO* 🚀

📝 *Comandos:*
/precios - Ver precios
/tasas - Ver tasa BCV
"""
        enviar_mensaje(chat_id, mensaje)
    
    elif texto == '/precios':
        mensaje = f"💰 *PRECIOS USDT P2P*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
        for m in ['VES', 'COP', 'PEN']:
            compra, venta = obtener_precios(m)
            if compra and venta:
                mensaje += f"*{m}*\n"
                mensaje += f"  🟢 COMPRA: {compra:.2f}\n"
                mensaje += f"  🔴 VENTA: {venta:.2f}\n\n"
            else:
                mensaje += f"*{m}*: ❌ No disponible\n\n"
        enviar_mensaje(chat_id, mensaje)
    
    elif texto == '/tasas':
        tasa = obtener_tasas()
        if tasa:
            mensaje = f"🏦 *TASA BCV*\n\n💵 USD: {tasa:.2f} Bs"
        else:
            mensaje = "❌ No disponible"
        enviar_mensaje(chat_id, mensaje)
    
    else:
        enviar_mensaje(chat_id, "❓ Usa /start para ver comandos")

# ==================== POLLING (RECIBIR MENSAJES) ====================

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
                                threading.Thread(target=procesar_comando, args=(chat_id, texto)).start()
            
            time.sleep(1)
            
        except Exception as e:
            print(f"❌ Error polling: {e}")
            time.sleep(5)

# ==================== FLASK (MANTENER ACTIVO) ====================

@app.route('/')
def home():
    return "✅ Bot activo 24/7"

# ==================== MAIN ====================

if __name__ == "__main__":
    print("🚀 Bot iniciando en Railway...")
    print(f"✅ TOKEN: {'Configurado' if TOKEN else 'FALTANTE'}")
    print(f"✅ ADMIN_ID: {ADMIN_ID if ADMIN_ID else 'FALTANTE'}")
    
    # Probar precios
    print("\n📊 Probando conexión...")
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios(m)
        if compra and venta:
            print(f"  ✅ {m}: {compra:.2f} / {venta:.2f}")
        else:
            print(f"  ❌ {m}: No disponible")
    
    # Iniciar hilo de polling
    print("\n🔄 Iniciando polling...")
    threading.Thread(target=recibir_mensajes, daemon=True).start()
    
    print("✅ Bot listo! Esperando mensajes...")
    print("📱 Envía /start a tu bot en Telegram")
    print("=" * 40)
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)