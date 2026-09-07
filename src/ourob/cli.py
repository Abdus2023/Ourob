"""Command-line interface.

Every command is a thin wrapper over the library; nothing important happens only
in here.  ``python bootstrap.py <command>`` reaches the same code without an
install step.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__, fsx
from .bootstrap.amend import AmendmentLedger
from .bootstrap.coldstart import boot
from .bootstrap.manifest import Manifest, compare
from .bootstrap.promote import Promotion
from .bootstrap.snapshot import Snapshot
from .config import Config
from .errors import OurobError
from .kernel import Kernel
from .planner.scripted import Plan, ScriptedPlanner
from .policies.rules import default_policy_set
from .skills.registry import SkillRegistry
from .state.store import StateStore
from .verify.gates import GATE_CLASSES
from .verify.suite import format_report, verify_repo


def _repo(args: argparse.Namespace) -> Path:
    start = Path(getattr(args, "repo", None) or Path.cwd())
    return fsx.repo_root(start)


# -- commands -------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    repo = _repo(args)
    config = Config.load(repo)
    registry = SkillRegistry(repo)
    problems = registry.discover()
    policies = default_policy_set()
    lines = [
        f"ourob {__version__} doctor",
        f"  repository   {repo}",
        f"  config       {config.source}",
        f"  python       {sys.executable} ({sys.version.split()[0]})",
        f"  skills       {len(registry.names())}: {', '.join(registry.names())}",
        f"  policies     {len(policies.names())}: {', '.join(policies.names())}",
        f"  gates        {', '.join(GATE_CLASSES)}",
        f"  protected    {len(config.policy.protected)} patterns",
    ]
    lock = repo / "bootstrap.lock.json"
    if lock.is_file():
        manifest = Manifest.load(repo)
        diff = compare(manifest, repo)
        lines.append(f"  lock         {manifest.digest[:16]} ({len(manifest)} files)")
        lines.append(f"  drift        {diff.describe()}")
        violations = [p for p in diff.touched if config.is_protected(p)]
        lines.append(
            f"  protected    {'CLEAN' if not violations else 'DRIFT: ' + ', '.join(violations)}"
        )
    else:
        lines.append("  lock         MISSING -- run `ourob manifest --rebuild`")
    for problem in problems:
        lines.append(f"  ! discovery  {problem}")
    chain = StateStore(repo).check_chain()
    bad = [c for c in chain if not c["ok"]]
    lines.append(
        f"  journal      {len(chain)} log(s), "
        f"{'all chains intact' if not bad else 'CORRUPT: ' + ', '.join(c['journal'] for c in bad)}"
    )
    print("\n".join(lines))
    return 0 if not problems and not bad else 1


def cmd_skills(args: argparse.Namespace) -> int:
    registry = SkillRegistry(_repo(args))
    registry.discover()
    if args.json:
        print(json.dumps(registry.catalogue(), indent=2))
        return 0
    for entry in registry.catalogue():
        print(f"{entry['name']}")
        print(f"    {entry['description']}")
        print(f"    module: {entry['module']}   mutating: {entry['mutating']}")
        for name, rule in entry["params"].items():
            flags = [rule.get("type", "any")]
            if rule.get("required"):
                flags.append("required")
            if "default" in rule:
                flags.append(f"default={rule['default']!r}")
            print(f"      {name}: {', '.join(str(f) for f in flags)}  {rule.get('desc', '')}")
        print()
    if registry.problems():
        print("discovery problems:")
        for problem in registry.problems():
            print(f"  ! {problem}")
        return 1
    return 0


def cmd_policies(args: argparse.Namespace) -> int:
    policies = default_policy_set()
    if args.json:
        print(json.dumps(policies.catalogue(), indent=2))
        return 0
    for entry in policies.catalogue():
        kind = "blocking" if entry["blocking"] else "advisory"
        print(f"{entry['name']} ({kind})")
        print(f"    {entry['description']}")
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    for name, cls in GATE_CLASSES.items():
        kind = "blocking" if cls.blocking else "advisory"
        print(f"{name} ({kind}) -- {cls.title}")
        print(f"    {cls.why}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    repo = _repo(args)
    gates = args.gates or None
    outcome = verify_repo(
        repo, gates=gates, run_id=args.run_id or "", amended_paths=args.amend or []
    )
    print(format_report(outcome.report, verbose=args.verbose))
    if outcome.unknown:
        print(f"unknown gate(s) ignored: {', '.join(outcome.unknown)}")
    return 0 if outcome.passed else 1


def cmd_manifest(args: argparse.Namespace) -> int:
    repo = _repo(args)
    config = Config.load(repo)
    if args.rebuild:
        previous_version = 0
        if (repo / "bootstrap.lock.json").is_file():
            previous_version = Manifest.load(repo).version
        manifest = Manifest.build(
            repo,
            notes=args.note or "rebuilt from the working tree",
            version=previous_version + 1,
        )
        path = manifest.save()
        print(
            f"lock rewritten: {len(manifest)} files, version {manifest.version}, "
            f"digest {manifest.digest[:16]}"
        )
        print(f"  -> {fsx.rel(path, repo)}")
        return 0
    if not (repo / "bootstrap.lock.json").is_file():
        print("no bootstrap.lock.json; run `ourob manifest --rebuild`")
        return 1
    manifest = Manifest.load(repo)
    diff = compare(manifest, repo)
    print(f"lock      {manifest.digest}")
    print(f"files     {len(manifest)} ({len(manifest.protected_paths)} protected)")
    print(f"created   {manifest.created_at}  version {manifest.version}")
    print(f"notes     {manifest.notes or '(none)'}")
    print(f"drift     {diff.describe()}")
    for label, items in (
        ("added", diff.added),
        ("changed", diff.changed),
        ("removed", diff.removed),
        ("missing", diff.missing),
    ):
        for item in items:
            flag = " [protected]" if config.is_protected(item) else ""
            print(f"  {label:<8} {item}{flag}")
    if args.list:
        print("\ntracked files:")
        for path in manifest.paths():
            print(f"  {'P' if manifest.files[path].protected else ' '} {path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo = _repo(args)
    plan = Plan.load(repo / args.plan) if not Path(args.plan).is_absolute() else Plan.load(args.plan)
    kernel = Kernel(
        repo,
        verify_at_end=not args.no_verify,
        logger=(lambda m: print(f"  {m}")) if args.verbose else None,
    )
    if args.max_steps:
        plan.max_steps = args.max_steps
    planner = ScriptedPlanner(plan)
    print(f"goal: {plan.goal}")
    print(f"planner: {planner.describe()}")
    outcome = kernel.run(plan.goal, planner, max_steps=plan.max_steps)
    print()
    print(outcome.summary())
    if args.json:
        print(json.dumps(outcome.run.to_dict(), indent=2))
    if outcome.snapshot and not outcome.ok:
        print(
            f"\nthe pre-run snapshot is at .ourob/snapshots/{outcome.snapshot}; "
            f"`ourob promote --snapshot {outcome.snapshot}` will roll the tree back."
        )
    return 0 if outcome.ok else 1


def cmd_promote(args: argparse.Namespace) -> int:
    repo = _repo(args)
    promotion = Promotion(
        repo,
        use_git=not args.no_git,
        rollback_on_failure=not args.no_rollback,
        snapshot_run_id=args.snapshot or "",
    )
    result = promotion.promote(
        message=args.message or "", amendment_id=args.amendment or "", gates=args.gates or None
    )
    print(result.describe())
    return 0 if result.accepted else 1


def cmd_amend(args: argparse.Namespace) -> int:
    repo = _repo(args)
    ledger = AmendmentLedger(repo / ".ourob" / "amendments")
    if args.list:
        amendments = ledger.all()
        if not amendments:
            print("no amendments on file")
            return 0
        for amendment in amendments:
            print(amendment.summary())
        return 0
    if args.reject:
        amendment = ledger.set_status(
            args.reject, "rejected", evidence={"withdrawn_by": args.by or "operator",
                                               "reason": args.why or "withdrawn"}
        )
        print(f"rejected {amendment.amendment_id}")
        print(f"  it authorised: {', '.join(amendment.paths)}")
        print(f"  reason:        {amendment.evidence.get('reason', '')}")
        print("\nthose paths are protected again.")
        return 0
    if not args.paths:
        print("usage: ourob amend <path> [<path> ...] --why \"rationale\"", file=sys.stderr)
        return 2
    amendment = ledger.propose(args.paths, args.why or "", proposed_by=args.by or "operator")
    print(f"proposed {amendment.amendment_id}")
    print(f"  paths     {', '.join(amendment.paths)}")
    print(f"  rationale {amendment.rationale}")
    print(f"  status    {amendment.status}")
    print("\nwrites to those paths are now permitted during a run.")
    print(f"promote with `ourob promote --amendment {amendment.amendment_id}` to make it official.")
    return 0


def cmd_ratify(args: argparse.Namespace) -> int:
    repo = _repo(args)
    promotion = Promotion(repo, use_git=not args.no_git, snapshot_run_id=args.snapshot or "")
    result = promotion.promote(
        message=args.message or f"ratify {args.amendment}", amendment_id=args.amendment
    )
    print(result.describe())
    return 0 if result.accepted else 1


def cmd_runs(args: argparse.Namespace) -> int:
    store = StateStore(_repo(args))
    runs = store.list_runs()
    if not runs:
        print("no runs recorded")
        return 0
    for run in runs:
        print(
            f"{run.get('run_id')}  {run.get('status', '?'):<10} "
            f"steps={run.get('steps', '?')}  {run.get('goal', '')}"
        )
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    store = StateStore(_repo(args))
    if args.check:
        ok = True
        for entry in store.check_chain():
            print(f"{'ok  ' if entry['ok'] else 'BAD '} {entry['journal']}: {entry['detail']}")
            ok = ok and entry["ok"]
        return 0 if ok else 1
    if not args.run_id:
        for event in store.system_events():
            print(f"{event['stamp']}  {event['kind']:<22} {json.dumps(event['payload'])[:160]}")
        return 0
    for event in store.journal(args.run_id).events():
        payload = json.dumps(event.get("payload", {}))
        if len(payload) > args.width:
            payload = payload[: args.width] + "..."
        print(f"{event['seq']:>4}  {event['stamp']}  {event['kind']:<22} {payload}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = StateStore(_repo(args))
    run = store.replay(args.run_id)
    print(f"run {run.run_id}  status={run.status.value}  planner={run.planner}")
    print(f"goal: {run.goal}")
    print(f"outcome: {run.outcome}")
    for step in run.steps:
        result = step.result
        status = step.status.value
        print(f"\n#{step.index} {step.invocation.skill} [{status}]")
        if step.invocation.args:
            print(f"    args: {json.dumps(step.invocation.args, default=str)[:400]}")
        for decision in step.decisions:
            if not decision.allowed or decision.severity != "info":
                print(f"    policy {decision.policy}: {decision.reason}")
        if result is not None:
            print(f"    -> {'ok' if result.ok else 'error'} {result.output[:400]}")
    if run.verification is not None:
        print("\n" + format_report(run.verification))
    return 0


def cmd_coldstart(args: argparse.Namespace) -> int:
    repo = _repo(args)
    try:
        _module, report = boot(
            repo, trust_drift=args.trust_drift, rebuild_lock=args.rebuild, import_runtime=True
        )
    except OurobError as exc:
        print(f"BOOT FAILED: {exc}", file=sys.stderr)
        return 1
    print(report.describe())
    return 0


def cmd_snapshots(args: argparse.Namespace) -> int:
    repo = _repo(args)
    if args.rollback:
        snapshot = Snapshot.load(repo, args.rollback)
        outcome = snapshot.restore()
        print(f"rolled back to snapshot {args.rollback}")
        restored = ", ".join(outcome["restored"]) or "(none)"
        removed = ", ".join(outcome["removed"]) or "(none)"
        print(f"  restored {len(outcome['restored'])} file(s): {restored}")
        print(f"  removed  {len(outcome['removed'])} file(s): {removed}")
        print(f"  drift since snapshot: {snapshot.drift_since().describe()}")
        return 0
    if args.discard:
        Snapshot.load(repo, args.discard).discard()
        print(f"discarded snapshot {args.discard}")
        return 0
    names = Snapshot.list_for(repo)
    if not names:
        print("no snapshots")
        return 0
    for name in names:
        snapshot = Snapshot.load(repo, name)
        print(f"{name}  {snapshot.created_at}  {snapshot.files} files  lock {snapshot.digest[:16]}")
        print(f"    drift since: {snapshot.drift_since().describe()}")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    repo = _repo(args)
    print(f"ourob {__version__} self-test in {repo}\n")
    steps = [
        ("cold start", lambda: cmd_coldstart(_ns(args, trust_drift=True, rebuild=False))),
        ("skill discovery", lambda: cmd_skills(_ns(args, json=False))),
        ("policy catalogue", lambda: cmd_policies(_ns(args, json=False))),
        ("gate catalogue", lambda: cmd_gates(_ns(args))),
        ("journal integrity", lambda: cmd_journal(_ns(args, check=True, run_id=None, width=200))),
        ("verification suite", lambda: cmd_verify(_ns(args, gates=[], verbose=False, amend=[], run_id=""))),
    ]
    failures = []
    for name, action in steps:
        print(f"--- {name} " + "-" * max(0, 60 - len(name)))
        code = action()
        print(f"    => {'ok' if code == 0 else f'exit {code}'}\n")
        if code != 0:
            failures.append(name)
    if failures:
        print(f"self-test FAILED: {', '.join(failures)}")
        return 1
    print("self-test PASSED")
    return 0


def _ns(base: argparse.Namespace, **overrides: Any) -> argparse.Namespace:
    data = vars(base).copy()
    data.update(overrides)
    return argparse.Namespace(**data)


# -- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ourob",
        description="autonomous-engineering runtime, self-hosted in its own repository",
    )
    parser.add_argument("--version", action="version", version=f"ourob {__version__}")
    parser.add_argument("--repo", help="repository root (default: nearest ancestor with ourob.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="report the health of the runtime and its repository").set_defaults(
        func=cmd_doctor
    )

    p = sub.add_parser("skills", help="list the skill catalogue")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("policies", help="list the policy set")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_policies)

    sub.add_parser("gates", help="list the verification gates").set_defaults(func=cmd_gates)

    p = sub.add_parser("verify", help="run the verification suite")
    p.add_argument("--gates", nargs="*", help="subset of gates to run")
    p.add_argument("--amend", nargs="*", default=[], help="paths covered by an amendment")
    p.add_argument("--run-id", default="", help="tag the report with this run id")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("manifest", help="inspect or rewrite the bootstrap lock")
    p.add_argument("--rebuild", action="store_true", help="rewrite the lock from the working tree")
    p.add_argument("--note", default="", help="note to record with the new lock")
    p.add_argument("--list", action="store_true", help="list every tracked file")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("run", help="execute a plan from plans/")
    p.add_argument("plan", help="path to a plan JSON file")
    p.add_argument("--max-steps", type=int, default=0)
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("promote", help="verify a change set and make it the new ratified state")
    p.add_argument("--amendment", default="", help="amendment id authorising protected drift")
    p.add_argument("--message", default="", help="note for the new lock and the commit")
    p.add_argument("--gates", nargs="*", help="subset of gates to run")
    p.add_argument("--snapshot", default="", help="run id whose snapshot to roll back to on failure")
    p.add_argument("--no-git", action="store_true")
    p.add_argument("--no-rollback", action="store_true")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("amend", help="propose (or list) constitutional amendments")
    p.add_argument("paths", nargs="*", help="protected paths to authorise")
    p.add_argument("--why", default="", help="rationale")
    p.add_argument("--by", default="", help="who is proposing")
    p.add_argument("--list", action="store_true")
    p.add_argument("--reject", default="", metavar="AMENDMENT_ID", help="withdraw an amendment")
    p.set_defaults(func=cmd_amend)

    p = sub.add_parser("ratify", help="promote under an amendment and mark it ratified")
    p.add_argument("amendment")
    p.add_argument("--message", default="")
    p.add_argument("--snapshot", default="")
    p.add_argument("--no-git", action="store_true")
    p.set_defaults(func=cmd_ratify)

    sub.add_parser("runs", help="list recorded runs").set_defaults(func=cmd_runs)

    p = sub.add_parser("journal", help="read the hash-chained journal")
    p.add_argument("run_id", nargs="?", default=None)
    p.add_argument("--check", action="store_true", help="verify every hash chain")
    p.add_argument("--width", type=int, default=200)
    p.set_defaults(func=cmd_journal)

    p = sub.add_parser("show", help="replay a run from its journal")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("coldstart", help="verify the tree and boot the runtime from it")
    p.add_argument("--trust-drift", action="store_true")
    p.add_argument("--rebuild", action="store_true")
    p.set_defaults(func=cmd_coldstart)

    p = sub.add_parser("snapshots", help="list, roll back to, or discard a snapshot")
    p.add_argument("--rollback", default="", metavar="RUN_ID", help="restore the tree exactly")
    p.add_argument("--discard", default="", metavar="RUN_ID")
    p.set_defaults(func=cmd_snapshots)

    sub.add_parser("selftest", help="run every read-only command and report").set_defaults(
        func=cmd_selftest
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except OurobError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
