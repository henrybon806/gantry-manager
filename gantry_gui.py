"""Small Tkinter visualization for the deterministic gantry simulation."""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk

from gantry import ExperimentClient, GantryManager, Substrate


WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 780
TICK_MS = 100
SIMULATION_STEP = 0.1


EXPERIMENT_NAMES = ("experiment_a", "experiment_b", "experiment_c")
DEFAULT_EXPERIMENT_SETTINGS = {
    "experiment_a": {"duration": 3.0, "location": 32.0},
    "experiment_b": {"duration": 4.0, "location": 55.0},
    "experiment_c": {"duration": 2.5, "location": 78.0},
}


def parse_routes(sample_count: int, route_text: str) -> list[tuple[str, ...]]:
    routes = []
    for line in route_text.splitlines():
        route = tuple(
            f"experiment_{token.strip().lower()}"
            for token in line.split(",")
            if token.strip()
        )
        if route:
            routes.append(route)
    if len(routes) != sample_count:
        raise ValueError(f"Enter exactly {sample_count} non-empty sample routes.")
    if any(experiment not in EXPERIMENT_NAMES for route in routes for experiment in route):
        raise ValueError("Routes may only use A, B, or C.")
    return routes


def create_manager(
    routes: list[tuple[str, ...]],
    settings: dict[str, dict[str, float]] | None = None,
) -> GantryManager:
    settings = settings or DEFAULT_EXPERIMENT_SETTINGS
    clients = [
        ExperimentClient(name, settings[name]["duration"])
        for name in EXPERIMENT_NAMES
    ]
    substrates = [Substrate(f"sample_{index + 1}", route) for index, route in enumerate(routes)]
    return GantryManager(clients, substrates, movement_time=1.0)


def create_demo_manager() -> GantryManager:
    return create_manager([
        ("experiment_a", "experiment_b", "experiment_c"),
        ("experiment_c", "experiment_a"),
        ("experiment_b", "experiment_c"),
    ], DEFAULT_EXPERIMENT_SETTINGS)


class ScrollText(tk.Frame):
    """Read-only text panel with its own scrollbar. Keeps scroll position on update."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, background="#fffdf8")
        self.text = tk.Text(
            self,
            state="disabled",
            wrap="none",
            background="#fffdf8",
            foreground="#526064",
            relief="flat",
            font=("Menlo", 9),
            padx=8,
            pady=4,
            cursor="arrow",
            highlightthickness=0,
        )
        bar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        self.text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.text.tag_configure(
            "header", font=("Helvetica", 9, "bold"), foreground="#6d7779", spacing1=6, spacing3=3
        )
        self._cache: list[tuple[str, str]] | None = None

    def set_lines(self, lines: list[tuple[str, str]]) -> None:
        if lines == self._cache:
            return
        self._cache = lines
        top = self.text.yview()[0]
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        for content, color in lines:
            if color == "header":
                tag = "header"
            else:
                tag = color
                self.text.tag_configure(tag, foreground=color)
            self.text.insert("end", content + "\n", tag)
        self.text.config(state="disabled")
        self.text.yview_moveto(top)

    def append(self, line: str) -> None:
        self.text.config(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.config(state="disabled")

    def clear(self) -> None:
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")


class GantryVisualizer:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Gantry Lab Simulator")
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.minsize(1050, 700)

        self.manager = create_demo_manager()
        self.running = False
        self.after_id: str | None = None
        self.last_event_count = 0
        self.station_positions: dict[str, tuple[float, float]] = {}
        self.sample_count_var = tk.IntVar(value=3)
        self.duration_vars = {
            name: tk.DoubleVar(value=DEFAULT_EXPERIMENT_SETTINGS[name]["duration"])
            for name in EXPERIMENT_NAMES
        }
        self.location_vars = {
            name: tk.DoubleVar(value=DEFAULT_EXPERIMENT_SETTINGS[name]["location"])
            for name in EXPERIMENT_NAMES
        }

        self._build_layout()
        self._render()
        self.canvas.bind("<Configure>", lambda _e: self._render())
        self.storage_canvas.bind("<Configure>", lambda _e: self._render())

    # ------------------------------------------------------------------ layout

    def _build_layout(self) -> None:
        self.root.configure(background="#f4f1ea")
        header = tk.Frame(self.root, background="#263238", height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="GANTRY LAB",
            font=("Helvetica", 18, "bold"),
            fg="#f4f1ea",
            bg="#263238",
        ).pack(side="left", padx=28)
        self.clock_label = tk.Label(
            header,
            text="SIM TIME  0.0 s",
            font=("Menlo", 11),
            fg="#a5d6a7",
            bg="#263238",
        )
        self.clock_label.pack(side="right", padx=28)

        content = tk.Frame(self.root, background="#f4f1ea")
        content.pack(fill="both", expand=True, padx=18, pady=14)

        visual_area = tk.Frame(content, background="#f4f1ea")
        visual_area.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(
            visual_area,
            height=280,
            background="#fffdf8",
            highlightthickness=1,
            highlightbackground="#d8d1c5",
        )
        self.canvas.pack(fill="x")

        storage_frame = tk.Frame(visual_area, background="#f4f1ea")
        storage_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.storage_canvas = tk.Canvas(
            storage_frame,
            background="#f8f5ee",
            highlightthickness=1,
            highlightbackground="#d8d1c5",
            yscrollincrement=20,
        )
        storage_scrollbar = ttk.Scrollbar(
            storage_frame, orient="vertical", command=self.storage_canvas.yview
        )
        self.storage_canvas.configure(yscrollcommand=storage_scrollbar.set)
        self.storage_canvas.pack(side="left", fill="both", expand=True)
        storage_scrollbar.pack(side="right", fill="y")
        self.storage_canvas.bind("<MouseWheel>", self._on_storage_scroll)
        self.storage_canvas.bind("<Button-4>", self._on_storage_scroll)
        self.storage_canvas.bind("<Button-5>", self._on_storage_scroll)

        sidebar = tk.Frame(content, width=340, background="#f4f1ea")
        sidebar.pack(side="right", fill="y", padx=(16, 0))
        sidebar.pack_propagate(False)

        # Sample setup
        setup = tk.Frame(sidebar, background="#f4f1ea")
        setup.pack(fill="x", pady=(0, 10))
        count_row = tk.Frame(setup, background="#f4f1ea")
        count_row.pack(fill="x", pady=(0, 4))
        tk.Label(count_row, text="SAMPLES", font=("Helvetica", 10, "bold"), fg="#6d7779", bg="#f4f1ea").pack(side="left")
        tk.Spinbox(count_row, from_=1, to=30, width=4, textvariable=self.sample_count_var).pack(side="right")
        route_frame = tk.Frame(setup, background="#f4f1ea")
        route_frame.pack(fill="x")
        self.route_text = tk.Text(route_frame, height=4, width=30, background="#fffdf8", foreground="#526064", relief="flat", font=("Menlo", 9))
        route_scrollbar = ttk.Scrollbar(route_frame, orient="vertical", command=self.route_text.yview)
        self.route_text.configure(yscrollcommand=route_scrollbar.set)
        self.route_text.insert("1.0", "A,B,C\nC,A\nB,C")
        self.route_text.pack(side="left", fill="both", expand=True)
        route_scrollbar.pack(side="right", fill="y")
        tk.Label(setup, text="One route per line, e.g. A,B,C", fg="#7c8788", bg="#f4f1ea", font=("Helvetica", 8)).pack(anchor="w", pady=(2, 4))
        ttk.Button(setup, text="Apply sample setup", command=self._apply_setup).pack(fill="x")
        queue_row = tk.Frame(setup, background="#f4f1ea")
        queue_row.pack(fill="x", pady=(5, 0))
        self.new_route_var = tk.StringVar(value="A,B,C")
        tk.Entry(queue_row, textvariable=self.new_route_var, background="#fffdf8", foreground="#526064", relief="flat", font=("Menlo", 9)).pack(side="left", fill="x", expand=True)
        ttk.Button(queue_row, text="Queue", command=self._queue_sample).pack(side="right", padx=(5, 0))

        # Experiment setup
        editor = tk.Frame(sidebar, background="#f4f1ea")
        editor.pack(fill="x", pady=(0, 10))
        tk.Label(editor, text="EXPERIMENTS", font=("Helvetica", 10, "bold"), fg="#6d7779", bg="#f4f1ea").pack(anchor="w")
        tk.Label(editor, text="Duration (s) left, floor position (%) right", fg="#7c8788", bg="#f4f1ea", font=("Helvetica", 8)).pack(anchor="w", pady=(0, 3))
        for name in EXPERIMENT_NAMES:
            row = tk.Frame(editor, background="#f4f1ea")
            row.pack(fill="x", pady=1)
            tk.Label(row, text=name.replace("experiment_", "EXP ").upper(), width=8, anchor="w", fg="#526064", bg="#f4f1ea", font=("Menlo", 9, "bold")).pack(side="left")
            tk.Spinbox(row, from_=0.5, to=60.0, increment=0.5, width=6, textvariable=self.duration_vars[name]).pack(side="left")
            tk.Spinbox(row, from_=18.0, to=88.0, increment=1.0, width=5, textvariable=self.location_vars[name]).pack(side="right")

        # Run controls
        controls = tk.Frame(sidebar, background="#f4f1ea")
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="Start", command=self._toggle_running)
        self.start_button.pack(side="left", fill="x", expand=True)
        ttk.Button(controls, text="Step", command=self._step).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(controls, text="Reset", command=self._reset).pack(side="left", fill="x", expand=True)

        self.status_label = tk.Label(
            sidebar, text="READY", font=("Helvetica", 12, "bold"), anchor="w", fg="#bf6b2e", bg="#f4f1ea"
        )
        self.status_label.pack(fill="x", pady=(8, 0))
        self.error_label = tk.Label(sidebar, text="", anchor="w", justify="left", fg="#b64242", bg="#f4f1ea", font=("Helvetica", 8), wraplength=320)
        self.error_label.pack(fill="x", pady=(0, 6))

        # Scrollable tabs
        notebook = ttk.Notebook(sidebar)
        notebook.pack(fill="both", expand=True)
        self.status_panel = ScrollText(notebook)
        self.sample_panel = ScrollText(notebook)
        self.log_panel = ScrollText(notebook)
        notebook.add(self.status_panel, text="Status")
        notebook.add(self.sample_panel, text="Samples")
        notebook.add(self.log_panel, text="Log")

    # ----------------------------------------------------------------- drawing

    def _draw_scene(self) -> None:
        self.canvas.delete("scene")
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1:
            width = 650
        if height <= 1:
            height = 280
        center_y = height * 0.58
        self.station_positions = {
            "shelf": (width * 0.10, center_y),
            "experiment_a": (width * 0.35, center_y),
            "experiment_b": (width * 0.58, center_y),
            "experiment_c": (width * 0.81, center_y),
        }

        self.canvas.create_text(
            20, 22,
            text="EXPERIMENT FLOOR  /  1 GANTRY",
            anchor="w",
            fill="#6d7779",
            font=("Helvetica", 10, "bold"),
            tags="scene",
        )
        self.canvas.create_text(
            width - 20, 22,
            text="GREEN = COMPLETE    RED = PENDING",
            anchor="e",
            fill="#7c8788",
            font=("Menlo", 8, "bold"),
            tags="scene",
        )
        shelf_x, shelf_y = self.station_positions["shelf"]
        self._draw_station(shelf_x, shelf_y, "SHELF ACCESS", "shelf", "#d7a84d", "READY")
        for name in EXPERIMENT_NAMES:
            x, y = self.station_positions[name]
            client = self.manager.clients[name]
            state = "DONE" if client.is_finished(self.manager.now) else ("BUSY" if client.busy else "IDLE")
            color = "#bf6b2e" if state == "DONE" else ("#d56b55" if state == "BUSY" else "#6b9da0")
            self._draw_station(x, y, name.replace("experiment_", "EXP ").upper(), name, color, state)

    def _draw_shelf(self) -> None:
        self.storage_canvas.delete("storage")
        width = self.storage_canvas.winfo_width()
        if width <= 1:
            width = 650
        top = 8
        self.storage_canvas.create_text(
            16, top + 12, text="SHELF STORAGE / VISUAL INVENTORY", anchor="w",
            fill="#6d7779", font=("Helvetica", 10, "bold"), tags="storage",
        )

        substrates = self.manager.substrates
        columns = max(1, int((width - 24) // 150))
        slot_width = (width - 24) / columns
        steps_per_line = max(1, int((slot_width - 16) // 26))
        max_steps = max((len(s.experiment_order) for s in substrates), default=1)
        lines = max(1, math.ceil(max_steps / steps_per_line))
        box_height = 22 + 14 * lines
        row_height = box_height + 8
        grid_top = top + 30

        for index, substrate in enumerate(substrates):
            column = index % columns
            row = index // columns
            x = 12 + column * slot_width
            y = grid_top + row * row_height
            if substrate.completed:
                state, color = "COMPLETED", "#4e9560"
            elif substrate.location == "shelf":
                state, color = "NEW", "#d7a84d"
            else:
                state, color = substrate.location.replace("experiment_", "").upper(), "#6b9da0"
            right = x + slot_width - 8
            self.storage_canvas.create_rectangle(x, y, right, y + box_height, fill="#fffdf8", outline=color, width=2, tags="storage")
            self.storage_canvas.create_text(x + 6, y + 10, text=substrate.substrate_id, anchor="w", fill="#263238", font=("Menlo", 8, "bold"), tags="storage")
            self.storage_canvas.create_text(right - 6, y + 10, text=state, anchor="e", fill=color, font=("Menlo", 8, "bold"), tags="storage")
            for step_index, experiment in enumerate(substrate.experiment_order):
                step_color = "#4e9560" if step_index < substrate.current_experiment_index else "#c94f4f"
                step_label = experiment.replace("experiment_", "").upper()
                line = step_index // steps_per_line
                slot = step_index % steps_per_line
                step_x = x + 8 + slot * 26
                step_y = y + 22 + line * 14
                self.storage_canvas.create_oval(step_x, step_y, step_x + 10, step_y + 10, fill=step_color, outline=step_color, tags="storage")
                self.storage_canvas.create_text(step_x + 13, step_y + 5, text=step_label, anchor="w", fill=step_color, font=("Menlo", 7, "bold"), tags="storage")

        rows = math.ceil(len(substrates) / columns) if substrates else 0
        content_height = grid_top + rows * row_height + 8
        self.storage_canvas.configure(
            scrollregion=(0, 0, width, max(content_height, self.storage_canvas.winfo_height()))
        )

    def _on_storage_scroll(self, event: tk.Event) -> str:
        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        else:
            units = -1 if event.delta > 0 else 1
        self.storage_canvas.yview_scroll(units, "units")
        return "break"

    def _draw_station(self, x: float, y: float, label: str, name: str, color: str, state: str) -> None:
        self.canvas.create_rectangle(
            x - 64, y - 46, x + 64, y + 46,
            fill="#f8f5ee", outline=color, width=2, tags=("scene", f"station:{name}"),
        )
        self.canvas.create_text(x, y - 12, text=label, fill="#263238", font=("Helvetica", 9, "bold"), tags="scene")
        self.canvas.create_text(x, y + 14, text=state, fill=color, font=("Menlo", 9, "bold"), tags=("scene", f"state:{name}"))

    def _draw_gantry(self) -> None:
        snapshot = self.manager.transport_snapshot
        if snapshot is None:
            station_x, station_y = self.station_positions.get(self.manager.position, self.station_positions["shelf"])
            point = (station_x, station_y - 82)
            carried = ""
        else:
            start_name = snapshot["position"] if snapshot["phase"] == "pickup" else snapshot["source"]
            end_name = snapshot["source"] if snapshot["phase"] == "pickup" else snapshot["destination"]
            start = self.station_positions[start_name]
            end = self.station_positions[end_name]
            progress = min(1.0, max(0.0, (self.manager.now - snapshot["started_at"]) / self.manager.movement_time))
            point = (
                start[0] + (end[0] - start[0]) * progress,
                start[1] + (end[1] - start[1]) * progress - 82,
            )
            carried = snapshot["substrate_id"] if snapshot["phase"] == "delivery" else ""

        x, y = point
        self.canvas.create_line(x, y + 16, x, y + 43, fill="#263238", width=2, tags="scene")
        self.canvas.create_rectangle(x - 27, y - 16, x + 27, y + 16, fill="#263238", outline="#a5d6a7", width=2, tags="scene")
        self.canvas.create_text(x, y, text="G", fill="#a5d6a7", font=("Helvetica", 12, "bold"), tags="scene")
        self.canvas.create_text(x + 42, y - 8, text="GANTRY", anchor="w", fill="#263238", font=("Menlo", 7, "bold"), tags="scene")
        if carried:
            self.canvas.create_text(x, y + 31, text=carried, fill="#bf6b2e", font=("Menlo", 8, "bold"), tags="scene")

    # ------------------------------------------------------------------ panels

    def _refresh_panels(self) -> None:
        self.clock_label.config(text=f"SIM TIME  {self.manager.now:04.1f} s")
        if self.manager.completed:
            self.status_label.config(text="COMPLETE", fg="#4e9560")
        else:
            self.status_label.config(text="RUNNING" if self.running else "PAUSED", fg="#bf6b2e")
        self.start_button.config(text="Pause" if self.running else "Start")

        lines: list[tuple[str, str]] = [("STATIONS", "header")]
        for server in self.manager.servers.values():
            status = server.read_status(self.manager.now)
            state = status["state"].upper()
            color = "#bf6b2e" if state == "FINISHED" else ("#d56b55" if state == "BUSY" else "#6b9da0")
            sample = status["substrate_id"] or "--"
            timer = ""
            if status["state"] in {"busy", "finished"}:
                timer = f" {status['elapsed']:04.1f}/{status['duration']:04.1f}s ({status['remaining']:04.1f} left)"
            lines.append((f"{server.client.name[-1].upper()}  {state:<9} {sample}  :{status['port']}{timer}", color))

        lines.append(("QUEUE", "header"))
        queue = self.manager.queue_snapshot
        if not queue:
            lines.append(("Queue clear", "#6b9da0"))
        else:
            for index, item in enumerate(queue, start=1):
                next_name = item["next_experiment"].replace("experiment_", "").upper()
                lines.append((f"{index:02d} {item['substrate_id']} -> {next_name} ({item['state']})", "#526064"))
        self.status_panel.set_lines(lines)

        sample_lines: list[tuple[str, str]] = []
        for substrate in self.manager.substrates:
            steps = []
            for index, experiment in enumerate(substrate.experiment_order):
                label = experiment.replace("experiment_", "").upper()
                if index < substrate.current_experiment_index:
                    steps.append(f"✓{label}")
                elif index == substrate.current_experiment_index and not substrate.completed:
                    steps.append(f"→{label}")
                else:
                    steps.append(f"○{label}")
            sample_lines.append((
                f"{substrate.substrate_id:<10} {' '.join(steps)}",
                "#4e9560" if substrate.completed else "#526064",
            ))
        self.sample_panel.set_lines(sample_lines)

    def _append_new_events(self) -> None:
        for event in self.manager.events[self.last_event_count:]:
            timestamp, kind, detail = event
            if kind == "gantry_departed":
                sample_route, sample_id = detail.rsplit(":", 1)
                message = f"Moving {sample_id}: {sample_route}"
            elif kind == "gantry_picked_up":
                message = f"Picked up {detail}"
            elif kind == "gantry_delivered":
                sample_id, destination = detail.split("->", 1)
                message = f"Delivered {sample_id} to {destination}"
            elif kind == "client_finished":
                client_name, sample_id, _ = detail.split(":")
                message = f"{client_name} finished {sample_id}; pickup queued"
            elif kind == "substrate_added":
                sample_id, route = detail.split(":", 1)
                message = f"Queued {sample_id}: {route}"
            else:
                message = detail
            self.log_panel.append(f"{timestamp:04.1f} {message}")
        self.last_event_count = len(self.manager.events)

    # -------------------------------------------------------------- simulation

    def _render(self) -> None:
        self._draw_scene()
        self._draw_shelf()
        self._draw_gantry()
        self._refresh_panels()
        self._append_new_events()

    def _cancel_loop(self) -> None:
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None

    def _loop(self) -> None:
        self.after_id = None
        if not self.running:
            return
        if self.manager.completed:
            self.running = False
            self._render()
            return
        self.manager.tick(self.manager.now + SIMULATION_STEP)
        if self.manager.completed:
            self.running = False
        self._render()
        if self.running:
            self.after_id = self.root.after(TICK_MS, self._loop)

    def _set_error(self, text: str) -> None:
        self.error_label.config(text=text)

    def _queue_sample(self) -> None:
        try:
            route = parse_routes(1, self.new_route_var.get())[0]
            sample_id = f"sample_{len(self.manager.substrates) + 1}"
            self.manager.add_substrate(Substrate(sample_id, route))
            self.sample_count_var.set(len(self.manager.substrates))
            self.route_text.insert("end", ",".join(name.replace("experiment_", "").upper() for name in route) + "\n")
            self._set_error("")
            self._render()
        except (TypeError, ValueError) as error:
            self._set_error(str(error))

    def _apply_setup(self) -> None:
        try:
            sample_count = int(self.sample_count_var.get())
            routes = parse_routes(sample_count, self.route_text.get("1.0", "end"))
            settings = {
                name: {
                    "duration": float(self.duration_vars[name].get()),
                    "location": float(self.location_vars[name].get()),
                }
                for name in EXPERIMENT_NAMES
            }
            if any(values["duration"] <= 0 for values in settings.values()):
                raise ValueError("Experiment durations must be positive.")
            locations = [values["location"] for values in settings.values()]
            if len(set(locations)) != len(locations):
                raise ValueError("Experiment locations must be different.")
            if any(location < 18 or location > 88 for location in locations):
                raise ValueError("Experiment locations must be between 18% and 88%.")
        except (TypeError, ValueError) as error:
            self._set_error(str(error))
            return
        self._cancel_loop()
        self.running = False
        self.manager.close()
        self.manager = create_manager(routes, settings)
        self.last_event_count = 0
        self.log_panel.clear()
        self._set_error("")
        self._render()

    def _toggle_running(self) -> None:
        if self.manager.completed:
            return
        self.running = not self.running
        if self.running:
            self._loop()
        else:
            self._cancel_loop()
            self._render()

    def _step(self) -> None:
        if not self.manager.completed:
            self.manager.tick(self.manager.now + SIMULATION_STEP)
            self._render()

    def _reset(self) -> None:
        self._apply_setup()


def main() -> None:
    root = tk.Tk()
    visualizer = GantryVisualizer(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (visualizer._cancel_loop(), visualizer.manager.close(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()