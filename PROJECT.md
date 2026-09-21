# PROJECT.md: Mercor Intelligence Agent Ecosystem & Archipelago Architecture

This document formalizes the architecture of Mercor Intelligence's agent ecosystem (`archipelago`, `harbor`, `ApexAgents-SkyRL-Recipe`) and establishes the operational guidelines for autonomous agents (such as Google Antigravity) developing and evaluating within this environment.

---

## 1. Pure Architecture Breakdown

Mercor’s infrastructure focuses on autonomous knowledge-work agents and reinforcement learning (RL) benchmarks, structured across three core pillars:

```
                  ┌──────────────────────────────────────────────┐
                  │          ApexAgents-SkyRL-Recipe             │
                  │    (RL Training via SkyRL + Harbor Harness)  │
                  └──────────────────────┬───────────────────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 ▼                                               ▼
     ┌───────────────────────┐                       ┌───────────────────────┐
     │      Archipelago      │                       │     Harbor / BUA      │
     │  (Execution Harness)  │                       │  (Benchmark Runner &  │
     │  - Agents (Loop/ReAct)│                       │      LLM Judges)      │
     │  - MCP Enterprise Hub │                       └───────────────────────┘
     │  - Grading & Verifiers│
     └───────────────────────┘
```

### Pillar A: `archipelago` (Harness & Execution Engine)
The central runtime repository containing agent execution loops, enterprise tools, environment sandboxes, and grading logic:

1. **`agents/` Subsystem**:
   - `loop_agent.py`: Baseline iterative agent loop with memory management.
   - `loop_truncated_tools_agent.py`: Sliding-window context manager that truncates large tool outputs (e.g., massive log streams, SEC 10-K filings, stack traces) to prevent context window saturation and model hallucinations.
   - `react_toolbelt_agent.py`: Classical ReAct (Reason + Act) paradigm over MCP tool collections.
   - `stirrup_agent.py`: Standardized adapter interface connecting agents directly to the `harbor` benchmark runner.

2. **`mcp_servers/` (Model Context Protocol Suite)**:
   - Enterprise Office Suite: `mail`, `calendar`, `chat`, `documents`, `spreadsheets`, `presentations`, `pdfs`.
   - Financial & Corporate Telemetry: `edgar_sec` (SEC 10-K/10-Q filings lookup), `fmp` (Financial Modeling Prep API).
   - System & Infrastructure: `filesystem`, `code_execution`, and `sre_bench` (distributed systems telemetry).
   - *Protocol Standard*: Independent subprocesses communicating strictly via standard JSON-RPC 2.0 over `stdio`. Agents are decoupled from server internals and interact exclusively via MCP schemas.

3. **`environment/` Subsystem**:
   - `coordinator.py`: Routes MCP tool calls, manages agent lifecycle, and aggregates execution metrics.
   - `gateway.py` & `middleware.py`: Message routing, protocol translation, and error handling.
   - `rootfs.py` & `snapshot.py`: Ephemeral copy-on-write filesystem sandboxing ensuring isolated state per task episode.

4. **`grading/` Subsystem**:
   - `verifiers.py`: Deterministic ground-truth assertions (zero-LLM state verification, SLA metrics, blast radius safety).
   - `rubrics.py` & `grade.py`: Multi-modal scoring combining deterministic assertions with rubric-guided evaluators.

---

### Pillar B: `harbor` & `harbor-bua-judge` (Benchmark Framework)
- **Harbor**: Containerized benchmark runner framework (forked from the Harbor specification) providing standardized sandboxed environments.
- **Harbor CLI Contract**: Invoked with `--host`, `--port`, `--task_id`, and `--scenario`.
- **harbor-bua-judge**: Business User Agent (BUA) evaluator scoring complex enterprise deliverables (structured post-mortems, financial models, slides).

---

### Pillar C: `ApexAgents-SkyRL-Recipe` (RL Post-Training)
- Reinforcement learning recipe utilizing the Sky Computing / SkyRL framework to train models on APEX trajectory data with dense, verifiable reward functions ($R_{\text{episode}}$).

---

## 2. Antigravity Operational Guidelines & Guardrails

When executing tasks in the Mercor ecosystem, Antigravity must strictly adhere to the following four rules:

### Rule 1: Sandboxing & Rootfs Fallback (No Docker Deadlock)
* **Context**: `environment/rootfs.py` attempts system mounts or Docker daemon interactions. If running in environments without an active Docker daemon (e.g., local Windows hosts, unprivileged containers), **never hang or loop attempting to connect to the Docker socket**.
* **Action**: Fall back to in-memory mock sandboxes or user-space simulated fixtures while keeping the containerized configuration runnable for production deployments.

### Rule 2: MCP Stdio Subprocess Decoupling
* **Context**: MCP servers are independent JSON-RPC subprocesses.
* **Action**: Never import internal MCP server implementation classes directly into agents. Always route tool calls via JSON-RPC 2.0 over `stdio`. Ensure `PYTHONPATH` includes the repository root to avoid `ModuleNotFoundError: No module named 'mcp_servers'`.

### Rule 3: Sliding Context & Payload Truncation
* **Context**: High-scale observability data (pprof dumps, Loki logs, PromQL matrices) can exceed 100,000 tokens in seconds.
* **Action**: Implement output truncation matching `loop_truncated_tools_agent.py`. Truncate long strings to head/tail slices with explicit omitted line markers.

### Rule 4: Zero-LLM Deterministic Verifiers
* **Context**: In Mercor APEX benchmarks, evaluation correctness must be mathematically reproducible.
* **Action**: Rely on `evaluator/verifier.py` and `evaluator/metrics.py` ($TTM$, $\mathcal{B}_{\text{safe}}$, $EGR$, $R_{\text{episode}}$) rather than subjective LLM judgments.

---

## 3. End-to-End Task Execution Flow

```text
[Harbor Runner] ──(--task_id, --host, --port)──► [Stirrup Adapter]
                                                          │
                                                          ▼
                                                  [Loop / ReAct Agent]
                                                          │
                                     (JSON-RPC over stdio)│
                                                          ▼
                                                 [Coordinator / Gateway]
                                                          │
                                                          ▼
                                                 [MCP SRE / Tool Server]
                                                          │
                                                          ▼
                                           [Deterministic State Oracle]
                                           (Evaluates TTM, B_safe, EGR)
```
