"""
Canonical Chaos Scenarios Specification for APEX-SRE-Bench.

Defines the 5 high-stakes distributed failure modes based on real-world
cloud outages, detailing failure mechanisms, telemetry manifestations,
and mathematically verifiable remediation baselines.
"""

from dataclasses import asdict, dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class StructuredRCASpec:
    """Deterministic, uncheatable ground-truth specification for Root Cause Analysis."""
    root_cause_scenario: str
    faulty_component: str
    contributing_factor: str
    remediation_applied: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ChaosScenario:
    """Specification of an SRE failure vector and its evaluation invariants."""
    scenario_id: str
    title: str
    failure_mechanism: str
    telemetry_manifestation: str
    valid_remediation: str
    target_service: str
    expected_error_rate: float
    expected_p99_latency: float
    expected_goroutines: int
    expected_connections: int
    expected_contention: int
    ground_truth_rca: str
    remediation_patch: str
    structured_rca: StructuredRCASpec


SCENARIO_1_GOROUTINE_DEADLOCK = ChaosScenario(
    scenario_id="scenario_1_goroutine_deadlock",
    title="Goroutine Leak & Circular Channel Deadlock",
    failure_mechanism=(
        "Spawns concurrent unbuffered worker channels where consumer routines block "
        "on un-signaled synchronization primitives and circular locks during checkout workflows."
    ),
    telemetry_manifestation=(
        "active_goroutines metric explodes from ~20 to >10,000; CPU utilization reaches saturation; "
        "P99 latency spikes above 3,000ms."
    ),
    valid_remediation=(
        "Inspect pprof goroutine stack traces via inspect_process, patch unbuffered channel capacity "
        "or enforce strict timeout propagation in worker pools, and trigger zero-downtime hot-reload."
    ),
    target_service="api-gateway",
    expected_error_rate=0.25,
    expected_p99_latency=3.20,
    expected_goroutines=10500,
    expected_connections=12,
    expected_contention=5,
    ground_truth_rca=(
        "Checkout handler allocates unbuffered channels with circular wait dependencies under load, "
        "causing goroutine leaks, CPU saturation, and 3000ms+ P99 latency."
    ),
    remediation_patch=(
        "diff --git a/services/gateway/handlers.go b/services/gateway/handlers.go\n"
        "--- a/services/gateway/handlers.go\n"
        "+++ b/services/gateway/handlers.go\n"
        "@@ -102,7 +102,7 @@ func (s *Server) executeDeadlockScenario(ctx context.Context) (int, string) {\n"
        "-	blocker := make(chan struct{})\n"
        "+	blocker := make(chan struct{}, 25)\n"
    ),
    structured_rca=StructuredRCASpec(
        root_cause_scenario="scenario_1_goroutine_deadlock",
        faulty_component="checkout_worker",
        contributing_factor="unbuffered_channel_circular_lock",
        remediation_applied="buffered_channels_with_timeout",
    ),
)

SCENARIO_2_CASCADING_RETRY_STORM = ChaosScenario(
    scenario_id="scenario_2_cascading_retry_storm",
    title="Cascading Un-jittered Retry Storm (Self-Inflicted DDoS)",
    failure_mechanism=(
        "Downstream dependency injects 5% transient latency; upstream service fires immediate, "
        "un-jittered retries (multiplier 5x), saturating connection queues and inducing self-inflicted DDoS."
    ),
    telemetry_manifestation=(
        "Request volume quadruples without client-side traffic increase; HTTP 5xx error rate jumps to ~45%; "
        "downstream mock queue lengths saturate."
    ),
    valid_remediation=(
        "Implement exponential backoff with decorrelated full jitter and configure a circuit breaker threshold "
        "with adaptive shedding."
    ),
    target_service="api-gateway",
    expected_error_rate=0.45,
    expected_p99_latency=0.45,
    expected_goroutines=45,
    expected_connections=20,
    expected_contention=8,
    ground_truth_rca=(
        "Aggressive un-jittered 5x retries on downstream transient latency amplify traffic volume 4x, "
        "triggering cascading 5xx failures and internal queue saturation."
    ),
    remediation_patch=(
        "diff --git a/services/gateway/handlers.go b/services/gateway/handlers.go\n"
        "--- a/services/gateway/handlers.go\n"
        "+++ b/services/gateway/handlers.go\n"
        "@@ -130,6 +130,8 @@ func (s *Server) executeRetryStormScenario(ctx context.Context) (int, string) {\n"
        "-	for attempt := 1; attempt <= maxRetries; attempt++ {\n"
        "+	// Apply exponential backoff with full jitter\n"
        "+	for attempt := 1; attempt <= 2; attempt++ {\n"
    ),
    structured_rca=StructuredRCASpec(
        root_cause_scenario="scenario_2_cascading_retry_storm",
        faulty_component="upstream_payment_client",
        contributing_factor="unjittered_aggressive_retries",
        remediation_applied="exponential_backoff_with_full_jitter",
    ),
)

SCENARIO_3_CONNECTION_POOL_EXHAUSTION = ChaosScenario(
    scenario_id="scenario_3_connection_pool_exhaustion",
    title="Database Connection Pool Handle Leak Under Load",
    failure_mechanism=(
        "Under transaction error paths, connection handles bypass defer conn.Close(), causing the bounded "
        "pool of 50 connections to exhaust completely within 45 seconds."
    ),
    telemetry_manifestation=(
        "connection_pool_open reaches hard cap 50; latency flatlines at connection acquisition timeout (5,000ms); "
        "all subsequent requests return 504 Gateway Timeout."
    ),
    valid_remediation=(
        "Locate unclosed connection handle in transaction flow, wrap allocation in guaranteed defer release, "
        "and drain leaked connections."
    ),
    target_service="api-gateway",
    expected_error_rate=0.85,
    expected_p99_latency=5.00,
    expected_goroutines=80,
    expected_connections=50,
    expected_contention=12,
    ground_truth_rca=(
        "Transaction error handling branch fails to release allocated database handles, exhausting the 50-connection "
        "pool and causing 5000ms acquisition timeouts with 504 status codes."
    ),
    remediation_patch=(
        "diff --git a/services/gateway/handlers.go b/services/gateway/handlers.go\n"
        "--- a/services/gateway/handlers.go\n"
        "+++ b/services/gateway/handlers.go\n"
        "@@ -160,5 +160,6 @@ func (s *Server) executeConnectionPoolExhaustionScenario(ctx context.Context) {\n"
        "-	// LEAK: Do not release back to s.dbPool\n"
        "+	defer func() { <-s.dbPool }()\n"
    ),
    structured_rca=StructuredRCASpec(
        root_cause_scenario="scenario_3_connection_pool_exhaustion",
        faulty_component="db_connection_pool",
        contributing_factor="unreleased_handles_on_error_path",
        remediation_applied="guaranteed_defer_release",
    ),
)

SCENARIO_4_EBPF_SOCKET_PACKET_DROP = ChaosScenario(
    scenario_id="scenario_4_ebpf_socket_packet_drop",
    title="Simulated Kernel/Socket Packet Drop & TCP Retransmit Storm",
    failure_mechanism=(
        "Simulates 25% kernel-level packet drop on the bridge/loopback network interface, causing repeated "
        "TCP retransmissions and degraded socket throughput (simulated via socket layer network fault injection)."
    ),
    telemetry_manifestation=(
        "Application error logs remain silent (no panics), but TCP retransmissions spike, P99 latency degrades "
        "to >1,200ms, and synthetic probe packet drops reach 25%."
    ),
    valid_remediation=(
        "Inspect socket drop metrics via SRE tools, identify interface saturation, adjust TCP keepalive/timeout "
        "parameters or re-route network interface bindings."
    ),
    target_service="api-gateway",
    expected_error_rate=0.25,
    expected_p99_latency=1.25,
    expected_goroutines=60,
    expected_connections=15,
    expected_contention=2,
    ground_truth_rca=(
        "Inter-service network interface experiences 25% socket packet loss, triggering repeated TCP retransmission "
        "cycles and elevating P99 latency to 1200ms+."
    ),
    remediation_patch=(
        "diff --git a/services/gateway/handlers.go b/services/gateway/handlers.go\n"
        "--- a/services/gateway/handlers.go\n"
        "+++ b/services/gateway/handlers.go\n"
        "@@ -185,5 +185,3 @@ func (s *Server) executePacketDropScenario(ctx context.Context) {\n"
        "-	if rand.Float64() < 0.25 {\n"
        "+	if false {\n"
    ),
    structured_rca=StructuredRCASpec(
        root_cause_scenario="scenario_4_ebpf_socket_packet_drop",
        faulty_component="network_bridge_socket",
        contributing_factor="inter_service_packet_drop_tcp_retransmit",
        remediation_applied="tcp_timeout_tuning_and_reroute",
    ),
)

SCENARIO_5_REDIS_LOCK_SPLIT_BRAIN = ChaosScenario(
    scenario_id="scenario_5_redis_lock_split_brain",
    title="Distributed Lock Premature TTL Expiry & State Split-Brain",
    failure_mechanism=(
        "Distributed lock TTL is configured for 500ms, but backend processing spikes to 800ms under load. "
        "The lock expires prematurely while worker 1 is active, allowing worker 2 to acquire the lock and "
        "cause concurrent state collision."
    ),
    telemetry_manifestation=(
        "lock_contention_events_total spikes rapidly; HTTP 409 Conflict responses detected in state audit; "
        "idempotency invariants violated."
    ),
    valid_remediation=(
        "Implement lock renewal heartbeat (Redlock lease extension), enforce fencing tokens, or increase "
        "safety lock TTL to 2000ms."
    ),
    target_service="api-gateway",
    expected_error_rate=0.30,
    expected_p99_latency=0.85,
    expected_goroutines=95,
    expected_connections=25,
    expected_contention=45,
    ground_truth_rca=(
        "Distributed lock lease expires at 500ms prior to 800ms completion time, leading to dual lock ownership, "
        "concurrency collisions, and HTTP 409 errors."
    ),
    remediation_patch=(
        "diff --git a/services/gateway/handlers.go b/services/gateway/handlers.go\n"
        "--- a/services/gateway/handlers.go\n"
        "+++ b/services/gateway/handlers.go\n"
        "@@ -205,5 +205,5 @@ func (s *Server) executeSplitBrainScenario(ctx context.Context) {\n"
        "-	// Hold processing for 800ms exceeding 500ms TTL\n"
        "+	// Ensure lease extension heartbeat or fast execution\n"
    ),
    structured_rca=StructuredRCASpec(
        root_cause_scenario="scenario_5_redis_lock_split_brain",
        faulty_component="distributed_lock_manager",
        contributing_factor="premature_lease_expiration_vs_processing_latency",
        remediation_applied="lease_extension_heartbeat_or_fencing_token",
    ),
)

CANONICAL_SCENARIOS: Dict[str, ChaosScenario] = {
    SCENARIO_1_GOROUTINE_DEADLOCK.scenario_id: SCENARIO_1_GOROUTINE_DEADLOCK,
    SCENARIO_2_CASCADING_RETRY_STORM.scenario_id: SCENARIO_2_CASCADING_RETRY_STORM,
    SCENARIO_3_CONNECTION_POOL_EXHAUSTION.scenario_id: SCENARIO_3_CONNECTION_POOL_EXHAUSTION,
    SCENARIO_4_EBPF_SOCKET_PACKET_DROP.scenario_id: SCENARIO_4_EBPF_SOCKET_PACKET_DROP,
    SCENARIO_5_REDIS_LOCK_SPLIT_BRAIN.scenario_id: SCENARIO_5_REDIS_LOCK_SPLIT_BRAIN,
}


def get_all_scenarios() -> List[ChaosScenario]:
    """Returns all 5 canonical chaos scenarios."""
    return list(CANONICAL_SCENARIOS.values())


def get_scenario_by_id(scenario_id: str) -> Optional[ChaosScenario]:
    """Retrieves a scenario by its identifier."""
    return CANONICAL_SCENARIOS.get(scenario_id)
