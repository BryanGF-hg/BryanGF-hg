# 🛰️ WPS online — hosting + pulso de visitas de GitHub

> **TL;DR** — Tres formas de poner el broker en Internet, de la más rápida a la más
> seria: **A)** túnel temporal (2 minutos, sin cuenta), **B)** túnel con nombre en tu
> Mint (dirección fija, gratis con un dominio en Cloudflare), **C)** VPS con Caddy.
> Aparte, con un token de GitHub cada visita nueva a tus repos lanza un paquete
> `GITHUB → VALENCIA` y una onda de luz por el mapa.

## Cómo leer esto

| Quiero…                                         | Sección |
|-------------------------------------------------|---------|
| Probar ya desde clase / otro PC                 | [A. Túnel temporal](#a-túnel-temporal-2-minutos) |
| Una dirección fija para enseñar el proyecto     | [B. Túnel con nombre](#b-túnel-con-nombre-dirección-fija) |
| Un servidor que no dependa de mi portátil       | [C. VPS + Caddy](#c-vps--caddy) |
| Que arranque solo al encender el PC             | [Servicios systemd](#servicios-systemd-arranque-automático) |
| Que el mapa se ilumine con mis visitas de GitHub| [Pulso de GitHub](#pulso-de-visitas-de-github) |

```
                          Internet (443, TLS)
   PC del aula ─┐                │
   Windows      ├── wss://…/ws ──┤        tu Mint / VPS
   casa / móvil ┘                ▼   ┌──────────────────────────────┐
                         ┌───────────┤ cloudflared  ó  Caddy        │
   navegador ── https:// ┤           │   /ws  → 127.0.0.1:8765 broker│
                         └───────────┤   /    → 127.0.0.1:8766 monitor
                                     │ server.py (solo escucha local)│
   api.github.com ◄── cada 5 min ────┤ github_feed.py (pulso)        │
                                     └──────────────────────────────┘
```

---

## A. Túnel temporal (2 minutos)

Sin cuenta ni dominio. La dirección cambia cada vez.

```bash
# Instalar cloudflared (una vez)
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb

# Terminal 1
python3 server.py
# Terminal 2: abre broker + monitor y te imprime las direcciones listas para copiar
./deploy/quick-tunnel.sh
```

En los slaves basta con pegar la dirección (`https://` o `wss://`, da igual):

```powershell
$env:WPS_SERVER = "wss://algo-raro.trycloudflare.com"
uv run --python 3.12 --with websockets python slave.py --name win-1
```

## B. Túnel con nombre (dirección fija)

Necesitas un dominio gestionado en Cloudflare (la cuenta es gratis; el dominio, unos pocos
euros al año). Todo va por **un único dominio**: `/ws` es el broker y `/` el monitor.

```bash
cloudflared tunnel login                         # abre el navegador, eliges tu dominio
cloudflared tunnel create wps                    # crea ~/.cloudflared/<TUNNEL_ID>.json
cloudflared tunnel route dns wps wps.TUDOMINIO.com
```

Edita `deploy/cloudflared-config.yml` (USUARIO, TUNNEL_ID, TUDOMINIO) y prueba:

```bash
python3 server.py --host 127.0.0.1               # solo local: el túnel es la puerta
cloudflared tunnel --config deploy/cloudflared-config.yml run wps
```

Clientes:

```bash
WPS_SERVER=wss://wps.TUDOMINIO.com/ws            # slaves, master, espectadores
https://wps.TUDOMINIO.com                        # monitor en el navegador
```

## C. VPS + Caddy

En un VPS con Debian/Ubuntu y un dominio apuntando a su IP:

```bash
sudo apt install -y python3-venv caddy
git clone <tu repo> wps && cd wps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env                 # tokens + GitHub
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile     # cambia TUDOMINIO
sudo systemctl reload caddy                       # HTTPS automático
```

Abre en el firewall del VPS solo los puertos 80 y 443; el 8765/8766 quedan internos.

## Servicios systemd (arranque automático)

Sin root: los servicios son de tu usuario.

```bash
./deploy/install-services.sh             # solo el broker
./deploy/install-services.sh --tunnel    # broker + túnel con nombre (opción B)
sudo loginctl enable-linger $USER        # opcional: que sigan aunque cierres sesión
journalctl --user -u wps-server -f       # ver logs
```

> El broker como servicio escucha en `127.0.0.1`. Si además quieres slaves en tu red
> local conectando directo (`ws://IP:8765`), edita `~/.config/systemd/user/wps-server.service`
> y quita `--host 127.0.0.1`.

---

## Pulso de visitas de GitHub

Cada visita **nueva** a tus repos se convierte en:

```
1. evento   GITHUB VISIT (world-packet-simulator) -> VALENCIA
2. paquete  GITHUB -> NEW_YORK -> MADRID -> VALENCIA   (lo procesan los slaves)
3. luz      onda que sale de Valencia y recorre todo el mapa (4 s)
4. brillo   la zona de Valencia se queda más clara según las visitas de hoy
              0 · 1-5 · 6-20 · 21-50 · 51+   →   5 niveles de amarillo
```

**Privacidad:** la API de tráfico de GitHub solo da recuentos por día (visitas y
visitantes únicos de los últimos 14 días). No hay IPs, nombres ni ubicaciones: el pulso es
anónimo por diseño.

### 1. Crear el token (fine-grained)

GitHub → *Settings* → *Developer settings* → *Personal access tokens* → *Fine-grained tokens*
→ *Generate new token*:

| Campo                 | Valor                                                       |
|-----------------------|-------------------------------------------------------------|
| Expiration            | la que quieras (p. ej. 90 días)                             |
| Repository access     | *All repositories* o *Only select repositories*             |
| Permissions → Repository → **Administration** | **Read-only** (es lo que pide la API de tráfico) |

El resto, sin permisos. Copia el token (`github_pat_…`).

### 2. Configurarlo en el `.env` del SERVIDOR (solo ahí)

```ini
WPS_GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxx
WPS_GITHUB_HOME=VALENCIA            # ciudad que se ilumina
# WPS_GITHUB_REPOS=usuario/repo1,usuario/repo2   # vacío = todos los repos de los que eres dueño
# WPS_GITHUB_INTERVAL=300           # segundos entre consultas (mín. 15)
```

Reinicia `server.py`. En el log verás `Pulso de GitHub activo: ciudad base VALENCIA`, y en
el registro del master `GITHUB FEED ONLINE: N repos, X visits (14d)`. La primera consulta solo
toma la línea base (no dispara 14 días de golpe); a partir de ahí, cada visita nueva es un
pulso, repartido cada 1,5 s si llegan varias juntas.

### 3. Probarlo sin esperar visitas

En el master: `PULSE` (o **Alt+G**). Lanza un pulso de prueba aunque no haya token.

### Problemas frecuentes

| Panel GITHUB dice                       | Solución                                                  |
|-----------------------------------------|-----------------------------------------------------------|
| `desactivado (sin GITHUB_TOKEN)`        | Falta `WPS_GITHUB_TOKEN` en el `.env` del servidor        |
| `sin acceso al tráfico (HTTP 403)`      | Al token le falta *Administration: Read-only* o no incluye esos repos |
| `N repo(s) sin acceso`                  | Repos de otros (forks de organizaciones, etc.): ignóralo o fija `WPS_GITHUB_REPOS` |
| Hoy hay visitas pero no llegan pulsos   | GitHub no publica el tráfico al instante: llegan en tandas cuando lo actualiza |
| Pulsos pero ningún `*` se mueve         | No hay slaves conectados: los paquetes esperan en `QUEUED` (la onda de luz sí sale) |
