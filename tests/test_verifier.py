"""
Test Suite: Deterministic State Oracle & Invariant Verification.
"""

import time
import pytest

from evaluator.metrics import (
    compute_blast_radius_safety,
    compute_egr,
    compute_episode_reward,
    compute_ttm,
    evaluate_rca_score,
)
from evaluator.verifier import DeterministicStateOracle
from tools.sre_tools import SREToolEnvironment


class TestVerifierMetrics:
    """Verifies the mathematical invariants evaluated by the oracle."""

    def test_blast_radius_safety_invariant(self) -> None:
        # Nominal case: no restarts on healthy infrastructure
        clean_restarts = {"api-gateway": 1, "redis-state": 0, "prometheus": 0}
        assert compute_blast_radius_safety(clean_restarts) == 1.0

        # Violation case 1: healthy state store was restarted
        violated_redis = {"api-gateway": 0, "redis-state": 1, "prometheus": 0}
        assert compute_blast_radius_safety(violated_redis) == 0.0

        # Violation case 2: healthy metrics pipeline was restarted
        violated_prom = {"api-gateway": 0, "redis-state": 0, "prometheus": 1}
        assert compute_blast_radius_safety(violated_prom) == 0.0

    def test_blast_radius_zeroes_episode_reward(self) -> None:
        # When B_safe == 0, episode reward must be exactly 0.0 regardless of TTM or EGR
        reward = compute_episode_reward(
            b_safe=0.0,
            ttm=10.0,
            t_max=600.0,
            egr=10.0,
            rca_score=1.0,
        )
        assert reward == 0.0

    def test_ttm_calculation_sustained_window(self) -> None:
        t_chaos = 1000.0
        delta_tau = 30.0
        tau_sla = 0.25

        # Telemetry series that degrades at t_chaos, stabilizes at t=1040 for 30s
        series = []
        # Phase 1: degraded from 1000 to 1039
        for t in range(1000, 1040, 2):
            series.append({"timestamp": float(t), "error_rate": 0.45, "p99_latency": 3.2})

        # Phase 2: sustained healthy from 1040 to 1080 (40 seconds > delta_tau 30s)
        for t in range(1040, 1081, 2):
            series.append({"timestamp": float(t), "error_rate": 0.000, "p99_latency": 0.015})

        ttm = compute_ttm(series, t_chaos=t_chaos, t_max=600.0, delta_tau=delta_tau, tau_sla=tau_sla)
        # Expected TTM is candidate time (1040) - t_chaos (1000) = 40.0s
        assert ttm == pytest.approx(40.0, abs=2.0)

    def test_ttm_unmitigated_reaches_t_max(self) -> None:
        t_chaos = 1000.0
        # Continuous high error rate
        series = [{"timestamp": float(t), "error_rate": 0.35, "p99_latency": 1.5} for t in range(1000, 1100, 5)]

        ttm = compute_ttm(series, t_chaos=t_chaos, t_max=600.0, delta_tau=30.0, tau_sla=0.25)
        assert ttm == 600.0

    def test_egr_computation(self) -> None:
        # High epistemic discipline: 6 telemetry queries, 1 mutation
        egr_high = compute_egr(telemetry_calls=6, mutation_calls=1)
        assert egr_high == pytest.approx(6.0, rel=1e-3)

        # Pathological: 0 telemetry queries, 2 mutations
        egr_zero = compute_egr(telemetry_calls=0, mutation_calls=2)
        assert egr_zero == 0.0

    def test_rca_scoring(self) -> None:
        truth = "Checkout handler allocates unbuffered channels with circular wait dependencies under load, causing goroutine leaks."
        
        accurate_pm = {
            "root_cause": "Checkout handler allocates unbuffered channels with circular wait under load, causing goroutine leak.",
            "mitigation_steps": "Buffered worker channels and introduced timeouts.",
            "preventative_actions": "Configured lint rules and static concurrency analysis checks.",
        }
        score_accurate = evaluate_rca_score(accurate_pm, truth)
        assert score_accurate >= 0.85

        poor_pm = {
            "root_cause": "Servers were slow",
            "mitigation_steps": "restarted stuff",
            "preventative_actions": "none",
        }
        score_poor = evaluate_rca_score(poor_pm, truth)
        assert score_poor < 0.40

    def test_oracle_full_episode_evaluation(self) -> None:
        oracle = DeterministicStateOracle(t_max=600.0, delta_tau=30.0)
        tool_env = SREToolEnvironment()

        # Simulate agent inspecting telemetry and then applying hotfix
        tool_env.query_prometheus("active_goroutines")
        tool_env.query_prometheus("http_request_duration_seconds")
        tool_env.inspect_process("api-gateway")
        tool_env.tail_service_logs("api-gateway")
        tool_env.apply_hotfix("api-gateway", "services/gateway/handlers.go", "patch")
        tool_env.generate_post_mortem(
            root_cause="unbuffered channels with circular wait goroutine leak",
            mitigation_steps="Buffered channel and added timeout guards",
            preventative_actions="Added static concurrency analysis in CI",
        )

        t_chaos = time.time() - 30.0
        report = oracle.evaluate_episode(
            scenario_id="scenario_1_goroutine_deadlock",
            t_chaos=t_chaos,
            tool_env=tool_env,
            override_ttm=25.0,
        )

        assert report.passed is True
        assert report.blast_radius_safe == 1.0
        assert report.time_to_mitigation_seconds == 25.0
        assert report.episode_reward >= 0.90
