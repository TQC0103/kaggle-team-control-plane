import io
import json
import unittest

from agent_interface.server import JsonRpcServer
from agent_interface.tools import ToolInputError, ToolRegistry


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, *, query=None, body=None):
        self.calls.append((method, path, query, body))
        return {"method": method, "path": path}


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.registry = ToolRegistry(self.client)

    def test_submit_batch_requires_account_for_every_experiment(self):
        with self.assertRaisesRegex(ToolInputError, r"experiments\[1\]\.account_id"):
            self.registry.call(
                "submit_batch",
                {
                    "name": "invalid batch",
                    "experiments": [
                        {
                            "account_id": "a1",
                            "experiment_name": "base",
                            "source_dir": "exp/base",
                            "kernel_slug": "base-v1",
                        },
                        {
                            "experiment_name": "missing",
                            "source_dir": "exp/missing",
                            "kernel_slug": "missing-v1",
                        },
                    ]
                },
            )
        self.assertEqual(self.client.calls, [])

    def test_submit_batch_requires_name(self):
        with self.assertRaisesRegex(ToolInputError, "name"):
            self.registry.call(
                "submit_batch",
                {
                    "experiments": [
                        {
                            "account_id": "a1",
                            "experiment_name": "baseline",
                            "source_dir": "exp/base",
                            "kernel_slug": "baseline-v1",
                        }
                    ]
                },
            )
        self.assertEqual(self.client.calls, [])

    def test_submit_batch_forwards_explicit_assignments(self):
        experiments = [
            {
                "account_id": "a1",
                "experiment_name": "baseline",
                "source_dir": "exp/base",
                "kernel_slug": "baseline-v1",
            },
            {
                "account_id": "a2",
                "experiment_name": "augmentation",
                "source_dir": "exp/aug",
                "kernel_slug": "augmentation-v1",
            },
        ]
        self.registry.call(
            "submit_batch",
            {
                "name": "sweep",
                "experiments": experiments,
                "idempotency_key": "agent-retry-0001",
            },
        )
        self.assertEqual(
            self.client.calls,
            [(
                "POST",
                "/api/batches",
                None,
                {
                    "name": "sweep",
                    "jobs": experiments,
                    "idempotency_key": "agent-retry-0001",
                },
            )],
        )

    def test_submit_batch_is_limited_to_ten_experiments(self):
        experiments = [
            {
                "account_id": f"a{index}",
                "experiment_name": f"experiment-{index}",
                "source_dir": "exp/base",
                "kernel_slug": f"experiment-{index}",
            }
            for index in range(11)
        ]
        with self.assertRaisesRegex(ToolInputError, "at most 10"):
            self.registry.call("submit_batch", {"name": "large", "experiments": experiments})
        self.assertEqual(self.client.calls, [])

    def test_run_id_is_path_encoded(self):
        self.registry.call("cancel_run", {"run_id": "run/one"})
        self.assertEqual(
            self.client.calls[0],
            ("POST", "/api/jobs/run%2Fone/cancel", None, {}),
        )


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = JsonRpcServer(ToolRegistry(FakeClient()))

    def test_tools_list_uses_mcp_shape(self):
        response = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            names,
            {"list_accounts", "submit_batch", "list_runs", "cancel_run", "retry_run", "fetch_result", "audit_events"},
        )
        submit = next(t for t in response["result"]["tools"] if t["name"] == "submit_batch")
        self.assertEqual(submit["inputSchema"]["required"], ["name", "experiments"])
        self.assertEqual(
            submit["inputSchema"]["properties"]["experiments"]["items"]["required"],
            ["account_id", "experiment_name", "source_dir", "kernel_slug"],
        )
        self.assertEqual(
            submit["inputSchema"]["properties"]["experiments"]["maxItems"], 10
        )

    def test_stdio_round_trip_and_notification(self):
        requests = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
            ]
        )
        output = io.StringIO()
        self.server.serve(io.StringIO(requests), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")

    def test_tool_input_failure_is_a_tool_error_not_rpc_failure(self):
        response = self.server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "fetch_result", "arguments": {}},
            }
        )
        self.assertTrue(response["result"]["isError"])
        self.assertIn("run_id", response["result"]["structuredContent"]["error"])


if __name__ == "__main__":
    unittest.main()
