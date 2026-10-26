"""Reading labnode nodes: one-off status reads and a background poller."""

from __future__ import annotations

import threading
import urllib.parse
from typing import Any, Optional

from gantry.scheduler import NOT_STARTED, UNREADABLE
from labnode import NodeClient, NodeError, NodeUnreachable
from labnode.client import normalize_url


POLL_INTERVAL = 0.2
REQUEST_TIMEOUT = 0.5


def position_from_payload(payload: dict[str, Any]) -> tuple[float, float]:
    return float(payload["x"]), float(payload["y"])


def problem_label(problem: str) -> str:
    return {NOT_STARTED: "NOT STARTED", UNREADABLE: "UNABLE TO READ"}.get(problem, problem.upper())


def fetch(address: str, path: str) -> tuple[Any, str, str]:
    """GET ``path`` from a node. Returns ``(payload, problem, detail)``.

    ``problem`` is "" on success, ``NOT_STARTED`` when nothing answers at the
    address, or ``UNREADABLE`` when something answered with a bad response.
    """
    try:
        return NodeClient(address, timeout=REQUEST_TIMEOUT).get(path), "", ""
    except NodeUnreachable as error:
        return None, NOT_STARTED, str(error)
    except (NodeError, ValueError) as error:
        return None, UNREADABLE, str(error) or type(error).__name__


def read_status(address: str) -> dict[str, Any]:
    """A node's /status, or ``{"state": NOT_STARTED | UNREADABLE, "error": ...}``."""
    payload, problem, detail = fetch(address, "/status")
    if not problem and not (isinstance(payload, dict) and isinstance(payload.get("state"), str)):
        problem, detail = UNREADABLE, "response is not a labnode status"
    if problem:
        return {"state": problem, "error": detail}
    return payload


def address_port(address: str) -> str:
    try:
        return str(urllib.parse.urlparse(normalize_url(address)).port)
    except ValueError:
        return "?"


class NodePoller:
    """Polls each station's GET /status and one gantry endpoint off the Tk thread.

    Only reads: it never starts, stops or commands a node.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stations: dict[str, str] = {}
        self._gantry: Optional[tuple[str, str]] = None
        self._statuses: dict[str, dict[str, Any]] = {}
        self._gantry_result: tuple[Any, str, str] = (None, "", "")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="gantry-view-poller", daemon=True)
        self._thread.start()

    def set_targets(self, stations: dict[str, str], gantry: Optional[str], gantry_path: str = "/position") -> None:
        """``stations`` maps names to node addresses; ``gantry`` is read at ``gantry_path``."""
        with self._lock:
            self._stations = dict(stations)
            self._gantry = (gantry, gantry_path) if gantry else None

    def snapshot(self) -> tuple[dict[str, dict[str, Any]], tuple[Any, str, str]]:
        """``(statuses by station, (gantry payload, problem, detail))``."""
        with self._lock:
            return dict(self._statuses), self._gantry_result

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                stations = dict(self._stations)
                gantry = self._gantry
            statuses = {name: read_status(address) for name, address in stations.items()}
            gantry_result = fetch(*gantry) if gantry else (None, "", "")
            with self._lock:
                self._statuses = statuses
                self._gantry_result = gantry_result
            self._stop.wait(POLL_INTERVAL)
