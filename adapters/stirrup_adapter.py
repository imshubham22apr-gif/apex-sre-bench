"""
Mercor Harbor Stirrup Runner Adapter for APEX-SRE-Bench.

Implements CLI contract expected by the Harbor benchmark harness:
`--host`, `--port`, `--task_id`, `--scenario`, `--mode`, `--output`.
Adapts APEX-SRE-Bench evaluation runs into Harbor / BUA judge compatible result schemas.
"""

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

from agent.mock_agent import BaseAgent, ExpertSRE, NaiveJuniorAgent
from chaos.scenarios import get_all_scenarios, get_scenario_by_id
from evaluator.verifier import DeterministicStateOracle
from tools.sre_tools import SREToolEnvironment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [Harbor-Stirrup]: %(message)s")
logger = logging.getLogger("HarborStirrupAdapter")


class StirrupHarborAdapter:
    """
    Adapter bridging Harbor's execution orchestrator with APEX-SRE-Bench.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        task_id: Optional[str] = None,
    ) -> None:
        self.host = host or "localhost"
        self.port = port or 8000
        self.task_id = task_id or f"harbor-sre-{int(time.time())}"
        self.oracle = DeterministicStateOracle()

    def execute_task(
        self,
        scenario_id: str,
        agent: Optional[BaseAgent] = None,
    ) -> Dict[str, Any]:
        """
        Executes an SRE incident mitigation task and formats results
        conforming to Harbor / BUA judge schemas.
        """
        scenario = get_scenario_by_id(scenario_id)
        if not scenario:
            raise ValueError(f"Harbor task mapping failed: unknown scenario '{scenario_id}'")

        active_agent = agent or ExpertSRE()
        logger.info("Executing Harbor task '%s' using agent '%s'", self.task_id, active_agent.name)

        alert_dict = scenario.alert_payload.to_dict() if hasattr(scenario, "alert_payload") else {}
        tool_env = SREToolEnvironment(
            simulated_context={
                "scenario_id": scenario_id,
                "remediation_applied": False,
                "alert_payload": alert_dict,
            }
        )

        t_chaos = time.time()
        active_agent.solve_incident(scenario_id, tool_env)

        report = self.oracle.evaluate_episode(
            scenario_id=scenario_id,
            t_chaos=t_chaos,
            tool_env=tool_env,
        )

        # Harbor / BUA format schema
        harbor_result = {
            "task_id": self.task_id,
            "benchmark": "apex-sre-bench",
            "scenario_id": scenario_id,
            "agent": active_agent.name,
            "score": report.episode_reward,
            "passed": report.passed,
            "verdict": "SUCCESS" if report.passed else "FAILURE",
            "metrics": {
                "time_to_mitigation_seconds": report.time_to_mitigation_seconds,
                "blast_radius_safe": report.blast_radius_safe,
                "epistemic_to_guessing_ratio": report.epistemic_to_guessing_ratio,
                "rca_score": report.rca_score,
            },
            "environment": {
                "host": self.host,
                "port": self.port,
                "total_tool_calls": len(tool_env.call_history),
            },
            "timestamp": time.time(),
        }
        return harbor_result


def main() -> None:
    """Harbor CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Mercor Harbor Stirrup Benchmark Adapter")
    parser.add_argument("--host", type=str, default="localhost", help="Harbor benchmark host")
    parser.add_argument("--port", type=int, default=8000, help="Harbor benchmark port")
    parser.add_argument("--task_id", type=str, default=None, help="Harbor task identifier")
    parser.add_argument("--scenario", type=str, default="scenario_1_goroutine_deadlock", help="Chaos scenario ID")
    parser.add_argument("--mode", type=str, default="expert", choices=["expert", "naive"], help="Agent persona")
    parser.add_argument("--output", type=str, default=None, help="Path for Harbor judge output")

    args = parser.parse_args()

    adapter = StirrupHarborAdapter(host=args.host, port=args.port, task_id=args.task_id)
    agent = ExpertSRE() if args.mode == "expert" else NaiveJuniorAgent()

    result = adapter.execute_task(scenario_id=args.scenario, agent=agent)

    print(json.dumps(result, indent=2))

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        logger.info("Saved Harbor evaluation artifact to %s", args.output)


if __name__ == "__main__":
    main()
