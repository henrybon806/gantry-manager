"""Deterministic simulation of a single gantry serving experiment clients."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
from dataclasses import dataclass
from typing import Any, Optional

from substrates import Substrate


@dataclass
class ExperimentClient:
    """A simulated experiment that can process one substrate at a time."""

    name: str
    duration: float
    substrate: Optional[Substrate] = None
    started_at: Optional[float] = None
    finishes_at: Optional[float] = None

    @property
    def busy(self) -> bool:
        return self.substrate is not None

    @property
    def finished(self) -> bool:
        return self.busy and self.finishes_at is not None

    def start(self, substrate: Substrate, now: float) -> None:
        if self.busy:
            raise RuntimeError(f"client {self.name} is already occupied")
        self.substrate = substrate
        self.started_at = now
        self.finishes_at = now + self.duration

    def is_finished(self, now: float) -> bool:
        return self.busy and self.finishes_at is not None and now >= self.finishes_at

    def release(self, now: float) -> Substrate:
        if self.substrate is None or not self.is_finished(now):
            raise RuntimeError(f"client {self.name} is not ready for pickup")
        substrate = self.substrate
        self.substrate = None
        self.started_at = None
        self.finishes_at = None
        return substrate


class ExperimentServer:
    """TCP status server for one simulated experiment client."""

    def __init__(self, client: ExperimentClient, host: str = "127.0.0.1") -> None:
        self.client = client
        self.host = host
        self.port: Optional[int] = None
        self._server: Optional[_ExperimentTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.start()

    @property
    def thread_name(self) -> Optional[str]:
        return self._thread.name if self._thread else None

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _ExperimentTCPServer((self.host, 0), _ExperimentRequestHandler, self)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"{self.client.name}-server",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        self.port = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def read_status(self, now: float) -> dict[str, Any]:
        if self.port is None:
            raise RuntimeError(f"{self.client.name} server is not running")
        with socket.create_connection((self.host, self.port), timeout=0.5) as connection:
            connection.sendall(f"STATUS {now}\n".encode("ascii"))
            response = connection.recv(4096).decode("utf-8")
        return json.loads(response)

    def _status_payload(self, now: float) -> dict[str, Any]:
        substrate = self.client.substrate
        if substrate is None:
            state = "idle"
            remaining = 0.0
        elif self.client.is_finished(now):
            state = "finished"
            remaining = 0.0
        else:
            state = "busy"
            remaining = max(0.0, (self.client.finishes_at or now) - now)
        elapsed = 0.0
        if self.client.started_at is not None:
            elapsed = min(self.client.duration, max(0.0, now - self.client.started_at))
        return {
            "name": self.client.name,
            "state": state,
            "substrate_id": substrate.substrate_id if substrate else None,
            "remaining": remaining,
            "elapsed": elapsed,
            "duration": self.client.duration,
            "port": self.port,
            "thread": self.thread_name,
        }


class _ExperimentRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        command = self.rfile.readline().decode("ascii", errors="ignore").strip().split()
        if len(command) == 2 and command[0].upper() == "STATUS":
            payload = self.server.experiment_server._status_payload(float(command[1]))
            self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
        else:
            self.wfile.write(b'{"error": "expected STATUS <time>"}\n')


class _ExperimentTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[socketserver.BaseRequestHandler], experiment_server: ExperimentServer) -> None:
        super().__init__(address, handler)
        self.experiment_server = experiment_server


@dataclass
class _Transport:
    substrate: Substrate
    source: str
    destination: str
    phase: str
    arrives_at: float


class GantryManager:
    """Schedule samples through clients while owning the one gantry resource."""

    def __init__(
        self,
        clients: list[ExperimentClient],
        substrates: list[Substrate],
        movement_time: float = 1.0,
    ) -> None:
        if movement_time <= 0:
            raise ValueError("movement_time must be positive")
        self.clients = {client.name: client for client in clients}
        if len(self.clients) != len(clients):
            raise ValueError("client names must be unique")
        self.substrates = substrates
        self.movement_time = movement_time
        self.now = 0.0
        self.position = "shelf"
        self._transport: Optional[_Transport] = None
        self._waiting: dict[str, tuple[Substrate, str]] = {}
        self.events: list[tuple[float, str, str]] = []

        for substrate in substrates:
            for experiment_name in substrate.experiment_order:
                if experiment_name not in self.clients:
                    raise ValueError(f"unknown experiment client: {experiment_name}")
        self.servers = {
            client.name: ExperimentServer(client) for client in clients
        }

    def close(self) -> None:
        for server in self.servers.values():
            server.close()

    @property
    def gantry_busy(self) -> bool:
        return self._transport is not None

    @property
    def completed(self) -> bool:
        return all(substrate.completed for substrate in self.substrates)

    @property
    def transport_snapshot(self) -> Optional[dict[str, Any]]:
        """Return the active movement state for observers such as the GUI."""
        if self._transport is None:
            return None
        return {
            "substrate_id": self._transport.substrate.substrate_id,
            "position": self.position,
            "source": self._transport.source,
            "destination": self._transport.destination,
            "phase": self._transport.phase,
            "started_at": self._transport.arrives_at - self.movement_time,
            "arrives_at": self._transport.arrives_at,
        }

    @property
    def queue_snapshot(self) -> list[dict[str, str]]:
        """Return substrates waiting for a gantry pickup, in scheduler order."""
        active_id = self._transport.substrate.substrate_id if self._transport else None
        waiting_ids = set(self._waiting)
        queue = []
        for substrate in self.substrates:
            if substrate.completed or substrate.substrate_id == active_id:
                continue
            if substrate.substrate_id in waiting_ids:
                state = "waiting for pickup"
            elif substrate.location == "shelf":
                state = "queued"
            else:
                continue
            queue.append({
                "substrate_id": substrate.substrate_id,
                "next_experiment": substrate.next_experiment or "return to shelf",
                "state": state,
            })
        return queue

    def add_substrate(self, substrate: Substrate) -> None:
        """Add a new substrate to the shelf queue without stopping active work."""
        if len(self.substrates) >= 30:
            raise ValueError("The simulator supports at most 30 substrates.")
        if any(existing.substrate_id == substrate.substrate_id for existing in self.substrates):
            raise ValueError(f"duplicate substrate id: {substrate.substrate_id}")
        for experiment_name in substrate.experiment_order:
            if experiment_name not in self.clients:
                raise ValueError(f"unknown experiment client: {experiment_name}")
        self.substrates.append(substrate)
        route = ",".join(substrate.experiment_order)
        self.events.append((self.now, "substrate_added", f"{substrate.substrate_id}:{route}"))
        if self._transport is None:
            self._schedule_next_transport()

    def tick(self, now: float) -> None:
        """Advance the simulation to ``now`` and schedule available work."""
        if now < self.now:
            raise ValueError("simulation time cannot move backwards")
        self.now = now
        self._collect_finished_clients()
        if self._transport is not None and now >= self._transport.arrives_at:
            self._finish_transport()
        if self._transport is None:
            self._schedule_next_transport()

    def run_until_complete(self) -> None:
        """Run by jumping between client and gantry events, without sleeping."""
        self.tick(self.now)
        while not self.completed:
            event_times = []
            if self._transport is not None:
                event_times.append(self._transport.arrives_at)
            event_times.extend(
                client.finishes_at
                for client in self.clients.values()
                if client.finishes_at is not None
            )
            if not event_times:
                raise RuntimeError("simulation is deadlocked")
            self.tick(min(event_time for event_time in event_times if event_time > self.now))

    def _collect_finished_clients(self) -> None:
        for server in self.servers.values():
            status = server.read_status(self.now)
            if status["state"] == "finished":
                substrate = server.client.substrate
                assert substrate is not None
                if substrate.substrate_id not in self._waiting:
                    self._waiting[substrate.substrate_id] = (substrate, server.client.name)
                    self.events.append((self.now, "client_finished", f"{server.client.name}:{substrate.substrate_id}:{self.gantry_busy}"))

    def _schedule_next_transport(self) -> None:
        for substrate_id, (substrate, source) in self._waiting.items():
            destination = substrate.next_experiment or "shelf"
            if destination == "shelf" or not self.clients[destination].busy:
                self._begin_transport(substrate, source, destination, "pickup")
                return

        # Break cycles where every finished sample's next client is occupied.
        blocked = next(
            (
                (substrate, source)
                for substrate, source in self._waiting.values()
                if source != "shelf"
            ),
            None,
        )
        if blocked is not None:
            substrate, source = blocked
            self._begin_transport(substrate, source, "shelf", "pickup")
            return

        for substrate in self.substrates:
            destination = substrate.next_experiment
            if substrate.location != "shelf" or destination is None:
                continue
            if not self.clients[destination].busy:
                self._begin_transport(substrate, "shelf", destination, "pickup")
                return

    def _begin_transport(
        self, substrate: Substrate, source: str, destination: str, phase: str
    ) -> None:
        self._transport = _Transport(
            substrate=substrate,
            source=source,
            destination=destination,
            phase=phase,
            arrives_at=self.now + self.movement_time,
        )
        self.events.append((self.now, "gantry_departed", f"{source}->{destination}:{substrate.substrate_id}"))

    def _finish_transport(self) -> None:
        transport = self._transport
        assert transport is not None
        self.position = transport.source if transport.phase == "pickup" else transport.destination

        if transport.phase == "pickup":
            if transport.source == "shelf":
                transport.substrate.location = "gantry"
                self._waiting.pop(transport.substrate.substrate_id, None)
            else:
                client = self.clients[transport.source]
                released = client.release(self.now)
                if released is not transport.substrate:
                    raise RuntimeError("client released the wrong substrate")
                self._waiting.pop(transport.substrate.substrate_id, None)
                transport.substrate.location = "gantry"
            transport.phase = "delivery"
            transport.arrives_at = self.now + self.movement_time
            self.events.append((self.now, "gantry_picked_up", transport.substrate.substrate_id))
            return

        if transport.destination == "shelf":
            if transport.substrate.next_experiment is None:
                transport.substrate.mark_returned_to_shelf()
            else:
                transport.substrate.location = "shelf"
                self._waiting[transport.substrate.substrate_id] = (
                    transport.substrate,
                    "shelf",
                )
        else:
            client = self.clients[transport.destination]
            transport.substrate.mark_delivered(transport.destination)
            client.start(transport.substrate, self.now)
        self.position = transport.destination
        self.events.append((self.now, "gantry_delivered", f"{transport.substrate.substrate_id}->{transport.destination}"))
        self._transport = None
