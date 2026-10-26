"""A fake gantry node: what the scheduler sends move, pick and place to.

    .venv/bin/python sim/fake_gantry.py                  # port 8100, 1 s per move
    .venv/bin/python sim/fake_gantry.py --move-time 2

Each move travels in a straight line for --move-time seconds; pick and place
take a moment. Every command is printed, so you can watch what the scheduler
asks for. Open http://127.0.0.1:8100/docs to send commands by hand.
"""

from __future__ import annotations

import argparse
import time

from labnode import Gantry


PICK_PLACE_SECONDS = 0.1


class FakeGantry(Gantry):
    name = "gantry"

    def __init__(self, move_time: float) -> None:
        self.move_time = move_time
        self._from = self._to = (0.0, 0.0)
        self._started = self._arrives = time.monotonic()

    def position(self):
        span = self._arrives - self._started
        progress = 1.0 if span <= 0 else min(1.0, (time.monotonic() - self._started) / span)
        x = self._from[0] + (self._to[0] - self._from[0]) * progress
        y = self._from[1] + (self._to[1] - self._from[1]) * progress
        return {"x": x, "y": y, "z": 0.0}

    def move(self, x, y, z, params):
        print(f"move to ({x:g}, {y:g}) {params.get('to', '')}", flush=True)
        here = self.position()
        self._from, self._to = (here["x"], here["y"]), (x, y)
        self._started = time.monotonic()
        self._arrives = self._started + self.move_time
        while time.monotonic() < self._arrives:
            if self.stop_requested:
                return
            time.sleep(0.02)

    def pick(self, params):
        print(f"pick  {params.get('sample_id', '')} at {params.get('station', '')}", flush=True)
        time.sleep(PICK_PLACE_SECONDS)

    def place(self, params):
        print(f"place {params.get('sample_id', '')} at {params.get('station', '')}", flush=True)
        time.sleep(PICK_PLACE_SECONDS)

    def stop(self):
        # Freeze where it is; move() sees stop_requested and returns.
        here = self.position()
        self._from = self._to = (here["x"], here["y"])
        self._started = self._arrives = time.monotonic()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--move-time", type=float, default=1.0, help="seconds per move (default 1)")
    args = parser.parse_args()
    if args.move_time <= 0:
        parser.error("--move-time must be positive")

    import uvicorn
    from labnode import create_app

    print(f"fake gantry listening on http://127.0.0.1:{args.port} ({args.move_time:g}s per move)", flush=True)
    # The scheduler polls several times a second, so skip per-request logs.
    uvicorn.run(create_app(FakeGantry(args.move_time)), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
