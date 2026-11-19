#!/usr/bin/env python3
"""Mapa mundi ASCII, panel de estadísticas y composición de la pantalla.

El mapa no es un dibujo fijo: se rasteriza a partir de polígonos (lon, lat) de
los continentes, así que se adapta al tamaño de la terminal. Todo se dibuja en
un `Canvas` (caracteres + estilo por celda) que se convierte a líneas ANSI
(master) o a texto plano (monitor HTTP).
"""
from __future__ import annotations

import time
from functools import lru_cache

import config
from network import Network, LAT_TOP, LAT_BOTTOM, OFFLINE, CONGESTED

# ----------------------------------------------------------------- estilos
TEXT, LAND, COAST, LINK, ACTIVE, NODE, PACKET, ALERT, TITLE, DIM, DOWN = range(11)
_STYLE = {  # estilo -> (color fg, negrita, inverso)
    TEXT: (config.COLOR_TEXT, False, False),
    LAND: (config.COLOR_LAND, False, False),
    COAST: (config.COLOR_COAST, False, False),
    LINK: (config.COLOR_LINK, False, False),
    ACTIVE: (config.COLOR_ACTIVE, True, False),
    NODE: (config.COLOR_NODE, True, False),
    PACKET: (config.COLOR_PACKET, True, False),
    ALERT: (config.COLOR_PACKET, True, True),
    TITLE: (config.COLOR_TEXT, True, True),
    DIM: (config.COLOR_DIM, False, False),
    DOWN: (config.COLOR_DIM, False, False),
}


HEAT = (LAND, COAST, LINK, ACTIVE, NODE)   # tierra cada vez más clara (siempre amarillos)


def sgr(style: int) -> str:
    fg, bold, rev = _STYLE[style]
    s = f"\x1b[0;38;5;{fg};48;5;{config.COLOR_BG}"
    if bold:
        s += ";1"
    if rev:
        s += ";7"
    return s + "m"


# ------------------------------------------------- polígonos (lon, lat)
LAND_POLYS = [
    # Norteamérica
    [(-168, 66), (-160, 71), (-140, 70), (-120, 72), (-95, 72), (-80, 70), (-68, 63), (-60, 56), (-56, 50),
     (-66, 45), (-70, 42), (-75, 38), (-76, 35), (-81, 31), (-80, 26), (-82, 27), (-84, 30), (-90, 30),
     (-95, 29), (-97, 26), (-97, 21), (-94, 18), (-90, 21), (-87, 21), (-88, 16), (-84, 15), (-83, 10),
     (-79, 9), (-77, 8), (-80, 7), (-85, 10), (-87, 13), (-92, 15), (-97, 16), (-105, 20), (-110, 24),
     (-115, 30), (-118, 34), (-121, 36), (-124, 40), (-124, 46), (-127, 50), (-133, 55), (-140, 59),
     (-150, 60), (-157, 57), (-165, 55), (-160, 59), (-166, 62), (-168, 66)],
    # Groenlandia
    [(-73, 78), (-60, 82), (-30, 83), (-20, 80), (-20, 72), (-30, 68), (-40, 65), (-43, 60), (-50, 62),
     (-55, 68), (-60, 75), (-73, 78)],
    # Islandia, Cuba
    [(-24, 64), (-14, 64), (-14, 66.5), (-22, 66.5), (-24, 64)],
    [(-85, 22), (-74, 20), (-78, 23), (-85, 22)],
    # Sudamérica
    [(-80, 9), (-75, 11), (-72, 12), (-62, 10), (-52, 5), (-50, 0), (-44, -2), (-35, -5), (-35, -9),
     (-39, -14), (-41, -22), (-48, -26), (-53, -33), (-58, -35), (-57, -38), (-63, -41), (-65, -45),
     (-68, -50), (-69, -53), (-72, -54), (-75, -50), (-74, -42), (-72, -35), (-71, -28), (-70, -18),
     (-76, -14), (-80, -6), (-81, -3), (-80, 1), (-78, 5), (-80, 9)],
    # Eurasia
    [(-9, 37), (-9, 43), (-2, 43), (-4, 48), (2, 51), (5, 53), (8, 54), (8, 57), (5, 59), (5, 62),
     (14, 68), (20, 70), (28, 71), (40, 67), (44, 68), (60, 69), (70, 73), (80, 73), (100, 77), (115, 74),
     (130, 71), (140, 72), (160, 70), (180, 68), (180, 65), (178, 62), (165, 60), (162, 55), (157, 51),
     (156, 57), (150, 59), (140, 54), (141, 48), (135, 43), (130, 42), (129, 35), (126, 35), (126, 38),
     (122, 40), (118, 38), (122, 37), (120, 32), (122, 30), (119, 25), (113, 22), (108, 21), (106, 17),
     (109, 12), (105, 9), (101, 13), (100, 7), (103, 2), (101, 3), (98, 8), (98, 16), (94, 18), (92, 22),
     (88, 22), (86, 20), (80, 15), (80, 10), (77, 8), (73, 16), (73, 21), (68, 24), (61, 25), (57, 25),
     (59, 22), (52, 16), (44, 12), (43, 15), (39, 22), (35, 28), (34, 31), (35, 33), (36, 36), (30, 37),
     (27, 37), (26, 40), (23, 40), (23, 37), (20, 40), (19, 42), (13, 45), (12, 44), (16, 41), (16, 38),
     (12, 38), (8, 44), (3, 43), (0, 39), (-2, 37), (-5, 36), (-9, 37)],
    # Gran Bretaña, Irlanda
    [(-5, 50), (1, 51), (2, 53), (-1, 55), (-2, 58), (-5, 58), (-6, 56), (-3, 54), (-5, 52), (-5, 50)],
    [(-10, 52), (-6, 52), (-6, 55), (-8, 55), (-10, 52)],
    # África y Madagascar
    [(-17, 21), (-16, 28), (-10, 30), (-6, 36), (10, 37), (11, 33), (20, 31), (25, 32), (32, 31), (34, 28),
     (38, 18), (43, 12), (51, 12), (51, 10), (47, 4), (40, -3), (40, -10), (35, -20), (33, -26), (28, -33),
     (20, -35), (18, -33), (15, -27), (12, -17), (13, -10), (9, -1), (9, 4), (5, 6), (-2, 5), (-8, 4),
     (-13, 8), (-17, 14), (-17, 21)],
    [(44, -25), (50, -16), (49, -12), (47, -14), (43, -22), (44, -25)],
    # Japón, Filipinas, Sumatra, Borneo, Java, Nueva Guinea
    [(130, 31), (132, 34), (136, 34), (140, 35), (142, 40), (141, 43), (145, 44), (141, 45), (140, 41),
     (136, 37), (132, 35), (130, 33), (130, 31)],
    [(120, 18), (122, 18), (126, 7), (122, 7), (120, 13), (120, 18)],
    [(95, 5), (98, 4), (104, -2), (106, -6), (102, -4), (95, 5)],
    [(109, 1), (113, -3), (116, -4), (119, 1), (117, 7), (113, 3), (109, 1)],
    [(105, -6), (115, -8), (112, -8.5), (105, -7), (105, -6)],
    [(131, -1), (141, -3), (150, -10), (143, -9), (138, -8), (131, -1)],
    # Australia, Nueva Zelanda
    [(114, -22), (114, -34), (117, -35), (124, -33), (131, -31), (138, -35), (140, -38), (146, -39),
     (150, -37), (153, -31), (153, -25), (146, -19), (142, -11), (141, -17), (136, -12), (130, -12),
     (126, -14), (122, -17), (114, -22)],
    [(172, -34), (178, -38), (175, -41), (171, -46), (167, -46), (172, -41), (172, -34)],
]
WATER_POLYS = [
    [(-95, 60), (-88, 57), (-80, 52), (-78, 58), (-80, 63), (-90, 64), (-95, 60)],          # Bahía de Hudson
    [(28, 41), (29, 45), (33, 46), (38, 47), (41, 42), (36, 41), (28, 41)],                  # Mar Negro
    [(47, 45), (52, 46), (54, 41), (54, 37), (50, 37), (49, 40), (47, 45)],                  # Caspio
    [(10, 54), (14, 54), (20, 55), (22, 58), (30, 60), (22, 61), (20, 64), (25, 66), (21, 66),
     (17, 62), (16, 57), (12, 56), (10, 54)],                                                # Báltico
    [(48, 30), (51, 27), (56, 26), (54, 24), (50, 25), (48, 30)],                            # Golfo Pérsico
]


def _inside(px: float, py: float, poly) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


@lru_cache(maxsize=8)
def land_mask(w: int, h: int) -> tuple:
    """Rasteriza los continentes: 0 agua, 1 interior, 2 costa."""
    land = [[False] * w for _ in range(h)]
    for y in range(h):
        lat = LAT_TOP - (y + 0.5) / h * (LAT_TOP - LAT_BOTTOM)
        for x in range(w):
            lon = (x + 0.5) / w * 360.0 - 180.0
            if any(_inside(lon, lat, p) for p in LAND_POLYS) and not any(_inside(lon, lat, p) for p in WATER_POLYS):
                land[y][x] = True
    out = []
    for y in range(h):
        row = []
        for x in range(w):
            if not land[y][x]:
                row.append(0)
                continue
            edge = any(not (0 <= y + dy < h) or not land[y + dy][(x + dx) % w]
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            row.append(2 if edge else 1)
        out.append(tuple(row))
    return tuple(out)


def proj(lat: float, lon: float, w: int, h: int) -> tuple[int, int]:
    x = int((lon + 180.0) / 360.0 * w)
    y = int((LAT_TOP - lat) / (LAT_TOP - LAT_BOTTOM) * h)
    return max(0, min(w - 1, x)), max(0, min(h - 1, y))


RIPPLE_SPEED = 32.0     # celdas por segundo (en un mapa de 100 columnas)
RIPPLE_LIFE = 4.0       # segundos que dura una onda de luz


def _cell_dist(x: int, y: int, cx: int, cy: int, w: int) -> float:
    """Distancia en celdas corrigiendo la proporción 2:1 de los caracteres y el antimeridiano."""
    dx = abs(x - cx)
    dx = min(dx, w - dx)
    return (dx * dx + (2 * (y - cy)) ** 2) ** 0.5


@lru_cache(maxsize=512)
def line_points(a: tuple[int, int], b: tuple[int, int], w: int) -> tuple:
    """Bresenham con cruce del antimeridiano (rutas transpacíficas)."""
    (x0, y0), (x1, y1) = a, b
    if abs(x1 - x0) > w / 2:
        x1 += -w if x1 > x0 else w
    pts = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    x, y = x0, y0
    while True:
        pts.append((x % w, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy
    return tuple(pts)


def _seg_char(p_prev, p_next, w: int) -> str:
    dx = p_next[0] - p_prev[0]
    if abs(dx) > w / 2:
        dx = -1 if dx > 0 else 1
    dx = (dx > 0) - (dx < 0)
    dy = (p_next[1] > p_prev[1]) - (p_next[1] < p_prev[1])
    if dy == 0:
        return "-"
    if dx == 0:
        return "|"
    return "\\" if dx * dy > 0 else "/"


# ------------------------------------------------------------------ canvas
class Canvas:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.ch = [[" "] * w for _ in range(h)]
        self.st = [[TEXT] * w for _ in range(h)]

    def put(self, x: int, y: int, c: str, style: int = TEXT) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.ch[y][x] = c
            self.st[y][x] = style

    def text(self, x: int, y: int, s: str, style: int = TEXT, maxw: int | None = None) -> None:
        if maxw is not None:
            s = s[:max(0, maxw)]
        for i, c in enumerate(s):
            self.put(x + i, y, c, style)

    def box(self, x: int, y: int, w: int, h: int, title: str = "", style: int = DIM) -> None:
        if w < 2 or h < 2:
            return
        for i in range(x + 1, x + w - 1):
            self.put(i, y, "-", style)
            self.put(i, y + h - 1, "-", style)
        for j in range(y + 1, y + h - 1):
            self.put(x, j, "|", style)
            self.put(x + w - 1, j, "|", style)
        for cx, cy in ((x, y), (x + w - 1, y), (x, y + h - 1), (x + w - 1, y + h - 1)):
            self.put(cx, cy, "+", style)
        if title:
            self.text(x + 2, y, f"[ {title} ]", NODE, maxw=w - 4)

    def clear(self, x: int, y: int, w: int, h: int) -> None:
        for j in range(y, y + h):
            for i in range(x, x + w):
                self.put(i, j, " ", TEXT)

    def ansi_lines(self) -> list[str]:
        lines = []
        for y in range(self.h):
            out, cur = [], None
            for x in range(self.w):
                s = self.st[y][x]
                if s != cur:
                    out.append(sgr(s))
                    cur = s
                out.append(self.ch[y][x])
            lines.append("".join(out))
        return lines

    def plain_lines(self) -> list[str]:
        return ["".join(r).rstrip() for r in self.ch]


# ------------------------------------------------------------------- mapa
def draw_map(cv: Canvas, ox: int, oy: int, w: int, h: int, net: Network, packets: list[dict],
             markers: list[dict] | None = None, tracked: dict | None = None,
             recovered: set[str] | None = None, show_labels: bool = True, fx: dict | None = None) -> None:
    """Dibuja el mapa en el rectángulo (ox, oy, w, h) del canvas.

    packets: [{'src','dst','progress' (0..1|None), 'route', ...}] en tránsito
    markers: [{'src','dst','progress'}] paquetes perdidos recientes (X)
    tracked: paquete seguido (muestra S, D y su ruta resaltada)
    fx:      efectos {'ripples': [{'lat','lon','age'}], 'glow': {'lat','lon','level'}}
             ondas de luz (pulsos de GitHub) y brillo persistente alrededor de la ciudad base
    """
    mask = land_mask(w, h)
    ripples = [(*proj(r["lat"], r["lon"], w, h), r["age"] * RIPPLE_SPEED * w / 100.0) for r in (fx or {}).get("ripples", [])]
    glow = (fx or {}).get("glow")
    gx = gy = None
    if glow and glow.get("level", 0) > 0:
        gx, gy = proj(glow["lat"], glow["lon"], w, h)
        g_level = min(4, int(glow["level"]))
        g_radius = (2 + 2.5 * g_level) * w / 100.0
    for y in range(h):
        row = mask[y]
        for x in range(w):
            cell = row[x]
            lvl = 0 if cell == 1 else 1           # interior / costa
            wave = 0
            for rx, ry, rr in ripples:
                d = _cell_dist(x, y, rx, ry, w)
                if abs(d - rr) < 1.6:
                    wave = max(wave, 4)
                elif rr - 5 < d < rr:
                    wave = max(wave, 2)
            if gx is not None and cell:
                d = _cell_dist(x, y, gx, gy, w)
                if d < g_radius:
                    lvl = min(4, lvl + max(1, round(g_level * (1 - d / g_radius))))
            if cell:
                lvl = max(lvl, wave)
                cv.put(ox + x, oy + y, "." if cell == 1 else ":", HEAT[lvl])
            elif wave >= 4:
                cv.put(ox + x, oy + y, ".", DIM)

    pos = {nid: proj(n.lat, n.lon, w, h) for nid, n in net.nodes.items()}
    node_cells = set(pos.values())
    route_links = set()
    if tracked and tracked.get("route"):
        r = tracked["route"]
        route_links = {frozenset(x) for x in zip(r, r[1:])}
    active = {frozenset((p["src"], p["dst"])) for p in packets if p.get("progress") is not None}

    # enlaces
    for l in net.links.values():
        pts = line_points(pos[l.source], pos[l.destination], w)
        pair = frozenset((l.source, l.destination))
        dead = not l.up or not net.nodes[l.source].available or not net.nodes[l.destination].available
        for i in range(1, len(pts) - 1):
            x, y = pts[i]
            if (x, y) in node_cells:
                continue
            if dead:
                if i % 2:
                    cv.put(ox + x, oy + y, "~", DOWN)
                continue
            if pair in active:
                cv.put(ox + x, oy + y, "#", ACTIVE)
                continue
            c = _seg_char(pts[i - 1], pts[i + 1], w)
            if l.congestion >= 0.6:
                c = "="
            cv.put(ox + x, oy + y, c, NODE if pair in route_links else LINK)

    # etiquetas
    if show_labels:
        taken = set(node_cells)
        for nid in sorted(net.nodes, key=lambda k: pos[k]):
            x, y = pos[nid]
            label = net.nodes[nid].code
            n = len(label)
            spots = [(x + 2, y), (x - 1 - n, y), (x + 1, y - 1), (x + 1, y + 1), (x - n, y - 1), (x - n, y + 1)]
            if any((x + k, y) in node_cells for k in range(1, n + 3)):   # vecino a la derecha: etiqueta a la izquierda
                spots[0], spots[1] = spots[1], spots[0]
            for lx, ly in spots:
                cells = {(lx + i, ly) for i in range(n)}
                if lx >= 0 and lx + n <= w and 0 <= ly < h and not cells & taken:
                    break
            else:
                lx, ly = spots[0] if x + 2 + n <= w else spots[1]
            taken |= {(lx + i, ly) for i in range(n)} | {(lx - 1, ly), (lx + n, ly)}
            cv.text(ox + lx, oy + ly, label, TEXT, maxw=w - lx)

    # paquetes en tránsito
    for p in packets:
        prog = p.get("progress")
        if prog is None or p["src"] not in pos or p["dst"] not in pos:
            continue
        pts = line_points(pos[p["src"]], pos[p["dst"]], w)
        if pos[p["src"]] != pts[0]:
            pts = pts[::-1]
        x, y = pts[min(len(pts) - 1, int(prog * (len(pts) - 1)))]
        if (x, y) not in node_cells:
            cv.put(ox + x, oy + y, "*", PACKET)

    # paquetes perdidos
    for m in markers or []:
        if m.get("src") in pos and m.get("dst") in pos:
            pts = line_points(pos[m["src"]], pos[m["dst"]], w)
            if pos[m["src"]] != pts[0]:
                pts = pts[::-1]
            x, y = pts[min(len(pts) - 1, int(m.get("progress", 0.5) * (len(pts) - 1)))]
        elif m.get("src") in pos:
            x, y = pos[m["src"]]
        else:
            continue
        cv.put(ox + x, oy + y, "X", ALERT)

    # nodos: ! (problema) > D > S > + (recuperado) > @
    src = tracked.get("source") if tracked else None
    dst = tracked.get("destination") if tracked else None
    for nid, n in net.nodes.items():
        x, y = pos[nid]
        if n.status in (OFFLINE, CONGESTED):
            cv.put(ox + x, oy + y, "!", ALERT if n.status == OFFLINE else PACKET)
        elif nid == dst:
            cv.put(ox + x, oy + y, "D", PACKET)
        elif nid == src:
            cv.put(ox + x, oy + y, "S", PACKET)
        elif recovered and nid in recovered:
            cv.put(ox + x, oy + y, "+", PACKET)
        else:
            cv.put(ox + x, oy + y, "@", NODE)


# ------------------------------------------------------------------ panel
def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _fmt_rate(bps: float) -> str:
    for unit in ("bps", "kbps", "Mbps"):
        if bps < 1000 or unit == "Mbps":
            return f"{bps:.1f} {unit}"
        bps /= 1000
    return str(bps)


def stats_rows(s: dict, cluster: dict) -> list[tuple[str, str]]:
    return [
        ("Paquetes creados", s.get("created", 0)),
        ("Transmitidos", s.get("transmitted", 0)),
        ("Entregados", s.get("delivered", 0)),
        ("Perdidos", s.get("lost", 0)),
        ("Fallidos", s.get("failed", 0)),
        ("Pendientes", s.get("pending", 0)),
        ("Rutas alternativas", s.get("reroutes", 0)),
        ("Latencia media", f"{s.get('avg_latency', 0):.1f} ms"),
        ("Latencia máxima", f"{s.get('max_latency', 0):.1f} ms"),
        ("Bytes transmitidos", _fmt_bytes(s.get("bytes", 0))),
        ("Velocidad simulada", _fmt_rate(s.get("rate_bps", 0))),
        ("Routers activos", f"{s.get('routers_online', 0)}/{s.get('routers_total', 0)}"),
        ("Conexiones activas", f"{s.get('links_active', 0)}/{s.get('links_total', 0)}"),
        ("Trabajadores", len(cluster.get("workers", []))),
        ("Trabajos pendientes", cluster.get("jobs_queued", 0)),
        ("Trabajos en ejecución", cluster.get("jobs_running", 0)),
    ]


def github_lines(g: dict) -> list[tuple[str, int]]:
    if not g:
        return []
    if not g.get("enabled"):
        return [("desactivado (sin GITHUB_TOKEN)", DIM), ("PULSE = pulso de prueba", DIM)]
    ago = g.get("last_pulse_ago")
    out = [(f"{g.get('home', '?')} hoy: {g.get('today_views', 0)} visitas", NODE),
           (f"únicos hoy {g.get('today_uniques', 0)} · 14d {g.get('views_14d', 0)}", TEXT),
           (f"repos {g.get('repos', 0)} · cola {g.get('queued', 0)}"
            + (f" · pulso hace {_fmt_ago(ago)}" if ago is not None else ""), DIM)]
    if g.get("error"):
        out.append((f"! {g['error']}", PACKET))
    return out


def _fmt_ago(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 60 else (f"{s // 60}m" if s < 3600 else f"{s // 3600}h")


def draw_panel(cv: Canvas, x: int, y: int, w: int, h: int, s: dict, cluster: dict, conn: str,
               github: dict | None = None) -> None:
    cv.box(x, y, w, h, "ESTADÍSTICAS", DIM)
    inner = w - 4
    state = "PAUSED" if s.get("paused") else ("RUNNING" if s.get("running") else "STOPPED")
    cv.text(x + 2, y + 1, f"{state:<8} TRAFFIC {s.get('traffic', '-'):<6}", TITLE if state != "STOPPED" else TEXT, inner)
    cv.text(x + 2, y + 2, conn, ALERT if conn.startswith("DESCONECTADO") else DIM, inner)
    row = y + 4
    for label, value in stats_rows(s, cluster):
        if row >= y + h - 1:
            return
        val = str(value)
        dots = "." * max(1, inner - len(label) - len(val) - 2)
        cv.text(x + 2, row, f"{label} {dots} ", TEXT, inner)
        cv.text(x + 2 + inner - len(val), row, val, NODE)
        row += 1
    row += 1
    if row < y + h - 2:
        cv.text(x + 2, row, "[ TRABAJADORES ]", NODE, inner)
        row += 1
    for wk in cluster.get("workers", []):
        if row >= y + h - 1:
            break
        bar_w = 8
        fill = int(bar_w * wk["busy"] / max(1, wk["max_tasks"]))
        bar = "#" * fill + "." * (bar_w - fill)
        line = f"{wk['name'][:11]:<11} {wk['region'][:4]:<4} [{bar}] {wk['busy']}/{wk['max_tasks']}"
        cv.text(x + 2, row, line, TEXT, inner)
        row += 1
    gl = github_lines(github or {})
    if gl and row + len(gl) + 2 < y + h - 1:
        row += 1
        cv.text(x + 2, row, "[ GITHUB ]", NODE, inner)
        for i, (text, st) in enumerate(gl):
            cv.text(x + 2, row + 1 + i, text, st, inner)
        row += len(gl) + 1
    if row + len(SHORTCUTS) + 1 < y + h - 1:
        row = y + h - len(SHORTCUTS) - 2
        cv.text(x + 2, row, "[ ATAJOS ]", NODE, inner)
        for i, (k, v) in enumerate(SHORTCUTS):
            cv.text(x + 2, row + 1 + i, f"{k:<11}", PACKET, inner)
            cv.text(x + 13, row + 1 + i, v, DIM, inner - 11)


LEGEND = "@ router  S origen  D destino  * paquete  # conexión activa  X perdido  ! problema  + recuperado  ~ caído  = congestión"
SHORTCUTS = [("Alt+S/X", "start / stop"), ("Alt+P", "pausa / sigue"), ("Alt+1/2/3", "tráfico bajo/medio/alto"),
             ("Alt+N / L", "fallo nodo / enlace"), ("Alt+R", "restaurar todo"), ("Alt+T", "seguir último"),
             ("Alt+G", "pulso GitHub de prueba"), ("Alt+H / F1", "ayuda"), ("Alt+Q", "salir")]


def draw_log(cv: Canvas, x: int, y: int, w: int, h: int, events: list[dict]) -> None:
    cv.box(x, y, w, h, "REGISTRO DE EVENTOS", DIM)
    rows = h - 2
    for i, e in enumerate(events[-rows:]):
        style = {"alert": ALERT, "warn": PACKET, "ok": NODE}.get(e.get("level"), TEXT)
        t = time.strftime("%H:%M:%S", time.localtime(e.get("ts", time.time())))
        cv.text(x + 2, y + 1 + i, f"{t} ", DIM)
        cv.text(x + 11, y + 1 + i, e.get("text", ""), style, maxw=w - 13)


HELP_LINES = [
    "COMANDOS (escribe y pulsa ENTER)",
    "",
    "START / STOP ............ generar / detener tráfico",
    "PAUSE / RESUME .......... congelar / continuar",
    "TRAFFIC LOW|MEDIUM|HIGH . nivel de tráfico",
    "STATUS .................. resumen en el registro",
    "FAIL NODE [ciudad] ...... tirar un router",
    "RESTORE NODE [ciudad|ALL] recuperar router",
    "FAIL LINK [a b] ......... cortar un enlace",
    "RESTORE LINK [a b|ALL] .. recuperar enlace",
    "SEND a b [bytes] ........ paquete manual",
    "TRACK [n] ............... seguir un paquete",
    "PULSE ................... pulso de GitHub de prueba",
    "QUIT .................... salir",
    "",
    "ATAJOS ALT (el panel se muestra al usarlos)",
    "Alt+S start  Alt+X stop  Alt+P pausa/sigue",
    "Alt+1/2/3 tráfico bajo/medio/alto",
    "Alt+N fallo nodo  Alt+R restaurar todo",
    "Alt+L fallo enlace  Alt+T seguir último  Alt+G pulso",
    "Alt+H o F1 esta ayuda   Alt+Q salir",
]


def draw_help(cv: Canvas, x: int, y: int, w: int, h: int) -> None:
    bw = min(w - 4, max(len(s) for s in HELP_LINES) + 6)
    bh = min(h - 2, len(HELP_LINES) + 2)
    bx, by = x + (w - bw) // 2, y + (h - bh) // 2
    cv.clear(bx, by, bw, bh)
    cv.box(bx, by, bw, bh, "AYUDA", NODE)
    for i, line in enumerate(HELP_LINES[:bh - 2]):
        cv.text(bx + 3, by + 1 + i, line, NODE if line.isupper() or line.startswith("ATAJOS") else TEXT, bw - 5)


# -------------------------------------------------------------- pantalla
MIN_W, MIN_H = 80, 24
PANEL_W = 38


def layout(w: int, h: int) -> dict:
    """Calcula los rectángulos de mapa, panel, registro y prompt."""
    side = w >= 118
    map_w = (w - PANEL_W - 3) if side else (w - 2)
    map_w = max(40, min(map_w, 150))
    map_h = max(12, min(int(map_w * 0.27), h - 13))
    lay = {"map": (1, 2, map_w, map_h), "side": side}
    log_y = 2 + map_h + 2
    log_h = max(3, h - log_y - 3)
    lay["log"] = (0, log_y, map_w + 2, log_h)
    lay["legend"] = (1, 2 + map_h + 1)
    lay["prompt"] = h - 2
    lay["panel"] = (map_w + 3, 1, w - map_w - 3, h - 3) if side else None
    return lay


def render_frame(w: int, h: int, net: Network, packets: list[dict], markers: list[dict], tracked: dict | None,
                 recovered: set[str], stats: dict, cluster: dict, events: list[dict], prompt: str,
                 status_msg: str, conn: str, show_help: bool, github: dict | None = None,
                 fx: dict | None = None) -> Canvas:
    cv = Canvas(w, h)
    if w < MIN_W or h < MIN_H:
        cv.text(0, 0, f"Terminal demasiado pequeña ({w}x{h}). Mínimo {MIN_W}x{MIN_H}; recomendado 140x42.", ALERT)
        return cv
    lay = layout(w, h)
    title = "[ WORLD PACKET SIMULATOR :: GLOBAL NETWORK OPERATIONS CENTER ]"
    cv.text(max(0, (lay["map"][2] + 2 - len(title)) // 2), 0, title, NODE)
    mx, my, mw, mh = lay["map"]
    cv.box(mx - 1, my - 1, mw + 2, mh + 2, "MAPA MUNDIAL", DIM)
    draw_map(cv, mx, my, mw, mh, net, packets, markers, tracked, recovered, fx=fx)
    lx, ly = lay["legend"]
    cv.text(lx, ly, LEGEND, DIM, maxw=mw)
    if tracked:
        info = (f"SEGUIMIENTO {tracked.get('label', '')}: {' > '.join(tracked.get('route', []))} "
                f"[{tracked.get('status', '')}]")
        cv.text(mx + 1, my - 1, f"[ {info} ]", PACKET, maxw=mw - 18)
    draw_log(cv, *lay["log"], events)
    if lay["panel"]:
        draw_panel(cv, *lay["panel"], stats, cluster, conn, github)
    else:
        s = stats
        cv.text(0, 1, f"ENTREGADOS {s.get('delivered', 0)}  PERDIDOS {s.get('lost', 0)}  PENDIENTES "
                      f"{s.get('pending', 0)}  LAT {s.get('avg_latency', 0):.0f} ms  {conn}", TEXT, maxw=w)
    py = lay["prompt"]
    cv.text(0, py, status_msg, DIM, maxw=w)
    cv.text(0, py + 1, "> " + prompt, NODE, maxw=w - 1)
    cv.put(min(w - 1, 2 + len(prompt)), py + 1, "_", PACKET)
    if show_help:
        draw_help(cv, mx, my, mw, mh)
    return cv


def render_plain_map(net: Network, packets: list[dict], w: int = 100, h: int = 26) -> str:
    """Versión de texto plano (para el monitor HTTP)."""
    cv = Canvas(w, h)
    draw_map(cv, 0, 0, w, h, net, packets)
    return "\n".join(cv.plain_lines())


if __name__ == "__main__":
    net = Network.default()
    print(render_plain_map(net, [{"src": "MADRID", "dst": "LONDON", "progress": 0.5}]))
