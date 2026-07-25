from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .authorization import summary_sha256
from .delegation import validate_disclosure, validate_request_policy, validate_response
from .models import (
    ActionClass,
    AuthorizationGrant,
    BrowserAction,
    DelegationRequest,
    DelegationResponse,
    DisclosureDecision,
    RoutingInput,
    RunState,
    TaskState,
)
from .policy import (
    ensure_external_tool_allowed,
    ensure_task_is_operable,
    requires_authorization,
)
from .routing import assess
from .scanner import scan_paths
from .task_store import (
    DEFAULT_LEASE_SECONDS,
    TaskStore,
)


EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_DENIED = 2
EXIT_CONFLICT = 3
EXIT_NOT_FOUND = 4
# Distinct from EXIT_DENIED so a caller can tell "the scan found something" from
# "the scan could not run"; conflating them let a broken scan read as clean.
EXIT_FINDINGS = 5

ACTIVE_RUN_STATE_CHOICES = tuple(
    item.value
    for item in RunState
    if item
    not in {
        RunState.SUCCEEDED,
        RunState.INTERRUPTED,
        RunState.FAILED,
        RunState.CANCELLED,
    }
)


def _add_action_contract_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--action-class", required=True, choices=tuple(
        item.value for item in ActionClass
    ))
    command.add_argument("--target", required=True)
    command.add_argument("--summary", required=True)
    command.add_argument(
        "--postcondition",
        action="append",
        default=[],
        metavar="JSON",
        help=(
            'observable check, e.g. \'{"type":"url_equals","value":"https://x"}\''
        ),
    )
    command.add_argument("--content-sha256")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="browser-tasks")
    root.add_argument("--root", type=Path, default=Path.cwd())
    commands = root.add_subparsers(dest="command", required=True)

    task = commands.add_parser("task")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    init = task_commands.add_parser("init")
    init.add_argument("task_id", metavar="SLUG")
    init.add_argument("--goal", required=True)
    init.add_argument("--title")
    init.add_argument("--idempotency-key")
    init.add_argument(
        "--delegation-policy", choices=("maximal", "off"), default="maximal"
    )
    init.add_argument("--browser-adapter", choices=("surf",), default="surf")
    init.add_argument(
        "--reasoning-effort", choices=("best", "high", "max"), default="best"
    )
    init.add_argument(
        "--deep-research", choices=("auto", "standard", "deep"), default="auto"
    )

    for name in ("status", "show", "deliverables", "doctor", "recover"):
        command = task_commands.add_parser(name)
        command.add_argument("task_id", metavar="SLUG")
        command.add_argument("--json", action="store_true")

    repair = task_commands.add_parser("repair")
    repair.add_argument("task_id", metavar="SLUG")
    repair.add_argument("--json", action="store_true")
    escape = repair.add_mutually_exclusive_group()
    escape.add_argument(
        "--adopt-visible",
        action="store_true",
        help="register the on-disk content of an unresolvable publish",
    )
    escape.add_argument(
        "--discard-journal",
        action="store_true",
        help="drop an unresolvable publish journal entry",
    )

    release = task_commands.add_parser("release-resources")
    release.add_argument("task_id", metavar="SLUG")

    prepared = task_commands.add_parser("delegation-prepared")
    prepared.add_argument("task_id", metavar="SLUG")
    prepared.add_argument("--request-id", required=True)
    prepared.add_argument("--purpose", required=True)
    prepared.add_argument("--context-sha256", required=True)
    prepared.add_argument("--destination", required=True)

    list_command = task_commands.add_parser("list")
    list_command.add_argument(
        "--state", choices=tuple(item.value for item in TaskState)
    )
    list_command.add_argument("--all", action="store_true")
    list_command.add_argument("--json", action="store_true")

    find_command = task_commands.add_parser("find")
    find_command.add_argument("query")
    find_command.add_argument("--json", action="store_true")

    audit = task_commands.add_parser("audit")
    audit.add_argument("task_id", metavar="SLUG")
    audit.add_argument("--jsonl", action="store_true")

    enforce = task_commands.add_parser("enforce-policy")
    enforce.add_argument("task_id", metavar="SLUG")

    for name in ("archive", "restore"):
        command = task_commands.add_parser(name)
        command.add_argument("task_id", metavar="SLUG")

    supersede = task_commands.add_parser("supersede")
    supersede.add_argument("task_id", metavar="SLUG")
    supersede.add_argument("--by", required=True, dest="replacement")

    alias = task_commands.add_parser("alias")
    alias.add_argument("task_id", metavar="SLUG")
    alias.add_argument("alias")

    outcome = task_commands.add_parser("outcome")
    outcome.add_argument("task_id", metavar="SLUG")
    outcome.add_argument("--summary", required=True)

    complete = task_commands.add_parser("complete")
    complete.add_argument("task_id", metavar="SLUG")
    complete.add_argument("--summary", required=True)

    cancel = task_commands.add_parser("cancel")
    cancel.add_argument("task_id", metavar="SLUG")
    cancel.add_argument("--reason", required=True)

    run_start = task_commands.add_parser("run-start")
    run_start.add_argument("task_id", metavar="SLUG")
    run_start.add_argument(
        "--state",
        choices=ACTIVE_RUN_STATE_CHOICES,
        default=RunState.EXECUTING.value,
    )
    run_start.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS
    )
    run_start.add_argument("--lease-owner")
    run_start.add_argument("--resumes-run-id")

    resume = task_commands.add_parser("resume")
    resume.add_argument("task_id", metavar="SLUG")
    resume.add_argument("--lease-owner", required=True)
    resume.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS
    )

    heartbeat = task_commands.add_parser("run-heartbeat")
    heartbeat.add_argument("task_id", metavar="SLUG")
    heartbeat.add_argument("run_id")
    heartbeat.add_argument("--lease-owner", required=True)
    heartbeat.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS
    )

    run_state = task_commands.add_parser("run-state")
    run_state.add_argument("task_id", metavar="SLUG")
    run_state.add_argument("run_id")
    run_state.add_argument("--state", choices=ACTIVE_RUN_STATE_CHOICES, required=True)
    run_state.add_argument("--lease-owner", required=True)

    abandon = task_commands.add_parser("run-abandon")
    abandon.add_argument("task_id", metavar="SLUG")
    abandon.add_argument("run_id")
    abandon.add_argument("--reason", required=True)

    run_finish = task_commands.add_parser("run-finish")
    run_finish.add_argument("task_id", metavar="SLUG")
    run_finish.add_argument("run_id")
    run_finish.add_argument("--lease-owner", required=True)
    run_finish.add_argument(
        "--state",
        choices=(
            RunState.SUCCEEDED.value,
            RunState.INTERRUPTED.value,
            RunState.FAILED.value,
            RunState.CANCELLED.value,
        ),
        required=True,
    )
    run_finish.add_argument("--checkpoint")
    run_finish.add_argument("--failure")
    run_finish.add_argument("--failure-class")
    recoverability = run_finish.add_mutually_exclusive_group()
    recoverability.add_argument(
        "--recoverable",
        action="store_const",
        const=True,
        dest="recoverable",
    )
    recoverability.add_argument(
        "--not-recoverable",
        action="store_const",
        const=False,
        dest="recoverable",
    )

    publish = task_commands.add_parser("publish")
    publish.add_argument("task_id", metavar="SLUG")
    publish.add_argument("source", type=Path)
    publish.add_argument("--name", required=True)
    publish.add_argument("--kind", required=True)
    publish.add_argument("--description", required=True)
    publish.add_argument("--reusable", action="store_true")
    publish.add_argument(
        "--verified",
        action="store_true",
        help="mark the deliverable as verified; required before completion",
    )
    publish.add_argument("--entrypoint")
    publish.add_argument("--run-id")

    artifact = task_commands.add_parser("artifact-store")
    artifact.add_argument("task_id", metavar="SLUG")
    artifact.add_argument("run_id")
    artifact.add_argument("source", type=Path)
    artifact.add_argument(
        "--category",
        choices=("evidence", "receipts", "delegations", "scratch"),
        required=True,
    )
    artifact.add_argument("--name", required=True)
    artifact.add_argument(
        "--lease-owner",
        help="required when the run is still active",
    )

    bind_adapter = task_commands.add_parser("bind-adapter")
    bind_adapter.add_argument("task_id", metavar="SLUG")
    bind_adapter.add_argument("--adapter", required=True)
    bind_adapter.add_argument(
        "--resource", action="append", default=[], required=True
    )

    grant = task_commands.add_parser("grant-install")
    grant.add_argument("task_id", metavar="SLUG")
    grant.add_argument("--grant-id", required=True)
    grant.add_argument("--expires-at", required=True)
    grant.add_argument("--max-uses", type=int, default=1)
    _add_action_contract_arguments(grant)

    intent = task_commands.add_parser("action-intent")
    intent.add_argument("task_id", metavar="SLUG")
    intent.add_argument("action_id")
    intent.add_argument("--run-id", required=True)
    intent.add_argument("--lease-owner", required=True)
    intent.add_argument(
        "--grant-id",
        help="installed grant to validate and consume; required for a "
        "consequential action class",
    )
    _add_action_contract_arguments(intent)

    result = task_commands.add_parser("action-result")
    result.add_argument("task_id", metavar="SLUG")
    result.add_argument("action_id")
    result.add_argument("--run-id", required=True)
    result.add_argument("--lease-owner", required=True)
    result.add_argument(
        "--outcome", required=True, choices=("verified", "ambiguous", "failed")
    )
    result.add_argument("--evidence-sha256")

    reconcile = task_commands.add_parser("action-reconcile")
    reconcile.add_argument("task_id", metavar="SLUG")
    reconcile.add_argument("action_id")
    outcome_group = reconcile.add_mutually_exclusive_group(required=True)
    outcome_group.add_argument("--verified", action="store_true")
    outcome_group.add_argument("--failed", action="store_true")
    reconcile.add_argument("--evidence-sha256", required=True)

    delegation = task_commands.add_parser("delegation-record")
    delegation.add_argument("task_id", metavar="SLUG")
    delegation.add_argument("run_id")
    delegation.add_argument("--receipt", type=Path, required=True)
    delegation.add_argument("--response", type=Path)
    delegation.add_argument(
        "--lease-owner",
        help="required when the recording run is still active",
    )

    route = commands.add_parser("route")
    route.add_argument("--architecture", action="store_true")
    route.add_argument("--steps", type=int, default=0)
    route.add_argument("--files", type=int, default=0)
    route.add_argument("--safety-review", action="store_true")
    route.add_argument("--ambiguity", action="store_true")
    route.add_argument("--web-research", action="store_true")
    route.add_argument("--current-information", action="store_true")
    route.add_argument("--cross-source-synthesis", action="store_true")
    route.add_argument("--regulatory", action="store_true")
    route.add_argument("--unfamiliar-domain", action="store_true")
    route.add_argument("--large-research-volume", action="store_true")
    route.add_argument("--deep-research", action="store_true")
    route.add_argument("--deep-research-unavailable", action="store_true")
    route.add_argument("--transport-unavailable", action="store_true")
    route.add_argument("--user-forced", action="store_true")
    route.add_argument("--deterministic", action="store_true")
    route.add_argument("--local-test-decides", action="store_true")
    route.add_argument("--live-observation-primary", action="store_true")
    route.add_argument("--repeated-failures", action="store_true")
    route.add_argument("--substantial-final-review", action="store_true")
    route.add_argument("--sensitive-broad-context", action="store_true")
    route.add_argument("--no-maximal-delegation", action="store_true")
    route.add_argument("--requested-provider", default="chatgpt-web")
    route.add_argument("--requested-transport", default="surf-ui")
    route.add_argument("--disclosure-authorized", action="store_true")
    route.add_argument("--provider-unavailable", action="store_true")

    guard = commands.add_parser("guard")
    guard.add_argument("task_id", metavar="SLUG")
    guard.add_argument(
        "--capability",
        choices=("browser", "reasoning", "research"),
        required=True,
    )
    guard.add_argument("--tool", required=True)

    scan = commands.add_parser("scan")
    scan.add_argument("--repo-root", type=Path, required=True)
    scan.add_argument(
        "--from-nul",
        type=Path,
        help="file holding NUL-separated repository-relative paths",
    )
    scan.add_argument("paths", nargs="*")
    scan.add_argument("--json", action="store_true")
    return root


def _print_task(task, *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(task.to_dict(), indent=2, sort_keys=True))
        return
    print(f"{task.task_id}\t{task.state.value}\t{task.goal}")


def _print_records(records, *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps([asdict(item) for item in records], indent=2, sort_keys=True))
        return
    for item in records:
        if hasattr(item, "goal"):
            print(f"{item.task_id}\t{item.state.value}\t{item.goal}")
        else:
            print(
                f"{item.name}\tr{item.revision}\t{item.kind}\t"
                f"{item.path}\t{item.description}"
            )


def _postconditions(values: list[str]) -> tuple[dict[str, str], ...]:
    parsed: list[dict[str, str]] = []
    for raw in values:
        try:
            requirement = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"postcondition is not valid JSON: {raw}") from error
        if not isinstance(requirement, dict):
            raise ValueError("each postcondition must be a JSON object")
        parsed.append({str(key): str(value) for key, value in requirement.items()})
    return tuple(parsed)


def _action_from_args(args: argparse.Namespace, action_id: str) -> BrowserAction:
    return BrowserAction(
        action_id,
        args.task_id,
        ActionClass(args.action_class),
        args.target,
        args.summary,
        _postconditions(args.postcondition),
        args.content_sha256,
    )


def _task_command(args: argparse.Namespace) -> int:
    store = TaskStore(args.root)
    command = args.task_command
    if command == "init":
        task = store.create(
            args.task_id,
            args.goal,
            title=args.title,
            idempotency_key=args.idempotency_key,
            delegation_policy=args.delegation_policy,
            allowed_browser_adapters=(args.browser_adapter,),
            reasoning_effort=args.reasoning_effort,
            deep_research_policy=args.deep_research,
        )
        _print_task(task)
        return EXIT_OK
    if command == "list":
        tasks = store.list_tasks(include_archived=args.all)
        if args.state:
            tasks = tuple(item for item in tasks if item.state.value == args.state)
        damaged = store.damaged_workspaces()
        if args.json:
            print(
                json.dumps(
                    {
                        "tasks": [item.to_dict() for item in tasks],
                        "damaged": list(damaged),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            _print_records(tasks)
            for item in damaged:
                print(f"DAMAGED\t{item['task_id']}\t{item['reason']}")
        return EXIT_OK
    if command == "find":
        _print_records(store.find(args.query), json_output=args.json)
        return EXIT_OK

    bound = store.bind(args.task_id)
    if command == "status":
        _print_task(bound.load(), json_output=args.json)
    elif command == "show":
        info = bound.info()
        if args.json:
            print(json.dumps(info, indent=2, sort_keys=True))
        else:
            _print_task(bound.load())
            print(f"path\t{bound.path()}")
            for item in bound.deliverables():
                print(f"deliverable\t{item.name}\t{item.path}")
    elif command == "deliverables":
        _print_records(bound.deliverables(), json_output=args.json)
    elif command == "audit":
        events = bound.audit_events()
        if args.jsonl:
            for event in events:
                print(json.dumps(event, sort_keys=True))
        else:
            for event in events:
                print(
                    f"{event['sequence']}\t{event['timestamp']}\t"
                    f"{event['event_type']}"
                )
    elif command == "doctor":
        issues = bound.doctor()
        if args.json:
            print(json.dumps({"task_id": args.task_id, "issues": issues}, indent=2))
        elif issues:
            for issue in issues:
                print(f"ERROR\t{issue}")
        else:
            print(f"{args.task_id}\tOK")
        # The exit code must not depend on the output format: a wrapper that
        # asks for JSON is exactly the caller that gates on the status.
        return EXIT_DENIED if issues else EXIT_OK
    elif command == "recover":
        recovered = bound.recover_expired_runs()
        if args.json:
            print(json.dumps({"task_id": args.task_id, "runs": recovered}, indent=2))
        else:
            print("\n".join(recovered) if recovered else "no expired runs")
    elif command == "repair":
        outcome = bound.repair(
            adopt_visible=args.adopt_visible,
            discard_journal=args.discard_journal,
        )
        if args.json:
            print(json.dumps({"task_id": args.task_id, **outcome}, indent=2))
        else:
            for action in outcome["actions"]:
                print(f"REPAIRED\t{action}")
            for diagnostic in outcome["diagnostics"]:
                print(f"UNRESOLVED\t{diagnostic}")
            if not outcome["actions"] and not outcome["diagnostics"]:
                print("nothing to repair")
        # An unresolved state is not a successful repair, whatever the output
        # format, so automation gating on the status sees the difference.
        return EXIT_DENIED if outcome["diagnostics"] else EXIT_OK
    elif command == "release-resources":
        released = bound.release_browser_resources()
        print("\n".join(released) if released else "no claimed resources")
    elif command == "delegation-prepared":
        bound.append_event(
            "delegation.prepared",
            {
                "request_id": args.request_id,
                "purpose": args.purpose,
                "context_sha256": args.context_sha256,
                "destination": args.destination,
                "provider": "chatgpt-web",
                "transport": "surf-ui",
            },
        )
        print(f"{args.task_id}\tdelegation prepared\t{args.request_id}")
    elif command == "enforce-policy":
        _print_task(bound.enforce_delegate_first_policy())
    elif command == "archive":
        bound.archive()
        print(f"{args.task_id}\tarchived")
    elif command == "restore":
        bound.restore()
        print(f"{args.task_id}\trestored")
    elif command == "supersede":
        _print_task(bound.supersede(args.replacement))
    elif command == "alias":
        bound.add_alias(args.alias)
        print(f"{args.task_id}\talias\t{args.alias}")
    elif command == "outcome":
        bound.set_outcome(args.summary)
        print(f"{args.task_id}\toutcome updated")
    elif command == "complete":
        _print_task(bound.complete(args.summary))
    elif command == "cancel":
        _print_task(bound.cancel(args.reason))
    elif command == "run-start":
        run = bound.start_run(
            RunState(args.state),
            lease_owner=args.lease_owner,
            lease_seconds=args.lease_seconds,
            resumes_run_id=args.resumes_run_id,
        )
        print(json.dumps(asdict(run), indent=2, sort_keys=True))
    elif command == "resume":
        run = bound.resume(
            lease_owner=args.lease_owner,
            lease_seconds=args.lease_seconds,
        )
        print(json.dumps(asdict(run), indent=2, sort_keys=True))
    elif command == "run-heartbeat":
        run = bound.heartbeat(
            args.run_id,
            lease_owner=args.lease_owner,
            lease_seconds=args.lease_seconds,
        )
        print(json.dumps(asdict(run), indent=2, sort_keys=True))
    elif command == "run-state":
        run = bound.set_run_state(
            args.run_id,
            RunState(args.state),
            lease_owner=args.lease_owner,
        )
        print(json.dumps(asdict(run), indent=2, sort_keys=True))
    elif command == "run-abandon":
        run = bound.abandon_run(args.run_id, args.reason)
        print(json.dumps(asdict(run), indent=2, sort_keys=True))
    elif command == "run-finish":
        run = bound.finish_run(
            args.run_id,
            RunState(args.state),
            lease_owner=args.lease_owner,
            checkpoint=args.checkpoint,
            failure=args.failure,
            failure_class=args.failure_class,
            recoverable=args.recoverable,
        )
        print(json.dumps(asdict(run), indent=2, sort_keys=True))
    elif command == "publish":
        deliverable = bound.publish_deliverable(
            args.source,
            args.name,
            kind=args.kind,
            description=args.description,
            reusable=args.reusable,
            verified=args.verified,
            entrypoint=args.entrypoint,
            produced_by_run=args.run_id,
        )
        print(json.dumps(asdict(deliverable), indent=2, sort_keys=True))
    elif command == "artifact-store":
        stored = bound.store_run_artifact(
            args.run_id,
            args.category,
            args.source,
            args.name,
            lease_owner=args.lease_owner,
        )
        print(json.dumps(stored, indent=2, sort_keys=True))
    elif command == "bind-adapter":
        _print_task(bound.bind_adapter(args.adapter, tuple(args.resource)))
    elif command == "grant-install":
        action = _action_from_args(args, f"grant-template:{args.grant_id}")
        grant = AuthorizationGrant(
            args.grant_id,
            args.task_id,
            ActionClass(args.action_class),
            args.target,
            summary_sha256(action),
            args.expires_at,
            max_uses=args.max_uses,
            content_sha256=args.content_sha256,
        )
        bound.install_grant(grant)
        print(json.dumps(asdict(grant), indent=2, sort_keys=True, default=str))
    elif command == "action-intent":
        action = _action_from_args(args, args.action_id)
        if args.grant_id or requires_authorization(action.action_class):
            # Consequential intents go through the reservation path, which is
            # the only code that validates and consumes the grant.
            consumed = bound.reserve_execution(
                action,
                args.grant_id,
                run_id=args.run_id,
                lease_owner=args.lease_owner,
            )
            print(
                f"{args.task_id}\tintent\t{args.action_id}\t"
                f"grant={consumed.grant_id if consumed else 'none'}"
            )
        else:
            bound.record_action_intent(
                action,
                run_id=args.run_id,
                lease_owner=args.lease_owner,
            )
            print(f"{args.task_id}\tintent\t{args.action_id}")
    elif command == "action-result":
        status = bound.record_action_result(
            args.action_id,
            args.outcome,
            args.evidence_sha256,
            run_id=args.run_id,
            lease_owner=args.lease_owner,
        )
        print(f"{args.task_id}\t{args.action_id}\t{status}")
    elif command == "action-reconcile":
        bound.reconcile_action(
            args.action_id,
            verified=args.verified,
            evidence_sha256=args.evidence_sha256,
        )
        print(f"{args.task_id}\treconciled\t{args.action_id}")
    elif command == "delegation-record":
        return _delegation_record(bound, args)
    else:
        raise ValueError(f"unsupported task command: {command}")
    return EXIT_OK


def _parse_receipt(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("receipt must be a regular file")
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip().strip('"')
    return fields


def _delegation_record(bound: TaskStore, args: argparse.Namespace) -> int:
    """Validate a transport receipt and persist it inside the workspace.

    Without this the delegate's answer is a free-form file in the system temp
    directory, bound to the request by nothing but its name.
    """

    receipt = _parse_receipt(args.receipt)
    required = (
        "task_id",
        "request_id",
        "provider",
        "transport",
        "purpose",
        "reasoning",
        "context_sha256",
    )
    missing = [key for key in required if not receipt.get(key)]
    if missing:
        raise ValueError("receipt is missing fields: " + ", ".join(missing))
    task = bound.load()
    request = DelegationRequest(
        task_id=receipt["task_id"],
        request_id=receipt["request_id"],
        provider=receipt["provider"],
        context_sha256=receipt["context_sha256"],
        purpose=receipt["purpose"],
        transport=receipt["transport"],
        reasoning_effort=receipt["reasoning"],
        research_mode=receipt.get("verified_research_mode")
        or receipt.get("requested_research_mode", "standard"),
        fallback_policy=receipt.get("fallback_policy", "block"),
    )
    if request.task_id != task.task_id:
        raise ValueError("receipt belongs to another workspace")
    validate_request_policy(task, request)
    validate_disclosure(
        DisclosureDecision(
            decision_id=request.request_id,
            task_id=request.task_id,
            provider=request.provider,
            context_sha256=request.context_sha256,
            included_roots=(),
            status="approved",
        ),
        request,
    )
    stored = [
        bound.store_run_artifact(
            args.run_id,
            "receipts",
            args.receipt,
            f"{request.request_id}.receipt.txt",
            lease_owner=args.lease_owner,
        )
    ]
    if args.response:
        response_digest = receipt.get("response_sha256")
        if not response_digest:
            raise ValueError("receipt does not record a response digest")
        validate_response(
            request,
            DelegationResponse(
                task_id=request.task_id,
                request_id=request.request_id,
                provider=request.provider,
                context_sha256=request.context_sha256,
                kind=request.purpose,
                advice={"response_sha256": response_digest},
            ),
        )
        # The store digest covers the file mode as well, so compare the raw
        # content digest with what the transport recorded.
        content_digest = hashlib.sha256(args.response.read_bytes()).hexdigest()
        if content_digest != response_digest:
            raise ValueError(
                "response content does not match the digest in the receipt"
            )
        stored.append(
            bound.store_run_artifact(
                args.run_id,
                "delegations",
                args.response,
                f"{request.request_id}.response.md",
                lease_owner=args.lease_owner,
            )
        )
    bound.append_event(
        "delegation.recorded",
        {
            "request_id": request.request_id,
            "provider": request.provider,
            "transport": request.transport,
            "purpose": request.purpose,
            "context_sha256": request.context_sha256,
            "artifacts": [item["path"] for item in stored],
        },
    )
    print(json.dumps({"request_id": request.request_id, "artifacts": stored}, indent=2, sort_keys=True))
    return EXIT_OK


def _guard_command(args: argparse.Namespace) -> int:
    def deny(reason: str, code: int) -> int:
        print(
            json.dumps(
                {
                    "allowed": False,
                    "capability": args.capability,
                    "reason": reason,
                    "task_id": args.task_id,
                    "tool": args.tool,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return code

    # Loading happens inside the handler so an unknown or damaged workspace
    # produces the documented denial instead of a traceback.
    try:
        bound = TaskStore(args.root).bind(args.task_id)
        task = bound.load()
        metadata = bound._meta()
    except FileNotFoundError as error:
        return deny(str(error), EXIT_NOT_FOUND)
    except (PermissionError, ValueError, OSError, sqlite3.Error) as error:
        return deny(str(error), EXIT_DENIED)
    try:
        ensure_task_is_operable(task, metadata)
        ensure_external_tool_allowed(task, args.capability, args.tool)
    except (PermissionError, ValueError) as error:
        return deny(str(error), EXIT_DENIED)
    print(
        json.dumps(
            {
                "allowed": True,
                "capability": args.capability,
                "tool": args.tool,
                "task_id": args.task_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _scan_command(args: argparse.Namespace) -> int:
    relatives: list[str] = list(args.paths)
    if args.from_nul:
        raw = args.from_nul.read_bytes().decode("utf-8")
        relatives.extend(item for item in raw.split("\0") if item)
    if not relatives:
        raise ValueError("scan requires at least one path")
    findings = scan_paths(args.repo_root, tuple(relatives))
    if args.json:
        print(
            json.dumps(
                {"findings": [asdict(item) for item in findings]},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for finding in findings:
            print(f"{finding.kind}\t{finding.path}")
        if not findings:
            print(f"clean\t{len(relatives)} paths")
    return EXIT_FINDINGS if findings else EXIT_OK


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "task":
        return _task_command(args)
    if args.command == "guard":
        return _guard_command(args)
    if args.command == "scan":
        return _scan_command(args)
    decision = assess(
        RoutingInput(
            architecture=args.architecture,
            dependent_steps=args.steps,
            relevant_files=args.files,
            safety_review=args.safety_review,
            ambiguity=args.ambiguity,
            web_research=args.web_research,
            current_information=args.current_information,
            cross_source_synthesis=args.cross_source_synthesis,
            regulatory=args.regulatory,
            unfamiliar_domain=args.unfamiliar_domain,
            large_research_volume=args.large_research_volume,
            deep_research_requested=args.deep_research,
            deep_research_available=not args.deep_research_unavailable,
            transport_available=not args.transport_unavailable,
            user_forced=args.user_forced,
            deterministic=args.deterministic,
            local_test_decides=args.local_test_decides,
            live_observation_primary=args.live_observation_primary,
            repeated_failures=args.repeated_failures,
            substantial_final_review=args.substantial_final_review,
            sensitive_broad_context=args.sensitive_broad_context,
            maximal_delegation=not args.no_maximal_delegation,
            requested_provider=args.requested_provider,
            requested_transport=args.requested_transport,
            disclosure_authorized=args.disclosure_authorized,
            provider_available=not args.provider_unavailable,
        )
    )
    print(json.dumps(asdict(decision), indent=2, sort_keys=True))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return _dispatch(args)
    except FileNotFoundError as error:
        print(f"not-found: {error}")
        return EXIT_NOT_FOUND
    except FileExistsError as error:
        print(f"conflict: {error}")
        return EXIT_CONFLICT
    except PermissionError as error:
        print(f"denied: {error}")
        return EXIT_DENIED
    except (ValueError, sqlite3.Error) as error:
        # Documented control-flow outcomes are results, not crashes: a caller
        # must be able to branch on them.
        print(f"error: {error}")
        return EXIT_DENIED
    except OSError as error:
        print(f"error: {error}")
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
