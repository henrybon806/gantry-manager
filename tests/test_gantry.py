import random
import unittest

from gantry import ExperimentClient, ExperimentServer, GantryManager
from gantry_gui import parse_routes
from substrates import Substrate


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
        manager.close()

    def test_experiment_server_reports_client_state(self):
        client = ExperimentClient("experiment_a", duration=2.0)
        server = ExperimentServer(client)

        self.assertEqual(server.read_status(0.0)["state"], "idle")
        client.start(Substrate("sample_1", ("experiment_a",)), now=0.0)
        self.assertEqual(server.read_status(1.0)["state"], "busy")
        self.assertEqual(server.read_status(2.0)["state"], "finished")
        self.assertTrue(server.thread_alive)
        self.assertIsNotNone(server.port)
        server.close()

    def test_routes_are_configurable_per_sample(self):
        routes = parse_routes(2, "A,B,C\nC,A\n")

        self.assertEqual(routes, [
            ("experiment_a", "experiment_b", "experiment_c"),
            ("experiment_c", "experiment_a"),
        ])
        with self.assertRaises(ValueError):
            parse_routes(1, "A\nB\n")

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

        servers = list(manager.servers.values())
        self.assertEqual(len(servers), 3)
        self.assertEqual(len({server.port for server in servers}), 3)
        self.assertTrue(all(server.thread_alive for server in servers))
        self.assertEqual(
            {server.thread_name for server in servers},
            {"experiment_a-server", "experiment_b-server", "experiment_c-server"},
        )

        manager.run_until_complete()

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
        manager.close()


if __name__ == "__main__":
    unittest.main()