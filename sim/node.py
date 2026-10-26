"""Run one simulated experiment as its own labnode process.

    python sim/node.py A --port 8001 --duration 3
    python sim/node.py B --port 8002 --duration 4
    python sim/node.py C --port 8003 --duration 2.5

Each node holds one sample at a time and finishes after ``--duration`` seconds.
Open http://127.0.0.1:<port>/docs to poke it by hand.
"""

from __future__ import annotations

import argparse
import os
import time

from labnode import Experiment
from labnode.server import LOOPBACK_HOSTS, TOKEN_ENV, create_app


class TimedExperiment(Experiment):
    def __init__(self, name: str, duration: float) -> None:
        self.name = name
        self.duration = duration

    def estimate(self, params):
        return self.duration

    def run(self, sample_id, params):
        end = time.monotonic() + self.duration
        while time.monotonic() < end:
            if self.stop_requested:
                return {"completed": False}
            time.sleep(0.05)
        return {"completed": True}

    def stop(self):
        # run() sees stop_requested and returns on its own.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("letter", help="experiment letter, e.g. A (node is named experiment_a)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--duration", type=float, default=3.0, help="seconds per sample (default 3)")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be positive")
    token = os.environ.get(TOKEN_ENV) or None
    if token is None and args.host not in LOOPBACK_HOSTS:
        parser.error(f"set {TOKEN_ENV} before listening on {args.host}")

    import uvicorn

    node = TimedExperiment(f"experiment_{args.letter.strip().lower()}", args.duration)
    print(f"{node.name} listening on http://{args.host}:{args.port} ({args.duration:g}s per sample)", flush=True)
    # The GUI and gantry poll several times a second, so skip per-request logs.
    uvicorn.run(create_app(node, token), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
