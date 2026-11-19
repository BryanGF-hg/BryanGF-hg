#!/usr/bin/env bash
# Instala el broker (y opcionalmente el túnel con nombre) como servicios de TU usuario.
# No necesita root, salvo el comando opcional 'loginctl enable-linger'.
#   ./deploy/install-services.sh           -> solo el broker
#   ./deploy/install-services.sh --tunnel  -> broker + túnel con nombre
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3)"
UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS"
[ -f "$DIR/.env" ] || { echo "Falta $DIR/.env (copia .env.example y rellena los tokens)"; exit 1; }

render() { sed -e "s|__DIR__|$DIR|g" -e "s|__PYTHON__|$PY|g" -e "s|__CLOUDFLARED__|${CF:-cloudflared}|g" "$1" > "$2"; }
render "$DIR/deploy/systemd/wps-server.service" "$UNITS/wps-server.service"
SERVICES=(wps-server)
if [ "${1:-}" = "--tunnel" ]; then
  CF="$(command -v cloudflared)" || { echo "cloudflared no está instalado"; exit 1; }
  grep -q TUDOMINIO "$DIR/deploy/cloudflared-config.yml" && { echo "Edita deploy/cloudflared-config.yml antes (TUDOMINIO, USUARIO, TUNNEL_ID)"; exit 1; }
  render "$DIR/deploy/systemd/wps-tunnel.service" "$UNITS/wps-tunnel.service"
  SERVICES+=(wps-tunnel)
fi
systemctl --user daemon-reload
systemctl --user enable --now "${SERVICES[@]}"
systemctl --user --no-pager status "${SERVICES[@]}" | head -20
echo
echo "Listo. Logs:   journalctl --user -u wps-server -f"
echo "Parar:         systemctl --user stop ${SERVICES[*]}"
echo "Para que sigan corriendo aunque cierres sesión:  sudo loginctl enable-linger $USER"
