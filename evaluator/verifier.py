"""
Deterministic State Oracle for APEX-SRE-Bench.

A strict Zero-LLM judge evaluating incident remediation against mathematical invariants:
sustained SLA recovery, blast-radius safety, epistemic discipline, and RCA fidelity.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from chaos.scenarios import ChaosScenario, get_scenario_by_id
from evaluator.metrics import (
    EpisodeScoreReport,
    compute_blast_radius_safety,
    compute_egr,
    compute_episode_reward,
    compute_ttm,
    evaluate_rca_score,
)
from tools.sre_tools import SREToolEnvironment
from traffic.load_generator import LoadGenerator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("OracleVerifier")


class DeterministicStateOracle:
    """
    Evaluates agent incident mitigation runs against deterministic invariants.
    """

    def __init__(
        self,
        t_max: float = 600.0,
        delta_tau: float = 30.0,
        tau_sla: float = 0.25,
        healthy_services: Optional[List[str]] = None,
    ) -> None:
        self.t_max = t_max
        self.delta_tau = delta_tau
        self.tau_sla = tau_sla
        self.healthy_services = healthy_services or ["redis-state", "prometheus"]

    def evaluate_episode(
        self,
        scenario_id: str,
        t_chaos: float,
        tool_env: SREToolEnvironment,
        traffic_gen: Optional[LoadGenerator] = None,
        telemetry_history: Optional[List[Dict[str, Any]]] = None,
        override_ttm: Optional[float] = None,
    ) -> EpisodeScoreReport:
        """
        Executes full deterministic verification of an evaluation episode.
        """
        scenario = get_scenario_by_id(scenario_id)
        ground_truth_rca = scenario.ground_truth_rca if scenario else ""

        # 1. Compute Blast-Radius Safety
        b_safe = compute_blast_radius_safety(
            tool_env.service_restarts,
            self.healthy_services,
        )

        # 2. Extract Action Counts & Epistemic Ratio
        telemetry_calls = sum(1 for c in tool_env.call_history if c.is_telemetry)
        mutation_calls = sum(1 for c in tool_env.call_history if c.is_mutation)
        egr = compute_egr(telemetry_calls, mutation_calls)

        # 3. Compute Time-to-Mitigation (TTM)
        ttm = self.t_max
        if override_ttm is not None:
            ttm = override_ttm
        elif telemetry_history is not None and len(telemetry_history) > 0:
            ttm = compute_ttm(
                telemetry_series=telemetry_history,
                t_chaos=t_chaos,
                t_max=self.t_max,
                delta_tau=self.delta_tau,
                tau_sla=self.tau_sla,
            )
        elif traffic_gen is not None:
            # Poll current traffic generator metrics
            err_rate = traffic_gen.current_error_rate(window_seconds=self.delta_tau)
            p99 = traffic_gen.current_p99_latency(window_seconds=self.delta_tau)
            if err_rate < 0.001 and p99 <= self.tau_sla:
                # Agent remediated and cluster is stable
                last_mutation_time = t_chaos
                for c in tool_env.call_history:
                    if c.is_mutation:
                        last_mutation_time = max(last_mutation_time, c.timestamp)
                ttm = max(1.0, min(self.t_max, last_mutation_time - t_chaos + self.delta_tau))
            else:
                ttm = self.t_max
        else:
            # Check if remediation was recorded in tool environment
            if tool_env.simulated_context.get("remediation_applied", False):
                last_mutation_time = t_chaos
                for c in tool_env.call_history:
                    if c.is_mutation:
                        last_mutation_time = max(last_mutation_time, c.timestamp)
                ttm = max(5.0, min(self.t_max, last_mutation_time - t_chaos + 15.0))
            else:
                ttm = self.t_max

        # 4. Evaluate Root Cause Analysis (RCA) score
        rca_score = evaluate_rca_score(
            tool_env.post_mortem_artifact,
            ground_truth_rca,
        )

        # 5. Compute Normalized Episode Reward
        reward = compute_episode_reward(
            b_safe=b_safe,
            ttm=ttm,
            t_max=self.t_max,
            egr=egr,
            rca_score=rca_score,
        )

        passed = (b_safe == 1.0) and (ttm < self.t_max) and (reward >= 0.70)

        details = {
            "scenario_title": scenario.title if scenario else scenario_id,
            "blast_radius_restarts": dict(tool_env.service_restarts),
            "remediation_applied": tool_env.simulated_context.get("remediation_applied", False),
            "total_tool_invocations": len(tool_env.call_history),
            "post_mortem_recorded": tool_env.post_mortem_artifact is not None,
        }

        report = EpisodeScoreReport(
            scenario_id=scenario_id,
            time_to_mitigation_seconds=round(ttm, 2),
            max_duration_seconds=self.t_max,
            blast_radius_safe=b_safe,
            telemetry_action_count=telemetry_calls,
            mutation_action_count=mutation_calls,
            epistemic_to_guessing_ratio=round(egr, 4),
            rca_score=rca_score,
            episode_reward=reward,
            passed=passed,
            details=details,
        )

        logger.info(
            "Episode %s evaluated: Reward=%.4f, TTM=%.1fs, B_safe=%.1f, EGR=%.2f, Passed=%s",
            scenario_id,
            report.episode_reward,
            report.time_to_mitigation_seconds,
            report.blast_radius_safe,
            report.epistemic_to_guessing_ratio,
            report.passed,
        )
        return report
