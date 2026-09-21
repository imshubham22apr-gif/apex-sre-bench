"""
Test Suite: Mercor Archipelago MCP Server & Harbor Stirrup Adapter Validation.
"""

import json
import pytest

from adapters.stirrup_adapter import StirrupHarborAdapter
from agent.mock_agent import ExpertSRE, NaiveJuniorAgent
from tools.mcp_server_stdio import MCPServerStdio, truncate_output


class TestMercorHarnessIntegration:
    """Verifies integration with Mercor Archipelago MCP protocol and Harbor runner."""

    def test_mcp_server_initialize_and_tool_list(self) -> None:
        server = MCPServerStdio()

        # 1. Test initialize
        init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        init_resp = server.handle_request(init_req)
        assert init_resp["jsonrpc"] == "2.0"
        assert init_resp["id"] == 1
        assert "serverInfo" in init_resp["result"]
        assert init_resp["result"]["serverInfo"]["name"] == "apex-sre-mcp-server"

        # 2. Test tools/list
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        list_resp = server.handle_request(list_req)
        assert list_resp["id"] == 2
        tools = list_resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert "query_prometheus" in tool_names
        assert "tail_service_logs" in tool_names
        assert "inspect_process" in tool_names
        assert "apply_hotfix" in tool_names
        assert "restart_service" in tool_names
        assert "generate_post_mortem" in tool_names
        assert "apply_runtime_config" in tool_names
        assert "submit_structured_rca" in tool_names
        assert "get_incident_alert" in tool_names

    def test_mcp_server_runtime_config_and_rca_dispatch(self) -> None:
        server = MCPServerStdio()

        # Test apply_runtime_config dispatch
        config_req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "apply_runtime_config",
                "arguments": {
                    "service_name": "api-gateway",
                    "config_key": "timeout_seconds",
                    "config_value": 5,
                },
            },
        }
        config_resp = server.handle_request(config_req)
        assert config_resp["id"] == 10
        assert config_resp["result"]["isError"] is False
        assert "timeout_seconds" in config_resp["result"]["content"][0]["text"]

        # Test submit_structured_rca dispatch
        rca_req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "submit_structured_rca",
                "arguments": {
                    "root_cause_scenario": "scenario_1_goroutine_deadlock",
                    "faulty_component": "checkout_worker",
                    "contributing_factor": "unbuffered_channel_circular_lock",
                    "remediation_applied": "buffered_channels_with_timeout",
                },
            },
        }
        rca_resp = server.handle_request(rca_req)
        assert rca_resp["id"] == 11
        assert rca_resp["result"]["isError"] is False
        assert "accepted" in rca_resp["result"]["content"][0]["text"]

    def test_mcp_server_get_incident_alert_dispatch(self) -> None:
        server = MCPServerStdio()
        alert_req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "get_incident_alert",
                "arguments": {},
            },
        }
        alert_resp = server.handle_request(alert_req)
        assert alert_resp["id"] == 12
        assert alert_resp["result"]["isError"] is False
        parsed_content = json.loads(alert_resp["result"]["content"][0]["text"])
        assert "alert_name" in parsed_content
        assert "severity" in parsed_content
        assert "service" in parsed_content

    def test_mcp_server_tool_dispatch_and_execution(self) -> None:
        server = MCPServerStdio()
        call_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "query_prometheus",
                "arguments": {"promql": "active_goroutines", "time_window_seconds": 30},
            },
        }
        call_resp = server.handle_request(call_req)
        assert call_resp["id"] == 3
        assert call_resp["result"]["isError"] is False
        content = call_resp["result"]["content"]
        assert len(content) > 0
        assert "active_goroutines" in content[0]["text"]

    def test_mcp_truncation_logic(self) -> None:
        # Short string should remain unmodified
        short = "Normal small output"
        assert truncate_output(short, max_chars=100) == short

        # Massive string exceeding max_chars should be truncated with notice
        massive = "X" * 5000
        truncated = truncate_output(massive, max_chars=1000)
        assert len(truncated) < 1500
        assert "Mercor Truncation Notice" in truncated
        assert "characters omitted to preserve context" in truncated

    def test_harbor_stirrup_adapter_expert_run(self) -> None:
        adapter = StirrupHarborAdapter(host="harbor.mercor.internal", port=8000, task_id="task-sre-001")
        result = adapter.execute_task("scenario_1_goroutine_deadlock", agent=ExpertSRE())

        assert result["task_id"] == "task-sre-001"
        assert result["benchmark"] == "apex-sre-bench"
        assert result["passed"] is True
        assert result["verdict"] == "SUCCESS"
        assert result["score"] >= 0.90
        assert result["metrics"]["blast_radius_safe"] == 1.0
        assert result["environment"]["host"] == "harbor.mercor.internal"

    def test_harbor_stirrup_adapter_naive_run(self) -> None:
        adapter = StirrupHarborAdapter(host="harbor.mercor.internal", port=8000, task_id="task-sre-002")
        result = adapter.execute_task("scenario_1_goroutine_deadlock", agent=NaiveJuniorAgent())

        assert result["task_id"] == "task-sre-002"
        assert result["passed"] is False
        assert result["verdict"] == "FAILURE"
        assert result["score"] == 0.0
        assert result["metrics"]["blast_radius_safe"] == 0.0
