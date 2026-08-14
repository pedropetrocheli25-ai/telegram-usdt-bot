import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from flask import Flask, request
import requests

# ==================== CONFIGURACIÓN ====================
os.environ["TZ"] = "America/Caracas"
try:
  time.tzset()
except AttributeError:
  pass

TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")

if not TOKEN or not ADMIN_ID:
  print("ERROR: TELEGRAM_TOKEN o ADMIN_ID no configurados")
  exit(1)

ADMIN_ID = int(ADMIN_ID)
URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/"

# ID de tu grupo o canal privado para restringir acceso (debe empezar con -100)
ID_CANAL_O_GRUPO = int(os.environ.get("ID_CANAL_O_GRUPO", "-1001234567890"))

# URL de Render para Self-Ping
RENDER_URL = "https://telegram-usdt-bot-vf5t.onrender.com"

# Lock para guardado seguro de config.json
config_lock = threading.Lock()

app = Flask(__name__)

# ==================== CONSTANTES Y CONFIGURACIÓN FINANCIERA ====================
PORCENTAJE_BPAY = 4.1  # 4.1% Comisión Procesamiento Bpay

# Diccionario de Bancos de referencia
BANCOS_COMISIONES = {
    "Venezuela (Física)": 1.5,
    "Venezuela (Digital)": 2.5,
    "Provincial": 0.0,
    "Tesoro": 2.5,
    "Bancamiga": 5.0,
    "BNC": 1.5,
    "Banesco (Física)": 1.5,
    "Banesco (Digital)": 2.5,
}

# ==================== PERSISTENCIA DE CONFIGURACIÓN ====================
CONFIG_FILE = "config.json"
TASA_SOLES_TARIFARIO = 3.80


def cargar_configuracion():
  global TASA_SOLES_TARIFARIO
  with config_lock:
    try:
      if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
          data = json.load(f)
          TASA_SOLES_TARIFARIO = data.get("tasa_soles", 3.80)
    except Exception as e:
      print(f"Error al cargar config.json: {e}")


def guardar_configuracion():
  with config_lock:
    try:
      with open(CONFIG_FILE, "w") as f:
        json.dump({"tasa_soles": TASA_SOLES_TARIFARIO}, f)
    except Exception as e:
      print(f"Error al guardar config.json: {e}")


# ==================== ALERTAS DE PRECIO FINANCIERO ====================
UMBRALES = {"VES": 0.50}
ultimos_precios = {"VES": None}


# ==================== CONTROL DE ACCESO (LIBERADO) ====================
def usuario_esta_en_grupo(user_id):
  return True


usuarios_activos = set([ADMIN_ID])


def obtener_usuarios():
  return list(usuarios_activos)


def guardar_usuario(chat_id):
  if chat_id not in usuarios_activos:
    usuarios_activos.add(chat_id)


# ==================== CACHÉ DE PRECIOS ====================
cache_precios = {}
cache_tiempo = {}
CACHE_DURACION = 30

# ==================== HISTORIAL ====================
historial_ves = deque(maxlen=1440)

# ==================== ESTADOS DE ENTRADA ====================
usuario_esperando_calculo = {}
usuario_configurando_soles = {}
usuario_esperando_margen = {}
usuario_esperando_tasa_mesa = {}  # Nuevo: Espera la tasa personalizada
usuario_tasa_mesa = {}  # Guarda la tasa de Mesa de Cambio ingresada
usuario_modo_calculo = {}  # 'bcv' o 'mesa_cambio'
usuario_monto_temporal = {}  # Guarda el monto ingresado para aplicar la comisión elegida


def limpiar_estados_usuario(user_id):
  usuario_esperando_calculo.pop(user_id, None)
  usuario_configurando_soles.pop(user_id, None)
  usuario_esperando_margen.pop(user_id, None)
  usuario_esperando_tasa_mesa.pop(user_id, None)
  usuario_tasa_mesa.pop(user_id, None)
  usuario_modo_calculo.pop(user_id, None)
  usuario_monto_temporal.pop(user_id, None)


# ==================== INTERFACES DE TECLADOS ====================


def crear_teclado_principal(user_id):
  teclado = [
      ["📈 Comparativa P2P vs BCV"],
      ["🧮 Conversor USD / Bs"],
      ["📊 Calculadora de Margen", "🏛️ Mesa de Cambio"],  # Nuevo botón agregado
      ["📈 Historial de brecha VES"],
  ]

  if user_id == ADMIN_ID:
    teclado.append(["💼 Panel de Operaciones"])

  teclado.append(["⚙️ Mercado P2P"])
  return {"keyboard": teclado, "resize_keyboard": True}


def crear_teclado_remesas(user_id):
  teclado = [
      ["📋 Tarifario USD"],
      ["📋 Tarifario Soles"],
      ["⚙️ Ajustar Tasa"],
      ["⬅️ Volver al Menú"],
  ]
  return {"keyboard": teclado, "resize_keyboard": True}


def crear_teclado_opciones(user_id):
  teclado = [["📊 P2P Multidivisa"], ["🇻🇪 Tasa VES"]]

  if user_id == ADMIN_ID:
    teclado.append(["👥 Usuarios Registrados"])

  teclado.append(["⬅️ Volver al Menú"])
  return {"keyboard": teclado, "resize_keyboard": True}


def crear_teclado_comisiones_y_bancos():
  """Genera el menú interactivo para elegir la comisión específica o un banco."""
  teclado = [
      # Fila de selección directa de porcentaje de tarjeta
      [
          {"text": "💳 1.5%", "callback_data": "pct:1.5"},
          {"text": "💳 2.0%", "callback_data": "pct:2.0"},
      ],
      [
          {"text": "💳 2.5%", "callback_data": "pct:2.5"},
          {"text": "💳 5.0%", "callback_data": "pct:5.0"},
      ],
  ]

  # Bancos preconfigurados opcionales
  fila_bancos = []
  for banco, pct in BANCOS_COMISIONES.items():
    fila_bancos.append(
        {"text": f"🏛️ {banco} ({pct}%)", "callback_data": f"banco:{banco}"}
    )
    if len(fila_bancos) == 2:
      teclado.append(fila_bancos)
      fila_bancos = []
  if fila_bancos:
    teclado.append(fila_bancos)

  return {"inline_keyboard": teclado}


def enviar_mensaje(chat_id, texto, teclado=None, tipo="reply"):
  try:
    url = URL_TELEGRAM + "sendMessage"
    data = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}
    if teclado:
      data["reply_markup"] = teclado
    response = requests.post(url, json=data, timeout=10)
    return response.status_code == 200
  except Exception as e:
    print(f"Error al enviar mensaje Telegram: {e}")
    return False


# ==================== OBTENCIÓN P2P Y BCV ====================


def obtener_precios_con_cache(fiat):
  global cache_precios, cache_tiempo
  ahora = time.time()

  if fiat in cache_precios and fiat in cache_tiempo:
    if ahora - cache_tiempo[fiat] < CACHE_DURACION:
      return cache_precios[fiat]["compra"], cache_precios[fiat]["venta"]

  compra, venta = obtener_precios_p2p_reales(fiat)
  if compra and venta:
    cache_precios[fiat] = {"compra": compra, "venta": venta}
    cache_tiempo[fiat] = ahora

  return compra, venta


def obtener_precios_p2p_reales(fiat):
  try:
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

    data = {
        "asset": "USDT",
        "fiat": fiat,
        "tradeType": "SELL",
        "page": 1,
        "rows": 10,
        "payTypes": [],
    }
    compra = None
    try:
      r = requests.post(url, json=data, headers=headers, timeout=10)
      if r.status_code == 200:
        result = r.json()
        if result.get("data"):
          precios = [
              float(a["adv"]["price"])
              for a in result["data"]
              if 1 < float(a["adv"]["price"]) < 100000
          ]
          if precios:
            compra = min(precios)
    except Exception as e:
      print(f"Error P2P SELL ({fiat}): {e}")

    data["tradeType"] = "BUY"
    venta = None
    try:
      r = requests.post(url, json=data, headers=headers, timeout=10)
      if r.status_code == 200:
        result = r.json()
        if result.get("data"):
          precios = [
              float(a["adv"]["price"])
              for a in result["data"]
              if 1 < float(a["adv"]["price"]) < 100000
          ]
          if precios:
            venta = max(precios)
    except Exception as e:
      print(f"Error P2P BUY ({fiat}): {e}")

    if compra is None or venta is None:
      return None, None
    if compra < venta:
      compra, venta = venta, compra
    return compra, venta
  except Exception as e:
    print(f"Error general P2P ({fiat}): {e}")
    return None, None


def obtener_tasa_bcv_actual():
  tasas = obtener_tasas_bcv()
  if tasas and tasas.get("usd"):
    return tasas["usd"]
  return 45.00


def obtener_tasas_bcv():
  tasas = {
      "usd": 0.0,
      "eur": 0.0,
      "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
  }
  try:
    r_usd = requests.get("https://ve.dolarapi.com/v1/dolares/oficial", timeout=8)
    if r_usd.status_code == 200:
      tasas["usd"] = float(r_usd.json().get("promedio", 0))

    r_eur = requests.get("https://ve.dolarapi.com/v1/euros/oficial", timeout=8)
    if r_eur.status_code == 200:
      tasas["eur"] = float(r_eur.json().get("promedio", 0))

    if tasas["usd"] > 0:
      return tasas
  except Exception as e:
    print(f"Error consultando DolarApi: {e}")

  try:
    url = "https://api.exchangerate-api.com/v4/latest/USD"
    r = requests.get(url, timeout=10)
    if r.status_code == 200:
      data = r.json()
      usd = data.get("rates", {}).get("VES", 0)
      eur = data.get("rates", {}).get("EUR", 0)
      if usd > 0:
        return {
            "usd": usd,
            "eur": usd * eur if eur > 0 else usd * 0.92,
            "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
  except Exception as e:
    print(f"Error consultando ExchangeRate-API: {e}")
  return None


# ==================== TARIFARIOS EN TEXTO ====================


def mostrar_tarifario_usd(chat_id):
  tasa_bcv = obtener_tasa_bcv_actual()
  dolares_lista = [10, 20, 30, 50, 100, 150, 200, 250, 300, 500]

  mensaje = (
      f"📋 *TARIFARIO EN USD*\n🕐 Tasa BCV: {tasa_bcv:.2f} Bs | Perú - Ven"
      f" Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n```\n"
      f"{'Dólares'.ljust(9)}|{'Recibes (Bs)'.ljust(14)}|{'Equivalente'.ljust(12)}\n---------------------------------\n"
  )
  for usd in dolares_lista:
    recibes_val = usd * tasa_bcv
    equiv_soles = (
        recibes_val / TASA_SOLES_TARIFARIO if TASA_SOLES_TARIFARIO > 0 else 0
    )
    mensaje += (
        f"{f'{usd}$'.ljust(9)}|{f'{recibes_val:,.2f}'.ljust(14)}|{f'{equiv_soles:,.2f} S/'.ljust(12)}\n"
    )
  mensaje += "```"
  enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))


def mostrar_tarifario_soles(chat_id):
  tasa_bcv = obtener_tasa_bcv_actual()
  soles_lista = [10, 20, 30, 50, 100, 150, 200, 300, 500, 1000]

  mensaje = (
      f"📋 *TARIFARIO EN SOLES A BOLÍVARES*\n🕐 Tasa BCV: {tasa_bcv:.2f} Bs |"
      f" Perú - Ven Configurada: {TASA_SOLES_TARIFARIO:.2f}\n\n```\n"
      f"{'Enviado'.ljust(10)}|{'Recibes (Bs)'.ljust(14)}|{'Equivalente'.ljust(12)}\n---------------------------------\n"
  )
  for soles in soles_lista:
    recibes_val = soles * TASA_SOLES_TARIFARIO
    equiv_usd = recibes_val / tasa_bcv if tasa_bcv > 0 else 0
    mensaje += (
        f"{f'{soles} S/'.ljust(10)}|{f'{recibes_val:,.2f}'.ljust(14)}|{f'{equiv_usd:,.2f}$'.ljust(12)}\n"
    )
  mensaje += "```"
  enviar_mensaje(chat_id, mensaje, crear_teclado_remesas(chat_id))


# ==================== HISTORIAL Y ALERTAS VES ====================


def guardar_historial_ves(precio):
  historial_ves.append(precio)


def obtener_analisis_ves():
  if not historial_ves or len(historial_ves) < 2:
    return None
  precios = list(historial_ves)
  precio_actual = precios[-1]
  precio_inicio = precios[0]
  cambio = precio_actual - precio_inicio
  cambio_porcentaje = (cambio / precio_inicio) * 100 if precio_inicio != 0 else 0
  tendencia = (
      "↗️ Alcista" if len(precios) > 10 and precios[-1] > precios[-10] else "↘️ Bajista"
  )
  if len(precios) > 10 and abs(precios[-1] - precios[-10]) < 0.01:
    tendencia = "➡️ Lateral"
  return {
      "actual": precio_actual,
      "apertura": precio_inicio,
      "cambio": cambio,
      "cambio_porcentaje": cambio_porcentaje,
      "maximo": max(precios),
      "minimo": min(precios),
      "tendencia": tendencia,
      "muestras": len(precios),
  }


def verificar_alertas(precios):
  global ultimos_precios
  if not precios or "VES" not in precios:
    return

  usuarios = obtener_usuarios()
  if not usuarios:
    return

  precio_actual = precios["VES"]["compra"]
  if ultimos_precios["VES"] is None:
    ultimos_precios["VES"] = precio_actual
    return

  cambio = abs(precio_actual - ultimos_precios["VES"])
  umbral = UMBRALES.get("VES", 0.50)

  if cambio >= umbral:
    direccion = "📈 SUBIÓ" if precio_actual > ultimos_precios["VES"] else "📉 BAJÓ"
    emoji = "🟢" if precio_actual > ultimos_precios["VES"] else "🔴"
    signo = "+" if precio_actual > ultimos_precios["VES"] else ""
    cambio_porcentaje = (
        ((precio_actual - ultimos_precios["VES"]) / ultimos_precios["VES"] * 100)
        if ultimos_precios["VES"] != 0
        else 0
    )

    mensaje = (
        f"\n{emoji} *🔔 ALERTA VES* {emoji}\n\n{direccion} en"
        f" {signo}{cambio:.2f}\n\n📊 *Detalles:*\n• Referencia Anterior:"
        f" {ultimos_precios['VES']:.2f}\n• Precio Actual:"
        f" {precio_actual:.2f}\n• Variación: {signo}{cambio:.2f}"
        f" ({signo}{cambio_porcentaje:.2f}%)\n\n🕐"
        f" {datetime.now().strftime('%H:%M:%S')}\n"
    )

    for usuario in usuarios:
      try:
        enviar_mensaje(usuario, mensaje)
        time.sleep(0.05)
      except Exception as e:
        print(f"Error al enviar alerta a usuario {usuario}: {e}")

    ultimos_precios["VES"] = precio_actual


def mostrar_precios_usdt(chat_id):
  compra, venta = obtener_precios_con_cache("VES")
  if not compra or not venta:
    enviar_mensaje(
        chat_id, "⏳ Obteniendo precios...", crear_teclado_opciones(chat_id)
    )
    return
  mensaje = (
      f"💰 *PRECIOS USDT P2P (VES)*\n🕐"
      f" {datetime.now().strftime('%H:%M:%S')}\n\n*VES*\n  🟢 COMPRA:"
      f" {compra:.2f}\n  🔴 VENTA: {venta:.2f}\n  📊 Spread: {compra-venta:.2f}\n"
  )
  enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))


def mostrar_precio_individual(chat_id, moneda):
  compra, venta = obtener_precios_con_cache(moneda)
  if not compra or not venta:
    enviar_mensaje(
        chat_id,
        f"⏳ Obteniendo precio {moneda}...",
        crear_teclado_opciones(chat_id),
    )
    return
  mensaje = (
      f"💰 *PRECIO {moneda}*\n🕐 {datetime.now().strftime('%H:%M:%S')}\n\n🟢"
      f" COMPRA: {compra:.2f}\n🔴 VENTA: {venta:.2f}\n📊 Spread:"
      f" {compra-venta:.2f}\n"
  )
  enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(chat_id))


# ==================== COMPARATIVA P2P VS BCV ====================


def mostrar_tether_vs_bcv(chat_id):
  compra, venta = obtener_precios_con_cache("VES")
  tasas = obtener_tasas_bcv()

  if not compra or not venta or not tasas:
    enviar_mensaje(
        chat_id,
        "⏳ Obteniendo precios del mercado...",
        crear_teclado_principal(chat_id),
    )
    return

  tasa_bcv_oficial = tasas["usd"]
  tasa_intervencion = tasa_bcv_oficial * 1.005
  media = (compra + venta) / 2.0

  analisis = obtener_analisis_ves()
  if analisis:
    max_24h, min_24h = analisis["maximo"], analisis["minimo"]
    var_pct, tendencia_str = (
        analisis["cambio_porcentaje"],
        analisis["tendencia"],
    )
  else:
    max_24h, min_24h, var_pct, tendencia_str = compra, compra, 0.0, "➡️ Lateral"

  brecha_compra_bcv = compra - tasa_bcv_oficial
  pct_compra_bcv = (
      (brecha_compra_bcv / tasa_bcv_oficial) * 100
      if tasa_bcv_oficial > 0
      else 0.0
  )

  brecha_venta_bcv = venta - tasa_bcv_oficial
  pct_venta_bcv = (
      (brecha_venta_bcv / tasa_bcv_oficial) * 100
      if tasa_bcv_oficial > 0
      else 0.0
  )

  diferencial_bruto = venta - tasa_intervencion
  margen_bruto_pct = (
      (diferencial_bruto / tasa_intervencion) * 100
      if tasa_intervencion > 0
      else 0.0
  )

  fecha_hora_str = datetime.now().strftime("%d/%m %H:%M")

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


# ==================== CALCULADORAS DE MARGEN (BCV Y MESA DE CAMBIO) ====================


def calcular_ganancia_neta(
    chat_id, monto=100.0, pct_tarjeta=1.5, etiqueta_fuente=None
):
  compra_ves, venta_ves = obtener_precios_con_cache("VES")
  tasas = obtener_tasas_bcv()

  if not venta_ves or not tasas:
    enviar_mensaje(
        chat_id,
        "⏳ Obteniendo precios del mercado...",
        crear_teclado_principal(chat_id),
    )
    return

  comision_tarjeta_pct = pct_tarjeta / 100.0
  comision_bpay_pct = PORCENTAJE_BPAY / 100.0

  tasa_bcv = tasas["usd"]
  bcv_mas_medio = tasa_bcv * 1.005
  costo_bcv_monto = monto * bcv_mas_medio

  comision_tarjeta = monto * comision_tarjeta_pct
  comision_bpay = monto * comision_bpay_pct
  total_comisiones = comision_tarjeta + comision_bpay

  usdt_neto = monto - total_comisiones

  total_retornado_bs = usdt_neto * venta_ves
  equivalente_usd_bcv = total_retornado_bs / tasa_bcv if tasa_bcv > 0 else 0.0

  ganancia_bs = total_retornado_bs - costo_bcv_monto
  ganancia_usd = equivalente_usd_bcv - monto
  rendimiento_pct = (ganancia_usd / monto) * 100 if monto > 0 else 0.0
  ganancia_por_dolar = ganancia_usd / monto if monto > 0 else 0.0

  detalle_tarjeta = (
      f"{etiqueta_fuente} ({pct_tarjeta}%)"
      if etiqueta_fuente
      else f"{pct_tarjeta}% Tarjeta Internacional"
  )

  mensaje = f"""🏦 *ANÁLISIS DE MARGEN Y LIQUIDACIÓN (BCV + 0.5%)*
💳 *Comisión Aplicada:* {detalle_tarjeta}
💰 Capital Invertido: *${monto:,.2f} USD*
📊 Tasa Oficial BCV: *{tasa_bcv:.2f} Bs*

━━━━━━━━━━━━━━━━━━━━━━━━━━
1️⃣ *COSTO DE INTERVENCIÓN (Egreso)*
• BCV Oficial: *{tasa_bcv:.2f} Bs*
• BCV + 0.50%: *{bcv_mas_medio:.2f} Bs*
• Total Invertido: *${monto:,.2f} USD* → *{costo_bcv_monto:,.2f} Bs*

━━━━━━━━━━━━━━━━━━━━━━━━━━
2️⃣ *ESTRUCTURA DE COMISIONES*
• Tarjeta Int. ({pct_tarjeta}%): *${comision_tarjeta:,.2f} USD*
• Bpay ({PORCENTAJE_BPAY}%): *${comision_bpay:,.2f} USD*
• TOTAL COMISIONES: *${total_comisiones:,.2f} USD*

━━━━━━━━━━━━━━━━━━━━━━━━━━
3️⃣ *LIQUIDACIÓN NETA (USDT)*
• Capital bruto: *{monto:,.2f} USDT*
• Comisiones: *-{total_comisiones:,.2f} USDT*
• USDT neto obtenido: *{usdt_neto:,.2f} USDT*

━━━━━━━━━━━━━━━━━━━━━━━━━━
4️⃣ *RETORNO EN MERCADO P2P*
• Tasa de Venta USDT: *{venta_ves:.2f} Bs*
• Total Retornado: *{total_retornado_bs:,.2f} Bs*
• Equivalente USD (BCV): *${equivalente_usd_bcv:,.2f}*

━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 *GANANCIA NETA FINAL*
• En Bolívares: *{ganancia_bs:+,.2f} Bs*
• En USD Equivalente: *${ganancia_usd:+,.2f}*
• Rendimiento Operativo: *{rendimiento_pct:+.2f}%*

• 💵 Ganancia por dólar: *${ganancia_por_dolar:+.3f}* por cada $1"""

  enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))


def calcular_ganancia_mesa_cambio(
    chat_id,
    monto=100.0,
    pct_tarjeta=1.5,
    tasa_compra_mesa=45.0,
    etiqueta_fuente=None,
):
  """Calculadora de Margen utilizando una tasa personalizada de Mesa de Cambio."""
  compra_ves, venta_ves = obtener_precios_con_cache("VES")

  if not venta_ves:
    enviar_mensaje(
        chat_id,
        "⏳ Obteniendo precios del mercado...",
        crear_teclado_principal(chat_id),
    )
    return

  comision_tarjeta_pct = pct_tarjeta / 100.0
  comision_bpay_pct = PORCENTAJE_BPAY / 100.0

  # Costo en Bs según la tasa de Mesa de Cambio elegida
  costo_mesa_monto_bs = monto * tasa_compra_mesa

  comision_tarjeta = monto * comision_tarjeta_pct
  comision_bpay = monto * comision_bpay_pct
  total_comisiones = comision_tarjeta + comision_bpay

  usdt_neto = monto - total_comisiones

  total_retornado_bs = usdt_neto * venta_ves
  equivalente_usd_mesa = (
      total_retornado_bs / tasa_compra_mesa if tasa_compra_mesa > 0 else 0.0
  )

  ganancia_bs = total_retornado_bs - costo_mesa_monto_bs
  ganancia_usd = equivalente_usd_mesa - monto
  rendimiento_pct = (ganancia_usd / monto) * 100 if monto > 0 else 0.0
  ganancia_por_dolar = ganancia_usd / monto if monto > 0 else 0.0

  detalle_tarjeta = (
      f"{etiqueta_fuente} ({pct_tarjeta}%)"
      if etiqueta_fuente
      else f"{pct_tarjeta}% Tarjeta Internacional"
  )

  mensaje = f"""🏛️ *ANÁLISIS DE MARGEN — MESA DE CAMBIO*
💳 *Comisión Aplicada:* {detalle_tarjeta}
💰 Capital Invertido: *${monto:,.2f} USD*
⚙️ Tasa Compra Mesa: *{tasa_compra_mesa:.2f} Bs*

━━━━━━━━━━━━━━━━━━━━━━━━━━
1️⃣ *COSTO EN MESA DE CAMBIO (Egreso)*
• Tasa Pactada: *{tasa_compra_mesa:.2f} Bs*
• Total Invertido: *${monto:,.2f} USD* → *{costo_mesa_monto_bs:,.2f} Bs*

━━━━━━━━━━━━━━━━━━━━━━━━━━
2️⃣ *ESTRUCTURA DE COMISIONES*
• Tarjeta Int. ({pct_tarjeta}%): *${comision_tarjeta:,.2f} USD*
• Bpay ({PORCENTAJE_BPAY}%): *${comision_bpay:,.2f} USD*
• TOTAL COMISIONES: *${total_comisiones:,.2f} USD*

━━━━━━━━━━━━━━━━━━━━━━━━━━
3️⃣ *LIQUIDACIÓN NETA (USDT)*
• Capital bruto: *{monto:,.2f} USDT*
• Comisiones: *-{total_comisiones:,.2f} USDT*
• USDT neto obtenido: *{usdt_neto:,.2f} USDT*

━━━━━━━━━━━━━━━━━━━━━━━━━━
4️⃣ *RETORNO EN MERCADO P2P*
• Tasa Venta USDT (Binance): *{venta_ves:.2f} Bs*
• Total Retornado: *{total_retornado_bs:,.2f} Bs*
• Equivalente USD (a Tasa Mesa): *${equivalente_usd_mesa:,.2f}*

━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 *GANANCIA NETA FINAL*
• En Bolívares: *{ganancia_bs:+,.2f} Bs*
• En USD Equivalente: *${ganancia_usd:+,.2f}*
• Rendimiento Operativo: *{rendimiento_pct:+.2f}%*

• 💵 Ganancia por dólar: *${ganancia_por_dolar:+.3f}* por cada $1"""

  enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))


def calcular_conversion_bcv_medio(chat_id, texto_monto):
  tasas = obtener_tasas_bcv()
  if not tasas:
    return
  tasa_bcv = tasas["usd"]
  bcv_mas_medio = tasa_bcv * 1.005
  texto_limpio = texto_monto.strip().lower()

  try:
    if "bs" in texto_limpio:
      monto_bs = float(
          texto_limpio.replace("bs", "").replace(",", ".").strip()
      )
      usd_oficial = monto_bs / tasa_bcv if tasa_bcv > 0 else 0
      usd_mas_medio = monto_bs / bcv_mas_medio if bcv_mas_medio > 0 else 0

      mensaje = f"""⚖️ *CONVERSOR USD / BS*

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

    elif "$" in texto_limpio or "usd" in texto_limpio:
      monto_usd = float(
          texto_limpio.replace("$", "").replace("usd", "").replace(",", ".").strip()
      )
      bs_oficial = monto_usd * tasa_bcv
      bs_mas_medio = monto_usd * bcv_mas_medio

      mensaje = f"""⚖️ *CONVERSOR USD / BS*

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
    print(f"Error en conversión BCV medio: {e}")


def mostrar_historial_ves(chat_id):
  analisis = obtener_analisis_ves()
  if not analisis:
    enviar_mensaje(
        chat_id,
        "📈 *HISTORIAL DE BRECHA VES*\n⏳ Sin datos suficientes aún",
        crear_teclado_principal(chat_id),
    )
    return
  mensaje = (
      f"📈 *HISTORIAL DE BRECHA VES (24h)*\n📊 *Apertura:*"
      f" {analisis['apertura']:.2f} Bs\n📊 *Actual:* {analisis['actual']:.2f}"
      f" Bs\n*Cambio:* {analisis['cambio']:+.2f} Bs"
      f" ({analisis['cambio_porcentaje']:+.1f}%)\n📈 *Máximo:"
      f" * {analisis['maximo']:.2f} Bs\n📉 *Mínimo:* {analisis['minimo']:.2f}"
      f" Bs\n🧭 *Tendencia:* {analisis['tendencia']}"
  )
  enviar_mensaje(chat_id, mensaje, crear_teclado_principal(chat_id))


# ==================== PROCESAR MENSAJES Y CALLBACKS ====================


def procesar_callback_inline(call):
  chat_id = call["message"]["chat"]["id"]
  user_id = call["from"]["id"]
  data = call["data"]

  monto = usuario_monto_temporal.get(user_id, 100.0)
  modo = usuario_modo_calculo.get(user_id, "bcv")

  pct_tarjeta = 1.5
  etiqueta = None

  if data.startswith("pct:"):
    pct_tarjeta = float(data.split("pct:")[1])
    etiqueta = "Selección Manual"
  elif data.startswith("banco:"):
    nombre_banco = data.split("banco:")[1]
    pct_tarjeta = BANCOS_COMISIONES.get(nombre_banco, 1.5)
    etiqueta = nombre_banco

  # Ejecuta el cálculo según el modo seleccionado
  if modo == "mesa_cambio":
    tasa_mesa = usuario_tasa_mesa.get(user_id, 45.0)
    calcular_ganancia_mesa_cambio(
        chat_id,
        monto=monto,
        pct_tarjeta=pct_tarjeta,
        tasa_compra_mesa=tasa_mesa,
        etiqueta_fuente=etiqueta,
    )
  else:
    calcular_ganancia_neta(
        chat_id,
        monto=monto,
        pct_tarjeta=pct_tarjeta,
        etiqueta_fuente=etiqueta,
    )

  usuario_monto_temporal.pop(user_id, None)


def procesar_mensaje(chat_id, user_id, texto):
  global usuario_esperando_calculo, usuario_configurando_soles, usuario_esperando_margen
  global usuario_esperando_tasa_mesa, usuario_tasa_mesa, usuario_modo_calculo
  global TASA_SOLES_TARIFARIO, usuario_monto_temporal

  if not usuario_esta_en_grupo(user_id):
    enviar_mensaje(
        chat_id,
        "⛔ *Acceso Denegado*\n\nDebes ser miembro del grupo o canal autorizado"
        " para utilizar este bot.",
    )
    return

  guardar_usuario(user_id)

  # Configuración de tasa soles por el admin
  if user_id == ADMIN_ID and usuario_configurando_soles.get(user_id, False):
    try:
      TASA_SOLES_TARIFARIO = float(texto.replace(",", "."))
      guardar_configuracion()
      usuario_configurando_soles[user_id] = False
      enviar_mensaje(
          chat_id,
          f"✅ *Tasa Soles actualizada con éxito:* `{TASA_SOLES_TARIFARIO:.2f}`",
          crear_teclado_remesas(user_id),
      )
      return
    except ValueError:
      pass

  # Captura de la Tasa de Mesa de Cambio enviada por el usuario
  if usuario_esperando_tasa_mesa.get(user_id, False):
    try:
      tasa_ingresada = float(texto.replace(",", "."))
      usuario_tasa_mesa[user_id] = tasa_ingresada
      usuario_esperando_tasa_mesa[user_id] = False
      usuario_esperando_margen[user_id] = True

      msg = (
          f"✅ *Tasa Mesa de Cambio registrada:* `{tasa_ingresada:.2f} Bs`\n\n"
          "1️⃣ Envía el **monto en USD** a procesar (ejemplo: `250`).\n"
          "2️⃣ O selecciona abajo la comisión para la base de **$100 USD**:"
      )
      enviar_mensaje(
          chat_id,
          msg,
          teclado=crear_teclado_comisiones_y_bancos(),
          tipo="inline",
      )
      return
    except ValueError:
      enviar_mensaje(
          chat_id,
          "⚠️ Por favor ingresa un número válido para la tasa (ejemplo:"
          " `65.50`).",
      )
      return

  # Entrada del monto en USD para el cálculo de margen
  if usuario_esperando_margen.get(user_id, False):
    try:
      monto = float(texto.replace(",", "."))
      usuario_esperando_margen[user_id] = False
      usuario_monto_temporal[user_id] = monto

      modo = usuario_modo_calculo.get(user_id, "bcv")
      encabezado = (
          f"🏛️ *Mesa de Cambio ({usuario_tasa_mesa.get(user_id, 0):.2f} Bs)*"
          if modo == "mesa_cambio"
          else "🏦 *Calculadora BCV*"
      )

      msg = (
          f"{encabezado}\n💰 Capital a procesar: *${monto:,.2f} USD*\n\n"
          "Selecciona el **% de comisión por tarjeta internacional** a evaluar:"
      )
      enviar_mensaje(
          chat_id,
          msg,
          teclado=crear_teclado_comisiones_y_bancos(),
          tipo="inline",
      )
      return
    except ValueError:
      pass

  # Detección de entradas numéricas explícitas para conversor
  if any(char.isdigit() for char in texto):
    if (
        usuario_esperando_calculo.get(user_id, False)
        or "bs" in texto.lower()
        or "$" in texto
        or "usd" in texto.lower()
    ):
      calcular_conversion_bcv_medio(chat_id, texto)
      usuario_esperando_calculo[user_id] = False
      return

  # Comandos y Navegación del Menú
  if texto == "/start":
    limpiar_estados_usuario(user_id)
    msg = (
        "👋 *¡Bienvenido al Terminal Financiero P2P & Remesas!*\n\nUsa el menú"
        " inferior para realizar conversiones, consultar tarifas o calcular"
        " tus márgenes operativos en tiempo real."
    )
    enviar_mensaje(chat_id, msg, crear_teclado_principal(user_id))

  elif texto == "📈 Comparativa P2P vs BCV":
    mostrar_tether_vs_bcv(chat_id)

  elif texto == "🧮 Conversor USD / Bs":
    usuario_esperando_calculo[user_id] = True
    msg = (
        "🧮 *CONVERSOR OFICIAL DE DIVISAS (USD / VES)*\n\nIngresa el monto a"
        " consultar especificando la moneda al final:\n\n• Ejemplo en"
        " Bolívares: `5000 Bs`\n• Ejemplo en Dólares: `100 USD` o `100 $`"
    )
    enviar_mensaje(chat_id, msg, crear_teclado_principal(user_id))

  elif texto == "📊 Calculadora de Margen":
    limpiar_estados_usuario(user_id)
    usuario_modo_calculo[user_id] = "bcv"
    usuario_esperando_margen[user_id] = True
    usuario_monto_temporal[user_id] = 100.0

    msg = (
        "📊 *SIMULADOR DE MARGEN Y LIQUIDACIÓN P2P (BCV)*\n\n1️⃣ Envía un monto"
        " específico en USD (ejemplo: `250`).\n2️⃣ O bien, presiona directamente"
        " abajo el **porcentaje de comisión** a evaluar para la base de"
        " **$100 USD**:"
    )
    enviar_mensaje(
        chat_id,
        msg,
        teclado=crear_teclado_comisiones_y_bancos(),
        tipo="inline",
    )

  elif texto == "🏛️ Mesa de Cambio":
    limpiar_estados_usuario(user_id)
    usuario_modo_calculo[user_id] = "mesa_cambio"
    usuario_esperando_tasa_mesa[user_id] = True
    usuario_monto_temporal[user_id] = 100.0

    msg = (
        "🏛️ *CALCULADORA DE MARGEN (MESA DE CAMBIO)*\n\n✍️ Ingresa la **tasa de"
        " compra en Bs** pactada en Mesa de Cambio (ejemplo: `65.50`):"
    )
    enviar_mensaje(chat_id, msg, crear_teclado_principal(user_id))

  elif texto == "📈 Historial de brecha VES":
    mostrar_historial_ves(chat_id)

  elif texto == "💼 Panel de Operaciones":
    if user_id == ADMIN_ID:
      enviar_mensaje(
          chat_id,
          "💼 *PANEL DE OPERACIONES & REMESAS*",
          crear_teclado_remesas(user_id),
      )
    else:
      enviar_mensaje(
          chat_id,
          "❌ Acción restringida a administradores.",
          crear_teclado_principal(chat_id),
      )

  elif texto == "📋 Tarifario USD":
    if user_id == ADMIN_ID:
      mostrar_tarifario_usd(chat_id)
    else:
      enviar_mensaje(
          chat_id,
          "❌ Acción restringida a administradores.",
          crear_teclado_remesas(user_id),
      )

  elif texto == "📋 Tarifario Soles":
    if user_id == ADMIN_ID:
      mostrar_tarifario_soles(chat_id)
    else:
      enviar_mensaje(
          chat_id,
          "❌ Acción restringida a administradores.",
          crear_teclado_remesas(user_id),
      )

  elif texto == "⚙️ Ajustar Tasa":
    if user_id == ADMIN_ID:
      usuario_configurando_soles[user_id] = True
      msg = (
          f"⚙️ *CONFIGURACIÓN DE TASA OPERATIVA (PEN/VES)*\n\n• Tasa actual"
          f" registrada: `{TASA_SOLES_TARIFARIO:.2f}`\n\n✍️ Envía el nuevo"
          f" valor de referencia (ejemplo: `3.85`)."
      )
      enviar_mensaje(chat_id, msg, crear_teclado_remesas(user_id))
    else:
      enviar_mensaje(
          chat_id,
          "❌ Acción restringida a administradores.",
          crear_teclado_remesas(user_id),
      )

  elif texto == "⚙️ Mercado P2P":
    msg = (
        "⚙️ *MONITOREO DE MERCADO P2P*\n\nSelecciona una opción para consultar"
        " la cotización en tiempo real:"
    )
    enviar_mensaje(chat_id, msg, crear_teclado_opciones(user_id))

  elif texto == "📊 P2P Multidivisa":
    mostrar_precios_usdt(chat_id)

  elif texto == "🇻🇪 Tasa VES":
    mostrar_precio_individual(chat_id, "VES")

  elif texto == "👥 Usuarios Registrados":
    if user_id == ADMIN_ID:
      usuarios = obtener_usuarios()
      mensaje = f"👥 *Usuarios activos registrados:* {len(usuarios)}"
      for uid in usuarios:
        mensaje += f"\n• `{uid}`"
      enviar_mensaje(chat_id, mensaje, crear_teclado_opciones(user_id))
    else:
      enviar_mensaje(
          chat_id,
          "❌ Acción restringida a administradores.",
          crear_teclado_opciones(user_id),
      )

  elif texto == "⬅️ Volver al Menú":
    limpiar_estados_usuario(user_id)
    enviar_mensaje(chat_id, "🏠 *Menú Principal*", crear_teclado_principal(user_id))

  else:
    try:
      monto_usuario = float(texto.replace(",", "."))
      if monto_usuario > 0:
        usuario_monto_temporal[user_id] = monto_usuario
        modo = usuario_modo_calculo.get(user_id, "bcv")
        encabezado = (
            f"🏛️ *Mesa de Cambio ({usuario_tasa_mesa.get(user_id, 0):.2f} Bs)*"
            if modo == "mesa_cambio"
            else "🏦 *Calculadora BCV*"
        )

        msg = (
            f"{encabezado}\n💰 Capital a procesar: *${monto_usuario:,.2f}"
            " USD*\n\nSelecciona el **% de comisión por tarjeta"
            " internacional** a evaluar:"
        )
        enviar_mensaje(
            chat_id,
            msg,
            teclado=crear_teclado_comisiones_y_bancos(),
            tipo="inline",
        )
    except ValueError:
      if chat_id > 0:
        enviar_mensaje(
            chat_id,
            "⚠️ Comando o formato no reconocido. Por favor, selecciona una"
            " opción del menú.",
            crear_teclado_principal(user_id),
        )


# ==================== RUTAS FLASK Y WEBHOOK ====================


@app.route("/", methods=["GET"])
def home():
  return f"Bot activo 24/7 | Muestras: {len(historial_ves)}", 200


@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
  if request.headers.get("content-type") == "application/json":
    json_string = request.get_data().decode("utf-8")
    update = json.loads(json_string)

    if "callback_query" in update:
      call = update["callback_query"]
      threading.Thread(target=procesar_callback_inline, args=(call,)).start()
      return "OK", 200

    message = update.get("message") or update.get("edited_message")
    if message:
      chat_id = message.get("chat", {}).get("id")
      user_id = message.get("from", {}).get("id")
      texto = message.get("text", "")

      if chat_id and user_id and texto:
        threading.Thread(
            target=procesar_mensaje, args=(chat_id, user_id, texto)
        ).start()

    return "OK", 200
  return "Forbidden", 403


def configurar_webhook():
  url_app = RENDER_URL
  url_set = f"{URL_TELEGRAM}setWebhook?url={url_app}/{TOKEN}"
  try:
    r = requests.get(url_set, timeout=10)
    print("Webhook configurado:", r.json())
  except Exception as e:
    print("Error configurando Webhook:", e)


# ==================== HILO SELF-PING Y ACTUALIZACIÓN ====================


def iniciar_self_ping():
  def ping():
    time.sleep(10)
    while True:
      try:
        res = requests.get(RENDER_URL, timeout=10)
        print(f"[Self-Ping] Status: {res.status_code}")
      except Exception as e:
        print(f"[Self-Ping Error]: {e}")
      time.sleep(270)

  threading.Thread(target=ping, daemon=True).start()


def actualizar_precios():
  global cache_precios, cache_tiempo, ultimos_precios
  while True:
    try:
      precios = {}
      compra, venta = obtener_precios_p2p_reales("VES")
      if compra and venta:
        precios["VES"] = {"compra": compra, "venta": venta}
        cache_precios["VES"] = {"compra": compra, "venta": venta}
        cache_tiempo["VES"] = time.time()
        guardar_historial_ves(compra)
        verificar_alertas(precios)
      time.sleep(60)
    except Exception as e:
      print(f"Error en hilo de actualizar_precios: {e}")
      time.sleep(60)


if __name__ == "__main__":
  cargar_configuracion()
  configurar_webhook()

  iniciar_self_ping()
  threading.Thread(target=actualizar_precios, daemon=True).start()

  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)
