#!/usr/bin/env bash
# Abre DOS túneles temporales de Cloudflare (sin cuenta ni dominio):
#   - broker WebSocket (8765)  -> para slaves / masters / espectadores
#   - monitor HTTP     (8766)  -> para el navegador
# Las direcciones cambian cada vez que lo lanzas. Ctrl+C para cerrarlos.
set -euo pipefail
command -v cloudflared >/dev/null || { echo "Instala cloudflared primero (ver deploy/HOSTING.md)"; exit 1; }
LOGS=$(mktemp -d)
cloudflared tunnel --no-autoupdate --url http://localhost:8765 >"$LOGS/ws.log" 2>&1 &  WS=$!
cloudflared tunnel --no-autoupdate --url http://localhost:8766 >"$LOGS/mon.log" 2>&1 & MON=$!
trap 'kill $WS $MON 2>/dev/null; rm -rf "$LOGS"; echo; echo "Túneles cerrados."' EXIT INT TERM

url() {
  for _ in $(seq 1 45); do
    u=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$1" | head -1 || true)
    [ -n "$u" ] && { echo "$u"; return; }
    sleep 1
  done
  echo "(no disponible: revisa $1)"
}
WS_URL=$(url "$LOGS/ws.log"); MON_URL=$(url "$LOGS/mon.log")
Y='\033[38;5;178m'; B='\033[1;38;5;226m'; R='\033[0m'
echo -e "${B}[ WORLD PACKET SIMULATOR :: TÚNELES ABIERTOS ]${R}"
echo -e "${Y}Broker   :${R} ${WS_URL/https:/wss:}"
echo -e "${Y}Monitor  :${R} $MON_URL"
echo
echo -e "${Y}En cada slave (PowerShell):${R}"
echo "  \$env:WPS_SERVER = \"${WS_URL/https:/wss:}\""
echo -e "${Y}En cada slave / master (Linux):${R}"
echo "  export WPS_SERVER=${WS_URL/https:/wss:}"
echo
echo "Deja esta terminal abierta. Ctrl+C cierra los túneles."
wait
