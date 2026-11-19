#!/usr/bin/env python3
"""WORLD PACKET SIMULATOR — centro de control y visualización (terminal).

* Se conecta al broker, genera el tráfico (si es el master generador) y envía
  órdenes de control.
* Dibuja el mapa mundi ASCII con los paquetes moviéndose salto a salto,
  el panel de estadísticas y el registro de eventos.
* La pantalla se actualiza por diferencias (solo se reescriben las líneas que
  cambian), sin borrar la terminal: no hay parpadeo.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import sys
import time
from collections import deque

import websockets

import config
import renderer
from github_feed import heat_level
from network import Network, OFFLINE
from packets import TERMINAL, TRANSMITTING

try:  # POSIX
    import termios
    import tty
except ImportError:  # Windows
    termios = tty = None
try:
    import msvcrt  # type: ignore
except ImportError:
    msvcrt = None

ALT_KEYS = {
    "s": "START", "x": "STOP", "p": "__TOGGLE_PAUSE__", "1": "TRAFFIC LOW", "2": "TRAFFIC MEDIUM",
    "3": "TRAFFIC HIGH", "n": "FAIL NODE", "l": "FAIL LINK", "r": "__RESTORE_ALL__", "t": "TRACK",
    "h": "HELP", "q": "QUIT", "g": "PULSE",
}


class Master:
    def __init__(self, args):
        self.a = args
        self.role = "viewer" if args.viewer else "master"
        self.token = config.secret("WPS_MONITOR_TOKEN" if args.viewer else "WPS_MASTER_TOKEN")
        self.net = Network.default()
        self.packets: dict[str, dict] = {}
        self.finished: dict[str, dict] = {}
        self.markers: list[dict] = []
        self.recovered: dict[str, float] = {}
        self.events: deque = deque(maxlen=400)
        self.stats: dict = {}
        self.cluster: dict = {}
        self.github: dict = {}
        self.ripples: list[dict] = []
        self.control = {"running": False, "paused": False, "traffic": config.DEFAULT_TRAFFIC}
        self.generator = False
        self.ws = None
        self.connected = False
        self.conn_msg = "DESCONECTADO"
        self.clock = 0.0              # reloj de simulación (se congela en pausa)
        self.prompt = ""
        self.status_msg = "Escribe HELP o pulsa F1 / Alt+H para ver los comandos."
        self.history: list[str] = []
        self.hist_idx = 0
        self.help_until = 0.0
        self.help_sticky = False
        self.track_mode: str | int | None = "auto"
        self.tracked_id: str | None = None
        self.quit = asyncio.Event()
        self.rng = random.Random(args.seed)

    # ============================================================ red
    async def send(self, obj: dict) -> bool:
        if not self.ws or not self.connected:
            self.status_msg = "Sin conexión con el broker: la orden no se ha enviado."
            return False
        try:
            await self.ws.send(json.dumps(obj))
            return True
        except websockets.exceptions.ConnectionClosed:
            return False

    async def connection(self) -> None:
        url = self.a.server
        ssl_ctx = config.client_ssl_context(url)
        while not self.quit.is_set():
            try:
                self.conn_msg = f"CONECTANDO {url}"
                async with websockets.connect(url, max_size=4 * 1024 * 1024, ping_interval=20, ping_timeout=40,
                                              ssl=ssl_ctx) as ws:
                    await ws.send(json.dumps({"role": self.role, "token": self.token, "name": self.a.name}))
                    reg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if reg.get("type") != "registered":
                        raise RuntimeError("registro rechazado")
                    self.ws, self.connected = ws, True
                    self.generator = bool(reg.get("generator"))
                    self.conn_msg = ("CONECTADO (espectador)" if self.a.viewer else
                                     f"CONECTADO {'(generador)' if self.generator else '(observador)'}")
                    self.local_event("CONNECTED TO BROKER " + url, "ok")
                    async for raw in ws:
                        try:
                            self.on_message(json.loads(raw))
                        except (ValueError, KeyError, TypeError) as exc:
                            self.local_event(f"Mensaje inválido del broker: {exc}", "warn")
            except (OSError, asyncio.TimeoutError, websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.InvalidHandshake, RuntimeError, json.JSONDecodeError) as exc:
                reason = getattr(exc, "reason", "") or str(exc) or type(exc).__name__
                if self.connected:
                    self.local_event(f"CONNECTION LOST: {reason}", "alert")
                self.conn_msg = f"DESCONECTADO - reintento en {config.RECONNECT_DELAY}s"
            finally:
                self.connected, self.ws = False, None
            if not self.quit.is_set():
                try:
                    await asyncio.wait_for(self.quit.wait(), config.RECONNECT_DELAY)
                except asyncio.TimeoutError:
                    pass

    # ====================================================== mensajes
    def local_event(self, text: str, level: str = "info") -> None:
        self.events.append({"ts": time.time(), "level": level, "text": text})
        if self.a.headless:
            self.print_event(self.events[-1])

    def print_event(self, e: dict) -> None:
        style = {"alert": renderer.ALERT, "warn": renderer.PACKET, "ok": renderer.NODE}.get(e["level"], renderer.TEXT)
        t = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        print(f"{renderer.sgr(style)}{t} {e['text']}\x1b[0m", flush=True)

    def set_packet(self, d: dict, server_time: float | None = None) -> None:
        pid = d["id"]
        p = self.packets.get(pid) or {}
        was = (p.get("status"), p.get("src"), p.get("dst"))
        p.update(d)
        p["label"] = f"PACKET #{d['seq']:04d}"
        if d["status"] == TRANSMITTING:
            p["src"], p["dst"] = d["current_node"], d["next_node"]
            if was != (TRANSMITTING, p["src"], p["dst"]):
                elapsed = max(0.0, server_time - d.get("hop_started", server_time)) if server_time else 0.0
                p["start"] = self.clock - elapsed
            p["duration"] = float(d.get("hop_duration") or 1.0)
        if d["status"] in TERMINAL:
            self.packets.pop(pid, None)
            p["done_at"] = time.time()
            self.finished[pid] = p
        else:
            self.packets[pid] = p

    def on_message(self, m: dict) -> None:
        typ = m.get("type")
        if typ == "packet_update":
            self.set_packet(m)
        elif typ == "event":
            self.events.append(m)
            if self.a.headless:
                self.print_event(m)
        elif typ == "packet_lost":
            self.markers.append({"src": m["src"], "dst": m["dst"], "progress": float(m["progress"]),
                                 "until": time.time() + 4})
        elif typ == "network_update":
            self.update_network(m["network"])
        elif typ == "github_pulse":
            self.ripples.append({"lat": float(m["lat"]), "lon": float(m["lon"]), "t0": time.time()})
            self.ripples = self.ripples[-6:]
        elif typ == "stats":
            self.stats, self.cluster = m.get("stats", {}), m.get("cluster", {})
            self.github = m.get("github", self.github)
            if "running" in self.stats:
                self.control.update({k: self.stats[k] for k in ("running", "paused", "traffic")})
        elif typ == "control_state":
            self.control.update({k: m[k] for k in ("running", "paused", "traffic")})
        elif typ == "snapshot":
            self.update_network(m["network"])
            self.packets.clear()
            for d in m.get("packets", []):
                self.set_packet(d, m.get("server_time"))
            self.stats, self.cluster = m.get("stats", {}), m.get("cluster", {})
            self.events.extend(m.get("events", []))
        elif typ == "generator":
            self.generator = bool(m.get("enabled"))
            self.conn_msg = "CONECTADO (generador)"
            self.local_event("THIS MASTER IS NOW THE TRAFFIC GENERATOR", "ok")
        elif typ in ("error", "rejected"):
            if m.get("reason") != "backpressure":
                self.status_msg = f"Broker: {m.get('error') or m.get('reason')}"
                if self.a.headless:
                    print(self.status_msg, flush=True)
        elif typ == "created" and self.track_mode == "auto":
            self.tracked_id = m.get("id")

    def update_network(self, data: dict) -> None:
        new = Network.from_dict(data)
        for nid, n in new.nodes.items():
            old = self.net.nodes.get(nid)
            if old and old.status == OFFLINE and n.status != OFFLINE:
                self.recovered[nid] = time.time() + 6
        self.net = new

    # ====================================================== generador
    async def generator_loop(self) -> None:
        while not self.quit.is_set():
            c = self.control
            if not (self.connected and self.generator and c.get("running") and not c.get("paused")):
                await asyncio.sleep(0.25)
                continue
            rate = config.TRAFFIC_LEVELS.get(c.get("traffic"), 1.0)
            await asyncio.sleep(self.rng.expovariate(rate))
            if not (self.control.get("running") and not self.control.get("paused")):
                continue
            if self.stats.get("pending", 0) >= config.MAX_PENDING_PACKETS * 0.9:
                continue
            online = [n.id for n in self.net.nodes.values() if n.status != OFFLINE and n.kind == "router"]
            if len(online) < 2:
                continue
            src, dst = self.rng.sample(online, 2)
            size = self.rng.choice([64, 128, 512, 576, 1024, 1500, 1500, 1500, 4096, 9000, 32768, 65535])
            prio = self.rng.choices([0, 1, 2], weights=[2, 6, 2])[0]
            await self.send({"type": "create_packet", "source": src, "destination": dst, "size": size,
                             "priority": prio})

    # ======================================================== comandos
    def _split_link(self, rest: str) -> tuple[str, str] | None:
        for sep in ("<->", "->", "-", ","):
            if sep in rest:
                a, b = [x.strip() for x in rest.split(sep, 1)]
                if self.net.resolve(a) and self.net.resolve(b):
                    return a, b
        words = rest.split()
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if self.net.resolve(a) and self.net.resolve(b):
                return a, b
        return None

    async def command(self, line: str) -> None:
        await self._command(line)
        if self.a.headless and self.status_msg and not self.status_msg.startswith(">"):
            print(self.status_msg, flush=True)
            self.status_msg = ""

    async def _command(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        up = line.upper()
        words = up.split()
        cmd2 = " ".join(words[:2])
        if self.a.viewer and words[0] not in ("TRACK", "HELP", "?", "AYUDA", "QUIT", "EXIT", "Q", "STATUS"):
            self.status_msg = "Modo espectador: solo lectura (TRACK, STATUS, HELP y QUIT disponibles)"
            return
        rest = line.split(None, 2)[2] if len(words) > 2 else ""
        self.status_msg = f"> {up}"
        if up in ("START", "STOP", "PAUSE", "RESUME"):
            await self.send({"type": "control", "action": up.lower()})
        elif up == "__TOGGLE_PAUSE__":
            await self.send({"type": "control", "action": "resume" if self.control.get("paused") else "pause"})
        elif words[0] == "TRAFFIC":
            level = words[1] if len(words) > 1 else ""
            if level in config.TRAFFIC_LEVELS:
                await self.send({"type": "traffic", "level": level})
            else:
                self.status_msg = "Uso: TRAFFIC LOW | MEDIUM | HIGH"
        elif up == "STATUS":
            await self.send({"type": "stats"})
            s, c = self.stats, self.cluster
            self.local_event(f"STATUS created={s.get('created', 0)} delivered={s.get('delivered', 0)} "
                             f"lost={s.get('lost', 0)} failed={s.get('failed', 0)} pending={s.get('pending', 0)} "
                             f"avg={s.get('avg_latency', 0)}ms max={s.get('max_latency', 0)}ms", "ok")
            self.local_event(f"STATUS routers={s.get('routers_online')}/{s.get('routers_total')} "
                             f"links={s.get('links_active')}/{s.get('links_total')} workers={len(c.get('workers', []))} "
                             f"jobs queued={c.get('jobs_queued', 0)} running={c.get('jobs_running', 0)}", "ok")
            for w in c.get("workers", []):
                self.local_event(f"STATUS worker {w['name']} [{w['id']}] {w['region']} {w['busy']}/{w['max_tasks']} "
                                 f"visto hace {w['last_seen']}s", "info")
        elif cmd2 in ("FAIL NODE", "RESTORE NODE"):
            if rest and rest.upper() != "ALL" and not self.net.resolve(rest):
                self.status_msg = f"Nodo desconocido: {rest}"
                return
            await self.send({"type": "fail_node" if words[0] == "FAIL" else "restore_node", "node": rest or None})
        elif cmd2 in ("FAIL LINK", "RESTORE LINK"):
            typ = "fail_link" if words[0] == "FAIL" else "restore_link"
            if not rest:
                await self.send({"type": typ})
            elif rest.upper() == "ALL" and typ == "restore_link":
                await self.send({"type": typ, "source": "ALL"})
            else:
                pair = self._split_link(rest)
                if not pair:
                    self.status_msg = "Uso: FAIL LINK <ciudad> <ciudad>  (p.ej. FAIL LINK DUBAI SINGAPORE)"
                    return
                await self.send({"type": typ, "source": pair[0], "destination": pair[1]})
        elif up == "__RESTORE_ALL__":
            await self.send({"type": "restore_node", "node": "ALL"})
            await self.send({"type": "restore_link", "source": "ALL"})
        elif words[0] == "SEND":
            args = line.split()[1:]
            size = 1500
            if args and args[-1].isdigit():
                size = int(args.pop())
            pair = self._split_link(" ".join(args))
            if not pair:
                self.status_msg = "Uso: SEND <origen> <destino> [bytes]   (p.ej. SEND MADRID TOKYO 1500)"
                return
            self.track_mode = "auto"
            await self.send({"type": "create_packet", "source": pair[0], "destination": pair[1], "size": size,
                             "priority": 2})
        elif words[0] == "TRACK":
            arg = words[1] if len(words) > 1 else ""
            if arg == "OFF":
                self.track_mode, self.tracked_id = None, None
            elif arg.lstrip("#").isdigit():
                self.track_mode = int(arg.lstrip("#"))
            else:
                self.track_mode = "auto"
            self.status_msg = f"Seguimiento: {self.track_mode or 'desactivado'}"
        elif up in ("PULSE", "PULSO"):
            await self.send({"type": "github_test"})
            self.status_msg = "Pulso de prueba enviado: GITHUB -> " + str(self.github.get("home", "VALENCIA"))
        elif up in ("HELP", "?", "AYUDA"):
            self.help_sticky = not self.help_sticky
        elif up in ("QUIT", "EXIT", "Q"):
            self.quit.set()
        else:
            self.status_msg = f"Comando desconocido: {line}  (HELP para ver la lista)"

    # =========================================================== teclado
    def handle_keys(self, data: str) -> None:
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                nxt = data[i + 1] if i + 1 < len(data) else ""
                if nxt in ("[", "O"):                     # secuencias de escape (flechas, F1)
                    j = i + 2
                    while j < len(data) and not (data[j].isalpha() or data[j] == "~"):
                        j += 1
                    seq = data[i:j + 1]
                    i = j + 1
                    if seq in ("\x1bOP", "\x1b[11~", "\x1b[[A"):
                        self.help_sticky = not self.help_sticky
                    elif seq == "\x1b[A" and self.history:
                        self.hist_idx = max(0, self.hist_idx - 1)
                        self.prompt = self.history[self.hist_idx]
                    elif seq == "\x1b[B" and self.history:
                        self.hist_idx = min(len(self.history), self.hist_idx + 1)
                        self.prompt = self.history[self.hist_idx] if self.hist_idx < len(self.history) else ""
                    continue
                if nxt:                                   # Alt + tecla
                    i += 2
                    action = ALT_KEYS.get(nxt.lower())
                    self.help_until = time.time() + 2.5   # overlay mientras usas los atajos Alt
                    if action == "HELP":
                        self.help_sticky = not self.help_sticky
                    elif action:
                        asyncio.ensure_future(self.command(action))
                    continue
                i += 1                                    # ESC sola: cerrar ayuda / limpiar
                self.help_sticky, self.help_until, self.prompt = False, 0.0, ""
                continue
            i += 1
            if ch in ("\r", "\n"):
                line, self.prompt = self.prompt, ""
                if line.strip():
                    self.history.append(line)
                    self.hist_idx = len(self.history)
                asyncio.ensure_future(self.command(line))
            elif ch in ("\x7f", "\b"):
                self.prompt = self.prompt[:-1]
            elif ch == "\x03":
                self.quit.set()
            elif ch == "\x15":
                self.prompt = ""
            elif ch.isprintable() and len(self.prompt) < 120:
                self.prompt += ch

    async def windows_keys(self) -> None:
        while not self.quit.is_set():
            buf = ""
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    code = msvcrt.getwch()
                    buf += {"H": "\x1b[A", "P": "\x1b[B", ";": "\x1bOP"}.get(code, "")
                else:
                    buf += ch
            if buf:
                self.handle_keys(buf)
            await asyncio.sleep(0.03)

    # ============================================================ render
    def view_packets(self) -> tuple[list[dict], dict | None]:
        out = []
        for p in self.packets.values():
            if p.get("status") == TRANSMITTING and "start" in p:
                prog = (self.clock - p["start"]) / max(0.05, p["duration"])
                out.append({"src": p["src"], "dst": p["dst"], "progress": max(0.0, min(0.97, prog))})
        # paquete seguido
        tracked = None
        if self.track_mode == "auto":
            cur = self.packets.get(self.tracked_id) or self.finished.get(self.tracked_id)
            if (not cur or (cur.get("done_at") and time.time() - cur["done_at"] > 3)) and self.packets:
                self.tracked_id = max(self.packets.values(), key=lambda p: p["seq"])["id"]
        elif isinstance(self.track_mode, int):
            self.tracked_id = f"PKT-{self.track_mode:06d}"
        if self.track_mode is not None and self.tracked_id:
            t = self.packets.get(self.tracked_id) or self.finished.get(self.tracked_id)
            if t:
                tracked = {"label": t["label"], "route": t.get("route", []), "status": t["status"],
                           "source": t["source"], "destination": t["destination"]}
        return out, tracked

    def frame(self, w: int, h: int) -> renderer.Canvas:
        now = time.time()
        self.markers = [m for m in self.markers if m["until"] > now]
        self.recovered = {k: v for k, v in self.recovered.items() if v > now}
        self.finished = {k: v for k, v in self.finished.items() if now - v["done_at"] < 10}
        packets, tracked = self.view_packets()
        stats = dict(self.stats)
        stats.update(self.control)
        self.ripples = [r for r in self.ripples if now - r["t0"] < renderer.RIPPLE_LIFE]
        fx = {"ripples": [{"lat": r["lat"], "lon": r["lon"], "age": now - r["t0"]} for r in self.ripples]}
        home = self.net.nodes.get(self.github.get("home", ""))
        if home:
            fx["glow"] = {"lat": home.lat, "lon": home.lon, "level": heat_level(self.github.get("today_views", 0))}
        return renderer.render_frame(w, h, self.net, packets, self.markers, tracked, set(self.recovered), stats,
                                     self.cluster, list(self.events), self.prompt, self.status_msg, self.conn_msg,
                                     self.help_sticky or now < self.help_until, self.github, fx)

    async def render_loop(self) -> None:
        out = sys.stdout
        prev: list[str] = []
        size = None
        last = time.monotonic()
        period = 1.0 / max(2, self.a.fps)
        while not self.quit.is_set():
            now = time.monotonic()
            if not self.control.get("paused"):
                self.clock += now - last
            last = now
            w, h = shutil.get_terminal_size((140, 42))
            if (w, h) != size:                              # redimensionado: repintado completo
                size, prev = (w, h), []
                out.write(renderer.sgr(renderer.TEXT) + "\x1b[2J")
            lines = self.frame(w, h).ansi_lines()
            chunks = [f"\x1b[{y + 1};1H{line}" for y, line in enumerate(lines) if y >= len(prev) or prev[y] != line]
            if chunks:
                out.write("".join(chunks))
                out.flush()
            prev = lines
            await asyncio.sleep(period)

    # ========================================================= headless
    async def headless_input(self) -> None:
        """Lee órdenes de stdin en un hilo daemon (no bloquea la salida del programa)."""
        import threading
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def reader():
            for line in sys.stdin:
                loop.call_soon_threadsafe(q.put_nowait, line)
            loop.call_soon_threadsafe(q.put_nowait, None)
        threading.Thread(target=reader, daemon=True).start()
        while not self.quit.is_set():
            line = await q.get()
            if line is None:
                return
            for _ in range(100):                 # espera a estar conectado antes de enviar
                if self.connected:
                    break
                await asyncio.sleep(0.1)
            await self.command(line)

    async def headless_status(self) -> None:
        while not self.quit.is_set():
            await asyncio.sleep(5)
            s, c = self.stats, self.cluster
            print(f"{renderer.sgr(renderer.DIM)}[STATS] created={s.get('created', 0)} delivered={s.get('delivered', 0)}"
                  f" lost={s.get('lost', 0)} failed={s.get('failed', 0)} pending={s.get('pending', 0)}"
                  f" avg={s.get('avg_latency', 0)}ms workers={len(c.get('workers', []))}"
                  f" jobs q={c.get('jobs_queued', 0)} r={c.get('jobs_running', 0)}\x1b[0m", flush=True)

    # ============================================================== main
    async def main(self) -> None:
        tasks = [asyncio.create_task(self.connection()), asyncio.create_task(self.generator_loop())]
        if self.a.headless:
            tasks.append(asyncio.create_task(self.headless_status()))
            if self.a.stdin_commands:
                tasks.append(asyncio.create_task(self.headless_input()))
        else:
            tasks.append(asyncio.create_task(self.render_loop()))
            if termios and sys.stdin.isatty():
                fd = sys.stdin.fileno()
                asyncio.get_running_loop().add_reader(fd, lambda: self.handle_keys(
                    os.read(fd, 1024).decode("utf-8", "ignore")))
            elif msvcrt:
                tasks.append(asyncio.create_task(self.windows_keys()))

        async def startup():
            for _ in range(100):
                if self.connected:
                    break
                await asyncio.sleep(0.1)
            if self.a.traffic:
                await self.command(f"TRAFFIC {self.a.traffic}")
            if self.a.auto_start:
                await self.command("START")
            for cmd in self.a.command or []:
                await self.command(cmd)
        tasks.append(asyncio.create_task(startup()))
        try:
            if self.a.duration:
                try:
                    await asyncio.wait_for(self.quit.wait(), self.a.duration)
                except asyncio.TimeoutError:
                    pass
            else:
                await self.quit.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.ws:
                await self.ws.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Centro de control de WORLD PACKET SIMULATOR")
    p.add_argument("--server", default=config.DEFAULT_SERVER_URL)
    p.add_argument("--name", default="noc")
    p.add_argument("--fps", type=int, default=12)
    p.add_argument("--viewer", action="store_true",
                   help="espectador de solo lectura: ve el mapa con WPS_MONITOR_TOKEN, no puede controlar")
    p.add_argument("--traffic", choices=list(config.TRAFFIC_LEVELS), help="nivel de tráfico inicial")
    p.add_argument("--auto-start", action="store_true", help="empieza a generar tráfico al conectar")
    p.add_argument("--command", action="append", help="orden a ejecutar al conectar (repetible)")
    p.add_argument("--headless", action="store_true", help="sin interfaz: imprime eventos (scripts / pruebas)")
    p.add_argument("--stdin-commands", action="store_true", help="en headless, lee órdenes de la entrada estándar")
    p.add_argument("--duration", type=float, default=0, help="salir tras N segundos")
    p.add_argument("--seed", type=int, default=None)
    a = p.parse_args()
    a.server = config.normalize_url(a.server)
    m = Master(a)
    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    old = termios.tcgetattr(fd) if (termios and fd is not None and not a.headless) else None
    if not a.headless:
        if os.name == "nt":
            os.system("")  # activa las secuencias ANSI en la consola de Windows
        sys.stdout.write("\x1b[?1049h\x1b[?25l" + renderer.sgr(renderer.TEXT) + "\x1b[2J")
        sys.stdout.flush()
        if old is not None:
            tty.setcbreak(fd)
            attrs = termios.tcgetattr(fd)
            attrs[3] &= ~termios.ECHO
            attrs[0] &= ~termios.IXON          # Ctrl+S / Ctrl+Q no congelan la terminal
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
    try:
        asyncio.run(m.main())
    except KeyboardInterrupt:
        pass
    finally:
        if not a.headless:
            if old is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            print("WORLD PACKET SIMULATOR: centro de control cerrado.")


if __name__ == "__main__":
    main()
