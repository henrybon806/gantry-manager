"""Sends the scheduler's commands to the gantry node (move, pick, place).

It also listens to the gantry's /status on its own thread, so ``position``
is the gantry's real (x, y) as last reported, never an estimate.

Fire and check: commands go out in order on the link's own thread, so the
scheduler never waits on the network. ``send`` returns a ticket, and
``done(ticket)`` turns true once the gantry has finished that command (up to
READY_TIMEOUT), so the scheduler can wait for the real gantry instead of its
own clock. A command the gantry rejects, or that cannot reach it, counts as
done at once; the reply only decides what ``report`` says, e.g. "move not
available: gantry NOT STARTED".
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any, Optional

from labnode import NodeClient, NodeError, NodeListener, NodeUnreachable
from labnode.listen import UNREADABLE


REQUEST_TIMEOUT = 2.0
# A long, slow leg can take a while; past this the link stops waiting for it.
READY_TIMEOUT = 120.0
READY_POLL = 0.05
POSITION_POLL = 0.02


class GantryLink:
    def __init__(self, address: str, token: Optional[str] = None, timeout: float = REQUEST_TIMEOUT) -> None:
        self.client = NodeClient(address, token=token, timeout=timeout)
        self.address = self.client.url
        self._lock = threading.Lock()
        self._last: Optional[dict[str, Any]] = None
        self._failures = 0
        self._queue: "queue.Queue[Optional[tuple[int, str, dict[str, Any]]]]" = queue.Queue()
        # Tickets are handed out in order and finished in order.
        self._issued = 0
        self._finished = 0
        # Bumped by stop_now: a command queued before an emergency stop is dropped.
        self._epoch = 0
        self._thread = threading.Thread(target=self._run, name="gantry-link", daemon=True)
        self._thread.start()
        self.listener = NodeListener(self.address, "/status", interval=POSITION_POLL)

    def position(self) -> tuple[Optional[tuple[float, float]], str, str]:
        """``((x, y), "", "")`` from the gantry's latest /status, else ``(None, problem, detail)``.

        ``problem`` is NOT_STARTED or UNREADABLE, as for any node.
        """
        payload, problem, detail = self.listener.latest()
        if problem:
            return None, problem, detail
        try:
            position = payload["position"]
            return (float(position["x"]), float(position["y"])), "", ""
        except (KeyError, TypeError, ValueError):
            return None, UNREADABLE, "the gantry's /status has no position"

    def z(self) -> Optional[float]:
        """The gantry's z from its latest /status, if it reports one."""
        payload, problem, _ = self.listener.latest()
        try:
            return None if problem else float(payload["position"]["z"])
        except (KeyError, TypeError, ValueError):
            return None

    def send(self, command: str, **arguments: Any) -> int:
        """Queue ``move``, ``pick`` or ``place`` (NodeClient method names); see stop_now for stop.

        Returns the command's ticket for ``done``.
        """
        with self._lock:
            self._issued += 1
            ticket = self._issued
        self._queue.put((ticket, command, arguments))
        return ticket

    def done(self, ticket: int) -> bool:
        """True once the gantry has finished (or could not take) command ``ticket``."""
        with self._lock:
            return ticket <= self._finished

    def _finish(self, ticket: int) -> None:
        with self._lock:
            self._finished = max(self._finished, ticket)

    def stop_now(self) -> None:
        """Emergency stop: drop queued commands and send /stop at once, without waiting."""
        with self._lock:
            self._epoch += 1
        try:
            while True:
                item = self._queue.get_nowait()
                if item is not None:
                    self._finish(item[0])
                self._queue.task_done()
        except queue.Empty:
            pass
        threading.Thread(target=self._record, args=("stop", {}), name="gantry-stop", daemon=True).start()

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
        self.listener.close()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                ticket, command, arguments = item
                with self._lock:
                    epoch = self._epoch
                self._wait_until_ready()
                with self._lock:
                    dropped = epoch != self._epoch
                if not dropped and self._record(command, arguments):
                    # Accepted: the gantry is busy with it until it finishes.
                    error = self._wait_until_ready().get("error")
                    if error:
                        self._failed(command, error)
                self._finish(ticket)
            finally:
                self._queue.task_done()

    def _record(self, command: str, arguments: dict[str, Any]) -> bool:
        """Send one command, remember how it went and return whether it was accepted."""
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
        return ok

    def _failed(self, command: str, error: str) -> None:
        """The gantry took ``command`` but could not finish it."""
        message = f"{command} failed: {error}"
        with self._lock:
            self._last = {"command": command, "ok": False, "message": message, "reason": error, "at": time.time()}
            self._failures += 1
        print(f"gantry: {message}", file=sys.stderr, flush=True)

    def _wait_until_ready(self) -> dict[str, Any]:
        """Let the gantry finish its current action and return its /status ({} if it cannot be read)."""
        deadline = time.monotonic() + READY_TIMEOUT
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                status = self.client.status()
                if not status.get("busy"):
                    return status
            except (NodeUnreachable, NodeError, ValueError, AttributeError):
                return {}
            time.sleep(READY_POLL)
        return status

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
