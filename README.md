# CBS Coordinator — Design Document

*A multi-agent coding coordinator built on conflict-based search principles.*

---

> [!NOTE]
>  This repository uses Claude as a comparison and a baseline agentic harness, however the only Claude-specific thing is the schema of the hooks. The concept of hooks exists for all agentic harnesses making this framework harness agnostic with minor revisions.

> [!NOTE]
> **Language scope:** The coordinator currently supports Python only. `_is_python()` guard in `handle_pretool()` passes non-Python files through uncoordinated. TypeScript/JavaScript dep graph integration (madge, dependency-cruiser) is a planned extension.

## Origin

The insight that started this came during a drive. Approaching a roundabout, the coordination problem became suddenly obvious: multiple cars, shared nodes, priority rules, no central dispatcher. Traffic works because every agent follows a local rule set that produces globally consistent behavior. The question that followed was: why don't coding agents do the same thing?

The observation that cemented it: Satisfactory's belt splitter networks, OS process schedulers, road networks, and multi-agent code conflicts are all instances of the same problem class — *k agents traversing a shared directed graph under contention with priority rules.* The formal name for this is Multi-Agent Path Finding (MAPF).

Git merge resolution is an ad-hoc, human-in-the-loop approximation of MAPF with no replanning. A principled coordinator could solve this cleanly and automatically.

---

## The Problem Class

In a multi-agent coding session, each agent is traversing a dependency graph — the graph of files and methods that make up the codebase. When two agents need to touch the same node (file, method) at the same time, you get a conflict. The current state of the art handles this poorly:

- **Claude Code Agent Teams**: worktrees for fs isolation, does not handle merge conflicts automatically. No graph analysis, no conflict detection.
- **OpenHands**: dependency mapping + topological ordering, but sequential only. Not parallel, not CBS.
- **SWE-agent**: single-agent by design.

The gap is runtime CBS-style coordination for genuinely parallel agents. Nobody in the current ecosystem does this.

From MAPF literature, the solution strategies are:

1. **Subgraph decomposition** — assign agents non-overlapping subgraphs before dispatch. Conflicts structurally impossible, but requires knowing which files each task will touch upfront, which is hard if not intractable for open-ended tasks.
2. **CBS (Conflict-Based Search)** — let agents plan freely, detect conflicts lazily at the moment of action, replan only the colliding agent. More flexible, more novel, the right target.
3. **Priority inheritance** — senior agent (by spawn order or explicit priority) holds the lock, junior rereroutes.

The coordinator implements CBS-style runtime coordination. Static subgraph decomposition can layer on top later as an optimization.

---

## Architecture Overview

The coordinator is a deterministic service integrated into the agents harness. It lives next to Claude Code as a local Python script, invoked via Claude Code's PreToolUse and PostToolUse, and other hook mechanisms. No LLM is in the coordination path — coordination is a graph algorithm problem, not a reasoning problem. Making the coordinator another AI would introduce nondeterminism exactly where you need guarantees.

The full architecture is versioned:

| Version | Capability | Status |
|---------|------------|--------|
| v0 | File-level lock, PreToolUse hook blocks conflicting writes | Implemented |
| v1 | Method-level subgraph lock, dep graph integration, subgraph Merkle hash | Implemented |
| v2 | I/O contract from call sites, open-world superset validation, cascade flow, TTL revert | Implemented |

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
    }],
    "PostToolUseFailure": [{
      "matcher": "Edit|Write|MultiEdit",
      "hooks": [{"type": "command", "command": "python /path/to/coordinator.py --failure"}]
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

SQLite with WAL mode. The lock table uses `file_path` as the sole primary key — a different agent hitting the same `file_path` gets an `IntegrityError` on insert, which is the conflict signal. The same agent re-entering its own lock is handled by checking the existing `agent_id` before inserting.

```sql
CREATE TABLE IF NOT EXISTS locks (
    file_path   TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    agent_type  TEXT,
    acquired_at TEXT DEFAULT (datetime('now'))
);
```

File paths are normalized to absolute paths before lookup so worktree variants collapse to one canonical key.

### Failure mode

The coordinator fails closed. If the DB is unavailable, or the hook script errors, the tool call is blocked with a coordinator error message. Failing open would silently degrade to zero coordination — agents would bulldoze each other and there would be no visible signal that anything was wrong. Visible failure is always preferable to silent degradation.

### Lock lifetime — the hopping model

The coordinator does not tie lock lifetime to agent lifetime. An agent editing three unrelated files — `parser.py`, `renderer.py`, `config.py` — with no shared dependency edges between them is executing three independent editing sessions. Holding a lock on `parser.py` while working on `renderer.py` is unnecessary and blocks other agents from files they could legitimately touch.

The lock window is precisely:

- **Acquired**: PreToolUse fires, coordinator inserts the lock row
- **Required**: the edit is in flight and the global cache has not yet been updated with the result
- **Released**: PostToolUse completes, global cache updated with new subgraph hash, lock row deleted

After PostToolUse for `parser.py`, the lock is released. The agent hops to `renderer.py`, acquires a new lock, edits, releases. Lock lifetime equals subgraph-editing-session, not agent lifetime.

The exception: if an agent's edits span a shared dependency subgraph, locks on all nodes in that subgraph must be held for the duration of all edits within it. Releasing mid-way would allow another agent to mutate a shared dependency while the first agent's work is still in progress.

### The editing session boundary problem

Claude Code does not expose a clean "editing session start" hook. There is no event that fires when Claude begins reasoning about what it intends to edit — only when it submits the tool call. This is not a gap in the hook design; it reflects a fundamental property of LLMs: intent cannot be determined from reasoning alone, only from what is ultimately acted on.

> Note: It is possible that future MI research might allow for a mostly deterministic selection, a way of knowing what files and methods specifically Claude and other LLMs want to touch, for example through natural language autoencoders.

The implication: the coordinator cannot pre-claim a subgraph before Claude reasons about it. Lock acquisition happens at PreToolUse, the earliest point where the target is known with certainty. Everything before that is probabilistic pre-warming. Everything after that is deterministic enforcement.

This creates an efficiency exposure: if Claude reasons for a long time before issuing an Edit call, another agent may apply changes to a dependency in the meantime. The PreToolUse hash check catches this — the subgraph has drifted, the edit is blocked, Claude replans with the current picture. The reasoning work is lost. This is a token efficiency problem, not a correctness problem; correctness is always maintained by the hash check. The frequency of this case is trackable heuristically via the timestamp gap between the last `FileChanged` on a dependency and the blocked `PreToolUse` — high rates indicate either long reasoning turns or high contention on a subgraph, and are worth logging as a diagnostic signal.

> LLMs are indeterministic actors and the system shall treat them as such.

### Lock release

The `--release` flag triggers `DELETE FROM locks WHERE file_path = ?`, releasing the lock for the completed editing session. Release failures are non-critical and swallowed silently — worst case a stale lock lingers until TTL expiry (v2).

---

## v1 — Subgraph Locking and Environment Drift Detection

### Why file-level locking is insufficient

File locking prevents two agents from writing the same file simultaneously, but it doesn't prevent semantic drift. Agent A finishes editing `format_data()` and releases its lock. Agent B then edits `validate_input()`, which `format_data()` depends on. Agent A's work is now built on a foundation that shifted under it — and nobody caught it.

The unit of locking needs to be the *subgraph*, not the file.

### Lock table (v1)

The lock table key changes from `file_path` (v0) to `node_id` — a `"abs_path::name"` identifier for the specific function or class being edited. This enables two agents editing different methods in the same file to proceed concurrently without conflict.

```sql
CREATE TABLE IF NOT EXISTS locks (
    node_id        TEXT PRIMARY KEY,   -- "abs_path::function_name" or "abs_path::__file__"
    file_path      TEXT NOT NULL,      -- denormalised for batch release queries
    agent_id       TEXT NOT NULL,
    agent_type     TEXT,
    subgraph_hash  TEXT,               -- Merkle root at acquisition time
    subgraph_nodes TEXT,               -- JSON list of node_ids in the 1-edge subgraph
    acquired_at    TEXT DEFAULT (datetime('now'))
);
```

The `abs_path::__file__` sentinel is used when the Write tool targets a file with no existing structural nodes (empty or brand-new file), giving the first writer an exclusive file-level lock until the file has content.

### Conflict check implementation

The coordinator loads the full locks table into a Python dict at PreToolUse:

```python
current_locks: dict[str, str] = dict(conn.execute(
    "SELECT node_id, agent_id FROM locks"
).fetchall())
```

A single read of all locks avoids TOCTOU races from concurrent acquisitions. At typical session scales (dozens of agents, hundreds of locked nodes), this is cheaper than per-node SQLite queries.

### Dependency graph

The coordinator builds its own dependency graph via static AST analysis (`dep_graph.py`). No third-party graph tools are required. Nodes are method-level:

- `abs_path::function_name` — module-level `def` and `async def` functions
- `abs_path::ClassName.method_name` — class methods (`def` and `async def`), both sync and async treated identically
- `abs_path::N` (line number) — module-level non-function statements (imports, variable declarations, bare expressions)

`async def` is a first-class node type, not a special case. Classification (`structural | external | benign`) and locking apply identically to sync and async callables. Edges are cross-file import relationships within the project.

Graph invalidation is asymmetric, which matters for performance:

- **Adds** (new files, new imports): append and branch. The existing graph stays valid, new edges extend it. Cheap, common case.
- **Deletes**: force re-indexing of dependent files. A node disappearing can orphan dependents or collapse a subgraph. Structurally destructive.

### Subgraph hash

**At PreToolUse (lock acquisition):** the coordinator crawls the dependency subgraph rooted at the target node, fetches the current content hashes from the global cache, computes a Merkle root over the 1-edge subgraph, and stores it alongside the lock row. This snapshot represents the state of the world at the moment the agent committed to its edit.

**At PostToolUse (lock release):** the same Merkle root is recomputed from the current global cache and compared against the stored value. If it has drifted — meaning something outside this agent mutated a dependency in the narrow window between snapshot and lock application — the agent's edit was built on stale assumptions. The coordinator blocks and explains, and the agent replans.

This is environment drift detection: not "what did this agent change" but "did the world this agent was reasoning from shift under it."

Edits that add new methods or files will trigger graph extensions and crawling PostToolUse.

### Ownership-aware drift detection

When multiple agents are working in parallel, a naive Merkle comparison would incorrectly flag an agent's own earlier writes as external drift. The coordinator tracks each agent's traversal (nodes targeted, edges followed) in `agent_traversal_nodes` and `agent_traversal_edges` tables. At PostToolUse drift check, nodes the agent itself wrote are excluded from the comparison, preventing false positives on parallel batch edits.

### Lazy cache population

There is no upfront crawl of the codebase. Upfront crawling doesn't scale — new codebases start from nothing with a rapidly growing dep graph, and large codebases take too long to crawl naively. Instead, the cache is populated lazily by write events.

Crawl depth is not uniform — it depends on the hook type:

- **PreToolUse (enforcement):** 1 edge deep. Tight scope, fast, captures direct dependencies only. This is the depth used for lock acquisition and drift detection.
- **Passive warming hooks** (`FileChanged`, `CwdChanged`, non-editing tool use): 2-3 edges deep. Wide scope, populates the cache ahead of writes that haven't happened yet.

When an agent's PreToolUse fires, the coordinator checks the 1-edge subgraph against the cache and stores the snapshot. The cache stays current as a natural side effect of write hooks: if agent B touched a dependency, it went through PreToolUse, meaning the coordinator already crawled and cached that node. By the time agent A wants to edit something in that dependency chain, fresh hashes are already in the DB. No read hooks are needed — write events are sufficient.

A global state is kept consistent even when agents are working in different git worktrees by utilizing git commands. Each agent's worktree has its own filesystem view; the coordinator normalises all paths to canonical project-root-relative keys and uses git to read file state from the authoritative tree rather than from a specific worktree's working copy.

An existing conceptual limitation is deeply nested dynamic imports e.g. `importlib.import_module()` where the target is a variable. Static AST crawl cannot resolve these cases.

### Worktree state vs. global cache

> Qualitatively: the worktree is "what is going on in my world." The global cache is "what is actually real."

Each agent operates in its own git worktree, giving it an isolated filesystem view. The coordinator maintains a global SQLite cache as the single source of truth for subgraph hashes across all agents. These two layers serve distinct purposes and must not be conflated.

**Passive crawl triggers and full hook inventory**

Beyond write-time crawling, Claude Code exposes hooks that allow the coordinator to update the global cache before any tool use fires. The full set of hook events and their coordinator relevance:

| Hook | Coordinator use |
|------|----------------|
| `SessionStart` | Initialize coordinator DB, run initial cache warm |
| `SubagentStart` | Register agent identity and priority tier, begin tracking |
| `SubagentStop` | Deregister agent, release any remaining locks held by that agent |
| `WorktreeCreate` | Record worktree↔agent mapping, used for path normalization |
| `WorktreeRemove` | Clean up worktree mapping, trigger final lock release for that worktree |
| `FileChanged` | File mutated in a worktree — crawl subgraph from that file, update global cache |
| `CwdChanged` | Agent navigated to new directory — crawl 2-3 edges from new context, warm cache |
| `PreToolUse` | **Primary enforcement point** — lock acquisition and drift check before any write |
| `PostToolUse` | **Primary release point** — cache update and lock release after write completes |
| `PostToolUseFailure` | Edit failed — release lock without updating cache, leave previous hash intact |
| `PreCompact` | Checkpoint WAL before context compaction |
| `SessionEnd` | Final cleanup, squash intermediate micro-commits, release all remaining locks |

> **Note:** `PostToolBatch` is **not** a registerable hook event in the current Claude Code CLI. The coordinator has a `--post-tool-batch` handler for manual or future use, but it cannot be wired up via `.claude/settings.json` today.

Hooks not listed (`UserPromptSubmit`, `Notification`, `Stop`, etc.) have no coordinator relevance.

`FileChanged` and `CwdChanged` are the primary passive warming triggers. By the time `PreToolUse` fires on a write, the cache is likely already warm from the agent's own reconnaissance. Cache checks on nodes that haven't changed since last crawl resolve as cheap hash comparisons with no further work. The crawl cost is paid once per node per session; subsequent hits are essentially free.

**Race conditions**

The passive crawl architecture introduces a race condition: agent A's `PostToolUse` or `FileChanged` updates the global cache while agent B is mid-reasoning about a node in the same subgraph. Agent B's reasoning was formed against a now-stale picture. The coordinator cannot detect this during B's reasoning turn.

This is acceptable for two reasons:

1. Locks prevent the worst case. If agent A holds a lock on any node, agent B cannot acquire a lock and apply an edit on any node that shares a dependency with it. The lock graph structurally prevents the most damaging collisions — two agents cannot simultaneously commit writes to a shared subgraph. However should the agents write quickly in sequence, any conflicting changes are blocked for the agent that writes last.

2. `PreToolUse` is the safety net. Even if agent B reasoned against stale state, the write-time hash comparison catches any drift before the edit is applied. Agent B gets blocked and replans with the current picture.

The race condition means the coordinator cannot guarantee that agent B's *reasoning* was accurate. It can guarantee that agent B's *writes* don't corrupt a subgraph that shifted under it. That is the correct promise for a deterministic coordination layer.

**Benign dependency collisions**

Not all dependency collisions warrant blocking. Import statements and module-level variable declarations are shared dependencies — every method in a file implicitly depends on them — but edits to them are typically additive and low-risk. Two agents both importing a new module from the same file do not conflict in any meaningful sense.

The coordinator classifies dependency nodes by type:

- **Structural nodes** (method definitions, class definitions): full lock enforcement, no concurrent writes.
- **Benign nodes** (import statements, module-level variable declarations): collision allowed, concurrent writes permitted, hash still tracked for drift detection.

This avoids false blocks on trivially non-conflicting edits while maintaining full protection on the nodes where semantic conflicts actually occur. The classification is statically determinable from the AST node type — no LLM needed.

> [!IMPORTANT]
> Benign nodes do not need to be locked as long as they are not the target of an edit.
> Edits targeting benign nodes directly will still be blocked when a lock is shared.

### Cold cache rule

PreToolUse on any write, if the node is not in cache: crawl now, store as baseline, then proceed with lock acquisition. No exceptions. If nodes are already crawled, the baseline is updated from global cache.

A cold cache on a write means no baseline hash exists, so drift cannot be detected. The on-demand crawl *is* the baseline establishment. Cold cache is never "skip" — it's always "crawl first."

The cost is acceptable: it happens once per node per session, and only for nodes that actually get touched. After the first crawl the node is warm and subsequent checks are hash comparisons only.

### Dual-use AST cache

The same parse and crawl serves both subgraph hash comparison (drift detection) and I/O contract validation. The expensive part — parsing — happens once per node per session. Two consumers, one cache. Allowed writes, that is, writes that pass the pre and post tool use hooks for the editing agent, which ensures consistency (see below), then overwrite the previous hash.

### Priority queue

A `lock_queue` table holds requests that arrive while a target node is already locked. Priority tiers are stored in the `agents` table (`orchestrator=100`, `senior/lead=80`, default=0). Queued agents are processed in priority order when the lock is released.

> **Known gap — classical deadlock:** The priority queue provides ordering but has no cycle detection. Agent A holding `foo()` and needing `bar()` while Agent B holds `bar()` and needs `foo()` will deadlock permanently. See [OPEN TODOs](#open-todos) for the suggested fix.

---

## v2 — I/O Contract Validation, Consensus, and Crash Recovery

### The "transposed matrix" problem

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
3. Execute the method with that input in a subprocess, snapshot the output *structure* — shape and types, not exact values
4. Store the snapshot alongside the lock in SQLite

**At write time:**
1. Re-execute the method with the same input
2. Validate the new output is a *superset* of the snapshotted structure

**Argument resolution order:** literal call sites are tried first. If none found, type annotations are used (`int → 0`, `str → ""`, etc.). If a parameter has no annotation or a complex type that isn't a known scalar, it is substituted with `None` at execution time. If the subprocess returns non-zero or times out (5s default), the snapshot is `None` — contract validation is silently skipped for that node. External nodes (functions that call `requests`, `anthropic`, `psycopg2`, etc.) are skipped entirely.

### Open-world validation

The validator uses subset semantics, not strict equality. Additive changes — new fields, extended output, additional return values — pass without issue. Subtractive or mutating changes — missing field, type changed, transposed matrix — block.

This is the open-world assumption applied to I/O contracts: the coordinator only cares that the minimum required pieces are present and correct. It ignores anything extra. This prevents the system from forcing technical debt workarounds when the right solution genuinely is to extend the output schema.

Strictly: `required_keys ⊆ new_output_keys` and `types_match(required_keys)`.

### Cascade Validation — Planned Contract Evolution

Hard-blocking on I/O contract changes is the wrong default. A refactor that
genuinely needs to change a return type or restructure output is legitimate work —
blocking it forces the agent into bad workarounds or silent schema drift. The correct
response to a detected contract change is not obstruction but surfacing: make the
blast radius explicit, force the change to be deliberate, and let the agent proceed
with full awareness of what it has committed to.

When PostToolUse detects an I/O contract change on a target node — a subtractive or
mutating delta that fails the superset check — the coordinator does not block. Instead
it crawls upward from the changed node to identify all direct and transitive dependents
whose I/O assumptions are now invalidated. The agent receives a cascade task list via
stderr:

    I/O contract change detected on mutate_data().
    Dependents requiring update: validate_input(), format_output(), serialize_result().
    Cascade lock acquired. Proceed with updates or revert to restore previous contract.

The cascade task list is ordered by dependency graph topology — innermost dependents
(direct callers) first, then transitive dependents, so each node is updated against
an already-resolved contract rather than a mix of old and new. The coordinator extends the subgraph lock to cover the entire
cascade set for the duration of the refactor. Any other agent attempting to acquire
a lock on a node within the cascade subgraph is blocked with the reason:

    Cascade refactor in progress on mutate_data() by agent <id>.
    Affected subgraph is locked until cascade completes or reverts.

The agent may revert at any point before the cascade completes. Revert triggers
TTL-style rollback to the pre-lock snapshot for all modified nodes in the cascade
set, restoring the previous contract in full. Once all dependents have been updated
and their PostToolUse checks pass, the cascade lock is released atomically and the
global cache is updated with the new contract state across the entire affected
subgraph.

The cascade produces a clean audit trail by construction — each step is a documented,
coordinator-verified contract update in topological order. Silent breaking changes
that surface as runtime bugs are structurally impossible under this model. The agent
does the work it would have done anyway; the coordinator ensures it does it completely
and in the right order.

### Deadlock resolution — preclaim consensus

Classical deadlock: Agent A holds a lock on `foo()` and needs `bar()`. Agent B holds `bar()` and needs `foo()`. Both wait forever.

The priority queue provides ordering when agents queue for the same node, but it does not detect cycles across distinct nodes. Classical deadlock (A→B→A in the lock-wait graph) is a known gap. The suggested fix is wound-wait:

1. Add an `agent_intents` table. At PreToolUse, before acquiring a lock, write the target `node_id` as an intent.
2. On each lock acquisition attempt, BFS the `(node_id → agent_id → intents)` graph for cycles.
3. On cycle detected, wound the lower-priority agent: release its held locks, requeue it, allow the higher-priority agent to proceed.

This is O(n) space for intent tracking, O(n²) worst case for cycle detection, where n is the number of concurrent agents — small in practice.

### Crash recovery — TTL revert

If an agent crashes mid-edit, its PostToolUse hook never fires. The lock is never released. Without recovery, the locked files are permanently inaccessible to other agents for the session.

On TTL expiry — where TTL scales with method line count as a heuristic for expected edit duration — the coordinator does not merely release the lock. It first reverts the file to the pre-lock snapshot (already stored for the I/O contract check), then releases. A crashed agent leaves zero damage. The codebase is in a known-good state.

TTL enforcement runs inline at PreToolUse (`_expire_stale_locks()`). The watchdog process (`watchdog.py`) provides a parallel sweep on a configurable polling interval (default 10s) as a belt-and-suspenders fallback for sessions where PreToolUse is not frequently firing.

The system makes no runtime guarantees of correctness. Revert-on-TTL is the correct conservative default: deterministic, safe, and leaves a clear audit trail.

## Worktree Merge Guarantee & Merkle Optimization

When a blocked agent is given the green light to re-attempt an edit after pulling
in changes from main, the resulting `git merge` into its worktree is guaranteed to
be clean **for CBS-managed files**. This is a logical consequence of the hash-based staleness detection: because CBS blocks any edit applied against an outdated dependency snapshot, by the time an agent holds a subgraph lock and proceeds to write, its view of the dependency graph is current by definition. Sequential writes to any given subgraph are enforced by the lock, so git has no grounds for a textual conflict on locked nodes.

The only conceivable conflicts would be cosmetic and outside the dependency graph entirely (whitespace, line endings), which are not CBS concerns.

> **Implementation note:** On PostToolUse success for worktree files, validated edits are propagated to the main repo immediately via `shutil.copy2()` — no git involved at that step. Git operations are deferred to `SessionEnd`, where intermediate micro-commits are squashed into a single commit. The `--worktree-push` path executes a real `git merge --no-ff` and can still fail on non-CBS-managed changes (e.g., files the agents modified outside coordinator hooks). That failure path is the agent's responsibility to resolve.

As an optimization, agents do not need to query every individual node hash before
acquiring a lock. Instead, a Merkle root over the dependency subgraph serves as a
cheap first-pass check: if the root matches the coordinator's source of truth, all
constituent node hashes are implicitly valid and SQLite queries are skipped entirely.
Only on a root mismatch does the agent walk the tree to identify which specific nodes
have drifted. This preserves the correctness guarantees of per-node content hashing
while significantly reducing coordinator overhead in the common case where no drift
has occurred.

---

## What This Is Not

This system is not a test suite. It does not verify program correctness. It does not understand what the code does semantically. It is a coordination layer that enforces structural consistency contracts between agents operating in parallel on a shared dependency graph.

The LLMs do the actual code work. The coordinator does assignment and collision detection. These are different jobs and they should be done by different systems.

---

## Complexity Estimate

- v0: ~100 LOC Python. An afternoon.
- v1: MSc or large project tier for a pragmatic Python implementation. The dep graph integration and subgraph hashing are the bulk of the work. **Implemented** (~2200 LOC Python total).
- v2: Approaches PhD territory only if targeting formal correctness guarantees across arbitrary languages. For pragmatic Python with open-world validation and heuristic TTL, it stays at MSc or comparable level. **Implemented** (cascade flow, TTL revert, watchdog, worktree management all functional).

The integration of runtime agent coordination, partial AST rollback, live dep graph, and I/O contract validation from real call sites is novel relative to the current ecosystem. No existing multi-agent coding framework implements this combination.

---

## Production Operation

### Prerequisites

- Python 3.11+. No third-party runtime dependencies.
- `pygit2` is optional — the coordinator falls back to `git` subprocess calls when it is not installed.
- All git operations are local. The coordinator runs fully offline. Network access is only required by the user code executed in contract snapshot subprocesses, and those degrade gracefully to `None` on failure.

### Installation

1. Clone this repository alongside your project, or copy `coordinator.py`, `dep_graph.py`, `contract.py`, and `watchdog.py` into a stable path.
2. Configure `.claude/settings.json` in your project root.
3. Optionally start `watchdog.py` as a background process for TTL enforcement independent of hook cadence.

### settings.json — full production configuration

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --session-start"}]}],
    "SessionEnd": [{"hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --session-end"}]}],
    "SubagentStart": [{"hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --subagent-start"}]}],
    "SubagentStop": [{"hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --subagent-stop"}]}],
    "WorktreeCreate": [{"hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --worktree-create-hook"}]}],
    "WorktreeRemove": [{"hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --worktree-remove"}]}],
    "FileChanged": [{"hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --file-changed"}]}],
    "CwdChanged": [{"hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --cwd-changed"}]}],
    "PreCompact": [{"matcher": "manual|auto", "hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --pre-compact"}]}],
    "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py"}]}],
    "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --release"}]}],
    "PostToolUseFailure": [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command",
      "command": "python /path/to/coordinator.py --failure"}]}]
  }
}
```

### CLI reference

All modes read JSON from stdin (the Claude Code hook payload format).

| Flag | Hook event | Description |
|------|------------|-------------|
| *(none)* | `PreToolUse` | Lock acquisition, drift check, contract snapshot |
| `--release` | `PostToolUse` | Drift recheck, cache update, lock release |
| `--failure` | `PostToolUseFailure` | Silent lock release, no cache update |
| `--session-start` | `SessionStart` | Initialize DB schema, optional project crawl |
| `--session-end` | `SessionEnd` | Final cleanup, squash micro-commits, release all locks |
| `--subagent-start` | `SubagentStart` | Register agent identity and priority |
| `--subagent-stop` | `SubagentStop` | Deregister agent, release all its locks |
| `--worktree-create-hook` | `WorktreeCreate` | Register new worktree↔agent mapping |
| `--worktree-remove` | `WorktreeRemove` | Remove worktree mapping, release agent locks |
| `--file-changed` | `FileChanged` | Re-index changed file, update cache (2-3 edge crawl) |
| `--cwd-changed` | `CwdChanged` | Warm cache from new working directory |
| `--pre-compact` | `PreCompact` | WAL checkpoint before context compaction |
| `--crawl-project` | Manual / SessionStart | Recursively index all `.py` files under project root |
| `--revert` | Manual / cascade | Revert all files in a cascade set to pre-lock snapshots |
| `--worktree-create` | Manual | Create a git worktree + branch for an agent |
| `--worktree-push` | Manual | Merge agent branch → main, re-index changed files |
| `--worktree-pull` | Manual | Rebase agent worktree onto latest main |
| `--worktree-status` | Manual | Report live status of all registered worktrees (JSON stdout) |
| `--worktree-log` | Manual | Query worktree event log (JSON stdout) |
| `--post-tool-batch` | Manual | Batch drift check (not a registerable hook event in Claude Code CLI) |

### Watchdog

`watchdog.py` runs alongside the coordinator for the duration of a session. It polls the locks table for expired TTLs, reverts files from snapshots, and marks in-progress cascades as reverted for any agent that crashed.

```bash
python watchdog.py                      # default: poll every 10s
python watchdog.py --poll-interval 5    # custom interval
```

The watchdog is standalone (stdlib only) and reads the same `coordinator.db` that the hook invocations write.

---

## OPEN TODOs

### External call classification

`contract.py` and `dep_graph.py` classify nodes as "external" (skip contract snapshot) by checking whether the function body calls any module in a hardcoded prefix set (`requests`, `openai`, `psycopg2`, etc.). This does not answer the real question: *does the call graph of this specific function reach a node with observable side effects at execution time?*

The current approach has false positives (functions that import `requests` but never use it in the relevant code path) and false negatives (functions that call user-defined wrappers around network calls).

**Suggested fix:** implement effect typing — classify nodes as `pure | io | network | db | ...` and propagate the classification transitively through the call graph. A pure function calling an io function is io. The coordinator already has the call graph; effect propagation is a DFS over it.

### Structural isolation before snapshot execution

`contract.py` executes the target function by importing the module directly in a subprocess. Any module-level code with side effects (initializing a DB connection pool, registering signal handlers, spawning a thread) runs on import and can cause the snapshot subprocess to hang, crash, or produce wrong output.

**Suggested fix:** use `ast.NodeTransformer` to strip or stub any top-level statements that aren't `def`, `async def`, or `class` definitions before writing the rewritten source to the snapshot interpreter. This prevents accidental initialization loops from ever reaching the execution step.

### Synthetic mocks for complex types

When argument resolution falls back to type annotations, only scalar types (`int`, `str`, `bool`, etc.) produce working default values. Parameters annotated with complex types (dataclasses, Pydantic models, SQLAlchemy ORM objects) fall back to `None`, which often causes the subprocess snapshot to fail and return `None` — silently skipping contract validation.

**Suggested fix:** when an annotation name is not a known scalar, query the `nodes` table for a `ClassDef` with that name. If found, read its `__init__` fields and construct a lightweight mock object with those fields set to their own defaults. `__getattr__` fallback to `None` prevents `AttributeError` during snapshot execution:

```python
def mock_type_from_db(annotation_name: str, conn: sqlite3.Connection) -> Any:
    row = conn.execute(
        "SELECT node_id, file_path FROM nodes WHERE name = ? AND kind = 'structural' LIMIT 1",
        (annotation_name,)
    ).fetchone()
    if row:
        class SyntheticMock:
            def __getattr__(self, item): return None
        return SyntheticMock()
    return None
```

### In-memory graph cache for high agent counts

At 100+ concurrent agents, `crawl_subgraph()` BFS traversals hit SQLite on every hop. At scale, loading the `edges` table into a `dict[str, set[str]]` at SessionStart and keeping it warm via incremental updates would reduce BFS to pure dict lookups.

### Deadlock cycle detection

Documented in the [v2 section](#deadlock-resolution--preclaim-consensus). The `lock_queue` priority queue provides ordering but not cycle detection. The wound-wait approach (add `agent_intents` table, BFS lock-wait graph before each acquisition, wound lower-priority agent on cycle) is the concrete suggested fix.

### Asymmetric invalidation cascade (edge case)

`update_file()` correctly re-indexes dependent files when a node is removed or renamed. However, if Agent A *adds* a new import statement to an existing file during an allowed edit, the new dependency edge lands instantly in the DB. Agent B, if it currently holds a subgraph lock that now includes this newly connected edge as a 1-hop forward target, will fail its PostToolUse Merkle check and have its edit rejected — even though Agent B didn't touch the file Agent A just modified.

This is a false positive. The correct behavior is to check whether the Merkle mismatch is caused exclusively by structural additions (new edges, new nodes) that do not invalidate the agent's write, as opposed to mutations that do.

### Multi-match target collateral

`identify_target_nodes()` in `dep_graph.py` maps `old_string` line positions to node line ranges using interval overlap:

```python
if line_start <= edit_line_end and line_end >= edit_line_start:
    matched.add(node_id)
```

If an edit spans a structural boundary — e.g., an `old_string` that overlaps the tail of one function and the head of the next — the agent will acquire locks on nodes it never intended to edit. This is safe (over-locking), but increases lock collision surface area at scale.

---

## Edge cases in dep_graph.py

When mapping an explicit AST to a relational database for deterministic runtime validation, several subtle edge-case interactions become apparent:

**1. The Multi-Match Target Collateral (The "Stretching Node" Trap)**

Look closely at `identify_target_nodes` for an Edit or MultiEdit call:

```python
edit_line_start = source[:offset].count("\n") + 1
edit_line_end   = edit_line_start + old_string.count("\n")
for node_id, line_start, line_end in rows:
    if line_start <= edit_line_end and line_end >= edit_line_start:
        matched.add(node_id)
```

If an agent is editing a large class or function, and they replace an `old_string` that happens to structurally match code found in both a target function and a trailing wrapper block (or if the edit overlaps structural boundaries), `identify_target_nodes` will return multiple structural node IDs. Because lock acquisition is all-or-nothing, the agent will accidentally claim locks on nodes it never intended to edit. This is safe, but increases the lock collision surface area at scale.

**2. The Asymmetric Invalidation Cascade**

Your graph update logic inside `update_file` handles structural deletions correctly:

```python
if prior_node_ids - new_node_ids:
    dependent_files = [r[0] for r in conn.execute(
        "SELECT from_file FROM edges WHERE to_file = ?", (abs_path,)
    ).fetchall()]
    for dep in dependent_files:
        if dep != abs_path and Path(dep).is_file():
            index_file(dep, project_root, conn)
```

However, if Agent A *adds* an entirely new import statement to an existing file during an allowed edit, `index_file` runs a clean purge-and-reinsert:

```python
conn.execute("DELETE FROM edges WHERE from_file = ?", (abs_path,))
conn.executemany("INSERT OR IGNORE INTO edges (from_file, to_file) VALUES (?, ?)", edges)
```

The new dependency edge lands instantly in the database. But if Agent B is currently holding a subgraph lock that now includes this newly connected edge as a 1-hop forward target, Agent B's PostToolUse will immediately fail its Merkle check and reject Agent B's edit, even though Agent B didn't touch the file Agent A just modified.

---

## License

GPLv3. Commercial products may ship the coordinator as an unmodified subprocess. Forks that add proprietary features must open source those modifications. This is intentional.

---

© [ieepirzy](https://github.com/ieepirzy) 2026
