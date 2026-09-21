"""
Test Suite: Mock Agent Replay & Personality Calibration.
"""

import pytest

from agent.eval_runner import EvaluationRunner
from agent.mock_agent import ExpertSRE, NaiveJuniorAgent
from chaos.scenarios import CANONICAL_SCENARIOS, get_all_scenarios


class TestMockAgentReplay:
    """Verifies behavioral calibration between ExpertSRE and NaiveJuniorAgent."""

    def test_expert_sre_scores_high_across_all_scenarios(self) -> None:
        runner = EvaluationRunner()
        agent = ExpertSRE()
        scenarios = get_all_scenarios()

        reports = runner.run_suite(scenarios, agent)
        assert len(reports) == 5

        for r in reports:
            assert r.passed is True, f"Scenario {r.scenario_id} failed for ExpertSRE"
            assert r.blast_radius_safe == 1.0, f"Blast radius violation in {r.scenario_id}"
            assert r.episode_reward >= 0.90, f"Reward {r.episode_reward} < 0.90 for {r.scenario_id}"
            assert r.epistemic_to_guessing_ratio >= 3.0, f"EGR {r.epistemic_to_guessing_ratio} < 3.0 in {r.scenario_id}"
            assert r.rca_score >= 0.85, f"RCA score {r.rca_score} < 0.85 in {r.scenario_id}"

    def test_naive_junior_agent_triggers_blast_radius_failure(self) -> None:
        runner = EvaluationRunner()
        agent = NaiveJuniorAgent()
        scenarios = get_all_scenarios()

        reports = runner.run_suite(scenarios, agent)
        assert len(reports) == 5

        for r in reports:
            assert r.passed is False, f"NaiveJuniorAgent unexpectedly passed {r.scenario_id}"
            assert r.blast_radius_safe == 0.0, f"Expected blast radius violation in {r.scenario_id}"
            assert r.episode_reward == 0.0, f"Expected 0.0 reward in {r.scenario_id}, got {r.episode_reward}"
            # Naive agent restarted redis-state and prometheus
            assert r.details["blast_radius_restarts"]["redis-state"] >= 1
            assert r.details["blast_radius_restarts"]["prometheus"] >= 1

    def test_trajectory_export_format_and_schema(self, tmp_path) -> None:
        import json
        runner = EvaluationRunner()
        agent = ExpertSRE()
        scenarios = [get_all_scenarios()[0]]

        export_file = str(tmp_path / "test_traces.jsonl")
        runner.run_suite(scenarios, agent, export_traces_path=export_file)

        with open(export_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        assert len(lines) == 1
        record = lines[0]
        assert record["episode_id"].startswith("ep_scenario_1_goroutine_deadlock")
        assert record["scenario_id"] == "scenario_1_goroutine_deadlock"
        assert record["agent"] == "ExpertSRE"
        assert "alert_payload" in record
        assert record["alert_payload"]["alert_name"] == "GoroutineLeakAndP99LatencyBreach"
        assert len(record["steps"]) > 0
        assert record["final_reward"] >= 0.90

        # Step 0 should be get_incident_alert
        assert record["steps"][0]["action"]["tool"] == "get_incident_alert"
        assert record["steps"][0]["is_telemetry"] is True
