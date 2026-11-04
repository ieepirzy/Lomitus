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

## Library API: dependency graph for CI

`lomitus.impact` exposes the same graph without the hook coordinator, for
test-impact selection and result caching. Run it from the project root, which
may be a subdirectory of the repository:

```python
from lomitus import impact

affected = impact.affected_files(".", "origin/main", "HEAD")  # project-relative paths
graph = impact.index_project(".")                             # in-memory SQLite, head graph
nodes = impact.changed_nodes(".", "origin/main", "HEAD")      # which functions changed, deletes, renames, reorders
effects = impact.node_effects(graph)                          # (abs file, qualname) -> external/structural/benign
key = impact.closure_hash(graph, "tests/test_api.py", ".", extra=[lockfile_digest])  # result-cache key
```

- `affected_files` takes the changed, added and renamed-to files and their
  importers on the head graph, and the importers of **deleted files and renamed
  files' old paths on the base revision's graph** (`importers_at`): a test that
  still imports a module the change deleted or moved is selected. If you build
  selection from `reverse_closure` on the head graph yourself, treat deleted and
  renamed `.py` files as mandatory full-run triggers.
- `changed_nodes` reports file-level `deleted`, `renamed` and `unparseable`
  entries (label `<file>`) and a `reordered` entry (label `<order>`) when
  top-level statements moved: import-time order matters.
- `changed_nodes`, `file_digest` and `closure_hash` hash the normalized AST by
  default (`hash_mode="ast"`), so formatting- and comment-only edits change
  nothing. The coordinator keeps hashing source text (`"source"`), since an agent
  reasons over text.
- `closure_hash` covers **code only**: the test file, the `conftest.py` files
  pytest loads for it, the modules their literal `pytest_plugins` name, and the
  forward import closure of all of these. It includes the Python version by
  default, because `ast.dump` output differs between Python versions, so a key is
  only comparable within one version. Data files, fixtures, the lockfile, plugins
  enabled outside the project and the environment are not covered: pass their
  digests in `extra`.
- Paths: `changed_files`, `changed_nodes`, `affected_files` and `importers_at`
  speak project-relative POSIX paths; the graph functions speak absolute paths.
  `to_absolute` raises on a path that doesn't exist under the project root
  (e.g. one that is repository-relative by mistake).
- Absolute imports resolve against import roots: `LOMITUS_IMPORT_ROOTS`
  (os.pathsep-separated) or `[tool.lomitus] import_roots` in `pyproject.toml`;
  otherwise detected (`src/`, setuptools/hatch/poetry package dirs, then the
  project root).
- `index_project` settles cross-file effect typing to a fixpoint, capped by
  `effect_passes` (default 10); hitting the cap warns and
  `index_stats(conn)["effects_converged"]` is `"false"`.
- The API answers graph questions only. Which changes force a full run (conftest,
  migrations, lockfiles, CI config, non-Python inputs), which tests always run,
  and a full run on the main branch as a safety net belong to the consumer.

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
