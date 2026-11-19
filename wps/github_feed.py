#!/usr/bin/env python3
"""Pulso de visitas de GitHub (anónimo) para WORLD PACKET SIMULATOR.

Consulta la API de tráfico de GitHub de tus repositorios y convierte cada
visita NUEVA en un "pulso": un paquete GITHUB -> tu ciudad y una onda de luz
en el mapa.

Privacidad: la API de tráfico solo da RECUENTOS por día (visitas y visitantes
únicos de los últimos 14 días). Aquí no se manejan IPs, nombres de usuario
ni ubicaciones de nadie: es imposible saber quién visita, y no se intenta.

Requisitos del token (variable WPS_GITHUB_TOKEN):
  * Fine-grained token: permiso de repositorio "Administration" (read)
    sobre los repos que quieras vigilar.
  * Solo funciona en repos donde tienes permiso de escritura (los tuyos).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("wps.github")


class GitHubFeed:
    def __init__(self, token: str, repos: list[str], home: str, interval: int, api: str,
                 seen: dict | None = None):
        self.token = token
        self.fixed_repos = repos            # vacío = todos los repos de los que eres dueño
        self.home = home
        self.interval = max(15, interval)
        self.api = api.rstrip("/")
        self.seen: dict[str, dict[str, int]] = seen or {}   # repo -> {timestamp_día: visitas}
        self.repos: list[str] = list(repos)
        self.repo_errors: dict[str, str] = {}
        self.error = ""
        self.last_poll = 0.0
        self.last_pulse = 0.0
        self.today_views = self.today_uniques = 0
        self.views_14d = self.uniques_14d = 0
        self.queued = 0
        self._discovered_at = 0.0

    # ------------------------------------------------------------ fábrica
    @classmethod
    def from_env(cls, seen: dict | None, home: str) -> "GitHubFeed":
        token = os.environ.get("WPS_GITHUB_TOKEN", "").strip()
        repos = [r.strip() for r in os.environ.get("WPS_GITHUB_REPOS", "").split(",") if r.strip()]
        interval = int(os.environ.get("WPS_GITHUB_INTERVAL", "300"))
        api = os.environ.get("WPS_GITHUB_API", "https://api.github.com")
        return cls(token, repos, home, interval, api, seen)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    # -------------------------------------------------------------- HTTP
    def _get(self, path: str):
        req = Request(self.api + path, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "world-packet-simulator",
        })
        with urlopen(req, timeout=20) as resp:
            return json.load(resp)

    def _discover(self) -> list[str]:
        repos, page = [], 1
        while page <= 10:
            data = self._get(f"/user/repos?affiliation=owner&per_page=100&page={page}")
            if not data:
                break
            repos += [r["full_name"] for r in data if isinstance(r, dict) and "full_name" in r]
            page += 1
        return list(dict.fromkeys(repos))

    # -------------------------------------------------------------- poll
    def poll_sync(self) -> list[dict]:
        """Una pasada por todos los repos. Devuelve un pulso por cada visita nueva."""
        pulses: list[dict] = []
        if not self.enabled:
            return pulses
        now = time.time()
        try:
            if not self.fixed_repos and now - self._discovered_at > 6 * 3600:
                self.repos = self._discover()
                self._discovered_at = now
        except (HTTPError, URLError, OSError, ValueError) as exc:
            self.error = f"no se pudieron listar los repos: {_short(exc)}"
            log.warning(self.error)
            return pulses
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        t_views = t_uniques = v14 = u14 = 0
        ok = 0
        for repo in self.repos:
            try:
                data = self._get(f"/repos/{repo}/traffic/views")
            except HTTPError as exc:
                self.repo_errors[repo] = f"HTTP {exc.code}"
                continue
            except (URLError, OSError, ValueError) as exc:
                self.repo_errors[repo] = _short(exc)
                continue
            self.repo_errors.pop(repo, None)
            ok += 1
            v14 += int(data.get("count", 0))
            u14 += int(data.get("uniques", 0))
            first_time = repo not in self.seen
            known = self.seen.setdefault(repo, {})
            for day in data.get("views", []):
                ts, count = day.get("timestamp", ""), int(day.get("count", 0))
                if ts == today:
                    t_views += count
                    t_uniques += int(day.get("uniques", 0))
                new = count - known.get(ts, 0)
                if new > 0 and not first_time:            # la 1ª vez solo se toma la línea base
                    pulses += [{"repo": repo, "ts": ts}] * min(new, 50)
                known[ts] = max(known.get(ts, 0), count)
            for ts in sorted(known)[:-16]:                 # solo hace falta recordar ~14 días
                known.pop(ts, None)
        self.today_views, self.today_uniques = t_views, t_uniques
        self.views_14d, self.uniques_14d = v14, u14
        self.last_poll = now
        if self.repos and ok == 0:
            sample = next(iter(self.repo_errors.values()), "?")
            self.error = f"sin acceso al tráfico ({sample}); revisa el permiso Administration:read"
        elif self.repo_errors:
            self.error = f"{len(self.repo_errors)} repo(s) sin acceso"
        else:
            self.error = ""
        return pulses

    async def poll(self) -> list[dict]:
        return await asyncio.to_thread(self.poll_sync)

    # ------------------------------------------------------------ estado
    def status(self) -> dict:
        now = time.time()
        return {"enabled": self.enabled, "home": self.home, "repos": len(self.repos),
                "today_views": self.today_views, "today_uniques": self.today_uniques,
                "views_14d": self.views_14d, "uniques_14d": self.uniques_14d,
                "queued": self.queued, "error": self.error, "interval": self.interval,
                "last_poll_ago": round(now - self.last_poll) if self.last_poll else None,
                "last_pulse_ago": round(now - self.last_pulse) if self.last_pulse else None}


def heat_level(views_today: int) -> int:
    """Brillo persistente alrededor de la ciudad base según las visitas de hoy."""
    if views_today <= 0:
        return 0
    if views_today <= 5:
        return 1
    if views_today <= 20:
        return 2
    if views_today <= 50:
        return 3
    return 4


def _short(exc: Exception) -> str:
    return (getattr(exc, "reason", None) or str(exc) or type(exc).__name__).__str__()[:80]
