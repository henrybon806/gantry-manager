"""Sends the scheduler's commands to the gantry node (move, pick, place).

Fire and check: commands go out in order on the link's own thread, so the
scheduler never waits on the network. Before each command the link waits
(up to READY_TIMEOUT) for the gantry to finish the previous one. The scheduler
keeps its own timing and assumes each command worked; the reply only decides
what ``report`` says, e.g. "move not available: gantry NOT STARTED".
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any, Optional

from labnode import NodeClient, NodeError, NodeUnreachable


REQUEST_TIMEOUT = 2.0
READY_TIMEOUT = 30.0
READY_POLL = 0.05


class GantryLink:
    def __init__(self, address: str, token: Optional[str] = None, timeout: float = REQUEST_TIMEOUT) -> None:
        self.client = NodeClient(address, token=token, timeout=timeout)
        self.address = self.client.url
        self._lock = threading.Lock()
        self._last: Optional[dict[str, Any]] = None
        self._failures = 0
        self._queue: "queue.Queue[Optional[tuple[str, dict[str, Any]]]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="gantry-link", daemon=True)
        self._thread.start()

    def send(self, command: str, **arguments: Any) -> None:
        """Queue ``move``, ``pick`` or ``place`` (NodeClient method names)."""
        self._queue.put((command, arguments))

    def report(self) -> dict[str, Any]:
        """The gantry address, the last command's outcome and the failure count."""
        with self._lock:
            return {"address": self.address, "last": self._last, "failures": self._failures}

    def wait_idle(self, timeout: float = 5.0) -> None:
        """Block until every queued command has been sent (tests)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                command, arguments = item
                self._wait_until_ready()
                ok, reason = self._send(command, arguments)
                message = f"{command} sent" if ok else f"{command} not available: {reason}"
                with self._lock:
                    # Print a failure once, not again for every command it repeats on.
                    repeated = self._last is not None and self._last["reason"] == reason
                    self._last = {"command": command, "ok": ok, "message": message, "reason": reason, "at": time.time()}
                    if not ok:
                        self._failures += 1
                if not ok and not repeated:
                    print(f"gantry: {message}", file=sys.stderr, flush=True)
            finally:
                self._queue.task_done()

    def _wait_until_ready(self) -> None:
        """Let the gantry finish its current action; give up quietly if it cannot be read."""
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                if not self.client.status().get("busy"):
                    return
            except (NodeUnreachable, NodeError, ValueError, AttributeError):
                return
            time.sleep(READY_POLL)

    def _send(self, command: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """``(ok, reason)``; the reason says why a command was not available."""
        try:
            getattr(self.client, command)(**arguments)
        except NodeUnreachable:
            return False, f"gantry NOT STARTED at {self.address}"
        except NodeError as error:
            if error.status == 501:
                return False, "the gantry does not implement it"
            if error.status == 409:
                return False, f"gantry busy ({error.detail})"
            return False, error.detail
        except ValueError as error:
            return False, f"UNABLE TO READ the reply ({error})"
        return True, ""
