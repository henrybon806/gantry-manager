"""Run the gantry scheduler without the GUI.

Start the experiment nodes first (each in its own terminal, see sim/),
then, from the project folder:

    python -m gantry        # same as python main.py --no-gui

Everything comes from data/presets.json (saved from the web GUI): the gantry
node's address, home, move time, shelf location and slots, each station's node
address and location, and the sample routes. Any flag overrides its preset:

    python -m gantry --port 8200 --gantry 127.0.0.1:8100 --home 0,0 --shelf 300,500 \\
        --node A=8001@1050,750 --node B=8002@1800,250 --node C=8003@2550,650 \\
        --route A,B,C --route C,A --route B,C

``@x,y`` is where each station sits on the bed, in mm from the bottom-left corner. The gantry starts at its home,
its own spot, and travels to the shelf like any other location.

The scheduler drives the nodes over HTTP (POST /start, POST /taken), sends the
gantry node move, pick and place (assuming they work; see gantry/link.py), and
serves its own state on --port (default 8200):

    GET /pos       current gantry position {"x", "y", "z", "moving", "at"}
    POST /pos      {"x": .., "y": ..} moves the idle gantry there; 409 while it moves
    GET /position  same as /pos (the labnode name, used by the GUI's Lab mode)
    GET /state     scheduler snapshot: samples, queue, transport, events
    GET /status    short summary
"""

from __future__ import annotations

import argparse
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from gantry.link import GantryLink
from gantry.scheduler import HOME, GantryManager, Point, RemoteExperiment, parse_route
from gantry.substrates import Substrate
from helpers.presets import PRESETS_PATH, load_presets, station_bed_to_raw
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
    routes: list[str],
    movement_time: float,
    home: "str | Point" = DEFAULT_HOME,
    shelf: "str | Point" = DEFAULT_SHELF,
    shelf_slots: Optional[int] = None,
    gantry: Optional[str] = None,
) -> GantryManager:
    """``nodes`` are ``(name, address, (x, y))`` tuples or ``"A=8001@35,45"`` strings.

    ``gantry`` is the gantry node's address; empty or None only simulates it.
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
        for index, route in enumerate(routes)
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
        "routes": presets["routes"],
        "movement_time": presets["move_time"],
        "home": station_bed_to_raw(presets, presets["gantry_home"]),
        "shelf": station_bed_to_raw(presets, [shelf["x"], shelf["y"]]),
        "shelf_slots": presets["shelf_slots"],
        "gantry": presets["gantry_address"],
    }


class MoveRequest(BaseModel):
    x: float
    y: float


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

    def node_statuses(self) -> dict[str, dict[str, Any]]:
        """Each node's latest /status as the scheduler last read it."""
        with self.lock:
            return {name: dict(client.status) for name, client in self.manager.clients.items()}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            state = self.manager.snapshot()
            state["clients"] = {
                name: {**state["clients"][name], "state": client.status.get("state")}
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
    app = FastAPI(title="gantry scheduler")
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
            "state": "done" if state["completed"] else "busy",
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
    parser.add_argument("--route", action="append", help="one sample's route, e.g. A,B,C; repeat per sample (replaces the preset routes)")
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
