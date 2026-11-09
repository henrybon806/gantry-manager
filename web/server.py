"""Locally hosted web GUI for the gantry bed.

    python main.py           # GUI and scheduler together; then open http://127.0.0.1:8080
    python -m web            # GUI only, to watch a gantry running elsewhere

Under main.py the page reads everything from the scheduler in the same
process: node statuses, the gantry and the samples, and moves go straight to
it. Alone (python -m web), this server polls the nodes and the scheduler over
HTTP on the page's behalf, because browsers cannot read them directly, and
forwards gantry moves to POST /pos.

    GET  /                     the page (web/static/index.html)
    GET  /api/presets          current settings: bed, gantry, shelf, stations, routes
    PUT  /api/presets          change the current settings (not yet written to disk)
    POST /api/presets/save     write the current settings to data/presets.json
    POST /api/presets/reload   throw away unsaved changes and re-read data/presets.json
    GET  /api/snapshot         latest station statuses, gantry reading and shelf slots
    GET  /api/gantry/stream    main.py only: the gantry's position as it changes (Server-Sent Events)
    POST /api/move             {"x": .., "y": ..} in gantry coordinates -> gantry POST /pos
    POST /api/substrates       {"route", "id", "slot", "metadata"} -> scheduler POST /substrates
    DELETE /api/shelf/{slot}   -> scheduler DELETE /shelf/{slot}
    DELETE /api/shelf          -> scheduler DELETE /shelf
    POST /api/stop, /api/resume -> scheduler POST /stop, /resume
    POST /api/experiments/{start|stop|reset}  {"name", "sample_id"} -> scheduler POST /experiments/{action}
    POST /api/experiments/rename  {"name", "new_name"} rename it here and in the running scheduler

Adding or removing an experiment in the settings reaches the running scheduler
too: every change to them is sent on as scheduler POST /experiments/stations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ValidationError

from gantry.scheduler import NODE_ACTIONS, NOT_STARTED
from gantry.server import NodeActionRequest, RenameRequest, SubstrateSpec
from helpers.polling import NodePoller, fetch
from helpers.presets import (DATA_DIR, LAYOUT_PATH, PRESETS_PATH, load_presets, poll_targets, save_presets,
                             station_bed_to_raw, validate_presets)
from labnode import NodeClient, NodeError, NodeUnreachable
from labnode.server import TOKEN_ENV


PAGE_PATH = Path(__file__).with_name("static") / "index.html"
# How often the stream checks for a new gantry position, and how often it
# sends a keep-alive when nothing changed (so closed tabs are noticed).
STREAM_INTERVAL = 0.02
STREAM_KEEPALIVE = 1.0


class MoveRequest(BaseModel):
    x: float
    y: float


class PresetsStore:
    """The settings being edited, and where the page's live readings come from.

    Edits stay in memory (shared by every open tab) until ``save`` writes
    presets.json. ``unsaved`` says whether they differ from the file.

    With ``scheduler`` (main.py), every reading comes from the scheduler in
    this process. Without it (python -m web), ``poller`` reads the nodes and
    the scheduler over HTTP.
    """

    def __init__(
        self,
        poller: Optional[NodePoller],
        path: Path = PRESETS_PATH,
        legacy_path: Optional[Path] = LAYOUT_PATH,
        scheduler: Optional[Any] = None,
    ) -> None:
        self.poller = poller
        self.scheduler = scheduler
        self.path = path
        self._lock = threading.Lock()
        # Bumped on every change, so an open page can tell its copy is stale.
        self._revision = 0
        # What the last settings change did to the scheduler's experiments.
        self.stations: dict[str, list[str]] = {"added": [], "removed": [], "kept": []}
        self._retry_at = 0.0
        self._presets = load_presets(path, legacy_path)
        self._saved = self._presets if path.exists() else None
        self._retarget(self._presets)

    def _retarget(self, presets: dict[str, Any]) -> None:
        if self.poller is not None:
            self.poller.set_targets(*poll_targets(presets))

    def sync_stations(self, presets: dict[str, Any]) -> dict[str, list[str]]:
        """Make the scheduler's experiments match the settings: added, removed, moved.

        The scheduler works in gantry coordinates, so each spot goes through
        the saved calibration first. ``kept`` names any experiment it would not
        let go of yet (busy, or samples still routed through it).
        """
        stations = [
            {"name": station["name"], "address": station["address"],
             "point": list(station_bed_to_raw(presets, [station["x"], station["y"]]))}
            for station in presets["stations"]
            if station["name"] != "shelf" and station["address"]
        ]
        reply = self.shelf_action("POST", "/experiments/stations", {"stations": stations})
        if not reply["ok"]:
            return {"added": [], "removed": [], "kept": [reply["message"]] if self.scheduler is not None else []}
        return reply["result"]

    def readings(self) -> tuple[dict[str, dict[str, Any]], tuple[Any, str, str]]:
        """``(statuses by station, (gantry payload, problem, detail))`` for the page."""
        if self.scheduler is None:
            return self.poller.snapshot()
        presets = self.get()
        statuses = self.scheduler.node_statuses()
        for station in presets["stations"]:
            name = station["name"]
            if name != "shelf" and station["address"] and name not in statuses:
                statuses[name] = {"state": NOT_STARTED, "error": "not in the running scheduler; restart main.py to add it"}
        payload = self.scheduler.pos()
        if payload["x"] is None:
            return statuses, (None, payload["problem"], payload["detail"])
        return statuses, (payload, "", "")

    def shelf(self) -> dict[str, Any]:
        """The scheduler's shelf slots, or ``{"problem": ...}`` when it cannot be read."""
        if self.scheduler is not None:
            return self.scheduler.shelf()
        address = self.get()["scheduler_address"]
        if not address:
            return {"problem": "No scheduler address set"}
        payload, problem, detail = fetch(address, "/shelf")
        if problem:
            label = "Scheduler NOT STARTED" if problem == NOT_STARTED else "Scheduler UNABLE TO READ"
            return {"problem": f"{label}: {detail}"}
        return payload

    def plan(self) -> Optional[dict[str, Any]]:
        """The scheduler's plan, or None when it cannot be read (the shelf says why)."""
        if self.scheduler is not None:
            return self.scheduler.plan()
        address = self.get()["scheduler_address"]
        payload, problem, _ = fetch(address, "/plan") if address else (None, "x", "")
        return None if problem else payload

    def shelf_action(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        """Add, clear, stop or resume on the scheduler; ``{"ok", "message", "result"}`` for the page."""
        if self.scheduler is not None:
            return scheduler_action(self.scheduler, method, path, body)
        return forward_action(self.get()["scheduler_address"], method, path, body)

    def rename_station(self, old: str, new: str) -> tuple[dict[str, Any], str]:
        """Rename a station in the presets and in the running scheduler, then save.

        Returns ``(presets, note)``; ``note`` says the scheduler could not be
        told, so it keeps the old name until it restarts. ValueError if the new
        name is not usable, RuntimeError if the scheduler refused it.
        """
        presets = self.get()
        entry = next((station for station in presets["stations"] if station["name"] == old), None)
        if entry is None:
            raise ValueError(f"unknown experiment: {old}")
        if old == "shelf":
            raise ValueError("the shelf cannot be renamed")
        candidate = validate_presets({
            **presets,
            "stations": [
                {**station, "name": new} if station["name"] == old else station
                for station in presets["stations"]
            ],
        })
        reply = self.shelf_action("POST", "/experiments/rename", {"name": old, "new_name": new})
        if not reply["ok"] and reply["message"].startswith("Refused"):
            raise RuntimeError(reply["message"])
        self.replace(candidate)
        self.save()
        note = ""
        if not reply["ok"]:
            note = f"The scheduler keeps {old} until it restarts: {reply['message']}"
        elif entry["address"] and not (reply.get("result") or {}).get("renamed"):
            note = f"The running scheduler has no {old}; it takes {new} when main.py restarts."
        return self.get(), note

    def gantry_reading(self) -> dict[str, Any]:
        """The gantry's position as the scheduler has it right now (main.py only)."""
        pos = self.scheduler.pos()
        return {key: pos[key] for key in ("x", "y", "z", "moving", "source", "problem", "detail")}

    def gantry_link(self) -> Optional[dict[str, Any]]:
        """What happened to the last command sent to the gantry node, under main.py."""
        if self.scheduler is None or self.scheduler.manager.gantry is None:
            return None
        return self.scheduler.manager.gantry.report()

    def close(self) -> None:
        if self.poller is not None:
            self.poller.close()

    @property
    def revision(self) -> int:
        """How many times the settings have changed, for pages to compare against."""
        with self._lock:
            return self._revision

    @property
    def unsaved(self) -> bool:
        with self._lock:
            return self._presets != self._saved

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._presets

    def retry_stations(self) -> None:
        """Try again, about once a second, for an experiment the scheduler would not let go of.

        Removing one that is busy or still in a route is not refused outright,
        it just waits: this is what eventually carries it out.
        """
        if not self.stations["kept"] or time.monotonic() < self._retry_at:
            return
        self._retry_at = time.monotonic() + 1.0
        self.stations = self.sync_stations(self.get())

    def replace(self, data: Any) -> dict[str, Any]:
        """Take new settings. The running scheduler's experiment list follows them."""
        presets = validate_presets(data)
        with self._lock:
            changed = presets != self._presets
            self._presets = presets
            self._revision += changed
            self._retarget(presets)
        self.stations = self.sync_stations(presets)
        return presets

    def save(self) -> dict[str, Any]:
        with self._lock:
            save_presets(self._presets, self.path)
            self._saved = self._presets
            return self._presets

    def reload(self) -> dict[str, Any]:
        presets = load_presets(self.path, legacy_path=None)
        with self._lock:
            self._revision += presets != self._presets
            self._presets = presets
            self._saved = presets if self.path.exists() else None
            self._retarget(presets)
        return presets


def move_scheduler(scheduler: Any, x: float, y: float) -> dict[str, Any]:
    """Ask the in-process scheduler to move the gantry and describe the outcome."""
    try:
        scheduler.move_to(x, y)
    except RuntimeError as error:
        return {"ok": False, "message": f"Refused: {error}"}
    except ValueError as error:
        return {"ok": False, "message": str(error)}
    return {"ok": True, "message": "Moving"}


def scheduler_action(scheduler: Any, method: str, path: str, body: Any = None) -> dict[str, Any]:
    """The in-process scheduler's side of ``shelf_action`` (main.py)."""
    try:
        if (method, path) == ("POST", "/substrates"):
            result = scheduler.add_substrate(SubstrateSpec(**body))
        elif (method, path) == ("DELETE", "/shelf"):
            result = scheduler.clear_shelf()
        elif method == "DELETE" and path.startswith("/shelf/"):
            result = scheduler.clear_slot(int(path.rsplit("/", 1)[1]))
        elif (method, path) == ("POST", "/stop"):
            result = scheduler.emergency_stop()
        elif (method, path) == ("POST", "/resume"):
            result = scheduler.resume()
        elif (method, path) == ("POST", "/experiments/stations"):
            result = scheduler.apply_stations(body["stations"])
        elif (method, path) == ("POST", "/experiments/rename"):
            result = scheduler.rename_client(body["name"], body["new_name"])
        elif method == "POST" and path.startswith("/experiments/"):
            body = body or {}
            result = scheduler.node_action(body.get("name"), path.rsplit("/", 1)[1], body.get("sample_id"))
        else:
            raise ValueError(f"unknown action {method} {path}")
    except RuntimeError as error:
        return {"ok": False, "message": f"Refused: {error}", "result": None}
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"])
        return {"ok": False, "message": f"{field}: {first['msg']}" if field else first["msg"], "result": None}
    except (ValueError, TypeError) as error:
        return {"ok": False, "message": str(error), "result": None}
    return {"ok": True, "message": "Done", "result": result}


def forward_action(address: str, method: str, path: str, body: Any = None) -> dict[str, Any]:
    """Send a shelf action to the scheduler at ``address`` (python -m web alone)."""
    if not address:
        return {"ok": False, "message": "No scheduler address set", "result": None}
    try:
        client = NodeClient(address, token=os.environ.get(TOKEN_ENV) or None, timeout=2.0)
        result = client._request(method, path, body)
    except NodeUnreachable:
        return {"ok": False, "message": "Scheduler NOT STARTED: nothing at that address", "result": None}
    except NodeError as error:
        prefix = "Refused: " if error.status == 409 else ""
        return {"ok": False, "message": f"{prefix}{error.detail}", "result": None}
    except ValueError as error:
        return {"ok": False, "message": f"UNABLE TO READ: {error}", "result": None}
    return {"ok": True, "message": "Done", "result": result}


def move_gantry(address: str, x: float, y: float) -> dict[str, Any]:
    """Send POST /pos to the scheduler at ``address`` and describe the outcome for the page."""
    if not address:
        return {"ok": False, "message": "No scheduler address set"}
    try:
        client = NodeClient(address, token=os.environ.get(TOKEN_ENV) or None, timeout=2.0)
        client._request("POST", "/pos", {"x": x, "y": y})
    except NodeUnreachable:
        return {"ok": False, "message": "Scheduler NOT STARTED: nothing at that address"}
    except NodeError as error:
        return {"ok": False, "message": f"Refused: {error.detail}"}
    except ValueError as error:
        return {"ok": False, "message": f"UNABLE TO READ: {error}"}
    return {"ok": True, "message": "Moving"}


def create_app(store: PresetsStore):
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import FileResponse, StreamingResponse

    app = FastAPI(
        title="gantry web GUI",
        description="The page's own API. Under main.py the scheduler's API (add substrates, shelf, "
                    "stop) is mounted at /scheduler, with its docs at /scheduler/docs.",
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(PAGE_PATH, headers={"Cache-Control": "no-store"})

    try:
        file_label = str(store.path.resolve().relative_to(DATA_DIR.parent))
    except ValueError:
        file_label = str(store.path)

    def presets_reply(presets: dict[str, Any]) -> dict[str, Any]:
        return {"presets": presets, "unsaved": store.unsaved, "file": file_label, "revision": store.revision,
                "stations": store.stations}

    @app.get("/api/presets")
    def get_presets() -> dict[str, Any]:
        return presets_reply(store.get())

    @app.put("/api/presets")
    def put_presets(data: Any = Body(...)) -> dict[str, Any]:
        try:
            return presets_reply(store.replace(data))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post("/api/presets/save")
    def post_save() -> dict[str, Any]:
        try:
            return presets_reply(store.save())
        except OSError as error:
            raise HTTPException(status_code=500, detail=f"could not write {store.path.name}: {error}") from None

    @app.post("/api/presets/reload")
    def post_reload() -> dict[str, Any]:
        try:
            return presets_reply(store.reload())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/api/snapshot")
    def get_snapshot() -> dict[str, Any]:
        store.retry_stations()
        statuses, (payload, problem, detail) = store.readings()
        return {
            "statuses": statuses,
            "gantry": {"payload": payload, "problem": problem, "detail": detail},
            "gantry_link": store.gantry_link(),
            "shelf": store.shelf(),
            "plan": store.plan(),
            "unsaved": store.unsaved,
            "presets_revision": store.revision,
            "stations": store.stations,
        }

    @app.get("/api/gantry/stream")
    async def gantry_stream() -> StreamingResponse:
        if store.scheduler is None:
            raise HTTPException(status_code=404, detail="the gantry stream needs main.py")

        async def events():
            last, quiet = None, 0.0
            while True:
                reading = store.gantry_reading()
                if reading != last:
                    yield f"data: {json.dumps(reading)}\n\n"
                    last, quiet = reading, 0.0
                elif quiet >= STREAM_KEEPALIVE:
                    yield ": keep-alive\n\n"
                    quiet = 0.0
                await asyncio.sleep(STREAM_INTERVAL)
                quiet += STREAM_INTERVAL

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/api/move")
    def post_move(request: MoveRequest) -> dict[str, Any]:
        if store.scheduler is not None:
            return move_scheduler(store.scheduler, request.x, request.y)
        return move_gantry(store.get()["scheduler_address"], request.x, request.y)

    @app.post("/api/substrates", summary="Add a substrate to a shelf slot (scheduler POST /substrates)")
    def post_substrate(request: SubstrateSpec) -> dict[str, Any]:
        return store.shelf_action("POST", "/substrates", request.model_dump(exclude_none=True))

    @app.delete("/api/shelf/{slot}", summary="Clear one shelf slot (scheduler DELETE /shelf/{slot})")
    def delete_slot(slot: int) -> dict[str, Any]:
        return store.shelf_action("DELETE", f"/shelf/{slot}")

    @app.delete("/api/shelf", summary="Clear every slot whose substrate is on the shelf")
    def delete_shelf() -> dict[str, Any]:
        return store.shelf_action("DELETE", "/shelf")

    @app.post("/api/stop", summary="Emergency stop (scheduler POST /stop)")
    def post_stop() -> dict[str, Any]:
        return store.shelf_action("POST", "/stop")

    @app.post("/api/experiments/rename",
              summary="Rename an experiment in the presets and in the running scheduler")
    def post_rename(request: RenameRequest) -> dict[str, Any]:
        try:
            presets, note = store.rename_station(request.name, request.new_name.strip())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except OSError as error:
            raise HTTPException(status_code=500, detail=f"could not write {store.path.name}: {error}") from None
        return {**presets_reply(presets), "note": note}

    @app.post("/api/experiments/{action}",
              summary="Start, stop or reset one experiment node by hand (scheduler POST /experiments/{action})")
    def post_experiment_action(action: Literal[NODE_ACTIONS], request: NodeActionRequest) -> dict[str, Any]:
        return store.shelf_action("POST", f"/experiments/{action}", request.model_dump())

    @app.post("/api/resume", summary="Carry on after an emergency stop (scheduler POST /resume)")
    def post_resume() -> dict[str, Any]:
        return store.shelf_action("POST", "/resume")

    return app


def store_from_args(parser: argparse.ArgumentParser, presets: Path, scheduler: Optional[Any] = None) -> PresetsStore:
    """With ``scheduler`` the page reads it directly; without one it polls over HTTP."""
    poller = None if scheduler is not None else NodePoller()
    try:
        return PresetsStore(poller, presets, scheduler=scheduler)
    except ValueError as error:
        if poller is not None:
            poller.close()
        parser.error(str(error))


def serve(store: PresetsStore, port: int, app: Optional[Any] = None) -> None:
    """Run the GUI (or ``app``, built from it) until Ctrl+C, shared with main.py."""
    import uvicorn

    print(f"gantry GUI on http://127.0.0.1:{port}", flush=True)
    try:
        # Loopback only: the page can move the gantry, so keep it off the network.
        # Open gantry streams never end on their own, so do not wait long for them on Ctrl+C.
        uvicorn.run(app or create_app(store), host="127.0.0.1", port=port, access_log=False,
                    timeout_graceful_shutdown=1)
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m web", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--presets", type=Path, default=PRESETS_PATH, help="presets file (default data/presets.json)")
    args = parser.parse_args()
    serve(store_from_args(parser, args.presets), args.port)


if __name__ == "__main__":
    main()
