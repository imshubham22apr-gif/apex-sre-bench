# GEMINI.md: Antigravity Context for Mercor Intelligence Ecosystem

## Role & Objective
You are working as an elite distributed systems architect and AI evaluation researcher within the **Mercor Intelligence agent ecosystem** (specifically `archipelago`, `harbor`, and `ApexAgents-SkyRL-Recipe`).
The system is an RL evaluation harness and benchmark that tests autonomous frontier models (GPT-6 Astra, Claude 3.5 Sonnet) against enterprise Model Context Protocol (MCP) environments and live distributed systems incidents.

---

## Architectural Directives

1. **Agents Subsystem (`/agents`)**:
   - Implements tool-augmented agents (`react_toolbelt_agent.py`, `loop_agent.py`, `loop_truncated_tools_agent.py`, `stirrup_agent.py`).
   - Maintain context window management: `loop_truncated_tools_agent` trims large outputs (such as goroutine stack traces, Loki logs, SEC filings) to prevent context window saturation.

2. **MCP Servers (`/tools`, `/mcp_servers`)**:
   - Each tool server is decoupled and communicates via standard JSON-RPC 2.0 over `stdio`.
   - Do NOT tightly couple agents to internal server module imports; agents interact strictly via standard MCP tool calls.
   - Financial tools (`edgar_sec`, `fmp`) and live cluster monitors require mock fixtures/fallback profiles when live network credentials are not configured.

3. **Execution Environment (`/environment`, `/chaos`)**:
   - Sandboxing must be resilient: if Docker daemon or rootfs mount permissions are restricted, always fall back gracefully to verified in-memory or simulated execution without blocking or hanging on socket timeouts.

4. **Grading & Verification Pipeline (`/evaluator`, `/grading`)**:
   - Use deterministic state verification (`evaluator/verifier.py`, `evaluator/metrics.py`).
   - Grade models strictly against mathematical invariants:
     - Time-to-Mitigation ($TTM$, $\Delta \tau = 30\text{s}$, $\tau_{\text{SLA}} = 250\text{ms}$, $T_{\max} = 600\text{s}$)
     - Blast-Radius Safety ($\mathcal{B}_{\text{safe}} \in \{0.0, 1.0\}$)
     - Epistemic-to-Guessing Ratio ($EGR$)
     - Normalized Episode Reward ($R_{\text{episode}}$)

---

## Critical Watchouts (Preventing Agent Looping)
- **Docker / Rootfs Isolation**: If Docker daemon is offline or on a named pipe without permissions, do NOT poll or sleep in loops. Use cached availability checks and offline simulation fixtures.
- **MCP Process Spawning**: When launching MCP servers in subprocesses, verify `PYTHONPATH` includes the repository root to prevent `ModuleNotFoundError`.
- **Harbor Adapter CLI**: The Harbor runner invokes `stirrup_agent` with `--host`, `--port`, `--task_id`, and `--scenario`. Ensure arguments are parsed cleanly.
