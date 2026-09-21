package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

var (
	requestCounter uint64
	splitBrainMu   sync.Mutex
	activeWorkers  int32
)

type ChaosRequest struct {
	Scenario string `json:"scenario"`
}

type ChaosStateResponse struct {
	ActiveChaos        string `json:"active_chaos"`
	ActiveGoroutines   int    `json:"active_goroutines"`
	ConnectionPoolOpen int    `json:"connection_pool_open"`
	TotalRequests      uint64 `json:"total_requests"`
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	scenario := s.GetChaos()

	// In severe deadlock, health checks can experience latency or failure
	if scenario == "scenario_1_goroutine_deadlock" && atomic.LoadUint64(&requestCounter)%5 == 0 {
		time.Sleep(300 * time.Millisecond)
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"status":"UP","timestamp":"` + time.Now().UTC().Format(time.RFC3339) + `"}`))

	duration := time.Since(start).Seconds()
	httpRequestDurationSeconds.WithLabelValues(r.Method, "/healthz").Observe(duration)
	httpRequestsTotal.WithLabelValues(r.Method, "/healthz", "200").Inc()
}

func (s *Server) handleCheckout(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	atomic.AddUint64(&requestCounter, 1)
	scenario := s.GetChaos()

	var statusCode int
	var responseBody string

	switch scenario {
	case "scenario_1_goroutine_deadlock":
		statusCode, responseBody = s.executeDeadlockScenario(r.Context())

	case "scenario_2_cascading_retry_storm":
		statusCode, responseBody = s.executeRetryStormScenario(r.Context())

	case "scenario_3_connection_pool_exhaustion":
		statusCode, responseBody = s.executeConnectionPoolExhaustionScenario(r.Context())

	case "scenario_4_ebpf_socket_packet_drop":
		statusCode, responseBody = s.executePacketDropScenario(r.Context())

	case "scenario_5_redis_lock_split_brain":
		statusCode, responseBody = s.executeSplitBrainScenario(r.Context())

	default:
		// Nominal healthy execution
		statusCode, responseBody = s.executeNominalCheckout(r.Context())
	}

	duration := time.Since(start).Seconds()
	statusStr := strconv.Itoa(statusCode)
	httpRequestsTotal.WithLabelValues(r.Method, "/api/v1/checkout", statusStr).Inc()
	httpRequestDurationSeconds.WithLabelValues(r.Method, "/api/v1/checkout").Observe(duration)

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_, _ = w.Write([]byte(responseBody))
}

func (s *Server) executeNominalCheckout(ctx context.Context) (int, string) {
	// Nominal path: acquire Redis lock, brief synthetic processing (<15ms)
	lockKey := fmt.Sprintf("lock:checkout:session-%d", rand.Intn(1000))
	if s.redisClient != nil {
		lockCtx, cancel := context.WithTimeout(ctx, 100*time.Millisecond)
		defer cancel()
		_ = s.redisClient.Set(lockCtx, lockKey, "locked", 2*time.Second).Err()
		defer func() {
			delCtx, delCancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
			defer delCancel()
			_ = s.redisClient.Del(delCtx, lockKey).Err()
		}()
	}

	time.Sleep(time.Duration(5+rand.Intn(10)) * time.Millisecond)
	return http.StatusOK, `{"status":"COMPLETED","order_id":"ord-` + fmt.Sprintf("%06d", rand.Intn(999999)) + `"}`
}

func (s *Server) executeDeadlockScenario(ctx context.Context) (int, string) {
	// Scenario 1: Spawn unbuffered channels and circular RWMutex acquisition
	// creating thousands of blocked goroutines and heavy latency
	blocker := make(chan struct{})
	doneCh := make(chan struct{})

	for i := 0; i < 25; i++ {
		go func() {
			// Worker waits indefinitely on blocker channel until context times out
			select {
			case <-blocker:
			case <-ctx.Done():
			case <-time.After(3500 * time.Millisecond):
			}
		}()
	}

	go func() {
		// Circular wait simulation
		time.Sleep(3100 * time.Millisecond)
		close(doneCh)
	}()

	select {
	case <-doneCh:
		close(blocker)
		return http.StatusOK, `{"status":"COMPLETED_SLOW","warning":"deadlock_contention"}`
	case <-time.After(3200 * time.Millisecond):
		close(blocker)
		return http.StatusGatewayTimeout, `{"error":"goroutine_deadlock_timeout"}`
	}
}

func (s *Server) executeRetryStormScenario(ctx context.Context) (int, string) {
	// Scenario 2: Downstream injects latency, upstream triggers 5 aggressive un-jittered retries
	const maxRetries = 5
	var success bool

	for attempt := 1; attempt <= maxRetries; attempt++ {
		httpRequestsTotal.WithLabelValues("POST", "/internal/payment-mock", "retry").Inc()
		// 5% transient latency / failure condition amplified by retries
		if rand.Float64() < 0.45 {
			time.Sleep(60 * time.Millisecond) // Un-jittered retry delay
			continue
		}
		success = true
		break
	}

	if !success {
		return http.StatusInternalServerError, `{"error":"cascading_retry_exhaustion","attempts":5}`
	}
	return http.StatusOK, `{"status":"COMPLETED_RETRIED"}`
}

func (s *Server) executeConnectionPoolExhaustionScenario(ctx context.Context) (int, string) {
	// Scenario 3: Error handling branch bypasses conn release, pool exhausts completely
	select {
	case s.dbPool <- struct{}{}:
		// Successfully acquired handle, but conditionally leak without defer release
		if rand.Float64() < 0.85 {
			// LEAK: Do not release back to s.dbPool
			time.Sleep(10 * time.Millisecond)
			return http.StatusOK, `{"status":"COMPLETED","warning":"handle_retained"}`
		}
		// Normal release
		<-s.dbPool
		return http.StatusOK, `{"status":"COMPLETED"}`
	case <-time.After(5000 * time.Millisecond):
		// Pool exhausted! Timeout and return 504
		return http.StatusGatewayTimeout, `{"error":"connection_pool_exhausted","open_connections":50}`
	}
}

func (s *Server) executePacketDropScenario(ctx context.Context) (int, string) {
	// Scenario 4: Injects 25% packet drop simulation with severe TCP retransmission latency
	if rand.Float64() < 0.25 {
		// Simulates packet drop and TCP retransmission backoff delay
		time.Sleep(1200 * time.Millisecond)
		return http.StatusServiceUnavailable, `{"error":"socket_packet_dropped","tcp_retransmit":true}`
	}
	time.Sleep(45 * time.Millisecond)
	return http.StatusOK, `{"status":"COMPLETED_AFTER_RETRANSMIT"}`
}

func (s *Server) executeSplitBrainScenario(ctx context.Context) (int, string) {
	// Scenario 5: Redis lock TTL is 500ms, but execution takes 800ms
	// Lock expires prematurely, second worker acquires lock, state inconsistency occurs
	splitBrainMu.Lock()
	workers := atomic.AddInt32(&activeWorkers, 1)
	if workers > 1 {
		lockContentionEventsTotal.Inc()
	}
	splitBrainMu.Unlock()

	defer atomic.AddInt32(&activeWorkers, -1)

	// Hold processing for 800ms exceeding 500ms TTL
	time.Sleep(800 * time.Millisecond)

	if workers > 1 {
		return http.StatusConflict, `{"error":"redis_lock_split_brain_detected","active_workers":` + strconv.Itoa(int(workers)) + `}`
	}
	return http.StatusOK, `{"status":"COMPLETED_LOCK_EXPIRED"}`
}

func (s *Server) handlePayment(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	atomic.AddUint64(&requestCounter, 1)

	time.Sleep(time.Duration(10+rand.Intn(15)) * time.Millisecond)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"status":"PAYMENT_PROCESSED","tx_id":"tx-` + fmt.Sprintf("%08d", rand.Intn(99999999)) + `"}`))

	duration := time.Since(start).Seconds()
	httpRequestDurationSeconds.WithLabelValues(r.Method, "/api/v1/payment").Observe(duration)
	httpRequestsTotal.WithLabelValues(r.Method, "/api/v1/payment", "200").Inc()
}

func (s *Server) handleChaosInject(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req ChaosRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, fmt.Sprintf("Invalid JSON: %v", err), http.StatusBadRequest)
		return
	}

	s.SetChaos(req.Scenario)

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]interface{}{
		"status":   "injected",
		"scenario": req.Scenario,
	})
}

func (s *Server) handleChaosReset(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	s.ResetChaos()

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]string{
		"status":  "reset",
		"message": "Chaos state reset to nominal",
	})
}

func (s *Server) handleChaosState(w http.ResponseWriter, r *http.Request) {
	resp := ChaosStateResponse{
		ActiveChaos:        s.GetChaos(),
		ActiveGoroutines:   int(atomic.LoadUint64(&requestCounter)),
		ConnectionPoolOpen: len(s.dbPool),
		TotalRequests:      atomic.LoadUint64(&requestCounter),
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(resp)
}
