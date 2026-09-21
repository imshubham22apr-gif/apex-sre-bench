"""
Test Suite: Live Frontier LLM Agent & MCP Tool Execution Loop.
"""

import json
import pytest

from agent.live_agent import LiveLLMAgent
from chaos.scenarios import get_scenario_by_id
from tools.sre_tools import SREToolEnvironment


class TestLiveLLMAgent:
    """Verifies LiveLLMAgent provider resolution, credentials, and multi-turn loops."""

    def test_provider_resolution(self) -> None:
        # 1. Default OpenAI
        agent_gpt = LiveLLMAgent(model="gpt-4o", api_key="sk-fake-openai")
        assert agent_gpt.provider == "openai"
        assert agent_gpt.endpoint_url == "https://api.openai.com/v1/chat/completions"

        # 2. Anthropic Claude
        agent_claude = LiveLLMAgent(model="claude-3-5-sonnet", api_key="sk-ant-fake")
        assert agent_claude.provider == "anthropic"
        assert agent_claude.endpoint_url == "https://api.anthropic.com/v1/messages"

        # 3. Google Gemini
        agent_gemini = LiveLLMAgent(model="gemini-1.5-pro", api_key="fake-gemini-key")
        assert agent_gemini.provider == "gemini"
        assert "generativelanguage.googleapis.com" in agent_gemini.endpoint_url

        # 4. Custom OpenAI-compatible endpoint
        agent_custom = LiveLLMAgent(
            model="meta-llama/Llama-3-70b",
            base_url="http://localhost:11434/v1",
            api_key="ollama",
        )
        assert agent_custom.provider == "openai"
        assert agent_custom.endpoint_url == "http://localhost:11434/v1/chat/completions"

    def test_missing_api_key_raises_error(self) -> None:
        agent = LiveLLMAgent(model="gpt-4o")
        # Ensure credentials are None
        agent.resolved_api_key = None
        scenario = get_scenario_by_id("scenario_1_goroutine_deadlock")
        assert scenario is not None
        tool_env = SREToolEnvironment(alert_payload=scenario.alert_payload)

        with pytest.raises(ValueError, match="No API key configured for live model 'gpt-4o'"):
            agent.solve_incident("scenario_1_goroutine_deadlock", tool_env)

    def test_openai_multi_turn_mock_caller(self) -> None:
        turn_counter = 0
        scenario = get_scenario_by_id("scenario_1_goroutine_deadlock")
        assert scenario is not None

        def mock_caller(messages, tools):
            nonlocal turn_counter
            turn_counter += 1

            if turn_counter == 1:
                # Turn 1: Call query_prometheus
                return {
                    "message": {
                        "content": "Checking active goroutines count...",
                        "tool_calls": [
                            {
                                "id": "call_001",
                                "function": {
                                    "name": "query_prometheus",
                                    "arguments": json.dumps({"promql": "active_goroutines"}),
                                },
                            }
                        ],
                    }
                }
            elif turn_counter == 2:
                # Turn 2: Call apply_hotfix
                return {
                    "message": {
                        "content": "Deadlock detected. Applying patch...",
                        "tool_calls": [
                            {
                                "id": "call_002",
                                "function": {
                                    "name": "apply_hotfix",
                                    "arguments": json.dumps({
                                        "service_name": "api-gateway",
                                        "filepath": "services/gateway/handlers.go",
                                        "patch_content": "buffered channel fix",
                                    }),
                                },
                            }
                        ],
                    }
                }
            elif turn_counter == 3:
                # Turn 3: Call submit_structured_rca and generate_post_mortem
                return {
                    "message": {
                        "content": "Submitting RCA post-mortem...",
                        "tool_calls": [
                            {
                                "id": "call_003",
                                "function": {
                                    "name": "submit_structured_rca",
                                    "arguments": json.dumps(scenario.structured_rca.to_dict()),
                                },
                            },
                            {
                                "id": "call_004",
                                "function": {
                                    "name": "generate_post_mortem",
                                    "arguments": json.dumps({
                                        "incident_id": "INC-001",
                                        "root_cause": "unbuffered channel circular lock",
                                        "actions_taken": "buffered channels with timeout",
                                    }),
                                },
                            },
                        ],
                    }
                }
            else:
                return {"message": {"content": "Incident resolved.", "tool_calls": None}}

        agent = LiveLLMAgent(model="gpt-4o", caller_fn=mock_caller)
        tool_env = SREToolEnvironment(alert_payload=scenario.alert_payload)

        agent.solve_incident("scenario_1_goroutine_deadlock", tool_env)

        # Verify tool calls executed in audit log
        tool_names = [c.tool_name for c in tool_env.call_history]
        assert "get_incident_alert" in tool_names
        assert "query_prometheus" in tool_names
        assert "apply_hotfix" in tool_names
        assert "submit_structured_rca" in tool_names
        assert "generate_post_mortem" in tool_names
        assert tool_env.structured_rca_artifact is not None

    def test_anthropic_multi_turn_mock_caller(self) -> None:
        turn_counter = 0
        scenario = get_scenario_by_id("scenario_2_cascading_retry_storm")
        assert scenario is not None

        def mock_caller(messages, tools):
            nonlocal turn_counter
            turn_counter += 1

            if turn_counter == 1:
                return {
                    "content": [
                        {"type": "text", "text": "Investigating logs for 5xx retry errors..."},
                        {
                            "type": "tool_use",
                            "id": "tu_001",
                            "name": "tail_service_logs",
                            "input": {"service_name": "api-gateway", "lines": 20},
                        },
                    ]
                }
            elif turn_counter == 2:
                return {
                    "content": [
                        {"type": "text", "text": "Submitting structured RCA..."},
                        {
                            "type": "tool_use",
                            "id": "tu_002",
                            "name": "submit_structured_rca",
                            "input": scenario.structured_rca.to_dict(),
                        },
                        {
                            "type": "tool_use",
                            "id": "tu_003",
                            "name": "generate_post_mortem",
                            "input": {
                                "incident_id": "INC-002",
                                "root_cause": "unjittered aggressive retries",
                                "actions_taken": "backoff with jitter",
                            },
                        },
                    ]
                }
            else:
                return {"content": [{"type": "text", "text": "All stabilized."}]}

        agent = LiveLLMAgent(model="claude-3-5-sonnet", caller_fn=mock_caller)
        tool_env = SREToolEnvironment(alert_payload=scenario.alert_payload)

        agent.solve_incident("scenario_2_cascading_retry_storm", tool_env)

        tool_names = [c.tool_name for c in tool_env.call_history]
        assert "get_incident_alert" in tool_names
        assert "tail_service_logs" in tool_names
        assert "submit_structured_rca" in tool_names
        assert tool_env.structured_rca_artifact is not None
