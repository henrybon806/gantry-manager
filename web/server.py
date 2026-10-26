"""Locally hosted web GUI for the gantry bed.

    python main.py           # GUI and scheduler together; then open http://127.0.0.1:8080
    python -m web            # GUI only, to watch a gantry running elsewhere

Start the experiments in their own terminals first (or after; the page shows
NOT STARTED until they answer). This server polls them
on the page's behalf, because browsers cannot read the nodes directly, and
forwards gantry moves to POST /pos.

    GET  /                     the page (web/static/index.html)
    GET  /api/presets          current settings: bed, gantry, shelf, stations, routes
    PUT  /api/presets          change the current settings (not yet written to disk)
    POST /api/presets/save     write the current settings to data/presets.json
    POST /api/presets/reload   throw away unsaved changes and re-read data/presets.json
    GET  /api/snapshot         latest station statuses and gantry reading
    POST /api/move             {"x": .., "y": ..} in gantry coordinates -> gantry POST /pos
"""

from __future__ import annotations

import argparse
import os
import threading
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from helpers.polling import NodePoller
from helpers.presets import DATA_DIR, LAYOUT_PATH, PRESETS_PATH, load_presets, poll_targets, save_presets, validate_presets
from labnode import NodeClient, NodeError, NodeUnreachable
from labnode.server import TOKEN_ENV


PAGE_PATH = Path(__file__).with_name("static") / "index.html"


class MoveRequest(BaseModel):
    x: float
    y: float


class PresetsStore:
    """The settings being edited, kept in step with the poller.

    Edits stay in memory (shared by every open tab) until ``save`` writes
    presets.json. ``unsaved`` says whether they differ from the file.
    """

    def __init__(self, poller: NodePoller, path: Path = PRESETS_PATH, legacy_path: Optional[Path] = LAYOUT_PATH) -> None:
        self.poller = poller
        self.path = path
        self._lock = threading.Lock()
        self._presets = load_presets(path, legacy_path)
        self._saved = self._presets if path.exists() else None
        self.poller.set_targets(*poll_targets(self._presets))

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
            self.poller.set_targets(*poll_targets(presets))
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
            self.poller.set_targets(*poll_targets(presets))
        return presets


def move_gantry(address: str, x: float, y: float) -> dict[str, Any]:
    """Send POST /pos to the gantry and describe the outcome for the page."""
    if not address:
        return {"ok": False, "message": "No gantry address set"}
    try:
        client = NodeClient(address, token=os.environ.get(TOKEN_ENV) or None, timeout=2.0)
        client._request("POST", "/pos", {"x": x, "y": y})
    except NodeUnreachable:
        return {"ok": False, "message": "Gantry NOT STARTED: nothing at that address"}
    except NodeError as error:
        return {"ok": False, "message": f"Refused: {error.detail}"}
    except ValueError as error:
        return {"ok": False, "message": f"UNABLE TO READ: {error}"}
    return {"ok": True, "message": "Moving"}


def create_app(store: PresetsStore):
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import FileResponse

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
        statuses, (payload, problem, detail) = store.poller.snapshot()
        return {
            "statuses": statuses,
            "gantry": {"payload": payload, "problem": problem, "detail": detail},
            "unsaved": store.unsaved,
        }

    @app.post("/api/move")
    def post_move(request: MoveRequest) -> dict[str, Any]:
        return move_gantry(store.get()["gantry_address"], request.x, request.y)

    return app


def store_from_args(parser: argparse.ArgumentParser, presets: Path) -> PresetsStore:
    poller = NodePoller()
    try:
        return PresetsStore(poller, presets)
    except ValueError as error:
        poller.close()
        parser.error(str(error))


def serve(store: PresetsStore, port: int) -> None:
    """Run the GUI until Ctrl+C, shared with main.py."""
    import uvicorn

    print(f"gantry GUI on http://127.0.0.1:{port}", flush=True)
    try:
        # Loopback only: the page can move the gantry, so keep it off the network.
        uvicorn.run(create_app(store), host="127.0.0.1", port=port, access_log=False)
    finally:
        store.poller.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m web", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--presets", type=Path, default=PRESETS_PATH, help="presets file (default data/presets.json)")
    args = parser.parse_args()
    serve(store_from_args(parser, args.presets), args.port)


if __name__ == "__main__":
    main()
