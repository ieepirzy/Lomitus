# CBS Coordinator — Design Document

*A multi-agent coding coordinator built on conflict-based search principles.*

---

> [!NOTE] This repository uses Claude as a comparison and a baseline agentic harness, however the only Claude-specific thing is the schema of the hooks. The concept of hooks exists for all agentic harnesses making this framework harness agnostic with minor revisions.

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

The coordinator is a deterministic service integrated into the agents harness. It lives next to Claude Code as a local Python script, invoked via Claude Code's PreToolUse and PostToolUse, and other hook mechanism. No LLM is in the coordination path — coordination is a graph algorithm problem, not a reasoning problem. Making the coordinator another AI would introduce nondeterminism exactly where you need guarantees.

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
    file_path      TEXT NOT NULL,      -- denormalised for bloom filter and batch release
    agent_id       TEXT NOT NULL,
    agent_type     TEXT,
    subgraph_hash  TEXT,               -- Merkle root at acquisition time
    subgraph_nodes TEXT,               -- JSON list of node_ids in the 1-edge subgraph
    acquired_at    TEXT DEFAULT (datetime('now'))
);
```

The `abs_path::__file__` sentinel is used when the Write tool targets a file with no existing structural nodes (empty or brand-new file), giving the first writer an exclusive file-level lock until the file has content.

### Dependency graph

Existing tooling handles most of the static graph construction:

- Python: `pydep`, `modulegraph`
- TypeScript/JavaScript: `madge`, `dependency-cruiser`

Graph invalidation is asymmetric, which matters for performance:

- **Adds** (new files, new imports): append and branch. The existing graph stays valid, new edges extend it. Cheap, common case.
- **Deletes**: force full recalculation. A node disappearing can orphan dependents or collapse a subgraph. Structurally destructive.

### Subgraph hash

**At PreToolUse (lock acquisition):** the coordinator crawls the dependency subgraph rooted at the target node, fetches the current content hashes from the global cache, computes a Merkle root over the 1-edge subgraph, and stores it alongside the lock row. This snapshot represents the state of the world at the moment the agent committed to its edit.

**At PostToolUse (lock release):** the same Merkle root is recomputed from the current global cache and compared against the stored value. If it has drifted — meaning something outside this agent mutated a dependency in the narrow window between snapshot and lock application — the agent's edit was built on stale assumptions. The coordinator blocks and explains, and the agent replans.

This is environment drift detection: not "what did this agent change" but "did the world this agent was reasoning from shift under it."

Note: this means that agents only claim relevant parts of the subgraph on the acting turn, which triggers the validation steps before being accepted.

Edits that add new methods or files will trigger graph extensions and crawling PostToolUse.

### Bloom filter

In large codebases, a dependency subgraph can contain thousands of nodes. Checking each against SQLite individually before acquiring a subgraph lock is wasteful. A bloom filter sits in front of the DB as a pre-check: nodes that are definitely not locked are skipped, only candidates hit SQLite. False positives just mean an unnecessary DB lookup, which is acceptable. False negatives are impossible by construction.

This is useful when the coordinator is checking if any dependencies have currently got locks on them.

For v0 with single-file locking, the bloom filter adds nothing — one primary key lookup is already optimal. It earns its place at subgraph scale.

### Lazy cache population

There is no upfront crawl of the codebase. Upfront crawling doesn't scale — new codebases start from nothing with a rapidly growing dep graph, and large codebases take too long to crawl naively. Instead, the cache is populated lazily by write events.

Crawl depth is not uniform — it depends on the hook type:

- **PreToolUse (enforcement):** 1 edge deep. Tight scope, fast, captures direct dependencies only. This is the depth used for lock acquisition and drift detection.
- **Passive warming hooks** (`FileChanged`, `CwdChanged`, non-editing tool use): 2-3 edges deep. Wide scope, populates the cache ahead of writes that haven't happened yet.

When an agent's PreToolUse fires, the coordinator checks the 1-edge subgraph against the cache and stores the snapshot. The cache stays current as a natural side effect of write hooks: if agent B touched a dependency, it went through PreToolUse, meaning the coordinator already crawled and cached that node. By the time agent A wants to edit something in that dependency chain, fresh hashes are already in the DB. No read hooks are needed — write events are sufficient.

A global state is kept consistent even when agents are working in different git worktrees by utilizing git commands. Each agent's worktree has its own filesystem view; the coordinator normalises all paths to canonical project-root-relative keys and uses git to read file state from the authoritative tree rather than from a specific worktree's working copy.

Exploratory tool use hooks are distinct from RW tool use hooks, this allows for different crawling depths based on the anticipated action.

An existing conceptual limitation is deeply nested dynamic imports ex. `importlib.import_module()` where the target is a variable. Static AST crawl cannot resolve these cases.

### Worktree state vs. global cache

> Qualitatively: the worktree is "what is going on in my world." The global cache is "what is actually real."

Each agent operates in its own git worktree, giving it an isolated filesystem view. The coordinator maintains a global SQLite cache as the single source of truth for subgraph hashes across all agents. These two layers serve distinct purposes and must not be conflated.

**Passive crawl triggers and full hook inventory**

Beyond write-time crawling, Claude Code exposes hooks that allow the coordinator to update the global cache before any tool use fires. The full set of hook events and their coordinator relevance:

| Hook | Coordinator use |
|------|----------------|
| `SessionStart` | Initialize coordinator DB for the session |
| `SubagentStart` | Register agent, record worktree assignment, begin tracking |
| `SubagentStop` | Deregister agent, release any remaining locks held by that agent |
| `WorktreeCreate` | Record worktree↔agent mapping, used for path normalization |
| `WorktreeRemove` | Clean up worktree mapping, trigger final lock release for that worktree |
| `FileChanged` | File mutated in a worktree — crawl subgraph from that file, update global cache |
| `CwdChanged` | Agent navigated to new directory — crawl 2-3 edges from new context, warm cache |
| `PostToolBatch` | After a parallel batch of tool calls resolves — validate batch as a unit before next model call, useful if an agent fires multiple edits in parallel |
| `PreToolUse` | **Primary enforcement point** — lock acquisition and drift check before any write |
| `PostToolUse` | **Primary release point** — cache update and lock release after write completes |
| `PostToolUseFailure` | Edit failed — release lock without updating cache, leave previous hash intact |
| `PreCompact` | Snapshot coordinator state before context compaction in case of post-compact replanning |
| `SessionEnd` | Final cleanup, release all remaining locks, persist cache to disk |

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

The coordinator should classify dependency nodes by type:

- **Structural nodes** (method definitions, class definitions): full lock enforcement, no concurrent writes.
- **Benign nodes** (import statements, module-level variable declarations): collision allowed, concurrent writes permitted, hash still tracked for drift detection.

This avoids false blocks on trivially non-conflicting edits while maintaining full protection on the nodes where semantic conflicts actually occur. The classification is statically determinable from the AST node type — no LLM needed.

> [!IMPORTANT] Benign nodes do not need to be locked as long as they are not the target of an edit
> edits targeting benign nodes directly will still be blocked when a lock is shared.

### Cold cache rule

PreToolUse on any write, if the node is not in cache: crawl now, store as baseline, then proceed with lock acquisition. No exceptions. If nodes are already crawled, the baseline is updated from global cache.

A cold cache on a write means no baseline hash exists, so drift cannot be detected. The on-demand crawl *is* the baseline establishment. Cold cache is never "skip" — it's always "crawl first."

The cost is acceptable: it happens once per node per session, and only for nodes that actually get touched. After the first crawl the node is warm and subsequent checks are hash comparisons only.

### Dual-use AST cache

The same parse and crawl serves both subgraph hash comparison (drift detection) and I/O contract validation. The expensive part — parsing — happens once per node per session. Two consumers, one cache. Allowed writes, that is, writes that pass the pre and post tool use hooks for the editing agent, which ensures consistency (see below), then overwrite the previous hash. 

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
3. Execute the method with that input, snapshot the output *structure* — shape and types, not exact values
4. Store the snapshot alongside the lock in SQLite

**At write time:**
1. Re-execute the method with the same input
2. Validate the new output is a *superset* of the snapshotted structure

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

The cascade task list is ordered by dependency graph topology — outermost dependents
first, so each node is updated against an already-resolved contract rather than a
mix of old and new. The coordinator extends the subgraph lock to cover the entire
cascade set for the duration of the refactor. Any other agent attempting to acquire
a lock on a node within the cascade subgraph is blocked with the reason:

    Cascade refactor in progress on mutate_data() by agent .
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

The solution maps directly to classical DB concurrency control — wound-wait or wait-die — because this is the same problem class. Before acquiring a lock, the agent broadcasts its intent. The coordinator checks the current lock graph for cycles. On cycle detected, rather than waiting, the coordinator negotiates a swap: agents trade their locked resources and proceed from the other's position. This is O(n) space to track agent intent, O(n²) worst case for cycle detection, but n (number of concurrent agents) is small in practice.

### Crash recovery — TTL revert

If an agent crashes mid-edit, its PostToolUse hook never fires. The lock is never released. Without recovery, the locked files are permanently inaccessible to other agents for the session.

On TTL expiry — where TTL scales with method line count as a heuristic for expected edit duration — the coordinator does not merely release the lock. It first reverts the file to the pre-lock snapshot (already stored for the I/O contract check), then releases. A crashed agent leaves zero damage. The codebase is in a known-good state.

The system makes no runtime guarantees of correctness. Revert-on-TTL is the correct conservative default: deterministic, safe, and leaves a clear audit trail.

## Worktree Merge Guarantee & Merkle Optimization

When a blocked agent is given the green light to re-attempt an edit after pulling
in changes from main, the resulting `git merge` into its worktree is guaranteed to
be clean. This is not an optimistic assumption — it is a logical consequence of the
hash-based staleness detection. Because CBS blocks any edit applied against an
outdated dependency snapshot, by the time an agent holds a subgraph lock and
proceeds to write, its view of the dependency graph is current by definition.
Sequential writes to any given subgraph are enforced by the lock, so git has no
grounds for a textual conflict on locked nodes. The only conceivable conflicts would
be cosmetic and outside the dependency graph entirely (whitespace, line endings),
which are not CBS concerns.

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
- v1: MSc or large project tier for a pragmatic Python + TS implementation. The dep graph integration and subgraph hashing are the bulk of the work.
- v2: Approaches PhD territory only if targeting formal correctness guarantees across arbitrary languages. For pragmatic Python + TS with open-world validation and heuristic TTL, it stays at MSc or comparable level.

The integration of runtime agent coordination, partial AST rollback, live dep graph, and I/O contract validation from real call sites is novel relative to the current ecosystem. No existing multi-agent coding framework implements this combination.

---

## License

GPLv3. Commercial products may ship the coordinator as an unmodified subprocess. Forks that add proprietary features must open source those modifications. This is intentional.

---

## OPEN TODOs (16.05.2026)

The Strategic Path forward: Replacing the Git Transport Layer
Since you've proven that your SQLite dependency tracker perfectly gates execution, the Git CLI is acting as a redundant, slow database layer.

[Agent A writes node] ──► PostToolUse ──► Update SQLite Cache ──► Write to Main Repo FS
                                                                         │
                                       ┌─────────────────────────────────┘
                                       ▼
[Agent B PreToolUse]  ──► Merkle Mismatch Detected ──► Python shutil.copy2() (No Git CLI)
                                                                         │
                                       ┌─────────────────────────────────┘
                                       ▼
                               Near 0ms Latency

Instead of running _auto_commit_and_push via process forks, your coordinator can copy the validated structural node text changes directly into the canonical main repo directory via Python filesystem operations, updating your nodes table instantly.

You then defer Git compilation entirely to SessionEnd

## IDEAS FOR I/O CONTRACTS

Optimizing contract.py without introducing an LLM
To elevate your execution-based approach to a highly reliable layer, you can extend your AST parse path to implement Automated Context Injections:

A. Deep Token Type Mocking
When a type hint isn't a scalar (like int or str), parse the type string to see if it belongs to a known dataclass or structural model within your nodes database cache. Since you've already indexed the codebase into SQLite, if a function takes a user: User, your coordinator can query the nodes table for User, look up its fields, and automatically instantiate a mock object payload for the subprocess script instead of giving up with None.

B. Structural Isolation
Instead of just blacklisting calls inside the function body, use ast.NodeTransformer within your parser to strip out or stub any top-level module statements that aren't class or function definitions before writing the dynamic file to the snapshot interpreter. This ensures that simply importing the target file never triggers an accidental external initialization loop.

## Edge cases in dep_graph.py:

When mapping an explicit AST to a relational database for deterministic runtime validation, several subtle edge-case interactions become apparent:

1. The Multi-Match Target Collateral (The "Stretching Node" Trap)
Look closely at identify_target_nodes for an Edit or MultiEdit call:

```python
edit_line_start = source[:offset].count("\n") + 1
edit_line_end   = edit_line_start + old_string.count("\n")
for node_id, line_start, line_end in rows:
    if line_start <= edit_line_end and line_end >= edit_line_start:
        matched.add(node_id)
```

If an agent is editing a large class or function, and they replace an old_string that happens to structurally match code found in both a target function and a trailing wrapper block (or if the edit overlaps structural boundaries, like updating an un-scoped variable at line 40 that bleeds into line 41), identify_target_nodes will return multiple structural node IDs.

Because lock acquisition is all-or-nothing:

```python
with conn:
    for nid in structural_targets:
        conn.execute("INSERT INTO locks ...")
```

If an edit spans across boundaries or triggers a multi-match, the agent will accidentally claim locks on nodes it never intended to edit. This is safe, but it increases the lock collision surface area at scale.

2. The Asymmetric Invalidation Cascade
Your graph update logic inside update_file handles structural deletions flawlessly:

```python
if prior_node_ids - new_node_ids:
    dependent_files = [r[0] for r in conn.execute(
        "SELECT from_file FROM edges WHERE to_file = ?", (abs_path,)
    ).fetchall()]
    for dep in dependent_files:
        if dep != abs_path and Path(dep).is_file():
            index_file(dep, project_root, conn)
```
If a method is removed or renamed, you re-index its dependent files to see if their imports broke or changed.

However, look at what happens if Agent A adds an entirely new import statement to an existing file during an allowed edit. index_file runs a clean purge-and-reinsert:

```python
conn.execute("DELETE FROM edges WHERE from_file = ?", (abs_path,))
# ...
conn.executemany("INSERT OR IGNORE INTO edges (from_file, to_file) VALUES (?, ?)", edges)
```

The new dependency edge lands instantly in the database. But if Agent B is currently holding a subgraph lock that now includes this newly connected edge as a 1-hop forward target, Agent B's PostToolBatch or PostToolUse will immediately fail its Merkle check, detect an unexpected environmental drift, and reject Agent B's edit, even though Agent B didn't touch the file Agent A just modified.

## Resolving var placeholder limitation

Resolving the var Placeholder Limitation
In contract.py, falling back to a dummy placeholder "var" (which maps to None at execution time) for unresolvable complex objects is a significant roadblock for real-world codebases like Django, FastAPI, or SQLAlchemy, where almost everything inherits from custom models or handles structured contexts.

Since dep_graph.py gives you an incredibly accurate, queryable map of every structural node in the codebase, you can pass this information directly to contract.py to solve the object resolution deficit.

Instead of abandoning typing or using simple scalar defaults, you can leverage your parsed database entries to build Synthetic Structural Mock Generics:

```python
# Conceptual extension inside contract.py using your nodes table cache
def mock_type_from_db(annotation_name: str, conn: sqlite3.Connection) -> Any:
    # Check if this annotation name matches a ClassDef we have indexed
    row = conn.execute(
        "SELECT node_id, file_path FROM nodes WHERE name = ? AND kind = 'structural' LIMIT 1",
        (annotation_name,)
    ).fetchone()
    
    if row:
        # We found the class definition! We can construct a lightweight 
        # dynamic Python Mock object that mirrors its expected attributes
        class SyntheticMock:
            def __init__(self):
                # Dynamically inject defaults or simple mock returns
                pass
            def __getattr__(self, item):
                return None # Graceful attribute fallback to prevent AttributeError
        return SyntheticMock()
        
    return None # Fallback to standard None if completely unindexed
```
Thank you for sharing dep_graph.py—this provides the exact technical anchor to see how the mathematical guarantees are built. Seeing the concrete implementation of both the static analysis layer and the indexing mechanism clarifies the entire graph life cycle.

My previous analysis of _subgraph_stale missed a crucial architecture detail that your codebase elegant addresses: compute_merkle_root does preserve the topology perfectly!

Python
leaf = hashlib.sha256(b"".join(r[0].encode() for r in rows)).hexdigest()
leaf_hashes.append(leaf)
...
return hashlib.sha256(b"".join(h.encode() for h in leaf_hashes)).hexdigest()
By grouping individual content_hash strings by file path, sorting them by line position, creating a file-level leaf hash, and then hashing the sorted leaf collection, you have effectively built a deterministic two-tiered hierarchical Merkel Tree.

Technical Deep-Dive & Edge Cases in dep_graph.py
When mapping an explicit AST to a relational database for deterministic runtime validation, several subtle edge-case interactions become apparent:

1. The Multi-Match Target Collateral (The "Stretching Node" Trap)
Look closely at identify_target_nodes for an Edit or MultiEdit call:

Python
edit_line_start = source[:offset].count("\n") + 1
edit_line_end   = edit_line_start + old_string.count("\n")
for node_id, line_start, line_end in rows:
    if line_start <= edit_line_end and line_end >= edit_line_start:
        matched.add(node_id)
If an agent is editing a large class or function, and they replace an old_string that happens to structurally match code found in both a target function and a trailing wrapper block (or if the edit overlaps structural boundaries, like updating an un-scoped variable at line 40 that bleeds into line 41), identify_target_nodes will return multiple structural node IDs.

Because lock acquisition is all-or-nothing:

Python
with conn:
    for nid in structural_targets:
        conn.execute("INSERT INTO locks ...")
If an edit spans across boundaries or triggers a multi-match, the agent will accidentally claim locks on nodes it never intended to edit. This is safe, but it increases the lock collision surface area at scale.

2. The Asymmetric Invalidation Cascade
Your graph update logic inside update_file handles structural deletions flawlessly:

Python
if prior_node_ids - new_node_ids:
    dependent_files = [r[0] for r in conn.execute(
        "SELECT from_file FROM edges WHERE to_file = ?", (abs_path,)
    ).fetchall()]
    for dep in dependent_files:
        if dep != abs_path and Path(dep).is_file():
            index_file(dep, project_root, conn)
If a method is removed or renamed, you re-index its dependent files to see if their imports broke or changed.

However, look at what happens if Agent A adds an entirely new import statement to an existing file during an allowed edit. index_file runs a clean purge-and-reinsert:

Python
conn.execute("DELETE FROM edges WHERE from_file = ?", (abs_path,))
# ...
conn.executemany("INSERT OR IGNORE INTO edges (from_file, to_file) VALUES (?, ?)", edges)
The new dependency edge lands instantly in the database. But if Agent B is currently holding a subgraph lock that now includes this newly connected edge as a 1-hop forward target, Agent B's PostToolBatch or PostToolUse will immediately fail its Merkle check, detect an unexpected environmental drift, and reject Agent B's edit, even though Agent B didn't touch the file Agent A just modified.

Resolving the var Placeholder Limitation
In contract.py, falling back to a dummy placeholder "var" (which maps to None at execution time) for unresolvable complex objects is a significant roadblock for real-world codebases like Django, FastAPI, or SQLAlchemy, where almost everything inherits from custom models or handles structured contexts.

Since dep_graph.py gives you an incredibly accurate, queryable map of every structural node in the codebase, you can pass this information directly to contract.py to solve the object resolution deficit.

Instead of abandoning typing or using simple scalar defaults, you can leverage your parsed database entries to build Synthetic Structural Mock Generics:

Python
# Conceptual extension inside contract.py using your nodes table cache
def mock_type_from_db(annotation_name: str, conn: sqlite3.Connection) -> Any:
    # Check if this annotation name matches a ClassDef we have indexed
    row = conn.execute(
        "SELECT node_id, file_path FROM nodes WHERE name = ? AND kind = 'structural' LIMIT 1",
        (annotation_name,)
    ).fetchone()
    
    if row:
        # We found the class definition! We can construct a lightweight 
        # dynamic Python Mock object that mirrors its expected attributes
        class SyntheticMock:
            def __init__(self):
                # Dynamically inject defaults or simple mock returns
                pass
            def __getattr__(self, item):
                return None # Graceful attribute fallback to prevent AttributeError
        return SyntheticMock()
        
    return None # Fallback to standard None if completely unindexed
By returning a basic object wrapped in a graceful __getattr__ safety switch instead of a raw standard NoneType, your execution runner will glide right past missing attribute assignments (request.user.id will cleanly evaluate to None instead of crashing with a fatal AttributeError), allowing you to extract structural I/O snapshots from highly complex code bases.

## Eliminating cache population latency

Your lazy caching methodology is clean:

PreToolUse enforces a precise 1-edge-deep scan.

Passive hooks (FileChanged, CwdChanged) warm the graph 2–3 edges out in the background.

At a massive scale of 100 to 500 parallel agents, this architecture can completely decouple from disk reads by utilizing an In-Memory Graph State Cache.

Since dep_graph.py writes directly to a centralized WAL-mode SQLite database, you can load the edges table into a fast, thread-safe in-memory graph structure (like a dict of sets) at SessionStart. Your BFS traversals inside crawl_subgraph can run at pure memory speeds without executing any SQL relational joins, reserving SQLite exclusively for holding long-lived transaction locks and content hashes.

## Other possible edge cases:

In dep_graph.py, when a file or node is structurally mutated or deleted, you proactively re-index its reverse dependents to ensure that the global edge list remains pristine:

```python
if prior_node_ids - new_node_ids:
    dependent_files = [r[0] for r in conn.execute("SELECT from_file FROM edges WHERE to_file = ?", (abs_path,)).fetchall()]
    for dep in dependent_files:
        index_file(dep, project_root, conn)
```

The Edge Case: If Agent A removes a function signature, it triggers this recursive re-indexing across all files that import it. If Agent B is simultaneously attempting to acquire a lock in one of those dependent files, Agent B's PreToolUse loop might hit a lock contention issue on the SQLite database while index_file(dep) is writing its new nodes.

To harden this for 500 agents, wrap these cascading dependent re-indexes inside a deferred background task or queue, rather than running them synchronously inside the active PostToolUse release window of the writing agent.

---

*Design developed May 2026.*
© Ilari Pirkkalainen [ieepirzy](https://github.com/ieepirzy) 2026

**AUTHORIZED EYES ONLY DOCUMENT NOT MEANT FOR PUBLIC DISCLOSURE OR CIRCULATION UNTIL INTENTIONAL PUBLIC RELEASE**
