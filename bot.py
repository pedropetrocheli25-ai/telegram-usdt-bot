import requests
import time
import os
import threading
import json
import logging
import traceback
from datetime import datetime
from collections import deque, OrderedDict
from flask import Flask, request, jsonify
from functools import wraps

# ==================== CONFIGURACIÓN DE LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot_errors.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== CONFIGURACIÓN ====================
os.environ['TZ'] = 'America/Caracas'
try:
    time.tzset()
except AttributeError:
    pass  

TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not TOKEN or not ADMIN_ID:
    print("ERROR: TELEGRAM_TOKEN o ADMIN_ID no configurados")
    exit(1)

ADMIN_ID = int(ADMIN_ID)
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"

app = Flask(__name__)

# ==================== TASAS PARA TARIFARIOS Y CONVERSIÓN MANUAL ====================
TASA_SOLES_TARIFARIO = 3.80

# ==================== ALERTAS DE PRECIO FINANCIERO ====================
UMBRALES = {
    'VES': 0.50,
    'COP': 30.0,  
    'PEN': 0.03    
}

FLUCTUACION_UMBRAL = 0.8

# ==================== CONTROL DE ACCESO ====================
GRUPO_AUTORIZADO_ID = -5370892602  

def usuario_esta_en_grupo(user_id):
    return True

usuarios_activos = set([ADMIN_ID])
def obtener_usuarios():
    return list(usuarios_activos)
def guardar_usuario(chat_id):
    if chat_id not in usuarios_activos:
        usuarios_activos.add(chat_id)

# ==================== CACHÉ ROBUSTO (PROBLEMA 5) ====================
class CacheRobusto:
    def __init__(self, ttl=30, max_size=10):
        self.cache = OrderedDict()
        self.ttl = ttl
        self.max_size = max_size
        self.lock = threading.Lock()
    
    def get(self, key):
        with self.lock:
            if key in self.cache:
                value, timestamp = self.cache[key]
                if time.time() - timestamp < self.ttl:
                    # Mover al final (LRU)
                    self.cache.move_to_end(key)
                    return value
                else:
                    # Expiró
                    del self.cache[key]
                    return None
            return None
    
    def set(self, key, value):
        with self.lock:
            # Si el caché está lleno, eliminar el más antiguo
            if len(self.cache) >= self.max_size:
                self.cache.popitem(last=False)
            
            self.cache[key] = (value, time.time())
            self.cache.move_to_end(key)
    
    def invalidate(self, key=None):
        with self.lock:
            if key:
                if key in self.cache:
                    del self.cache[key]
            else:
                self.cache.clear()
    
    def get_stats(self):
        with self.lock:
            return {
                'size': len(self.cache),
                'max_size': self.max_size,
                'keys': list(self.cache.keys())
            }

cache_precios = CacheRobusto(ttl=30, max_size=10)
cache_tiempo = {}  # Mantener por compatibilidad

# ==================== ESTADO COMPARTIDO CON LOCK (PROBLEMA 9 - Parcial) ====================
class EstadoCompartido:
    def __init__(self):
        self.lock = threading.Lock()
        self.ultimos_precios = {'VES': None, 'COP': None, 'PEN': None}
        self.ultimas_tasas_cruzadas = {}
        self.historial_ves = deque(maxlen=1440)
        self.precio_apertura_ves = None
    
    def actualizar_precio(self, moneda, precio):
        with self.lock:
            self.ultimos_precios[moneda] = precio
    
    def obtener_precio(self, moneda):
        with self.lock:
            return self.ultimos_precios.get(moneda)
    
    def actualizar_tasas(self, tasas):
        with self.lock:
            self.ultimas_tasas_cruzadas = tasas.copy()
    
    def obtener_tasas(self):
        with self.lock:
            return self.ultimas_tasas_cruzadas.copy()
    
    def agregar_historial_ves(self, precio):
        with self.lock:
            self.historial_ves.append(precio)
            if self.precio_apertura_ves is None:
                self.precio_apertura_ves = precio
    
    def obtener_historial_ves(self):
        with self.lock:
            return list(self.historial_ves)

estado = EstadoCompartido()

# Mantener variables globales para compatibilidad
ultimos_precios = estado.ultimos_precios
historial_ves = estado.historial_ves
precio_apertura_ves = None

# ==================== HISTORIAL ====================
def guardar_historial_ves(precio):
    estado.agregar_historial_ves(precio)

def obtener_analisis_ves():
    precios = estado.obtener_historial_ves()
    if not precios: 
        return None
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
        'actual': precio_actual, 'apertura': precio_inicio, 'cambio': cambio,
        'cambio_porcentaje': cambio_porcentaje, 'maximo': precio_max, 'minimo': precio_min,
        'tendencia': tendencia, 'muestras': len(precios)
    }

# ==================== RATE LIMITING (PROBLEMA 2) ====================
class RateLimiterAvanzado:
    def __init__(self):
        self.usuarios = {}
        self.ip_limiter = {}
        self.global_counter = 0
        self.last_reset = time.time()
        self.GLOBAL_LIMIT = 100
        self.USER_LIMIT = 30
        self.IP_LIMIT = 50
        self.WINDOW = 60
        self.lock = threading.Lock()
    
    def _clean_old_records(self, now):
        cutoff = now - self.WINDOW
        with self.lock:
            self.usuarios = {k: [t for t in v if t > cutoff] for k, v in self.usuarios.items()}
            self.ip_limiter = {k: [t for t in v if t > cutoff] for k, v in self.ip_limiter.items()}
            
            # Reset global counter cada minuto
            if now - self.last_reset >= self.WINDOW:
                self.global_counter = 0
                self.last_reset = now
    
    def check_limit(self, user_id, ip=None):
        now = time.time()
        self._clean_old_records(now)
        
        with self.lock:
            # Verificar límite global
            self.global_counter += 1
            if self.global_counter > self.GLOBAL_LIMIT:
                logger.warning(f"Límite global excedido: {self.global_counter}")
                return False
            
            # Verificar límite por usuario
            if user_id not in self.usuarios:
                self.usuarios[user_id] = []
            if len(self.usuarios[user_id]) >= self.USER_LIMIT:
                logger.warning(f"Límite de usuario {user_id} excedido")
                return False
            
            # Verificar límite por IP (si está disponible)
            if ip:
                import hashlib
                ip_hash = hashlib.md5(ip.encode()).hexdigest()
                if ip_hash not in self.ip_limiter:
                    self.ip_limiter[ip_hash] = []
                if len(self.ip_limiter[ip_hash]) >= self.IP_LIMIT:
                    logger.warning(f"Límite de IP {ip_hash} excedido")
                    return False
                self.ip_limiter[ip_hash].append(now)
            
            self.usuarios[user_id].append(now)
            return True
    
    def get_stats(self):
        with self.lock:
            return {
                'usuarios_activos': len(self.usuarios),
                'ips_activas': len(self.ip_limiter),
                'peticiones_globales': self.global_counter,
                'limite_usuario': self.USER_LIMIT,
                'limite_ip': self.IP_LIMIT
            }

rate_limiter = RateLimiterAvanzado()

# ==================== DECORADOR DE MÉTRICAS (PROBLEMA 9) ====================
class Metricas:
    def __init__(self):
        self.tiempos = {}
        self.contadores = {}
        self.lock = threading.Lock()
    
    def medir_tiempo(self, nombre):
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                inicio = time.time()
                try:
                    resultado = func(*args, **kwargs)
                    duracion = (time.time() - inicio) * 1000  # en ms
                    with self.lock:
                        if nombre not in self.tiempos:
                            self.tiempos[nombre] = {'sum': 0, 'count': 0, 'max': 0, 'min': float('inf')}
                        self.tiempos[nombre]['sum'] += duracion
                        self.tiempos[nombre]['count'] += 1
                        self.tiempos[nombre]['max'] = max(self.tiempos[nombre]['max'], duracion)
                        self.tiempos[nombre]['min'] = min(self.tiempos[nombre]['min'], duracion)
                        if nombre not in self.contadores:
                            self.contadores[nombre] = 0
                        self.contadores[nombre] += 1
                    return resultado
                except Exception as e:
                    with self.lock:
                        if nombre not in self.contadores:
                            self.contadores[nombre] = 0
                        # Usar clave especial para errores
                        error_key = f"{nombre}_errores"
                        if error_key not in self.contadores:
                            self.contadores[error_key] = 0
                        self.contadores[error_key] += 1
                    raise
            return wrapper
        return decorator
    
    def obtener_metricas(self):
        with self.lock:
            metricas = {
                'tiempos': {},
                'contadores': self.contadores.copy()
            }
            for nombre, data in self.tiempos.items():
                if data['count'] > 0:
                    metricas['tiempos'][nombre] = {
                        'promedio_ms': data['sum'] / data['count'],
                        'max_ms': data['max'],
                        'min_ms': data['min'],
                        'count': data['count']
                    }
            return metricas

metricas = Metricas()

# ==================== ESTADOS DE ENTRADA ====================
usuario_esperando_calculo = {} 
usuario_esperando_cruzado = {}  
usuario_configurando_soles = {}  

# ==================== INTERFACES DE TECLADOS ====================
def crear_teclado_principal(chat_id):
    teclado = [
        ["Tether + BCV"],
        ["¿Cuánto Es?"], 
        ["¿Cuánto Gané?"],
        ["📈 Historial de brecha VES"]
    ]

    if chat_id == ADMIN_ID:
        teclado.append(["Remesas 💼"])

    teclado.append(["+ Opciones"])
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_remesas(chat_id):
    teclado = [
        ["¿Cuánto es Cruzado?"],
        ["📋 Tarifario USD"],
        ["📋 Tarifario Soles"],
        ["⚙️ Ajustar Tasa"],
        ["Tasas Cruzadas"],
        ["Volver al menú anterior"]
    ]
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_opciones(chat_id):
    teclado = [
        ["Precio USDT"],
        ["Precio VES"],
        ["Precio COP"],
        ["Precio PEN"]
    ]

    if chat_id == ADMIN_ID:
        teclado.append(["Usuarios Registrados"])

    teclado.append(["Volver al menú anterior"])
    return {"keyboard": teclado, "resize_keyboard": True}

def crear_teclado_cruzado_rapido(chat_id):
    teclado = [
        ["20 S/", "50 S/", "100 S/"],
        ["5000 Bs", "10000 Bs", "20000 Bs"],
        ["Volver al menú anterior"]
    ]
    return {"keyboard": teclado, "resize_keyboard": True}

def enviar_mensaje(chat_id, texto, teclado=None):
    try:
        url = URL_TELEGRAM + "sendMessage"
        data = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}
        if teclado:
            data["reply_markup"] = teclado
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Error enviando mensaje a {chat_id}: {e}")
        return False

# ==================== API FALLBACKS (PROBLEMA 10) ====================
API_FALLBACKS = [
    "https://api.exchangerate-api.com/v4/latest/USD",
    "https://api.exchangeratesapi.io/latest?base=USD",
]

def obtener_tasas_bcv_con_fallback():
    """Intenta obtener tasas de múltiples fuentes"""
    for i, api_url in enumerate(API_FALLBACKS):
        try:
            logger.info(f"Intentando API {i+1}: {api_url}")
            response = requests.get(api_url, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                tasa = None
                
                if 'exchangerate-api' in api_url:
                    tasa = data.get('rates', {}).get('VES', 0)
                elif 'exchangeratesapi' in api_url:
                    tasa = data.get('rates', {}).get('VES', 0)
                
                if tasa and tasa > 0:
                    logger.info(f"Tasa obtenida de API {i+1}: {tasa}")
                    eur = data.get('rates', {}).get('EUR', 0)
                    return {
                        'usd': tasa,
                        'eur': tasa * eur if eur > 0 else tasa * 0.92,
                        'fecha': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'fuente': f"API {i+1}"
                    }
            else:
                logger.warning(f"API {i+1} respondió con código {response.status_code}")
        except Exception as e:
            logger.warning(f"API {i+1} falló: {e}")
            continue
    
    logger.error("Todas las APIs fallaron, usando tasa por defecto")
    return {
        'usd': 45.00,
        'eur': 41.40,
        'fecha': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'fuente': 'Tasa por defecto'
    }

# ==================== OBTENCIÓN P2P Y BCV ====================

@metricas.medir_tiempo('obtener_precios_cache')
def obtener_precios_con_cache(fiat):
    # Intentar obtener del caché robusto
    cached = cache_precios.get(fiat)
    if cached:
        return cached['compra'], cached['venta']
    
    compra, venta = obtener_precios_p2p_reales(fiat)
    if compra and venta:
        cache_precios.set(fiat, {'compra': compra, 'venta': venta})
        # Mantener cache_tiempo para compatibilidad
        cache_tiempo[fiat] = time.time()
    
    return compra, venta

@metricas.medir_tiempo('obtener_precios_p2p')
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
                        p = float(a['adv']['price'])
                        if 1 < p < 100000:
                            precios.append(p)
                    if precios:
                        compra = min(precios)
        except requests.exceptions.Timeout:
            logger.error(f"Timeout obteniendo precios SELL para {fiat}")
        except Exception as e:
            logger.error(f"Error en SELL para {fiat}: {e}")

        data = {"asset": "USDT", "fiat": fiat, "tradeType": "BUY", "page": 1, "rows": 10, "payTypes": []}
        venta = None
        try:
            r = requests.post(url, json=data, headers=headers, timeout=10)
            if r.status_code == 200:
                result = r.json()
                if result.get('data'):
                    precios = []
                    for a in result['data']:
                        p = float(a['adv']['price'])
                        if 1 < p < 100000:
                            precios.append(p)
                    if precios:
                        venta = max(precios)
        except requests.exceptions.Timeout:
            logger.error(f"Timeout obteniendo precios BUY para {fiat}")
        except Exception as e:
            logger.error(f"Error en BUY para {fiat}: {e}")

        if compra is None or venta is None:
            return None, None
        if compra < venta:
            compra, venta = venta, compra
        return compra, venta
    except Exception as e:
        logger.error(f"Error inesperado obteniendo precios para {fiat}: {e}\n{traceback.format_exc()}")
        return None, None

def obtener_tasa_bcv_actual():
    tasas = obtener_tasas_bcv()
    if tasas and tasas.get('usd'):
        return tasas['usd']
    return 45.00 

def obtener_tasas_bcv():
    return obtener_tasas_bcv_con_fallback()

# ==================== MOSTRAR TARIFARIOS EN TEXTO ====================
def mostrar_tarifario_usd(chat_id):
    tasa_bcv = obtener_tasa_bcv_actual()
    dolares_lista = [10, 20, 30, 50, 100, 150, 200, 250, 300, 500]

    mensaje = f"📋 *TARIFARIO EN USD*\n🕐 Tasa BCV: {tasa_bcv:.2f} Bs | Perú - Ven Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n```\n{'Dólares'.ljust(9)}|{'Recibes (Bs)'.ljust(14)}|{'Equivalente'.ljust(12)}\n---------------------------------\n"
    for usd in dolares_lista:
        recibes_val = usd * tasa_bcv
        equiv_soles = recibes_val / TASA_SOLES_TARIFARIO if TASA_SOLES_TARIFARIO > 0 else 0
        mensaje += f"{f'{usd}$'.ljust(9)}|{f'{recibes_val:,.2f}'.ljust(14)}|{f'{equiv_soles:,.2f} S/'.ljust(12)}\n"
    mensaje += "```"
    enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))

def mostrar_tarifario_soles(chat_id):
    tasa_bcv = obtener_tasa_bcv_actual()
    soles_lista = [10, 20, 30, 50, 100, 150, 200, 300, 500, 1000]

    mensaje = f"📋 *TARIFARIO EN SOLES A BOLÍVARES*\n🕐 Tasa BCV: {tasa_bcv:.2f} Bs | Perú - Ven Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n```\n{'Enviado'.ljust(10)}|{'Recibes (Bs)'.ljust(14)}|{'Equivalente'.ljust(12)}\n---------------------------------\n"
    for soles in soles_lista:
        recibes_val = soles * TASA_SOLES_TARIFARIO
        equiv_usd = recibes_val / tasa_bcv if tasa_bcv > 0 else 0
        mensaje += f"{f'{soles} S/'.ljust(10)}|{f'{recibes_val:,.2f}'.ljust(14)}|{f'{equiv_usd:,.2f}$'.ljust(12)}\n"
    mensaje += "```"
    enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))

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
        enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))
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

    enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))

# ==================== ¿CUÁNTO ES CRUZADO? ====================
def calcular_conversion_tasas_cruzadas(chat_id, texto_monto):
    tasas = obtener_tasas_bcv()
    if not tasas:
        enviar_mensaje(chat_id, "⏳ No se pudo obtener la tasa BCV oficial en este momento.", crear_teclado_remesas(chat_id))
        return

    tasa_bcv = tasas['usd']
    texto_limpio = texto_monto.strip().lower()

    tasa_peru_ven = TASA_SOLES_TARIFARIO
    tasa_ven_peru = TASA_SOLES_TARIFARIO + 15

    try:
        if 's/' in texto_limpio or 'soles' in texto_limpio or 'sol' in texto_limpio:
            monto_str = texto_limpio.replace('s/', '').replace('soles', '').replace('sol', '').replace(',', '.').strip()
            monto_soles = float(monto_str)

            resultado_bs_pv = monto_soles * tasa_peru_ven
            resultado_usd_pv = resultado_bs_pv / tasa_bcv

            resultado_bs_vp = monto_soles * tasa_ven_peru
            resultado_usd_vp = resultado_bs_vp / tasa_bcv

            mensaje = f"""📊 *PROCESAMIENTO DINÁMICO DE SOLES*

*Tasa BCV:* {tasa_bcv:.2f} Bs
*Tasa Perú - Venezuela:* {tasa_peru_ven:.2f}
*Tasa Venezuela - Perú:* {tasa_ven_peru:.2f}
━━━━━━━━━━━━━━━━━━━━
🇵🇪 ➔ 🇻🇪 *Operación Perú - Venezuela:*
• {monto_soles:,.2f} Soles, Equivalente a *{resultado_bs_pv:,.2f} Bs*, *{resultado_usd_pv:,.2f}$* a tasa BCV

🇻🇪 ➔ 🇵🇪 *Operación Venezuela - Perú:*
• Para que lleguen {monto_soles:,.2f} Soles se necesita *{resultado_bs_vp:,.2f} Bs*, equivalente a *{resultado_usd_vp:,.2f}$* a tasa BCV
━━━━━━━━━━━━━━━━━━━━
🕐 {datetime.now().strftime('%H:%M:%S')} (Caracas)"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_cruzado_rapido(chat_id))

        elif 'bs' in texto_limpio:
            monto_str = texto_limpio.replace('bs', '').replace(',', '.').strip()
            monto_bs = float(monto_str)

            resultado_usd_bcv = monto_bs / tasa_bcv
            resultado_soles_pv = monto_bs / tasa_peru_ven if tasa_peru_ven > 0 else 0
            resultado_soles_vp = monto_bs / tasa_ven_peru if tasa_ven_peru > 0 else 0

            mensaje = f"""⚖️ *CALCULADORA DE TASAS CRUZADAS (TASA MANUAL)*

*Tasa BCV:* {tasa_bcv:.2f} Bs
*Tasa Perú - Venezuela (Manual):* {tasa_peru_ven:.2f} | *Tasa Venezuela - Perú:* {tasa_ven_peru:.2f}
━━━━━━━━━━━━━━━━━━━━
🇵🇪 ➔ 🇻🇪 *Fórmula Perú - Venezuela:*
• {monto_bs:,.2f} Bs, *${resultado_usd_bcv:,.2f}$* a tasa BCV, son *{resultado_soles_pv:,.2f} Soles*

🇻🇪 ➔ 🇵🇪 *Fórmula Venezuela - Perú:*
• Por {monto_bs:,.2f} Bs equivalente a *${resultado_usd_bcv:,.2f}$* a tasa BCV, llegan *{resultado_soles_vp:,.2f} Soles*
━━━━━━━━━━━━━━━━━━━━
🕐 {datetime.now().strftime('%H:%M:%S')} (Caracas)"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_cruzado_rapido(chat_id))
        else:
            enviar_mensaje(chat_id, "⚠️ Para cálculos cruzados indica la cantidad añadiendo *S/* o *Bs* al final (ejemplo: `100 S/` o `25000 Bs`).", crear_teclado_cruzado_rapido(chat_id))

    except ValueError:
        enviar_mensaje(chat_id, "❌ Error al realizar la conversión cruzada. Verifica la cantidad escrita.", crear_teclado_cruzado_rapido(chat_id))

# ==================== FLUCTUACIÓN E HISTORIAL ====================
with open("tasas_anteriores.json", "w") as f: pass

def guardar_tasas_anteriores():
    try:
        tasas = estado.obtener_tasas()
        with open("tasas_anteriores.json", 'w') as f: 
            json.dump(tasas, f)
    except Exception as e:
        logger.error(f"Error guardando tasas anteriores: {e}")

def cargar_tasas_anteriores():
    try:
        if os.path.exists("tasas_anteriores.json"):
            with open("tasas_anteriores.json", 'r') as f: 
                tasas = json.load(f)
                estado.actualizar_tasas(tasas)
    except Exception as e:
        logger.error(f"Error cargando tasas anteriores: {e}")

def verificar_fluctuacion_tasas():
    tasas_actuales = calcular_tasas_cruzadas()
    if not tasas_actuales:
        return
    
    tasas_anteriores = estado.obtener_tasas()
    if not tasas_anteriores:
        estado.actualizar_tasas(tasas_actuales.copy())
        guardar_tasas_anteriores()
        return

    mensaje = "⚠️ *ALERTA DE FLUCTUACIÓN DE TASAS* ⚠️\n"
    mensaje += f"🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    hubo_fluctuacion = False

    for clave, valor_actual in tasas_actuales.items():
        if clave in tasas_anteriores:
            valor_anterior = tasas_anteriores[clave]
            if valor_anterior > 0:
                fluctuacion = abs((valor_actual - valor_anterior) / valor_anterior) * 100
                if fluctuacion >= FLUCTUACION_UMBRAL:
                    direccion = "📈 SUBIÓ" if valor_actual > valor_anterior else "📉 BAJÓ"
                    mensaje += f"• *{clave}*: {direccion} en {fluctuacion:.2f}%\n"
                    mensaje += f"  Anterior: {valor_anterior:.4f} → Actual: {valor_actual:.4f}\n\n"
                    hubo_fluctuacion = True

    if hubo_fluctuacion:
        for usuario in obtener_usuarios():
            try:
                enviar_mensaje(usuario, mensaje)
                time.sleep(0.05)
            except Exception as e:
                logger.error(f"Error enviando alerta a {usuario}: {e}")
    
    estado.actualizar_tasas(tasas_actuales.copy())
    guardar_tasas_anteriores()

# ==================== VERIFICAR ALERTAS ACUMULATIVAS ====================
def verificar_alertas(precios):
    if not precios: 
        return

    usuarios = obtener_usuarios()
    if not usuarios: 
        return

    for moneda in ['VES', 'COP', 'PEN']:
        if moneda not in precios or not precios[moneda]: 
            continue

        precio_actual = precios[moneda]['compra']
        precio_anterior = estado.obtener_precio(moneda)

        if precio_anterior is None:
            estado.actualizar_precio(moneda, precio_actual)
            continue

        cambio = abs(precio_actual - precio_anterior)
        umbral = UMBRALES.get(moneda, 0)

        if cambio >= umbral:
            direccion = "📈 SUBIÓ" if precio_actual > precio_anterior else "📉 BAJÓ"
            emoji = "🟢" if precio_actual > precio_anterior else "🔴"
            signo = "+" if precio_actual > precio_anterior else ""

            cambio_porcentaje = ((precio_actual - precio_anterior) / precio_anterior * 100) if precio_anterior != 0 else 0

            mensaje = (
                f"\n{emoji} *🔔 ALERTA {moneda}* {emoji}\n\n"
                f"{direccion} en {signo}{cambio:.2f}\n\n"
                f"📊 *Detalles:*\n"
                f"• Referencia Anterior: {precio_anterior:.2f}\n"
                f"• Precio Actual: {precio_actual:.2f}\n"
                f"• Variación: {signo}{cambio:.2f} ({signo}{cambio_porcentaje:.2f}%)\n\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
            )

            for usuario in usuarios:
                try:
                    enviar_mensaje(usuario, mensaje)
                    time.sleep(0.05)
                except Exception as e:
                    logger.error(f"Error enviando alerta a {usuario}: {e}")

            estado.actualizar_precio(moneda, precio_actual)

# ==================== FUNCIONES DE PRECIOS ====================
def mostrar_precios_usdt(chat_id):
    precios = {}
    for m in ['VES', 'COP', 'PEN']:
        compra, venta = obtener_precios_con_cache(m)
        if compra and venta: 
            precios[m] = {'compra': compra, 'venta': venta}
    if not precios:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios...", crear_teclado_opciones(chat_id))
        return
    mensaje = f"💰 *PRECIOS USDT P2P*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    for m, datos in precios.items():
        mensaje += f"*{m}*\n  🟢 COMPRA: {datos['compra']:.2f}\n  🔴 VENTA: {datos['venta']:.2f}\n  📊 Spread: {datos['compra']-datos['venta']:.2f}\n\n"
    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

def mostrar_precio_individual(chat_id, moneda):
    compra, venta = obtener_precios_con_cache(moneda)
    if not compra or not venta:
        enviar_mensaje(chat_id, f"⏳ Obteniendo precio {moneda}...", crear_teclado_opciones(chat_id))
        return
    mensaje = f"💰 *PRECIO {moneda}*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"🟢 COMPRA: {compra:.2f}\n🔴 VENTA: {venta:.2f}\n📊 Spread: {compra-venta:.2f}\n"
    enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

# ==================== TETHER + BCV ====================
def mostrar_tether_vs_bcv(chat_id):
    compra, venta = obtener_precios_con_cache('VES')
    tasas = obtener_tasas_bcv()

    if not compra or not venta or not tasas:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios del mercado...", crear_teclado_principal(chat_id))
        return

    tasa_bcv_oficial = tasas['usd']
    tasa_intervencion = tasa_bcv_oficial * 1.005
    media = (compra + venta) / 2.0

    analisis = obtener_analisis_ves()
    if analisis:
        max_24h = analisis['maximo']
        min_24h = analisis['minimo']
        var_pct = analisis['cambio_porcentaje']
        tendencia_str = analisis['tendencia']
    else:
        max_24h = compra
        min_24h = compra
        var_pct = 0.0
        tendencia_str = "➡️ Lateral"

    brecha_compra_bcv = compra - tasa_bcv_oficial
    pct_compra_bcv = (brecha_compra_bcv / tasa_bcv_oficial) * 100 if tasa_bcv_oficial > 0 else 0.0

    brecha_venta_bcv = venta - tasa_bcv_oficial
    pct_venta_bcv = (brecha_venta_bcv / tasa_bcv_oficial) * 100 if tasa_bcv_oficial > 0 else 0.0

    diferencial_bruto = venta - tasa_intervencion
    margen_bruto_pct = (diferencial_bruto / tasa_intervencion) * 100 if tasa_intervencion > 0 else 0.0

    fecha_hora_str = datetime.now().strftime('%d/%m %H:%M')

    mensaje = f"""📊 *MERCADO USDT / BCV — {fecha_hora_str}*

🟢 *Binance P2P (USDT)*
• Venta: *{venta:.2f} Bs*
• Compra: *{compra:.2f} Bs*
• Media: *{media:.2f} Bs*
• Tendencia (24h): {tendencia_str} (Máx {max_24h:.2f} | Mín {min_24h:.2f} | {var_pct:+.2f}%)

🏦 *Tasa Oficial (BCV)*
• BCV Oficial: *{tasa_bcv_oficial:.2f} Bs*
• BCV + 0.50%: *{tasa_intervencion:.2f} Bs*

📐 *Análisis de Brecha*
• P2P Compra vs BCV: *+{brecha_compra_bcv:.2f} Bs* ({pct_compra_bcv:+.2f}%)
• P2P Venta vs BCV: *+{brecha_venta_bcv:.2f} Bs* ({pct_venta_bcv:+.2f}%)
• Margen Operación Intervención: *~{margen_bruto_pct:.2f}%* ({diferencial_bruto:.2f} Bs)

💡 *Nota:* Si ejecutas la venta de USDT a {venta:.2f} Bs y recompras USD por intervención bancaria a {tasa_intervencion:.2f} Bs, el retorno bruto estimado ronda el {margen_bruto_pct:.1f}% antes de comisiones bancarias locales."""

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== FUNCIÓN MODIFICADA: CUÁNTO GANÉ ====================
def calcular_ganancia_neta(chat_id, monto=100.0):
    compra_ves, venta_ves = obtener_precios_con_cache('VES')
    tasas = obtener_tasas_bcv()

    if not venta_ves or not tasas:
        enviar_mensaje(chat_id, "⏳ Obteniendo precios del mercado...", crear_teclado_principal(chat_id))
        return
    
    tasa_bcv = tasas['usd']
    bcv_mas_medio = tasa_bcv * 1.005
    costo_bcv_monto = monto * bcv_mas_medio

    comision_tarjeta = monto * 0.015
    comision_bpay = monto * 0.041
    total_comisiones = comision_tarjeta + comision_bpay

    usdt_neto = monto - total_comisiones

    total_retornado_bs = usdt_neto * venta_ves
    equivalente_usd_bcv = total_retornado_bs / tasa_bcv if tasa_bcv > 0 else 0.0

    ganancia_bs = total_retornado_bs - costo_bcv_monto
    ganancia_usd = equivalente_usd_bcv - monto
    rendimiento_pct = (ganancia_usd / monto) * 100 if monto > 0 else 0.0
    ganancia_por_dolar = ganancia_usd / monto if monto > 0 else 0.0

    mensaje = f"""🏦 *ANÁLISIS COMPLETO CUÁNTO GANÉ*
💰 Capital: *${monto:,.2f} USD*
📊 Tasa BCV: *{tasa_bcv:.2f} Bs*

━━━━━━━━━━━━━━━━━━━━━━━━━━
1️⃣ *COSTO DE INTERVENCIÓN (Egreso)*
• BCV Oficial: *{tasa_bcv:.2f} Bs*
• BCV + 0.50%: *{bcv_mas_medio:.2f} Bs*
• Total Invertido: *${monto:,.2f} USD* → *{costo_bcv_monto:,.2f} Bs*

━━━━━━━━━━━━━━━━━━━━━━━━━━
2️⃣ *COMISIONES DE BANESCO*
• Tarjeta (1.5%): *${comision_tarjeta:,.2f} USD*
• Bpay (4.1%): *${comision_bpay:,.2f} USD*
• TOTAL COMISIONES: *${total_comisiones:,.2f} USD*

━━━━━━━━━━━━━━━━━━━━━━━━━━
3️⃣ *LIQUIDACIÓN FINAL (USDT)*
• Capital bruto: *{monto:,.2f} USDT*
• Comisiones: *-{total_comisiones:,.2f} USDT*
• USDT neto: *{usdt_neto:,.2f} USDT*

━━━━━━━━━━━━━━━━━━━━━━━━━━
4️⃣ *RETORNO EN P2P*
• Tasa de Venta USDT: *{venta_ves:.2f} Bs*
• Total Retornado: *{total_retornado_bs:,.2f} Bs*
• Equivalente USD (BCV): *${equivalente_usd_bcv:,.2f}*

━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 *GANANCIA NETA TOTAL*
• En Bs: *{ganancia_bs:+,.2f} Bs*
• En USD: *${ganancia_usd:+,.2f}*
• Rendimiento: *{rendimiento_pct:+.2f}%*

• 💵 Ganancia por dólar: *${ganancia_por_dolar:+.3f}* por cada $1"""

    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

def calcular_conversion_bcv_medio(chat_id, texto_monto):
    tasas = obtener_tasas_bcv()
    if not tasas: return
    tasa_bcv = tasas['usd']
    bcv_mas_medio = tasa_bcv * 1.005
    texto_limpio = texto_monto.strip().lower()

    try:
        if 'bs' in texto_limpio:
            monto_bs = float(texto_limpio.replace('bs', '').replace(',', '.').strip())

            usd_oficial = monto_bs / tasa_bcv if tasa_bcv > 0 else 0
            usd_mas_medio = monto_bs / bcv_mas_medio if bcv_mas_medio > 0 else 0

            mensaje = f"""⚖️ *CALCULADORA DE CONVERSIÓN*

📊 *Tasa BCV Oficial:* {tasa_bcv:.2f} Bs
━━━━━━━━━━━━━━━━━━━━
✍️ *Operación (Bs ➔ $):* {monto_bs:,.2f} Bs
🇺🇸 *Total equivalente:* *${usd_oficial:,.2f} USD*
━━━━━━━━━━━━━━━━━━━━

📊 *Tasa BCV Oficial:* {tasa_bcv:.2f} Bs + 0.50%
━━━━━━━━━━━━━━━━━━━━
✍️ *Operación (Bs ➔ $):* {monto_bs:,.2f} Bs
🇺🇸 *Total equivalente:* *${usd_mas_medio:,.2f} USD*
━━━━━━━━━━━━━━━━━━━━"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

        elif '$' in texto_limpio or 'usd' in texto_limpio:
            monto_usd = float(texto_limpio.replace('$', '').replace('usd', '').replace(',', '.').strip())

            bs_oficial = monto_usd * tasa_bcv
            bs_mas_medio = monto_usd * bcv_mas_medio

            mensaje = f"""⚖️ *CALCULADORA DE CONVERSIÓN*

📊 *Tasa BCV Oficial:* {tasa_bcv:.2f} Bs
━━━━━━━━━━━━━━━━━━━━
✍️ *Operación ($ ➔ Bs):* ${monto_usd:,.2f} USD
🇻🇪 *Total equivalente:* *{bs_oficial:,.2f} Bs*
━━━━━━━━━━━━━━━━━━━━

📊 *Tasa BCV Oficial:* {tasa_bcv:.2f} Bs + 0.50%
━━━━━━━━━━━━━━━━━━━━
✍️ *Operación ($ ➔ Bs):* ${monto_usd:,.2f} USD
🇻🇪 *Total equivalente:* *{bs_mas_medio:,.2f} Bs*
━━━━━━━━━━━━━━━━━━━━"""
            enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))
    except Exception as e:
        logger.error(f"Error en conversión BCV: {e}")

def mostrar_historial_ves(chat_id):
    analisis = obtener_analisis_ves()
    if not analisis:
        enviar_mensaje(chat_id, "📈 *HISTORIAL DE BRECHA VES*\n⏳ Sin datos suficientes aún", crear_teclado_principal(chat_id))
        return
    mensaje = f"📈 *HISTORIAL DE BRECHA VES (24h)*\n📊 *Apertura:* {analisis['apertura']:.2f} Bs\n📊 *Actual:* {analisis['actual']:.2f} Bs\n*Cambio:* {analisis['cambio']:+.2f} Bs ({analisis['cambio_porcentaje']:+.1f}%)\n📈 *Máximo:* {analisis['maximo']:.2f} Bs\n📉 *Mínimo:* {analisis['minimo']:.2f} Bs\n🧭 *Tendencia:* {analisis['tendencia']}"
    enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))

# ==================== PROCESAR MENSAJES ====================
def procesar_mensaje(chat_id, texto):
    global usuario_esperando_calculo, usuario_esperando_cruzado, usuario_configurando_soles
    global TASA_SOLES_TARIFARIO

    if not usuario_esta_en_grupo(chat_id): 
        return
    guardar_usuario(chat_id)

    if chat_id == ADMIN_ID and usuario_configurando_soles.get(chat_id):
        try:
            TASA_SOLES_TARIFARIO = float(texto.replace(',', '.'))
            usuario_configurando_soles[chat_id] = False  
            enviar_mensaje(chat_id, f"✅ *Tasa Soles configurada con éxito:* {TASA_SOLES_TARIFARIO:.2f}", crear_teclado_remesas(chat_id))
            return
        except ValueError:
            pass  

    if any(char.isdigit() for char in texto):
        if usuario_esperando_cruzado.get(chat_id) or 's/' in texto.lower() or 'soles' in texto.lower():
            calcular_conversion_tasas_cruzadas(chat_id, texto)
            return
        elif usuario_esperando_calculo.get(chat_id) or 'bs' in texto.lower() or '$' in texto or 'usd' in texto.lower():
            calcular_conversion_bcv_medio(chat_id, texto)
            usuario_esperando_calculo[chat_id] = False
            return

    if texto == '/start':
        usuario_configurando_soles[chat_id] = False
        enviar_mensaje(chat_id, "Bienvenido a Asistente Remesas P2P.", crear_teclado_principal(chat_id))

    elif texto == 'Tether + BCV':
        mostrar_tether_vs_bcv(chat_id)

    elif texto == '¿Cuánto Es?':
        usuario_esperando_calculo[chat_id] = True
        usuario_esperando_cruzado[chat_id] = False
        enviar_mensaje(chat_id, "✍️ Escribe la cantidad seguida de *Bs* o *$*.", crear_teclado_principal(chat_id))

    elif texto == '¿Cuánto Gané?':
        enviar_mensaje(chat_id, "✍️ Escribe directamente el monto en *USD* que deseas calcular (Ej: `100`).", crear_teclado_principal(chat_id))

    elif texto == '📈 Historial de brecha VES':
        mostrar_historial_ves(chat_id)

    elif texto == 'Remesas 💼':
        if chat_id == ADMIN_ID:
            enviar_mensaje(chat_id, "💼 *SUBMENÚ REMESAS & TARIFARIOS MANUALEZ*", crear_teclado_remesas(chat_id))
        else:
            enviar_mensaje(chat_id, "❌ Acción restringida.", crear_teclado_principal(chat_id))

    elif texto == '¿Cuánto es Cruzado?':
        if chat_id == ADMIN_ID:
            usuario_esperando_calculo[chat_id] = False
            usuario_esperando_cruzado[chat_id] = True
            enviar_mensaje(chat_id, "✍️ Escribe el monto seguido de *S/* o *Bs*.", crear_teclado_cruzado_rapido(chat_id))
        else:
            enviar_mensaje(chat_id, "❌ Acción restringida.", crear_teclado_principal(chat_id))

    elif texto == '📋 Tarifario USD':
        if chat_id == ADMIN_ID: 
            mostrar_tarifario_usd(chat_id)

    elif texto == '📋 Tarifario Soles':
        if chat_id == ADMIN_ID: 
            mostrar_tarifario_soles(chat_id)

    elif texto == '⚙️ Ajustar Tasa':
        if chat_id == ADMIN_ID:
            usuario_configurando_soles[chat_id] = True
            enviar_mensaje(chat_id, f"⚙️ *Tasa Actual:* {TASA_SOLES_TARIFARIO:.2f}\n\n✍️ Envía el nuevo valor (Ej: `3.85`).", crear_teclado_remesas(chat_id))

    elif texto == 'Tasas Cruzadas':
        if chat_id == ADMIN_ID:
            mostrar_tasas_cambio(chat_id)

    elif texto == '+ Opciones':
        enviar_mensaje(chat_id, "📋 *SEGUNDO MENÚ (MERCADO P2P)*", crear_teclado_opciones(chat_id))

    elif texto == 'Precio USDT': 
        mostrar_precios_usdt(chat_id)
    elif texto == 'Precio VES': 
        mostrar_precio_individual(chat_id, 'VES')
    elif texto == 'Precio COP': 
        mostrar_precio_individual(chat_id, 'COP')
    elif texto == 'Precio PEN': 
        mostrar_precio_individual(chat_id, 'PEN')

    elif texto == 'Usuarios Registrados':
        if chat_id == ADMIN_ID:
            usuarios = obtener_usuarios()
            mensaje = f"👥 *Usuarios activos:* {len(usuarios)}"
            for uid in usuarios: 
                mensaje += f"\n• `{uid}`"
            enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))

    elif texto == 'Volver al menú anterior':
        usuario_configurando_soles[chat_id] = False
        usuario_esperando_calculo[chat_id] = False
        usuario_esperando_cruzado[chat_id] = False
        enviar_mensaje(chat_id, "🏠 *Regresando al menú principal*", crear_teclado_principal(chat_id))

    else:
        try:
            monto_usuario = float(texto.replace(',', '.'))
            if monto_usuario > 0: 
                calcular_ganancia_neta(chat_id, monto_usuario)
        except ValueError:
            enviar_mensaje(chat_id, "Comando no reconocido.", crear_teclado_principal(chat_id))

# ==================== HEALTH CHECKS (PROBLEMA 4) ====================
def check_binance_connection():
    try:
        compra, venta = obtener_precios_p2p_reales('VES')
        return {'status': 'ok', 'has_data': compra is not None}
    except Exception as e:
        logger.error(f"Error en check_binance_connection: {e}")
        return {'status': 'error', 'message': str(e)[:100]}

def check_bcv_connection():
    try:
        tasa = obtener_tasa_bcv_actual()
        return {'status': 'ok', 'tasa': tasa}
    except Exception as e:
        logger.error(f"Error en check_bcv_connection: {e}")
        return {'status': 'error', 'message': str(e)[:100]}

start_time = time.time()

@app.route('/', methods=['GET'])
def home():
    return f"Bot activo 24/7 | Muestras: {len(estado.obtener_historial_ves())}", 200

@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint de health check completo"""
    historial = estado.obtener_historial_ves()
    stats_cache = cache_precios.get_stats()
    
    status = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'metrics': {
            'usuarios_activos': len(usuarios_activos),
            'historial_ves': len(historial),
            'cache_size': stats_cache['size'],
            'ultima_actualizacion': cache_tiempo.get('VES', 0),
            'uptime_segundos': int(time.time() - start_time)
        },
        'dependencies': {
            'binance': check_binance_connection(),
            'bcv_api': check_bcv_connection()
        },
        'rate_limiter': rate_limiter.get_stats(),
        'metricas': metricas.obtener_metricas()
    }
    
    # Verificar si la última actualización fue hace más de 5 minutos
    if cache_tiempo.get('VES', 0) < time.time() - 300:
        status['status'] = 'degraded'
        status['message'] = 'Última actualización de precios hace más de 5 minutos'
    
    return jsonify(status)

@app.route('/metrics', methods=['GET'])
def get_metrics():
    """Endpoint para obtener métricas detalladas"""
    return jsonify({
        'timestamp': datetime.now().isoformat(),
        'metricas': metricas.obtener_metricas(),
        'rate_limiter': rate_limiter.get_stats(),
        'cache': cache_precios.get_stats()
    })

# ==================== WEBHOOK CON RECONEXIÓN (PROBLEMA 6) ====================
def configurar_webhook_con_reintentos(max_retries=5, delay=5):
    """Configura el webhook con sistema de reintentos"""
    url_app = os.environ.get('URL_APP', 'https://telegram-usdt-bot-vf5t.onrender.com')
    url_set = f"{URL_TELEGRAM}setWebhook?url={url_app}/{TOKEN}"
    
    for attempt in range(max_retries):
        try:
            response = requests.get(url_set, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    logger.info(f"Webhook configurado exitosamente: {data}")
                    return True
                else:
                    logger.warning(f"Error configurando webhook: {data}")
            else:
                logger.warning(f"HTTP {response.status_code} configurando webhook")
        except Exception as e:
            logger.error(f"Intento {attempt + 1}/{max_retries} falló: {e}")
        
        if attempt < max_retries - 1:
            time.sleep(delay * (attempt + 1))  # Backoff exponencial
    
    logger.error("No se pudo configurar el webhook después de todos los reintentos")
    return False

def monitorear_webhook():
    """Monitorea el webhook periódicamente y lo reconecta si es necesario"""
    url_app = os.environ.get('URL_APP', 'https://telegram-usdt-bot-vf5t.onrender.com')
    
    while True:
        try:
            url_get = f"{URL_TELEGRAM}getWebhookInfo"
            response = requests.get(url_get, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    info = data.get('result', {})
                    current_url = info.get('url', '')
                    expected_url = f"{url_app}/{TOKEN}"
                    
                    if current_url != expected_url:
                        logger.warning(f"Webhook no configurado correctamente. Actual: {current_url}, Esperado: {expected_url}")
                        configurar_webhook_con_reintentos()
                    else:
                        logger.info("Webhook verificado correctamente")
        except Exception as e:
            logger.error(f"Error monitoreando webhook: {e}")
        
        time.sleep(300)  # Cada 5 minutos

@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        try:
            json_string = request.get_data().decode('utf-8')
            update = json.loads(json_string)
            
            message = update.get('message')
            if message:
                chat_id = message.get('chat', {}).get('id')
                texto = message.get('text', '')
                
                if chat_id and texto:
                    # Obtener IP del cliente para rate limiting
                    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
                    
                    # Verificar rate limit
                    if not rate_limiter.check_limit(chat_id, ip):
                        enviar_mensaje(chat_id, "⏳ Demasiadas solicitudes. Espera un momento.")
                        return 'OK', 200
                    
                    # Procesar en hilo separado
                    threading.Thread(target=procesar_mensaje, args=(chat_id, texto)).start()
            
            return 'OK', 200
        except json.JSONDecodeError as e:
            logger.error(f"Error decodificando JSON: {e}")
            return 'Bad Request', 400
        except Exception as e:
            logger.error(f"Error en webhook: {e}\n{traceback.format_exc()}")
            return 'Internal Server Error', 500
    
    return 'Forbidden', 403

# ==================== ACTUALIZACIÓN DE PRECIOS (PROBLEMA 9) ====================
@metricas.medir_tiempo('actualizar_precios')
def actualizar_precios():
    while True:
        try:
            precios = {}
            for m in ['VES', 'COP', 'PEN']:
                compra, venta = obtener_precios_p2p_reales(m)
                if compra and venta:
                    precios[m] = {'compra': compra, 'venta': venta}
                    cache_precios.set(m, {'compra': compra, 'venta': venta})
                    cache_tiempo[m] = time.time()
                    if m == 'VES': 
                        estado.agregar_historial_ves(compra)
            
            if precios:
                verificar_alertas(precios)
                verificar_fluctuacion_tasas()
            
            time.sleep(60)
        except Exception as e:
            logger.error(f"Error en actualizar_precios: {e}\n{traceback.format_exc()}")
            time.sleep(60)

if __name__ == "__main__":
    cargar_tasas_anteriores()
    
    # Configurar webhook con reintentos
    if not configurar_webhook_con_reintentos():
        logger.warning("El bot iniciará pero el webhook podría no funcionar correctamente")
    
    # Iniciar hilo de monitoreo de webhook
    threading.Thread(target=monitorear_webhook, daemon=True).start()
    
    # Iniciar hilo de actualización de precios
    threading.Thread(target=actualizar_precios, daemon=True).start()
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)