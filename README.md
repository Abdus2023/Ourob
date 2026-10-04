# ourob

`ourob` is a self-hosted autonomous-engineering runtime. Its source code, skills,
policies, state model, verification machinery, and bootstrap process all live in
the repository that it is capable of modifying.

The central design rule is simple:

> A self-modification is not accepted merely because it ran. It must pass the same
> policy, journaling, verification, review, and promotion path as any other change.

The runtime is intentionally small and inspectable. It has no required service,
database, or installed daemon: a checkout and Python are enough to cold-start it.

## Quick start

`ourob` requires Python 3.11 or newer. The runtime itself has no mandatory third-
party dependencies.

```bash
# Verify that the checkout is trusted before loading the runtime.
python bootstrap.py --prove

# Inspect repository, policy, skill, gate, and journal health.
python bootstrap.py doctor

# Run the read-only end-to-end smoke checks.
python bootstrap.py selftest

# Run the verification suite.
python bootstrap.py verify -v
```

For development and testing, install the complete documented toolchain:

```bash
python -m pip install -e '.[dev]'
python -m pytest
```

The `dev` extra includes `httpx` because the LLM-planner test module imports it
while pytest collects the suite. `httpx` remains optional for a normal runtime
installation; the LLM planner requires it when enabled. Install only that
runtime feature with:

```bash
python -m pip install -e '.[llm]'
```

The bootstrap script places `src/` on `sys.path`, so the commands above work from
the repository root without a separate installation. The editable install is
convenient for development and exposes the `ourob` console script.

## Repository map

```text
ourob/
├── bootstrap.py                 stdlib-only root of trust and cold-start entry point
├── bootstrap.lock.json          hash manifest for the protected runtime tree
├── ourob.toml                   protected runtime and policy configuration
├── pyproject.toml               package metadata, dependencies, and tool settings
├── plans/                       deterministic, reviewable execution plans
│   ├── amend_policy.json        example protected-path amendment plan
│   ├── healthcheck.json         read-only health-check plan
│   ├── refused.json             deliberately denied operations
│   └── self_extend.json          example self-extension plan
├── docs/
│   ├── ARCHITECTURE.md          design rationale and system invariants
│   └── CONSTITUTION.md          amendment, protection, and ratification rules
├── src/ourob/                   runtime package (src-layout)
│   ├── __init__.py              package metadata and public package surface
│   ├── cli.py                   command-line interface and command dispatch
│   ├── clock.py                 injectable time source used by runtime records
│   ├── config.py                TOML configuration loading and validation
│   ├── errors.py                domain-specific exception types
│   ├── fsx.py                   confined filesystem and snapshot helpers
│   ├── kernel.py                planner → policy → journal → skill execution loop
│   ├── schema.py                plan, skill, invocation, and result validation
│   │
│   ├── bootstrap/               promotion and cold-start lifecycle
│   │   ├── amend.py             create and inspect protected-path amendments
│   │   ├── coldstart.py         verify a tree before booting it
│   │   ├── manifest.py          read, compare, and rewrite the lock manifest
│   │   ├── promote.py            review, verify, ratify, and promote changes
│   │   └── snapshot.py           exact pre-run snapshots and rollback support
│   │
│   ├── planner/                 the only component that chooses the next action
│   │   ├── base.py              planner protocol and runtime view
│   │   ├── scripted.py           deterministic JSON-plan replay
│   │   └── llm.py                optional HTTP-based LLM planner
│   │
│   ├── policies/                deny-wins safety policy set (protected)
│   │   ├── base.py               policy protocol and policy decisions
│   │   └── rules.py              path, command, budget, payload, journal,
│   │                              protected-path, and retry policies
│   │
│   ├── skills/                  discoverable capabilities
│   │   ├── base.py               skill protocol, metadata, and contracts
│   │   ├── registry.py           built-in and contribution skill discovery
│   │   ├── builtin/
│   │   │   ├── files.py          read, write, edit, and directory operations
│   │   │   ├── shell.py          constrained command and Python execution
│   │   │   └── verification.py   test, gate, and amendment operations
│   │   └── contrib/
│   │       └── rot13.py          example contribution skill
│   │
│   ├── state/                   append-only run history
│   │   ├── model.py              Run, Step, Invocation, decision, and result types
│   │   └── store.py              hash-chained JSONL journal and `.head` anchors
│   │
│   └── verify/                  independent acceptance gates (protected)
│       ├── gates.py              individual compile, import, lint, test, and policy gates
│       └── suite.py              subprocess-isolated gate orchestration
│
├── tests/                       executable specification and adversarial coverage
│   ├── test_bootstrap.py         manifest proof and cold-start behavior
│   ├── test_cli.py               command-line behavior
│   ├── test_config.py            configuration and protected-path rules
│   ├── test_contrib_rot13.py     contribution discovery and execution
│   ├── test_fsx.py               filesystem confinement and snapshots
│   ├── test_kernel.py            planner, policy, journal, and skill loop
│   ├── test_llm_planner.py       optional planner protocol behavior
│   ├── test_manifest.py          lock and tree integrity
│   ├── test_policies.py          deny-wins policy behavior
│   ├── test_resource_limits.py   subprocess limits and process-group cleanup
│   ├── test_review.py            run diffs and promotion descriptions
│   ├── test_rollback_invariant.py exact rollback behavior
│   ├── test_schema.py            schema and plan validation
│   ├── test_skills.py            skill contracts and discovery
│   ├── test_state.py             journal persistence and tamper detection
│   └── test_verify.py             verification gate behavior
│
└── .ourob/                      runtime journals and indexes (generated/ignored)
```

## How the runtime works

A run follows one narrow path:

```text
Planner
   │ chooses an invocation
   ▼
PolicySet ── deny or allow, with every verdict journalled
   │
   ▼
Skill ── performs the operation and reports touched artifacts
   │
   ▼
Journal ── records the result and advances the hash chain
   │
   ▼
Verification ── checks whether the resulting tree is acceptable
   │
   ├── failure: rollback to the pre-run snapshot
   └── success: review and promote the change
```

### Planners

The planner is the only component that decides what to do next. The safety
machinery is independent of the planner, so scripted and LLM-driven runs receive
the same policy and verification treatment.

- **`ScriptedPlanner`** replays a JSON plan from `plans/`. It is deterministic,
  diffable, and suitable for tests and review.
- **`OpenAIPlanner`** is optional and uses `httpx` plus `OUROB_API_KEY`. Its
  invocations are still constrained, journalled, and verified by the runtime.

Planners receive a restricted `RuntimeView` containing the goal, available skills
and parameter schemas, protected paths, policy names, remaining budget, and the
run transcript. They do not receive unrestricted hidden state.

### Skills

A skill declares a unique name, description, parameter schema, and whether it
mutates the repository. The registry discovers built-ins from `skills/builtin/`
and contributions from `skills/contrib/` rather than relying on a central table.
That means adding a contribution is itself a file-writing operation.

A partially written or invalid contribution is quarantined and reported instead
of preventing the rest of the runtime from booting. Duplicate names and invalid
contracts are rejected by discovery and the `skill-contract` gate.

The shell and Python execution skills are intentionally constrained. Child
processes run in their own process group and receive resource ceilings before
`exec`, including address-space and file-size limits, optional CPU and process
limits, and disabled core dumps. On supported Linux hosts, a Landlock ruleset
also denies child and descendant content/namespace writes to configured
protected paths, `.ourob`, and `.git`; a seccomp filter denies selected ownership
and extended-attribute changes. It does not mediate `chmod` or timestamp
changes. Children may create or modify file contents and namespace entries
only in existing unprotected repository directories and a per-call temporary
directory.
Unsupported hosts fail closed for child execution.

This is a **narrow child-process write boundary**, not a general OS sandbox. It
does not restrict reads, networking, or general process inspection; it does not
confine file-mode or timestamp changes; and it does not stop a user with checkout
access or arbitrary in-process Python code from writing files directly. No
namespace, container, or privilege-separation claim is made.

### Policies

Policies are small, independent checks combined with **deny-wins** semantics. A
policy failure or exception becomes a denial rather than permission. The policy
set protects the following boundaries:

| Policy area | Purpose |
| --- | --- |
| Path confinement | Keep declared path parameters inside the repository. |
| Protected paths | Require a separately authorized, exact-scope amendment before changing guardrails or bootstrap files. |
| Journal integrity | Deny ordinary runtime writes to history and verify hash chains, anchors, and system-event semantics. |
| Budget | Bound steps and execution resources. |
| Payload size | Reject unreasonably large writes. |
| Command allowlist | Restrict commands to those configured in `ourob.toml`. |
| Child filesystem | Require a supported write boundary before executing child code. |
| Loop breaker | Prevent endless retries of the same failing operation. |

Path-bearing skill parameters must explicitly declare `"path": true` in their
schema. The contract checker rejects a skill that appears to carry paths without
declaring them, closing the gap at registration time rather than relying only on
runtime key-name heuristics.

### State and journals

Run state is persisted as append-only JSONL under `.ourob/`. A journal records
runs, steps, invocations, policy decisions, and skill results. Each line contains
the hash of the preceding line, and a `.head` file records the final sequence and
hash. Together these detect edits, reordering, and truncation.

Useful commands include:

```bash
python bootstrap.py runs
python bootstrap.py show <run-id>
python bootstrap.py journal --check
python bootstrap.py diff <run-id>
python bootstrap.py diff <run-id> --stat
```

The journal is operational state, not source. It is ignored by Git and should not
be hand-edited.

### Verification gates

The verifier runs gates in isolated subprocesses so a broken tree cannot take
down the verifier itself. The configured gates are:

| Gate | Question |
| --- | --- |
| `policy-integrity` | Do the declared protections still cover the hard floor? |
| `skill-contract` | Does every discovered skill have a valid contract? |
| `compile` | Does all Python source compile? |
| `import` | Does the package import cleanly from this checkout? |
| `manifest` | Does the lock match the protected tree? |
| `lint` | Does Ruff accept the source and tests? |
| `tests` | Does the executable specification pass? |
| `bootstrap` | Can the repository cold-start itself? |

`ourob.toml` may escalate advisory gates to blocking, but it cannot demote a gate
that is blocking in code. The tests gate determines a suitable worker count from
CPU affinity when xdist is available and falls back to serial execution when it
is not.

## Protected paths and amendments

The following paths are protected by `ourob.toml` and cannot be modified by a
normal mutating skill without a separately recorded, currently valid operator grant:

```text
ourob.toml
bootstrap.py
bootstrap.lock.json
src/ourob/bootstrap/
src/ourob/policies/
src/ourob/verify/
```

The bootstrap script additionally hard-codes protection for its own root of trust
and lock file. Removing those entries from configuration cannot disable the floor.

To propose a protected change:

```bash
python bootstrap.py amend src/ourob/policies/ \
  --why "explain the constitutional change"
```

A proposal alone grants no write authority. Before a protected change, an
operator must separately authorize the exact displayed proposal in an interactive
terminal; this pins the proposal digest, base revision, lock digest, and path set
in `.ourob/system.jsonl`:

```bash
python bootstrap.py authorize <amendment-id> --by "operator identity"
```

The command requires typing the proposal ID. `--by` is a recorded, self-asserted
label, not cryptographic identity proof. After the run has produced a candidate
tree, ratification verifies it and either promotes it or rolls it back exactly:

```bash
python bootstrap.py ratify <amendment-id> \
  --message "describe the accepted change"
```

An operator grant permits only the proposal's exact paths while its pinned base
remains current; it does not accept the change. Only a green verification suite
can ratify the resulting tree. See [`docs/CONSTITUTION.md`](docs/CONSTITUTION.md)
for the full rules and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design
rationale.

## Running plans

Plans are JSON documents containing deterministic skill invocations. A plan can
be inspected before execution and replayed without an LLM:

```bash
python bootstrap.py run plans/healthcheck.json -v
python bootstrap.py run plans/self_extend.json -v
python bootstrap.py verify -v
```

The self-extension example adds a new `text_metrics` contribution skill and its
behavioral tests. The refused plan attempts operations such as leaving the
repository, changing protected machinery, rewriting the journal, and running a
denied destructive command. Those calls are rejected before the skill executes.

A run that changes files is **drift** until it is promoted. Review the actual
journal-derived change set before promotion:

```bash
python bootstrap.py diff <run-id>
python bootstrap.py diff <run-id> --stat
python bootstrap.py promote --message "describe the reviewed change"
```

Promotion compares the tree with the lock, checks amendment requirements, runs
the verification suite, and updates the lock only on success. Failed promotion
rolls back to the snapshot taken before the first mutating step, including files
created during the run.

## Development workflow

1. Start from a clean checkout and run `python bootstrap.py --prove`.
2. Read the relevant architecture and constitution sections before changing
   policies, bootstrap, verification, or state code.
3. Make a focused change and add or update the corresponding tests.
4. Run the narrowest relevant test first, then the complete suite:

   ```bash
   python -m pytest
   python bootstrap.py --prove --strict
   python bootstrap.py verify -v
   ```

5. Inspect the diff and confirm generated files such as `.coverage`, caches, and
   `.ourob/` state are not being committed.
6. Use an amendment and ratification workflow for protected paths.

The repository's tests copy the checkout into temporary directories when they need
to exercise self-modification, so normal test runs do not intentionally mutate the
working tree. The generated `.coverage`, `.pytest_cache/`, `__pycache__/`, build
artifacts, and `.ourob/` directories are ignored by Git.

## Design boundaries

`ourob` deliberately does **not** claim to provide:

- a general OS sandbox or privilege separation for child processes;
- protection from arbitrary in-process Python or a user who can directly edit the checkout;
- concurrent runs against one kernel and journal; or
- semantic judgment about whether a change is wise or useful.

The runtime provides procedural controls: explicit application policies,
hash-chained journals with head anchors, reproducible plans, independent gates,
rollback, and human-readable promotion records. These controls are bounded by
their application and child-process paths; they do not replace human judgment or
operating-system security.

## License

MIT.
