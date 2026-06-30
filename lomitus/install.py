from __future__ import annotations

import json
import shutil
from pathlib import Path


def _is_lomitus_entry(entry: dict) -> bool:
    for h in entry.get("hooks", []):
        if "lomitus" in h.get("command", ""):
            return True
    return False


def cmd_init(args: list[str]) -> None:
    use_global = "--global" in args
    settings_path = (
        Path.home() / ".claude" / "settings.json"
        if use_global
        else Path.cwd() / ".claude" / "settings.json"
    )
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    config: dict = {}
    if settings_path.exists():
        with open(settings_path) as f:
            config = json.load(f)

    hooks = config.setdefault("hooks", {})
    exe = shutil.which("lomitus") or "lomitus"

    entries = {
        "PreToolUse": {
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{"type": "command", "command": f"{exe} hook"}],
        },
        "PostToolUse": {
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{"type": "command", "command": f"{exe} hook --release"}],
        },
        "PostToolUseFailure": {
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{"type": "command", "command": f"{exe} hook --failure"}],
        },
        "SessionStart": {
            "hooks": [{"type": "command", "command": f"{exe} hook --session-start"}],
        },
        "SessionEnd": {
            "hooks": [{"type": "command", "command": f"{exe} hook --session-end"}],
        },
        "SubagentStart": {
            "hooks": [{"type": "command", "command": f"{exe} hook --subagent-start"}],
        },
        "SubagentStop": {
            "hooks": [{"type": "command", "command": f"{exe} hook --subagent-stop"}],
        },
        "WorktreeCreate": {
            "hooks": [{"type": "command", "command": f"{exe} hook --worktree-create-hook"}],
        },
        "WorktreeRemove": {
            "hooks": [{"type": "command", "command": f"{exe} hook --worktree-remove"}],
        },
        "FileChanged": {
            "hooks": [{"type": "command", "command": f"{exe} hook --file-changed"}],
        },
        "CwdChanged": {
            "hooks": [{"type": "command", "command": f"{exe} hook --cwd-changed"}],
        },
        "PreCompact": {
            "matcher": "manual|auto",
            "hooks": [{"type": "command", "command": f"{exe} hook --pre-compact"}],
        },
    }

    for event, entry in entries.items():
        event_hooks = hooks.setdefault(event, [])
        event_hooks[:] = [h for h in event_hooks if not _is_lomitus_entry(h)]
        event_hooks.append(entry)

    with open(settings_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    scope = "global" if use_global else "project"
    print(f"Lomitus hooks installed ({scope}): {settings_path}")


def cmd_uninstall(args: list[str]) -> None:
    use_global = "--global" in args
    settings_path = (
        Path.home() / ".claude" / "settings.json"
        if use_global
        else Path.cwd() / ".claude" / "settings.json"
    )
    if not settings_path.exists():
        print("No settings.json found, nothing to do.")
        return

    with open(settings_path) as f:
        config = json.load(f)

    hooks = config.get("hooks", {})
    for event in list(hooks.keys()):
        hooks[event] = [h for h in hooks[event] if not _is_lomitus_entry(h)]
        if not hooks[event]:
            del hooks[event]

    with open(settings_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"Lomitus hooks removed: {settings_path}")
