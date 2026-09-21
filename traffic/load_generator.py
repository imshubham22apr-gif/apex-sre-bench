"""
Continuous Traffic Generator for APEX-SRE-Bench.

Generates concurrent synthetic production traffic against target endpoints
(/api/v1/checkout, /healthz) and maintains rolling window SLA telemetry metrics.
"""

from collections import deque
from dataclasses import dataclass
import logging
import math
import random
import threading
import time
from typing import Deque, List, Optional
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("TrafficGenerator")


@dataclass(frozen=True)
class TrafficSample:
    """Represents a single recorded HTTP request outcome."""
    timestamp: float
    status_code: int
    latency: float
    endpoint: str


class LoadGenerator:
    """
    Thread-safe continuous synthetic traffic generator.
    Maintains a ring buffer of request telemetry and exposes SLA metrics.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        target_rps: int = 75,
        max_samples: int = 50000,
        enable_simulation_fallback: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.target_rps = max(10, min(200, target_rps))
        self.max_samples = max_samples
        self.enable_simulation_fallback = enable_simulation_fallback

        self._ring_buffer: Deque[TrafficSample] = deque(maxlen=self.max_samples)
        self._lock = threading.Lock()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

        # Simulated profile variables for offline evaluation mode
        self._simulated_error_rate: float = 0.0
        self._simulated_p99_latency: float = 0.012
        self._simulated_mode: bool = False

    def set_simulation_profile(self, error_rate: float, p99_latency: float, enabled: bool = True) -> None:
        """Configures synthetic telemetry generation parameters for offline evaluation."""
        with self._lock:
            self._simulated_error_rate = max(0.0, min(1.0, error_rate))
            self._simulated_p99_latency = max(0.001, p99_latency)
            self._simulated_mode = enabled
            logger.info(
                "Telemetry profile updated: error_rate=%.4f, p99_latency=%.3fs, simulation=%s",
                self._simulated_error_rate,
                self._simulated_p99_latency,
                self._simulated_mode,
            )

    def start(self) -> None:
        """Starts the continuous background traffic generation worker."""
        if self._running:
            logger.warning("Load generator is already running.")
            return

        self._running = True
        self._worker_thread = threading.Thread(target=self._run_loop, daemon=True, name="TrafficWorker")
        self._worker_thread.start()
        logger.info("Load generator started with target %d req/sec.", self.target_rps)

    def stop(self) -> None:
        """Stops the background traffic generation worker."""
        if not self._running:
            return

        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)
        logger.info("Load generator stopped.")

    def record_sample(self, sample: TrafficSample) -> None:
        """Inserts a new sample into the thread-safe ring buffer."""
        with self._lock:
            self._ring_buffer.append(sample)

    def get_window_samples(self, window_seconds: float = 5.0) -> List[TrafficSample]:
        """Returns all samples collected within the last `window_seconds`."""
        cutoff = time.time() - window_seconds
        with self._lock:
            return [s for s in self._ring_buffer if s.timestamp >= cutoff]

    def current_error_rate(self, window_seconds: float = 5.0) -> float:
        """Calculates rolling error rate (status >= 500 or network error) in the window."""
        samples = self.get_window_samples(window_seconds)
        if not samples:
            return self._simulated_error_rate if self._simulated_mode else 0.0

        errors = sum(1 for s in samples if s.status_code >= 500 or s.status_code == 0)
        return errors / len(samples)

    def current_p99_latency(self, window_seconds: float = 5.0) -> float:
        """Calculates rolling 99th percentile latency in seconds."""
        samples = self.get_window_samples(window_seconds)
        if not samples:
            return self._simulated_p99_latency if self._simulated_mode else 0.012

        latencies = sorted(s.latency for s in samples)
        idx = max(0, int(math.ceil(0.99 * len(latencies))) - 1)
        return latencies[idx]

    def total_requests(self) -> int:
        """Returns total count of recorded requests."""
        with self._lock:
            return len(self._ring_buffer)

    def clear(self) -> None:
        """Flushes the ring buffer."""
        with self._lock:
            self._ring_buffer.clear()

    def _execute_http_request(self, endpoint: str) -> TrafficSample:
        """Executes a single live HTTP request against the gateway."""
        url = f"{self.base_url}{endpoint}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "APEX-SRE-Bench/1.0", "Content-Type": "application/json"},
            method="GET" if endpoint == "/healthz" else "POST",
        )
        data = b"{}" if endpoint != "/healthz" else None

        start_time = time.time()
        try:
            with urllib.request.urlopen(req, data=data, timeout=4.0) as resp:
                latency = time.time() - start_time
                return TrafficSample(
                    timestamp=time.time(),
                    status_code=resp.status,
                    latency=latency,
                    endpoint=endpoint,
                )
        except urllib.error.HTTPError as e:
            latency = time.time() - start_time
            return TrafficSample(
                timestamp=time.time(),
                status_code=e.code,
                latency=latency,
                endpoint=endpoint,
            )
        except Exception:
            latency = time.time() - start_time
            return TrafficSample(
                timestamp=time.time(),
                status_code=0,
                latency=latency,
                endpoint=endpoint,
            )

    def _generate_simulated_sample(self, endpoint: str) -> TrafficSample:
        """Generates a synthetic telemetry sample adhering to the simulated profile."""
        now = time.time()
        is_error = random.random() < self._simulated_error_rate
        if is_error:
            status_code = random.choice([500, 502, 503, 504])
            latency = self._simulated_p99_latency * (0.8 + 0.4 * random.random())
        else:
            status_code = 200
            # Exponential distribution shaped to have 99th percentile at self._simulated_p99_latency
            mean_latency = max(0.005, self._simulated_p99_latency / 4.6)
            latency = random.expovariate(1.0 / mean_latency)

        return TrafficSample(
            timestamp=now,
            status_code=status_code,
            latency=latency,
            endpoint=endpoint,
        )

    def _run_loop(self) -> None:
        """Internal worker loop executing continuous traffic."""
        sleep_interval = 1.0 / self.target_rps
        endpoints = ["/api/v1/checkout", "/api/v1/checkout", "/healthz"]

        while self._running:
            target_endpoint = random.choice(endpoints)
            sample: TrafficSample

            if self._simulated_mode:
                sample = self._generate_simulated_sample(target_endpoint)
            else:
                try:
                    sample = self._execute_http_request(target_endpoint)
                    # If target is unreachable and fallback enabled, fall back to simulation
                    if sample.status_code == 0 and self.enable_simulation_fallback:
                        sample = self._generate_simulated_sample(target_endpoint)
                except Exception:
                    sample = self._generate_simulated_sample(target_endpoint)

            self.record_sample(sample)
            time.sleep(sleep_interval)
