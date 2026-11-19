#!/usr/bin/env python3
"""WORLD PACKET SIMULATOR — trabajador distribuido (simulador de nodos de red).

Recibe trabajos del broker (un trabajo = un salto de un paquete) y simula:
procesamiento en el router, latencia del enlace, congestión, pérdida de
paquetes, confirmación de recepción y fallos temporales.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import secrets
import socket
import time

import websockets

import config

Y = f"\x1b[38;5;{config.COLOR_TEXT}m"
B = f"\x1b[1;38;5;{config.COLOR_PACKET}m"
R = "\x1b[0m"


def out(text: str, strong: bool = False) -> None:
    print(f"{B if strong else Y}{time.strftime('%H:%M:%S')} {text}{R}", flush=True)


def valid_job(j: dict) -> bool:
    try:
        return (isinstance(j.get("id"), str) and isinstance(j.get("src"), str) and isinstance(j.get("dst"), str)
                and 0 < float(j["duration"]) <= 60 and float(j["latency_ms"]) >= 0
                and 0 <= float(j["loss_prob"]) <= 1 and 0 <= float(j["fail_prob"]) <= 1
                and 0 <= float(j.get("congestion", 0)) <= 1 and int(j.get("size", 0)) >= 0)
    except (KeyError, TypeError, ValueError):
        return False


class Worker:
    def __init__(self, args):
        self.a = args
        self.token = config.secret("WPS_SLAVE_TOKEN")
        self.paused = False
        self.active: set[str] = set()
        self.done = 0
        self.failed = 0

    async def sim_sleep(self, seconds: float) -> None:
        """Espera simulada que se congela mientras la simulación está en pausa."""
        remaining = seconds
        last = time.monotonic()
        while remaining > 0:
            await asyncio.sleep(min(0.05, remaining))
            now = time.monotonic()
            if not self.paused:
                remaining -= now - last
            last = now

    async def simulate(self, job: dict) -> dict:
        """Simula las operaciones de red de un salto y devuelve el mensaje de resultado."""
        ops = ["process"]
        load = len(self.active) / max(1, self.a.max_tasks)
        size = int(job.get("size", 1500))
        # 1) procesar el paquete / paso por el router (depende de la capacidad del trabajador)
        processing_ms = (0.3 + size / 8000.0) * (1.0 + load) / self.a.capacity
        latency_ms = float(job["latency_ms"])
        base_dur = float(job["duration"])
        # 2) congestión
        extra_ms, congested = 0.0, False
        if random.random() < float(job.get("congestion", 0)) * 0.6:
            congested = True
            extra_ms = latency_ms * random.uniform(0.2, 0.8)
            ops.append("congestion")
        total_ms = latency_ms + processing_ms + extra_ms
        duration = base_dur * (total_ms / max(1e-6, latency_ms)) * random.uniform(0.92, 1.08)
        duration = min(config.MAX_HOP_SECONDS * 1.6, duration)
        ops.append("latency")
        # 3) fallo temporal del router -> el broker reintentará el salto
        if random.random() < float(job["fail_prob"]):
            await self.sim_sleep(duration * random.uniform(0.1, 0.5))
            raise RuntimeError(f"fallo temporal del router {job['src']}")
        # 4) pérdida del paquete en el enlace
        if random.random() < float(job["loss_prob"]):
            progress = random.uniform(0.25, 0.9)
            await self.sim_sleep(duration * progress)
            ops.append("loss")
            return {"type": "result", "id": job["id"], "outcome": "lost", "latency_ms": round(total_ms * progress, 2),
                    "progress": round(progress, 3), "congested": congested, "extra_ms": round(extra_ms, 2), "ops": ops}
        await self.sim_sleep(duration)
        ops.append("ack" if job.get("final") else "forward")
        return {"type": "result", "id": job["id"], "outcome": "forwarded", "latency_ms": round(total_ms, 2),
                "congested": congested, "extra_ms": round(extra_ms, 2), "ops": ops}

    async def session(self, ws) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.a.max_tasks)

        async def heartbeat():
            while True:
                await asyncio.sleep(config.HEARTBEAT_INTERVAL)
                await ws.send(json.dumps({"type": "heartbeat", "ids": list(self.active), "load": len(self.active)}))

        async def process():
            while True:
                job = await queue.get()
                jid = job["id"]
                try:
                    result = await self.simulate(job)
                    await ws.send(json.dumps(result))
                    self.done += 1
                    tag = "LOST " if result["outcome"] == "lost" else ("ACK  " if job.get("final") else "FWD  ")
                    out(f"{tag}{job.get('packet_id', jid)[:10]} {job['src']} -> {job['dst']} "
                        f"{result['latency_ms']:.0f} ms{'  [congestión]' if result['congested'] else ''}",
                        strong=result["outcome"] == "lost")
                except websockets.exceptions.ConnectionClosed:
                    raise
                except Exception as exc:  # noqa: BLE001  (cualquier fallo se reporta al broker)
                    self.failed += 1
                    try:
                        await ws.send(json.dumps({"type": "failed", "id": jid, "error": str(exc)[:200]}))
                    except websockets.exceptions.ConnectionClosed:
                        raise
                    out(f"FAIL {job.get('packet_id', jid)[:10]} {exc}", strong=True)
                finally:
                    self.active.discard(jid)       # liberación del trabajo al finalizar
                    queue.task_done()

        tasks = [asyncio.create_task(heartbeat())] + [asyncio.create_task(process()) for _ in range(self.a.max_tasks)]
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                typ = msg.get("type") if isinstance(msg, dict) else None
                if typ == "job":
                    if not valid_job(msg):
                        await ws.send(json.dumps({"type": "failed", "id": str(msg.get("id", ""))[:80],
                                                  "error": "trabajo inválido"}))
                        continue
                    if queue.full():
                        await ws.send(json.dumps({"type": "failed", "id": msg["id"], "error": "cola interna llena"}))
                        continue
                    self.active.add(msg["id"])
                    queue.put_nowait(msg)
                elif typ == "control":
                    paused = bool(msg.get("paused"))
                    if paused != self.paused or paused:
                        out("SIMULACIÓN EN PAUSA" if paused else "SIMULACIÓN REANUDADA", strong=True)
                    self.paused = paused
                    if not paused:
                        out("Esperando trabajos... (en el master: START o SEND madrid tokio)")
                elif typ == "error":
                    out(f"Broker: {msg.get('error')}")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.active.clear()          # el broker reencola lo que no se completó

    async def run(self) -> None:
        url = self.a.server
        ssl_ctx = config.client_ssl_context(url)
        if not ssl_ctx and not config.is_local_url(url):
            out("AVISO: conexión sin TLS a un servidor remoto; usa wss:// en producción")
        while True:
            try:
                async with websockets.connect(url, max_size=config.MAX_MESSAGE_BYTES, ping_interval=20,
                                              ping_timeout=40, ssl=ssl_ctx) as ws:
                    await ws.send(json.dumps({"role": "slave", "token": self.token, "name": self.a.name,
                                              "id": self.a.id, "region": self.a.region,
                                              "capacity": self.a.capacity, "max_tasks": self.a.max_tasks}))
                    resp = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if resp.get("type") != "registered":
                        raise RuntimeError("registro rechazado")
                    out(f"CONECTADO {self.a.name} [{self.a.id}] región={self.a.region} "
                        f"capacidad=x{self.a.capacity} tareas={self.a.max_tasks}", strong=True)
                    await self.session(ws)
            except (OSError, asyncio.TimeoutError, websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.InvalidHandshake, RuntimeError, json.JSONDecodeError) as exc:
                reason = getattr(exc, "reason", "") or str(exc) or type(exc).__name__
                out(f"Desconectado ({reason}). Reconexión en {config.RECONNECT_DELAY} s "
                    f"| completados {self.done} fallidos {self.failed}")
                await asyncio.sleep(config.RECONNECT_DELAY)


def main() -> None:
    p = argparse.ArgumentParser(description="Trabajador de WORLD PACKET SIMULATOR")
    p.add_argument("--server", default=config.DEFAULT_SERVER_URL)
    p.add_argument("--name", default=socket.gethostname()[:30])
    p.add_argument("--id", default=secrets.token_hex(3), help="identificador único del trabajador")
    p.add_argument("--region", default="ANY",
                   help="región que representa: AMERICA, EUROPE, AFRICA, MIDDLE_EAST, ASIA, OCEANIA o ANY")
    p.add_argument("--capacity", type=float, default=1.0, help="velocidad de procesamiento (0.1-10)")
    p.add_argument("--max-tasks", type=int, default=4, help=f"tareas simultáneas (1-{config.MAX_SLAVE_TASKS})")
    p.add_argument("--seed", type=int, default=None)
    a = p.parse_args()
    a.server = config.normalize_url(a.server)
    a.region = a.region.upper()
    a.capacity = max(0.1, min(10.0, a.capacity))
    a.max_tasks = max(1, min(config.MAX_SLAVE_TASKS, a.max_tasks))
    if a.seed is not None:
        random.seed(a.seed)
    try:
        asyncio.run(Worker(a).run())
    except KeyboardInterrupt:
        print(R)


if __name__ == "__main__":
    main()
