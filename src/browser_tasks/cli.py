from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import RoutingInput
from .routing import assess
from .task_store import TaskStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="browser-tasks")
    root.add_argument("--root", type=Path, default=Path.cwd())
    commands = root.add_subparsers(dest="command", required=True)
    task = commands.add_parser("task")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    init = task_commands.add_parser("init")
    init.add_argument("task_id")
    init.add_argument("--goal", required=True)
    status = task_commands.add_parser("status")
    status.add_argument("task_id")
    route = commands.add_parser("route")
    route.add_argument("--architecture", action="store_true")
    route.add_argument("--steps", type=int, default=0)
    route.add_argument("--files", type=int, default=0)
    route.add_argument("--safety-review", action="store_true")
    route.add_argument("--disclosure-authorized", action="store_true")
    route.add_argument("--provider-unavailable", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "task":
        store = TaskStore(args.root)
        task = store.create(args.task_id, args.goal) if args.task_command == "init" else store.load(args.task_id)
        print(json.dumps(task.to_dict(), indent=2, sort_keys=True))
        return 0
    decision = assess(RoutingInput(
        architecture=args.architecture,
        dependent_steps=args.steps,
        relevant_files=args.files,
        safety_review=args.safety_review,
        disclosure_authorized=args.disclosure_authorized,
        provider_available=not args.provider_unavailable,
    ))
    print(json.dumps(decision.__dict__, indent=2, sort_keys=True))
    return 0
