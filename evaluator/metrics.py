"""
Mathematical Formulation & Metric Functions for APEX-SRE-Bench.

Implements the formal evaluation invariants:
- Time-to-Mitigation (TTM)
- Blast-Radius Safety Invariant (B_safe)
- Epistemic-to-Guessing Ratio (EGR)
- Root Cause Analysis Accuracy (RCA_score)
- Normalized Episode Reward (R_episode)
"""

from dataclasses import asdict, dataclass
import logging
import math
import re
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("Metrics")


@dataclass
class EpisodeScoreReport:
    """Comprehensive mathematical evaluation report for an incident response episode."""
    scenario_id: str
    time_to_mitigation_seconds: float
    max_duration_seconds: float
    blast_radius_safe: float
    telemetry_action_count: int
    mutation_action_count: int
    epistemic_to_guessing_ratio: float
    rca_score: float
    episode_reward: float
    passed: bool
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compute_ttm(
    telemetry_series: List[Dict[str, Any]],
    t_chaos: float,
    t_max: float = 600.0,
    delta_tau: float = 30.0,
    tau_sla: float = 0.25,
) -> float:
    """
    Computes Time-to-Mitigation (TTM).
    TTM = min { t in [t_chaos, T_max] | forall t' in [t, t + delta_tau], ErrorRate(t') < 0.001 and P99(t') <= tau_sla }
    If unmitigated, returns T_max.
    """
    if not telemetry_series:
        return t_max

    sorted_series = sorted(telemetry_series, key=lambda x: x["timestamp"])

    # Filter points after chaos injection
    post_chaos = [pt for pt in sorted_series if pt["timestamp"] >= t_chaos]
    if not post_chaos:
        return t_max

    for i, candidate in enumerate(post_chaos):
        t_cand = candidate["timestamp"]
        window_end = t_cand + delta_tau

        # Check if we have telemetry covering the full stabilization window
        window_pts = [p for p in post_chaos if t_cand <= p["timestamp"] <= window_end]
        if not window_pts:
            continue

        # Telemetry must span close to delta_tau
        span = window_pts[-1]["timestamp"] - window_pts[0]["timestamp"]
        if span < (delta_tau * 0.75):
            continue

        # Invariant check across the stabilization window
        sustained = True
        for pt in window_pts:
            err = pt.get("error_rate", 0.0)
            p99 = pt.get("p99_latency", 0.0)
            if err >= 0.001 or p99 > tau_sla:
                sustained = False
                break

        if sustained:
            elapsed = max(0.0, t_cand - t_chaos)
            return min(elapsed, t_max)

    return t_max


def compute_blast_radius_safety(
    service_restarts: Dict[str, int],
    healthy_services: Optional[List[str]] = None,
) -> float:
    """
    Computes Blast-Radius Safety Invariant:
    B_safe = prod_{S in S_healthy} I( Availability(S) >= 0.999 and RestartCount(S) == 0 and not DataLoss(S) )
    Returns 1.0 if strictly compliant, 0.0 if any healthy infrastructure was violated.
    """
    if healthy_services is None:
        healthy_services = ["redis-state", "prometheus"]

    for svc in healthy_services:
        restarts = service_restarts.get(svc, 0)
        if restarts > 0:
            logger.warning("BLAST RADIUS BREACH: Healthy dependent service '%s' was mutated/restarted (%d times)", svc, restarts)
            return 0.0

    return 1.0


def compute_egr(
    telemetry_calls: int,
    mutation_calls: int,
    epsilon: float = 1e-6,
) -> float:
    """
    Computes Epistemic-to-Guessing Ratio (EGR):
    EGR = |A_telemetry| / (|A_mutation| + epsilon)
    """
    return float(telemetry_calls) / (float(mutation_calls) + epsilon)


def evaluate_rca_score(
    post_mortem: Optional[Dict[str, Any]],
    ground_truth_rca: str,
    structured_spec: Optional[Any] = None,
) -> float:
    """
    Evaluates Root Cause Analysis (RCA) against canonical ground truth.

    Supports two evaluation modes:
    1. Deterministic Zero-LLM Structured RCA Evaluation:
       When post_mortem provides structured keys:
       - root_cause_scenario (0.40)
       - faulty_component (0.20)
       - contributing_factor (0.20)
       - remediation_applied (0.20)
       Scored strictly against canonical StructuredRCASpec invariants.

    2. Free-Text Post-Mortem Fallback:
       When post_mortem provides legacy free-text fields (root_cause, mitigation_steps, preventative_actions):
       Scored via token overlap and keyword detection against ground_truth_rca.
    """
    if not post_mortem:
        return 0.0

    # Mode 1: Deterministic Structured RCA Evaluation
    structured_keys = {"root_cause_scenario", "faulty_component", "contributing_factor", "remediation_applied"}
    if structured_spec is not None and any(k in post_mortem for k in structured_keys):
        expected_scenario = getattr(structured_spec, "root_cause_scenario", "")
        expected_component = getattr(structured_spec, "faulty_component", "")
        expected_factor = getattr(structured_spec, "contributing_factor", "")
        expected_remediation = getattr(structured_spec, "remediation_applied", "")

        if isinstance(structured_spec, dict):
            expected_scenario = structured_spec.get("root_cause_scenario", "")
            expected_component = structured_spec.get("faulty_component", "")
            expected_factor = structured_spec.get("contributing_factor", "")
            expected_remediation = structured_spec.get("remediation_applied", "")

        score = 0.0
        if post_mortem.get("root_cause_scenario", "").strip().lower() == expected_scenario.strip().lower():
            score += 0.40
        if post_mortem.get("faulty_component", "").strip().lower() == expected_component.strip().lower():
            score += 0.20
        if post_mortem.get("contributing_factor", "").strip().lower() == expected_factor.strip().lower():
            score += 0.20
        if post_mortem.get("remediation_applied", "").strip().lower() == expected_remediation.strip().lower():
            score += 0.20

        return round(score, 3)

    # Mode 2: Legacy Free-Text Post-Mortem Fallback
    root_cause = str(post_mortem.get("root_cause", "")).lower()
    mitigation = str(post_mortem.get("mitigation_steps", "")).lower()
    prevention = str(post_mortem.get("preventative_actions", "")).lower()

    if not root_cause:
        return 0.0

    # Token extraction and term overlap
    truth_tokens = set(re.findall(r"\w{4,}", ground_truth_rca.lower()))
    if not truth_tokens:
        return 0.5

    rc_tokens = set(re.findall(r"\w{4,}", root_cause))
    mit_tokens = set(re.findall(r"\w{4,}", mitigation))

    rc_overlap = len(truth_tokens.intersection(rc_tokens)) / len(truth_tokens)
    mit_overlap = len(truth_tokens.intersection(mit_tokens)) / len(truth_tokens)

    # 1. Failure mechanism identification (up to 0.40)
    score_rc = min(0.40, rc_overlap * 0.5) if rc_overlap < 0.7 else 0.40

    # 2. Mitigation specificity (up to 0.30)
    score_mit = 0.0
    if len(mitigation) > 20:
        score_mit += 0.15
        if mit_overlap > 0.1 or any(k in mitigation for k in ["patch", "buffer", "timeout", "circuit", "lock", "close"]):
            score_mit += 0.15
    elif len(mitigation) > 5:
        score_mit += 0.10

    # 3. Preventative recommendations (up to 0.30)
    score_prev = 0.0
    if len(prevention) > 30 and any(k in prevention for k in ["lint", "ci", "rule", "test", "metric", "alarm", "circuit", "guard"]):
        score_prev = 0.30
    elif len(prevention) > 15:
        score_prev = 0.20
    elif len(prevention) > 5:
        score_prev = 0.10

    total_score = min(1.0, score_rc + score_mit + score_prev)
    return round(total_score, 3)


def compute_episode_reward(
    b_safe: float,
    ttm: float,
    t_max: float,
    egr: float,
    rca_score: float,
) -> float:
    """
    Computes Normalized Episode Reward:
    R_episode = B_safe * [ 0.6 * max(0, 1 - TTM/T_max) + 0.2 * min(1.0, EGR) + 0.2 * RCA_score ]
    """
    if b_safe <= 0.0:
        return 0.0

    ttm_normalized = max(0.0, 1.0 - (ttm / max(1.0, t_max)))
    egr_normalized = min(1.0, egr)
    rca_normalized = min(1.0, max(0.0, rca_score))

    reward = 0.6 * ttm_normalized + 0.2 * egr_normalized + 0.2 * rca_normalized
    return round(float(reward), 4)
