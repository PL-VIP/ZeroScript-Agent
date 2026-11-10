from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_bridge import Gateway, PendingApproval
from zeroscript_agent.workspace import WorkspaceRuntime


class GatewaySecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = {
            "workspace": {"exclude": [".git"], "secret_patterns": [".env"]},
            "runner": {"mode": "disabled"},
            "security": {"allow_missing_origin": True},
        }
        self.gateway = Gateway(WorkspaceRuntime(root, config, root / ".data"), config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def socket(origin: str | None):
        return SimpleNamespace(request=SimpleNamespace(headers={"Origin": origin} if origin else {}))

    def test_extension_origin_is_allowed(self) -> None:
        self.assertTrue(self.gateway._origin_allowed(self.socket("chrome-extension://test-id")))

    def test_web_page_origin_is_rejected(self) -> None:
        self.assertFalse(self.gateway._origin_allowed(self.socket("https://example.com")))

    def test_operation_fingerprint_changes_with_arguments(self) -> None:
        first = self.gateway._fingerprint("run_command", {"argv": ["python", "-V"]})
        second = self.gateway._fingerprint("run_command", {"argv": ["python", "script.py"]})
        self.assertNotEqual(first, second)

    def test_denied_approval_is_consumed_and_cannot_be_reused(self) -> None:
        class FakeSocket:
            def __init__(self):
                self.messages = []

            async def send(self, payload):
                self.messages.append(json.loads(payload))

        async def scenario():
            socket = FakeSocket()
            args = {"argv": ["python", "-V"]}
            token = "one-time-token"
            self.gateway.approvals[token] = PendingApproval(
                token=token,
                name="run_command",
                arguments=args,
                fingerprint=self.gateway._fingerprint("run_command", args),
                created=time.monotonic(),
                connection_id="connection-a",
            )
            await self.gateway._approve(
                socket,
                {"id": 1, "approval_token": token, "approved": False},
                "connection-a",
            )
            await self.gateway._approve(
                socket,
                {"id": 2, "approval_token": token, "approved": True},
                "connection-a",
            )
            self.assertEqual(socket.messages[0]["kind"], "denied")
            self.assertEqual(socket.messages[1]["kind"], "approval_expired")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
