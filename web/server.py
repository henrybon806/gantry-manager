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
    GET  /api/snapshot         latest station statuses and gantry reading
    GET  /api/gantry/stream    main.py only: the gantry's position as it changes (Server-Sent Events)
    POST /api/move             {"x": .., "y": ..} in gantry coordinates -> gantry POST /pos
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from gantry.scheduler import NOT_STARTED
from helpers.polling import NodePoller
from helpers.presets import DATA_DIR, LAYOUT_PATH, PRESETS_PATH, load_presets, poll_targets, save_presets, validate_presets
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
        self._presets = load_presets(path, legacy_path)
        self._saved = self._presets if path.exists() else None
        self._retarget(self._presets)

    def _retarget(self, presets: dict[str, Any]) -> None:
        if self.poller is not None:
            self.poller.set_targets(*poll_targets(presets))

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
    def unsaved(self) -> bool:
        with self._lock:
            return self._presets != self._saved

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._presets

    def replace(self, data: Any) -> dict[str, Any]:
        presets = validate_presets(data)
        with self._lock:
            self._presets = presets
            self._retarget(presets)
        return presets

    def save(self) -> dict[str, Any]:
        with self._lock:
            save_presets(self._presets, self.path)
            self._saved = self._presets
            return self._presets

    def reload(self) -> dict[str, Any]:
        presets = load_presets(self.path, legacy_path=None)
        with self._lock:
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

    app = FastAPI(title="gantry web GUI")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(PAGE_PATH, headers={"Cache-Control": "no-store"})

    try:
        file_label = str(store.path.resolve().relative_to(DATA_DIR.parent))
    except ValueError:
        file_label = str(store.path)

    def presets_reply(presets: dict[str, Any]) -> dict[str, Any]:
        return {"presets": presets, "unsaved": store.unsaved, "file": file_label}

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
        statuses, (payload, problem, detail) = store.readings()
        return {
            "statuses": statuses,
            "gantry": {"payload": payload, "problem": problem, "detail": detail},
            "gantry_link": store.gantry_link(),
            "unsaved": store.unsaved,
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
