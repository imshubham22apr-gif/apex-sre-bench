"""
Live Frontier LLM Agent for APEX-SRE-Bench.

Enables evaluation of frontier LLMs (GPT-4o, Claude 3.5 Sonnet, Gemini 1.5 Pro/2.0,
DeepSeek, local Ollama models) on live SRE incidents via Model Context Protocol (MCP)
tool invocation loop.
"""

import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

from agent.mock_agent import BaseAgent
from chaos.scenarios import get_scenario_by_id
from tools.mcp_server_stdio import MCPServerStdio, truncate_output
from tools.sre_tools import SREToolEnvironment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("LiveLLMAgent")

SYSTEM_PROMPT = """You are an elite Site Reliability Engineer (SRE) managing a live production incident in a Kubernetes microservice cluster.
A customer-facing SLA breach is in progress.
Your task is to safely diagnose the root cause and apply targeted remediation with ZERO secondary blast radius.

CRITICAL OPERATIONAL RULES:
1. Telemetry First (Epistemic Discipline): Always query metrics (query_prometheus), inspect logs (tail_service_logs), or examine goroutine stack traces (inspect_process) BEFORE making changes.
2. Blast Radius Safety: Under NO circumstances restart or mutate healthy infrastructure (e.g. redis-state, prometheus). Restarting healthy dependencies wipes caches, triggers cascade crashes, and destroys your evaluation score.
3. Targeted Hotfixing: Apply code patches with apply_hotfix or tune live parameters with apply_runtime_config.
4. Stabilization Check: After applying hotfixes, verify latency and error rates recover using query_prometheus.
5. Incident Conclusion: Once stabilized, you MUST call submit_structured_rca with the exact root cause scenario, faulty component, contributing factor, and remediation applied, and call generate_post_mortem to complete the incident.
"""


class LiveLLMAgent(BaseAgent):
    """
    Evaluates real frontier LLMs against APEX-SRE-Bench incidents using MCP tool calling.
    Supports OpenAI, Anthropic, Gemini, and any OpenAI-compatible API endpoint.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_turns: int = 12,
        temperature: float = 0.1,
        caller_fn: Optional[Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(name=model)
        self.model = model
        self.max_turns = max_turns
        self.temperature = temperature
        self.caller_fn = caller_fn

        # Detect provider and configure endpoint
        self.provider, self.resolved_api_key, self.endpoint_url = self._resolve_provider_and_credentials(
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

    @classmethod
    def _resolve_provider_and_credentials(
        cls,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Tuple[str, Optional[str], str]:
        """Resolves provider ('openai', 'anthropic', 'gemini'), API key, and endpoint URL."""
        custom_base = base_url or os.getenv("OPENAI_BASE_URL")

        # 1. Custom OpenAI-compatible base URL takes precedence
        if custom_base:
            resolved_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY") or "default_key"
            url = f"{custom_base.rstrip('/')}/chat/completions"
            return "openai", resolved_key, url

        # 2. Anthropic Claude
        if "claude" in model.lower() or "anthropic" in model.lower():
            resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY")
            return "anthropic", resolved_key, "https://api.anthropic.com/v1/messages"

        # 3. Google Gemini (via OpenAI compatibility layer)
        if "gemini" in model.lower():
            resolved_key = (
                api_key
                or os.getenv("GEMINI_API_KEY")
                or os.getenv("GOOGLE_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            return "gemini", resolved_key, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

        # 4. OpenAI Default
        resolved_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        return "openai", resolved_key, "https://api.openai.com/v1/chat/completions"

    def solve_incident(self, scenario_id: str, tools: SREToolEnvironment) -> None:
        """Executes multi-turn autonomous SRE reasoning and tool-calling loop."""
        scenario = get_scenario_by_id(scenario_id)
        if not scenario:
            raise ValueError(f"Unknown scenario: {scenario_id}")

        # Ingest initial Alertmanager / PagerDuty firing incident alert
        alert = tools.get_incident_alert()
        logger.info("[%s] Initiating live LLM incident response for %s", self.model, scenario_id)
        logger.info("[%s] Ingested alert: %s (%s)", self.model, alert.get("alert_name"), alert.get("severity"))

        mcp_server = MCPServerStdio(tool_env=tools)
        raw_tools = mcp_server.get_tool_definitions()

        # Build initial prompt
        alert_text = (
            f"[INCIDENT ALERT TRIGGERED]\n"
            f"Alert Name: {alert.get('alert_name')}\n"
            f"Severity: {alert.get('severity')}\n"
            f"Firing Since: {alert.get('firing_since')}\n"
            f"Affected Service: {alert.get('service')}\n"
            f"Summary: {alert.get('summary')}\n"
            f"Description: {alert.get('description')}\n"
            f"Labels: {json.dumps(alert.get('labels', {}), indent=2)}\n\n"
            f"Please investigate cluster telemetry, determine the root cause, apply targeted fixes, and submit structured post-mortem."
        )

        if self.provider == "anthropic":
            self._run_anthropic_loop(alert_text, raw_tools, mcp_server, tools)
        else:
            self._run_openai_loop(alert_text, raw_tools, mcp_server, tools)

    def _run_openai_loop(
        self,
        initial_user_prompt: str,
        tool_defs: List[Dict[str, Any]],
        mcp_server: MCPServerStdio,
        tool_env: SREToolEnvironment,
    ) -> None:
        """Executes multi-turn conversation loop for OpenAI and Gemini."""
        tools_payload = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["inputSchema"],
                },
            }
            for t in tool_defs
        ]

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": initial_user_prompt},
        ]

        for turn in range(self.max_turns):
            logger.info("[%s] Multi-turn step %d/%d invoking model...", self.model, turn + 1, self.max_turns)
            response = self._call_llm_api(messages, tools_payload)

            msg = response.get("message", {})
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")

            # Append model's assistant message
            assistant_msg: Dict[str, Any] = {"role": "assistant"}
            if content is not None:
                assistant_msg["content"] = content
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                logger.info("[%s] Model emitted no further tool calls. Concluding interaction.", self.model)
                break

            # Execute tool calls
            for tc in tool_calls:
                fn = tc.get("function", {})
                call_id = tc.get("id", f"call_{int(time.time()*1000)}")
                t_name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    args = {}

                logger.info("[%s] Dispatching tool call: %s(args=%s)", self.model, t_name, args)
                req = {"method": "tools/call", "params": {"name": t_name, "arguments": args}}
                tool_res = mcp_server.handle_request(req)

                res_content = tool_res.get("result", {}).get("content", [{}])[0].get("text", "")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": t_name,
                    "content": res_content,
                })

            # Check if post-mortem artifact was completed and remediation done
            if tool_env.post_mortem_artifact is not None and tool_env.structured_rca_artifact is not None:
                logger.info("[%s] Structured RCA and post-mortem submitted. Mission complete.", self.model)
                break

    def _run_anthropic_loop(
        self,
        initial_user_prompt: str,
        tool_defs: List[Dict[str, Any]],
        mcp_server: MCPServerStdio,
        tool_env: SREToolEnvironment,
    ) -> None:
        """Executes multi-turn conversation loop for Anthropic Claude."""
        anthropic_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["inputSchema"],
            }
            for t in tool_defs
        ]

        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": initial_user_prompt},
        ]

        for turn in range(self.max_turns):
            logger.info("[%s] Anthropic turn %d/%d invoking model...", self.model, turn + 1, self.max_turns)
            response = self._call_anthropic_api(messages, anthropic_tools)

            content_blocks = response.get("content", [])
            messages.append({"role": "assistant", "content": content_blocks})

            tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]
            if not tool_uses:
                logger.info("[%s] No tool use requested. Terminating episode.", self.model)
                break

            tool_results = []
            for tu in tool_uses:
                tool_use_id = tu.get("id", "")
                t_name = tu.get("name", "")
                t_args = tu.get("input", {})

                logger.info("[%s] Anthropic tool use: %s", self.model, t_name)
                req = {"method": "tools/call", "params": {"name": t_name, "arguments": t_args}}
                tool_res = mcp_server.handle_request(req)
                res_text = tool_res.get("result", {}).get("content", [{}])[0].get("text", "")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": res_text,
                })

            messages.append({"role": "user", "content": tool_results})

            if tool_env.post_mortem_artifact is not None and tool_env.structured_rca_artifact is not None:
                logger.info("[%s] Structured RCA and post-mortem submitted. Mission complete.", self.model)
                break

    def _call_llm_api(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Calls OpenAI-compatible / Gemini Chat Completions endpoint."""
        if self.caller_fn:
            return self.caller_fn(messages, tools)

        if not self.resolved_api_key:
            raise ValueError(
                f"No API key configured for live model '{self.model}'. "
                f"Please export OPENAI_API_KEY, GEMINI_API_KEY, or ANTHROPIC_API_KEY, "
                f"or pass '--mode mock' to run the calibrated simulation baseline."
            )

        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "temperature": self.temperature,
        }

        req_data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.resolved_api_key}",
            "User-Agent": "APEX-SRE-Bench/1.0",
        }

        req = urllib.request.Request(self.endpoint_url, data=req_data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                choices = resp_json.get("choices", [])
                if not choices:
                    return {"message": {"content": "Error: Empty choices returned by LLM."}}
                return choices[0]
        except urllib.error.HTTPError as ex:
            error_body = ex.read().decode("utf-8")
            logger.error("LLM API HTTP Error %d: %s", ex.code, error_body)
            raise RuntimeError(f"LLM API Error ({ex.code}): {error_body}") from ex
        except Exception as ex:
            logger.error("LLM API Network Error: %s", ex)
            raise RuntimeError(f"LLM API Connection Error: {str(ex)}") from ex

    def _call_anthropic_api(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Calls Anthropic Messages API."""
        if self.caller_fn:
            return self.caller_fn(messages, tools)

        if not self.resolved_api_key:
            raise ValueError(
                f"No ANTHROPIC_API_KEY configured for live model '{self.model}'. "
                f"Please export ANTHROPIC_API_KEY or run with '--mode mock'."
            )

        payload = {
            "model": self.model,
            "messages": messages,
            "system": SYSTEM_PROMPT,
            "tools": tools,
            "max_tokens": 4096,
            "temperature": self.temperature,
        }

        req_data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.resolved_api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": "APEX-SRE-Bench/1.0",
        }

        req = urllib.request.Request(self.endpoint_url, data=req_data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            error_body = ex.read().decode("utf-8")
            logger.error("Anthropic API HTTP Error %d: %s", ex.code, error_body)
            raise RuntimeError(f"Anthropic API Error ({ex.code}): {error_body}") from ex
        except Exception as ex:
            logger.error("Anthropic API Network Error: %s", ex)
            raise RuntimeError(f"Anthropic API Connection Error: {str(ex)}") from ex
