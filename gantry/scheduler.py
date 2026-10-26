"""Scheduler for a single gantry serving experiment clients.

Clients are either in-memory ``ExperimentClient`` objects (tests, event-jumping
simulation) or ``RemoteExperiment`` handles for labnode processes that were
started separately, e.g. from ``sim/`` or lab-devices.

The scheduler times every gantry leg itself (``movement_time``). With a
``gantry`` link it also sends the gantry node move, pick and place as it
goes, and assumes they worked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Union

from labnode import NodeClient, NodeError, NodeListener, NodeUnreachable
from gantry.substrates import Substrate


# Node states that mean the sample has stopped and is waiting for pickup.
COLLECTABLE_STATES = {"done", "stopped", "error"}
NOT_STARTED = "not started"
UNREADABLE = "unreadable"
# How often each experiment's listener thread reads its /status.
STATUS_POLL = 0.1
# The gantry's own resting spot. It is not a station: nothing is picked up there.
HOME = "home"
# Position name after a manual move: parked at a free point, not a location.
MANUAL = "manual"

Point = tuple[float, float]


def parse_route(text: str, known: set[str]) -> tuple[str, ...]:
    """Turn ``"A,B,C"`` into ``("experiment_a", "experiment_b", "experiment_c")``.

    A token that is already a station name (e.g. ``keyence``) is used as is.
    """
    route = tuple(
        token.strip() if token.strip() in known else f"experiment_{token.strip().lower()}"
        for token in text.split(",")
        if token.strip()
    )
    if not route:
        raise ValueError("Routes need at least one experiment.")
    unknown = [name for name in route if name not in known]
    if unknown:
        raise ValueError(f"Unknown experiments in route {text!r}: {', '.join(unknown)}")
    return route


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

    def poll(self, now: float) -> str:
        if self.substrate is None:
            return "idle"
        return "done" if self.is_finished(now) else "busy"

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


class RemoteExperiment:
    """An experiment running as its own labnode process, driven over HTTP.

    The node is never started from here: if nothing answers at ``address`` the
    client reports ``not started`` and counts as busy so no sample is sent to it.

    A listener thread keeps the node's latest /status, so ``poll`` never waits
    on the network; replies to /start and /taken are stored in it right away.
    """

    def __init__(self, name: str, address: str, token: Optional[str] = None, timeout: float = 1.0) -> None:
        self.name = name
        self.node = NodeClient(address, token=token, timeout=timeout)
        self.url = self.node.url
        self.substrate: Optional[Substrate] = None
        self.status: dict[str, Any] = {"state": NOT_STARTED}
        self.listener = NodeListener(self.url, "/status", interval=STATUS_POLL)

    def close(self) -> None:
        self.listener.close()

    @property
    def busy(self) -> bool:
        return self.substrate is not None or self.status.get("occupied", True)

    def poll(self, now: float) -> str:
        payload, problem, detail = self.listener.latest()
        self.status = {"state": problem, "error": detail} if problem else payload
        state = self.status["state"]
        if self.substrate is not None and state in COLLECTABLE_STATES:
            return "done"
        return state

    def start(self, substrate: Substrate, now: float) -> None:
        try:
            self.status = self.node.start(substrate.substrate_id)
            self.listener.update(self.status)
        except (NodeError, NodeUnreachable, ValueError) as error:
            raise RuntimeError(f"{self.name} did not start {substrate.substrate_id}: {error}") from None
        self.substrate = substrate

    def release(self, now: float) -> Substrate:
        if self.substrate is None:
            raise RuntimeError(f"client {self.name} has no sample to release")
        try:
            self.status = self.node.taken(self.substrate.substrate_id)
            self.listener.update(self.status)
        except (NodeError, NodeUnreachable, ValueError) as error:
            raise RuntimeError(f"{self.name} did not release {self.substrate.substrate_id}: {error}") from None
        substrate = self.substrate
        self.substrate = None
        return substrate


Client = Union[ExperimentClient, RemoteExperiment]


@dataclass
class _ManualMove:
    start: Point
    end: Point
    arrives_at: float


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
        clients: list[Client],
        substrates: list[Substrate],
        movement_time: float = 1.0,
        locations: Optional[dict[str, Point]] = None,
        shelf_slots: Optional[int] = None,
        gantry: Optional[Any] = None,
    ) -> None:
        """``locations`` maps "home", "shelf" and each client name to bed (x, y).

        ``shelf_slots`` caps how many samples the shelf holds (each keeps its slot
        while it is out at an experiment); None means only the 30-sample limit.
        ``gantry`` is a GantryLink (anything with ``send(command, **arguments)``);
        None means the gantry is only simulated.
        """
        if movement_time <= 0:
            raise ValueError("movement_time must be positive")
        self.clients = {client.name: client for client in clients}
        if len(self.clients) != len(clients):
            raise ValueError("client names must be unique")
        if shelf_slots is not None and len(substrates) > shelf_slots:
            raise ValueError(f"{len(substrates)} samples but the shelf has {shelf_slots} slots")
        self.substrates = substrates
        self.shelf_slots = shelf_slots
        self.gantry = gantry
        self.movement_time = movement_time
        self.now = 0.0
        self.position = HOME
        self._manual_point: Optional[Point] = None
        self._manual_move: Optional[_ManualMove] = None
        self.locations = dict(locations or {})
        if self.locations:
            missing = [name for name in (HOME, "shelf", *self.clients) if name not in self.locations]
            if missing:
                raise ValueError(f"no location for: {', '.join(missing)}")
        self._transport: Optional[_Transport] = None
        self._waiting: dict[str, tuple[Substrate, str]] = {}
        self.events: list[tuple[float, str, str]] = []

        for substrate in substrates:
            for experiment_name in substrate.experiment_order:
                if experiment_name not in self.clients:
                    raise ValueError(f"unknown experiment client: {experiment_name}")

    @property
    def gantry_busy(self) -> bool:
        return self._transport is not None or self._manual_move is not None

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

    def coordinates(self) -> Optional[Point]:
        """The gantry's exact position, or None when it cannot be read."""
        return self.position_reading()[0]

    def position_reading(self) -> tuple[Optional[Point], str, str, str]:
        """``(point, source, problem, detail)`` for the gantry head.

        With a gantry node, ``point`` is what the node last reported (source
        "gantry"); if it cannot be read, ``point`` is None and ``problem`` says
        why. Without one, the gantry is only simulated (source "simulated").
        """
        if self.gantry is not None:
            point, problem, detail = self.gantry.position()
            return point, "gantry", problem, detail
        point = self._simulated_coordinates()
        if point is None:
            return None, "simulated", UNREADABLE, "gantry has no locations configured"
        return point, "simulated", "", ""

    def _simulated_coordinates(self) -> Optional[Point]:
        """Where a simulated gantry is, from the scheduler's own timing."""
        if not self.locations:
            return None
        transport = self._transport
        if self._manual_move is not None:
            start, end = self._manual_move.start, self._manual_move.end
            started_at = self._manual_move.arrives_at - self.movement_time
        elif transport is None:
            return self._point_of(self.position)
        else:
            if transport.phase == "pickup":
                start, end = self._point_of(self.position), self.locations[transport.source]
            else:
                start, end = self.locations[transport.source], self.locations[transport.destination]
            started_at = transport.arrives_at - self.movement_time
        progress = min(1.0, max(0.0, (self.now - started_at) / self.movement_time))
        return (
            start[0] + (end[0] - start[0]) * progress,
            start[1] + (end[1] - start[1]) * progress,
        )

    def pos(self) -> dict[str, Any]:
        """Payload for GET /pos. ``x`` and ``y`` are None when the gantry cannot be read."""
        point, source, problem, detail = self.position_reading()
        moving = self.gantry_busy
        return {
            "x": None if point is None else point[0],
            "y": None if point is None else point[1],
            "z": 0.0,
            "moving": moving,
            "at": None if moving or self.position == MANUAL else self.position,
            "source": source,
            "problem": problem or None,
            "detail": detail or None,
        }

    def move_to(self, point: Point) -> None:
        """Send the idle gantry to ``point``; it takes ``movement_time`` like any leg.

        Refused while it is moving so a sample is never abandoned mid-delivery.
        The scheduler waits for the move, then carries on from the new spot.
        """
        if not self.locations:
            raise ValueError("gantry has no locations configured")
        if not all(math.isfinite(value) for value in point):
            raise ValueError("x and y must be finite numbers")
        if self.gantry_busy:
            raise RuntimeError("gantry is busy moving; try again when it stops")
        start = self._simulated_coordinates()
        assert start is not None
        self._manual_move = _ManualMove(start, (float(point[0]), float(point[1])), self.now + self.movement_time)
        self._command("move", x=float(point[0]), y=float(point[1]), params={"to": MANUAL})
        self.events.append((self.now, "gantry_manual_move", f"({point[0]:g}, {point[1]:g})"))

    def _command(self, command: str, **arguments: Any) -> None:
        if self.gantry is not None:
            self.gantry.send(command, **arguments)

    def _move_command(self, location: str) -> None:
        if self.locations:
            x, y = self.locations[location]
            self._command("move", x=x, y=y, params={"to": location})

    def _hand_command(self, command: str, location: str, substrate: Substrate) -> None:
        self._command(command, params={"station": location, "sample_id": substrate.substrate_id})

    def _point_of(self, position: str) -> Point:
        if position == MANUAL:
            assert self._manual_point is not None
            return self._manual_point
        return self.locations[position]

    def snapshot(self, event_limit: int = 200) -> dict[str, Any]:
        """JSON-ready view of the whole scheduler, served at /state."""
        point, source, problem, detail = self.position_reading()
        return {
            "now": self.now,
            "completed": self.completed,
            "position": self.position,
            "movement_time": self.movement_time,
            "shelf_slots": self.shelf_slots,
            "gantry_link": self.gantry.report() if self.gantry is not None else None,
            "transport": self.transport_snapshot,
            "manual_move": None if self._manual_move is None else {
                "to": list(self._manual_move.end),
                "arrives_at": self._manual_move.arrives_at,
            },
            "coordinates": None if point is None else list(point),
            "coordinates_source": source,
            "coordinates_problem": problem or None,
            "coordinates_detail": detail or None,
            "locations": self.locations,
            "clients": {
                name: {
                    "url": getattr(client, "url", None),
                    "sample_id": client.substrate.substrate_id if client.substrate else None,
                }
                for name, client in self.clients.items()
            },
            "substrates": [
                {
                    "substrate_id": substrate.substrate_id,
                    "experiment_order": list(substrate.experiment_order),
                    "current_experiment_index": substrate.current_experiment_index,
                    "location": substrate.location,
                    "completed": substrate.completed,
                }
                for substrate in self.substrates
            ],
            "queue": self.queue_snapshot,
            "event_count": len(self.events),
            "events": [list(event) for event in self.events[-event_limit:]],
        }

    def add_substrate(self, substrate: Substrate) -> None:
        """Add a new substrate to the shelf queue without stopping active work."""
        if len(self.substrates) >= 30:
            raise ValueError("The simulator supports at most 30 substrates.")
        if self.shelf_slots is not None and len(self.substrates) >= self.shelf_slots:
            raise ValueError(f"The shelf is full ({self.shelf_slots} slots).")
        if any(existing.substrate_id == substrate.substrate_id for existing in self.substrates):
            raise ValueError(f"duplicate substrate id: {substrate.substrate_id}")
        for experiment_name in substrate.experiment_order:
            if experiment_name not in self.clients:
                raise ValueError(f"unknown experiment client: {experiment_name}")
        self.substrates.append(substrate)
        route = ",".join(substrate.experiment_order)
        self.events.append((self.now, "substrate_added", f"{substrate.substrate_id}:{route}"))
        if not self.gantry_busy:
            self._schedule_next_transport()

    def tick(self, now: float) -> None:
        """Advance the simulation to ``now`` and schedule available work."""
        if now < self.now:
            raise ValueError("simulation time cannot move backwards")
        self.now = now
        self._collect_finished_clients()
        if self._manual_move is not None and now >= self._manual_move.arrives_at:
            self._manual_point = self._manual_move.end
            self._manual_move = None
            self.position = MANUAL
            self.events.append((self.now, "gantry_manual_arrived", f"({self._manual_point[0]:g}, {self._manual_point[1]:g})"))
        if self._transport is not None and now >= self._transport.arrives_at:
            self._finish_transport()
        if not self.gantry_busy:
            self._schedule_next_transport()

    def run_until_complete(self) -> None:
        """Run by jumping between client and gantry events, without sleeping."""
        self.tick(self.now)
        while not self.completed:
            event_times = []
            if self._transport is not None:
                event_times.append(self._transport.arrives_at)
            if self._manual_move is not None:
                event_times.append(self._manual_move.arrives_at)
            event_times.extend(
                client.finishes_at
                for client in self.clients.values()
                if getattr(client, "finishes_at", None) is not None
            )
            if not event_times:
                raise RuntimeError("simulation is deadlocked")
            self.tick(min(event_time for event_time in event_times if event_time > self.now))

    def _collect_finished_clients(self) -> None:
        for client in self.clients.values():
            substrate = client.substrate
            if client.poll(self.now) == "done" and substrate is not None:
                if substrate.substrate_id not in self._waiting:
                    self._waiting[substrate.substrate_id] = (substrate, client.name)
                    self.events.append((self.now, "client_finished", f"{client.name}:{substrate.substrate_id}:{self.gantry_busy}"))

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
        self._move_command(source)
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
                try:
                    released = client.release(self.now)
                except RuntimeError as error:
                    # Leave the sample waiting; the next tick tries the pickup again.
                    self._transport = None
                    self.events.append((self.now, "pickup_failed", str(error)))
                    return
                if released is not transport.substrate:
                    raise RuntimeError("client released the wrong substrate")
                self._waiting.pop(transport.substrate.substrate_id, None)
                transport.substrate.location = "gantry"
            self._hand_command("pick", transport.source, transport.substrate)
            transport.phase = "delivery"
            transport.arrives_at = self.now + self.movement_time
            self._move_command(transport.destination)
            self.events.append((self.now, "gantry_picked_up", transport.substrate.substrate_id))
            return

        self._hand_command("place", transport.destination, transport.substrate)
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
            try:
                client.start(transport.substrate, self.now)
            except RuntimeError as error:
                # The node would not take the sample, so carry it back to the shelf.
                self.events.append((self.now, "delivery_failed", str(error)))
                self._hand_command("pick", transport.destination, transport.substrate)
                transport.source = transport.destination
                transport.destination = "shelf"
                transport.arrives_at = self.now + self.movement_time
                self._move_command("shelf")
                return
            transport.substrate.mark_delivered(transport.destination)
        self.position = transport.destination
        self.events.append((self.now, "gantry_delivered", f"{transport.substrate.substrate_id}->{transport.destination}"))
        self._transport = None
