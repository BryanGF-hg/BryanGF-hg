#!/usr/bin/env python3
"""Modelo de paquete y su ciclo de vida."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

CREATED = "CREATED"
QUEUED = "QUEUED"
TRANSMITTING = "TRANSMITTING"
ROUTING = "ROUTING"
DELIVERED = "DELIVERED"
LOST = "LOST"
FAILED = "FAILED"

STATES = (CREATED, QUEUED, TRANSMITTING, ROUTING, DELIVERED, LOST, FAILED)
TERMINAL = frozenset({DELIVERED, LOST, FAILED})

# Transiciones permitidas: evita que un resultado tardío "resucite" un paquete
TRANSITIONS = {
    CREATED: {QUEUED, FAILED},
    QUEUED: {TRANSMITTING, ROUTING, LOST, FAILED},
    TRANSMITTING: {ROUTING, QUEUED, DELIVERED, LOST, FAILED},
    ROUTING: {QUEUED, TRANSMITTING, LOST, FAILED},
    DELIVERED: set(), LOST: set(), FAILED: set(),
}

PRIORITIES = {0: "LOW", 1: "NORMAL", 2: "HIGH"}


class InvalidTransition(Exception):
    pass


@dataclass
class Packet:
    id: str
    seq: int
    source: str
    destination: str
    size: int
    priority: int = 1
    route: list[str] = field(default_factory=list)
    hop: int = 0                     # índice del salto actual (route[hop] -> route[hop+1])
    latency: float = 0.0             # ms acumulados
    created_at: float = field(default_factory=time.time)
    status: str = CREATED
    reroutes: int = 0
    reason: str = ""                 # motivo de pérdida/fallo
    finished_at: float = 0.0
    tag: str = ""                    # origen especial, p. ej. "github" (pulso de visitas)

    # ------------------------------------------------------------ derivados
    @property
    def total_hops(self) -> int:
        return max(0, len(self.route) - 1)

    @property
    def current_node(self) -> str:
        return self.route[self.hop] if self.route else self.source

    @property
    def next_node(self) -> str | None:
        return self.route[self.hop + 1] if self.hop + 1 < len(self.route) else None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def label(self) -> str:
        return f"PACKET #{self.seq:04d}"

    # ------------------------------------------------------------ ciclo vida
    def set_status(self, new: str, reason: str = "") -> None:
        if new not in STATES:
            raise InvalidTransition(f"Estado desconocido {new}")
        if new != self.status and new not in TRANSITIONS[self.status]:
            raise InvalidTransition(f"{self.id}: {self.status} -> {new} no permitido")
        self.status = new
        if reason:
            self.reason = reason
        if new in TERMINAL:
            self.finished_at = time.time()

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(total_hops=self.total_hops, current_node=self.current_node, next_node=self.next_node)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Packet":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: d[k] for k in keys if k in d})


def packet_id(seq: int) -> str:
    return f"PKT-{seq:06d}"
