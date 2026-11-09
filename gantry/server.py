"""Run the gantry scheduler without the GUI.

Start the experiment nodes first (each in its own terminal, see sim/),
then, from the project folder:

    python -m gantry        # same as python main.py --no-gui

Everything comes from data/presets.json (saved from the web GUI): the gantry
node's address, home, move time, shelf location and slots, and each station's
node address and location. Any flag overrides its preset:

    python -m gantry --port 8200 --gantry 127.0.0.1:8100 --home 0,0 --shelf 300,500 \\
        --node A=8001@1050,750 --node B=8002@1800,250 --node C=8003@2550,650 \\
        --route A,B,C --route C,A --route B,C

``@x,y`` is where each station sits on the bed, in mm from the bottom-left corner. The gantry starts at its home,
its own spot, and travels to the shelf like any other location.

The scheduler is always on. Substrates (each with a route) are added to
shelf slots through the API below whenever they arrive, and run as soon as
their instruments are free; --route adds some at startup.

The scheduler drives the nodes over HTTP (POST /start, POST /taken), sends the
gantry node move, pick and place (assuming they work; see gantry/link.py), and
serves its own state on --port (default 8200):

    GET /pos       current gantry position {"x", "y", "z", "moving", "at"}
    POST /pos      {"x": .., "y": ..} moves the idle gantry there; 409 while it moves
    GET /position  same as /pos (the labnode name, used by the GUI's Lab mode)
    GET /state     scheduler snapshot: samples, queue, transport, events
    GET /status    short summary

    GET    /shelf               every slot: empty, or its substrate and what it is doing
    GET    /plan                the current move, each instrument's sample, and the upcoming moves
    POST   /substrates          {"route": ["A", "B"], "id": opt, "slot": opt, "metadata": opt}
    DELETE /shelf/{slot}        clear one slot (its substrate leaves the queue and the view)
    DELETE /shelf               clear every slot whose substrate is on the shelf
    POST   /stop                emergency stop: halt scheduling, /stop the gantry and busy instruments
    POST   /resume              carry on after a stop
    POST   /experiments/stations  {"stations": [{"name", "address", "point"}]} make the experiment list match
    POST   /experiments/rename  {"name": "experiment_a", "new_name": "keyence"} rename it without restarting
    POST   /experiments/start   {"name": "experiment_a", "sample_id": opt} run that node by hand
    POST   /experiments/stop    {"name": "experiment_a"} stop that node's run
    POST   /experiments/reset   {"name": "experiment_a"} POST /taken on it: out of error or stopped, back to idle

A route is a list of experiments (letters or station names) or "A,B,C".
Slots are numbered from 1; without one, a substrate takes the first free
slot. Without an id it gets the next free sample_N.
"""

from __future__ import annotations

import argparse
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from gantry.link import GantryLink
from gantry.scheduler import ExperimentClient, HOME, GantryManager, NODE_ACTIONS, Point, RemoteExperiment, parse_route
from gantry.substrates import Substrate
from helpers.presets import PRESETS_PATH, load_presets, station_bed_to_raw
from labnode.client import normalize_url
from labnode.server import LOOPBACK_HOSTS, TOKEN_ENV


# No network calls happen in a tick (listeners do the reading), so it can run often.
TICK_SECONDS = 0.02
DEFAULT_PORT = 8200
DEFAULT_HOME = "0,0"
DEFAULT_SHELF = "300,500"


def parse_point(text: str) -> Point:
    try:
        x, y = (float(part) for part in text.split(","))
    except ValueError:
        raise ValueError(f"expected x,y, not {text!r}") from None
    return x, y


def parse_node(entry: str) -> tuple[str, str, Point]:
    """``"A=8001@35,45"`` -> ``("experiment_a", "8001", (35.0, 45.0))``."""
    letter, sep, rest = entry.partition("=")
    address, at, point = rest.partition("@")
    if not sep or not at or not letter.strip() or not address.strip():
        raise ValueError(f"--node must look like A=8001@35,45, not {entry!r}")
    return f"experiment_{letter.strip().lower()}", address.strip(), parse_point(point)


def build_manager(
    nodes: list,
    routes: Optional[list[str]],
    movement_time: float,
    home: "str | Point" = DEFAULT_HOME,
    shelf: "str | Point" = DEFAULT_SHELF,
    shelf_slots: Optional[int] = None,
    gantry: Optional[str] = None,
) -> GantryManager:
    """``nodes`` are ``(name, address, (x, y))`` tuples or ``"A=8001@35,45"`` strings.

    ``routes`` (one per substrate) fill shelf slots 1, 2, ... straight away;
    more can be added any time with POST /substrates. ``gantry`` is the gantry
    node's address; empty or None only simulates it.
    """
    token = os.environ.get(TOKEN_ENV) or None
    clients = []
    locations = {
        HOME: parse_point(home) if isinstance(home, str) else home,
        "shelf": parse_point(shelf) if isinstance(shelf, str) else shelf,
    }
    for node in nodes:
        name, address, point = parse_node(node) if isinstance(node, str) else node
        clients.append(RemoteExperiment(name, address, token=token))
        locations[name] = point
    known = {client.name for client in clients}
    substrates = [
        Substrate(f"sample_{index + 1}", parse_route(route, known))
        for index, route in enumerate(routes or [])
    ]
    link = GantryLink(gantry.strip(), token=token) if gantry and gantry.strip() else None
    return GantryManager(
        clients, substrates, movement_time=movement_time, locations=locations, shelf_slots=shelf_slots,
        gantry=link,
    )


def settings_from_presets(presets: dict[str, Any]) -> dict[str, Any]:
    """build_manager arguments from presets.json.

    Presets hold bed coordinates; the gantry works in its own, so they go
    through the saved HOME/REF calibration.
    """
    shelf = next(station for station in presets["stations"] if station["name"] == "shelf")
    nodes = [
        (station["name"], station["address"], station_bed_to_raw(presets, [station["x"], station["y"]]))
        for station in presets["stations"]
        if station["name"] != "shelf" and station["address"]
    ]
    return {
        "nodes": nodes,
        "routes": None,
        "movement_time": presets["move_time"],
        "home": station_bed_to_raw(presets, presets["gantry_home"]),
        "shelf": station_bed_to_raw(presets, [shelf["x"], shelf["y"]]),
        "shelf_slots": presets["shelf_slots"],
        "gantry": presets["gantry_address"],
    }


class MoveRequest(BaseModel):
    x: float
    y: float


class StationSpec(BaseModel):
    """One experiment the running scheduler should have."""

    name: str
    address: str = Field(description="Its labnode address; an experiment without one is not scheduled")
    point: Optional[list[float]] = Field(None, description="Where it sits, in gantry coordinates")


class StationsRequest(BaseModel):
    stations: list[StationSpec] = Field(description="The experiments the scheduler should have, in full")


class RenameRequest(BaseModel):
    """Which experiment to rename, and to what."""

    name: str = Field(description="The experiment's current name")
    new_name: str = Field(description="Its new name: unique, no comma, and not home or shelf")


class NodeActionRequest(BaseModel):
    """Which experiment node to drive by hand."""

    name: str = Field(description="The experiment's name, e.g. experiment_a")
    sample_id: Optional[str] = Field(
        None, description='For start: the sample to run. Without one, the node\'s current sample, else "manual"')


class SubstrateSpec(BaseModel):
    """One substrate to put on the shelf."""

    model_config = {"json_schema_extra": {"examples": [
        {"route": ["A", "B", "C"]},
        {"id": "wafer_12", "route": "C,A", "slot": 4, "metadata": {"notes": "batch 3"}},
    ]}}

    route: "list[str] | str" = Field(description='Experiments in order: letters or station names, a list or "A,B,C"')
    id: Optional[str] = Field(None, description="Unique id; without one it gets the next free sample_N")
    slot: Optional[int] = Field(None, description="Shelf slot from 1; without one, the first free slot")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Anything to keep with the substrate")


class Scheduler:
    """Ticks the manager on wall-clock time in the background."""

    def __init__(self, manager: GantryManager) -> None:
        self.manager = manager
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="gantry-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        if self.manager.gantry is not None:
            self.manager.gantry.close()
        for client in self.manager.clients.values():
            if hasattr(client, "close"):
                client.close()

    def pos(self) -> dict[str, Any]:
        with self.lock:
            return self.manager.pos()

    def move_to(self, x: float, y: float) -> dict[str, Any]:
        with self.lock:
            self.manager.move_to((x, y))
            return self.manager.pos()

    def _substrate(self, spec: SubstrateSpec) -> Substrate:
        """A Substrate for ``spec``; without an id it becomes the next free sample_N."""
        route = spec.route if isinstance(spec.route, str) else ",".join(spec.route)
        sample_id = (spec.id or "").strip()
        if not sample_id:
            used = {substrate.substrate_id for substrate in self.manager.substrates}
            counter = 1
            while f"sample_{counter}" in used:
                counter += 1
            sample_id = f"sample_{counter}"
        return Substrate(sample_id, parse_route(route, set(self.manager.clients)), metadata=dict(spec.metadata))

    def shelf(self) -> dict[str, Any]:
        with self.lock:
            return self.manager.shelf()

    def plan(self) -> dict[str, Any]:
        with self.lock:
            return self.manager.plan()

    def add_substrate(self, spec: SubstrateSpec) -> dict[str, Any]:
        with self.lock:
            substrate = self._substrate(spec)
            self.manager.add_substrate(substrate, spec.slot)
            return self.manager.substrate_view(substrate)

    def clear_slot(self, slot: int) -> dict[str, Any]:
        with self.lock:
            self.manager.clear_slot(slot)
            return self.manager.shelf()

    def clear_shelf(self) -> dict[str, Any]:
        with self.lock:
            cleared, kept = self.manager.clear_shelf()
            return {**self.manager.shelf(), "cleared": cleared, "kept": kept}

    def apply_stations(self, stations: list[Any]) -> dict[str, list[str]]:
        """Make the running scheduler's experiments match ``stations`` (name, address, point).

        Adds the ones it does not have, drops the ones that are gone, builds a
        fresh client for one whose address changed, and moves the rest on the
        bed. One that is busy or still has samples routed through it is left
        alone and named in ``kept``, with why.
        """
        token = os.environ.get(TOKEN_ENV) or None
        wanted: dict[str, tuple[str, Optional[Point]]] = {}
        for station in stations:
            entry = station if isinstance(station, dict) else station.model_dump()
            point = entry.get("point")
            wanted[entry["name"]] = (entry["address"], None if point is None else (float(point[0]), float(point[1])))
        report: dict[str, list[str]] = {"added": [], "removed": [], "kept": []}
        retired = []
        with self.lock:
            for name in list(self.manager.clients):
                address = wanted.get(name, (None, None))[0]
                if address is not None and not self._address_changed(self.manager.clients[name], address):
                    continue
                try:
                    retired.append(self.manager.remove_client(name))
                except RuntimeError as error:
                    report["kept"].append(f"{name}: {error}")
                    wanted.pop(name, None)
                    continue
                report["removed"].append(name)
            for name, (address, point) in wanted.items():
                if name in self.manager.clients:
                    if point is not None:
                        self.manager.move_client(name, point)
                    continue
                try:
                    self.manager.add_client(RemoteExperiment(name, address, token=token), point)
                except (RuntimeError, ValueError) as error:
                    report["kept"].append(f"{name}: {error}")
                    continue
                report["added"].append(name)
        for client in retired:
            client.close()
        return report

    @staticmethod
    def _address_changed(client: Any, address: str) -> bool:
        """True when ``client`` is no longer pointed at ``address`` (so it has to be rebuilt)."""
        current = getattr(client, "url", None)
        if current is None:
            return False
        try:
            return normalize_url(address) != current
        except ValueError:
            return True

    def rename_client(self, name: str, new_name: str) -> dict[str, Any]:
        """Rename one experiment in the running scheduler; ``renamed`` is False if it has no such client."""
        with self.lock:
            renamed = self.manager.rename_client(name, new_name)
        return {"from": name, "to": new_name, "renamed": renamed, "experiments": list(self.manager.clients)}

    def node_action(self, name: str, action: str, sample_id: Optional[str] = None) -> dict[str, Any]:
        """Start, stop or reset one experiment node by hand; returns its new /status."""
        with self.lock:
            status = self.manager.node_action(name, action, sample_id)
        return {"name": name, "action": action, "state": status.get("state"), "status": status}

    def resume(self) -> dict[str, Any]:
        with self.lock:
            self.manager.resume()
            return self.manager.shelf()

    def emergency_stop(self) -> dict[str, Any]:
        """Emergency stop: halt scheduling, then /stop the gantry and busy instruments."""
        with self.lock:
            busy = self.manager.emergency_stop()
            if self.manager.gantry is not None:
                self.manager.gantry.stop_now()
            clients = [self.manager.clients[name] for name in busy]
            shelf = self.manager.shelf()
        for client in clients:
            if isinstance(client, ExperimentClient):
                continue
            threading.Thread(target=self._stop_node, args=(client,), name=f"stop {client.name}", daemon=True).start()
        return shelf

    def _stop_node(self, client: RemoteExperiment) -> None:
        try:
            client.node.stop()
            outcome = ("node_stopped", client.name)
        except Exception as error:
            outcome = ("node_stop_failed", f"{client.name}: {error}")
        with self.lock:
            self.manager.events.append((self.manager.now, *outcome))

    def _status(self, client: Any) -> dict[str, Any]:
        # Remote clients keep their node's last /status; in-memory ones only have a state.
        if isinstance(client, ExperimentClient):
            return {"state": client.poll(self.manager.now)}
        return dict(client.status)

    def node_statuses(self) -> dict[str, dict[str, Any]]:
        """Each node's latest /status as the scheduler last read it."""
        with self.lock:
            return {name: self._status(client) for name, client in self.manager.clients.items()}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            state = self.manager.snapshot()
            state["clients"] = {
                name: {**state["clients"][name], "state": self._status(client).get("state")}
                for name, client in self.manager.clients.items()
            }
            return state

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self.lock:
                    self.manager.tick(time.monotonic() - self._started)
            except Exception:
                traceback.print_exc()
            self._stop.wait(TICK_SECONDS)


def create_app(scheduler: Scheduler, token: Optional[str] = None):
    """With ``token`` set, POST endpoints need ``Authorization: Bearer <token>``."""
    import secrets

    from fastapi import Depends, FastAPI, HTTPException
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    app = FastAPI(
        title="gantry scheduler",
        description="Always-on scheduler: POST /substrates to add one to the shelf; it runs as soon as "
                    "its instruments are free. Interactive docs at /docs.",
    )
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
        if token is None:
            return
        if credentials is None or not secrets.compare_digest(credentials.credentials, token):
            raise HTTPException(status_code=401, detail="missing or wrong bearer token")

    @app.get("/pos", summary="Current gantry position on the bed")
    def get_pos() -> dict[str, Any]:
        return scheduler.pos()

    @app.post("/pos", status_code=202, dependencies=[Depends(authorize)],
              summary="Move the idle gantry to x, y; 409 while it is moving")
    def post_pos(request: MoveRequest) -> dict[str, Any]:
        try:
            return scheduler.move_to(request.x, request.y)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    def call(action, *args) -> dict[str, Any]:
        try:
            return action(*args)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/shelf", summary="Every shelf slot: empty, or its substrate and what it is doing")
    def get_shelf() -> dict[str, Any]:
        return scheduler.shelf()

    @app.get("/plan", summary="What the scheduler is doing now, what each instrument holds, and what it plans next")
    def get_plan() -> dict[str, Any]:
        return scheduler.plan()

    @app.post("/substrates", status_code=201, dependencies=[Depends(authorize)],
              summary="Add a substrate to a slot (or the first free one); it runs as soon as it can")
    def post_substrate(request: SubstrateSpec) -> dict[str, Any]:
        return call(scheduler.add_substrate, request)

    @app.delete("/shelf/{slot}", dependencies=[Depends(authorize)],
                summary="Clear one slot; 409 while its substrate is out at an instrument or moving")
    def delete_slot(slot: int) -> dict[str, Any]:
        return call(scheduler.clear_slot, slot)

    @app.delete("/shelf", dependencies=[Depends(authorize)],
                summary="Clear every slot whose substrate is on the shelf")
    def delete_shelf() -> dict[str, Any]:
        return call(scheduler.clear_shelf)

    @app.post("/stop", dependencies=[Depends(authorize)],
              summary="Emergency stop: halt scheduling and /stop the gantry and busy instruments")
    def post_stop() -> dict[str, Any]:
        return call(scheduler.emergency_stop)

    # Declared before /experiments/{action} so these are not read as actions.
    @app.post("/experiments/stations", dependencies=[Depends(authorize)],
              summary="Make the running scheduler's experiments match this list: add, drop, re-point, move")
    def post_experiment_stations(request: StationsRequest) -> dict[str, list[str]]:
        return call(scheduler.apply_stations, request.stations)


    @app.post("/experiments/rename", dependencies=[Depends(authorize)],
              summary="Rename one experiment in the running scheduler, routes and all")
    def post_experiment_rename(request: RenameRequest) -> dict[str, Any]:
        return call(scheduler.rename_client, request.name, request.new_name)

    @app.post("/experiments/{action}", dependencies=[Depends(authorize)],
              summary="Drive one experiment node by hand: start a run, stop it, or reset it with /taken")
    def post_experiment_action(action: Literal[NODE_ACTIONS], request: NodeActionRequest) -> dict[str, Any]:
        return call(scheduler.node_action, request.name, action, request.sample_id)

    @app.post("/resume", dependencies=[Depends(authorize)], summary="Carry on scheduling after a stop")
    def post_resume() -> dict[str, Any]:
        return call(scheduler.resume)

    @app.get("/position", summary="Same as /pos (labnode name)")
    def get_position() -> dict[str, Any]:
        return scheduler.pos()

    @app.get("/state", summary="Full scheduler snapshot for the GUI")
    def get_state() -> dict[str, Any]:
        return scheduler.snapshot()

    @app.get("/status", summary="Short summary")
    def get_status() -> dict[str, Any]:
        state = scheduler.snapshot()
        return {
            "name": "gantry",
            "state": state["state"],
            "shelf": {key: value for key, value in state["shelf"].items() if key != "shelf"},
            "now": state["now"],
            "position": state["position"],
            "clients": state["clients"],
        }

    return app


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The gantry's flags, shared with main.py."""
    parser.add_argument("--presets", type=Path, default=PRESETS_PATH, help="presets file (default data/presets.json)")
    parser.add_argument("--port", type=int, help=f"scheduler API port without the GUI (default {DEFAULT_PORT})")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--node", action="append", help="LETTER=address@x,y, repeat per node (replaces the preset stations)")
    parser.add_argument("--home", help="gantry home x,y")
    parser.add_argument("--shelf", help="shelf x,y")
    parser.add_argument("--slots", type=int, help="shelf slots")
    parser.add_argument("--route", action="append", help="add a substrate at startup: its route, e.g. A,B,C; repeat per substrate")
    parser.add_argument("--move-time", type=float, help="seconds per gantry leg")
    parser.add_argument("--gantry", help="gantry node address for move/pick/place; \"\" to only simulate it")


def scheduler_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Scheduler, int, Optional[str]]:
    """Build the (not yet started) scheduler, its port and token from presets and flags."""
    try:
        settings = settings_from_presets(load_presets(args.presets))
        if args.node:
            settings["nodes"] = [parse_node(entry) for entry in args.node]
        if args.route:
            settings["routes"] = args.route
        for key, value in (("home", args.home), ("shelf", args.shelf)):
            if value is not None:
                settings[key] = parse_point(value)
        if args.slots is not None:
            settings["shelf_slots"] = args.slots
        if args.move_time is not None:
            settings["movement_time"] = args.move_time
        if args.gantry is not None:
            settings["gantry"] = args.gantry
        manager = build_manager(**settings)
    except ValueError as error:
        parser.error(str(error))

    token = os.environ.get(TOKEN_ENV) or None
    if token is None and args.host not in LOOPBACK_HOSTS:
        parser.error(f"set {TOKEN_ENV} before listening on {args.host}")
    return Scheduler(manager), args.port or DEFAULT_PORT, token


def describe(scheduler: Scheduler, state_url: str) -> str:
    manager = scheduler.manager
    stations = ", ".join(f"{name} {client.url}" for name, client in manager.clients.items())
    gantry = manager.gantry.address if manager.gantry is not None else "simulated only"
    return (f"scheduler on {state_url}  |  gantry {gantry}  |  {len(manager.substrates)} samples, "
            f"{manager.shelf_slots} shelf slots  |  {stations}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m gantry", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(parser)
    args = parser.parse_args()
    scheduler, port, token = scheduler_from_args(parser, args)

    import uvicorn

    scheduler.start()
    print(describe(scheduler, f"http://{args.host}:{port}/state"), flush=True)
    try:
        uvicorn.run(create_app(scheduler, token), host=args.host, port=port, access_log=False)
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
