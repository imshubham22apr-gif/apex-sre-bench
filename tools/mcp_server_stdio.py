"""
Standalone Stdio MCP Server for APEX-SRE-Bench (Mercor Archipelago Compatible).

Implements Model Context Protocol JSON-RPC 2.0 over standard I/O (stdio).
Includes output truncation logic matching `loop_truncated_tools_agent.py` to prevent
context window blowups from high-volume telemetry or stack traces.
"""

import json
import logging
import sys
from typing import Any, Dict, List, Optional

from tools.sre_tools import SREToolEnvironment

# Configure logger to output exclusively to stderr so stdout remains clean JSON-RPC
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [MCP-Server]: %(message)s",
)
logger = logging.getLogger("MCPServer")

# Output truncation ceiling matching Mercor loop_truncated_tools_agent specifications
MAX_OUTPUT_CHARS = 4000


def truncate_output(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """
    Truncates large tool outputs (pprof dumps, log streams) to preserve context window.
    Appends explicit omission notice.
    """
    if len(text) <= max_chars:
        return text
    head_len = int(max_chars * 0.6)
    tail_len = int(max_chars * 0.3)
    omitted = len(text) - (head_len + tail_len)
    return (
        f"{text[:head_len]}\n\n"
        f"[... Mercor Truncation Notice: {omitted} characters omitted to preserve context ...]\n\n"
        f"{text[-tail_len:]}"
    )


class MCPServerStdio:
    """Handles JSON-RPC 2.0 requests over stdin and emits responses over stdout."""

    def __init__(self, tool_env: Optional[SREToolEnvironment] = None) -> None:
        self.tool_env = tool_env or SREToolEnvironment()

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Returns MCP tool definitions."""
        return [
            {
                "name": "get_incident_alert",
                "description": "Retrieve the triggering incident alert payload from Alertmanager/PagerDuty (alertname, severity, initial symptoms).",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "query_prometheus",
                "description": "Execute a PromQL query against Prometheus to retrieve time-series telemetry vectors.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "promql": {"type": "string", "description": "PromQL query string"},
                        "time_window_seconds": {"type": "integer", "default": 60},
                    },
                    "required": ["promql"],
                },
            },
            {
                "name": "tail_service_logs",
                "description": "Stream recent container logs for a target service.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string", "description": "Name of service"},
                        "lines": {"type": "integer", "default": 50},
                        "grep_pattern": {"type": "string", "description": "Optional regex pattern filter"},
                    },
                    "required": ["service_name"],
                },
            },
            {
                "name": "inspect_process",
                "description": "Inspect process-level telemetry, active goroutines, open FDs, and pprof stacks.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string", "description": "Name of service"},
                    },
                    "required": ["service_name"],
                },
            },
            {
                "name": "apply_hotfix",
                "description": "Apply code patch to service codebase and trigger graceful hot-reload.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string", "description": "Target service"},
                        "filepath": {"type": "string", "description": "Relative path to file"},
                        "patch_content": {"type": "string", "description": "Unified diff patch"},
                    },
                    "required": ["service_name", "filepath", "patch_content"],
                },
            },
            {
                "name": "restart_service",
                "description": "Restart a containerized service. Note: Mutating healthy services violates Blast Radius!",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string", "description": "Target service to restart"},
                    },
                    "required": ["service_name"],
                },
            },
            {
                "name": "generate_post_mortem",
                "description": "Record final Root Cause Analysis (RCA) artifact for incident evaluation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "root_cause": {"type": "string"},
                        "mitigation_steps": {"type": "string"},
                        "preventative_actions": {"type": "string"},
                    },
                    "required": ["root_cause", "mitigation_steps", "preventative_actions"],
                },
            },
            {
                "name": "apply_runtime_config",
                "description": "Apply dynamic runtime configuration parameters (timeouts, pool size, retry policies, flags) to a service without full container restart.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string", "description": "Target service"},
                        "config_key": {"type": "string", "description": "Configuration parameter key name"},
                        "config_value": {"description": "Configuration value (string, integer, boolean, or object)"},
                    },
                    "required": ["service_name", "config_key", "config_value"],
                },
            },
            {
                "name": "submit_structured_rca",
                "description": "Submit a structured Root Cause Analysis (RCA) artifact for deterministic Zero-LLM evaluation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "root_cause_scenario": {"type": "string", "description": "Canonical scenario identifier"},
                        "faulty_component": {"type": "string", "description": "Exact component or subsystem at fault"},
                        "contributing_factor": {"type": "string", "description": "Technical contributing factor or mechanism"},
                        "remediation_applied": {"type": "string", "description": "Remediation strategy applied to resolve"},
                    },
                    "required": [
                        "root_cause_scenario",
                        "faulty_component",
                        "contributing_factor",
                        "remediation_applied",
                    ],
                },
            },
        ]

    def handle_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Routes JSON-RPC request to appropriate MCP handler."""
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "apex-sre-mcp-server", "version": "1.0.0"},
                },
            }

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": self.get_tool_definitions()},
            }

        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})

            try:
                raw_result = self._dispatch_tool(tool_name, tool_args)
                result_str = json.dumps(raw_result, indent=2) if isinstance(raw_result, (dict, list)) else str(raw_result)
                truncated_text = truncate_output(result_str)

                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": truncated_text}],
                        "isError": False,
                    },
                }
            except Exception as ex:
                logger.error("Error executing tool %s: %s", tool_name, ex)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Tool execution failed: {str(ex)}"}],
                        "isError": True,
                    },
                }

        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

    def _dispatch_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """Dispatches call to underlying SREToolEnvironment."""
        if name == "get_incident_alert":
            return self.tool_env.get_incident_alert()
        elif name == "query_prometheus":
            return self.tool_env.query_prometheus(
                promql=args["promql"],
                time_window_seconds=args.get("time_window_seconds", 60),
            )
        elif name == "tail_service_logs":
            return self.tool_env.tail_service_logs(
                service_name=args["service_name"],
                lines=args.get("lines", 50),
                grep_pattern=args.get("grep_pattern"),
            )
        elif name == "inspect_process":
            return self.tool_env.inspect_process(service_name=args["service_name"])
        elif name == "apply_hotfix":
            return self.tool_env.apply_hotfix(
                service_name=args["service_name"],
                filepath=args["filepath"],
                patch_content=args["patch_content"],
            )
        elif name == "restart_service":
            return self.tool_env.restart_service(service_name=args["service_name"])
        elif name == "generate_post_mortem":
            return self.tool_env.generate_post_mortem(
                root_cause=args.get("root_cause", ""),
                mitigation_steps=args.get("mitigation_steps", args.get("actions_taken", "")),
                preventative_actions=args.get("preventative_actions", "monitor"),
            )
        elif name == "apply_runtime_config":
            return self.tool_env.apply_runtime_config(
                service_name=args["service_name"],
                config_key=args["config_key"],
                config_value=args["config_value"],
            )
        elif name == "submit_structured_rca":
            if "rca_report" in args and isinstance(args["rca_report"], dict):
                return self.tool_env.submit_structured_rca(args["rca_report"])
            return self.tool_env.submit_structured_rca(args)
        else:
            raise ValueError(f"Unknown MCP tool: {name}")

    def run_stdio_loop(self) -> None:
        """Reads JSON-RPC lines from stdin and writes responses to stdout."""
        logger.info("APEX-SRE-Bench MCP stdio server listening on stdin...")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = self.handle_request(req)
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
            except json.JSONDecodeError as err:
                logger.error("Invalid JSON received: %s", err)
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }
                sys.stdout.write(json.dumps(err_resp) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    server = MCPServerStdio()
    server.run_stdio_loop()
