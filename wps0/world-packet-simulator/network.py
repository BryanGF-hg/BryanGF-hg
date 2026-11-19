#!/usr/bin/env python3
"""Topología mundial (grafo) y cálculo de rutas con Dijkstra."""
from __future__ import annotations

import heapq
import unicodedata
from dataclasses import dataclass, field, asdict

ONLINE, OFFLINE, CONGESTED = "online", "offline", "congested"
NODE_STATES = (ONLINE, OFFLINE, CONGESTED)

# Rejilla de referencia del mapa ASCII (el renderer reescala a la terminal)
MAP_W, MAP_H = 100, 26
LAT_TOP, LAT_BOTTOM = 80.0, -58.0


def project(lat: float, lon: float, w: int = MAP_W, h: int = MAP_H) -> tuple[int, int]:
    """Proyección equirectangular lat/lon -> (x, y) dentro de una rejilla w x h."""
    x = int((lon + 180.0) / 360.0 * w)
    y = int((LAT_TOP - lat) / (LAT_TOP - LAT_BOTTOM) * h)
    return max(0, min(w - 1, x)), max(0, min(h - 1, y))


@dataclass
class Node:
    id: str
    name: str
    code: str
    region: str
    lat: float
    lon: float
    capacity: int          # paquetes simultáneos que soporta sin congestionarse
    base_latency: float    # ms de procesamiento en el router
    status: str = ONLINE
    forced_down: bool = False      # caído por orden del operador (FAIL NODE)
    load: int = 0                  # paquetes en tránsito que tocan el nodo
    kind: str = "router"           # "router" o "external" (p. ej. GitHub: no genera tráfico aleatorio)

    @property
    def x(self) -> int:
        return project(self.lat, self.lon)[0]

    @property
    def y(self) -> int:
        return project(self.lat, self.lon)[1]

    @property
    def available(self) -> bool:
        return self.status != OFFLINE

    def to_dict(self) -> dict:
        d = asdict(self)
        d["x"], d["y"] = self.x, self.y
        return d


@dataclass
class Link:
    source: str
    destination: str
    latency: float         # ms
    bandwidth: float       # Mbps simulados
    congestion: float = 0.0  # 0..1
    up: bool = True
    load: int = 0          # paquetes atravesando el enlace ahora mismo

    @property
    def key(self) -> str:
        return link_key(self.source, self.destination)

    def other(self, node_id: str) -> str:
        return self.destination if node_id == self.source else self.source

    def effective_latency(self) -> float:
        """Latencia usada por Dijkstra: crece de forma cuadrática con la congestión."""
        return self.latency * (1.0 + 3.0 * self.congestion ** 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d


def link_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


# --------------------------------------------------------------------------
# Topología por defecto
# --------------------------------------------------------------------------
DEFAULT_NODES = [
    # id,            nombre,          código, región,         lat,     lon,   cap, ms
    ("NEW_YORK",     "New York",      "NYC", "AMERICA",      40.71,  -74.00, 10, 2.0),
    ("LOS_ANGELES",  "Los Angeles",   "LAX", "AMERICA",      34.05, -118.24,  8, 2.5),
    ("SAO_PAULO",    "São Paulo",     "SAO", "AMERICA",     -23.55,  -46.63,  7, 3.0),
    ("LONDON",       "London",        "LON", "EUROPE",       51.51,   -0.13, 12, 1.5),
    ("MADRID",       "Madrid",        "MAD", "EUROPE",       40.42,   -3.70,  8, 2.0),
    ("CAIRO",        "Cairo",         "CAI", "AFRICA",       30.04,   31.24,  6, 3.0),
    ("JOHANNESBURG", "Johannesburg",  "JNB", "AFRICA",      -26.20,   28.05,  6, 3.5),
    ("DUBAI",        "Dubai",         "DXB", "MIDDLE_EAST",  25.20,   55.27,  9, 2.0),
    ("MUMBAI",       "Mumbai",        "BOM", "ASIA",         19.08,   72.88,  8, 2.5),
    ("SINGAPORE",    "Singapore",     "SIN", "ASIA",          1.35,  103.82, 11, 1.5),
    ("TOKYO",        "Tokyo",         "TYO", "ASIA",         35.68,  139.69, 12, 1.5),
    ("SYDNEY",       "Sydney",        "SYD", "OCEANIA",     -33.87,  151.21,  8, 2.5),
    ("VALENCIA",     "Valencia",      "VLC", "EUROPE",       39.47,   -0.38,  8, 2.0),
]
# Nodos externos: participan en el enrutamiento pero no en el tráfico aleatorio
EXTERNAL_NODES = [
    ("GITHUB",       "GitHub",        "GIT", "AMERICA",      37.77, -122.42, 20, 1.0),
]

DEFAULT_LINKS = [
    # origen,        destino,        latencia ms, ancho de banda Mbps
    ("NEW_YORK",     "LONDON",        70, 400),
    ("NEW_YORK",     "MADRID",        95, 200),
    ("NEW_YORK",     "LOS_ANGELES",   65, 300),
    ("NEW_YORK",     "SAO_PAULO",    110, 150),
    ("LOS_ANGELES",  "TOKYO",        110, 300),
    ("LOS_ANGELES",  "SYDNEY",       140, 120),
    ("SAO_PAULO",    "MADRID",       120, 100),
    ("SAO_PAULO",    "JOHANNESBURG", 160,  60),
    ("LONDON",       "MADRID",        25, 400),
    ("LONDON",       "CAIRO",         60, 200),
    ("LONDON",       "DUBAI",         85, 250),
    ("MADRID",       "CAIRO",         65, 150),
    ("MADRID",       "JOHANNESBURG", 150,  80),
    ("CAIRO",        "DUBAI",         50, 200),
    ("CAIRO",        "JOHANNESBURG", 110,  80),
    ("JOHANNESBURG", "DUBAI",        130,  90),
    ("DUBAI",        "MUMBAI",        30, 250),
    ("DUBAI",        "SINGAPORE",     80, 200),
    ("MUMBAI",       "SINGAPORE",     60, 200),
    ("SINGAPORE",    "TOKYO",         65, 300),
    ("SINGAPORE",    "SYDNEY",        95, 150),
    ("TOKYO",        "SYDNEY",       110, 150),
    ("VALENCIA",     "MADRID",         8, 400),
    ("VALENCIA",     "CAIRO",         60, 150),
    ("GITHUB",       "LOS_ANGELES",   12, 1000),
    ("GITHUB",       "NEW_YORK",      70, 1000),
]


# Nombres en español y alias habituales
ALIASES = {
    "NUEVA_YORK": "NEW_YORK", "NY": "NEW_YORK", "LONDRES": "LONDON", "SAO_PABLO": "SAO_PAULO",
    "JOHANNESBURGO": "JOHANNESBURG", "JOBURG": "JOHANNESBURG", "DUBAI": "DUBAI", "SINGAPUR": "SINGAPORE",
    "TOKIO": "TOKYO", "SIDNEY": "SYDNEY", "EL_CAIRO": "CAIRO", "BOMBAY": "MUMBAI", "LA": "LOS_ANGELES",
    "LOS_ANGELES": "LOS_ANGELES", "GH": "GITHUB", "GIT_HUB": "GITHUB",
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.upper().replace(" ", "_").replace("-", "_").strip("_")


class Network:
    """Grafo no dirigido de routers y enlaces."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.links: dict[str, Link] = {}
        self.adj: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- build
    @classmethod
    def default(cls) -> "Network":
        net = cls()
        net.ensure_defaults()
        return net

    def ensure_defaults(self) -> list[str]:
        """Añade los nodos/enlaces por defecto que falten (p. ej. tras actualizar el proyecto
        con una base de datos antigua). Conserva el estado de los existentes."""
        added = []
        for kind, rows in (("router", DEFAULT_NODES), ("external", EXTERNAL_NODES)):
            for nid, name, code, region, lat, lon, cap, ms in rows:
                if nid not in self.nodes:
                    self.add_node(Node(nid, name, code, region, lat, lon, cap, ms, kind=kind))
                    added.append(nid)
        for a, b, lat, bw in DEFAULT_LINKS:
            if link_key(a, b) not in self.links:
                self.add_link(Link(a, b, float(lat), float(bw)))
                added.append(f"{a}-{b}")
        return added

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node
        self.adj.setdefault(node.id, [])

    def add_link(self, link: Link) -> None:
        if link.source not in self.nodes or link.destination not in self.nodes:
            raise ValueError(f"Enlace con nodos desconocidos: {link.source}-{link.destination}")
        self.links[link.key] = link
        self.adj[link.source].append(link.key)
        self.adj[link.destination].append(link.key)

    # --------------------------------------------------------------- lookup
    def resolve(self, text: str | None) -> str | None:
        """Acepta id, nombre, código IATA o prefijo (sin acentos ni mayúsculas)."""
        if not text:
            return None
        q = _norm(text)
        if q in ALIASES and ALIASES[q] in self.nodes:
            return ALIASES[q]
        for n in self.nodes.values():
            if q in (n.id, _norm(n.name), n.code):
                return n.id
        matches = [n.id for n in self.nodes.values() if n.id.startswith(q)]
        return matches[0] if len(matches) == 1 else None

    def link(self, a: str, b: str) -> Link | None:
        return self.links.get(link_key(a, b))

    def hop_available(self, a: str, b: str) -> bool:
        l = self.link(a, b)
        return bool(l and l.up and self.nodes[a].available and self.nodes[b].available)

    # -------------------------------------------------------------- routing
    def dijkstra(self, source: str, destination: str,
                 avoid_nodes: set[str] | None = None) -> tuple[list[str], float] | None:
        """Ruta de menor latencia efectiva. Ignora nodos offline y enlaces caídos.

        Devuelve (lista_de_nodos, latencia_total_ms) o None si no hay camino.
        """
        if source not in self.nodes or destination not in self.nodes:
            return None
        if not self.nodes[source].available or not self.nodes[destination].available:
            return None
        avoid = avoid_nodes or set()
        dist = {source: 0.0}
        prev: dict[str, str] = {}
        heap = [(0.0, source)]
        visited: set[str] = set()
        while heap:
            d, u = heapq.heappop(heap)
            if u in visited:
                continue
            visited.add(u)
            if u == destination:
                break
            for key in self.adj[u]:
                l = self.links[key]
                v = l.other(u)
                if not l.up or v in visited or v in avoid:
                    continue
                node_v = self.nodes[v]
                if not node_v.available:
                    continue
                # coste = enlace + procesamiento del router (+ penalización si congestionado)
                penalty = node_v.base_latency * (3.0 if node_v.status == CONGESTED else 1.0)
                nd = d + l.effective_latency() + penalty
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))
        if destination not in dist:
            return None
        path = [destination]
        while path[-1] != source:
            path.append(prev[path[-1]])
        path.reverse()
        return path, round(dist[destination], 2)

    # ---------------------------------------------------------------- state
    def online_count(self) -> int:
        return sum(1 for n in self.nodes.values() if n.available)

    def active_links(self) -> int:
        return sum(1 for l in self.links.values()
                   if l.up and self.nodes[l.source].available and self.nodes[l.destination].available)

    def to_dict(self) -> dict:
        return {"nodes": [n.to_dict() for n in self.nodes.values()],
                "links": [l.to_dict() for l in self.links.values()]}

    @classmethod
    def from_dict(cls, data: dict) -> "Network":
        net = cls()
        for n in data.get("nodes", []):
            net.add_node(Node(n["id"], n["name"], n["code"], n["region"], float(n["lat"]), float(n["lon"]),
                              int(n["capacity"]), float(n["base_latency"]), n.get("status", ONLINE),
                              bool(n.get("forced_down", False)), int(n.get("load", 0)),
                              n.get("kind", "router")))
        for l in data.get("links", []):
            net.add_link(Link(l["source"], l["destination"], float(l["latency"]), float(l["bandwidth"]),
                              float(l.get("congestion", 0.0)), bool(l.get("up", True)), int(l.get("load", 0))))
        return net


if __name__ == "__main__":
    net = Network.default()
    for a, b in [("MADRID", "TOKYO"), ("NEW_YORK", "SYDNEY"), ("SAO_PAULO", "MUMBAI")]:
        print(a, "->", b, net.dijkstra(a, b))
