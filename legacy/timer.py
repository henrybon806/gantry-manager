import tkinter as tk
import socketserver
import threading
import time

HOST = "127.0.0.1"
PORT = 5050
DEFAULT_DURATION = 60.0  # seconds


class CountdownTimer:
    """Thread-safe countdown timer."""

    def __init__(self, duration=DEFAULT_DURATION):
        self._lock = threading.Lock()
        self._duration = duration      # total time to count down from
        self._remaining = duration     # time left when paused
        self._running = False
        self._deadline = None          # monotonic time when it should hit 0

    def start(self, seconds=None):
        with self._lock:
            if seconds is not None:
                self._duration = seconds
                self._remaining = seconds
            if not self._running and self._remaining > 0:
                self._running = True
                self._deadline = time.monotonic() + self._remaining

    def pause(self):
        with self._lock:
            if self._running:
                self._remaining = max(0.0, self._deadline - time.monotonic())
                self._running = False
                self._deadline = None

    def reset(self, seconds=None):
        with self._lock:
            if seconds is not None:
                self._duration = seconds
            self._remaining = self._duration
            self._running = False
            self._deadline = None

    def remaining(self):
        with self._lock:
            if self._running:
                r = self._deadline - time.monotonic()
                if r <= 0:
                    self._running = False
                    self._deadline = None
                    self._remaining = 0.0
                    return 0.0
                return r
            return self._remaining

    def status(self):
        r = self.remaining()
        with self._lock:
            state = "RUNNING" if self._running else ("EXPIRED" if r <= 0 else "PAUSED")
        return f"{state} {r:.2f}"


class CommandHandler(socketserver.StreamRequestHandler):
    """Handles newline-terminated commands, one connection can send many."""

    def handle(self):
        timer: CountdownTimer = self.server.timer
        while True:
            line = self.rfile.readline()
            if not line:
                break
            parts = line.decode("utf-8", errors="ignore").strip().split()
            if not parts:
                continue
            cmd = parts[0].upper()
            arg = parts[1] if len(parts) > 1 else None

            try:
                if cmd == "START":
                    seconds = float(arg) if arg is not None else None
                    timer.start(seconds)
                    response = "OK STARTED"
                elif cmd in ("STOP", "PAUSE"):
                    timer.pause()
                    response = "OK PAUSED"
                elif cmd == "RESET":
                    seconds = float(arg) if arg is not None else None
                    timer.reset(seconds)
                    response = "OK RESET"
                elif cmd == "STATUS":
                    response = timer.status()
                elif cmd in ("QUIT", "EXIT"):
                    self.wfile.write(b"OK BYE\n")
                    break
                else:
                    response = f"ERROR unknown command: {cmd}"
            except ValueError:
                response = "ERROR invalid number"

            self.wfile.write((response + "\n").encode("utf-8"))


class TimerTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_cls, timer: CountdownTimer):
        super().__init__(server_address, handler_cls)
        self.timer = timer


class TimerApp:
    def __init__(self, root, timer: CountdownTimer):
        self.root = root
        self.timer = timer

        root.title("Network-Controlled Countdown Timer")
        root.geometry("340x230")

        self.time_label = tk.Label(root, text="00:00", font=("Consolas", 40))
        self.time_label.pack(pady=10)

        self.status_label = tk.Label(root, text="PAUSED", font=("Arial", 12))
        self.status_label.pack()

        entry_frame = tk.Frame(root)
        entry_frame.pack(pady=8)
        tk.Label(entry_frame, text="Seconds:").pack(side="left")
        self.duration_entry = tk.Entry(entry_frame, width=8)
        self.duration_entry.insert(0, str(int(DEFAULT_DURATION)))
        self.duration_entry.pack(side="left", padx=5)

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="Start", width=8, command=self._on_start).grid(row=0, column=0, padx=5)
        tk.Button(btn_frame, text="Pause", width=8, command=self.timer.pause).grid(row=0, column=1, padx=5)
        tk.Button(btn_frame, text="Reset", width=8, command=self._on_reset).grid(row=0, column=2, padx=5)

        info = tk.Label(root, text=f"TCP listening on {HOST}:{PORT}", font=("Arial", 9), fg="gray")
        info.pack(side="bottom", pady=5)

        self._update_display()

    def _on_start(self):
        try:
            seconds = float(self.duration_entry.get())
        except ValueError:
            seconds = None
        self.timer.start(seconds)

    def _on_reset(self):
        try:
            seconds = float(self.duration_entry.get())
        except ValueError:
            seconds = None
        self.timer.reset(seconds)

    def _update_display(self):
        remaining = self.timer.remaining()
        minutes, seconds = divmod(int(remaining + 0.5), 60)
        self.time_label.config(text=f"{minutes:02d}:{seconds:02d}")

        status_text = self.timer.status().split()[0]
        self.status_label.config(text=status_text)
        if status_text == "EXPIRED":
            self.time_label.config(fg="red")
        else:
            self.time_label.config(fg="black")

        self.root.after(100, self._update_display)


def main():
    timer = CountdownTimer()

    server = TimerTCPServer((HOST, PORT), CommandHandler, timer)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    root = tk.Tk()
    TimerApp(root, timer)

    def on_close():
        server.shutdown()
        server.server_close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()