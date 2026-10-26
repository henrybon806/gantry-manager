"""A fake gantry with a draggable dot.

    .venv/bin/python sim/fake_gantry.py

Opens http://127.0.0.1:8100 in your browser. Drag the dot to move the gantry.
The same data is served as JSON at /position and /status.
Standard library only, no installs needed.
"""

import json
import math
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8100
X_MAX, Y_MAX = 100.0, 60.0

lock = threading.Lock()
state = {"x": 50.0, "y": 30.0, "z": 0.0, "wander": False, "updated_at": time.time()}


def position():
    """Current position. If wander is on, it drifts around like the original fake gantry."""
    if state["wander"]:
        t = time.monotonic() / 4
        return {"x": 50 + 40 * math.cos(t), "y": 30 + 20 * math.sin(2 * t), "z": 0.0}
    return {"x": state["x"], "y": state["y"], "z": state["z"]}


def status():
    with lock:
        return {
            "name": "gantry",
            "status": "wandering" if state["wander"] else "idle",
            "position": position(),
            "updated_at": state["updated_at"],
        }


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Fake gantry</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 24px; background: #f5f5f5; }
  .row { display: flex; gap: 24px; flex-wrap: wrap; align-items: flex-start; }
  canvas { background: #fff; border: 2px solid #333; border-radius: 6px; touch-action: none; cursor: grab; }
  pre { background: #111; color: #7CFC9A; padding: 12px; border-radius: 6px; min-width: 320px; margin: 0; }
  label { display: block; margin-top: 12px; }
</style>
</head>
<body>
<h2>Fake gantry</h2>
<div class="row">
  <div>
    <canvas id="c" width="600" height="360"></canvas>
    <label><input type="checkbox" id="wander"> Wander on its own</label>
  </div>
  <pre id="out">loading...</pre>
</div>
<script>
const W = 600, H = 360, XMAX = 100, YMAX = 60;
const sx = W / XMAX, sy = H / YMAX;
const c = document.getElementById("c"), ctx = c.getContext("2d");
const out = document.getElementById("out"), wander = document.getElementById("wander");
let pos = {x: 50, y: 30}, dragging = false;

function draw() {
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "#e0e0e0";
  for (let x = 0; x <= XMAX; x += 10) { ctx.beginPath(); ctx.moveTo(x * sx, 0); ctx.lineTo(x * sx, H); ctx.stroke(); }
  for (let y = 0; y <= YMAX; y += 10) { ctx.beginPath(); ctx.moveTo(0, H - y * sy); ctx.lineTo(W, H - y * sy); ctx.stroke(); }
  ctx.fillStyle = "#d33";
  ctx.beginPath(); ctx.arc(pos.x * sx, H - pos.y * sy, 10, 0, 7); ctx.fill();
}

function fromEvent(e) {
  const r = c.getBoundingClientRect();
  const x = (e.clientX - r.left) / sx, y = YMAX - (e.clientY - r.top) / sy;
  return {x: Math.max(0, Math.min(XMAX, x)), y: Math.max(0, Math.min(YMAX, y))};
}

async function send(p) {
  await fetch("/position", {method: "POST", body: JSON.stringify(p)});
}

c.addEventListener("pointerdown", e => {
  dragging = true; c.setPointerCapture(e.pointerId); c.style.cursor = "grabbing";
  wander.checked = false; pos = fromEvent(e); draw(); send(pos);
});
c.addEventListener("pointermove", e => { if (dragging) { pos = fromEvent(e); draw(); send(pos); } });
c.addEventListener("pointerup", () => { dragging = false; c.style.cursor = "grab"; });
wander.addEventListener("change", () =>
  fetch("/wander", {method: "POST", body: JSON.stringify({wander: wander.checked})}));

async function poll() {
  try {
    const s = await (await fetch("/status")).json();
    out.textContent = "GET /status\\n" + JSON.stringify(s, null, 2);
    if (!dragging) { pos = s.position; wander.checked = s.status === "wandering"; draw(); }
  } catch (e) { out.textContent = "server not reachable"; }
}
draw(); poll(); setInterval(poll, 150);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif self.path == "/position":
            with lock:
                self._send(json.dumps(position()))
        elif self.path == "/status":
            self._send(json.dumps(status()))
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        with lock:
            if self.path == "/position":
                state["wander"] = False
                state["x"] = max(0.0, min(X_MAX, float(body.get("x", state["x"]))))
                state["y"] = max(0.0, min(Y_MAX, float(body.get("y", state["y"]))))
                state["z"] = float(body.get("z", state["z"]))
            elif self.path == "/wander":
                if state["wander"] is False and body.get("wander"):
                    pass
                state["wander"] = bool(body.get("wander"))
            else:
                self.send_error(404)
                return
            state["updated_at"] = time.time()
        self._send(json.dumps({"ok": True}))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Fake gantry running at {url} (Ctrl+C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass