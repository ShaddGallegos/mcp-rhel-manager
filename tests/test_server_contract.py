import json
import os
import sys
import unittest
from unittest.mock import patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server


SAMPLE_DIAG = {
    "host": "unit-host",
    "timestamp": "2026-01-01T00:00:00Z",
    "os_version": "10.1",
    "product": "UnitProduct",
    "serial": "UNIT-123",
    "hardware": [
        {
            "title": "CPU over-temperature: 99C",
            "driver": "coretemp",
            "hardware": "CPU package",
            "error_text": "peak 99C",
            "severity": "Critical",
            "remediations": ["Reduce load immediately"],
        }
    ],
    "security": [
        {
            "title": "Remote root login observed",
            "driver": "sshd",
            "hardware": "SSH/root",
            "error_text": "Accepted publickey for root",
            "severity": "Critical",
            "remediations": ["Disable root login"],
        }
    ],
    "excerpts": {
        "sensors": "temp1: +99.0C",
        "dmesg": "critical thermal event",
        "lspci": "00:00.0 Host bridge",
        "ssh_journal": "Accepted publickey for root",
    },
}


class ServerContractTests(unittest.TestCase):
    def _assert_envelope(self, payload, tool_name):
        self.assertIsInstance(payload, dict)
        self.assertIn("ok", payload)
        self.assertEqual(payload.get("tool"), tool_name)
        self.assertIn("code", payload)
        self.assertIn("message", payload)
        self.assertIn("timestamp", payload)

    def test_capabilities_contract(self):
        payload = json.loads(server.mcp_server_capabilities())
        self._assert_envelope(payload, "mcp_server_capabilities")
        self.assertTrue(payload["ok"])
        self.assertIn("data", payload)
        self.assertIn("features", payload["data"])
        self.assertTrue(payload["data"]["features"].get("structured_error_category"))
        self.assertTrue(payload["data"]["features"].get("structured_action_hint"))

    def test_health_contract(self):
        payload = json.loads(server.mcp_server_health())
        self._assert_envelope(payload, "mcp_server_health")
        self.assertIn(payload.get("ok"), (True, False))
        self.assertTrue("data" in payload or "details" in payload)

    def test_readiness_contract(self):
        payload = json.loads(server.mcp_server_readiness())
        self._assert_envelope(payload, "mcp_server_readiness")
        self.assertIn(payload.get("ok"), (True, False))
        body = payload.get("data") if payload.get("ok") else payload.get("details")
        self.assertIsInstance(body, dict)
        self.assertIn("ready", body)
        self.assertIn("status", body)
        self.assertIn("blockers", body)

    def test_hardware_contract_with_stubbed_payload(self):
        with patch.object(server, "full_diagnostics_json", return_value=json.dumps(SAMPLE_DIAG)):
            payload = json.loads(server.hardware_diagnostics())
        self._assert_envelope(payload, "hardware_diagnostics")
        self.assertEqual(payload.get("code"), "DIAGNOSTICS_READY")
        self.assertTrue(payload.get("ok"))
        self.assertIn("data", payload)
        self.assertIn("summary", payload["data"])
        self.assertIn("report_text", payload["data"])
        self.assertIn("issues", payload["data"])

    def test_security_contract_with_stubbed_payload(self):
        with patch.object(server, "full_diagnostics_json", return_value=json.dumps(SAMPLE_DIAG)):
            payload = json.loads(server.security_diagnostics())
        self._assert_envelope(payload, "security_diagnostics")
        self.assertEqual(payload.get("code"), "DIAGNOSTICS_READY")
        self.assertTrue(payload.get("ok"))
        self.assertIn("data", payload)
        self.assertIn("summary", payload["data"])
        self.assertIn("report_text", payload["data"])
        self.assertIn("issues", payload["data"])

    def test_structured_error_fields_present(self):
        payload = json.loads(
            server._tool_err(
                "unit_test_tool",
                "UNIT_TEST_ERROR",
                "unit test failure",
                error_category="validation",
                action_hint="Check test input and retry.",
            )
        )
        self._assert_envelope(payload, "unit_test_tool")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload.get("error_category"), "validation")
        self.assertEqual(payload.get("action_hint"), "Check test input and retry.")


if __name__ == "__main__":
    unittest.main()
