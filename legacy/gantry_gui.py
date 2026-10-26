"""Old Tkinter window for the gantry (the web GUI, python -m web, replaces it).

    python -m legacy.gantry_gui

Start each experiment and the gantry in their own terminals (see sim/
and python -m gantry). The window polls the addresses typed into it, shows
NOT STARTED or UNABLE TO READ for anything it cannot read, and can move the
gantry with POST /pos.
"""

from __future__ import annotations

import tkinter as tk

from legacy.gantry_visualization import GantryLiveView


WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 820


def main() -> None:
    root = tk.Tk()
    root.title("Gantry Lab")
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
    root.minsize(1050, 760)
    root.configure(background="#f4f1ea")

    header = tk.Frame(root, background="#263238", height=56)
    header.pack(fill="x")
    header.pack_propagate(False)
    tk.Label(
        header,
        text="GANTRY LAB",
        font=("Helvetica", 18, "bold"),
        fg="#f4f1ea",
        bg="#263238",
    ).pack(side="left", padx=28)

    view = GantryLiveView(root)
    view.pack(fill="both", expand=True)
    root.protocol("WM_DELETE_WINDOW", lambda: (view.close(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
