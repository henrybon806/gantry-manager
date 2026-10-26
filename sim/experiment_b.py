"""Example experiment B. Run it in its own terminal:

    .venv/bin/python sim/experiment_b.py

Serves labnode on http://127.0.0.1:8002 (try /docs). Each sample takes
4.0 seconds. Copy this file to make a new experiment: change NAME, PORT
and run().
"""

import time

import uvicorn
from labnode import Experiment, create_app


NAME = "experiment_b"
PORT = 8002
DURATION = 4.0  # seconds per sample


class ExperimentB(Experiment):
    name = NAME

    def estimate(self, params):
        return DURATION

    def run(self, sample_id, params):
        # A real instrument would do its measurement here.
        end = time.monotonic() + DURATION
        while time.monotonic() < end:
            if self.stop_requested:
                return {"completed": False}
            time.sleep(0.05)
        return {"completed": True}

    def stop(self):
        # run() sees stop_requested and returns on its own.
        pass


if __name__ == "__main__":
    print(f"{NAME} listening on http://127.0.0.1:{PORT} ({DURATION:g}s per sample)", flush=True)
    # The GUI and gantry poll several times a second, so skip per-request logs.
    uvicorn.run(create_app(ExperimentB()), host="127.0.0.1", port=PORT, access_log=False)
