"""Example experiment C. Run it in its own terminal:

    .venv/bin/python sim/experiment_c.py

Serves labnode on http://127.0.0.1:8003 (try /docs). Each sample takes
2.5 seconds. Copy this file to make a new experiment: change NAME, PORT
and run().
"""

import time

import uvicorn
from labnode import Experiment, create_app


NAME = "experiment_c"
PORT = 8003
DURATION = 2.5  # seconds per sample


class ExperimentC(Experiment):
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
    uvicorn.run(create_app(ExperimentC()), host="127.0.0.1", port=PORT, access_log=False)
