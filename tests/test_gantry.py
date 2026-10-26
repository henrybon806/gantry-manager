import json
import random
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sim.node import TimedExperiment
from gantry.scheduler import NOT_STARTED, UNREADABLE, ExperimentClient, GantryManager, RemoteExperiment, parse_route
from gantry.server import Scheduler, build_manager, create_app, settings_from_presets
from gantry.substrates import Substrate
from helpers.calibration import Calibration
from helpers.polling import NodePoller, address_port, position_from_payload, read_status
from helpers.presets import default_presets, load_presets
from labnode.server import create_app as create_node_app


def validate_state(payload):
    """The /state fields the bed view reads."""
    keys = ("now", "completed", "position", "movement_time", "transport", "coordinates", "locations", "substrates")
    missing = [key for key in keys if key not in payload]
    if missing:
        raise ValueError(f"state missing {missing}")
    return payload


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveNode:
    """Serve a TimedExperiment over real HTTP, like `python sim/node.py` in a terminal."""

    def __init__(self, letter, duration):
        import uvicorn

        self.port = free_port()
        self.node = TimedExperiment(f"experiment_{letter}", duration)
        config = uvicorn.Config(create_node_app(self.node), host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)

    @property
    def address(self):
        return f"127.0.0.1:{self.port}"

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def serve_text(body, status=200):
    """A stdlib server that answers every GET with ``body``."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class GantrySimulationTests(unittest.TestCase):
    def test_new_substrate_can_join_a_running_queue(self):
        client = ExperimentClient("experiment_a", duration=3.0)
        first = Substrate("sample_1", ("experiment_a",))
        manager = GantryManager([client], [first], movement_time=1.0)
        manager.tick(0.0)

        manager.add_substrate(Substrate("sample_2", ("experiment_a",)))

        self.assertEqual(manager.queue_snapshot[0]["substrate_id"], "sample_2")
        self.assertEqual(manager.queue_snapshot[0]["state"], "queued")
        manager.run_until_complete()
        self.assertTrue(manager.completed)

    def test_simulated_client_reports_state(self):
        client = ExperimentClient("experiment_a", duration=2.0)

        self.assertEqual(client.poll(0.0), "idle")
        client.start(Substrate("sample_1", ("experiment_a",)), now=0.0)
        self.assertEqual(client.poll(1.0), "busy")
        self.assertEqual(client.poll(2.0), "done")

    def test_routes_are_configurable_per_sample(self):
        known = {"experiment_a", "experiment_b", "experiment_c"}

        self.assertEqual(parse_route("A, b,C", known), ("experiment_a", "experiment_b", "experiment_c"))
        with self.assertRaises(ValueError):
            parse_route("A,D", known)
        with self.assertRaises(ValueError):
            parse_route(" , ", known)

    def test_three_random_experiments_preserve_order_and_single_occupancy(self):
        random_generator = random.Random(7)
        experiment_names = ["experiment_a", "experiment_b", "experiment_c"]
        clients = [
            ExperimentClient(name, random_generator.uniform(2.0, 4.0))
            for name in experiment_names
        ]
        substrates = [
            Substrate("sample_1", ("experiment_a", "experiment_b", "experiment_c")),
            Substrate("sample_2", ("experiment_c", "experiment_a")),
            Substrate("sample_3", ("experiment_b", "experiment_c")),
        ]
        manager = GantryManager(clients, substrates, movement_time=1.0)
        threads_before = threading.active_count()

        manager.run_until_complete()
        self.assertEqual(threading.active_count(), threads_before)

        self.assertTrue(manager.completed)
        for substrate in substrates:
            self.assertTrue(substrate.completed)
            self.assertEqual(list(substrate.experiment_order), substrate.history)
            self.assertEqual(substrate.location, "shelf")
        self.assertTrue(all(not client.busy for client in clients))

        self.assertTrue(any(
            event[1] == "client_finished" and event[2].endswith(":True")
            for event in manager.events
        ))
        self.assertTrue(any(event[1] == "gantry_picked_up" for event in manager.events))

        movement_events = [
            event for event in manager.events
            if event[1] in {"gantry_departed", "gantry_picked_up", "gantry_delivered"}
        ]
        for start, finish in zip(movement_events[::3], movement_events[1::3]):
            self.assertEqual(start[1], "gantry_departed")
            self.assertEqual(finish[1], "gantry_picked_up")
            self.assertEqual(finish[0] - start[0], manager.movement_time)
        for start, finish in zip(movement_events[1::3], movement_events[2::3]):
            self.assertEqual(finish[1], "gantry_delivered")
            self.assertEqual(finish[0] - start[0], manager.movement_time)


    def test_two_point_homing_maps_raw_to_bed_and_back(self):
        calibration = Calibration()
        self.assertEqual(calibration.to_bed((5.0, 7.0)), (5.0, 7.0))

        calibration.set_home((1000.0, 2000.0), (0.0, 0.0))
        calibration.set_reference((1800.0, 1200.0), (10.0, 10.0))

        self.assertEqual(calibration.scale, (10.0 / 800.0, -10.0 / 800.0))
        self.assertEqual(calibration.to_bed((1400.0, 1600.0)), (5.0, 5.0))
        self.assertEqual(calibration.to_raw((5.0, 5.0)), (1400.0, 1600.0))

        # Re-homing after a power cycle keeps the measured scale.
        calibration.set_home((0.0, 0.0), (0.0, 0.0))
        self.assertEqual(calibration.to_bed((800.0, -800.0)), (10.0, 10.0))

        with self.assertRaises(ValueError):
            calibration.set_reference((0.0, 50.0), (10.0, 10.0))
        with self.assertRaises(ValueError):
            Calibration().set_reference((1.0, 1.0), (10.0, 10.0))

    def test_position_payload_and_node_ports(self):
        self.assertEqual(position_from_payload({"x": 1.5, "y": -2, "z": 9}), (1.5, -2.0))
        with self.assertRaises(KeyError):
            position_from_payload({"x": 1})
        self.assertEqual(address_port("5051"), "5051")
        self.assertEqual(address_port("http://10.0.0.2:8000"), "8000")
        self.assertEqual(address_port("not a node"), "?")

    def test_status_reads_label_not_started_and_unreadable(self):
        self.assertEqual(read_status(f"127.0.0.1:{free_port()}")["state"], NOT_STARTED)
        self.assertEqual(read_status("not a node")["state"], UNREADABLE)
        for body, status in ((b"<html>hi</html>", 200), (b'{"x": 1}', 200), (b'{"detail": "nope"}', 500)):
            server = serve_text(body, status)
            try:
                self.assertEqual(read_status(f"127.0.0.1:{server.server_address[1]}")["state"], UNREADABLE)
            finally:
                server.shutdown()
                server.server_close()

    def test_remote_experiment_waits_for_a_node_that_is_not_started(self):
        client = RemoteExperiment("experiment_a", f"127.0.0.1:{free_port()}")
        manager = GantryManager([client], [Substrate("sample_1", ("experiment_a",))], movement_time=0.05)

        manager.tick(0.0)

        self.assertEqual(client.status["state"], NOT_STARTED)
        self.assertTrue(client.busy)
        self.assertIsNone(manager.transport_snapshot)

    def test_scheduler_drives_separately_started_nodes(self):
        nodes = [LiveNode("a", 0.2), LiveNode("b", 0.2)]
        try:
            manager = build_manager(
                [f"A={nodes[0].address}@30,0", f"B={nodes[1].address}@60,0"], ["A,B", "B"], movement_time=0.05,
            )
            scheduler = Scheduler(manager)
            scheduler.start()
            try:
                deadline = time.monotonic() + 10
                while not scheduler.snapshot()["completed"] and time.monotonic() < deadline:
                    time.sleep(0.05)
                state = validate_state(scheduler.snapshot())
            finally:
                scheduler.stop()
            self.assertTrue(state["completed"])
            self.assertEqual(
                [substrate["location"] for substrate in state["substrates"]], ["shelf", "shelf"],
            )
            self.assertEqual(nodes[0].node.status()["state"], "idle")
            self.assertEqual(state["clients"]["experiment_a"]["state"], "idle")
        finally:
            for node in nodes:
                node.close()

    def test_gantry_node_serves_state(self):
        from fastapi.testclient import TestClient

        manager = build_manager([f"A=127.0.0.1:{free_port()}@30,40"], ["A"], movement_time=1.0, home="0,0", shelf="10,20")
        client = TestClient(create_app(Scheduler(manager)))

        pos = client.get("/pos").json()
        self.assertEqual((pos["x"], pos["y"], pos["at"], pos["moving"]), (0.0, 0.0, "home", False))
        self.assertEqual(client.get("/position").json(), pos)

        state = validate_state(client.get("/state").json())
        self.assertEqual(state["substrates"][0]["substrate_id"], "sample_1")
        self.assertEqual(state["clients"]["experiment_a"]["state"], NOT_STARTED)
        with self.assertRaises(ValueError):
            validate_state({"now": 0})

    def test_gantry_starts_at_home_and_moves_independently_of_shelf(self):
        client = ExperimentClient("experiment_a", duration=3.0)
        locations = {"home": (0.0, 0.0), "shelf": (10.0, 0.0), "experiment_a": (30.0, 20.0)}
        manager = GantryManager([client], [Substrate("sample_1", ("experiment_a",))], movement_time=1.0, locations=locations)

        self.assertEqual(manager.pos()["at"], "home")
        manager.tick(0.0)  # leaves home for the shelf
        manager.tick(0.5)
        self.assertEqual(manager.coordinates(), (5.0, 0.0))
        self.assertTrue(manager.pos()["moving"])
        manager.tick(1.0)  # picked up at the shelf, heading to A
        manager.tick(1.5)
        self.assertEqual(manager.coordinates(), (20.0, 10.0))
        manager.tick(2.0)
        self.assertEqual(manager.pos(), {"x": 30.0, "y": 20.0, "z": 0.0, "moving": False, "at": "experiment_a"})
        with self.assertRaises(ValueError):
            GantryManager([client], [], locations={"shelf": (0.0, 0.0)})

    def test_manual_move_waits_for_idle_gantry_and_scheduler_resumes(self):
        client = ExperimentClient("experiment_a", duration=3.0)
        locations = {"home": (0.0, 0.0), "shelf": (10.0, 0.0), "experiment_a": (30.0, 0.0)}
        manager = GantryManager([client], [], movement_time=1.0, locations=locations)

        manager.move_to((40.0, 20.0))
        with self.assertRaises(RuntimeError):
            manager.move_to((1.0, 1.0))
        manager.tick(0.5)
        self.assertEqual(manager.coordinates(), (20.0, 10.0))
        self.assertTrue(manager.pos()["moving"])
        manager.tick(1.0)
        self.assertEqual(manager.pos(), {"x": 40.0, "y": 20.0, "z": 0.0, "moving": False, "at": None})

        # A queued sample starts from the manual spot.
        manager.add_substrate(Substrate("sample_1", ("experiment_a",)))
        manager.tick(1.5)
        self.assertEqual(manager.coordinates(), (25.0, 10.0))
        manager.run_until_complete()
        self.assertTrue(manager.completed)
        with self.assertRaises(ValueError):
            manager.move_to((float("nan"), 0.0))

    def test_post_pos_moves_gantry_and_refuses_while_busy(self):
        from fastapi.testclient import TestClient

        manager = build_manager([f"A=127.0.0.1:{free_port()}@30,40"], ["A"], movement_time=1.0)
        client = TestClient(create_app(Scheduler(manager)))

        response = client.post("/pos", json={"x": 50, "y": 25})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["moving"])
        self.assertEqual(client.post("/pos", json={"x": 1, "y": 1}).status_code, 409)
        self.assertEqual(client.post("/pos", json={"x": "left"}).status_code, 422)
        manager.tick(1.0)
        self.assertEqual(client.get("/pos").json()["x"], 50.0)

        locked = TestClient(create_app(Scheduler(manager), token="secret"))
        self.assertEqual(locked.post("/pos", json={"x": 1, "y": 1}).status_code, 401)
        ok = locked.post("/pos", json={"x": 1, "y": 1}, headers={"Authorization": "Bearer secret"})
        self.assertEqual(ok.status_code, 202)

    def test_gantry_gets_move_pick_and_place_in_order(self):
        class Recorder:
            def __init__(self):
                self.sent = []

            def send(self, command, **arguments):
                self.sent.append((command, arguments.get("params", {}).get("to") or arguments["params"].get("station")))

        recorder = Recorder()
        client = ExperimentClient("experiment_a", duration=1.0)
        locations = {"home": (0.0, 0.0), "shelf": (10.0, 20.0), "experiment_a": (30.0, 40.0)}
        manager = GantryManager([client], [Substrate("s1", ("experiment_a",))], movement_time=1.0,
                                locations=locations, gantry=recorder)
        manager.run_until_complete()
        self.assertEqual(recorder.sent, [
            ("move", "shelf"), ("pick", "shelf"), ("move", "experiment_a"), ("place", "experiment_a"),
            ("move", "experiment_a"), ("pick", "experiment_a"), ("move", "shelf"), ("place", "shelf"),
        ])

    def test_gantry_link_reports_commands_that_are_not_available(self):
        from gantry.link import GantryLink

        link = GantryLink(f"127.0.0.1:{free_port()}", timeout=0.5)
        self.addCleanup(link.close)
        self.assertIsNone(link.report()["last"])
        link.send("move", x=1.0, y=2.0)
        link.wait_idle()
        report = link.report()
        self.assertFalse(report["last"]["ok"])
        self.assertIn("move not available: gantry NOT STARTED", report["last"]["message"])
        self.assertEqual(report["failures"], 1)

    def test_gantry_link_drives_a_labnode_gantry(self):
        import uvicorn

        from gantry.link import GantryLink
        from sim.fake_gantry import FakeGantry

        node = FakeGantry(move_time=0.05)
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(create_node_app(node), host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        link = GantryLink(f"127.0.0.1:{port}")
        try:
            while not server.started:
                time.sleep(0.01)
            link.send("move", x=3.0, y=4.0, params={"to": "experiment_a"})
            link.wait_idle()
            self.assertTrue(link.report()["last"]["ok"])
            node.wait(2)
            self.assertEqual(node.position()["x"], 3.0)
            link.send("pick", params={"station": "experiment_a"})
            link.wait_idle()
            self.assertEqual(link.report()["last"]["message"], "pick sent")
        finally:
            link.close()
            server.should_exit = True
            thread.join(timeout=5)

    def test_poller_reads_nodes_and_gantry_position(self):
        gantry = serve_text(b'{"x": 12.5, "y": 3.0, "z": 0}')
        node = LiveNode("a", 5.0)
        node.node.start("sample_1")
        poller = NodePoller()
        try:
            poller.set_targets(
                {"experiment_a": node.address, "dead": f"127.0.0.1:{free_port()}"},
                f"127.0.0.1:{gantry.server_address[1]}",
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                statuses, (payload, problem, _) = poller.snapshot()
                if payload is not None and "experiment_a" in statuses:
                    break
                time.sleep(0.05)
            self.assertEqual(problem, "")
            self.assertEqual(position_from_payload(payload), (12.5, 3.0))
            self.assertEqual(statuses["experiment_a"]["state"], "busy")
            self.assertEqual(statuses["dead"]["state"], NOT_STARTED)
        finally:
            poller.close()
            node.close()
            gantry.shutdown()
            gantry.server_close()

class WebGuiTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient

        from web import server as web_server

        self.tmp = tempfile.TemporaryDirectory()
        self.presets_path = Path(self.tmp.name) / "presets.json"
        self.poller = NodePoller()
        self.store = web_server.PresetsStore(self.poller, self.presets_path, legacy_path=None)
        self.client = TestClient(web_server.create_app(self.store))

    def tearDown(self):
        self.poller.close()
        self.tmp.cleanup()

    def test_presets_edit_save_and_reload(self):
        import json

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Save presets", page.text)

        reply = self.client.get("/api/presets").json()
        self.assertEqual(reply["presets"], default_presets())
        self.assertTrue(reply["unsaved"])  # no presets.json yet

        presets = reply["presets"]
        presets["stations"][1]["x"] = 12.5
        presets["gantry_source"] = "lab"
        presets["shelf_slots"] = 4
        presets["routes"] = ["A", "B,A"]
        edited = self.client.put("/api/presets", json=presets).json()
        self.assertTrue(edited["unsaved"])
        self.assertFalse(self.presets_path.exists())  # edits are not written until Save
        self.assertEqual(self.poller._gantry[1], "/position")

        saved = self.client.post("/api/presets/save").json()
        self.assertFalse(saved["unsaved"])
        on_disk = json.loads(self.presets_path.read_text())
        self.assertEqual((on_disk["stations"][1]["x"], on_disk["shelf_slots"], on_disk["routes"]), (12.5, 4, ["A", "B,A"]))

        presets["bed"] = [50, 50]
        self.client.put("/api/presets", json=presets)
        reloaded = self.client.post("/api/presets/reload").json()
        self.assertEqual(reloaded["presets"]["bed"], [100.0, 60.0])
        self.assertFalse(reloaded["unsaved"])

        for bad in ({"bed": [0, 10]}, {"shelf_slots": 0}, {"shelf_slots": 1, "routes": ["A", "B"]},
                    {"stations": [{"name": "home", "address": "", "x": 0, "y": 0}]}):
            self.assertEqual(self.client.put("/api/presets", json={**presets, **bad}).status_code, 422, bad)

    def test_presets_import_old_layout_files(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "gantry_layout.json"
            legacy.write_text('{"bed": [100, 60], "scale": [2, 2], "stations": [{"name": "experiment_a", "address": "8001", "x": 1, "y": 2}]}')
            presets = load_presets(Path(tmp) / "presets.json", legacy)
        self.assertEqual(presets["calibration"]["scale"], [2.0, 2.0])
        self.assertEqual(presets["stations"][0]["name"], "shelf")
        self.assertEqual(presets["shelf_slots"], 10)

    def test_gantry_node_builds_from_presets(self):
        presets = default_presets()
        presets["gantry_address"] = "127.0.0.1:8123"
        presets["shelf_slots"] = 3
        presets["calibration"] = {"home_raw": [100.0, 100.0], "home_bed": [0.0, 0.0], "scale": [0.5, 0.5]}
        settings = settings_from_presets(presets)

        self.assertEqual(settings["gantry"], "127.0.0.1:8123")
        # Bed (35, 45) through the calibration: raw = 100 + bed / 0.5.
        self.assertEqual(settings["nodes"][0], ("experiment_a", "127.0.0.1:8001", (170.0, 190.0)))
        manager = build_manager(**settings)
        self.addCleanup(manager.gantry.close)
        self.assertEqual(manager.gantry.address, "http://127.0.0.1:8123")
        self.assertEqual((manager.shelf_slots, len(manager.substrates)), (3, 3))
        with self.assertRaises(ValueError):
            manager.add_substrate(Substrate("sample_4", ("experiment_a",)))
        with self.assertRaises(ValueError):
            build_manager(**{**settings, "shelf_slots": 2, "gantry": ""})

    def test_snapshot_reports_nodes_that_are_not_started(self):
        presets = default_presets()
        presets["gantry_address"] = f"127.0.0.1:{free_port()}"
        presets["stations"] = [presets["stations"][0], {"name": "experiment_a", "address": f"127.0.0.1:{free_port()}", "x": 1, "y": 1}]
        self.client.put("/api/presets", json=presets)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            snap = self.client.get("/api/snapshot").json()
            if snap["statuses"] and snap["gantry"]["problem"]:
                break
            time.sleep(0.05)
        self.assertEqual(snap["statuses"]["experiment_a"]["state"], NOT_STARTED)
        self.assertEqual(snap["gantry"]["problem"], NOT_STARTED)

    def test_move_is_forwarded_to_the_scheduler(self):
        import uvicorn

        manager = build_manager([f"A=127.0.0.1:{free_port()}@30,40"], ["A"], movement_time=0.2)
        scheduler = Scheduler(manager)
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(create_app(scheduler), host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        scheduler.start()
        try:
            while not server.started:
                time.sleep(0.01)
            presets = default_presets()
            presets["scheduler_address"] = f"127.0.0.1:{port}"
            self.client.put("/api/presets", json=presets)

            self.assertEqual(self.client.post("/api/move", json={"x": 40, "y": 20}).json(), {"ok": True, "message": "Moving"})
            busy = self.client.post("/api/move", json={"x": 1, "y": 1}).json()
            self.assertFalse(busy["ok"])
            self.assertIn("busy", busy["message"])
            time.sleep(0.4)
            self.assertEqual(scheduler.pos()["x"], 40.0)

            presets["scheduler_address"] = f"127.0.0.1:{free_port()}"
            self.client.put("/api/presets", json=presets)
            self.assertIn("NOT STARTED", self.client.post("/api/move", json={"x": 1, "y": 1}).json()["message"])
        finally:
            scheduler.stop()
            server.should_exit = True
            thread.join(timeout=5)

    def test_gui_reads_and_moves_the_scheduler_directly(self):
        from fastapi.testclient import TestClient

        from web import server as web_server

        manager = build_manager([f"A=127.0.0.1:{free_port()}@30,40"], ["A"], movement_time=0.2)
        scheduler = Scheduler(manager)
        presets = default_presets()
        presets["gantry_source"] = "simulator"
        self.presets_path.write_text(json.dumps(presets))
        store = web_server.PresetsStore(None, self.presets_path, legacy_path=None, scheduler=scheduler)
        app = web_server.create_app(store)
        app.mount("/scheduler", create_app(scheduler))
        client = TestClient(app)
        scheduler.start()
        try:
            time.sleep(0.3)
            snap = client.get("/api/snapshot").json()
            self.assertEqual(snap["statuses"]["experiment_a"]["state"], NOT_STARTED)
            # Stations the running scheduler does not know are reported, not polled.
            self.assertIn("restart main.py", snap["statuses"]["experiment_b"]["error"])
            self.assertEqual(snap["gantry"]["problem"], "")
            self.assertIn("substrates", snap["gantry"]["payload"])
            self.assertIn("substrates", client.get("/scheduler/state").json())

            self.assertEqual(client.post("/api/move", json={"x": 5, "y": 5}).json(), {"ok": True, "message": "Moving"})
            self.assertIn("Refused", client.post("/api/move", json={"x": 1, "y": 1}).json()["message"])
        finally:
            scheduler.stop()


if __name__ == "__main__":
    unittest.main()