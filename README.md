# Lomitus

CBS-style multi-agent coordinator for Claude Code.

> [!NOTE]
> **Language scope:** Currently Python-only. `_is_python()` in `handle_pretool()` passes non-Python files through uncoordinated. TypeScript/JavaScript dep graph support (madge, dependency-cruiser) is a planned extension.
>
> The coordinator is harness-agnostic at the hook schema level — Claude Code is the reference integration but the concept applies to any agent harness that exposes PreToolUse / PostToolUse lifecycle hooks.

Lomitus is a deterministic coordination layer for parallel agent sessions. It treats concurrent agents editing a shared codebase as a Multi-Agent Path Finding (MAPF) problem on a dependency graph and resolves conflicts via Conflict-Based Search (CBS) — no LLM in the coordination path.

## How it works

Three versioned capability layers, all implemented:

| Version | Capability | Status |
|---------|------------|--------|
| v0 | File-level lock, PreToolUse hook blocks conflicting writes | Implemented |
| v1 | Method-level subgraph lock, AST dep graph, Merkle drift detection | Implemented |
| v2 | I/O contract snapshot, open-world superset validation, cascade flow, TTL revert | Implemented |

See [docs/design.md](docs/design.md) for the full architecture and design rationale.

Lomitus is alpha software. Its coordination guarantees apply only to Python files and
the hook-managed edit window. I/O contract snapshots are best-effort: unsupported
arguments or execution failures are recorded and skip contract comparison for that
node. The snapshot rewriter removes initialization from the target module, but imports
and the selected function still execute; it is process isolation, not a security sandbox.

Current scalability and correctness limitations—including conservative
multi-node targeting and effect-classification blind spots—are listed in the
[design document](docs/design.md#current-limitations-and-roadmap). Classical lock-wait
deadlocks are detected and broken via wound-wait (see
[Deadlock resolution — preclaim consensus](docs/design.md#deadlock-resolution--preclaim-consensus)).

## Install

```bash
pip install lomitus
```

Register hooks into your project's `.claude/settings.json`:

```bash
lomitus init
```

Or globally across all projects:

```bash
lomitus init --global
```

To remove:

```bash
lomitus uninstall
```

## Requirements

- Python 3.11+
- [`pygit2`](https://www.pygit2.org/) (installed automatically)

## CLI reference

```
lomitus init [--global]       Install hooks into .claude/settings.json
lomitus uninstall [--global]  Remove lomitus hooks from settings.json
lomitus hook [FLAGS]          Hook entrypoint (called by Claude Code)
```

Hook flags (passed by Claude Code automatically after `lomitus init`):

| Flag | Hook event |
|------|------------|
| *(none)* | `PreToolUse` — lock acquisition, drift check, contract snapshot |
| `--release` | `PostToolUse` — drift recheck, cache update, lock release |
| `--failure` | `PostToolUseFailure` — silent lock release, no cache update |
| `--session-start` | `SessionStart` — init DB schema, optional project crawl |
| `--session-end` | `SessionEnd` — cleanup, squash micro-commits, release all locks |
| `--subagent-start` | `SubagentStart` — register agent identity and priority |
| `--subagent-stop` | `SubagentStop` — deregister agent, release its locks |
| `--worktree-create-hook` | `WorktreeCreate` — register worktree↔agent mapping |
| `--worktree-remove` | `WorktreeRemove` — remove mapping, release agent locks |
| `--file-changed` | `FileChanged` — re-index changed file, update cache |
| `--cwd-changed` | `CwdChanged` — warm cache from new working directory |
| `--pre-compact` | `PreCompact` — WAL checkpoint before context compaction |

Manual-only flags:

| Flag | Description |
|------|-------------|
| `--crawl-project` | Recursively index all `.py` files under project root |
| `--revert` | Revert all files in a cascade set to pre-lock snapshots |
| `--worktree-create` | Create a git worktree + branch for an agent |
| `--worktree-push` | Merge agent branch → main, re-index changed files |
| `--worktree-pull` | Rebase agent worktree onto latest main |
| `--worktree-status` | Report live status of all registered worktrees (JSON stdout) |
| `--worktree-log` | Query worktree event log (JSON stdout) |

## Watchdog

Start the watchdog alongside a session for TTL-based lock revert on agent crashes:

```bash
python -m lomitus.watchdog                   # default: poll every 10s
python -m lomitus.watchdog --poll-interval 5
```

The watchdog is standalone (stdlib only) and reads the same `coordinator.db` the hook invocations write.

## License

GPLv3. Commercial products may ship the coordinator as an unmodified subprocess. Forks that add proprietary features must open-source those modifications.

---

© [ieepirzy](https://github.com/ieepirzy) 2026
