"""
SRE Tool Environment for APEX-SRE-Bench (Model Context Protocol / Archipelago Compatible).

Provides diagnostic telemetry inspection and mutating remediation tools for autonomous agents.
Tracks action history to compute epistemic metrics (EGR) and blast-radius invariants.
"""

from dataclasses import dataclass, field
import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("SRETools")


@dataclass
class ToolCallRecord:
    """Audit entry for every agent tool invocation."""
    tool_name: str
    is_telemetry: bool
    is_mutation: bool
    args: Dict[str, Any]
    result: Any
    timestamp: float = field(default_factory=time.time)


class SREToolEnvironment:
    """
    Model Context Protocol (MCP) compliant tool provider.
    Maintains tool audit log for deterministic evaluation oracle.
    """

    TELEMETRY_TOOLS = {"query_prometheus", "tail_service_logs", "inspect_process"}
    MUTATION_TOOLS = {"apply_hotfix", "restart_service", "apply_runtime_config", "exec_command"}

    _docker_checked: bool = False
    _docker_available: bool = False
    _prom_checked: bool = False
    _prom_available: bool = False
    _gateway_checked: bool = False
    _gateway_available: bool = False

    def __init__(
        self,
        prometheus_url: str = "http://localhost:9090",
        gateway_url: str = "http://localhost:8080",
        simulated_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.gateway_url = gateway_url.rstrip("/")
        self.call_history: List[ToolCallRecord] = []
        self.service_restarts: Dict[str, int] = {
            "api-gateway": 0,
            "redis-state": 0,
            "prometheus": 0,
        }
        self.simulated_context: Dict[str, Any] = simulated_context or {}
        self.post_mortem_artifact: Optional[Dict[str, str]] = None
        self.structured_rca_artifact: Optional[Dict[str, str]] = None

    @classmethod
    def _is_docker_available(cls) -> bool:
        if not cls._docker_checked:
            cls._docker_checked = True
            try:
                proc = subprocess.run(["docker", "ps"], capture_output=True, text=True, timeout=0.5)
                cls._docker_available = (proc.returncode == 0)
            except Exception:
                cls._docker_available = False
        return cls._docker_available

    def _is_prom_available(self) -> bool:
        if not self._prom_checked:
            SREToolEnvironment._prom_checked = True
            try:
                req = urllib.request.Request(f"{self.prometheus_url}/-/healthy")
                with urllib.request.urlopen(req, timeout=0.3) as resp:
                    SREToolEnvironment._prom_available = (resp.status == 200)
            except Exception:
                SREToolEnvironment._prom_available = False
        return SREToolEnvironment._prom_available

    def _is_gateway_available(self) -> bool:
        if not self._gateway_checked:
            SREToolEnvironment._gateway_checked = True
            try:
                req = urllib.request.Request(f"{self.gateway_url}/healthz")
                with urllib.request.urlopen(req, timeout=0.3) as resp:
                    SREToolEnvironment._gateway_available = (resp.status == 200)
            except Exception:
                SREToolEnvironment._gateway_available = False
        return SREToolEnvironment._gateway_available

    def record_call(self, tool_name: str, args: Dict[str, Any], result: Any) -> None:
        """Appends tool invocation record to the epistemic audit trail."""
        is_telemetry = tool_name in self.TELEMETRY_TOOLS
        is_mutation = tool_name in self.MUTATION_TOOLS
        record = ToolCallRecord(
            tool_name=tool_name,
            is_telemetry=is_telemetry,
            is_mutation=is_mutation,
            args=args,
            result=result,
        )
        self.call_history.append(record)
        logger.info(
            "Tool invoked: %s (telemetry=%s, mutation=%s)",
            tool_name,
            is_telemetry,
            is_mutation,
        )

    def query_prometheus(self, promql: str, time_window_seconds: int = 60) -> Dict[str, Any]:
        """
        Queries Prometheus time-series metrics vector.
        MCP tool: read-only telemetry.
        """
        result: Dict[str, Any]
        if self._is_prom_available():
            params = urllib.parse.urlencode({"query": promql})
            url = f"{self.prometheus_url}/api/v1/query?{params}"
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
            except Exception as ex:
                logger.debug("Prometheus query failed (%s), generating simulated metric", ex)
                result = self._generate_simulated_promql(promql, time_window_seconds)
        else:
            result = self._generate_simulated_promql(promql, time_window_seconds)

        self.record_call("query_prometheus", {"promql": promql, "time_window_seconds": time_window_seconds}, result)
        return result

    def tail_service_logs(self, service_name: str, lines: int = 50, grep_pattern: Optional[str] = None) -> str:
        """
        Retrieves recent container logs.
        MCP tool: read-only telemetry.
        """
        logs = ""
        if self._is_docker_available():
            try:
                cmd = ["docker", "logs", "--tail", str(lines), service_name]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1.0)
                if proc.returncode == 0:
                    logs = proc.stdout + proc.stderr
            except Exception:
                logs = ""

        if not logs:
            logs = self._generate_simulated_logs(service_name, lines)

        if grep_pattern:
            filtered = [line for line in logs.splitlines() if re.search(grep_pattern, line, re.IGNORECASE)]
            logs = "\n".join(filtered)

        self.record_call(
            "tail_service_logs",
            {"service_name": service_name, "lines": lines, "grep_pattern": grep_pattern},
            logs,
        )
        return logs

    def inspect_process(self, service_name: str) -> Dict[str, Any]:
        """
        Returns runtime diagnostics including active goroutines, memory usage,
        open file descriptors, and pprof summary.
        MCP tool: read-only telemetry.
        """
        data: Dict[str, Any]
        if service_name == "api-gateway" and self._is_gateway_available():
            try:
                pprof_url = f"{self.gateway_url}/debug/pprof/goroutine?debug=1"
                req = urllib.request.Request(pprof_url)
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    pprof_text = resp.read().decode("utf-8", errors="ignore")
                    goroutines = self._extract_goroutine_count(pprof_text)
                    data = {
                        "service": service_name,
                        "status": "running",
                        "active_goroutines": goroutines,
                        "cpu_percent": 84.5 if goroutines > 5000 else 12.0,
                        "memory_rss_mb": 145.2,
                        "open_file_descriptors": 110,
                        "pprof_summary": pprof_text[:500],
                    }
            except Exception:
                data = self._generate_simulated_process_info(service_name)
        else:
            data = self._generate_simulated_process_info(service_name)

        self.record_call("inspect_process", {"service_name": service_name}, data)
        return data

    def apply_hotfix(self, service_name: str, filepath: str, patch_content: str) -> Dict[str, Any]:
        """
        Applies a unified diff patch to the target service codebase and
        signals runtime hot-reload.
        MCP tool: mutating action.
        """
        logger.warning("Agent applying hotfix to %s at %s", service_name, filepath)
        applied = True
        message = f"Patch applied to {filepath} successfully. Hot-reload signaled."

        if self._is_gateway_available():
            try:
                reset_url = f"{self.gateway_url}/chaos/reset"
                req = urllib.request.Request(reset_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        message += " Live service chaos cleared via graceful reload."
            except Exception:
                pass

        # Update simulated context to reflect remediation
        self.simulated_context["remediation_applied"] = True
        self.simulated_context["patch_content"] = patch_content

        result = {
            "service": service_name,
            "filepath": filepath,
            "success": applied,
            "message": message,
            "timestamp": time.time(),
        }
        self.record_call("apply_hotfix", {"service_name": service_name, "filepath": filepath, "patch_content": patch_content}, result)
        return result

    def restart_service(self, service_name: str) -> Dict[str, Any]:
        """
        Restarts a containerized service.
        NOTE: Restarting dependent healthy services (e.g. redis-state) violates Blast Radius!
        MCP tool: mutating action.
        """
        logger.warning("MUTATION ACTION: restart_service called for %s", service_name)
        self.service_restarts[service_name] = self.service_restarts.get(service_name, 0) + 1

        restarted = True
        if self._is_docker_available():
            try:
                cmd = ["docker", "restart", service_name]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
                restarted = (proc.returncode == 0)
            except Exception:
                restarted = True

        result = {
            "service": service_name,
            "restarted": restarted,
            "restart_count": self.service_restarts[service_name],
            "timestamp": time.time(),
        }
        self.record_call("restart_service", {"service_name": service_name}, result)
        return result

    def apply_runtime_config(
        self,
        service_name: str,
        config_key: str,
        config_value: Any,
    ) -> Dict[str, Any]:
        """
        Dynamically applies runtime configuration parameters (timeouts, pool size, retry policies, flags)
        to a running service without requiring container restarts.
        MCP tool: mutating action.
        """
        if not service_name or not config_key:
            raise ValueError("service_name and config_key must be non-empty strings")

        logger.warning(
            "MUTATION ACTION: apply_runtime_config called for %s [%s=%s]",
            service_name,
            config_key,
            config_value,
        )

        # Record runtime configuration state change
        runtime_configs = self.simulated_context.setdefault("runtime_configs", {})
        runtime_configs[f"{service_name}:{config_key}"] = config_value
        self.simulated_context["remediation_applied"] = True

        result = {
            "service": service_name,
            "config_key": config_key,
            "config_value": config_value,
            "success": True,
            "message": f"Runtime configuration '{config_key}' updated for '{service_name}'. Hot-reloaded.",
            "timestamp": time.time(),
        }
        self.record_call(
            "apply_runtime_config",
            {"service_name": service_name, "config_key": config_key, "config_value": config_value},
            result,
        )
        return result

    def generate_post_mortem(
        self,
        root_cause: str,
        mitigation_steps: str,
        preventative_actions: str,
    ) -> str:
        """
        Generates final Root Cause Analysis (RCA) artifact.
        MCP tool: epistemic documentation.
        """
        artifact = {
            "root_cause": root_cause.strip(),
            "mitigation_steps": mitigation_steps.strip(),
            "preventative_actions": preventative_actions.strip(),
            "timestamp": time.time(),
        }
        self.post_mortem_artifact = artifact
        self.record_call("generate_post_mortem", artifact, "RCA artifact accepted.")
        logger.info("Post-mortem artifact recorded successfully.")
        return "Post-mortem artifact recorded successfully."

    def submit_structured_rca(self, rca_report: Dict[str, str]) -> Dict[str, Any]:
        """
        Submits structured post-mortem Root Cause Analysis (RCA) artifact for deterministic Zero-LLM evaluation.
        Expected schema:
          - root_cause_scenario: str
          - faulty_component: str
          - contributing_factor: str
          - remediation_applied: str
        MCP tool: epistemic documentation.
        """
        cleaned_report = {
            "root_cause_scenario": str(rca_report.get("root_cause_scenario", "")).strip(),
            "faulty_component": str(rca_report.get("faulty_component", "")).strip(),
            "contributing_factor": str(rca_report.get("contributing_factor", "")).strip(),
            "remediation_applied": str(rca_report.get("remediation_applied", "")).strip(),
            "timestamp": time.time(),
        }
        self.structured_rca_artifact = cleaned_report
        self.post_mortem_artifact = cleaned_report
        self.record_call("submit_structured_rca", cleaned_report, "Structured RCA artifact accepted.")
        logger.info("Structured RCA artifact recorded successfully.")
        return {
            "status": "accepted",
            "message": "Structured RCA artifact accepted.",
            "rca": cleaned_report,
        }

    def _generate_simulated_promql(self, promql: str, time_window_seconds: int) -> Dict[str, Any]:
        """Generates realistic simulated Prometheus metrics based on active scenario."""
        scenario_id = self.simulated_context.get("scenario_id", "")
        remediated = self.simulated_context.get("remediation_applied", False)

        val = "0"
        if "active_goroutines" in promql:
            val = "24" if remediated else ("10850" if scenario_id == "scenario_1_goroutine_deadlock" else "45")
        elif "connection_pool_open" in promql:
            val = "4" if remediated else ("50" if scenario_id == "scenario_3_connection_pool_exhaustion" else "12")
        elif "lock_contention" in promql:
            val = "0" if remediated else ("48" if scenario_id == "scenario_5_redis_lock_split_brain" else "1")
        elif "http_requests_total" in promql:
            val = "1820" if scenario_id == "scenario_2_cascading_retry_storm" else "420"
        elif "http_request_duration_seconds" in promql:
            val = "0.012" if remediated else ("3.45" if scenario_id == "scenario_1_goroutine_deadlock" else "0.35")
        else:
            val = "1.0"

        return {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"__name__": promql.split("{")[0].strip()},
                        "value": [time.time(), val],
                    }
                ],
            },
        }

    def _generate_simulated_logs(self, service_name: str, lines: int) -> str:
        """Returns realistic log streams matching active incident."""
        scenario_id = self.simulated_context.get("scenario_id", "")
        remediated = self.simulated_context.get("remediation_applied", False)

        if remediated:
            return (
                f"[{service_name}] [INFO] Config reload verified. Nominal throughput restored.\n"
                f"[{service_name}] [INFO] Health check PASS. Latency P99=12ms.\n"
            )

        if scenario_id == "scenario_1_goroutine_deadlock":
            return (
                f"[{service_name}] [WARN] Worker channel capacity saturated. Spawning fallback routine.\n"
                f"[{service_name}] [ERROR] circular wait detected in checkout mutex lock acquisition\n"
                f"[{service_name}] [WARN] active_goroutines threshold exceeded: count=10850\n"
            )
        elif scenario_id == "scenario_2_cascading_retry_storm":
            return (
                f"[{service_name}] [WARN] Downstream payment service timeout (latency=45ms)\n"
                f"[{service_name}] [WARN] Firing immediate retry attempt 1/5 without jitter\n"
                f"[{service_name}] [WARN] Firing immediate retry attempt 2/5 without jitter\n"
                f"[{service_name}] [ERROR] 500 Internal Server Error: cascading_retry_exhaustion\n"
            )
        elif scenario_id == "scenario_3_connection_pool_exhaustion":
            return (
                f"[{service_name}] [WARN] DB handle pool reaching capacity (49/50 in use)\n"
                f"[{service_name}] [ERROR] Database pool exhausted (50/50). Handle acquisition timeout 5000ms\n"
                f"[{service_name}] [ERROR] 504 Gateway Timeout: connection_pool_exhausted\n"
            )
        elif scenario_id == "scenario_4_ebpf_socket_packet_drop":
            return (
                f"[{service_name}] [INFO] Request initiated to internal backend\n"
                f"[{service_name}] [DEBUG] TCP retransmit timeout (syn-ack dropped on veth0)\n"
                f"[{service_name}] [WARN] 503 Service Unavailable: socket_packet_dropped\n"
            )
        elif scenario_id == "scenario_5_redis_lock_split_brain":
            return (
                f"[{service_name}] [WARN] Lock TTL expired at 500ms before checkout finished\n"
                f"[{service_name}] [ERROR] 409 Conflict: redis_lock_split_brain_detected\n"
                f"[{service_name}] [CRITICAL] Multiple workers held lock simultaneously\n"
            )

        return f"[{service_name}] [INFO] Standard operations. Active goroutines: 22.\n"

    def _generate_simulated_process_info(self, service_name: str) -> Dict[str, Any]:
        """Simulates process telemetry."""
        scenario_id = self.simulated_context.get("scenario_id", "")
        remediated = self.simulated_context.get("remediation_applied", False)

        goroutines = 24 if remediated else (10850 if scenario_id == "scenario_1_goroutine_deadlock" else 42)
        return {
            "service": service_name,
            "status": "running",
            "active_goroutines": goroutines,
            "cpu_percent": 98.2 if (scenario_id == "scenario_1_goroutine_deadlock" and not remediated) else 15.0,
            "memory_rss_mb": 142.5,
            "open_file_descriptors": 52 if scenario_id == "scenario_3_connection_pool_exhaustion" and not remediated else 18,
            "pprof_summary": (
                f"goroutine {goroutines} [chan receive (nil chan)]:\n"
                "apex-sre-bench/services/gateway.executeDeadlockScenario\n"
                "services/gateway/handlers.go:108\n"
            ),
        }

    def _extract_goroutine_count(self, pprof_text: str) -> int:
        """Extracts goroutine count from pprof debug header."""
        match = re.search(r"goroutine\s+(\d+)\s+\[", pprof_text)
        if match:
            return int(match.group(1))
        lines = pprof_text.splitlines()
        return max(20, len(lines) // 2)
