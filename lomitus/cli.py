from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]

    if not args:
        _usage()
        sys.exit(1)

    cmd = args[0]

    if cmd == "hook":
        sys.argv = [sys.argv[0]] + args[1:]
        from lomitus.coordinator import main as coord_main
        coord_main()

    elif cmd == "init":
        from lomitus.install import cmd_init
        cmd_init(args[1:])

    elif cmd == "uninstall":
        from lomitus.install import cmd_uninstall
        cmd_uninstall(args[1:])

    else:
        _usage()
        sys.exit(1)


def _usage() -> None:
    print(
        "lomitus — CBS-style multi-agent coordinator for Claude Code\n"
        "\n"
        "Commands:\n"
        "  lomitus init [--global]       Install hooks into .claude/settings.json\n"
        "  lomitus uninstall [--global]  Remove lomitus hooks from settings.json\n"
        "  lomitus hook [FLAGS]          Hook entrypoint (called by Claude Code)\n"
        "    --release                     PostToolUse\n"
        "    --failure                     PostToolUseFailure\n"
        "    --session-start/end           Session lifecycle\n"
        "    --subagent-start/stop         Subagent lifecycle\n"
        "    --worktree-create/push/pull/status/log/remove\n"
        "    --crawl-project               Index all .py files in project\n"
        "    --file-changed / --cwd-changed\n"
        "    --pre-compact\n"
    )
