"""Run the gantry: the scheduler, its position API and the web GUI, in one process.

    .venv/bin/python main.py             # then open http://127.0.0.1:8080
    .venv/bin/python main.py --no-gui    # scheduler only (same as python -m gantry)

Start the experiment nodes in their own terminals (sim/ or lab-devices).
Settings come from data/presets.json; the gantry flags (--node, --route,
--port, ...) are the same as python -m gantry's.
"""

import argparse
import threading
import time

from gantry import server as gantry_server
from web import server as web_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="python main.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    gantry_server.add_arguments(parser)
    parser.add_argument("--gui-port", type=int, default=8080, help="web GUI port (default 8080)")
    parser.add_argument("--no-gui", action="store_true", help="run the scheduler without the web GUI")
    args = parser.parse_args()
    scheduler, port, token = gantry_server.scheduler_from_args(parser, args)
    store = None if args.no_gui else web_server.store_from_args(parser, args.presets)

    import uvicorn

    app = gantry_server.create_app(scheduler, token)
    gantry = uvicorn.Server(uvicorn.Config(app, host=args.host, port=port, access_log=False))
    scheduler.start()
    print(gantry_server.describe(scheduler, args.host, port), flush=True)
    if store is None:
        try:
            gantry.run()
        finally:
            scheduler.stop()
        return

    # The gantry API runs in the background; the GUI owns the main thread and Ctrl+C.
    thread = threading.Thread(target=gantry.run, name="gantry-api", daemon=True)
    thread.start()
    while not gantry.started and thread.is_alive():
        time.sleep(0.05)
    if not gantry.started:
        scheduler.stop()
        store.poller.close()
        raise SystemExit(f"gantry could not listen on {args.host}:{port}")
    try:
        web_server.serve(store, args.gui_port)
    finally:
        gantry.should_exit = True
        thread.join(timeout=2.0)
        scheduler.stop()


if __name__ == "__main__":
    main()
