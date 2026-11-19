# 🌐 WORLD PACKET SIMULATOR — un NOC de Internet en tu terminal

> **TL;DR** — Un broker (`server.py`) es la única autoridad de la red mundial; los
> `slave.py` simulan cada salto de cada paquete (latencia, congestión, pérdidas,
> fallos); el `master.py` genera tráfico y lo pinta en un mapa mundi ASCII amarillo
> sobre negro, con los `*` viajando salto a salto. Todo va por WebSockets + JSON y la
> cola de trabajos vive en SQLite, así que puedes matar workers (o el propio
> servidor) y la simulación sigue donde estaba.

```
3 terminales y listo:
  python3 server.py          # broker + monitor HTTP
  python3 slave.py           # (repite en varias terminales / ordenadores)
  python3 master.py --auto-start
```

## Cómo leer esto

| Si quieres…                                   | Ve a                                       |
|-----------------------------------------------|--------------------------------------------|
| Arrancarlo ya                                 | [Instalación](#instalación) → [Ejecución](#ejecución) |
| Entender cómo encaja todo                     | [Arquitectura](#arquitectura)              |
| Saber qué teclas pulsar                       | [Controles](#controles)                    |
| Ver el protocolo JSON                         | [Protocolo](#protocolo-de-eventos-json)    |
| Repasar lease, reintentos, TLS…               | [Robustez y seguridad](#robustez-y-seguridad) |
| Saber qué hace cada fichero                   | [Archivos](#organización-de-archivos)      |
| Ponerlo en Internet / pulso de GitHub         | [`deploy/HOSTING.md`](deploy/HOSTING.md)   |
| Ver el mapa sin poder controlarlo             | [Modo espectador](#6-modo-espectador-solo-ver-el-mapa) |
| Poner el mapa animado en mi perfil de GitHub  | [`github-profile/README.md`](github-profile/README.md) |

---

## Arquitectura

```
 +---------------------------+         +-------------------------------------------+
 | [ master.py ]  NOC        |  WS/JSON|  [ server.py ]  BROKER  (autoridad)        |
 |  - mapa ASCII + panel     |<------->|                                           |
 |  - generador de tráfico   | create_ |  Simulation (simulation.py)               |
 |  - teclado / atajos Alt   | packet  |   - Network + Dijkstra (network.py)       |
 +---------------------------+ control |   - Packet lifecycle   (packets.py)       |
        ^  packet_update /             |                                           |
        |  event / stats / network     |  Planificador:  capacidad · lease ·       |
        |                              |  heartbeats · reintentos · región         |
 +---------------------------+         |                                           |
 | [ master.py ] observador  |<------->|  SQLite (WAL)                             |
 +---------------------------+         |   jobs · packets · events · kv            |
                                       |                                           |
 +---------------------------+  job    |  Monitor HTTP :8766                       |
 | [ slave.py ] EUROPE  x4   |<------->|   GET /  (panel)   GET /api/stats (JSON)  |
 +---------------------------+ result  |   GET /api/map (texto)  GET /health       |
 +---------------------------+ failed  +-------------------------------------------+
 | [ slave.py ] ASIA    x6   |<------->          ^
 +---------------------------+ heartbeat         | Bearer WPS_MONITOR_TOKEN
 +---------------------------+                   |
 | [ slave.py ] ANY     x4   |<------->   [ navegador ]
 +---------------------------+
```

### Un paquete, salto a salto

Cada **salto** de un paquete es un **trabajo** en la cola. El broker no "inventa" el
resultado: espera a que un slave lo simule.

```
 master              server (broker)                         slave
   | create_packet      |                                       |
   |------------------->| Dijkstra -> ruta                      |
   |                    | packet CREATED -> QUEUED              |
   |                    | INSERT job(hop 0)  [queued]           |
   |                    |--- job {src,dst,duration,loss...} --->|  procesar en router
   |<-- packet_update --| [running, lease=now+30s]              |  latencia del enlace
   |   TRANSMITTING     |                                       |  ¿congestión? ¿pérdida?
   |   (el * avanza)    |<-------------- heartbeat (ids) -------|  ¿fallo temporal?
   |                    |<-------------- result (forwarded) ----|
   |<-- packet_update --| hop+1 · ROUTING · INSERT job(hop 1)   |
   |        ...         |                 ...                   |
   |<-- DELIVERED ------| último salto: ACK                     |
```

### Esquema SQLite

```
 +------------------------------+     +----------------------------+
 | jobs                         |     | packets                    |
 |------------------------------|     |----------------------------|
 | id PK   packet_id   hop      |---->| id PK   status             |
 | src  dst  region  priority   |     | data (JSON del paquete)    |
 | status queued|running|done|  |     | created  updated           |
 |        failed|cancelled      |     +----------------------------+
 | worker attempts lease        |     +----------------------------+
 | payload result error updated |     | events  id ts level text   |
 +------------------------------+     +----------------------------+
  idx(status,priority,updated)        +----------------------------+
  idx(packet_id,status)               | kv  network · counters ·   |
  idx(worker,status) idx(status,lease)|     seq · control          |
                                      +----------------------------+
```

---

## Instalación

Requiere **Python 3.10+**.

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Genera TRES tokens distintos y pégalos en .env:
python3 -c "import secrets;print(secrets.token_urlsafe(32))"
```

Los tokens **nunca** están en el código: se leen de variables de entorno (o del `.env`
del directorio desde el que lanzas cada programa).

## Ejecución

### 1. El servidor

```bash
python3 server.py                    # ws://0.0.0.0:8765  +  monitor http://0.0.0.0:8766
python3 server.py --reset            # borra la base de datos y empieza de cero
python3 server.py --lease 30 --max-attempts 4 --db wps.sqlite3
```

### 2. Un master (centro de control)

```bash
python3 master.py                         # se conecta y espera órdenes
python3 master.py --auto-start --traffic HIGH
python3 master.py --server ws://192.168.1.50:8765
```

Usa una terminal de **140×42 o más** (mínimo 80×24; por debajo de 118 columnas el
panel lateral se compacta). El primer master conectado es el **generador** de tráfico;
los siguientes son **observadores** que ven exactamente la misma simulación. Si el
generador se va, el broker promociona a otro.

### 3. Varios slaves

```bash
python3 slave.py --name eu-1   --region EUROPE --max-tasks 4
python3 slave.py --name asia-1 --region ASIA   --max-tasks 6 --capacity 2
python3 slave.py --name any-1                  --max-tasks 4
```

Desde **otro ordenador**: copia el proyecto, pon el mismo `WPS_SLAVE_TOKEN` en su `.env`
y apunta al servidor:

```bash
python3 slave.py --server ws://IP_DEL_SERVIDOR:8765 --name portatil --region AMERICA
```

`--region` hace que el broker prefiera darle los saltos que salen de routers de esa
región (AMERICA, EUROPE, AFRICA, MIDDLE_EAST, ASIA, OCEANIA o ANY). `--max-tasks` es
la cola interna/concurrencia; `--capacity` acelera el procesamiento simulado.

Puedes matar un slave (Ctrl+C, cerrar la terminal, desenchufar el cable): sus trabajos
vuelven a la cola, se reasignan y el slave se reconecta solo cuando vuelve.

### 4. Niveles de tráfico

```bash
python3 master.py --auto-start --traffic LOW      # ~0.4 paquetes/s
python3 master.py --auto-start --traffic MEDIUM   # ~1.5 paquetes/s
python3 master.py --auto-start --traffic HIGH     # ~5 paquetes/s (congestión garantizada)
```

…o en caliente con `TRAFFIC LOW|MEDIUM|HIGH` / `Alt+1`, `Alt+2`, `Alt+3`.

### 5. El monitor HTTP

Abre `http://IP_DEL_SERVIDOR:8766` y pega el `WPS_MONITOR_TOKEN` cuando lo pida (se
guarda solo en la pestaña). Muestra estado general, contadores, mapa en vivo,
trabajadores con su ocupación y última actividad, nodos y conexiones.

```bash
curl -H "Authorization: Bearer $WPS_MONITOR_TOKEN" http://localhost:8766/api/stats   # JSON
curl -H "Authorization: Bearer $WPS_MONITOR_TOKEN" http://localhost:8766/api/map     # mapa ASCII
curl http://localhost:8766/health
```

### 6. Modo espectador (solo ver el mapa)

Mismo mapa, panel y registro en directo, pero en solo lectura. Usa el token de monitor,
así que en esa máquina nunca hace falta el token del master:

```bash
WPS_MONITOR_TOKEN=... python3 master.py --viewer --server wss://tu-servidor/ws
```

Solo admite `TRACK`, `STATUS`, `HELP` y `QUIT`; cualquier orden de control la rechaza el
propio broker.

### 7. En Internet y con el pulso de GitHub

Todo lo necesario (túnel temporal, túnel con nombre, VPS con Caddy, servicios systemd y
el token de GitHub) está en **[`deploy/HOSTING.md`](deploy/HOSTING.md)**. Resumen:

```bash
./deploy/quick-tunnel.sh              # túnel temporal: imprime la dirección wss:// para los slaves
./deploy/install-services.sh          # broker como servicio de usuario (arranca solo)
```

Con `WPS_GITHUB_TOKEN` en el `.env` del servidor, cada visita nueva a tus repos lanza un
paquete `GITHUB → VALENCIA` y una onda de luz que recorre el mapa; la zona de Valencia
brilla más cuantas más visitas haya hoy. `PULSE` / **Alt+G** lanza uno de prueba.

### 8. En tu perfil de GitHub (sin servidor)

`profile_svg.py` genera un **SVG animado** del mapa (paquetes GITHUB → Valencia, ondas de
luz y contadores) y una GitHub Action lo regenera cada 3 horas con el tráfico de tus repos.
Instrucciones en **[`github-profile/README.md`](github-profile/README.md)**.

```bash
python3 profile_svg.py --out wps-map.svg --views 12 --views14 140   # vista previa local
```

### Modo sin pantalla (scripts y pruebas)

```bash
python3 master.py --headless --auto-start --traffic HIGH --duration 60
echo "FAIL NODE LONDON" | python3 master.py --headless --stdin-commands
python3 master.py --headless --command "FAIL LINK DUBAI SINGAPORE" --command START
```

---

## Controles

Escribe la orden y pulsa **Enter** (↑/↓ recorren el historial, Esc limpia).

| Orden                              | Qué hace                                             |
|------------------------------------|------------------------------------------------------|
| `START` / `STOP`                   | Inicia / detiene la generación de tráfico            |
| `PAUSE` / `RESUME`                 | Congela / reanuda toda la simulación (también slaves)|
| `TRAFFIC LOW\|MEDIUM\|HIGH`        | Nivel de tráfico                                     |
| `STATUS`                           | Resumen de estadísticas y workers en el registro     |
| `FAIL NODE [ciudad]`               | Tira un router (aleatorio si no indicas cuál)        |
| `RESTORE NODE [ciudad\|ALL]`       | Recupera un router                                   |
| `FAIL LINK [a b]`                  | Corta un enlace (`FAIL LINK DUBAI SINGAPORE`)        |
| `RESTORE LINK [a b\|ALL]`          | Recupera un enlace                                   |
| `SEND a b [bytes]`                 | Paquete manual de alta prioridad (se sigue en el mapa)|
| `TRACK [n\|OFF]`                   | Sigue el paquete nº n (sin número: el último)        |
| `PULSE`                            | Pulso de GitHub de prueba (GITHUB → VALENCIA + onda) |
| `HELP`                             | Muestra/oculta la ayuda                              |
| `QUIT`                             | Salir                                                |

Las ciudades aceptan nombre, código o alias en español: `TOKIO`, `LONDRES`,
`NUEVA YORK`, `SÃO PAULO`, `SYD`, `dubai`…

### Atajos Alt (con overlay)

Al pulsar cualquier atajo `Alt+…` aparece el panel de ayuda durante 2,5 s (una
terminal no puede detectar "mantener Alt", así que se muestra mientras los usas);
`Alt+H` o `F1` lo dejan fijo.

```
Alt+S  start        Alt+X  stop          Alt+P  pausa / sigue
Alt+1  tráfico bajo Alt+2  medio         Alt+3  alto
Alt+N  fallo nodo   Alt+L  fallo enlace  Alt+R  restaurar todo
Alt+T  seguir último Alt+G  pulso GitHub  Alt+H / F1  ayuda   Alt+Q  salir
```

> En macOS Terminal activa *"Usar Opción como tecla Meta"*. En Windows los atajos Alt
> no llegan a la consola: escribe las órdenes (funcionan igual) y usa Windows Terminal.

### Símbolos del mapa

```
@ router   S origen   D destino   * paquete   # conexión activa   X paquete perdido
tierra más clara / onda de luz = visitas de GitHub llegando a Valencia
! nodo con problemas (caído o congestionado)   + nodo recuperado   ~ enlace caído
= enlace congestionado   - / \ |  enlace libre
```

---

## Protocolo de eventos JSON

Todo mensaje se valida (tipo, campos, rangos, tamaño máximo 64 KB) antes de procesarse.

```jsonc
// registro (primer mensaje, obligatorio en 10 s)
{"role": "master", "token": "…", "name": "noc"}
{"role": "slave",  "token": "…", "name": "eu-1", "id": "a1b2c3",
 "region": "EUROPE", "capacity": 1.0, "max_tasks": 4}

// master -> server
{"type": "create_packet", "source": "MADRID", "destination": "TOKYO", "size": 1500, "priority": 1}
{"type": "control", "action": "start|stop|pause|resume"}
{"type": "traffic", "level": "LOW|MEDIUM|HIGH"}
{"type": "link_failure", "source": "DUBAI", "destination": "SINGAPORE"}
{"type": "fail_node", "node": "LONDON"}   {"type": "restore_link", "source": "ALL"}

// server -> master
{"type": "packet_update", "id": "PKT-000001", "current_node": "LONDON",
 "next_node": "DUBAI", "status": "TRANSMITTING", "route": [...], "hop": 1, ...}
{"type": "event", "level": "info|ok|warn|alert", "text": "PACKET #0001 DELIVERED AT TOKYO ..."}
{"type": "network_update", "network": {...}}   {"type": "stats", "stats": {...}, "cluster": {...}}

// server <-> slave
{"type": "job", "id": "PKT-000001:1:9f2c1a", "src": "LONDON", "dst": "DUBAI",
 "duration": 2.1, "latency_ms": 96.3, "loss_prob": 0.01, "fail_prob": 0.02, "congestion": 0.2, ...}
{"type": "result", "id": "…", "outcome": "forwarded|lost", "latency_ms": 97.1, "congested": false}
{"type": "failed", "id": "…", "error": "fallo temporal del router LONDON"}
{"type": "heartbeat", "ids": ["…"], "load": 2}
```

Ciclo de vida visible en el registro:

```
PACKET #0001 CREATED: MADRID -> TOKYO (1500 B)
PACKET #0001 ROUTE: MADRID -> LONDON -> DUBAI -> SINGAPORE -> TOKYO (~262 ms)
PACKET #0001 TRANSMITTING FROM MADRID -> LONDON
PACKET #0001 TRANSMITTING VIA LONDON -> DUBAI
PACKET #0001 TRANSMITTING VIA DUBAI -> SINGAPORE
PACKET #0001 TRANSMITTING VIA SINGAPORE -> TOKYO
PACKET #0001 DELIVERED AT TOKYO (268 ms, 4 hops) ACK
```

Estados: `CREATED → QUEUED → TRANSMITTING ⇄ ROUTING → DELIVERED | LOST | FAILED`
(las transiciones están validadas: un resultado tardío no puede "resucitar" un paquete).

---

## Modelo de red y tiempo

* **Grafo**: 13 routers (los 9 pedidos + Los Ángeles, Mumbai, El Cairo y Valencia) más
  un nodo externo `GITHUB` (San Francisco) que solo emite los pulsos de visitas, y 26
  enlaces con latencia, ancho de banda, congestión y disponibilidad. Si actualizas el
  proyecto con una base de datos antigua, los nodos nuevos se añaden solos al arrancar.
* **Dijkstra** sobre la latencia *efectiva*: `latencia · (1 + 3·congestión²)` + coste del
  router (triple si está congestionado). Ignora nodos caídos y enlaces cortados.
* **Rutas alternativas**: al caer un nodo o enlace se recalculan las rutas de los
  paquetes afectados desde su posición actual; si un enlace cae *durante* un salto, el
  paquete se retransmite por otro camino. Si no hay camino → `FAILED`; si el router
  donde está el paquete cae → `LOST`.
* **Duración de un salto** = latencia del enlace (con congestión) + transmisión
  (`bytes·8 / Mbps`) + procesamiento del router (según su carga), escalada por
  `WPS_TIME_SCALE` y limitada a 0,8–9 s reales. El slave añade su propio
  procesamiento, congestión aleatoria y la posible pérdida a mitad de enlace (la `X`
  aparece donde se perdió).
* **Congestión dinámica**: cada segundo cada enlace tiende hacia su carga real
  (paquetes en tránsito / ancho de banda) más un sesgo según el nivel de tráfico.
  Un router con más paquetes que su capacidad pasa a `congested` (`!`).

## Robustez y seguridad

| Mecanismo                 | Implementación                                                         |
|---------------------------|------------------------------------------------------------------------|
| Autenticación             | Token distinto por rol (master / slave / monitor), `hmac.compare_digest` |
| Secretos                  | Solo variables de entorno / `.env` (en `.gitignore`)                    |
| Validación                | Esquema por tipo de mensaje y rol; JSON inválido → error, >50 → expulsión |
| Límites                   | 64 KB por mensaje, colas de salida acotadas (cliente lento → se cierra) |
| Sin duplicados            | `UPDATE … WHERE status='queued'` + comprobación de `rowcount`; IDs de worker únicos |
| Lease                     | 30 s renovados por heartbeat; al expirar el trabajo vuelve a la cola   |
| Heartbeats                | Slave cada 5 s; 20 s de silencio → desconexión y recuperación          |
| Reintentos                | Máximo 4 por salto (`--max-attempts`); una desconexión no consume intento |
| Persistencia              | SQLite WAL: trabajos, paquetes, eventos, contadores y topología; reinicio sin pérdidas |
| Reconexión                | Master y slaves reintentan cada 3 s; el master recibe un `snapshot` completo |
| Concurrencia              | `asyncio` + `asyncio.Lock` alrededor de cada mutación de estado/BD      |

### TLS fuera de la red local

```bash
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout certs/server.key -out certs/server.crt -subj "/CN=mi-servidor" \
  -addext "subjectAltName=DNS:mi-servidor,IP:203.0.113.10"
# servidor (.env):  WPS_TLS_CERT=certs/server.crt  WPS_TLS_KEY=certs/server.key
# clientes  (.env): WPS_SERVER=wss://mi-servidor:8765   WPS_TLS_CA=certs/server.crt
```

Con TLS activo el monitor también pasa a `https://`. Si el servidor escucha fuera de
`localhost` sin TLS, lo avisa al arrancar (y los slaves también).

---

## Organización de archivos

```
world-packet-simulator/
├── server.py        broker WebSocket, planificador SQLite, monitor HTTP
├── master.py        centro de control: TUI sin parpadeo, generador, teclado
├── slave.py         trabajador: simula cada salto (latencia/congestión/pérdida/fallo)
├── network.py       grafo, topología por defecto, Dijkstra, alias de ciudades
├── packets.py       modelo de paquete, estados y transiciones válidas
├── simulation.py    lógica temporal: saltos, rerouting, congestión, estadísticas
├── renderer.py      mapa mundi ASCII rasterizado + panel + registro + ayuda
├── github_feed.py   pulso anónimo de visitas (API de tráfico de GitHub)
├── profile_svg.py   SVG animado del mapa para el README de tu perfil
├── github-profile/  Action (.github/workflows/wps-map.yml), install.sh, guía
├── config.py        parámetros, colores, límites y lectura de secretos/TLS
├── deploy/          HOSTING.md, quick-tunnel.sh, install-services.sh,
│                    cloudflared-config.yml, Caddyfile, systemd/
├── requirements.txt
├── .env.example
└── README.md
```

Parámetros útiles en `config.py`: `TIME_SCALE` (velocidad visual), `TRAFFIC_LEVELS`,
`MAX_PENDING_PACKETS` (contrapresión), probabilidades de pérdida/fallo, `LEASE_SECONDS`,
`HEARTBEAT_*` y la paleta (`COLOR_*`, ANSI 256: 178 ≈ `#d4b000`, fondo 233 ≈ `#151515`).

## Problemas frecuentes

| Síntoma                                   | Solución                                              |
|-------------------------------------------|-------------------------------------------------------|
| `Falta la variable de entorno WPS_…`      | Crea el `.env` (ver Instalación) en el directorio desde el que lanzas |
| El master dice `Unauthorized`             | El token del `.env` del master no coincide con el del servidor |
| Los paquetes se quedan en `QUEUED`        | No hay slaves conectados (panel → Trabajadores)       |
| `Duplicate worker id`                     | Dos slaves con el mismo `--id`; quítalo para que se genere uno |
| Colores raros                             | Usa una terminal con 256 colores (`TERM=xterm-256color`) |
| `Terminal demasiado pequeña`              | Agranda la ventana (recomendado 140×42)               |
| Quiero empezar de cero                    | `python3 server.py --reset`                           |
