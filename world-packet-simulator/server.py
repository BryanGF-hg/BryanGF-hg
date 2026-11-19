#!/usr/bin/env python3
"""WORLD PACKET SIMULATOR — broker WebSocket, planificador de trabajos y monitor HTTP.

* Autoridad única del estado global (topología, paquetes, estadísticas).
* Cola persistente de trabajos en SQLite (un trabajo = un salto de un paquete).
* Asignación por capacidad con lease, heartbeats y recuperación de trabajos abandonados.
* Monitor HTTP con panel web y endpoint JSON autenticado.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import random
import secrets
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field

import websockets

import config
import renderer
from github_feed import GitHubFeed
from network import Network, OFFLINE
from packets import Packet, TERMINAL, TRANSMITTING, QUEUED, ROUTING
from simulation import Simulation, Effects

log = logging.getLogger("wps.server")

# =========================================================================
# Validación de mensajes
# =========================================================================
OPT = object()  # marcador de campo opcional

NUM = (int, float)
SCHEMAS: dict[str, dict] = {
    # master
    "create_packet": {"source": str, "destination": str, "size": int, "priority": (int, OPT)},
    "control": {"action": ("start", "stop", "pause", "resume")},
    "traffic": {"level": tuple(config.TRAFFIC_LEVELS)},
    "fail_node": {"node": (str, OPT)},
    "restore_node": {"node": (str, OPT)},
    "fail_link": {"source": (str, OPT), "destination": (str, OPT)},
    "link_failure": {"source": str, "destination": str},
    "restore_link": {"source": (str, OPT), "destination": (str, OPT)},
    "stats": {},
    "ping": {},
    "github_test": {},
    # slave
    "heartbeat": {"ids": list, "load": (int, OPT)},
    "result": {"id": str, "outcome": ("forwarded", "lost"), "latency_ms": NUM, "progress": (NUM, OPT),
               "congested": (bool, OPT), "extra_ms": (NUM, OPT), "ops": (list, OPT)},
    "failed": {"id": str, "error": str},
}
ROLE_TYPES = {
    "master": {"create_packet", "control", "traffic", "fail_node", "restore_node", "fail_link",
               "link_failure", "restore_link", "stats", "ping", "github_test"},
    "slave": {"heartbeat", "result", "failed", "ping"},
    "viewer": {"stats", "ping"},          # espectador: solo lectura (token de monitor)
}


class BadMessage(ValueError):
    pass


def _check(value, spec, name):
    optional = isinstance(spec, tuple) and len(spec) == 2 and spec[1] is OPT
    if optional:
        spec = spec[0]
    if value is None:
        if optional:
            return None
        raise BadMessage(f"falta '{name}'")
    if isinstance(spec, tuple) and spec and all(isinstance(s, str) for s in spec):   # enumerado
        if value not in spec:
            raise BadMessage(f"'{name}' debe ser uno de {spec}")
        return value
    if spec is NUM or (isinstance(spec, tuple) and spec == NUM):
        if isinstance(value, bool) or not isinstance(value, NUM):
            raise BadMessage(f"'{name}' debe ser numérico")
        return float(value)
    if spec is int and (isinstance(value, bool) or not isinstance(value, int)):
        raise BadMessage(f"'{name}' debe ser entero")
    if not isinstance(value, spec):
        raise BadMessage(f"'{name}' tiene un tipo inválido")
    if isinstance(value, str) and len(value) > 200:
        raise BadMessage(f"'{name}' demasiado largo")
    if isinstance(value, list) and len(value) > 64:
        raise BadMessage(f"'{name}' demasiado largo")
    return value


def validate(raw, role: str) -> dict:
    if isinstance(raw, bytes):
        raise BadMessage("solo se aceptan mensajes de texto")
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        raise BadMessage("JSON inválido")
    if not isinstance(msg, dict):
        raise BadMessage("el mensaje debe ser un objeto JSON")
    typ = msg.get("type")
    if typ not in ROLE_TYPES.get(role, ()):
        raise BadMessage(f"tipo '{typ}' no permitido para {role}")
    clean = {"type": typ}
    for key, spec in SCHEMAS[typ].items():
        clean[key] = _check(msg.get(key), spec, key)
    return clean


# =========================================================================
# Persistencia
# =========================================================================
class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, packet_id TEXT NOT NULL, hop INTEGER NOT NULL,
            src TEXT NOT NULL, dst TEXT NOT NULL, region TEXT, priority INTEGER DEFAULT 1,
            status TEXT NOT NULL, worker TEXT, attempts INTEGER DEFAULT 0, lease REAL DEFAULT 0,
            payload TEXT, result TEXT, error TEXT, created REAL, updated REAL);
        CREATE INDEX IF NOT EXISTS idx_jobs_dispatch ON jobs(status, priority, updated);
        CREATE INDEX IF NOT EXISTS idx_jobs_packet ON jobs(packet_id, status);
        CREATE INDEX IF NOT EXISTS idx_jobs_worker ON jobs(worker, status);
        CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(status, lease);
        CREATE TABLE IF NOT EXISTS packets(
            id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL, created REAL, updated REAL);
        CREATE INDEX IF NOT EXISTS idx_packets_status ON packets(status, updated);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, packet_id TEXT, text TEXT);
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()

    # ---- kv
    def kv_get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))

    # ---- paquetes
    def save_packet(self, p: Packet) -> None:
        now = time.time()
        self.db.execute("""INSERT INTO packets(id,status,data,created,updated) VALUES(?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data,updated=excluded.updated""",
                        (p.id, p.status, json.dumps(p.to_dict()), p.created_at, now))

    def active_packets(self) -> list[Packet]:
        rows = self.db.execute("SELECT data FROM packets WHERE status NOT IN ('DELIVERED','LOST','FAILED')").fetchall()
        return [Packet.from_dict(json.loads(r["data"])) for r in rows]

    # ---- trabajos
    def insert_job(self, p: Packet, region: str) -> str:
        jid = f"{p.id}:{p.hop}:{secrets.token_hex(3)}"
        now = time.time()
        self.db.execute("""INSERT INTO jobs(id,packet_id,hop,src,dst,region,priority,status,created,updated)
                           VALUES(?,?,?,?,?,?,?,'queued',?,?)""",
                        (jid, p.id, p.hop, p.current_node, p.next_node, region, p.priority, now, now))
        return jid

    def cancel_jobs(self, packet_id: str) -> None:
        self.db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE packet_id=? AND status='queued'",
                        (time.time(), packet_id))

    def has_open_job(self, p: Packet) -> bool:
        return self.db.execute("SELECT 1 FROM jobs WHERE packet_id=? AND hop=? AND status IN ('queued','running')",
                               (p.id, p.hop)).fetchone() is not None

    def counts(self) -> dict:
        return {r["status"]: r["n"] for r in self.db.execute("SELECT status,count(*) n FROM jobs GROUP BY status")}

    def save_event(self, ts: float, level: str, pid: str | None, text: str) -> None:
        self.db.execute("INSERT INTO events(ts,level,packet_id,text) VALUES(?,?,?,?)", (ts, level, pid, text))

    def recent_events(self, n: int = 60) -> list[dict]:
        rows = self.db.execute("SELECT ts,level,packet_id,text FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune(self) -> None:
        self.db.execute("DELETE FROM events WHERE id <= (SELECT max(id) FROM events) - ?", (config.EVENT_RETENTION,))
        self.db.execute("DELETE FROM jobs WHERE status IN ('done','cancelled','failed') AND updated < ?",
                        (time.time() - 600,))
        self.db.execute("""DELETE FROM packets WHERE status IN ('DELIVERED','LOST','FAILED') AND id NOT IN
                           (SELECT id FROM packets ORDER BY updated DESC LIMIT ?)""", (config.PACKET_RETENTION,))
        self.db.commit()


# =========================================================================
# Clientes
# =========================================================================
@dataclass(eq=False)
class Client:
    ws: object
    role: str
    name: str
    wid: str = ""
    region: str = "ANY"
    capacity: float = 1.0
    max_tasks: int = 0
    assigned: set = field(default_factory=set)
    seen: float = field(default_factory=time.time)
    generator: bool = False
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(config.MAX_OUTBOUND_QUEUE))
    writer: asyncio.Task | None = None
    errors: int = 0

    @property
    def key(self) -> str:
        return self.wid or self.name


class Broker:
    def __init__(self, args):
        self.args = args
        self.tokens = {"master": config.secret("WPS_MASTER_TOKEN"),
                       "slave": config.secret("WPS_SLAVE_TOKEN")}
        self.monitor_token = config.secret("WPS_MONITOR_TOKEN")
        self.tokens["viewer"] = self.monitor_token   # el token de solo lectura sirve para el mapa en vivo
        if len({self.tokens["master"], self.tokens["slave"], self.monitor_token}) < 3:
            raise SystemExit("Los tokens de master, slave y monitor deben ser distintos.")
        self.store = Store(args.db)
        self.lock = asyncio.Lock()
        self.clients: dict[object, Client] = {}
        self.sim = self._restore()
        home = self.sim.net.resolve(config.GITHUB_HOME) or "MADRID"
        self.github = GitHubFeed.from_env(self.store.kv_get("github_seen", {}), home)
        self.pulses: deque = deque(maxlen=200)
        self._last_persist = 0.0
        self._last_prune = time.time()

    # ------------------------------------------------------------ arranque
    def _restore(self) -> Simulation:
        st = self.store
        net_data = st.kv_get("network")
        net = Network.from_dict(net_data) if net_data else Network.default()
        added = net.ensure_defaults()
        if added:
            log.info("Topología ampliada con: %s", ", ".join(added))
        sim = Simulation(net, st.kv_get("counters"), st.kv_get("seq", 0))
        ctl = st.kv_get("control", {})
        sim.running, sim.paused = bool(ctl.get("running", False)), bool(ctl.get("paused", False))
        sim.traffic = ctl.get("traffic", config.DEFAULT_TRAFFIC)
        # Trabajos que estaban en ejecución cuando cayó el servidor -> de vuelta a la cola
        st.db.execute("UPDATE jobs SET status='queued',worker=NULL,updated=? WHERE status='running'", (time.time(),))
        restored = 0
        for p in st.active_packets():
            if p.status == TRANSMITTING:
                p.status = QUEUED
            sim.packets[p.id] = p
            if p.next_node and not st.has_open_job(p):
                st.insert_job(p, net.nodes[p.current_node].region)
            restored += 1
        st.db.commit()
        log.info("Estado restaurado: %d paquetes activos, secuencia %d", restored, sim.seq)
        return sim

    def persist(self) -> None:
        st = self.store
        st.kv_set("network", self.sim.net.to_dict())
        st.kv_set("counters", self.sim.counters)
        st.kv_set("seq", self.sim.seq)
        st.kv_set("control", {"running": self.sim.running, "paused": self.sim.paused, "traffic": self.sim.traffic})
        st.db.commit()

    # ------------------------------------------------------------- salida
    def send(self, c: Client, obj: dict) -> None:
        try:
            c.queue.put_nowait(json.dumps(obj, ensure_ascii=False))
        except asyncio.QueueFull:
            log.warning("Cliente %s saturado: se cierra la conexión", c.name)
            asyncio.ensure_future(c.ws.close(1013, "Slow consumer"))

    def broadcast(self, obj: dict, role: str | None = "master") -> None:
        """role='master' llega a masters y espectadores (todos los que ven el mapa)."""
        roles = ("master", "viewer") if role == "master" else (role,)
        for c in list(self.clients.values()):
            if role is None or c.role in roles:
                self.send(c, obj)

    async def writer(self, c: Client) -> None:
        try:
            while True:
                msg = await c.queue.get()
                await c.ws.send(msg)
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("writer %s: %s", c.name, exc)

    # ---------------------------------------------------------- efectos
    def apply(self, fx: Effects) -> None:
        st = self.store
        now = time.time()
        for pid in fx.cancel:
            st.cancel_jobs(pid)
        for p in fx.jobs:
            if not p.terminal and p.next_node and p.id in self.sim.packets:
                st.insert_job(p, self.sim.net.nodes[p.current_node].region)
        for p in fx.updates:
            st.save_packet(p)
            self.broadcast({"type": "packet_update", **self.sim.packet_view(p)})
        for level, text, pid in fx.events:
            st.save_event(now, level, pid, text)
            self.broadcast({"type": "event", "ts": now, "level": level, "text": text, "packet_id": pid})
        if fx.network:
            self.broadcast({"type": "network_update", "network": self.sim.net.to_dict()})
        st.db.commit()

    def control_state(self) -> dict:
        return {"type": "control_state", "running": self.sim.running, "paused": self.sim.paused,
                "traffic": self.sim.traffic}

    # -------------------------------------------------------- estadísticas
    def cluster(self) -> dict:
        counts = self.store.counts()
        now = time.time()
        return {
            "workers": [{"name": c.name, "id": c.wid, "region": c.region, "capacity": c.capacity,
                         "max_tasks": c.max_tasks, "busy": len(c.assigned), "last_seen": round(now - c.seen, 1)}
                        for c in self.clients.values() if c.role == "slave"],
            "masters": sum(c.role == "master" for c in self.clients.values()),
            "viewers": sum(c.role == "viewer" for c in self.clients.values()),
            "jobs_queued": counts.get("queued", 0), "jobs_running": counts.get("running", 0),
            "jobs_done": counts.get("done", 0), "jobs_failed": counts.get("failed", 0),
        }

    def full_stats(self) -> dict:
        net = self.sim.net
        return {"stats": self.sim.stats(), "cluster": self.cluster(),
                "nodes": [{"id": n.id, "name": n.name, "region": n.region, "status": n.status,
                           "load": n.load, "capacity": n.capacity} for n in net.nodes.values()],
                "links": [{"source": l.source, "destination": l.destination, "up": l.up,
                           "latency": l.latency, "bandwidth": l.bandwidth, "congestion": l.congestion,
                           "load": l.load} for l in net.links.values()],
                "github": {**self.github.status(), "queued": len(self.pulses)},
                "server_time": time.time()}

    # --------------------------------------------------------- planificador
    def _requeue(self, row, reason: str, exhausted: bool | None = None) -> None:
        if exhausted is None:
            exhausted = row["attempts"] >= self.args.max_attempts
        status = "failed" if exhausted else "queued"
        self.store.db.execute("UPDATE jobs SET status=?,worker=NULL,error=?,updated=? WHERE id=?",
                              (status, reason[:500], time.time(), row["id"]))
        p = self.sim.packets.get(row["packet_id"])
        if p and p.hop == row["hop"]:
            self.apply(self.sim.retry_hop(p, reason, exhausted))
        else:
            self.store.db.commit()

    async def schedule(self) -> None:
        async with self.lock:
            db, now = self.store.db, time.time()
            for row in db.execute("SELECT * FROM jobs WHERE status='running' AND lease<?", (now,)).fetchall():
                for c in self.clients.values():
                    c.assigned.discard(row["id"])
                log.info("Lease expirado: %s (%s)", row["id"], row["worker"])
                self._requeue(row, "lease expirado")
            if self.sim.paused:
                return
            slaves = [c for c in self.clients.values() if c.role == "slave"]
            progress = True
            while progress:
                progress = False
                for c in sorted(slaves, key=lambda s: len(s.assigned) / s.max_tasks):
                    if len(c.assigned) >= c.max_tasks or c.ws not in self.clients:
                        continue
                    if self._assign_one(c):
                        progress = True

    def _assign_one(self, c: Client) -> bool:
        db = self.store.db
        while True:
            job = db.execute("""SELECT * FROM jobs WHERE status='queued' AND attempts<?
                                ORDER BY (region=?) DESC, priority DESC, updated LIMIT 1""",
                             (self.args.max_attempts, c.region)).fetchone()
            if not job:
                exhausted = db.execute("SELECT * FROM jobs WHERE status='queued' AND attempts>=? LIMIT 20",
                                       (self.args.max_attempts,)).fetchall()
                for row in exhausted:
                    self._requeue(row, "intentos agotados", exhausted=True)
                return False
            p = self.sim.packets.get(job["packet_id"])
            verdict, fx = self.sim.validate_hop(p, job["hop"])
            if verdict != "ok":
                db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status='queued'",
                           (time.time(), job["id"]))
                self.apply(fx)
                continue
            prof = self.sim.hop_profile(p)
            now = time.time()
            cur = db.execute("""UPDATE jobs SET status='running',worker=?,attempts=attempts+1,lease=?,
                                payload=?,updated=? WHERE id=? AND status='queued'""",
                             (c.key, now + self.args.lease, json.dumps(prof), now, job["id"]))
            if cur.rowcount != 1:          # otro proceso lo tomó: evita asignaciones duplicadas
                continue
            c.assigned.add(job["id"])
            self.apply(self.sim.start_hop(p, prof))
            self.send(c, {"type": "job", "id": job["id"], "packet_id": p.id, "hop": job["hop"],
                          "attempt": job["attempts"] + 1, **prof})
            return True

    # ------------------------------------------------------------ handler
    async def handler(self, ws) -> None:
        c: Client | None = None
        try:
            raw = await asyncio.wait_for(ws.recv(), config.HELLO_TIMEOUT)
            hello = json.loads(raw) if isinstance(raw, str) else {}
            role, token = hello.get("role"), hello.get("token", "")
            expected = self.tokens.get(role)
            if expected is None or not isinstance(token, str) or not hmac.compare_digest(token.encode(), expected.encode()):
                await ws.close(1008, "Unauthorized")
                return
            name = str(hello.get("name") or role)[:config.MAX_NAME_LEN]
            c = Client(ws, role, name)
            if role == "slave":
                c.wid = str(hello.get("id") or secrets.token_hex(4))[:40]
                c.region = str(hello.get("region") or "ANY").upper()[:20]
                try:
                    c.capacity = max(0.1, min(10.0, float(hello.get("capacity", 1.0))))
                    c.max_tasks = max(1, min(config.MAX_SLAVE_TASKS, int(hello.get("max_tasks", 4))))
                except (TypeError, ValueError):
                    await ws.close(1008, "Invalid hello")
                    return
                if any(o.role == "slave" and o.wid == c.wid for o in self.clients.values()):
                    await ws.close(1008, "Duplicate worker id")
                    return
            elif role == "master":
                c.generator = not any(o.role == "master" and o.generator for o in self.clients.values())
            self.clients[ws] = c
            c.writer = asyncio.create_task(self.writer(c))
            self.send(c, {"type": "registered", "role": role, "generator": c.generator, "server_time": time.time()})
            log.info("Conectado %s %s %s", role, name, f"región={c.region} cap={c.capacity} max={c.max_tasks}"
                     if role == "slave" else f"generador={c.generator}")
            async with self.lock:
                if role in ("master", "viewer"):
                    self.send(c, {"type": "snapshot", "network": self.sim.net.to_dict(),
                                  "packets": [self.sim.packet_view(p) for p in self.sim.packets.values()],
                                  "stats": self.sim.stats(), "cluster": self.cluster(),
                                  "events": self.store.recent_events(), "server_time": time.time()})
                    self.send(c, self.control_state())
                else:
                    self.send(c, {"type": "control", "paused": self.sim.paused})
                    self.apply(Effects(events=[("info", f"WORKER {name} ONLINE ({c.region}, x{c.max_tasks})", None)]))
            await self.schedule()
            async for raw in ws:
                c.seen = time.time()
                try:
                    msg = validate(raw, role)
                except BadMessage as exc:
                    c.errors += 1
                    self.send(c, {"type": "error", "error": str(exc)})
                    if c.errors > 50:
                        await ws.close(1008, "Too many invalid messages")
                        break
                    continue
                await self.dispatch(c, msg)
        except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            log.info("Desconectado %s: %s", c.name if c else "?", str(exc)[:100])
        finally:
            if c is not None:
                await self.disconnect(c)

    async def disconnect(self, c: Client) -> None:
        if self.clients.pop(c.ws, None) is None:
            return
        if c.writer:
            c.writer.cancel()
        async with self.lock:
            if c.role == "slave":
                rows = self.store.db.execute("SELECT * FROM jobs WHERE worker=? AND status='running'", (c.key,)).fetchall()
                for row in rows:          # reintentos no consumidos: la culpa no es del paquete
                    self.store.db.execute("UPDATE jobs SET attempts=max(0,attempts-1) WHERE id=?", (row["id"],))
                    self._requeue(row, f"worker {c.name} desconectado", exhausted=False)
                self.apply(Effects(events=[("warn", f"WORKER {c.name} OFFLINE ({len(rows)} jobs recovered)", None)]))
            elif c.generator:
                nxt = next((o for o in self.clients.values() if o.role == "master"), None)
                if nxt:
                    nxt.generator = True
                    self.send(nxt, {"type": "generator", "enabled": True})
        log.info("Baja %s %s", c.role, c.name)
        await self.schedule()

    # ---------------------------------------------------------- mensajes
    def _pick(self, nodes: list[str]) -> str | None:
        return random.choice(nodes) if nodes else None

    async def dispatch(self, c: Client, m: dict) -> None:
        typ = m["type"]
        sim, net = self.sim, self.sim.net
        if typ == "ping":
            self.send(c, {"type": "pong", "server_time": time.time()})
            return
        if typ == "stats":
            self.send(c, {"type": "stats", **self.full_stats()})
            return
        if typ == "github_test":
            self.pulses.append({"repo": "TEST", "test": True})
            return
        if typ == "heartbeat":
            active = {i for i in m["ids"] if isinstance(i, str)} & c.assigned
            if active:
                async with self.lock:
                    self.store.db.executemany(
                        "UPDATE jobs SET lease=? WHERE id=? AND status='running' AND worker=?",
                        [(time.time() + self.args.lease, jid, c.key) for jid in active])
                    self.store.db.commit()
            return
        if typ in ("result", "failed"):
            await self.on_result(c, m)
            return

        async with self.lock:
            if typ == "create_packet":
                src, dst = net.resolve(m["source"]), net.resolve(m["destination"])
                size, prio = m["size"], m["priority"] if m["priority"] is not None else 1
                if not src or not dst or src == dst:
                    self.send(c, {"type": "error", "error": "origen/destino inválidos"})
                elif not (config.PACKET_MIN_SIZE <= size <= config.PACKET_MAX_SIZE) or prio not in (0, 1, 2):
                    self.send(c, {"type": "error", "error": "tamaño o prioridad fuera de rango"})
                elif len(sim.packets) >= config.MAX_PENDING_PACKETS:
                    self.send(c, {"type": "rejected", "reason": "backpressure", "pending": len(sim.packets)})
                elif not net.nodes[src].available:
                    self.send(c, {"type": "rejected", "reason": f"origen {src} offline"})
                else:
                    p, fx = sim.create_packet(src, dst, size, prio)
                    self.apply(fx)
                    self.send(c, {"type": "created", "id": p.id, "seq": p.seq})
            elif typ == "control":
                act = m["action"]
                if act in ("start", "stop"):
                    sim.running = act == "start"
                    text = "TRAFFIC GENERATION STARTED" if sim.running else "TRAFFIC GENERATION STOPPED"
                else:
                    sim.paused = act == "pause"
                    text = "SIMULATION PAUSED" if sim.paused else "SIMULATION RESUMED"
                    self.broadcast({"type": "control", "paused": sim.paused}, role="slave")
                self.apply(Effects(events=[("ok", text, None)]))
                self.broadcast(self.control_state())
                self.persist()
            elif typ == "traffic":
                sim.traffic = m["level"]
                self.apply(Effects(events=[("ok", f"TRAFFIC LEVEL {sim.traffic}", None)]))
                self.broadcast(self.control_state())
                self.persist()
            elif typ in ("fail_node", "restore_node"):
                target = m["node"]
                if typ == "fail_node":
                    nid = net.resolve(target) if target else self._pick([n.id for n in net.nodes.values() if n.status != OFFLINE and n.kind == "router"])
                    fx = sim.fail_node(nid) if nid else Effects()
                elif target and target.upper() == "ALL":
                    fx = Effects()
                    for n in net.nodes.values():
                        fx.merge(sim.restore_node(n.id))
                else:
                    nid = net.resolve(target) if target else self._pick([n.id for n in net.nodes.values() if n.status == OFFLINE])
                    fx = sim.restore_node(nid) if nid else Effects()
                if not fx.events:
                    self.send(c, {"type": "error", "error": f"nada que hacer con el nodo '{target or 'aleatorio'}'"})
                self.apply(fx)
                self.persist()
            elif typ in ("fail_link", "link_failure", "restore_link"):
                a, b = m["source"], m["destination"]
                restoring = typ == "restore_link"
                if restoring and a and a.upper() == "ALL":
                    fx = Effects()
                    for l in net.links.values():
                        fx.merge(sim.restore_link(l.source, l.destination))
                else:
                    if a and b:
                        l = net.link(net.resolve(a) or "", net.resolve(b) or "")
                    else:
                        cands = [l for l in net.links.values() if l.up != restoring]
                        l = random.choice(cands) if cands else None
                    if l is None:
                        fx = Effects()
                    else:
                        fx = sim.restore_link(l.source, l.destination) if restoring else sim.fail_link(l.source, l.destination)
                if not fx.events:
                    self.send(c, {"type": "error", "error": "enlace inexistente o sin cambios"})
                self.apply(fx)
                self.persist()
        await self.schedule()

    async def on_result(self, c: Client, m: dict) -> None:
        jid = m["id"]
        if jid not in c.assigned:
            return                     # resultado tardío de un trabajo reasignado
        async with self.lock:
            c.assigned.discard(jid)
            db = self.store.db
            row = db.execute("SELECT * FROM jobs WHERE id=? AND status='running' AND worker=?", (jid, c.key)).fetchone()
            if not row:
                return
            p = self.sim.packets.get(row["packet_id"])
            if m["type"] == "result":
                db.execute("UPDATE jobs SET status='done',result=?,updated=? WHERE id=?",
                           (json.dumps({k: m[k] for k in ("outcome", "latency_ms", "congested")}), time.time(), jid))
                if p and p.hop == row["hop"]:
                    if m["outcome"] == "lost":
                        self.broadcast({"type": "packet_lost", "id": p.id, "src": row["src"], "dst": row["dst"],
                                        "progress": max(0.0, min(1.0, m["progress"] or 0.5))})
                    self.apply(self.sim.complete_hop(p, m))
                else:
                    db.commit()
            else:
                self._requeue(row, m["error"])
        await self.schedule()

    # ------------------------------------------------------------- ticker
    async def ticker(self) -> None:
        last_stats = 0.0
        while True:
            await asyncio.sleep(config.TICK_SECONDS)
            now = time.time()
            for c in list(self.clients.values()):
                if c.role == "slave" and now - c.seen > config.HEARTBEAT_TIMEOUT:
                    log.warning("Slave %s sin heartbeat: se desconecta", c.name)
                    await c.ws.close(1011, "Heartbeat timeout")
                    await self.disconnect(c)
            async with self.lock:
                self.apply(self.sim.tick())
                if now - last_stats >= config.STATS_BROADCAST_SECONDS:
                    last_stats = now
                    self.broadcast({"type": "stats", **self.full_stats()})
                if now - self._last_persist > 5:
                    self._last_persist = now
                    self.persist()
                if now - self._last_prune > 60:
                    self._last_prune = now
                    self.store.prune()
            await self.schedule()

    # ------------------------------------------------------------ github
    async def github_loop(self) -> None:
        """Consulta la API de tráfico y encola un pulso por cada visita nueva."""
        g = self.github
        if not g.enabled:
            log.info("Pulso de GitHub desactivado (define WPS_GITHUB_TOKEN para activarlo)")
            return
        log.info("Pulso de GitHub activo: ciudad base %s, consulta cada %d s", g.home, g.interval)
        first = True
        while True:
            try:
                pulses = await g.poll()
            except Exception as exc:  # noqa: BLE001  (nunca tumbar el broker por la API externa)
                g.error = str(exc)[:80]
                pulses = []
            async with self.lock:
                self.store.kv_set("github_seen", g.seen)
                self.store.db.commit()
                if first:
                    first = False
                    msg = (f"GITHUB FEED ONLINE: {len(g.repos)} repos, {g.views_14d} visits (14d), "
                           f"{g.today_views} today -> {g.home}")
                    self.apply(Effects(events=[("ok" if not g.error else "warn",
                                                msg + (f" | {g.error}" if g.error else ""), None)]))
            self.pulses.extend(pulses)
            if pulses:
                log.info("GitHub: %d visitas nuevas", len(pulses))
            await asyncio.sleep(g.interval)

    async def pulse_loop(self) -> None:
        """Reparte los pulsos en el tiempo (no todos de golpe)."""
        while True:
            await asyncio.sleep(config.GITHUB_PULSE_SPACING)
            if not self.pulses or self.sim.paused:
                continue
            p = self.pulses.popleft()
            async with self.lock:
                self.emit_pulse(p)
            await self.schedule()

    def emit_pulse(self, p: dict) -> None:
        sim, home = self.sim, self.github.home
        repo = p.get("repo", "?")
        label = "TEST PULSE" if p.get("test") else f"GITHUB VISIT ({repo.split('/')[-1]})"
        fx = Effects(events=[("ok", f"{label} -> {home}", None)])
        if (len(sim.packets) < config.MAX_PENDING_PACKETS and "GITHUB" in sim.net.nodes
                and sim.net.nodes["GITHUB"].available and sim.net.nodes[home].available):
            _, pfx = sim.create_packet("GITHUB", home, 1500, 2, tag="github")
            fx.merge(pfx)
        self.github.last_pulse = time.time()
        self.apply(fx)
        n = sim.net.nodes[home]
        self.broadcast({"type": "github_pulse", "repo": repo, "home": home, "lat": n.lat, "lon": n.lon,
                        "test": bool(p.get("test"))})

    # ----------------------------------------------------------- monitor
    async def monitor(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(request) > 8192:
                raise ValueError("cabecera demasiado grande")
            lines = request.decode("latin1").split("\r\n")
            method, path = lines[0].split(" ")[:2]
            headers = {k.lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:] if ":" in x)}
            auth = headers.get("authorization", "")
            authorized = hmac.compare_digest(auth.encode(), f"Bearer {self.monitor_token}".encode())
            if method != "GET":
                code, ct, body = "405 Method Not Allowed", "text/plain", b"Method not allowed"
            elif path == "/health":
                code, ct, body = "200 OK", "application/json", b'{"ok":true}'
            elif path.startswith("/api") and not authorized:
                code, ct, body = "401 Unauthorized", "text/plain", b"Unauthorized"
            elif path.startswith("/api/map"):
                packets = []
                now = time.time()
                for pid, h in self.sim.hops.items():
                    packets.append({"src": h["src"], "dst": h["dst"],
                                    "progress": min(0.97, (now - h["started"]) / max(0.1, h["duration"]))})
                body = renderer.render_plain_map(self.sim.net, packets, 100, 26).encode()
                code, ct = "200 OK", "text/plain; charset=utf-8"
            elif path.startswith("/api"):
                body = json.dumps(self.full_stats(), ensure_ascii=False).encode()
                code, ct = "200 OK", "application/json; charset=utf-8"
            elif path in ("/", "/index.html"):
                code, ct, body = "200 OK", "text/html; charset=utf-8", MONITOR_HTML.encode()
            else:
                code, ct, body = "404 Not Found", "text/plain", b"Not found"
            writer.write(f"HTTP/1.1 {code}\r\nContent-Type: {ct}\r\nContent-Length: {len(body)}\r\n"
                         f"Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
                         f"Content-Security-Policy: default-src 'self' 'unsafe-inline'\r\n"
                         f"Connection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- main
    async def run(self) -> None:
        ssl_ctx = config.server_ssl_context()
        if ssl_ctx is None and self.args.host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("Sin TLS escuchando en %s: define WPS_TLS_CERT/WPS_TLS_KEY para usar wss:// fuera de local",
                        self.args.host)
        async with websockets.serve(self.handler, self.args.host, self.args.port, max_size=config.MAX_MESSAGE_BYTES,
                                    ping_interval=20, ping_timeout=40, ssl=ssl_ctx):
            mon = await asyncio.start_server(self.monitor, self.args.host, self.args.monitor_port, ssl=ssl_ctx)
            async with mon:
                scheme = "wss" if ssl_ctx else "ws"
                log.info("Broker %s://%s:%d  |  Monitor %s://%s:%d", scheme, self.args.host, self.args.port,
                         "https" if ssl_ctx else "http", self.args.host, self.args.monitor_port)
                extra = [asyncio.create_task(self.github_loop()), asyncio.create_task(self.pulse_loop())]
                try:
                    await self.ticker()
                finally:
                    for t in extra:
                        t.cancel()
                    async with self.lock:
                        self.persist()


MONITOR_HTML = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>WPS | Monitor</title>
<style>
:root{--bg:#151515;--y:#d4b000;--d:#7a6600}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--y);font:13px 'Courier New',Courier,monospace}
main{max-width:1400px;margin:auto;padding:24px}h1{font-size:18px;text-align:center;margin:0 0 20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.card{border:1.5px solid var(--y);padding:12px}.card b{display:block;font-size:22px}.card span{opacity:.8}
.box{border:1.5px solid var(--y);padding:12px;margin-bottom:18px;overflow:auto}.box.async{border-style:dashed}
.box h2{font-size:14px;margin:0 0 10px}table{width:100%;border-collapse:collapse}
td,th{padding:5px 8px;text-align:left;border-bottom:1px solid var(--d)}pre{margin:0;line-height:1.05;font-size:12px}
.bar{display:inline-block;width:120px;height:10px;border:1px solid var(--y);vertical-align:middle}
.bar i{display:block;height:100%;background:var(--y)}.bad{background:var(--y);color:var(--bg);padding:0 4px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:900px){.cols{grid-template-columns:1fr}}
#err{text-align:center}button{background:none;color:var(--y);border:1px solid var(--y);font:inherit;cursor:pointer}
</style></head><body><main>
<h1>[ WORLD PACKET SIMULATOR - MONITOR HTTP ]</h1>
<div id="state" class="box"><h2>[ ESTADO GENERAL ]</h2><div id="sys">...</div></div>
<div id="cards" class="grid"></div>
<div class="box async"><h2>[ GITHUB · PULSO DE VISITAS ]</h2><div id="gh">...</div></div>
<div class="box"><h2>[ MAPA EN VIVO ]</h2><pre id="map"></pre></div>
<div class="box"><h2>[ TRABAJADORES ]</h2><table><thead><tr><th>Nombre</th><th>ID</th><th>Región</th>
<th>Capacidad</th><th>Ocupación</th><th>Última actividad</th></tr></thead><tbody id="workers"></tbody></table></div>
<div class="cols"><div class="box"><h2>[ NODOS ]</h2><table><thead><tr><th>Router</th><th>Región</th><th>Estado</th>
<th>Carga</th></tr></thead><tbody id="nodes"></tbody></table></div>
<div class="box"><h2>[ CONEXIONES ]</h2><table><thead><tr><th>Enlace</th><th>Estado</th><th>Lat.</th><th>BW</th>
<th>Congestión</th></tr></thead><tbody id="links"></tbody></table></div></div>
<p id="err"></p><p style="text-align:center"><button id="logout">cambiar token</button> · JSON: <code>GET /api/stats</code> (Authorization: Bearer)</p>
</main><script>
let token=sessionStorage.getItem('wps_monitor')||prompt('Token de monitor (WPS_MONITOR_TOKEN)');
if(token)sessionStorage.setItem('wps_monitor',token);
document.getElementById('logout').onclick=()=>{sessionStorage.removeItem('wps_monitor');location.reload()};
const el=(t,txt,cls)=>{const e=document.createElement(t);if(txt!==undefined)e.textContent=txt;if(cls)e.className=cls;return e};
function row(cells){const tr=el('tr');cells.forEach(c=>{const td=el('td');if(c instanceof Node)td.append(c);else td.textContent=c;tr.append(td)});return tr}
function bar(f){const b=el('span',undefined,'bar'),i=el('i');i.style.width=Math.round(Math.min(1,f)*100)+'%';b.append(i);return b}
async function refresh(){try{
 const h={headers:{Authorization:'Bearer '+token}};
 const r=await fetch('/api/stats',h);if(!r.ok)throw Error(r.status==401?'Token incorrecto':'Error '+r.status);
 const d=await r.json(),s=d.stats,c=d.cluster;
 document.getElementById('sys').textContent=`${s.paused?'PAUSADO':(s.running?'GENERANDO TRÁFICO':'DETENIDO')} · tráfico ${s.traffic} · masters ${c.masters} · workers ${c.workers.length} · trabajos en cola ${c.jobs_queued} · en ejecución ${c.jobs_running} · routers ${s.routers_online}/${s.routers_total} · enlaces ${s.links_active}/${s.links_total}`;
 const cards=[['creados',s.created],['transmitidos',s.transmitted],['entregados',s.delivered],['perdidos',s.lost],['fallidos',s.failed],['pendientes',s.pending],['lat. media',s.avg_latency+' ms'],['lat. máx.',s.max_latency+' ms'],['bytes',s.bytes],['velocidad',(s.rate_bps/1000).toFixed(1)+' kbps'],['rutas alt.',s.reroutes],['saltos',s.hops]];
 document.getElementById('cards').replaceChildren(...cards.map(([k,v])=>{const d=el('div',undefined,'card');d.append(el('b',String(v)),el('span',k));return d}));
 document.getElementById('workers').replaceChildren(...c.workers.map(w=>{const o=el('span');o.append(bar(w.busy/w.max_tasks),' '+w.busy+'/'+w.max_tasks);return row([w.name,w.id,w.region,'x'+w.capacity,o,w.last_seen+' s'])}));
 document.getElementById('nodes').replaceChildren(...d.nodes.map(n=>row([n.name,n.region,n.status==='online'?'online':el('span',n.status,'bad'),n.load+'/'+n.capacity])));
 document.getElementById('links').replaceChildren(...d.links.map(l=>row([l.source+' - '+l.destination,l.up?'up':el('span','DOWN','bad'),l.latency+' ms',l.bandwidth+' Mbps',bar(l.congestion)])));
 const g=d.github||{};document.getElementById('gh').textContent=!g.enabled?'desactivado (define WPS_GITHUB_TOKEN en el servidor)':
  `${g.home} · hoy ${g.today_views} visitas (${g.today_uniques} únicos) · 14 días ${g.views_14d} (${g.uniques_14d} únicos) · repos ${g.repos} · pulsos en cola ${g.queued}`+(g.last_pulse_ago!=null?` · último pulso hace ${g.last_pulse_ago}s`:'')+(g.error?` · ⚠ ${g.error}`:'');
 const m=await fetch('/api/map',h);document.getElementById('map').textContent=await m.text();
 document.getElementById('err').textContent='Actualizado '+new Date().toLocaleTimeString();
}catch(e){document.getElementById('err').textContent=e.message}}
refresh();setInterval(refresh,1000);
</script></body></html>"""


def main() -> None:
    p = argparse.ArgumentParser(description="Broker de WORLD PACKET SIMULATOR")
    p.add_argument("--host", default=config.DEFAULT_HOST)
    p.add_argument("--port", type=int, default=config.DEFAULT_WS_PORT)
    p.add_argument("--monitor-port", type=int, default=config.DEFAULT_MONITOR_PORT)
    p.add_argument("--db", default=config.DEFAULT_DB)
    p.add_argument("--lease", type=int, default=config.LEASE_SECONDS)
    p.add_argument("--max-attempts", type=int, default=config.MAX_ATTEMPTS)
    p.add_argument("--reset", action="store_true", help="borra la base de datos y empieza de cero")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if args.reset:
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(args.db + suffix)
            except FileNotFoundError:
                pass
    broker = Broker(args)
    try:
        asyncio.run(broker.run())
    except KeyboardInterrupt:
        log.info("Servidor detenido")


if __name__ == "__main__":
    main()
