"""Old Tk top-down view of the gantry bed. The web GUI (python -m web) replaces it."""

from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from dataclasses import asdict
from tkinter import ttk
from typing import Any, Optional

from dataclasses import dataclass

from gantry.scheduler import NOT_STARTED, UNREADABLE
from helpers.calibration import Calibration
from helpers.polling import NodePoller, address_port, position_from_payload, problem_label
from helpers.presets import DEFAULT_HOST, DEFAULT_STATIONS, LAYOUT_PATH
from labnode import NodeClient, NodeError, NodeUnreachable
from labnode.server import TOKEN_ENV


REFRESH_MS = 100
BED_MARGIN = 44
STATION_HALF_WIDTH = 52
STATION_HALF_HEIGHT = 32

STATE_COLORS = {
    "idle": "#6b9da0",
    "busy": "#d56b55",
    "stopping": "#b64242",
    "done": "#bf6b2e",
    "stopped": "#b64242",
    "error": "#b64242",
    "offline": "#9aa3a5",
    NOT_STARTED: "#9aa3a5",
    UNREADABLE: "#b64242",
    "shelf": "#d7a84d",
}


@dataclass
class StationLayout:
    name: str
    address: str
    x: float
    y: float

    @property
    def is_shelf(self) -> bool:
        return self.name == "shelf"

    @property
    def label(self) -> str:
        return self.name.replace("experiment_", "EXP ").upper()


# ---------------------------------------------------------------------- view

def _grid_step(span: float) -> float:
    for magnitude in (1, 10, 100, 1000, 10000):
        for base in (1, 2, 5):
            if base * magnitude >= span / 12:
                return float(base * magnitude)
    return span


class GantryLiveView(tk.Frame):
    """Notebook page showing where the gantry and experiments are on the bed."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, background="#f4f1ea")
        self.calibration = Calibration()
        self.stations: list[StationLayout] = []
        self.rows: dict[str, dict[str, Any]] = {}
        self.source_var = tk.StringVar(value="simulator")
        self.gantry_address_var = tk.StringVar(value=f"{DEFAULT_HOST}:8100")
        self.bed_width_var = tk.StringVar(value="100")
        self.bed_height_var = tk.StringVar(value="60")
        self.home_x_var = tk.StringVar(value="0")
        self.home_y_var = tk.StringVar(value="0")
        self.ref_x_var = tk.StringVar(value="10")
        self.ref_y_var = tk.StringVar(value="10")
        self.new_name_var = tk.StringVar()
        self.move_x_var = tk.StringVar(value="50")
        self.move_y_var = tk.StringVar(value="30")
        self._move_result: Optional[tuple[str, str]] = None

        self._gantry_raw: Optional[tuple[float, float]] = None
        self._gantry_state: Optional[dict[str, Any]] = None
        self._gantry_problem = ""
        self._statuses: dict[str, dict[str, Any]] = {}
        self._drag: Optional[tuple[str, float, float]] = None
        self._hover: Optional[tuple[float, float]] = None
        self._bed_scale = 1.0
        self._origin = (0.0, 0.0)

        self.poller = NodePoller()
        station_data = self._load_layout()
        self._build()
        for data in station_data:
            self._add_station(StationLayout(**data))
        self._suggest_name()
        self.after_id: Optional[str] = self.after(REFRESH_MS, self._tick)

    # ------------------------------------------------------------ persistence

    def _load_layout(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(LAYOUT_PATH.read_text())
            self.bed_width_var.set(str(data["bed"][0]))
            self.bed_height_var.set(str(data["bed"][1]))
            self.source_var.set("lab" if data.get("gantry_source") in ("lab", "tcp") else "simulator")
            self.gantry_address_var.set(data.get("gantry_address", self.gantry_address_var.get()))
            self.calibration.scale = tuple(data.get("scale", (1.0, 1.0)))
            stations = [
                {key: entry[key] for key in ("name", "address", "x", "y")}
                for entry in data["stations"]
            ]
            if not any(entry["name"] == "shelf" for entry in stations):
                stations.insert(0, dict(DEFAULT_STATIONS[0]))
            return stations
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return [dict(entry) for entry in DEFAULT_STATIONS]

    def save_layout(self) -> None:
        bed = self._bed_size()
        LAYOUT_PATH.write_text(json.dumps({
            "bed": list(bed),
            "gantry_source": self.source_var.get(),
            "gantry_address": self.gantry_address_var.get(),
            "scale": list(self.calibration.scale),
            "stations": [asdict(station) for station in self.stations],
        }, indent=2) + "\n")

    def close(self) -> None:
        if self.after_id is not None:
            self.after_cancel(self.after_id)
            self.after_id = None
        self.poller.close()
        try:
            self.save_layout()
        except OSError:
            pass

    # ----------------------------------------------------------------- layout

    def _section(self, parent: tk.Misc, text: str, hint: str = "") -> tk.Frame:
        frame = tk.Frame(parent, background="#f4f1ea")
        frame.pack(fill="x", pady=(0, 10))
        tk.Label(frame, text=text, font=("Helvetica", 10, "bold"), fg="#6d7779", bg="#f4f1ea").pack(anchor="w")
        if hint:
            tk.Label(frame, text=hint, fg="#7c8788", bg="#f4f1ea", font=("Helvetica", 8), justify="left", wraplength=320).pack(anchor="w", pady=(0, 3))
        return frame

    def _entry(self, parent: tk.Misc, variable: tk.StringVar, width: int) -> tk.Entry:
        return tk.Entry(parent, textvariable=variable, width=width, background="#fffdf8", foreground="#526064", relief="flat", font=("Menlo", 9))

    def _build(self) -> None:
        content = tk.Frame(self, background="#f4f1ea")
        content.pack(fill="both", expand=True, padx=18, pady=14)

        self.canvas = tk.Canvas(content, background="#fffdf8", highlightthickness=1, highlightbackground="#d8d1c5")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<Double-Button-1>", self._move_to_click)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Configure>", lambda _e: self._draw())

        sidebar = tk.Frame(content, width=340, background="#f4f1ea")
        sidebar.pack(side="right", fill="y", padx=(16, 0))
        sidebar.pack_propagate(False)

        # Gantry source
        source = self._section(sidebar, "GANTRY SOURCE", "Simulator reads GET /state from the gantry (python -m gantry) and places the gantry between your boxes. Lab reads GET /position (x, y), which the gantry also serves.")
        source_row = tk.Frame(source, background="#f4f1ea")
        source_row.pack(fill="x")
        for value, text in (("simulator", "Simulator"), ("lab", "Lab (HTTP)")):
            tk.Radiobutton(source_row, text=text, value=value, variable=self.source_var, bg="#f4f1ea", fg="#526064", font=("Helvetica", 9), highlightthickness=0).pack(side="left")
        self._entry(source_row, self.gantry_address_var, 16).pack(side="right")
        self.readout_label = tk.Label(source, text="", anchor="w", fg="#263238", bg="#f4f1ea", font=("Menlo", 9))
        self.readout_label.pack(fill="x", pady=(4, 0))

        # Move
        move = self._section(sidebar, "MOVE GANTRY", "Bed coordinates; sends POST /pos to the gantry address above. Double-click the bed to move there. Refused while the gantry is moving.")
        move_row = tk.Frame(move, background="#f4f1ea")
        move_row.pack(fill="x")
        for text, variable in (("x", self.move_x_var), ("y", self.move_y_var)):
            tk.Label(move_row, text=text, fg="#7c8788", bg="#f4f1ea", font=("Menlo", 9)).pack(side="left", padx=(0, 2))
            entry = self._entry(move_row, variable, 7)
            entry.pack(side="left", padx=(0, 6))
            entry.bind("<Return>", lambda _e: self._move_gantry())
        self.move_button = ttk.Button(move_row, text="Move gantry", command=self._move_gantry)
        self.move_button.pack(side="right")
        self.move_label = tk.Label(move, text="", anchor="w", justify="left", fg="#526064", bg="#f4f1ea", font=("Menlo", 8), wraplength=320)
        self.move_label.pack(fill="x", pady=(2, 0))

        # Homing
        homing = self._section(sidebar, "HOMING", "Jog the gantry to a known spot and capture it. HOME sets the offset; REF (a second spot) sets the scale.")
        for label, x_var, y_var, command in (
            ("Set HOME here", self.home_x_var, self.home_y_var, self._set_home),
            ("Set REF here", self.ref_x_var, self.ref_y_var, self._set_reference),
        ):
            row = tk.Frame(homing, background="#f4f1ea")
            row.pack(fill="x", pady=1)
            ttk.Button(row, text=label, command=command, width=13).pack(side="left")
            tk.Label(row, text="bed", fg="#7c8788", bg="#f4f1ea", font=("Helvetica", 8)).pack(side="left", padx=(6, 2))
            self._entry(row, x_var, 6).pack(side="left")
            tk.Label(row, text=",", fg="#7c8788", bg="#f4f1ea").pack(side="left")
            self._entry(row, y_var, 6).pack(side="left")
        calibration_row = tk.Frame(homing, background="#f4f1ea")
        calibration_row.pack(fill="x", pady=(3, 0))
        self.calibration_label = tk.Label(calibration_row, text="", anchor="w", justify="left", fg="#bf6b2e", bg="#f4f1ea", font=("Menlo", 8))
        self.calibration_label.pack(side="left", fill="x", expand=True)
        ttk.Button(calibration_row, text="Clear", command=self._clear_calibration, width=6).pack(side="right")

        # Bed
        bed = self._section(sidebar, "BED SIZE")
        bed_row = tk.Frame(bed, background="#f4f1ea")
        bed_row.pack(fill="x")
        self._entry(bed_row, self.bed_width_var, 7).pack(side="left")
        tk.Label(bed_row, text="×", fg="#7c8788", bg="#f4f1ea").pack(side="left", padx=3)
        self._entry(bed_row, self.bed_height_var, 7).pack(side="left")
        tk.Label(bed_row, text="bed units (origin bottom-left)", fg="#7c8788", bg="#f4f1ea", font=("Helvetica", 8)).pack(side="left", padx=6)

        # Experiments
        experiments = self._section(sidebar, "EXPERIMENTS", "Drag boxes on the bed to place them. Node is http://host:port, host:port or just port.")
        add_row = tk.Frame(experiments, background="#f4f1ea")
        add_row.pack(fill="x", side="bottom", pady=(5, 0))
        name_entry = self._entry(add_row, self.new_name_var, 18)
        name_entry.pack(side="left", fill="x", expand=True)
        name_entry.bind("<Return>", lambda _e: self._add_from_entry())
        ttk.Button(add_row, text="Add experiment", command=self._add_from_entry).pack(side="right", padx=(5, 0))

        list_frame = tk.Frame(experiments, background="#fffdf8")
        list_frame.pack(fill="x")
        self.list_canvas = tk.Canvas(list_frame, height=150, background="#fffdf8", highlightthickness=0)
        list_bar = ttk.Scrollbar(list_frame, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=list_bar.set)
        self.list_canvas.pack(side="left", fill="both", expand=True)
        list_bar.pack(side="right", fill="y")
        self.rows_frame = tk.Frame(self.list_canvas, background="#fffdf8")
        self.list_canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", lambda _e: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")))
        for column, text in enumerate(("NAME", "NODE", "X", "Y", "")):
            tk.Label(self.rows_frame, text=text, fg="#7c8788", bg="#fffdf8", font=("Helvetica", 8, "bold")).grid(row=0, column=column, sticky="w", padx=2)

        self.error_label = tk.Label(sidebar, text="", anchor="w", justify="left", fg="#b64242", bg="#f4f1ea", font=("Helvetica", 8), wraplength=320)
        self.error_label.pack(fill="x")

    # ------------------------------------------------------------ station rows

    def _station(self, name: str) -> Optional[StationLayout]:
        return next((station for station in self.stations if station.name == name), None)

    def _add_station(self, station: StationLayout) -> None:
        self.stations.append(station)
        row_index = len(self.stations)
        vars_ = {
            "address": tk.StringVar(value=station.address),
            "x": tk.StringVar(value=f"{station.x:g}"),
            "y": tk.StringVar(value=f"{station.y:g}"),
        }
        widgets = [
            tk.Label(self.rows_frame, text=station.label, width=8, anchor="w", fg="#526064", bg="#fffdf8", font=("Menlo", 9, "bold")),
        ]
        if station.is_shelf:
            widgets.append(tk.Label(self.rows_frame, text="--", fg="#9aa3a5", bg="#fffdf8", font=("Menlo", 9)))
        else:
            widgets.append(self._entry(self.rows_frame, vars_["address"], 15))
        widgets.append(self._entry(self.rows_frame, vars_["x"], 5))
        widgets.append(self._entry(self.rows_frame, vars_["y"], 5))
        if not station.is_shelf:
            remove = tk.Label(self.rows_frame, text="✕", fg="#b64242", bg="#fffdf8", cursor="hand2", font=("Helvetica", 10))
            remove.bind("<Button-1>", lambda _e, name=station.name: self._remove_station(name))
            widgets.append(remove)
        for column, widget in enumerate(widgets):
            widget.grid(row=row_index, column=column, sticky="w", padx=2, pady=1)
        for key, variable in vars_.items():
            variable.trace_add("write", lambda *_a, name=station.name: self._on_row_edit(name))
        self.rows[station.name] = {"vars": vars_, "widgets": widgets}

    def _remove_station(self, name: str) -> None:
        station = self._station(name)
        if station is None or station.is_shelf:
            return
        self.stations.remove(station)
        for widget in self.rows.pop(name)["widgets"]:
            widget.destroy()
        self._suggest_name()
        self._draw()

    def _on_row_edit(self, name: str) -> None:
        station = self._station(name)
        if station is None:
            return
        vars_ = self.rows[name]["vars"]
        station.address = vars_["address"].get().strip()
        try:
            station.x = float(vars_["x"].get())
            station.y = float(vars_["y"].get())
        except ValueError:
            pass
        self._draw()

    def _suggest_name(self) -> None:
        names = {station.name for station in self.stations}
        for letter in "abcdefghijklmnopqrstuvwxyz":
            if f"experiment_{letter}" not in names:
                self.new_name_var.set(f"experiment_{letter}")
                return
        self.new_name_var.set("")

    def _add_from_entry(self) -> None:
        name = self.new_name_var.get().strip()
        if not name:
            self._set_error("Enter a name for the new experiment.")
            return
        if self._station(name) is not None:
            self._set_error(f"{name} already exists.")
            return
        width, height = self._bed_size()
        self._add_station(StationLayout(name, "", round(width / 2, 1), round(height / 2, 1)))
        self._set_error("")
        self._suggest_name()
        self.list_canvas.update_idletasks()
        self.list_canvas.yview_moveto(1.0)
        self._draw()

    # ------------------------------------------------------------------ moving

    def _move_to_click(self, event: tk.Event) -> None:
        if self._station_at(event.x, event.y) is not None:
            return
        bed_w, bed_h = self._bed_size()
        bx, by = self._to_bed(event.x, event.y)
        if not (0 <= bx <= bed_w and 0 <= by <= bed_h):
            return
        self.move_x_var.set(f"{bx:.1f}")
        self.move_y_var.set(f"{by:.1f}")
        self._move_gantry()

    def _move_gantry(self) -> None:
        try:
            bed = (float(self.move_x_var.get()), float(self.move_y_var.get()))
        except ValueError:
            self.move_label.config(text="x and y must be numbers", fg="#b64242")
            return
        # The gantry moves in its own (raw) coordinates; HOME/REF calibration maps bed to raw.
        x, y = self.calibration.to_raw(bed)
        address = self.gantry_address_var.get().strip()
        self.move_button.config(state="disabled")
        self.move_label.config(text=f"Sending move to bed ({bed[0]:g}, {bed[1]:g})...", fg="#526064")

        def send() -> None:
            try:
                client = NodeClient(address, token=os.environ.get(TOKEN_ENV) or None, timeout=2.0)
                client._request("POST", "/pos", {"x": x, "y": y})
                result = (f"Moving to bed ({bed[0]:g}, {bed[1]:g})", "#4e9560")
            except NodeUnreachable:
                result = ("Gantry NOT STARTED: nothing at that address", "#b64242")
            except NodeError as error:
                result = (f"Refused: {error.detail}", "#b64242")
            except ValueError as error:
                result = (f"UNABLE TO READ: {error}", "#b64242")
            self._move_result = result

        threading.Thread(target=send, name="gantry-move", daemon=True).start()

    def _show_move_result(self) -> None:
        if self._move_result is not None:
            text, color = self._move_result
            self._move_result = None
            self.move_label.config(text=text, fg=color)
            self.move_button.config(state="normal")

    def _set_error(self, text: str) -> None:
        self.error_label.config(text=text)

    # ---------------------------------------------------------------- homing

    def _entered_point(self, x_var: tk.StringVar, y_var: tk.StringVar) -> tuple[float, float]:
        try:
            return float(x_var.get()), float(y_var.get())
        except ValueError:
            raise ValueError("Bed coordinates must be numbers.") from None

    def _set_home(self) -> None:
        try:
            if self._gantry_raw is None:
                raise ValueError("No live gantry position to home against.")
            self.calibration.set_home(self._gantry_raw, self._entered_point(self.home_x_var, self.home_y_var))
            self._set_error("")
        except ValueError as error:
            self._set_error(str(error))

    def _set_reference(self) -> None:
        try:
            if self._gantry_raw is None:
                raise ValueError("No live gantry position to calibrate against.")
            self.calibration.set_reference(self._gantry_raw, self._entered_point(self.ref_x_var, self.ref_y_var))
            self._set_error("")
        except ValueError as error:
            self._set_error(str(error))

    def _clear_calibration(self) -> None:
        self.calibration = Calibration()
        self._set_error("")

    # --------------------------------------------------------------- polling

    def _tick(self) -> None:
        self.after_id = None
        try:
            simulator = self.source_var.get() == "simulator"
            self._push_targets(simulator)
            statuses, (payload, problem, detail) = self.poller.snapshot()
            raw, state = None, None
            if not problem and payload is not None:
                try:
                    if simulator:
                        state = payload if isinstance(payload, dict) else None
                        if state is None:
                            raise TypeError
                        raw = self._simulated_position(state)
                        if raw is None:
                            detail = "gantry location not on the bed layout"
                    else:
                        raw = position_from_payload(payload)
                except (KeyError, TypeError, ValueError):
                    raw, state, problem = None, None, UNREADABLE
                    detail = "unexpected response"
            error_text = f"{problem_label(problem)}  {detail}" if problem else detail
            self._statuses = statuses
            self._gantry_raw = raw
            self._gantry_state = state
            self._gantry_problem = problem_label(problem) if problem else ("NO ADDRESS" if payload is None else "OFF LAYOUT")
            self._refresh_labels(error_text)
            self._show_move_result()
            if self.winfo_ismapped():
                self._draw()
        finally:
            self.after_id = self.after(REFRESH_MS, self._tick)

    def _push_targets(self, simulator: bool) -> None:
        targets = {
            station.name: station.address
            for station in self.stations
            if not station.is_shelf and station.address
        }
        self.poller.set_targets(
            targets,
            self.gantry_address_var.get().strip() or None,
            "/state" if simulator else "/position",
        )

    def _simulated_position(self, state: dict[str, Any]) -> Optional[tuple[float, float]]:
        """Interpolate the gantry between stations from its /state; reported as raw = bed."""
        # Stations come from this layout; anything else (the gantry's home) from the gantry.
        points = {name: tuple(point) for name, point in (state.get("locations") or {}).items()}
        points.update({station.name: (station.x, station.y) for station in self.stations})
        snapshot = state["transport"]
        if state.get("manual_move") or state["position"] not in points:
            # Manual moves are free x, y: take the gantry's own coordinates.
            coordinates = state.get("coordinates")
            return (float(coordinates[0]), float(coordinates[1])) if coordinates else None
        if snapshot is None:
            return points.get(state["position"])
        start_name = snapshot["position"] if snapshot["phase"] == "pickup" else snapshot["source"]
        end_name = snapshot["source"] if snapshot["phase"] == "pickup" else snapshot["destination"]
        if start_name not in points or end_name not in points:
            return None
        start, end = points[start_name], points[end_name]
        progress = min(1.0, max(0.0, (state["now"] - snapshot["started_at"]) / state["movement_time"]))
        return (
            start[0] + (end[0] - start[0]) * progress,
            start[1] + (end[1] - start[1]) * progress,
        )

    def _refresh_labels(self, error_text: str) -> None:
        raw = self._gantry_raw
        if raw is None:
            self.readout_label.config(text=(error_text or "NO GANTRY ADDRESS")[:52], fg="#b64242")
        else:
            bed = self.calibration.to_bed(raw)
            self.readout_label.config(
                text=f"RAW {raw[0]:.1f}, {raw[1]:.1f}  →  BED {bed[0]:.1f}, {bed[1]:.1f}",
                fg="#263238",
            )
        scale = self.calibration.scale
        if self.calibration.homed:
            self.calibration_label.config(text=f"HOMED  scale {scale[0]:.4g}, {scale[1]:.4g}", fg="#4e9560")
        else:
            self.calibration_label.config(text="NOT HOMED  showing raw coordinates", fg="#bf6b2e")

    # --------------------------------------------------------------- drawing

    def _bed_size(self) -> tuple[float, float]:
        try:
            width = float(self.bed_width_var.get())
            height = float(self.bed_height_var.get())
            if width > 0 and height > 0:
                return width, height
        except ValueError:
            pass
        return 100.0, 60.0

    def _to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return self._origin[0] + x * self._bed_scale, self._origin[1] - y * self._bed_scale

    def _to_bed(self, cx: float, cy: float) -> tuple[float, float]:
        return (cx - self._origin[0]) / self._bed_scale, (self._origin[1] - cy) / self._bed_scale

    def _draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 200)
        bed_w, bed_h = self._bed_size()
        top = BED_MARGIN + 10
        self._bed_scale = min((width - 2 * BED_MARGIN) / bed_w, (height - top - BED_MARGIN) / bed_h)
        self._origin = (BED_MARGIN, top + bed_h * self._bed_scale)
        left, bottom = self._to_canvas(0, 0)
        right, upper = self._to_canvas(bed_w, bed_h)

        canvas.create_text(20, 20, text="GANTRY BED  /  LIVE POSITION", anchor="w", fill="#6d7779", font=("Helvetica", 10, "bold"))
        canvas.create_text(width - 20, 20, text="TEAL IDLE   RED BUSY   AMBER DONE   GREY NOT STARTED", anchor="e", fill="#7c8788", font=("Menlo", 8, "bold"))
        canvas.create_rectangle(left, upper, right, bottom, fill="#f8f5ee", outline="#d8d1c5", width=2)

        step = _grid_step(max(bed_w, bed_h))
        value = 0.0
        while value <= bed_w + 1e-9:
            x, _ = self._to_canvas(value, 0)
            canvas.create_line(x, upper, x, bottom, fill="#ebe5d9")
            canvas.create_text(x, bottom + 10, text=f"{value:g}", fill="#9aa3a5", font=("Menlo", 7))
            value += step
        value = 0.0
        while value <= bed_h + 1e-9:
            _, y = self._to_canvas(0, value)
            canvas.create_line(left, y, right, y, fill="#ebe5d9")
            canvas.create_text(left - 8, y, text=f"{value:g}", anchor="e", fill="#9aa3a5", font=("Menlo", 7))
            value += step

        if self.calibration.homed:
            hx, hy = self._to_canvas(*self.calibration.home_bed)
            canvas.create_line(hx - 7, hy, hx + 7, hy, fill="#4e9560", width=2)
            canvas.create_line(hx, hy - 7, hx, hy + 7, fill="#4e9560", width=2)
            canvas.create_text(hx + 9, hy + 9, text="HOME", anchor="w", fill="#4e9560", font=("Menlo", 7, "bold"))

        for station in self.stations:
            self._draw_station(station)
        self._draw_gantry(upper, bottom)

        if self._hover is not None:
            bx, by = self._hover
            text = f"CURSOR  bed {bx:.1f}, {by:.1f}"
            if self.calibration.homed:
                rx, ry = self.calibration.to_raw(self._hover)
                text += f"   raw {rx:.1f}, {ry:.1f}"
            canvas.create_text(20, height - 14, text=text, anchor="w", fill="#7c8788", font=("Menlo", 8))

    def _draw_station(self, station: StationLayout) -> None:
        canvas = self.canvas
        x, y = self._to_canvas(station.x, station.y)
        if station.is_shelf:
            state, color, detail = "SHELF", STATE_COLORS["shelf"], ""
        elif not station.address:
            state, color, detail = "NO PORT", STATE_COLORS["offline"], ""
        else:
            status = self._statuses.get(station.name, {"state": NOT_STARTED})
            state = status["state"]
            color = STATE_COLORS.get(state, STATE_COLORS["offline"])
            detail = status.get("sample_id") or ""
            if state == "busy" and isinstance(status.get("remaining"), (int, float)):
                detail = f"{detail} {status['remaining']:.1f}s"
            state = problem_label(state)
        outline_width = 3 if self._drag and self._drag[0] == station.name else 2
        canvas.create_rectangle(
            x - STATION_HALF_WIDTH, y - STATION_HALF_HEIGHT, x + STATION_HALF_WIDTH, y + STATION_HALF_HEIGHT,
            fill="#fffdf8", outline=color, width=outline_width,
        )
        canvas.create_text(x, y - 20, text=station.label, fill="#263238", font=("Helvetica", 9, "bold"))
        canvas.create_text(x, y - 6, text=f"{state} {detail}".strip(), fill=color, font=("Menlo", 8, "bold"))
        port = f":{address_port(station.address)}" if station.address else ""
        canvas.create_text(x, y + 7, text=f"({station.x:g}, {station.y:g}) {port}".strip(), fill="#7c8788", font=("Menlo", 7))
        if self.calibration.homed:
            rx, ry = self.calibration.to_raw((station.x, station.y))
            canvas.create_text(x, y + 20, text=f"go raw {rx:.1f}, {ry:.1f}", fill="#4e9560", font=("Menlo", 7))

    def _draw_gantry(self, upper: float, bottom: float) -> None:
        canvas = self.canvas
        if self._gantry_raw is None:
            canvas.create_text(self.canvas.winfo_width() / 2, upper - 14, text=f"GANTRY {self._gantry_problem}", fill="#b64242", font=("Menlo", 9, "bold"))
            return
        bed = self.calibration.to_bed(self._gantry_raw)
        bed_w, bed_h = self._bed_size()
        clamped = (min(max(bed[0], 0.0), bed_w), min(max(bed[1], 0.0), bed_h))
        x, y = self._to_canvas(*clamped)
        canvas.create_line(x, upper, x, bottom, fill="#263238", width=4)
        canvas.create_rectangle(x - 6, upper - 6, x + 6, upper, fill="#263238", outline="")
        canvas.create_rectangle(x - 6, bottom, x + 6, bottom + 6, fill="#263238", outline="")
        canvas.create_rectangle(x - 13, y - 13, x + 13, y + 13, fill="#263238", outline="#a5d6a7", width=2)
        canvas.create_line(x - 5, y, x + 5, y, fill="#a5d6a7", width=2)
        canvas.create_line(x, y - 5, x, y + 5, fill="#a5d6a7", width=2)
        label = f"GANTRY {bed[0]:.1f}, {bed[1]:.1f}"
        if clamped != bed:
            label += "  (OFF BED)"
        anchor, offset = ("w", 20) if x < self.canvas.winfo_width() - 180 else ("e", -20)
        canvas.create_text(x + offset, y - 6, text=label, anchor=anchor, fill="#263238", font=("Menlo", 8, "bold"))
        if self._gantry_state is not None:
            snapshot = self._gantry_state.get("transport")
            if isinstance(snapshot, dict) and snapshot.get("phase") == "delivery":
                canvas.create_text(x + offset, y + 8, text=snapshot["substrate_id"], anchor=anchor, fill="#bf6b2e", font=("Menlo", 8, "bold"))

    # ----------------------------------------------------------- interaction

    def _station_at(self, cx: float, cy: float) -> Optional[StationLayout]:
        for station in reversed(self.stations):
            x, y = self._to_canvas(station.x, station.y)
            if abs(cx - x) <= STATION_HALF_WIDTH and abs(cy - y) <= STATION_HALF_HEIGHT:
                return station
        return None

    def _start_drag(self, event: tk.Event) -> None:
        station = self._station_at(event.x, event.y)
        if station is None:
            return
        bx, by = self._to_bed(event.x, event.y)
        self._drag = (station.name, bx - station.x, by - station.y)

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag is None:
            return
        name, offset_x, offset_y = self._drag
        bx, by = self._to_bed(event.x, event.y)
        bed_w, bed_h = self._bed_size()
        x = round(min(max(bx - offset_x, 0.0), bed_w), 1)
        y = round(min(max(by - offset_y, 0.0), bed_h), 1)
        self._hover = (bx, by)
        vars_ = self.rows[name]["vars"]
        vars_["x"].set(f"{x:g}")
        vars_["y"].set(f"{y:g}")

    def _end_drag(self, _event: tk.Event) -> None:
        self._drag = None
        self._draw()

    def _on_hover(self, event: tk.Event) -> None:
        self._hover = self._to_bed(event.x, event.y)
        over_station = self._station_at(event.x, event.y) is not None
        self.canvas.config(cursor="fleur" if over_station or self._drag else "")

    def _on_leave(self, _event: tk.Event) -> None:
        self._hover = None
