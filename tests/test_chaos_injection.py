"""
Test Suite: Chaos Scenario Injection & Telemetry Degradation Validation.
"""

import time
import pytest

from chaos.injector import ChaosInjector
from chaos.scenarios import (
    CANONICAL_SCENARIOS,
    get_all_scenarios,
    get_scenario_by_id,
)
from traffic.load_generator import LoadGenerator, TrafficSample


class TestChaosInjection:
    """Verifies that all 5 canonical chaos scenarios trigger expected telemetry degradation."""

    def test_all_five_canonical_scenarios_registered(self) -> None:
        scenarios = get_all_scenarios()
        assert len(scenarios) == 5
        expected_ids = {
            "scenario_1_goroutine_deadlock",
            "scenario_2_cascading_retry_storm",
            "scenario_3_connection_pool_exhaustion",
            "scenario_4_ebpf_socket_packet_drop",
            "scenario_5_redis_lock_split_brain",
        }
        actual_ids = {s.scenario_id for s in scenarios}
        assert actual_ids == expected_ids

    @pytest.mark.parametrize("scenario_id", [
        "scenario_1_goroutine_deadlock",
        "scenario_2_cascading_retry_storm",
        "scenario_3_connection_pool_exhaustion",
        "scenario_4_ebpf_socket_packet_drop",
        "scenario_5_redis_lock_split_brain",
    ])
    def test_scenario_injection_telemetry_impact(self, scenario_id: str) -> None:
        scenario = get_scenario_by_id(scenario_id)
        assert scenario is not None

        # Setup load generator with simulation enabled
        traffic_gen = LoadGenerator(target_rps=50, enable_simulation_fallback=True)
        injector = ChaosInjector(traffic_generator=traffic_gen)

        # Pre-injection state: nominal error rate and latency
        assert traffic_gen.current_error_rate() == 0.0
        assert traffic_gen.current_p99_latency() <= 0.05

        # Inject chaos scenario
        success = injector.inject_scenario(scenario_id)
        assert success is True
        assert injector.current_scenario_id == scenario_id

        # Verify traffic generator profile has degraded to scenario specifications
        degraded_error_rate = traffic_gen.current_error_rate()
        degraded_p99_latency = traffic_gen.current_p99_latency()

        assert degraded_error_rate == pytest.approx(scenario.expected_error_rate, rel=0.05)
        assert degraded_p99_latency == pytest.approx(scenario.expected_p99_latency, rel=0.05)

        # Reset chaos
        injector.reset()
        assert injector.current_scenario_id is None
        assert traffic_gen.current_error_rate() == 0.0
        assert traffic_gen.current_p99_latency() <= 0.05

    def test_invalid_scenario_raises_error(self) -> None:
        injector = ChaosInjector()
        with pytest.raises(ValueError, match="Unknown scenario ID"):
            injector.inject_scenario("non_existent_failure_mode")

    def test_load_generator_ring_buffer_and_percentiles(self) -> None:
        gen = LoadGenerator(max_samples=1000)
        now = time.time()

        # Insert 100 samples with varying latencies
        for i in range(1, 101):
            gen.record_sample(TrafficSample(
                timestamp=now,
                status_code=200 if i <= 95 else 500,
                latency=float(i) * 0.005,  # 0.005s to 0.500s
                endpoint="/api/v1/checkout",
            ))

        assert gen.total_requests() == 100
        # 5 errors out of 100 => 0.05
        assert gen.current_error_rate(window_seconds=10.0) == pytest.approx(0.05, abs=0.001)
        # P99 should be near 99th percentile (approx 0.495s)
        p99 = gen.current_p99_latency(window_seconds=10.0)
        assert p99 >= 0.45 and p99 <= 0.51
