"""Run the gantry: the scheduler and the web GUI, in one process.

    .venv/bin/python main.py             # then open http://127.0.0.1:8080
    .venv/bin/python main.py --no-gui    # scheduler only (same as python -m gantry)

Start the experiment nodes in their own terminals (sim/ or lab-devices).
Settings come from data/presets.json; the gantry flags (--node, --route, ...)
are the same as python -m gantry's.

The GUI reads the scheduler directly. Its API is served on the same port under
/scheduler (e.g. GET /scheduler/state); with --no-gui it is served on --port.
"""

import argparse

from gantry import server as gantry_server
from web import server as web_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="python main.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    gantry_server.add_arguments(parser)
    parser.add_argument("--gui-port", type=int, default=8080, help="web GUI port (default 8080)")
    parser.add_argument("--no-gui", action="store_true", help="run the scheduler without the web GUI")
    args = parser.parse_args()
    scheduler, port, token = gantry_server.scheduler_from_args(parser, args)
    scheduler_app = gantry_server.create_app(scheduler, token)

    import uvicorn

    scheduler.start()
    try:
        if args.no_gui:
            print(gantry_server.describe(scheduler, f"http://{args.host}:{port}/state"), flush=True)
            uvicorn.run(scheduler_app, host=args.host, port=port, access_log=False)
            return
        store = web_server.store_from_args(parser, args.presets, scheduler=scheduler)
        app = web_server.create_app(store)
        app.mount("/scheduler", scheduler_app)
        print(gantry_server.describe(scheduler, f"http://127.0.0.1:{args.gui_port}/scheduler/state"), flush=True)
        web_server.serve(store, args.gui_port, app)
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
