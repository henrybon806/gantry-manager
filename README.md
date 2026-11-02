# Gantry Manager

Schedules one gantry moving samples between lab experiment stations. It drives
real instruments over HTTP, and ships fake nodes (`sim/`) so the whole setup
runs on one computer with no hardware. It enforces these constraints:

- only one gantry can move at a time
- a station cannot be re-used while it is still busy
- a substrate cannot be picked up again immediately after finishing a step if the gantry is still in motion
- each experiment is backed by its own live HTTP status server (labnode)
- every sample carries route metadata and workflow state in a dedicated `Substrate` model

Related repos (clone them next to this one):

- [labnode](https://github.com/henrybon806/labnode): the small HTTP API every experiment node speaks
- [lab-devices](https://github.com/henrybon806/lab-devices): the real hardware nodes (instruments, and later the gantry)

## Configuration view

The initial view shows the experiment floor, separate shelf-access station, shelf inventory, route editor, experiment settings, and controls.

![Gantry Lab configuration view](assets/gantry-lab-window.png)

### Active simulation

The active view shows the gantry moving through the floor, live client status, queue progress, experiment timers, and per-substrate route markers.

![Gantry Lab active simulation](assets/gantry-lab-active.png)

## What this project simulates

The system models a lab floor with:

- a single gantry arm
- three experiment stations: A, B, and C
- a shelf for queued and completed samples
- per-sample route ordering and progress tracking
- live status from each experiment over HTTP

The scheduler advances by event time instead of real wall-clock sleep, which keeps the behavior deterministic and easy to reason about while still reflecting the lab's real timing constraints.

## Core behavior

### Single gantry occupancy
The gantry can only do one movement at a time. It does not overlap deliveries or pickups, and it cannot immediately process a sample that is still leaving or arriving at a busy station.

### Travel time
With a gantry node, each leg takes as long as the real gantry does. Without one, every leg takes the fixed `move_time`.

### Substrate tracking
Each sample is represented by a `Substrate` object with:

- unique sample ID
- ordered route through experiments
- current progress index
- current location
- completion state
- metadata dictionary for sample-specific information

This keeps route logic and sample identity separated from the transport state.

### Separate live experiment endpoints
Each experiment is its own process exposing labnode JSON status over HTTP. The GUI and scheduler both read from those live endpoints rather than faking values in memory.

## Project structure

```text
gantry/            the gantry
  scheduler.py       scheduling core: GantryManager, in-memory and remote experiment clients
  substrates.py      Substrate model and workflow state
  server.py          scheduler thread + its API (/pos, /state); python -m gantry runs it without the GUI
web/               the GUI, a local website (python -m web runs it alone)
  server.py          serves the page, polls nodes for it, forwards moves, saves presets
  static/index.html  the page
helpers/           shared code
  polling.py         reading labnode nodes; NOT STARTED / UNABLE TO READ
  calibration.py     HOME/REF calibration between gantry and bed coordinates
  presets.py         loading, checking and saving presets.json
sim/               fake hardware for running without a lab, one terminal each
  experiment_a.py …  fake experiment nodes with a fixed name, port and duration
  node.py            generic fake node (letter, port, duration as arguments)
  fake_gantry.py     minimal gantry node (:8100): prints pick and place; ../sim/gantry travels like the real one
tests/             unit and end-to-end tests
legacy/            the old Tkinter GUI and the timer prototype, kept for reference
data/              your settings
  presets.json       written by the GUI's Save presets, read by the gantry and the GUI
  gantry_layout.json older settings file, imported once if presets.json is missing
assets/            screenshots
main.py            the normal way to run: scheduler + gantry API + GUI in one process
requirements.txt   labnode, pinned to a release tag
```

## Architecture

```mermaid
flowchart LR
	Browser[browser] --> Main
	subgraph Main[python main.py :8080]
		GUI[GUI] -- reads, moves --> Scheduler[scheduler]
	end
	Scheduler -- start / taken / status --> A[experiment A :8001]
	Scheduler -- start / taken / status --> B[experiment B :8002]
	Scheduler -- start / taken / status --> C[experiment C :8003]
```

`python main.py` runs the GUI and the scheduler in one process, on one port.
The GUI reads everything (node statuses, the gantry, the samples) straight
from the scheduler, and only the scheduler talks to the nodes. Each experiment
node is its own
process, usually on its instrument's computer. The GUI is an observer that can
only move the gantry; the scheduler owns scheduling and transport state; each
node owns its experiment.

## Scheduling model

1. A substrate starts on the shelf.
2. The gantry travels to the floor-level shelf-access station.
3. It picks up one substrate and delivers it to the next experiment in that substrate's route.
4. The experiment client processes the substrate for its configured duration.
5. The manager polls the client's HTTP /status endpoint. A finished substrate joins the pickup queue.
6. The gantry returns the substrate to its next experiment or to the shelf when its route is complete.
7. If a destination is occupied, the substrate waits or is staged on the shelf; it is never placed into a busy client.
8. Every gantry leg uses the same fixed movement time, independent of visual distance.

## Run it

Every piece is its own process. Install once:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

labnode lives in its own repo, [henrybon806/labnode](https://github.com/henrybon806/labnode),
pinned to a tag in `requirements.txt`. To work on it alongside the gantry, clone
it next to this repo and install from there with `.venv/bin/pip install "../labnode[test]"`.
Use a regular install rather than `pip install -e`: on this Mac the `.venv`
folder carries the macOS "hidden" flag, and Python 3.13 skips the hidden
`.pth` file an editable install relies on. Reinstall after changing labnode.

Then open one terminal per process, in any order:

```bash
.venv/bin/python sim/experiment_a.py     # terminal 1
.venv/bin/python sim/experiment_b.py     # terminal 2
.venv/bin/python sim/experiment_c.py     # terminal 3
.venv/bin/python sim/fake_gantry.py      # terminal 4 (optional): the gantry node
.venv/bin/python main.py                 # terminal 5: scheduler + GUI, then open http://127.0.0.1:8080
```

The scheduler tells the gantry node at `gantry_address` (default
`127.0.0.1:8100`) where to go by name, never by coordinates. Each trip is a
`pick` at the source, then a `place` at the destination, with
`{"station", "sample_id"}` plus `"slot"` on the shelf; every sample keeps one
shelf slot until it is cleared. The gantry node owns the coordinates and
finds its own way, like Gantry-Communication's `pickupNamed`/`dropoffNamed`.

Each leg lasts as long as the gantry takes, and at least `move_time`. An
instrument gets `POST /taken` only after the gantry has finished picking from
it, and `POST /start` only after it has finished placing in it, so nothing is
loaded into or taken out of a busy instrument. If the gantry rejects a command,
fails one, or is not running, the scheduler carries on as if the command
worked. The GUI shows the problem under the gantry readout (e.g. *pick not
available: gantry NOT STARTED*) and the terminal prints it once. Clear the
gantry address (or pass `--gantry ""`) to only simulate the gantry, with every
leg taking `move_time`.

The gantry position the GUI draws is always the one the gantry node reports
(the scheduler listens to its `/status`), never an estimate. If the node cannot
be read, no dot is drawn and the readout says why. Only with no gantry address
is the position simulated, and the readout then says *SIMULATED*.

`main.py --no-gui` (or `python -m gantry`) runs the scheduler and its API
without the GUI, on `--port` (default 8200); `python -m web` runs only the GUI, to watch a gantry running
elsewhere. `--gui-port` moves the GUI off 8080.

`main.py` takes its settings from `data/presets.json` (see **Presets**
below). Any flag overrides its preset, e.g.:

```bash
.venv/bin/python main.py --gantry 127.0.0.1:8100 --home 0,0 --shelf 300,500 --slots 10 \
    --node A=8001@1050,750 --node B=8002@1800,250 --node C=8003@2550,650 \
    --route A,B,C --route C,A --route B,C --move-time 1
```

Each experiment file in `sim/` has a fixed name, port and duration (A :8001 3 s,
B :8002 4 s, C :8003 2.5 s). For a different port or duration without editing
a file, use the generic node instead:

```bash
.venv/bin/python sim/node.py A --port 8001 --duration 3
```

Real instruments live in [lab-devices](https://github.com/henrybon806/lab-devices);
start a new one from its `templates/experiment/`. To point the gantry at real nodes, put their
addresses in `data/presets.json` (or pass `--node`).

The real gantry is a `Gantry` node in lab-devices (`templates/gantry/`); point
`gantry_address` at it instead of the fake one.

`main.py` drives the nodes over HTTP (`POST /start`, `POST /taken`) on
wall-clock time and serves its API under `/scheduler` on the GUI's port, 8080
(on `--port`, default 8200, with `--no-gui`).

`@x,y` is where each station sits on the bed, in mm from the bottom-left corner. The gantry has its own `--home`
spot, separate from the shelf: it starts there and travels to the shelf like
any other location. Ask it where it is at any time:

```bash
curl http://127.0.0.1:8080/scheduler/pos
# {"x": 21.2, "y": 36.7, "z": 0.0, "moving": true, "at": null}
```

Move it while it is stopped (it lasts as long as the gantry takes; it answers 409
while the gantry is moving, and needs `Authorization: Bearer $LABNODE_TOKEN`
when a token is set):

```bash
curl -X POST http://127.0.0.1:8080/scheduler/pos -H 'content-type: application/json' -d '{"x": 40, "y": 20}'
```

The scheduler waits for the move, then carries on from that spot. In the GUI,
the **MOVE GANTRY** box (or a double-click on the bed) sends the same request.

`at` names the location when the gantry is stopped (`home`, `shelf`,
`experiment_a`, ...). `GET /position` returns the same thing, so the
GUI's *Lab (HTTP)* mode can point at the gantry. A node that is not running counts as busy,
so the gantry never delivers to it; samples wait on the shelf until it is up.

The GUI only sends one command, the gantry move; everything else is read. Type
the gantry address under **GANTRY SOURCE** and each node's address in the
**EXPERIMENTS** list (defaults match the commands above). Anything it cannot
read is labelled:

- **NOT STARTED**: nothing answers at that address
- **UNABLE TO READ**: something answered, but not with a valid status or state

The reason (for example `Connection refused`) is shown under the gantry source.

## Shelf and substrates

The scheduler is always on: it never finishes and needs no start. Whenever a
substrate arrives, send it to `POST /substrates` with its route; it goes into
a numbered shelf slot and runs as soon as its next instrument is free.
`--route` on the command line adds some at startup, but you don't need to.

```bash
S=http://127.0.0.1:8080/scheduler

# Add a substrate. Route: letters or station names, a list or "A,B,C".
# Without "slot" it takes the first free one; without "id" it gets the next free sample_N.
curl -X POST $S/substrates -H 'content-type: application/json' \
  -d '{"route": ["A", "B", "C"]}'
curl -X POST $S/substrates -H 'content-type: application/json' \
  -d '{"id": "wafer_12", "route": "C,A", "slot": 4, "metadata": {"notes": "batch 3"}}'

# What it is doing and plans next: current move, instruments, upcoming moves, recent events.
curl $S/plan

# Every slot: null when empty, else the substrate and its status
# (queued, waiting, moving, in experiment, done). Also state: idle, running or stopped.
curl $S/shelf

# Clear one slot, or every slot whose substrate is on the shelf. A cleared
# substrate leaves the queue and the shelf view.
curl -X DELETE $S/shelf/4
curl -X DELETE $S/shelf

# Emergency stop, and carry on afterwards.
curl -X POST $S/stop
curl -X POST $S/resume
```

Interactive docs for all of it are at http://127.0.0.1:8080/scheduler/docs
(the GUI's own API is at http://127.0.0.1:8080/docs; with `--no-gui`, the
scheduler's docs are at `/docs` on `--port`).

Slots are numbered from 1 in the API and GUI. A substrate keeps its slot from
when it's added until it's cleared, including while it's out at an
instrument. The gantry's `slot` parameter in pick/place numbers them from 0.
A finished substrate stays in its slot, marked done, until you clear it.

Refusals: a taken or out-of-range slot, a full shelf, a duplicate id or an
unknown experiment get 422. Clearing a slot whose substrate is at an
instrument or on the gantry gets 409 while the scheduler runs.

**Stop is an emergency stop.** The scheduler stops scheduling at once and
abandons the current gantry move. It sends `/stop` to the gantry (ahead of any
queued commands) and to every instrument holding a sample. Samples stay where
they are. You can still add substrates (they wait) and clear any slot (after
taking that substrate out by hand). `POST /resume` carries on. A substrate left
in a stopped instrument is collected once that node reports stopped or done.
One that was on the gantry is assumed to be back in its shelf slot, so put
it there before resuming.

With `LABNODE_TOKEN` set, the POST and DELETE endpoints need
`Authorization: Bearer $LABNODE_TOKEN`.

## Run the tests

```bash
.venv/bin/python -m unittest discover -v
```

This project is validated against a small deterministic suite covering:

- substrate queue insertion while work is running
- HTTP experiment status handling and the visualization poller
- route parsing and validation
- three random experiment runs preserving order and occupancy rules

## Experiment nodes (labnode)

Every experiment, real or simulated, is a [labnode](https://github.com/henrybon806/labnode)
HTTP node. A real instrument computer runs a small `Experiment` subclass that
implements `run()` (and optionally `stop()`), which gives it:

```text
GET  /status     idle | busy | stopping | done | stopped | error, plus sample and timing
POST /start      {"sample_id": ..., "params": {...}}
POST /stop       emergency stop
POST /taken      sample removed, back to idle
GET  /position   gantry node only
```

`sim/node.py` is a simulated node of this kind; nothing starts it for you.

```bash
curl http://127.0.0.1:8001/status
curl http://127.0.0.1:8080/scheduler/state
```

## GUI

`python main.py` serves the GUI as a local website on http://127.0.0.1:8080
(`--port` to change it). It listens on loopback only, since the page can move
the gantry. The server polls the nodes and the gantry for the page (browsers
cannot read the nodes directly) and forwards moves to the gantry.

The page shows the bed full width at its real shape (3000 × 1000 mm by
default), with the live gantry and each experiment's state, and the gantry's
X, Y and Z right under it. Below that is the **shelf**: one card per slot.
An empty slot is dashed; click it (or *+ Add substrate* for the next free
one) to pick experiments in order and add a substrate. A filled slot shows
the substrate's id, what it's doing, and its route with finished steps in
green and the current one in red. ✕ clears a slot, *Clear shelf* clears every
slot on the shelf, and *Emergency stop* / *Resume* stop and restart
scheduling. The **Scheduler** card (with the settings) shows what the gantry is doing
now, what each instrument holds, the upcoming moves in the order the scheduler
will take them (each with what it is waiting for), and recent events; the same
data is at `GET /scheduler/plan`. *API docs* in the header opens the scheduler's `/docs`. Settings
are in cards below, and durations come from each node.

- **Gantry**: the gantry node's address (empty to only simulate the gantry),
  the last command's result, its home spot and the move time.
- **Move gantry**: type x, y in mm and press *Move gantry*, or double-click an
  empty spot on the bed. The reply shows, e.g. *Refused: gantry is busy moving*.
- **Bed & shelf**: bed size in mm and the number of shelf slots.
- **Experiments**: set each node address (`http://host:port`, `host:port` or a
  port), drag boxes to place them, and add or remove experiments.
- Edits apply straight away (every open tab sees them) but are only written
  to disk when you press **Save presets**. *Reload* throws away unsaved edits.

The server's API, if you want to script it:

```text
GET  /api/presets    PUT /api/presets    POST /api/presets/save    POST /api/presets/reload
GET  /api/snapshot   POST /api/move {"x", "y"}   GET /api/gantry/stream (main.py only)
```

The older Tkinter window (`python -m legacy.gantry_gui`) still works but keeps its
own `data/gantry_layout.json`.

## Presets

`data/presets.json` holds everything you would otherwise re-enter each run. Edit
it in the web GUI and press **Save presets**; `main.py` (and
`python -m gantry`, `python -m web`) read it at startup (`--presets` to use another file).

```json
{
  "bed": [3000.0, 1000.0],
  "gantry_address": "127.0.0.1:8100",
  "scheduler_address": "127.0.0.1:8200",
  "gantry_home": [0.0, 0.0],
  "move_time": 1.0,
  "shelf_slots": 10,
  "calibration": {"home_raw": null, "home_bed": [0.0, 0.0], "scale": [1.0, 1.0]},
  "stations": [
    {"name": "shelf", "address": "", "x": 300.0, "y": 500.0},
    {"name": "experiment_a", "address": "127.0.0.1:8001", "x": 1050.0, "y": 750.0}
  ]
}
```

- The bed is 3000 × 1000 mm by default. Positions are bed coordinates in mm,
  with (0, 0) at the bottom-left corner, as the GUI shows them. The scheduler
  converts them to gantry coordinates through the saved calibration.
- `gantry_address` is the gantry node the scheduler sends pick and place (by
  station name) to; empty means the gantry is only simulated.
- `scheduler_address` is only for `python -m web` on its own: where it finds a
  scheduler started with `--no-gui`.
- Stations without a node address are drawn but not used by the gantry.
- Route entries are letters (`A` → `experiment_a`) or station names.
- Substrates and routes are not presets: they come through `POST /substrates`.
- If there is no `data/presets.json` yet, the first run starts from an existing
  `data/gantry_layout.json`, so nothing you set up before is lost.
- Experiment durations are not presets: each experiment decides its own run
  time (see `sim/`).

## Troubleshooting

### The terminal stays busy

That is expected: `python main.py` and each experiment are servers that run until you press Ctrl+C in their terminal. Closing the browser tab does not stop anything.

### The page says GUI SERVER DOWN

`python main.py` is not running (or is on another port). Start it and the page reconnects by itself.

### Everything says NOT STARTED

Nothing is listening at those addresses. Start the experiments and `main.py` in their own terminals and check the ports match the addresses in the GUI.

### A node stays DONE after restarting the gantry

A node keeps its sample until something calls `POST /taken`. If the gantry restarts mid-run it no longer knows that sample, so restart that node too.

### Tkinter is unavailable

Check the selected Python installation with:

```bash
python3 -c "import tkinter; print('Tkinter available')"
```

Use a desktop Python distribution that includes Tk support if this import fails.

### Capture a fresh macOS screenshot

The checked-in screenshots were captured from the running app. To capture another app-only image, launch the GUI and use macOS Screenshot or:

```bash
screencapture -x -l <window-id> assets/gantry-lab-current.png
```

The window ID can be obtained with a macOS window-inspection tool.
