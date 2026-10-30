# Usage-aware resource gate — design

Status: Draft — pending approval (phase 1 only)
Date: 2026-10-30
Builds on: `harness/mcp_gate.py` (on-demand MCP gate, branch `claude/work-20261029-d98e59c8`)

## Intent

Agent tooling should hold memory only while it is actually in use, so a
16 GB or 32 GB workstation can run several agents at once without paging.
Two kinds of resource are left running today whether or not anything uses
them:

1. **MCP servers.** Every agent session starts its own copy of every
   registered server and keeps it for the whole session.
2. **Container stacks in WSL/Docker.** Local Supabase (or any compose
   project) stacks stay up for weeks; two copies of the same project's stack
   can run side by side; the WSL VM keeps their memory plus Linux page cache.

The gate already fixes "starts before it is needed" for MCP servers. This
design covers the rest of what the operator asked for: how long to keep a
resource warm, accounting for what each one costs, knowing whether another
agent is using it, and proving it was cleaned up.

## Goals

- A resource is running only while some agent is using it or is likely to use
  it again within a short, measured window.
- Keep-warm time shrinks automatically under memory pressure, so a 16 GB
  machine degrades by restarting tools more often, not by paging.
- Every wake, call, and stop is accounted (startup time, process-tree memory),
  so keep/remove decisions rest on data.
- A stop is verified: the process tree is gone, and any survivor is reported.
- Parallel agents can share one instance of a server that is safe to share.

## Non-goals

- No cloud component; nothing leaves the machine.
- No automatic stopping of container stacks. Stacks hold data, and agents
  restart them; stopping one stays a human action. The tooling reports and
  recommends.
- No change to how agents choose tools; this is lifecycle, not routing of
  requests between providers.

## Anti-goals (failure even if it "works")

- An agent's call fails or hangs because the gate stopped a server mid-use.
- Two sessions silently share a stateful server (a browser, a terminal
  session) and corrupt each other's state.
- A local listener that another user or process on the machine can drive.

## Phase 1 — accounting, pressure-scaled idle, verified cleanup (in the gate)

No daemon; everything stays inside the per-session gate.

1. **Accounting.** Emit schema-1 telemetry records through the existing
   `harness.telemetry_store`: `gate_wake` (`server`, `session`, `startup_ms`),
   `gate_call` (`server`, `tool`, `duration_ms`, `ok`), `gate_sleep`
   (`server`, `reason`: idle | pressure | manual | exit | dead, `awake_s`,
   `calls`), each with the downstream process tree's working set (`tree_mb`)
   sampled at wake and before stop, and an ISO-8601 UTC `ts`. Stdlib only:
   Win32 `GetProcessMemoryInfo` / Toolhelp snapshot on Windows, `/proc` on
   Linux, `ps` on macOS; a failed sample records `null`, never raises.
2. **Pressure-scaled idle.** Effective idle timeout =
   - `idle_timeout_seconds` while free RAM ≥ `low_watermark`;
   - `min_idle_seconds` (default 60) below `low_watermark`;
   - immediate stop of every idle server below `critical_watermark`.
   Watermarks default from total RAM (tier table below) and are overridable
   in `gate.json`. The reaper samples free RAM each tick.
3. **Verified cleanup.** The gate records each downstream's root PID at wake
   (from the process it launches) and, after a stop, checks the root and its
   descendants are gone within `STOP_WAIT_SECONDS`. Survivors are logged to
   stderr and recorded in the `gate_sleep` record (`leaked_pids`). On start,
   the gate sweeps PIDs it recorded in a previous run that crashed, but only
   if their image path and start time still match the record.
4. **Report.** `agent-harness-gate report [--days N]` summarizes per server:
   wakes, calls, warm minutes, median startup, median tree MB, MB·hours held,
   and a recommendation (`unused in N days: remove`, `re-woken > X/day by
   the same session after short gaps: raise idle`, `startup > Ys: pin the
   package version`).

Tier defaults (overridable; to be checked against phase-1 data):

| Total RAM | `low_watermark` | `critical_watermark` | default idle |
|---|---|---|---|
| ≤ 16 GB | 3 GB free | 1.5 GB free | 300 s |
| ≤ 32 GB | 5 GB free | 2.5 GB free | 600 s |
| > 32 GB | 8 GB free | 4 GB free | 900 s |

Tests: pressure tier selection with an injected free-RAM sampler; idle
shortening and critical stop with real fake-server processes (as the existing
lifecycle tests do); telemetry records written and parseable; leaked-PID
detection with a downstream that spawns a detached grandchild; report
recommendations from a synthetic telemetry file.

### Decisions made while building (2026-10-30)

- **Own ledger file.** Gate records go to `gate-YYYYMM.jsonl` with their own
  schema 1, not through `telemetry_store`'s `events-*.jsonl`. That keeps the
  agent/event contract and its readers unchanged. `gate_sleep` also carries
  `tree_pids` (every PID verified at stop) and `reaped_pids`, and there is a
  `gate_sweep` record for crash cleanup.
- **The tier also sets the default idle.** A server with no
  `idle_timeout_seconds` (and no top-level default) gets the tier's default
  idle, resolved when the config is loaded. Unknown total RAM gives the 32 GB
  tier. Unknown free RAM counts as "normal", so a failed probe never stops a
  server.
- **Critical means an effective idle of 0.** Every server with no call in
  flight stops on the next reaper tick. A stop counts as "pressure" only when
  the configured timeout alone would not have stopped the server.
- **Root PID attribution.** The SDK does not expose the PID it launched, so
  the gate snapshots its own direct children just before and just after the
  spawn, under a gate-wide lock. Only the spawn is under the lock, not the
  MCP startup that follows, so a slow server does not hold up other wakes.
  The root counts only when exactly one new child appears, and other servers'
  roots are excluded. Otherwise `root_pid` is null: accounting degrades,
  behaviour does not.
- **Tree sampling is identity-guarded.** The tree is sampled from the root
  only while the root still has its recorded creation time, plus earlier
  identities that are still alive. Sampling happens at wake, on every reaper
  tick (to refresh the crash registry) and just before stop. A recycled root
  PID therefore never pulls in a stranger's tree.
- **Process identity is PID plus creation time; the image path is not
  compared.** That is enough to rule out PID reuse. A POSIX `exec` keeps both
  values but changes the image, and treating that process as gone would hide
  a leak. This deviates from item 3's "image path and start time".
- **Verified stop.** The gate waits `STOP_VERIFY_SECONDS` (3 s) for the tree
  to exit, then calls `procinfo.terminate`, which re-checks the identity
  through the same handle it kills with. Anything still alive is
  `leaked_pids` plus one stderr line. A server whose process exits on its own
  is finalized as `dead` on the next reaper tick or wake.
- **Module layout.** Config lives in `harness.gate_config` and process
  accounting in `harness.gate_runtime`. The command line lives in
  `harness.gate_cli`, which `harness.mcp_gate.main` delegates to. This keeps
  each file under 800 lines. `harness.mcp_gate` re-exports the config names.

## Phase 2 — machine-wide broker for shareable servers

Only sharing saves memory across sessions; knowing that another agent uses a
tool is worth nothing if each session still runs its own copy.

- One broker per user per machine owns downstream processes. Session gates
  become thin clients of it, or clients connect to it directly.
- **Shareability is per server and opt-in**: `share: "session"` (default —
  one instance per session, lifecycle still brokered) or `share: "machine"`
  (one instance for all sessions; only for stateless servers).
- **Leases.** Each session holds a lease per server it has used; a shared
  server stops only when no lease has had a call within its idle window and
  no call is in flight from any session. `gate_status` shows other sessions'
  leases ("in use by 2 other agents").
- **Transport.** Loopback only (`127.0.0.1`), per-user random token stored in
  a file readable only by the user, rejected without it. Before building,
  verify from current docs whether Claude Code accepts `type: "http"` MCP
  entries and whether Codex accepts streamable-HTTP `url` entries; where a
  client needs stdio, the session keeps the existing gate (~54 MB measured)
  as a shim, which still replaces every heavy server it fronts.
- **Adaptive keep-warm** (only after phase-1 data exists): keep a server warm
  for the recent p80 gap between calls in the same burst, clamped to
  `[min_idle, idle_timeout]`, and never above what the pressure tier allows.

## Phase 3 — WSL / Docker footprint: report and recommend

- Extend the report with container stacks grouped by compose project: memory,
  uptime, last activity (CPU / network deltas over a sample window), and
  duplicates of the same project.
- Report published ports bound to `0.0.0.0` for local dev stacks as a
  finding, since they expose databases to the LAN.
- Recommend per-tier `.wslconfig` caps derived from the measured size of the
  stacks actually in use, and `autoMemoryReclaim`.
- Recommend a lean local Supabase profile (services an agent workflow does not
  need, such as Studio, the mail catcher, and edge runtime). The exact
  `supabase/config.toml` keys must be verified against current Supabase docs
  before the report prints them.
- Stopping a stack remains a human action (`supabase stop` keeps volumes).

## Decided

- Default share mode is per session; machine sharing is opt-in per server.
- Container stacks are never stopped automatically.
- Phase 1 uses fixed, pressure-scaled timeouts; prediction waits for data.

## Left to the builder

- Telemetry field names beyond those listed, and the report's layout.
- Sampling interval for free RAM (bounded by the reaper interval).

## Open before phase 2

- Client transport support (see Transport).
- Which of the operator's servers are genuinely stateless enough to share.
