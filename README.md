# CBS Coordinator — Design Document

*A multi-agent coding coordinator built on conflict-based search principles.*

---

## Origin

The insight that started this came during a drive. Approaching a roundabout, the coordination problem became suddenly obvious: multiple cars, shared nodes, priority rules, no central dispatcher. Traffic works because every agent follows a local rule set that produces globally consistent behavior. The question that followed was: why don't coding agents do the same thing?

The observation that cemented it: Satisfactory's belt splitter networks, OS process schedulers, road networks, and multi-agent code conflicts are all instances of the same problem class — *k agents traversing a shared directed graph under contention with priority rules.* The formal name for this is Multi-Agent Path Finding (MAPF).

Git merge resolution is an ad-hoc, human-in-the-loop approximation of MAPF with no replanning. A principled coordinator could solve this cleanly and automatically.

---

## The Problem Class

In a multi-agent coding session, each agent is traversing a dependency graph — the graph of files and methods that make up the codebase. When two agents need to touch the same node (file, method) at the same time, you get a conflict. The current state of the art handles this poorly:

- **Claude Code Agent Teams**: worktrees + manual task scoping. No graph analysis, no conflict detection.
- **OpenHands**: dependency mapping + topological ordering, but sequential only. Not parallel, not CBS.
- **SWE-agent**: single-agent by design.

The gap is runtime CBS-style coordination for genuinely parallel agents. Nobody in the current ecosystem does this.

From MAPF literature, the solution strategies are:

1. **Subgraph decomposition** — assign agents non-overlapping subgraphs before dispatch. Conflicts structurally impossible, but requires knowing which files each task will touch upfront, which is hard for open-ended tasks.
2. **CBS (Conflict-Based Search)** — let agents plan freely, detect conflicts lazily at the moment of action, replan only the colliding agent. More flexible, more novel, the right target.
3. **Priority inheritance** — senior agent (by spawn order or explicit priority) holds the lock, junior rereroutes.

The coordinator implements CBS-style runtime coordination. Static subgraph decomposition can layer on top later as an optimization.

---

## Architecture Overview

The coordinator is a deterministic service, not an LLM. It lives next to Claude Code as a local Python script, invoked via Claude Code's PreToolUse and PostToolUse hook mechanism. No LLM is in the coordination path — coordination is a graph algorithm problem, not a reasoning problem. Making the coordinator another AI would introduce nondeterminism exactly where you need guarantees.

The full architecture is versioned:

| Version | Capability |
|---------|------------|
| v0 | File-level lock, PreToolUse hook blocks conflicting writes |
| v1 | Subgraph lock, dep graph integration, bloom filter, subgraph hash |
| v2 | I/O contract from call sites, subset validation, preclaim consensus, TTL revert |

---

## v0 — File-Level Locking

### Hook integration

Claude Code exposes a hook system via `.claude/settings.json`. Hooks fire as shell commands at defined points in the agent's tool use lifecycle. The coordinator registers on `PreToolUse` for `Edit`, `Write`, and `MultiEdit` tools, and on `PostToolUse` for the same.

The hook receives tool context as JSON on stdin. Exit 0 allows the tool call. Exit 2 blocks it, and the content written to stderr is fed back to the agent so it can replan.

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write|MultiEdit",
      "hooks": [{"type": "command", "command": "python /path/to/coordinator.py"}]
    }],
    "PostToolUse": [{
      "matcher": "Edit|Write|MultiEdit",
      "hooks": [{"type": "command", "command": "python /path/to/coordinator.py --release"}]
    }]
  }
}
```

### Agent identity

Claude Code's hook payload includes:

- `session_id` — shared across main agent and all subagents in a session
- `agent_id` — present only in subagent contexts, unique per subagent instance
- `agent_type` — the subagent's type name, null for the main agent

The coordinator uses `agent_id` as the lock owner identity, falling back to `session_id` for the main agent. This gives unambiguous ownership without any extra instrumentation at spawn time.

### Lock table

SQLite with WAL mode. The lock table uses a composite primary key of `(file_path, agent_id)` — a deliberate design: the same agent can re-enter its own lock (idempotent), but a different agent hitting the same file_path gets an `IntegrityError` on insert, which is the conflict signal.

```sql
CREATE TABLE IF NOT EXISTS locks (
    file_path   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    agent_type  TEXT,
    acquired_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (file_path, agent_id)
);
```

File paths are normalized to absolute paths before lookup so worktree variants collapse to one canonical key.

### Failure mode

The coordinator fails closed. If the DB is unavailable, or the hook script errors, the tool call is blocked with a coordinator error message. Failing open would silently degrade to zero coordination — agents would bulldoze each other and there would be no visible signal that anything was wrong. Visible failure is always preferable to silent degradation.

### Lock release

The `--release` flag on PostToolUse triggers `DELETE FROM locks WHERE agent_id = ?`, releasing all locks held by the completing agent. Release failures are non-critical and swallowed silently — worst case a stale lock lingers until TTL expiry (v2).

---

## v1 — Subgraph Locking and Environment Drift Detection

### Why file-level locking is insufficient

File locking prevents two agents from writing the same file simultaneously, but it doesn't prevent semantic drift. Agent A finishes editing `format_data()` and releases its lock. Agent B then edits `validate_input()`, which `format_data()` depends on. Agent A's work is now built on a foundation that shifted under it — and nobody caught it.

The unit of locking needs to be the *subgraph*, not the file.

### Dependency graph

Existing tooling handles most of the static graph construction:

- Python: `pydep`, `modulegraph`
- TypeScript/JavaScript: `madge`, `dependency-cruiser`

Graph invalidation is asymmetric, which matters for performance:

- **Adds** (new files, new imports): append and branch. The existing graph stays valid, new edges extend it. Cheap, common case.
- **Deletes**: force full recalculation. A node disappearing can orphan dependents or collapse a subgraph. Structurally destructive.

### Subgraph hash

At lock acquisition, the coordinator crawls the dependency subgraph rooted at the target node and hashes the relevant bits — file mtimes for v1, AST node signatures for later. This hash is stored alongside the lock.

At write time (PostToolUse), the same hash is recomputed and compared against the stored value. If it has drifted — meaning something outside this agent mutated a dependency while the agent was working — the agent's edit was built on stale assumptions. The coordinator blocks and explains, and the agent replans.

This is environment drift detection: not "what did this agent change" but "did the world this agent was reasoning from shift under it."

### Bloom filter

In large codebases, a dependency subgraph can contain thousands of nodes. Checking each against SQLite individually before acquiring a subgraph lock is wasteful. A bloom filter sits in front of the DB as a pre-check: nodes that are definitely not locked are skipped, only candidates hit SQLite. False positives just mean an unnecessary DB lookup, which is acceptable. False negatives are impossible by construction.

For v0 with single-file locking, the bloom filter adds nothing — one primary key lookup is already optimal. It earns its place at subgraph scale.

---

## v2 — I/O Contract Validation, Consensus, and Crash Recovery

### The transposed matrix problem

Subgraph hashing catches environment drift, but not semantic contract violations. Consider `mutate_data()` which returns a matrix. An agent comes along and changes it to return a transposed matrix because their new module needs it that way. The file hash changes, but the function signature doesn't — same name, same argument types, same return type annotation. Static analysis sees nothing wrong.

The methods that depend on `mutate_data()` were written assuming a non-transposed output. They are now silently broken.

The coordinator needs to validate I/O contracts at runtime, not just statically.

### I/O contract from call sites

The key insight: the coordinator doesn't need a test suite. The dependency graph itself provides the test data.

Every method has callers (one edge up the dep graph) and callees (one edge down). The callers are the source of real, representative inputs — they contain actual call sites with real arguments. One edge deep is the right bound: deep enough to get real data, shallow enough to stay cheap.

The protocol:

**At lock acquisition:**
1. Crawl one edge up the dep graph to find direct callers of the target method
2. Extract a real input from an actual call site (literal arguments, or constructed from type annotations via AST)
3. Execute the method with that input, snapshot the output *structure* — shape and types, not exact values
4. Store the snapshot alongside the lock in SQLite

**At write time:**
1. Re-execute the method with the same input
2. Validate the new output is a *superset* of the snapshotted structure

### Open-world validation

The validator uses subset semantics, not strict equality. Additive changes — new fields, extended output, additional return values — pass without issue. Subtractive or mutating changes — missing field, type changed, transposed matrix — block.

This is the open-world assumption applied to I/O contracts: the coordinator only cares that the minimum required pieces are present and correct. It ignores anything extra. This prevents the system from forcing technical debt workarounds when the right solution genuinely is to extend the output schema.

Strictly: `required_keys ⊆ new_output_keys` and `types_match(required_keys)`.

### Deadlock resolution — preclaim consensus

Classical deadlock: Agent A holds a lock on `foo()` and needs `bar()`. Agent B holds `bar()` and needs `foo()`. Both wait forever.

The solution maps directly to classical DB concurrency control — wound-wait or wait-die — because this is the same problem class. Before acquiring a lock, the agent broadcasts its intent. The coordinator checks the current lock graph for cycles. On cycle detected, rather than waiting, the coordinator negotiates a swap: agents trade their locked resources and proceed from the other's position. This is O(n) space to track agent intent, O(n²) worst case for cycle detection, but n (number of concurrent agents) is small in practice.

### Crash recovery — TTL revert

If an agent crashes mid-edit, its PostToolUse hook never fires. The lock is never released. Without recovery, the locked files are permanently inaccessible to other agents for the session.

On TTL expiry — where TTL scales with method line count as a heuristic for expected edit duration — the coordinator does not merely release the lock. It first reverts the file to the pre-lock snapshot (already stored for the I/O contract check), then releases. A crashed agent leaves zero damage. The codebase is in a known-good state.

The system makes no runtime guarantees of correctness. Revert-on-TTL is the correct conservative default: deterministic, safe, and leaves a clear audit trail.

---

## What This Is Not

This system is not a test suite. It does not verify program correctness. It does not understand what the code does semantically. It is a coordination layer that enforces structural consistency contracts between agents operating in parallel on a shared dependency graph.

The LLMs do the actual code work. The coordinator does assignment and collision detection. These are different jobs and they should be done by different systems.

---

## Complexity Estimate

- v0: ~100 LOC Python. An afternoon.
- v1: Large project tier for a pragmatic Python + TS implementation. The dep graph integration and subgraph hashing are the bulk of the work.
- v2: Approaches PhD territory only if targeting formal correctness guarantees across arbitrary languages. For pragmatic Python + TS with open-world validation and heuristic TTL, it stays at Large project level.

The integration of runtime agent coordination, partial AST rollback, live dep graph, and I/O contract validation from real call sites is novel relative to the current ecosystem. No existing multi-agent coding framework implements this combination.

---

## License

GPLv3. Commercial products may ship the coordinator as an unmodified subprocess. Forks that add proprietary features must open source those modifications. This is intentional.

---

*Design developed, latest update May 2026. v0 confirmed working.*
© Ilari Pirkkalainen [ieepirzy](https://github.com/ieepirzy) 2026

**AUTHORIZED EYES ONLY DOCUMENT NOT MEANT FOR PUBLIC DISCLOSURE OR CIRCULATION UNTIL INTENTIONAL PUBLIC RELEASE**