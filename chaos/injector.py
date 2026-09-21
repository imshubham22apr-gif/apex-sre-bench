"""
Chaos Injector Orchestrator for APEX-SRE-Bench.

Controls scenario lifecycle, drives live microservice injection via HTTP hooks,
and coordinates with traffic generators to enforce reproducible telemetry degradation.
"""

from dataclasses import dataclass
import json
import logging
import time
from typing import Any, Dict, Optional
import urllib.request
import urllib.error

from chaos.scenarios import ChaosScenario, get_scenario_by_id
from traffic.load_generator import LoadGenerator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ChaosInjector")


@dataclass
class ChaosInjectionRecord:
    """Record of an active or past chaos injection event."""
    scenario_id: str
    injected_at: float
    target_service: str
    active: bool
    details: Dict[str, Any]


class ChaosInjector:
    """
    Lifecycle controller for distributed chaos scenarios.
    Coordinates live HTTP injection hooks and traffic generator profiles.
    """

    _gateway_checked: bool = False
    _gateway_available: bool = False

    def __init__(
        self,
        gateway_base_url: str = "http://localhost:8080",
        traffic_generator: Optional[LoadGenerator] = None,
    ) -> None:
        self.gateway_base_url = gateway_base_url.rstrip("/")
        self.traffic_generator = traffic_generator
        self._active_record: Optional[ChaosInjectionRecord] = None

    @classmethod
    def _is_gateway_available(cls, url: str) -> bool:
        if not cls._gateway_checked:
            cls._gateway_checked = True
            try:
                req = urllib.request.Request(f"{url}/healthz")
                with urllib.request.urlopen(req, timeout=0.3) as resp:
                    cls._gateway_available = (resp.status == 200)
            except Exception:
                cls._gateway_available = False
        return cls._gateway_available

    @property
    def current_scenario_id(self) -> Optional[str]:
        """Returns the ID of currently active scenario, if any."""
        if self._active_record and self._active_record.active:
            return self._active_record.scenario_id
        return None

    def inject_scenario(self, scenario_id: str) -> bool:
        """
        Injects the specified chaos scenario into the cluster.
        Returns True if injection succeeded or was successfully simulated.
        """
        scenario: Optional[ChaosScenario] = get_scenario_by_id(scenario_id)
        if not scenario:
            raise ValueError(f"Unknown scenario ID: {scenario_id}")

        logger.info("Injecting chaos scenario: %s (%s)", scenario.scenario_id, scenario.title)

        live_injection_success = self._send_http_inject(scenario.scenario_id)

        # Configure continuous traffic generator profile to match scenario failure profile
        if self.traffic_generator is not None:
            self.traffic_generator.set_simulation_profile(
                error_rate=scenario.expected_error_rate,
                p99_latency=scenario.expected_p99_latency,
                enabled=not live_injection_success,
            )

        self._active_record = ChaosInjectionRecord(
            scenario_id=scenario.scenario_id,
            injected_at=time.time(),
            target_service=scenario.target_service,
            active=True,
            details={
                "live_target_reached": live_injection_success,
                "expected_p99_latency": scenario.expected_p99_latency,
                "expected_error_rate": scenario.expected_error_rate,
            },
        )
        return True

    def reset(self) -> bool:
        """
        Clears all active chaos scenarios, restoring nominal cluster operations.
        """
        logger.info("Resetting all chaos scenarios...")
        live_reset_success = self._send_http_reset()

        if self.traffic_generator is not None:
            self.traffic_generator.set_simulation_profile(
                error_rate=0.0,
                p99_latency=0.012,
                enabled=False,
            )

        if self._active_record is not None:
            self._active_record.active = False

        return live_reset_success

    def query_service_state(self) -> Dict[str, Any]:
        """Queries the live gateway `/chaos/state` endpoint or returns active record status."""
        if not self._is_gateway_available(self.gateway_base_url):
            return {
                "active_chaos": self.current_scenario_id or "none",
                "simulated": True,
            }

        url = f"{self.gateway_base_url}/chaos/state"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return dict(data)
        except Exception as ex:
            logger.debug("Live gateway unreachable (%s), using local injection state", ex)
            return {
                "active_chaos": self.current_scenario_id or "none",
                "simulated": True,
            }

    def _send_http_inject(self, scenario_id: str) -> bool:
        """Sends POST /chaos/inject to live gateway."""
        if not self._is_gateway_available(self.gateway_base_url):
            return False

        url = f"{self.gateway_base_url}/chaos/inject"
        payload = json.dumps({"scenario": scenario_id}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return bool(resp.status == 200)
        except Exception as ex:
            logger.debug("HTTP inject hook failed: %s", ex)
            return False

    def _send_http_reset(self) -> bool:
        """Sends POST /chaos/reset to live gateway."""
        if not self._is_gateway_available(self.gateway_base_url):
            return False

        url = f"{self.gateway_base_url}/chaos/reset"
        req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return bool(resp.status == 200)
        except Exception as ex:
            logger.debug("HTTP reset hook failed: %s", ex)
            return False
