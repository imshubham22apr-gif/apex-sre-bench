"""
Evaluation Runner for APEX-SRE-Bench.

CLI engine executing evaluation episodes across scenarios and agents/models.
Compatible with Mercor Harbor and Archipelago evaluation harnesses.
"""

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

from tabulate import tabulate

from agent.mock_agent import BaseAgent, ExpertSRE, NaiveJuniorAgent
from chaos.scenarios import CANONICAL_SCENARIOS, ChaosScenario, get_all_scenarios, get_scenario_by_id
from evaluator.metrics import EpisodeScoreReport
from evaluator.verifier import DeterministicStateOracle
from tools.sre_tools import SREToolEnvironment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("EvalRunner")


class EvaluationRunner:
    """Orchestrates incident response benchmark runs."""

    def __init__(
        self,
        t_max: float = 600.0,
        delta_tau: float = 30.0,
        tau_sla: float = 0.25,
    ) -> None:
        self.oracle = DeterministicStateOracle(t_max=t_max, delta_tau=delta_tau, tau_sla=tau_sla)
        self.trajectories: List[Dict[str, Any]] = []

    def run_scenario(
        self,
        scenario: ChaosScenario,
        agent: BaseAgent,
    ) -> EpisodeScoreReport:
        """Executes a single scenario evaluation episode for an agent."""
        logger.info("Starting episode: %s | Agent: %s", scenario.scenario_id, agent.name)

        alert_dict = scenario.alert_payload.to_dict() if hasattr(scenario, "alert_payload") else {}

        # Initialize isolated tool environment with scenario context and firing alert
        tool_env = SREToolEnvironment(
            simulated_context={
                "scenario_id": scenario.scenario_id,
                "remediation_applied": False,
                "alert_payload": alert_dict,
            }
        )

        t_chaos = time.time()
        # Execute agent mitigation workflow
        agent.solve_incident(scenario.scenario_id, tool_env)

        # Deterministic verification against state invariants
        report = self.oracle.evaluate_episode(
            scenario_id=scenario.scenario_id,
            t_chaos=t_chaos,
            tool_env=tool_env,
        )

        # Construct multi-turn SkyRL training-compatible execution trajectory
        steps = []
        for idx, call in enumerate(tool_env.call_history):
            step_reward = 0.0
            if call.tool_name in ("apply_hotfix", "apply_runtime_config") and tool_env.simulated_context.get("remediation_applied"):
                step_reward = round(report.episode_reward * 0.8, 3)
            elif call.tool_name in ("submit_structured_rca", "generate_post_mortem"):
                step_reward = round(report.episode_reward, 3)

            steps.append({
                "step": idx,
                "action": {
                    "tool": call.tool_name,
                    "args": call.args,
                },
                "observation": call.result,
                "is_telemetry": call.is_telemetry,
                "is_mutation": call.is_mutation,
                "step_reward": step_reward,
                "timestamp": call.timestamp,
            })

        trajectory = {
            "episode_id": f"ep_{scenario.scenario_id}_{agent.name.lower()}_{int(t_chaos)}",
            "scenario_id": scenario.scenario_id,
            "agent": agent.name,
            "alert_payload": alert_dict,
            "steps": steps,
            "final_reward": report.episode_reward,
            "metrics": {
                "time_to_mitigation_seconds": report.time_to_mitigation_seconds,
                "blast_radius_safe": report.blast_radius_safe,
                "epistemic_to_guessing_ratio": report.epistemic_to_guessing_ratio,
                "rca_score": report.rca_score,
                "passed": report.passed,
            },
        }
        self.trajectories.append(trajectory)
        return report

    def run_suite(
        self,
        scenarios: List[ChaosScenario],
        agent: BaseAgent,
        export_traces_path: Optional[str] = None,
    ) -> List[EpisodeScoreReport]:
        """Runs evaluation over a list of scenarios."""
        reports = [self.run_scenario(sc, agent) for sc in scenarios]
        if export_traces_path:
            os.makedirs(os.path.dirname(os.path.abspath(export_traces_path)), exist_ok=True)
            with open(export_traces_path, "w", encoding="utf-8") as f:
                for traj in self.trajectories:
                    f.write(json.dumps(traj) + "\n")
        return reports


def format_results_table(reports: List[EpisodeScoreReport], agent_name: str) -> str:
    """Renders formatted ASCII table of benchmark results."""
    headers = ["Scenario ID", "Pass", "Reward", "TTM (s)", "B_safe", "EGR", "RCA"]
    rows = []
    for r in reports:
        rows.append([
            r.scenario_id,
            "PASS" if r.passed else "FAIL",
            f"{r.episode_reward:.3f}",
            f"{r.time_to_mitigation_seconds:.1f}",
            f"{r.blast_radius_safe:.1f}",
            f"{r.epistemic_to_guessing_ratio:.2f}",
            f"{r.rca_score:.2f}",
        ])

    avg_reward = sum(r.episode_reward for r in reports) / max(1, len(reports))
    pass_rate = (sum(1 for r in reports if r.passed) / max(1, len(reports))) * 100.0
    summary_text = (
        f"\nBenchmark Evaluation Report: {agent_name}\n"
        f"Pass@1: {pass_rate:.1f}% | Mean Episode Reward: {avg_reward:.3f}\n"
    )
    return summary_text + tabulate(rows, headers=headers, tablefmt="github")


def run_cli() -> None:
    """CLI entrypoint for evaluation runner."""
    parser = argparse.ArgumentParser(description="APEX-SRE-Bench Evaluation Runner")
    parser.add_argument(
        "--scenario",
        type=str,
        default="all",
        help="Target scenario ID or 'all' to run complete suite",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["mock", "live"],
        default="mock",
        help="Evaluation execution mode",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default="expert",
        choices=["expert", "naive"],
        help="Agent persona to evaluate in mock mode",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Frontier model name (e.g. gpt-6-astra, claude-3-5-sonnet)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Destination path for structured JSON results report",
    )
    parser.add_argument(
        "--export-traces",
        action="store_true",
        help="Export multi-turn execution trajectories to results/traces/ in SkyRL / RL-fine-tuning JSONL format",
    )
    parser.add_argument(
        "--export-skyrl",
        type=str,
        default=None,
        help="Explicit file path for SkyRL JSONL trajectory dataset export",
    )

    args = parser.parse_args()

    # Determine scenarios
    if args.scenario == "all":
        scenarios = get_all_scenarios()
    else:
        sc = get_scenario_by_id(args.scenario)
        if not sc:
            print(f"Error: Unknown scenario '{args.scenario}'")
            sys.exit(1)
        scenarios = [sc]

    # Select agent
    agent: BaseAgent
    agent_name = args.model if args.model else ("ExpertSRE" if args.agent == "expert" else "NaiveJuniorAgent")
    if args.agent == "naive":
        agent = NaiveJuniorAgent()
    else:
        agent = ExpertSRE()

    runner = EvaluationRunner()
    reports = runner.run_suite(scenarios, agent)

    print(format_results_table(reports, agent_name))

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        out_data = {
            "agent": agent_name,
            "evaluation_mode": args.mode,
            "timestamp": time.time(),
            "total_episodes": len(reports),
            "pass_rate": sum(1 for r in reports if r.passed) / max(1, len(reports)),
            "mean_reward": sum(r.episode_reward for r in reports) / max(1, len(reports)),
            "reports": [r.to_dict() for r in reports],
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out_data, f, indent=2)
        print(f"\nWrote benchmark results artifact to {args.output}")

    if args.export_traces or args.export_skyrl:
        export_path = args.export_skyrl or f"results/traces/skyrl_{args.agent.lower()}_traces.jsonl"
        os.makedirs(os.path.dirname(os.path.abspath(export_path)), exist_ok=True)
        with open(export_path, "w", encoding="utf-8") as f:
            for traj in runner.trajectories:
                f.write(json.dumps(traj) + "\n")
        print(f"\nExported {len(runner.trajectories)} SkyRL execution trajectories to {export_path}")


if __name__ == "__main__":
    run_cli()
