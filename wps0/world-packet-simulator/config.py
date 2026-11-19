#!/usr/bin/env python3
"""Configuración y parámetros de WORLD PACKET SIMULATOR.

Los secretos NUNCA se escriben en el código: se leen de variables de entorno
(o de un fichero .env opcional en el directorio de trabajo).

    WPS_MASTER_TOKEN   token del centro de control (master.py)
    WPS_SLAVE_TOKEN    token de los trabajadores (slave.py)
    WPS_MONITOR_TOKEN  token del monitor HTTP (navegador / API JSON)

Opcionales:
    WPS_SERVER         URL del broker para clientes (ws://127.0.0.1:8765)
    WPS_TLS_CERT       certificado PEM del servidor  -> activa wss://
    WPS_TLS_KEY        clave privada PEM del servidor
    WPS_TLS_CA         CA (PEM) que deben aceptar los clientes con wss://
"""
from __future__ import annotations

import os
import ssl
from pathlib import Path

# --------------------------------------------------------------------------
# .env opcional (no sobrescribe variables ya definidas en el entorno)
# --------------------------------------------------------------------------

def load_dotenv(path: str | os.PathLike = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()

# --------------------------------------------------------------------------
# Red / puertos
# --------------------------------------------------------------------------
DEFAULT_HOST = os.environ.get("WPS_HOST", "0.0.0.0")
DEFAULT_WS_PORT = int(os.environ.get("WPS_PORT", "8765"))
DEFAULT_MONITOR_PORT = int(os.environ.get("WPS_MONITOR_PORT", "8766"))
DEFAULT_SERVER_URL = os.environ.get("WPS_SERVER", f"ws://127.0.0.1:{DEFAULT_WS_PORT}")
DEFAULT_DB = os.environ.get("WPS_DB", "wps.sqlite3")

# --------------------------------------------------------------------------
# Límites y robustez
# --------------------------------------------------------------------------
MAX_MESSAGE_BYTES = 64 * 1024          # tamaño máximo de un mensaje WebSocket
MAX_OUTBOUND_QUEUE = 2000              # mensajes pendientes por cliente antes de expulsarlo
HELLO_TIMEOUT = 10                     # s para enviar el mensaje de registro
LEASE_SECONDS = 30                     # duración del lease de un trabajo
HEARTBEAT_INTERVAL = 5                 # s entre heartbeats de los slaves
HEARTBEAT_TIMEOUT = 20                 # s sin noticias -> slave desconectado
MAX_ATTEMPTS = 4                       # reintentos máximos por salto
MAX_SLAVE_TASKS = 16                   # tope de tareas simultáneas por slave
MAX_PENDING_PACKETS = 120              # contrapresión: paquetes vivos simultáneos
MAX_NAME_LEN = 60
RECONNECT_DELAY = 3                    # s antes de reintentar la conexión
TICK_SECONDS = 1.0                     # ticker del servidor
STATS_BROADCAST_SECONDS = 1.0
EVENT_RETENTION = 5000                 # filas de eventos conservadas en SQLite
PACKET_RETENTION = 20000               # paquetes terminados conservados en SQLite

# --------------------------------------------------------------------------
# Simulación temporal
# --------------------------------------------------------------------------
# 1 ms de latencia simulada = TIME_SCALE ms reales en pantalla.
TIME_SCALE = float(os.environ.get("WPS_TIME_SCALE", "22"))
MIN_HOP_SECONDS = 0.8
MAX_HOP_SECONDS = 9.0

PACKET_MIN_SIZE = 64
PACKET_MAX_SIZE = 65535

BASE_LOSS_PROB = 0.004                 # pérdida base por salto
CONGESTION_LOSS_FACTOR = 0.10          # + pérdida por congestión del enlace
CONGESTED_NODE_LOSS = 0.04             # + pérdida si el router está congestionado
TEMP_FAILURE_PROB = 0.02               # fallo temporal del trabajador (se reintenta)

# Paquetes por segundo generados por el master según el nivel de tráfico
TRAFFIC_LEVELS = {"LOW": 0.4, "MEDIUM": 1.5, "HIGH": 5.0}
DEFAULT_TRAFFIC = "MEDIUM"

# --------------------------------------------------------------------------
# Pulso de visitas de GitHub (opcional; ver github_feed.py)
# --------------------------------------------------------------------------
GITHUB_HOME = os.environ.get("WPS_GITHUB_HOME", "VALENCIA")   # ciudad que se ilumina
GITHUB_PULSE_SPACING = 1.5            # s entre pulsos cuando llegan varias visitas a la vez

# --------------------------------------------------------------------------
# Estética de terminal (todo amarillo sobre negro; colores ANSI 256)
# --------------------------------------------------------------------------
COLOR_BG = 233        # #121212  (≈ #151515 del diseño base)
COLOR_TEXT = 178      # #d7af00  (≈ #d4b000 del diseño base)
COLOR_LAND = 58       # #5f5f00  amarillo oliva oscuro
COLOR_COAST = 100     # #878700
COLOR_LINK = 136      # #af8700
COLOR_ACTIVE = 178
COLOR_NODE = 220      # #ffd700
COLOR_PACKET = 226    # #ffff00
COLOR_DIM = 94        # #875f00

# --------------------------------------------------------------------------
# Secretos
# --------------------------------------------------------------------------

def secret(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise SystemExit(
            f"Falta la variable de entorno {name}.\n"
            f"Genera un token con:  python3 -c \"import secrets;print(secrets.token_urlsafe(32))\"\n"
            f"y expórtalo (export {name}=...) o añádelo a un fichero .env (ver .env.example)."
        )
    return value


def server_ssl_context() -> ssl.SSLContext | None:
    """Contexto TLS del servidor si WPS_TLS_CERT y WPS_TLS_KEY están definidos."""
    cert = os.environ.get("WPS_TLS_CERT")
    key = os.environ.get("WPS_TLS_KEY")
    if not cert or not key:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def client_ssl_context(url: str) -> ssl.SSLContext | None:
    """Contexto TLS de cliente para URLs wss:// (con CA propia opcional)."""
    if not url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    ca = os.environ.get("WPS_TLS_CA")
    if ca:
        ctx.load_verify_locations(ca)
    return ctx


def normalize_url(url: str) -> str:
    """Acepta también http(s):// (p. ej. la URL de un túnel) y la convierte a ws(s)://."""
    url = url.strip().rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    elif "://" not in url:
        url = "ws://" + url
    if not url.startswith(("ws://", "wss://")):
        raise SystemExit(f"URL de servidor no válida: {url}  (usa ws://host:8765 o wss://host)")
    return url


def is_local_url(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
    return host in ("127.0.0.1", "localhost", "::1")
