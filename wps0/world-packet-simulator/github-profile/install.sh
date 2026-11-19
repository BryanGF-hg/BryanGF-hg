#!/usr/bin/env bash
# Copia el generador del mapa y la Action en tu repo de perfil (USUARIO/USUARIO).
#   ./github-profile/install.sh ~/ruta/a/tu-repo-de-perfil
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST="${1:?Uso: $0 <carpeta del repo USUARIO/USUARIO clonado>}"
[ -d "$DST/.git" ] || { echo "$DST no parece un repo git clonado"; exit 1; }
mkdir -p "$DST/wps" "$DST/.github/workflows"
cp "$SRC"/{profile_svg.py,renderer.py,network.py,config.py,github_feed.py} "$DST/wps/"
cp "$SRC/github-profile/.github/workflows/wps-map.yml" "$DST/.github/workflows/"
printf "__pycache__/\n" > "$DST/wps/.gitignore"
PYTHONDONTWRITEBYTECODE=1 python3 "$DST/wps/profile_svg.py" --out "$DST/wps-map.svg" --views 3 --views14 20
echo
echo "Hecho. Ahora:"
echo "  1. Pega en $DST/README.md el bloque de github-profile/README-snippet.md"
echo "  2. cd \"$DST\" && git add -A && git commit -m 'WPS map' && git push"
echo "  3. En GitHub: Settings > Secrets and variables > Actions > New secret WPS_GITHUB_TOKEN"
echo "  4. Actions > WPS map > Run workflow"
