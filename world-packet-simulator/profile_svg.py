#!/usr/bin/env python3
"""Genera un SVG animado del mapa de WORLD PACKET SIMULATOR para tu README de GitHub.

* Sin JavaScript: solo SVG + animaciones SMIL, que GitHub sí muestra en un README
  (los <script> de un SVG nunca se ejecutan en GitHub, así que esto es lo "seguro").
* Estética de los SVG base del proyecto: fondo #151515, amarillo #d4b000, Courier.
* Con --from-github lee el tráfico de tus repos (WPS_GITHUB_TOKEN) y:
    - ilumina la zona de tu ciudad según las visitas de hoy,
    - lanza más paquetes GITHUB -> tu ciudad cuantas más visitas haya,
    - pinta las ondas de luz y los contadores.

Uso:
    python3 profile_svg.py --out wps-map.svg                       # demo, sin datos
    python3 profile_svg.py --out wps-map.svg --views 12 --uniques 5 --views14 140
    WPS_GITHUB_TOKEN=... python3 profile_svg.py --from-github --out wps-map.svg
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import renderer as R
from github_feed import GitHubFeed, heat_level
from network import Network

# Paleta (equivalentes hex de los colores ANSI de la terminal)
HEX = {R.TEXT: "#d4b000", R.LAND: "#5f5f00", R.COAST: "#878700", R.LINK: "#af8700", R.ACTIVE: "#d7af00",
       R.NODE: "#ffd700", R.PACKET: "#ffff00", R.ALERT: "#ffff00", R.TITLE: "#d4b000", R.DIM: "#875f00",
       R.DOWN: "#875f00"}
BG, Y = "#151515", "#d4b000"
FONT = "'Courier New', Courier, monospace"
CW, RH, FS = 7.2, 14, 12          # ancho de carácter, alto de fila y tamaño de letra (px)
MAP_W, MAP_H = 110, 30
OX, OY = 30, 70                   # esquina del mapa dentro del SVG


def cell_xy(col: int, row: int) -> tuple[float, float]:
    return OX + col * CW + CW / 2, OY + row * RH + RH / 2


def route_path(net: Network, route: list[str]) -> str:
    """Trayectoria en píxeles que sigue los caracteres de cada enlace (para animateMotion)."""
    pos = {n: R.proj(net.nodes[n].lat, net.nodes[n].lon, MAP_W, MAP_H) for n in route}
    parts, last = [], None
    for a, b in zip(route, route[1:]):
        pts = list(R.line_points(pos[a], pos[b], MAP_W))
        if pts[0] != pos[a]:
            pts.reverse()
        for col, row in pts:
            x, y = cell_xy(col, row)
            jump = last is None or abs(col - last) > MAP_W / 2
            parts.append(f"{'M' if jump else 'L'}{x:.1f},{y:.1f}")
            last = col
    return " ".join(parts)


def map_text(cv: R.Canvas) -> list[str]:
    """Convierte el canvas en <text> por fila, agrupando caracteres del mismo color."""
    out = []
    for row in range(cv.h):
        runs, cur, start, buf = [], None, 0, []
        for col in range(cv.w + 1):
            st = cv.st[row][col] if col < cv.w else None
            ch = cv.ch[row][col] if col < cv.w else ""
            if st != cur or col == cv.w:
                if buf and "".join(buf).strip():
                    runs.append((start, cur, "".join(buf)))
                cur, start, buf = st, col, []
            buf.append(ch)
        for start, st, text in runs:
            x = OX + start * CW
            y = OY + row * RH + RH * 0.78
            weight = ' font-weight="bold"' if st in (R.NODE, R.PACKET, R.ALERT) else ""
            out.append(f'<text xml:space="preserve" x="{x:.1f}" y="{y:.1f}" fill="{HEX.get(st, Y)}"{weight} '
                       f'textLength="{len(text) * CW:.1f}" lengthAdjust="spacingAndGlyphs">{escape(text)}</text>')
    return out


def build_svg(views: int, uniques: int, views14: int, repos: int, home: str, demo: bool) -> str:
    net = Network.default()
    home = net.resolve(home) or "VALENCIA"
    hnode = net.nodes[home]
    level = heat_level(views)
    cv = R.Canvas(MAP_W, MAP_H)
    R.draw_map(cv, 0, 0, MAP_W, MAP_H, net, [], fx={"glow": {"lat": hnode.lat, "lon": hnode.lon, "level": max(level, 1)}})

    width = OX * 2 + MAP_W * CW
    height = OY + MAP_H * RH + 150
    hx, hy = cell_xy(*R.proj(hnode.lat, hnode.lon, MAP_W, MAP_H))
    route = net.dijkstra("GITHUB", home)[0]
    path = route_path(net, route)

    n_packets = 1 if views <= 0 else min(8, 1 + views // 3)
    trip = 7.0
    n_rings = 1 + level

    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
         f'font-family="{FONT}" font-size="{FS}" role="img" aria-label="World Packet Simulator: tráfico de GitHub hacia {hnode.name}">',
         f'<title>World Packet Simulator — {views} visitas hoy hacia {hnode.name}</title>',
         f'<defs><clipPath id="mapclip"><rect x="{OX - 10}" y="{OY - 14}" width="{MAP_W * CW + 20:.0f}" '
         f'height="{MAP_H * RH + 26}"/></clipPath></defs>',
         f'<rect width="100%" height="100%" fill="{BG}"/>',
         f'<text x="{width / 2:.0f}" y="34" text-anchor="middle" fill="{Y}" font-size="17" font-weight="bold">'
         f'[ WORLD PACKET SIMULATOR :: GITHUB TRAFFIC ]</text>',
         f'<rect x="{OX - 10}" y="{OY - 14}" width="{MAP_W * CW + 20:.0f}" height="{MAP_H * RH + 26}" fill="none" '
         f'stroke="{Y}" stroke-width="1.5"/>',
         f'<text x="{OX + 4}" y="{OY - 18}" fill="{Y}" font-weight="bold">[ MAPA MUNDIAL ]</text>']
    s += map_text(cv)

    # Ondas de luz desde la ciudad base (recortadas al marco del mapa)
    s.append('<g clip-path="url(#mapclip)">')
    for i in range(n_rings):
        begin = f"{i * 4.0 / n_rings:.2f}s"
        s.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="0" fill="none" stroke="{Y}" stroke-width="2">'
                 f'<animate attributeName="r" from="0" to="{width * 0.55:.0f}" dur="4s" begin="{begin}" repeatCount="indefinite"/>'
                 f'<animate attributeName="opacity" values="0.9;0.35;0" dur="4s" begin="{begin}" repeatCount="indefinite"/>'
                 f'</circle>')
    s.append("</g>")
    # Paquetes GITHUB -> ciudad base
    s.append(f'<path id="route" d="{path}" fill="none" stroke="#ffd700" stroke-width="1" stroke-dasharray="2 5" opacity="0.6"/>')
    for i in range(n_packets):
        begin = f"{i * trip / n_packets:.2f}s"
        s.append(f'<text text-anchor="middle" dominant-baseline="central" fill="#ffff00" font-weight="bold" font-size="15">*'
                 f'<animateMotion dur="{trip}s" begin="{begin}" repeatCount="indefinite" path="{path}"/></text>')
    # Destello en la ciudad al "llegar"
    s.append(f'<text x="{hx:.1f}" y="{hy:.1f}" text-anchor="middle" dominant-baseline="central" fill="#ffff00" '
             f'font-weight="bold" font-size="16">@<animate attributeName="opacity" values="1;0.25;1" dur="1.2s" '
             f'repeatCount="indefinite"/></text>')

    # Panel inferior estilo "cajas" del diseño base
    by = OY + MAP_H * RH + 30
    bw = (MAP_W * CW + 20 - 2 * 16) / 3
    boxes = [(f"[ {hnode.name.upper()} HOY ]", f"{views} visitas · {uniques} únicos"),
             ("[ ÚLTIMOS 14 DÍAS ]", f"{views14} visitas · {repos} repos"),
             ("[ RUTA DEL PULSO ]", " > ".join(net.nodes[n].code for n in route))]
    for i, (title, value) in enumerate(boxes):
        x = OX - 10 + i * (bw + 16)
        s.append(f'<rect x="{x:.0f}" y="{by}" width="{bw:.0f}" height="62" fill="none" stroke="{Y}" stroke-width="1.5"/>')
        s.append(f'<text x="{x + bw / 2:.0f}" y="{by + 24}" text-anchor="middle" fill="{Y}" font-weight="bold">{escape(title)}</text>')
        s.append(f'<text x="{x + bw / 2:.0f}" y="{by + 46}" text-anchor="middle" fill="{Y}">{escape(value)}</text>')
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    note = "modo demo (sin token)" if demo else f"actualizado {stamp}"
    s.append(f'<text x="{OX - 10}" y="{by + 92}" fill="#875f00">@ router  * paquete  ◯ onda = visitas  '
             f'· más claro = más visitas hoy · {escape(note)}</text>')
    s.append("</svg>")
    return "\n".join(s)


def main() -> None:
    p = argparse.ArgumentParser(description="SVG animado de WPS para el README de GitHub")
    p.add_argument("--out", default="wps-map.svg")
    p.add_argument("--from-github", action="store_true", help="lee el tráfico real con WPS_GITHUB_TOKEN")
    p.add_argument("--views", type=int, default=0)
    p.add_argument("--uniques", type=int, default=0)
    p.add_argument("--views14", type=int, default=0)
    p.add_argument("--home", default=os.environ.get("WPS_GITHUB_HOME", "VALENCIA"))
    a = p.parse_args()
    views, uniques, views14, repos, demo = a.views, a.uniques, a.views14, 0, not a.from_github
    if a.from_github:
        feed = GitHubFeed.from_env({}, a.home)
        if not feed.enabled:
            raise SystemExit("Falta WPS_GITHUB_TOKEN")
        feed.poll_sync()
        if not feed.repos or len(feed.repo_errors) >= len(feed.repos):
            # No machacar el SVG bueno con ceros: la Action falla y no hace commit
            raise SystemExit(f"Error GitHub: {feed.error or 'sin repos'}")
        if feed.error:
            print("Aviso GitHub:", feed.error)
        views, uniques, views14, repos = feed.today_views, feed.today_uniques, feed.views_14d, len(feed.repos)
    svg = build_svg(views, uniques, views14, repos, a.home, demo)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"{a.out}: {views} visitas hoy, {views14} en 14 días, {repos} repos")


if __name__ == "__main__":
    main()
