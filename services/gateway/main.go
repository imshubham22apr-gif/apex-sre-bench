package main

import (
	"context"
	"log"
	"net/http"
	"net/http/pprof"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
)

var (
	httpRequestsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "http_requests_total",
			Help: "Total number of HTTP requests handled, partitioned by method, endpoint, and status.",
		},
		[]string{"method", "endpoint", "status"},
	)

	httpRequestDurationSeconds = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "http_request_duration_seconds",
			Help:    "HTTP request latency in seconds.",
			Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0},
		},
		[]string{"method", "endpoint"},
	)

	activeGoroutines = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "active_goroutines",
			Help: "Current count of active worker goroutines in the gateway service.",
		},
	)

	connectionPoolOpen = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "connection_pool_open",
			Help: "Current count of open connections in the simulated database pool.",
		},
	)

	lockContentionEventsTotal = prometheus.NewCounter(
		prometheus.CounterOpts{
			Name: "lock_contention_events_total",
			Help: "Total count of distributed lock timeouts and concurrency collisions.",
		},
	)
)

func init() {
	prometheus.MustRegister(httpRequestsTotal)
	prometheus.MustRegister(httpRequestDurationSeconds)
	prometheus.MustRegister(activeGoroutines)
	prometheus.MustRegister(connectionPoolOpen)
	prometheus.MustRegister(lockContentionEventsTotal)
}

// Server encapsulates gateway state, dependencies, and chaos hooks.
type Server struct {
	redisClient    *redis.Client
	chaosMu        sync.RWMutex
	activeChaos    string
	dbPool         chan struct{}
	dbPoolCapacity int
	deadlockHaltCh chan struct{}
}

func NewServer(redisAddr string, poolCapacity int) *Server {
	var rdb *redis.Client
	if redisAddr != "" {
		rdb = redis.NewClient(&redis.Options{
			Addr:         redisAddr,
			DialTimeout:  1 * time.Second,
			ReadTimeout:  1 * time.Second,
			WriteTimeout: 1 * time.Second,
			PoolSize:     50,
			MinIdleConns: 5,
		})
	}

	srv := &Server{
		redisClient:    rdb,
		activeChaos:    "none",
		dbPoolCapacity: poolCapacity,
		dbPool:         make(chan struct{}, poolCapacity),
		deadlockHaltCh: make(chan struct{}),
	}

	return srv
}

func (s *Server) SetChaos(scenario string) {
	s.chaosMu.Lock()
	defer s.chaosMu.Unlock()
	s.activeChaos = scenario
	log.Printf("[CHAOS] State transitioned to: %s", scenario)
}

func (s *Server) GetChaos() string {
	s.chaosMu.RLock()
	defer s.chaosMu.RUnlock()
	return s.activeChaos
}

func (s *Server) ResetChaos() {
	s.chaosMu.Lock()
	defer s.chaosMu.Unlock()
	s.activeChaos = "none"

	// Reset db pool handles
drainLoop:
	for {
		select {
		case <-s.dbPool:
		default:
			break drainLoop
		}
	}
	connectionPoolOpen.Set(0)
	log.Printf("[CHAOS] State reset to nominal operations")
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	metricsPort := os.Getenv("METRICS_PORT")
	if metricsPort == "" {
		metricsPort = "2112"
	}

	redisAddr := os.Getenv("REDIS_ADDR")
	if redisAddr == "" {
		redisAddr = "localhost:6379"
	}

	poolCapacity := 50
	if capEnv := os.Getenv("REDIS_POOL_SIZE"); capEnv != "" {
		if val, err := strconv.Atoi(capEnv); err == nil && val > 0 {
			poolCapacity = val
		}
	}

	server := NewServer(redisAddr, poolCapacity)

	// Background ticker for system telemetry gauges
	go func() {
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		for range ticker.C {
			activeGoroutines.Set(float64(runtime.NumGoroutine()))
			connectionPoolOpen.Set(float64(len(server.dbPool)))
		}
	}()

	// Application Router
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", server.handleHealthz)
	mux.HandleFunc("/api/v1/checkout", server.handleCheckout)
	mux.HandleFunc("/api/v1/payment", server.handlePayment)

	// Chaos injection hooks
	mux.HandleFunc("/chaos/inject", server.handleChaosInject)
	mux.HandleFunc("/chaos/reset", server.handleChaosReset)
	mux.HandleFunc("/chaos/state", server.handleChaosState)

	// Pprof debug profiles
	mux.HandleFunc("/debug/pprof/", pprof.Index)
	mux.HandleFunc("/debug/pprof/cmdline", pprof.Cmdline)
	mux.HandleFunc("/debug/pprof/profile", pprof.Profile)
	mux.HandleFunc("/debug/pprof/symbol", pprof.Symbol)
	mux.HandleFunc("/debug/pprof/trace", pprof.Trace)

	appServer := &http.Server{
		Addr:         ":" + port,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 15 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// Metrics Router
	metricsMux := http.NewServeMux()
	metricsMux.Handle("/metrics", promhttp.Handler())
	metricsServer := &http.Server{
		Addr:         ":" + metricsPort,
		Handler:      metricsMux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("[METRICS] Prometheus scrape endpoint active on :%s/metrics", metricsPort)
		if err := metricsServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("[METRICS] Scrape server error: %v", err)
		}
	}()

	// Signal handling for graceful shutdown and SIGHUP hot-reload
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)

	go func() {
		for sig := range sigChan {
			switch sig {
			case syscall.SIGHUP:
				log.Printf("[HOT-RELOAD] SIGHUP signal received. Re-reading runtime configurations.")
				server.ResetChaos()
			case syscall.SIGINT, syscall.SIGTERM:
				log.Printf("[SHUTDOWN] Terminating server gracefully...")
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				_ = appServer.Shutdown(ctx)
				_ = metricsServer.Shutdown(ctx)
				return
			}
		}
	}()

	log.Printf("[GATEWAY] Production HTTP server listening on :%s", port)
	if err := appServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("[GATEWAY] HTTP server error: %v", err)
	}
}
