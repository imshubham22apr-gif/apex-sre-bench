"""
Deterministic Mock Agents for APEX-SRE-Bench.

Implements two contrasting agent personas:
1. ExpertSRE: Exhibits rigorous epistemic discipline (telemetry query -> hypothesis -> targeted hotfix -> verification -> post-mortem). Achieves R_episode >= 0.95.
2. NaiveJuniorAgent: Exhibits the "guess-and-restart" pathology (restarts healthy databases, skips telemetry, triggers blast radius breach). Achieves R_episode = 0.0.
"""

from abc import ABC, abstractmethod
import logging
import time
from typing import Any, Dict

from chaos.scenarios import ChaosScenario, get_scenario_by_id
from tools.sre_tools import SREToolEnvironment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("MockAgent")


class BaseAgent(ABC):
    """Abstract base class for benchmark evaluation agents."""

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def solve_incident(self, scenario_id: str, tools: SREToolEnvironment) -> None:
        """Executes the incident response workflow using the provided tools."""
        pass


class ExpertSRE(BaseAgent):
    """
    Expert SRE persona demonstrating optimal epistemic discipline.
    Strictly interrogates telemetry before applying zero-blast-radius remediation.
    """

    def __init__(self) -> None:
        super().__init__(name="ExpertSRE")

    def solve_incident(self, scenario_id: str, tools: SREToolEnvironment) -> None:
        scenario = get_scenario_by_id(scenario_id)
        if not scenario:
            raise ValueError(f"Unknown scenario: {scenario_id}")

        logger.info("[ExpertSRE] Initiating epistemic diagnosis for %s", scenario_id)

        # Step 0: Ingest Incident Alert from Alertmanager / PagerDuty
        alert = tools.get_incident_alert()
        logger.info("[ExpertSRE] Ingested firing alert: %s (%s)", alert.get("alert_name"), alert.get("severity"))

        # Step 1: Telemetry Interrogation - Query Prometheus request rates & latencies
        tools.query_prometheus("http_request_duration_seconds", time_window_seconds=60)
        tools.query_prometheus("http_requests_total", time_window_seconds=60)

        # Step 2: Telemetry Interrogation - Specific metric diagnosis
        if scenario_id == "scenario_1_goroutine_deadlock":
            tools.query_prometheus("active_goroutines", time_window_seconds=60)
            tools.inspect_process("api-gateway")
        elif scenario_id == "scenario_3_connection_pool_exhaustion":
            tools.query_prometheus("connection_pool_open", time_window_seconds=60)
            tools.inspect_process("api-gateway")
        elif scenario_id == "scenario_5_redis_lock_split_brain":
            tools.query_prometheus("lock_contention_events_total", time_window_seconds=60)

        # Step 3: Telemetry Interrogation - Container Log Audit
        tools.tail_service_logs("api-gateway", lines=50)

        # Step 4: Targeted Non-Destructive Remediation (Hotfix to gateway code)
        patch_path = "services/gateway/handlers.go"
        tools.apply_hotfix("api-gateway", patch_path, scenario.remediation_patch)

        # Step 5: Telemetry Verification - Post-fix observation
        tools.query_prometheus("http_request_duration_seconds", time_window_seconds=30)

        # Step 6: Epistemic Documentation - Root Cause Analysis
        if hasattr(scenario, "structured_rca") and scenario.structured_rca:
            tools.submit_structured_rca(scenario.structured_rca.to_dict())
        tools.generate_post_mortem(
            root_cause=scenario.ground_truth_rca,
            mitigation_steps=f"Applied unified diff patch to {patch_path}; cleared blocked concurrency state.",
            preventative_actions="Added strict timeout boundaries, circuit breaker guards, and automated lint rules.",
        )
        logger.info("[ExpertSRE] Completed remediation and RCA documentation.")


class NaiveJuniorAgent(BaseAgent):
    """
    Pathological agent persona exemplifying the 'Guess-and-Restart' breakdown.
    Immediately mutates dependent infrastructure, triggering cascading outages.
    """

    def __init__(self) -> None:
        super().__init__(name="NaiveJuniorAgent")

    def solve_incident(self, scenario_id: str, tools: SREToolEnvironment) -> None:
        logger.warning("[NaiveJuniorAgent] Encountered high latency! Guessing Redis is hung...")

        # PATHOLOGY: Blindly restart the healthy state store without consulting telemetry
        tools.restart_service("redis-state")

        # PATHOLOGY: Blindly restart metrics pipeline
        tools.restart_service("prometheus")

        # PATHOLOGY: Apply unvalidated random patch to configuration
        tools.apply_hotfix("api-gateway", "services/gateway/handlers.go", "// blind unverified edit\n")

        # Incomplete / missing RCA (erroneous structured RCA + invalid post-mortem)
        tools.submit_structured_rca({
            "root_cause_scenario": "unknown_failure",
            "faulty_component": "redis-state",
            "contributing_factor": "unverified_speculation",
            "remediation_applied": "restart_healthy_services",
        })
        tools.generate_post_mortem(
            root_cause="Server was slow so I restarted redis",
            mitigation_steps="Restarted services",
            preventative_actions="None",
        )
        logger.warning("[NaiveJuniorAgent] Execution finished (Secondary outages induced).")
