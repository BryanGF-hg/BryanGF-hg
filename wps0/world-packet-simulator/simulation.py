#!/usr/bin/env python3
"""Lógica temporal de la simulación (autoridad del estado global).

El servidor es el único que instancia `Simulation`. Cada operación devuelve un
objeto `Effects` con lo que el broker debe persistir y difundir:

    events   -> líneas del registro (también en SQLite)
    updates  -> paquetes cuyo estado cambió (packet_update a todos los masters)
    jobs     -> paquetes que necesitan un trabajo para su salto actual
    cancel   -> paquetes cuyos trabajos en cola dejan de ser válidos
    network  -> la topología cambió (network_update)
"""
from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field

import config
from network import Network, ONLINE, OFFLINE, CONGESTED
from packets import (Packet, packet_id, CREATED, QUEUED, TRANSMITTING, ROUTING,
                     DELIVERED, LOST, FAILED, TERMINAL, InvalidTransition)

MAX_REROUTES = 8


@dataclass
class Effects:
    events: list = field(default_factory=list)      # (level, text, packet_id|None)
    updates: list = field(default_factory=list)     # Packet
    jobs: list = field(default_factory=list)        # Packet
    cancel: list = field(default_factory=list)      # packet ids
    network: bool = False

    def log(self, text: str, level: str = "info", pid: str | None = None) -> None:
        self.events.append((level, text, pid))

    def touch(self, p: Packet) -> None:
        if p not in self.updates:
            self.updates.append(p)

    def merge(self, other: "Effects") -> "Effects":
        self.events += other.events
        for p in other.updates:
            self.touch(p)
        self.jobs += [p for p in other.jobs if p not in self.jobs]
        self.cancel += other.cancel
        self.network = self.network or other.network
        return self


def hop_duration_seconds(total_ms: float) -> float:
    return max(config.MIN_HOP_SECONDS, min(config.MAX_HOP_SECONDS, total_ms * config.TIME_SCALE / 1000.0))


class Simulation:
    def __init__(self, network: Network | None = None, counters: dict | None = None, seq: int = 0):
        self.net = network or Network.default()
        self.packets: dict[str, Packet] = {}
        self.hops: dict[str, dict] = {}      # pid -> {src,dst,started,duration}
        self.seq = seq
        self.counters = {"created": 0, "transmitted": 0, "hops": 0, "delivered": 0, "lost": 0,
                         "failed": 0, "bytes": 0, "latency_sum": 0.0, "latency_max": 0.0,
                         "reroutes": 0}
        if counters:
            self.counters.update(counters)
        self.running = False
        self.paused = False
        self.traffic = config.DEFAULT_TRAFFIC
        self._rate = deque()                 # (ts, bytes) de saltos completados
        self._alerts: set[str] = set()       # enlaces con alerta de congestión activa
        self._left_origin: set[str] = set()  # paquetes ya contados como transmitidos
        for n in self.net.nodes.values():    # la carga se reconstruye en memoria
            n.load = 0
        for l in self.net.links.values():
            l.load = 0

    # ================================================================ util
    def _set(self, p: Packet, status: str, fx: Effects, reason: str = "") -> bool:
        try:
            p.set_status(status, reason)
        except InvalidTransition:
            return False
        fx.touch(p)
        return True

    def _finish(self, p: Packet, status: str, reason: str, fx: Effects) -> None:
        self._release(p)
        if not self._set(p, status, fx, reason):
            return
        fx.cancel.append(p.id)
        key = {DELIVERED: "delivered", LOST: "lost", FAILED: "failed"}[status]
        self.counters[key] += 1
        if status == DELIVERED:
            self.counters["latency_sum"] += p.latency
            self.counters["latency_max"] = max(self.counters["latency_max"], p.latency)
        self.packets.pop(p.id, None)
        self._left_origin.discard(p.id)

    def _release(self, p: Packet) -> None:
        h = self.hops.pop(p.id, None)
        if not h:
            return
        n = self.net.nodes.get(h["src"])
        if n:
            n.load = max(0, n.load - 1)
        l = self.net.link(h["src"], h["dst"])
        if l:
            l.load = max(0, l.load - 1)

    def route_text(self, route: list[str]) -> str:
        return " -> ".join(route)

    # ============================================================ paquetes
    def create_packet(self, source: str, destination: str, size: int, priority: int = 1,
                      tag: str = "") -> tuple[Packet | None, Effects]:
        fx = Effects()
        self.seq += 1
        p = Packet(packet_id(self.seq), self.seq, source, destination, int(size), int(priority), tag=tag)
        self.packets[p.id] = p
        self.counters["created"] += 1
        fx.touch(p)
        extra = f" [{tag.upper()}]" if tag else ""
        fx.log(f"{p.label} CREATED: {source} -> {destination} ({p.size} B){extra}", "info", p.id)
        found = self.net.dijkstra(source, destination)
        if not found:
            self._finish(p, FAILED, "sin ruta disponible", fx)
            fx.log(f"{p.label} FAILED: NO ROUTE {source} -> {destination}", "alert", p.id)
            return p, fx
        p.route, est = found
        self._set(p, QUEUED, fx)
        fx.log(f"{p.label} ROUTE: {self.route_text(p.route)} (~{est:.0f} ms)", "info", p.id)
        fx.jobs.append(p)
        return p, fx

    def hop_profile(self, p: Packet) -> dict:
        """Parámetros del salto actual: duración, latencia y probabilidades."""
        a, b = p.current_node, p.next_node
        link = self.net.link(a, b)
        src, dst = self.net.nodes[a], self.net.nodes[b]
        link_ms = link.latency * (1.0 + 2.0 * link.congestion ** 2)
        tx_ms = p.size * 8 / (link.bandwidth * 1000.0)              # bytes -> ms a X Mbps
        node_ms = src.base_latency * (1.0 + 3.0 * src.load / max(1, src.capacity))
        total = link_ms + tx_ms + node_ms
        loss = config.BASE_LOSS_PROB + link.congestion * config.CONGESTION_LOSS_FACTOR
        if CONGESTED in (src.status, dst.status):
            loss += config.CONGESTED_NODE_LOSS
        if p.priority >= 2:
            loss *= 0.5
        return {"src": a, "dst": b, "latency_ms": round(total, 2), "duration": round(hop_duration_seconds(total), 3),
                "loss_prob": round(min(0.6, loss), 4), "fail_prob": config.TEMP_FAILURE_PROB,
                "congestion": round(link.congestion, 3), "size": p.size, "priority": p.priority,
                "final": b == p.destination}

    def validate_hop(self, p: Packet | None, hop: int) -> tuple[str, Effects]:
        """Antes de asignar un trabajo: 'ok', 'stale' (descartar) o 'rerouted'."""
        fx = Effects()
        if p is None or p.terminal or p.hop != hop or p.status not in (QUEUED, ROUTING):
            return "stale", fx
        if not self.net.nodes[p.current_node].available:
            self._finish(p, LOST, f"router {p.current_node} caído", fx)
            fx.log(f"{p.label} LOST AT {p.current_node} (ROUTER DOWN)", "alert", p.id)
            return "stale", fx
        if p.next_node and self.net.hop_available(p.current_node, p.next_node):
            return "ok", fx
        self.reroute(p, f"salto {p.current_node}->{p.next_node} no disponible", fx)
        return "rerouted", fx

    def start_hop(self, p: Packet, profile: dict) -> Effects:
        fx = Effects()
        if not self._set(p, TRANSMITTING, fx):
            return fx
        a, b = profile["src"], profile["dst"]
        self.hops[p.id] = {"src": a, "dst": b, "started": time.time(), "duration": profile["duration"]}
        self.net.nodes[a].load += 1
        self.net.link(a, b).load += 1
        if p.hop == 0:
            if p.id not in self._left_origin:
                self._left_origin.add(p.id)
                self.counters["transmitted"] += 1
            fx.log(f"{p.label} TRANSMITTING FROM {a} -> {b}", "info", p.id)
        else:
            fx.log(f"{p.label} TRANSMITTING VIA {a} -> {b}", "info", p.id)
        return fx

    def complete_hop(self, p: Packet | None, result: dict) -> Effects:
        fx = Effects()
        if p is None or p.status != TRANSMITTING:
            return fx
        a, b = p.current_node, p.next_node
        self._release(p)
        if result.get("congested"):
            fx.log(f"{p.label} CONGESTION ON {a}-{b} (+{result.get('extra_ms', 0):.0f} ms)", "warn", p.id)
        if result.get("outcome") == "lost":
            self._finish(p, LOST, f"perdido en {a}-{b}", fx)
            fx.log(f"{p.label} LOST BETWEEN {a} AND {b}", "alert", p.id)
            return fx
        # El enlace o el destino cayeron mientras el paquete viajaba: retransmitir
        if not self.net.hop_available(a, b):
            fx.log(f"{p.label} HOP {a}->{b} INTERRUPTED, RETRANSMITTING", "warn", p.id)
            self._set(p, ROUTING, fx)
            self.reroute(p, "fallo durante la transmisión", fx)
            return fx
        p.latency = round(p.latency + float(result.get("latency_ms", 0)), 2)
        p.hop += 1
        self.counters["hops"] += 1
        self.counters["bytes"] += p.size
        self._rate.append((time.time(), p.size))
        if p.current_node == p.destination:
            self._finish(p, DELIVERED, "", fx)
            fx.log(f"{p.label} DELIVERED AT {b} ({p.latency:.0f} ms, {p.total_hops} hops) ACK", "ok", p.id)
            return fx
        self._set(p, ROUTING, fx)
        if self.net.hop_available(p.current_node, p.next_node):
            fx.jobs.append(p)
        else:
            self.reroute(p, f"siguiente salto {p.current_node}->{p.next_node} caído", fx)
        return fx

    def retry_hop(self, p: Packet | None, error: str, exhausted: bool) -> Effects:
        fx = Effects()
        if p is None or p.terminal:
            return fx
        self._release(p)
        where = p.current_node
        if exhausted:
            self._finish(p, FAILED, f"reintentos agotados: {error}", fx)
            fx.log(f"{p.label} FAILED AT {where} (MAX RETRIES: {error})", "alert", p.id)
        else:
            if p.status == TRANSMITTING:
                self._set(p, QUEUED, fx)
            fx.log(f"{p.label} RETRY AT {where}: {error}", "warn", p.id)
        return fx

    def reroute(self, p: Packet, reason: str, fx: Effects) -> None:
        here = p.current_node
        if not self.net.nodes[here].available:
            self._finish(p, LOST, f"router {here} caído", fx)
            fx.log(f"{p.label} LOST AT {here} (ROUTER DOWN)", "alert", p.id)
            return
        if p.reroutes >= MAX_REROUTES:
            self._finish(p, FAILED, "demasiadas rutas alternativas", fx)
            fx.log(f"{p.label} FAILED: TOO MANY REROUTES", "alert", p.id)
            return
        found = self.net.dijkstra(here, p.destination)
        if not found:
            self._finish(p, FAILED, "sin ruta alternativa", fx)
            fx.log(f"{p.label} FAILED: NO ALTERNATIVE ROUTE FROM {here}", "alert", p.id)
            return
        path, est = found
        p.route = p.route[:p.hop] + path
        p.reroutes += 1
        self.counters["reroutes"] += 1
        if p.status == TRANSMITTING:
            self._release(p)
        self._set(p, ROUTING, fx)
        fx.cancel.append(p.id)
        fx.jobs.append(p)
        fx.log(f"{p.label} REROUTE ({reason}): {self.route_text(path)}", "warn", p.id)

    def _reroute_affected(self, fx: Effects, node: str | None = None, link: tuple[str, str] | None = None) -> None:
        for p in list(self.packets.values()):
            if p.status not in (QUEUED, ROUTING):
                continue
            rest = p.route[p.hop:]
            if node and node in rest:
                if p.current_node == node:
                    self._finish(p, LOST, f"router {node} caído", fx)
                    fx.log(f"{p.label} LOST AT {node} (ROUTER DOWN)", "alert", p.id)
                else:
                    self.reroute(p, f"nodo {node} caído", fx)
            elif link:
                pairs = {frozenset(x) for x in zip(rest, rest[1:])}
                if frozenset(link) in pairs:
                    self.reroute(p, f"enlace {link[0]}-{link[1]} caído", fx)

    # ============================================================== control
    def fail_node(self, nid: str) -> Effects:
        fx = Effects()
        n = self.net.nodes[nid]
        if n.status == OFFLINE:
            return fx
        n.status, n.forced_down = OFFLINE, True
        fx.network = True
        fx.log(f"NODE {nid} FAILED", "alert")
        self._reroute_affected(fx, node=nid)
        return fx

    def restore_node(self, nid: str) -> Effects:
        fx = Effects()
        n = self.net.nodes[nid]
        if n.status != OFFLINE:
            return fx
        n.status, n.forced_down = ONLINE, False
        fx.network = True
        fx.log(f"NODE {nid} RESTORED", "ok")
        return fx

    def fail_link(self, a: str, b: str) -> Effects:
        fx = Effects()
        l = self.net.link(a, b)
        if not l or not l.up:
            return fx
        l.up = False
        fx.network = True
        fx.log(f"LINK FAILURE {l.source} <-> {l.destination}", "alert")
        self._reroute_affected(fx, link=(l.source, l.destination))
        return fx

    def restore_link(self, a: str, b: str) -> Effects:
        fx = Effects()
        l = self.net.link(a, b)
        if not l or l.up:
            return fx
        l.up = True
        fx.network = True
        fx.log(f"LINK RESTORED {l.source} <-> {l.destination}", "ok")
        return fx

    # ================================================================ tick
    def tick(self) -> Effects:
        """Dinámica de congestión: deriva aleatoria + carga real de cada enlace."""
        fx = Effects(network=True)
        if self.paused:
            return fx
        traffic_bias = {"LOW": 0.0, "MEDIUM": 0.05, "HIGH": 0.15}.get(self.traffic, 0.05)
        for l in self.net.links.values():
            if not l.up:
                l.congestion = max(0.0, l.congestion * 0.5)
                continue
            target = min(1.0, l.load * 17.5 / l.bandwidth + traffic_bias)
            l.congestion += (target - l.congestion) * 0.3 + random.gauss(0, 0.025)
            l.congestion = round(max(0.0, min(1.0, l.congestion)), 3)
            if l.congestion >= 0.75 and l.key not in self._alerts:
                self._alerts.add(l.key)
                fx.log(f"CONGESTION ALERT {l.source} <-> {l.destination} ({l.congestion:.0%})", "warn")
            elif l.congestion < 0.5 and l.key in self._alerts:
                self._alerts.discard(l.key)
        for n in self.net.nodes.values():
            if n.status == OFFLINE:
                continue
            if n.status == ONLINE and n.load >= n.capacity:
                n.status = CONGESTED
                fx.log(f"NODE {n.id} CONGESTED ({n.load}/{n.capacity})", "warn")
            elif n.status == CONGESTED and n.load < n.capacity * 0.7:
                n.status = ONLINE
                fx.log(f"NODE {n.id} RECOVERED FROM CONGESTION", "ok")
        return fx

    # =============================================================== stats
    def stats(self) -> dict:
        c = self.counters
        now = time.time()
        while self._rate and now - self._rate[0][0] > 10:
            self._rate.popleft()
        rate = sum(b for _, b in self._rate) / 10.0
        pending = len(self.packets)
        return {
            "created": c["created"], "transmitted": c["transmitted"], "hops": c["hops"],
            "delivered": c["delivered"], "lost": c["lost"], "failed": c["failed"], "pending": pending,
            "reroutes": c["reroutes"],
            "avg_latency": round(c["latency_sum"] / c["delivered"], 1) if c["delivered"] else 0.0,
            "max_latency": round(c["latency_max"], 1), "bytes": c["bytes"], "rate_bps": round(rate * 8, 1),
            "routers_online": self.net.online_count(), "routers_total": len(self.net.nodes),
            "links_active": self.net.active_links(), "links_total": len(self.net.links),
            "running": self.running, "paused": self.paused, "traffic": self.traffic,
        }

    def packet_view(self, p: Packet) -> dict:
        d = p.to_dict()
        h = self.hops.get(p.id)
        if h:
            d["hop_started"], d["hop_duration"] = h["started"], h["duration"]
        return d
