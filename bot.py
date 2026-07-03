import requests
import json
import time
import threading
from datetime import datetime, timedelta
from collections import deque

# ==================== CONFIGURACIÓN ====================
TOKEN = "8925407023:AAFcITHXtPYhNJ9-O4kZT73LaYpKtKp3pe4"  # Reemplaza con tu token de BotFather
ID_ADMIN = 1373859142  # Reemplaza con TU ID de Telegram

# URL de la API de Telegram
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"

# Umbrales de alerta por moneda
UMBRALES = {
    'VES': 1.0,
    'COP': 20.0,
    'PEN': 0.20
}

# Almacenamiento de precios
cache_precios = {}
ultimo_cache_time = None
historial_precios = {
    'VES': deque(maxlen=1440),
    'COP': deque(maxlen=1440),
    'PEN': deque(maxlen=1440)
}
ultimos_precios = {
    'VES': None,
    'COP': None,
    'PEN': None
}
precios_anterior = {
    'VES': None,
    'COP': None,
    'PEN': None
}
ultimo_update_id = 0

# ==================== FUNCIONES DE TELEGRAM ====================

def enviar_mensaje(chat_id, texto, parse_mode=None):
    """Envía un mensaje por Telegram"""
    try:
        url = URL_TELEGRAM + "sendMessage"
        datos = {
            'chat_id': chat_id,
            'text': texto,
            'parse_mode': parse_mode
        }
        response = requests.post(url, json=datos, timeout=10)
        return response.json()
    except Exception as e:
        print(f"❌ Error enviando mensaje: {e}")
        return None

def obtener_actualizaciones(offset=0):
    """Obtiene nuevos mensajes de Telegram"""
    try:
        url = URL_TELEGRAM + "getUpdates"
        params = {
            'offset': offset,
            'timeout': 30
        }
        response = requests.get(url, params=params, timeout=35)
        if response.status_code == 200:
            return response.json()
        else:
            return None
    except Exception as e:
        print(f"❌ Error obteniendo actualizaciones: {e}")
        return None

# ==================== FUNCIONES DE PRECIOS P2P CORREGIDAS ====================

def obtener_precios_p2p(fiat):
    """Obtiene precios REALES de COMPRA y VENTA de Binance P2P"""
    try:
        url_p2p = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/json'
        }
        
        # PRECIO DE COMPRA (tú compras USDT): Buscar SELL (anuncios de VENTA)
        # La gente vende USDT al precio que ELLOS quieren (tú pagas ese precio)
        data_compra = {
            "asset": "USDT",
            "fiat": fiat,
            "tradeType": "SELL",  # SELL = gente vendiendo USDT
            "page": 1,
            "rows": 10,
            "payTypes": []
        }
        
        # PRECIO DE VENTA (tú vendes USDT): Buscar BUY (anuncios de COMPRA)
        # La gente compra USDT al precio que ELLOS quieren (tú recibes ese precio)
        data_venta = {
            "asset": "USDT",
            "fiat": fiat,
            "tradeType": "BUY",  # BUY = gente comprando USDT
            "page": 1,
            "rows": 10,
            "payTypes": []
        }
        
        precio_compra = None  # Lo que TÚ pagas al comprar
        precio_venta = None   # Lo que TÚ recibes al vender
        precios_compra_lista = []
        precios_venta_lista = []
        
        # Obtener precio de COMPRA (el más BAJO entre los que venden)
        try:
            response = requests.post(url_p2p, json=data_compra, headers=headers, timeout=10)
            if response.status_code == 200:
                result = response.json()
                if result.get('data') and len(result['data']) > 0:
                    precios_compra_lista = [float(anuncio['adv']['price']) for anuncio in result['data']]
                    precio_compra = min(precios_compra_lista)  # El más bajo = mejor para ti
                    print(f"  📊 Precios de VENTA (anuncios): {precios_compra_lista[:5]}")
                    print(f"  ✅ Mejor precio para COMPRAR: {precio_compra:.2f}")
        except Exception as e:
            print(f"  ⚠️ Error obteniendo compra {fiat}: {e}")
        
        # Obtener precio de VENTA (el más ALTO entre los que compran)
        try:
            response = requests.post(url_p2p, json=data_venta, headers=headers, timeout=10)
            if response.status_code == 200:
                result = response.json()
                if result.get('data') and len(result['data']) > 0:
                    precios_venta_lista = [float(anuncio['adv']['price']) for anuncio in result['data']]
                    precio_venta = max(precios_venta_lista)  # El más alto = mejor para ti
                    print(f"  📊 Precios de COMPRA (anuncios): {precios_venta_lista[:5]}")
                    print(f"  ✅ Mejor precio para VENDER: {precio_venta:.2f}")
        except Exception as e:
            print(f"  ⚠️ Error obteniendo venta {fiat}: {e}")
        
        if precio_compra is None or precio_venta is None:
            print(f"  ❌ No se pudieron obtener precios para {fiat}")
            return None, None
        
        # EN LA VIDA REAL: 
        # COMPRA (tú pagas) > VENTA (tú recibes)
        # Ejemplo: Pagas 741 Bs por USDT, pero lo vendes a 704 Bs
        # El precio de compra SIEMPRE es mayor que el de venta
        if precio_compra < precio_venta:
            print(f"  ⚠️ Precios invertidos para {fiat}, corrigiendo...")
            # Intercambiar para que compra > venta
            precio_compra, precio_venta = precio_venta, precio_compra
        
        return precio_compra, precio_venta
            
    except Exception as e:
        print(f"❌ Error P2P {fiat}: {e}")
        return None, None

def obtener_precio_compra_venta_todas():
    """Obtiene precios de compra y venta para todas las monedas"""
    resultado = {}
    
    print("\n" + "="*50)
    print("📊 PRECIOS REALES DE BINANCE P2P")
    print("="*50)
    print("🟢 COMPRA = Precio que TÚ PAGAS al comprar USDT")
    print("🔴 VENTA = Precio que TÚ RECIBES al vender USDT")
    print("💡 NOTA: Siempre Compra > Venta (así funciona el mercado)")
    print("-" * 50)
    
    for moneda in ['VES', 'COP', 'PEN']:
        print(f"\n🔍 Buscando {moneda}...")
        print("-" * 30)
        
        compra, venta = obtener_precios_p2p(moneda)
        
        if compra is not None and venta is not None:
            # Asegurar que compra > venta (realidad del mercado)
            if compra < venta:
                compra, venta = venta, compra
            
            resultado[moneda] = {
                'compra': compra,
                'venta': venta,
                'diferencia': compra - venta,  # Spread positivo
                'porcentaje': ((compra - venta) / venta) * 100 if venta != 0 else 0
            }
            print(f"  ✅ {moneda}:")
            print(f"     🟢 COMPRA (pagas): {compra:.2f}")
            print(f"     🔴 VENTA (recibes): {venta:.2f}")
            print(f"     📊 Spread: {compra - venta:.2f}")
        else:
            resultado[moneda] = None
            print(f"  ❌ {moneda}: No disponible")
    
    print("="*50 + "\n")
    return resultado

def verificar_alerta(moneda, precio_actual):
    """Verifica si debe enviar alerta por cambio significativo"""
    global ultimos_precios
    
    if ultimos_precios[moneda] is None:
        ultimos_precios[moneda] = precio_actual
        return
    
    cambio = abs(precio_actual - ultimos_precios[moneda])
    umbral = UMBRALES.get(moneda, 0)
    
    if cambio >= umbral:
        direccion = "📈 SUBIÓ" if precio_actual > ultimos_precios[moneda] else "📉 BAJÓ"
        cambio_signo = "+" if precio_actual > ultimos_precios[moneda] else ""
        cambio_porcentaje = ((precio_actual - ultimos_precios[moneda]) / ultimos_precios[moneda] * 100) if ultimos_precios[moneda] != 0 else 0
        
        mensaje = f"""
⚠️ *ALERTA DE PRECIO {moneda}* ⚠️

{direccion} en {cambio_signo}{cambio:.2f} {moneda}

Precio anterior: {ultimos_precios[moneda]:.2f} {moneda}
Precio actual: {precio_actual:.2f} {moneda}

📊 Porcentaje: {cambio_signo}{cambio_porcentaje:.2f}%
🕐 Hora: {datetime.now().strftime('%H:%M:%S')}
"""
        
        enviar_mensaje(ID_ADMIN, mensaje, parse_mode='Markdown')
        print(f"✅ Alerta enviada para {moneda}")
        
        ultimos_precios[moneda] = precio_actual

def actualizar_datos():
    """Actualiza los precios en segundo plano"""
    global cache_precios, ultimo_cache_time, precios_anterior
    
    while True:
        try:
            print(f"\n🔄 Actualizando precios... {datetime.now().strftime('%H:%M:%S')}")
            
            precios = obtener_precio_compra_venta_todas()
            
            if precios:
                cache_precios = precios
                ultimo_cache_time = datetime.now()
                
                for moneda in ['VES', 'COP', 'PEN']:
                    if precios.get(moneda) and precios[moneda] is not None:
                        # Usar el precio de compra para las alertas
                        precio_actual = precios[moneda]['compra']
                        historial_precios[moneda].append(precio_actual)
                        
                        if precios_anterior[moneda] is not None:
                            cambio_abs = abs(precio_actual - precios_anterior[moneda])
                            if cambio_abs >= UMBRALES.get(moneda, 0):
                                verificar_alerta(moneda, precio_actual)
                        
                        precios_anterior[moneda] = precio_actual
                
                print("✅ Precios actualizados correctamente")
            else:
                print("⚠️ No se obtuvieron precios válidos")
            
            time.sleep(60)
            
        except Exception as e:
            print(f"❌ Error en actualización: {e}")
            time.sleep(60)

def analizar_movimiento_diario(moneda):
    """Analiza el movimiento del precio en el día"""
    if not historial_precios[moneda]:
        return "No hay datos suficientes para análisis"
    
    precios = list(historial_precios[moneda])
    
    if len(precios) < 2:
        return "No hay suficientes datos para análisis"
    
    precio_actual = precios[-1]
    precio_24h = precios[0] if len(precios) >= 1440 else precios[0]
    
    cambio = precio_actual - precio_24h
    cambio_porcentaje = (cambio / precio_24h) * 100 if precio_24h != 0 else 0
    
    precio_max = max(precios)
    precio_min = min(precios)
    
    volatilidad = ((precio_max - precio_min) / precio_actual) * 100 if precio_actual != 0 else 0
    
    ultimos_30min = precios[-30:] if len(precios) >= 30 else precios
    tendencia = "↗️ Alcista" if len(ultimos_30min) > 1 and ultimos_30min[-1] > ultimos_30min[0] else "↘️ Bajista"
    
    return {
        'precio_actual': precio_actual,
        'precio_24h': precio_24h,
        'cambio': cambio,
        'cambio_porcentaje': cambio_porcentaje,
        'precio_max': precio_max,
        'precio_min': precio_min,
        'volatilidad': volatilidad,
        'tendencia': tendencia
    }

# ==================== MANEJADORES DE COMANDOS ====================

def procesar_comando(chat_id, texto):
    """Procesa los comandos del bot"""
    
    print(f"📥 Procesando comando: {texto} de {chat_id}")
    
    if texto == '/start' or texto == '/help':
        mensaje = """
🤖 *BOT DE PRECIOS USDT P2P BINANCE* 🤖

📊 *¿Qué significan los precios?*

🟢 *COMPRA*: Precio que TÚ PAGAS por 1 USDT
   (Siempre es el más alto)

🔴 *VENTA*: Precio que TÚ RECIBES por 1 USDT
   (Siempre es el más bajo)

💡 *Ejemplo VES real:*
- COMPRA: 741 Bs (pagas 741 Bs por 1 USDT)
- VENTA: 704 Bs (recibes 704 Bs por 1 USDT)
- Diferencia: 37 Bs (ganancia del exchange)

📝 *Comandos:*
/precios - Ver precios exactos actuales
/analisis - Ver análisis diario
/alertas - Ver configuración
/estado - Ver estado del bot

🔄 Actualización cada 60 segundos
"""
        enviar_mensaje(chat_id, mensaje, parse_mode='Markdown')
        
    elif texto == '/precios':
        try:
            if cache_precios and ultimo_cache_time:
                precios = cache_precios
                timestamp = ultimo_cache_time.strftime('%Y-%m-%d %H:%M:%S')
            else:
                precios = obtener_precio_compra_venta_todas()
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            mensaje = f"💰 *PRECIOS USDT P2P BINANCE* 💰\n"
            mensaje += f"🕐 Actualizado: {timestamp}\n\n"
            
            for moneda, datos in precios.items():
                if datos and datos is not None:
                    mensaje += f"*{moneda}*\n"
                    mensaje += f"  🟢 COMPRA (pagas): {datos['compra']:.2f}\n"
                    mensaje += f"  🔴 VENTA (recibes): {datos['venta']:.2f}\n"
                    mensaje += f"  📊 Spread: {datos['diferencia']:.2f} ({datos['porcentaje']:.2f}%)\n\n"
                else:
                    mensaje += f"*{moneda}*: ⚠️ No disponible\n\n"
            
            mensaje += """
💡 *Interpretación:*
• COMPRA = Lo que PAGAS por 1 USDT
• VENTA = Lo que RECIBES por 1 USDT
• Mayor spread = Mayor ganancia para el exchange
"""
            
            enviar_mensaje(chat_id, mensaje, parse_mode='Markdown')
            
        except Exception as e:
            enviar_mensaje(chat_id, f"❌ Error: {e}")
    
    elif texto == '/analisis':
        try:
            mensaje = "📊 *ANÁLISIS DIARIO* 📊\n"
            mensaje += f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            
            for moneda in ['VES', 'COP', 'PEN']:
                analisis = analizar_movimiento_diario(moneda)
                
                if isinstance(analisis, str):
                    mensaje += f"*{moneda}*: {analisis}\n\n"
                    continue
                
                mensaje += f"*{moneda}*\n"
                mensaje += f"  💰 Actual: {analisis['precio_actual']:.2f}\n"
                mensaje += f"  📅 Hace 24h: {analisis['precio_24h']:.2f}\n"
                
                cambio_emoji = "📈" if analisis['cambio'] > 0 else "📉"
                mensaje += f"  {cambio_emoji} Cambio: {analisis['cambio']:+.2f} ({analisis['cambio_porcentaje']:+.2f}%)\n"
                mensaje += f"  📈 Máx: {analisis['precio_max']:.2f}\n"
                mensaje += f"  📉 Mín: {analisis['precio_min']:.2f}\n"
                mensaje += f"  🧭 Tendencia: {analisis['tendencia']}\n\n"
            
            enviar_mensaje(chat_id, mensaje, parse_mode='Markdown')
            
        except Exception as e:
            enviar_mensaje(chat_id, f"❌ Error: {e}")
    
    elif texto == '/alertas':
        mensaje = """
🔔 *CONFIGURACIÓN DE ALERTAS*

Umbrales de cambio:
• VES: ±1.00 Bs
• COP: ±20.00 COP  
• PEN: ±0.20 PEN

📊 *Últimos precios guardados:*
"""
        for moneda in ['VES', 'COP', 'PEN']:
            if ultimos_precios[moneda] is not None:
                mensaje += f"• {moneda}: {ultimos_precios[moneda]:.2f}\n"
            else:
                mensaje += f"• {moneda}: Sin datos\n"
        
        enviar_mensaje(chat_id, mensaje, parse_mode='Markdown')
    
    elif texto == '/estado':
        mensaje = f"""
📊 *ESTADO DEL BOT*

✅ Bot activo
🔄 Actualización: Activa (cada 60s)
📦 Monedas: {len(cache_precios)}
🕐 Última actualización: {ultimo_cache_time.strftime('%H:%M:%S') if ultimo_cache_time else 'Nunca'}

📈 *Precios actuales:*
"""
        for moneda, datos in cache_precios.items():
            if datos:
                mensaje += f"• {moneda}: C:{datos['compra']:.2f} / V:{datos['venta']:.2f}\n"
        
        enviar_mensaje(chat_id, mensaje, parse_mode='Markdown')

# ==================== BUCLE PRINCIPAL ====================

def main():
    global ultimo_update_id
    
    print("🚀 Iniciando Bot de Precios USDT P2P...")
    print("📱 Bot creado para Pydroid 3")
    print("📊 Obteniendo precios REALES de Binance P2P")
    print("💡 COMPRA > VENTA (así funciona el mercado)")
    
    # Verificar que el bot está activo
    try:
        test_url = URL_TELEGRAM + "getMe"
        response = requests.get(test_url)
        if response.status_code == 200:
            bot_info = response.json()
            print(f"✅ Bot conectado: @{bot_info['result']['username']}")
        else:
            print(f"❌ Error conectando bot")
            return
    except Exception as e:
        print(f"❌ Error de conexión: {e}")
        return
    
    # Iniciar hilo de actualización
    hilo_actualizacion = threading.Thread(target=actualizar_datos, daemon=True)
    hilo_actualizacion.start()
    
    print("✅ Bot iniciado correctamente")
    print("🤖 Esperando comandos en Telegram...")
    print("📝 Comandos: /precios, /analisis, /alertas, /estado")
    print("⚠️ Envía /start a tu bot en Telegram")
    
    # Bucle principal para recibir mensajes
    while True:
        try:
            updates = obtener_actualizaciones(ultimo_update_id + 1)
            
            if updates and updates.get('ok'):
                resultados = updates.get('result', [])
                if resultados:
                    print(f"📨 Recibidos {len(resultados)} mensajes")
                
                for update in resultados:
                    ultimo_update_id = update.get('update_id', 0)
                    message = update.get('message')
                    
                    if message:
                        chat_id = message.get('chat', {}).get('id')
                        texto = message.get('text', '')
                        
                        if chat_id and texto:
                            print(f"📩 Mensaje de {chat_id}: {texto}")
                            threading.Thread(
                                target=procesar_comando, 
                                args=(chat_id, texto)
                            ).start()
            
            time.sleep(1)
            
        except Exception as e:
            print(f"❌ Error en bucle principal: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()