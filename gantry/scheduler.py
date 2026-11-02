"""Scheduler for a single gantry serving experiment clients.

Clients are either in-memory ``ExperimentClient`` objects (tests, event-jumping
simulation) or ``RemoteExperiment`` handles for labnode processes that were
started separately, e.g. from ``sim/`` or lab-devices.

The gantry is told where to go by name, never by coordinates: each sample's
trip is a ``pick`` at its source then a ``place`` at its destination, with
``station`` (and ``slot`` on the shelf), and the gantry node finds its own
way there. With a ``gantry`` link each leg lasts as long as the real gantry
takes (and at least ``movement_time``); without one, exactly ``movement_time``.
An instrument gets /taken only once the gantry has picked from it, and
/start only once the gantry has placed in it. ``locations`` are bed points
for drawing and for the gantry simulated without a node.

The scheduler never finishes: substrates are added to numbered shelf slots
whenever they arrive and run as soon as their next instrument is free. A
finished substrate stays in its slot (shown as done) until the slot is
cleared. An emergency stop halts scheduling until ``resume``.
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
MAX_SAMPLES = 30

# Scheduler states: nothing left to do, substrates moving or running, or
# halted by an emergency stop until resumed.
IDLE = "idle"
RUNNING = "running"
STOPPED = "stopped"

# What a substrate in a slot is doing, for the shelf view.
QUEUED = "queued"        # on the shelf, no step run yet
WAITING = "waiting"      # on the shelf between steps
MOVING = "moving"        # on the gantry
IN_EXPERIMENT = "in experiment"
DONE = "done"

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


def describe_event(kind: str, detail: str) -> str:
    """One scheduler event as a short sentence for the GUI."""
    try:
        if kind == "gantry_departed":
            route, sample = detail.rsplit(":", 1)
            source, destination = route.split("->")
            return f"Gantry went to pick {sample} from {source} for {destination}"
        if kind == "gantry_picked_up":
            return f"Picked up {detail}"
        if kind == "gantry_delivered":
            sample, destination = detail.split("->")
            return f"Placed {sample} in {destination}"
        if kind == "client_finished":
            client, sample, _ = detail.split(":")
            return f"{client} finished {sample}"
        if kind == "substrate_added":
            where, route = detail.split(":", 1)
            sample, slot = where.rsplit("@", 1)
            return f"Added {sample} to slot {slot} ({route})"
        if kind == "substrate_cleared":
            sample, slot = detail.rsplit("@", 1)
            return f"Cleared slot {slot} ({sample})"
    except ValueError:
        pass
    labels = {
        "scheduler_stopped": "EMERGENCY STOP",
        "scheduler_resumed": "Resumed",
        "gantry_manual_move": "Manual move to",
        "gantry_manual_arrived": "Manual move arrived at",
        "delivery_failed": "Delivery failed:",
        "taken_failed": "Pickup not confirmed:",
        "node_stopped": "Stopped",
        "node_stop_failed": "Stop failed:",
    }
    label = labels.get(kind, kind.replace("_", " "))
    return f"{label} {detail}".strip() if kind != "scheduler_stopped" else label


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
    ticket: Optional[int] = None


@dataclass
class _Transport:
    """One sample's trip: pickup (go to source and pick), then delivery (go to destination and place)."""

    substrate: Substrate
    source: str
    destination: str
    phase: str
    arrives_at: float
    # The gantry command the current phase waits for; None without a gantry node.
    ticket: Optional[int] = None


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

        ``shelf_slots`` is how many numbered slots the shelf has (each substrate
        keeps its slot while it is out at an experiment); None means 30.
        ``substrates`` are added to slots 1, 2, ... straight away.
        ``gantry`` is a GantryLink (anything with ``send(command, **arguments)``,
        plus ``done(ticket)`` if ``send`` returns tickets); None means the gantry
        is only simulated.
        """
        if movement_time <= 0:
            raise ValueError("movement_time must be positive")
        self.clients = {client.name: client for client in clients}
        if len(self.clients) != len(clients):
            raise ValueError("client names must be unique")
        if shelf_slots is not None and not 1 <= shelf_slots <= MAX_SAMPLES:
            raise ValueError(f"shelf_slots must be 1 to {MAX_SAMPLES}")
        self.shelf_slots = shelf_slots or MAX_SAMPLES
        if len(substrates) > self.shelf_slots:
            raise ValueError(f"{len(substrates)} samples but the shelf has {self.shelf_slots} slots")
        # In the order they were added, which is the order they are started in.
        self.substrates: list[Substrate] = []
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
        self.stopped = False
        self.stopped_at: Optional[float] = None
        # Scheduled from the first tick.
        for substrate in substrates:
            self._place(substrate, None)

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
        """Payload for GET /pos. ``x``, ``y`` and ``z`` are None when the gantry cannot be read."""
        point, source, problem, detail = self.position_reading()
        moving = self.gantry_busy
        if point is None:
            z = None
        elif self.gantry is not None:
            z = self.gantry.z()
        else:
            z = 0.0
        return {
            "x": None if point is None else point[0],
            "y": None if point is None else point[1],
            "z": z,
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
        self._manual_move.ticket = self._command("move", x=float(point[0]), y=float(point[1]), params={"to": MANUAL})
        self.events.append((self.now, "gantry_manual_move", f"({point[0]:g}, {point[1]:g})"))

    def _command(self, command: str, **arguments: Any) -> Optional[int]:
        """Send a gantry command; returns its ticket, or None if there is nothing to wait for."""
        if self.gantry is None:
            return None
        return self.gantry.send(command, **arguments)

    def _gantry_done(self, ticket: Optional[int]) -> bool:
        return ticket is None or self.gantry is None or self.gantry.done(ticket)

    def _hand_command(self, command: str, location: str, substrate: Substrate) -> Optional[int]:
        """``pick`` or ``place`` at a named location; the gantry finds its own way there."""
        params: dict[str, Any] = {"station": location, "sample_id": substrate.substrate_id}
        if location == "shelf":
            # Each sample keeps its own shelf slot from added to cleared.
            # The gantry numbers slots from 0; the API and GUI from 1.
            params["slot"] = substrate.slot - 1
        return self._command(command, params=params)

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
            "state": self.state,
            "stopped_at": self.stopped_at,
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
                    "slot": substrate.slot,
                }
                for substrate in self.substrates
            ],
            "shelf": self.shelf(),
            "queue": self.queue_snapshot,
            "event_count": len(self.events),
            "events": [list(event) for event in self.events[-event_limit:]],
        }

    @property
    def state(self) -> str:
        """``stopped`` after an emergency stop, ``running`` while there is work, else ``idle``."""
        if self.stopped:
            return STOPPED
        return RUNNING if self.gantry_busy or not self.completed else IDLE

    def _slot_map(self) -> dict[int, Substrate]:
        return {substrate.slot: substrate for substrate in self.substrates}

    def free_slots(self) -> list[int]:
        taken = self._slot_map()
        return [slot for slot in range(1, self.shelf_slots + 1) if slot not in taken]

    def add_substrate(self, substrate: Substrate, slot: Optional[int] = None) -> int:
        """Put ``substrate`` in shelf ``slot`` (or the first free one) and queue it; returns the slot.

        It is scheduled as soon as its first instrument is free, unless the
        scheduler is stopped. ValueError if the slot is taken or out of range,
        the shelf is full, the id is in use, or the route names an unknown
        experiment.
        """
        slot = self._place(substrate, slot)
        if not self.stopped and not self.gantry_busy:
            self._schedule_next_transport()
        return slot

    def _place(self, substrate: Substrate, slot: Optional[int]) -> int:
        """Validate ``substrate`` and put it in ``slot`` (or the first free one), without scheduling."""
        if not substrate.experiment_order:
            raise ValueError(f"{substrate.substrate_id} needs at least one experiment in its route")
        unknown = [name for name in substrate.experiment_order if name not in self.clients]
        if unknown:
            raise ValueError(f"unknown experiment client: {', '.join(unknown)}")
        if any(other.substrate_id == substrate.substrate_id for other in self.substrates):
            raise ValueError(f"duplicate sample id: {substrate.substrate_id}")
        if slot is None:
            free = self.free_slots()
            if not free:
                raise ValueError(f"the shelf is full ({self.shelf_slots} slots)")
            slot = free[0]
        elif isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= self.shelf_slots:
            raise ValueError(f"slot must be 1 to {self.shelf_slots}")
        elif slot in self._slot_map():
            raise ValueError(f"slot {slot} already holds {self._slot_map()[slot].substrate_id}")
        substrate.slot = slot
        substrate.added_at = self.now
        self.substrates.append(substrate)
        route = ",".join(substrate.experiment_order)
        self.events.append((self.now, "substrate_added", f"{substrate.substrate_id}@{slot}:{route}"))
        return slot

    def add_samples(self, substrates: list[Substrate]) -> list[int]:
        """Add several substrates to the first free slots; all or nothing."""
        if len(substrates) > len(self.free_slots()):
            raise ValueError(f"the shelf is full ({self.shelf_slots} slots)")
        ids = [substrate.substrate_id for substrate in substrates]
        duplicates = sorted({sample_id for sample_id in ids if ids.count(sample_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate sample id: {', '.join(duplicates)}")
        added: list[Substrate] = []
        try:
            for substrate in substrates:
                self._place(substrate, None)
                added.append(substrate)
        except ValueError:
            for substrate in added:
                self.substrates.remove(substrate)
            raise
        if not self.stopped and not self.gantry_busy:
            self._schedule_next_transport()
        return [substrate.slot for substrate in substrates]

    def _clearable(self, substrate: Substrate) -> Optional[str]:
        """Why ``substrate`` cannot be cleared now, or None if it can."""
        if self.stopped:
            return None
        if self._transport is not None and self._transport.substrate is substrate:
            return "the gantry is moving it"
        if substrate.location != "shelf":
            return f"it is in {substrate.location}"
        return None

    def clear_slot(self, slot: int) -> Substrate:
        """Remove the substrate in ``slot`` from the queue and the shelf view.

        While the scheduler runs, only substrates sitting on the shelf can be
        cleared (RuntimeError otherwise); after an emergency stop any can be,
        once it has been taken out by hand. ValueError if the slot is empty.
        """
        substrate = self._slot_map().get(slot)
        if substrate is None:
            raise ValueError(f"slot {slot} is empty")
        reason = self._clearable(substrate)
        if reason is not None:
            raise RuntimeError(f"cannot clear slot {slot}: {reason}")
        self._remove(substrate)
        return substrate

    def clear_shelf(self) -> tuple[list[int], list[int]]:
        """Clear every slot that can be cleared; returns ``(cleared, kept)`` slot numbers."""
        cleared, kept = [], []
        for substrate in sorted(self.substrates, key=lambda item: item.slot):
            if self._clearable(substrate) is None:
                self._remove(substrate)
                cleared.append(substrate.slot)
            else:
                kept.append(substrate.slot)
        return cleared, kept

    def _remove(self, substrate: Substrate) -> None:
        self.substrates.remove(substrate)
        self._waiting.pop(substrate.substrate_id, None)
        for client in self.clients.values():
            # Only after a stop: the instrument itself stays occupied until it gets /taken.
            if client.substrate is substrate:
                client.substrate = None
        self.events.append((self.now, "substrate_cleared", f"{substrate.substrate_id}@{substrate.slot}"))

    def emergency_stop(self) -> list[str]:
        """Halt scheduling now; return the names of clients holding a sample.

        Nothing new is scheduled and the current gantry move is abandoned.
        Samples stay where they are: in an instrument, on the shelf, or on the
        gantry if it was carrying one. The caller sends the actual /stop to
        the gantry and those clients. RuntimeError if already stopped.
        """
        if self.stopped:
            raise RuntimeError("the scheduler is already stopped")
        point = self._simulated_coordinates()
        if self._transport is not None or self._manual_move is not None:
            # Parked wherever it was, as far as the scheduler knows.
            self._manual_point = point
            self.position = MANUAL if point is not None else self.position
        self._transport = None
        self._manual_move = None
        self._waiting.clear()
        busy = [name for name, client in self.clients.items() if client.substrate is not None]
        self.stopped = True
        self.stopped_at = self.now
        self.events.append((self.now, "scheduler_stopped", "emergency stop"))
        return busy

    def resume(self) -> None:
        """Carry on scheduling after an emergency stop.

        Samples left in an instrument are collected once that node says it is
        done or stopped again; a sample that was on the gantry is taken to be
        back on the shelf in its slot. RuntimeError if not stopped.
        """
        if not self.stopped:
            raise RuntimeError("the scheduler is not stopped")
        for substrate in self.substrates:
            if substrate.location == "gantry":
                substrate.location = "shelf"
        self.stopped = False
        self.stopped_at = None
        self.events.append((self.now, "scheduler_resumed", ""))
        if not self.gantry_busy:
            self._schedule_next_transport()

    def substrate_view(self, substrate: Substrate) -> dict[str, Any]:
        if substrate.completed:
            status = DONE
        elif substrate.location == "gantry":
            status = MOVING
        elif substrate.location == "shelf":
            status = QUEUED if substrate.current_experiment_index == 0 else WAITING
        else:
            status = IN_EXPERIMENT
        moving = self._transport is not None and self._transport.substrate is substrate
        return {
            "id": substrate.substrate_id,
            "slot": substrate.slot,
            "route": list(substrate.experiment_order),
            "step": substrate.current_experiment_index,
            "next": substrate.next_experiment,
            "location": substrate.location,
            "status": status,
            "picking_up": moving and self._transport.phase == "pickup",
            "waiting_for_pickup": substrate.substrate_id in self._waiting and substrate.location != "shelf",
            "completed": substrate.completed,
            "metadata": substrate.metadata,
            "added_at": substrate.added_at,
        }

    def shelf(self) -> dict[str, Any]:
        """JSON-ready view of every shelf slot, for GET /shelf."""
        taken = self._slot_map()
        return {
            "state": self.state,
            "stopped_at": self.stopped_at,
            "slots": self.shelf_slots,
            "done": sum(substrate.completed for substrate in self.substrates),
            "total": len(self.substrates),
            "shelf": [
                {"slot": slot, "substrate": self.substrate_view(taken[slot]) if slot in taken else None}
                for slot in range(1, self.shelf_slots + 1)
            ],
        }

    def tick(self, now: float) -> None:
        """Advance the simulation to ``now`` and schedule available work."""
        if now < self.now:
            raise ValueError("simulation time cannot move backwards")
        self.now = now
        running = not self.stopped
        if running:
            self._collect_finished_clients()
        else:
            # Keep node statuses fresh for the GUI, but start nothing.
            for client in self.clients.values():
                client.poll(now)
        manual = self._manual_move
        if manual is not None and now >= manual.arrives_at and self._gantry_done(manual.ticket):
            self._manual_point = self._manual_move.end
            self._manual_move = None
            self.position = MANUAL
            self.events.append((self.now, "gantry_manual_arrived", f"({self._manual_point[0]:g}, {self._manual_point[1]:g})"))
        if not running:
            return
        transport = self._transport
        if transport is not None and now >= transport.arrives_at and self._gantry_done(transport.ticket):
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

    def _next_choice(
        self, exclude: Optional[Substrate] = None, reserved: frozenset[str] = frozenset()
    ) -> Optional[tuple[Substrate, str, str]]:
        """``(substrate, source, destination)`` the gantry would carry next, or None.

        The one place the order is decided, for scheduling and for the plan the
        GUI shows. ``exclude`` (the sample being carried) is skipped and
        ``reserved`` stations (where it is going) count as busy.
        """
        def free(name: str) -> bool:
            return name == "shelf" or (not self.clients[name].busy and name not in reserved)

        waiting = [(substrate, source) for substrate, source in self._waiting.values() if substrate is not exclude]
        for substrate, source in waiting:
            destination = substrate.next_experiment or "shelf"
            if free(destination):
                return substrate, source, destination
        # Break cycles where every finished sample's next client is occupied.
        for substrate, source in waiting:
            if source != "shelf":
                return substrate, source, "shelf"
        for substrate in self.substrates:
            destination = substrate.next_experiment
            if substrate is exclude or substrate.location != "shelf" or destination is None:
                continue
            if free(destination):
                return substrate, "shelf", destination
        return None

    def _schedule_next_transport(self) -> None:
        choice = self._next_choice()
        if choice is not None:
            self._begin_transport(*choice, "pickup")

    def plan(self, event_limit: int = 12) -> dict[str, Any]:
        """What the scheduler is doing and plans to do, for the GUI and GET /plan.

        ``upcoming`` lists every substrate waiting for the gantry in the order
        it will be considered; ``next`` marks the one it takes when the
        current move ends (or now, if the gantry is free), assuming nothing
        else finishes first.
        """
        transport = self._transport
        current = None
        if transport is not None:
            current = {
                "kind": "transport",
                "id": transport.substrate.substrate_id,
                "slot": transport.substrate.slot,
                "phase": transport.phase,
                "from": transport.source,
                "to": transport.destination,
                "remaining": max(0.0, transport.arrives_at - self.now),
            }
        elif self._manual_move is not None:
            current = {"kind": "manual", "to": list(self._manual_move.end),
                       "remaining": max(0.0, self._manual_move.arrives_at - self.now)}
        exclude = transport.substrate if transport is not None else None
        reserved = frozenset({transport.destination}) if transport is not None else frozenset()
        choice = None if self.stopped else self._next_choice(exclude, reserved)

        def busy_reason(name: str) -> Optional[str]:
            if name == "shelf":
                return None
            client = self.clients[name]
            if name in reserved:
                return f"{name} is about to get {exclude.substrate_id}"
            if client.substrate is not None:
                return f"{name} has {client.substrate.substrate_id}"
            if client.busy:
                state = getattr(client, "status", {}).get("state", "busy")
                return f"{name} is {state}"
            return None

        upcoming = []
        candidates = [(substrate, source) for substrate, source in self._waiting.values() if substrate is not exclude]
        candidates += [
            (substrate, "shelf") for substrate in self.substrates
            if substrate is not exclude and substrate.location == "shelf"
            and substrate.next_experiment is not None and substrate.substrate_id not in self._waiting
        ]
        for substrate, source in candidates:
            destination = substrate.next_experiment or "shelf"
            is_next = choice is not None and choice[0] is substrate
            if is_next:
                destination = choice[2]
            reason = None if is_next else busy_reason(destination)
            if is_next and destination == "shelf" and substrate.next_experiment is not None:
                reason = f"parked on the shelf: {busy_reason(substrate.next_experiment)}"
            upcoming.append({
                "id": substrate.substrate_id,
                "slot": substrate.slot,
                "from": source,
                "to": destination,
                "next": is_next,
                "blocked": reason if not is_next else None,
                "note": reason if is_next else None,
                "remaining_steps": len(substrate.experiment_order) - substrate.current_experiment_index,
            })
        upcoming.sort(key=lambda item: not item["next"])
        instruments = [
            {
                "name": name,
                "sample_id": client.substrate.substrate_id if client.substrate else None,
                "waiting_for_pickup": client.substrate is not None and client.substrate.substrate_id in self._waiting,
            }
            for name, client in self.clients.items()
        ]
        return {
            "state": self.state,
            "now": self.now,
            "current": current,
            "instruments": instruments,
            "upcoming": upcoming,
            "recent": [
                {"t": t, "kind": kind, "text": describe_event(kind, detail)}
                for t, kind, detail in reversed(self.events[-event_limit:])
            ],
        }

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
        self._transport.ticket = self._hand_command("pick", source, substrate)
        self.events.append((self.now, "gantry_departed", f"{source}->{destination}:{substrate.substrate_id}"))

    def _finish_transport(self) -> None:
        """The gantry has finished the current pick or place: tell the station, then go on."""
        transport = self._transport
        assert transport is not None
        substrate = transport.substrate

        if transport.phase == "pickup":
            self.position = transport.source
            client = self.clients.get(transport.source)
            if client is not None and client.substrate is substrate:
                try:
                    client.release(self.now)
                except RuntimeError as error:
                    # The gantry holds the sample anyway; the node stays occupied until cleared.
                    client.substrate = None
                    self.events.append((self.now, "taken_failed", str(error)))
            self._waiting.pop(substrate.substrate_id, None)
            substrate.location = "gantry"
            transport.phase = "delivery"
            transport.arrives_at = self.now + self.movement_time
            transport.ticket = self._hand_command("place", transport.destination, substrate)
            self.events.append((self.now, "gantry_picked_up", substrate.substrate_id))
            return

        self.position = transport.destination
        if transport.destination == "shelf":
            if substrate.next_experiment is None:
                substrate.mark_returned_to_shelf()
            else:
                substrate.location = "shelf"
                self._waiting[substrate.substrate_id] = (substrate, "shelf")
        else:
            client = self.clients[transport.destination]
            try:
                client.start(substrate, self.now)
            except RuntimeError as error:
                # The node would not take the sample, so pick it back up and carry it to the shelf.
                self.events.append((self.now, "delivery_failed", str(error)))
                transport.source = transport.destination
                transport.destination = "shelf"
                transport.phase = "pickup"
                transport.arrives_at = self.now + self.movement_time
                transport.ticket = self._hand_command("pick", transport.source, substrate)
                return
            substrate.mark_delivered(transport.destination)
        self.events.append((self.now, "gantry_delivered", f"{substrate.substrate_id}->{transport.destination}"))
        self._transport = None
