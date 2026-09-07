# ourob

An autonomous-engineering runtime whose **source, skills, policies, state model,
verification machinery and bootstrap mechanism all live inside the repository it
is capable of engineering.**

That sentence is the whole design. `ourob` can rewrite `ourob`. What keeps that
from being reckless is that every self-modification travels the same road as any
other change: it is proposed by a planner, judged by a policy set, written to a
tamper-evident journal, and only *accepted* if the repository's own verification
gates are green afterwards. A change that has not been promoted is drift, and
drift does not become the runtime.

```
ourob/
├── bootstrap.py            root of trust: stdlib-only, verifies the tree, then boots it
├── bootstrap.lock.json     self-describing manifest: every file that is "the runtime"
├── ourob.toml              protected config: what may not be touched without an amendment
├── plans/                  deterministic, reviewable plans
├── src/ourob/
│   ├── kernel.py           the loop
│   ├── skills/             what the runtime can do      (self-extendable)
│   ├── policies/           what it is allowed to do     (protected)
│   ├── state/              what it remembers            (append-only, hash-chained)
│   ├── verify/             how a change is judged       (protected)
│   ├── bootstrap/          how it rebuilds itself       (protected)
│   ├── planner/            the only component that decides
│   └── cli.py
└── tests/                  the runtime's specification
```

## Try it

Nothing to install. The bootstrap puts the runtime on `sys.path` itself.

```bash
python bootstrap.py --prove      # integrity proof for this tree
python bootstrap.py doctor       # skills, policies, gates, lock, journal health
python bootstrap.py selftest     # every read-only command, end to end
python bootstrap.py verify -v    # run the gates
```

The interesting one: have the runtime add a capability to itself, then verify it.

```bash
python bootstrap.py run plans/self_extend.json -v
python bootstrap.py verify -v                       # gates must be green
python bootstrap.py promote --message "add the rot13 skill"
python bootstrap.py skills | grep rot13             # it is part of the runtime now
```

And the one that matters: try to make it break its own rules.

```bash
python bootstrap.py run plans/refused.json -v
```

All four attempts — escaping the repository, editing the policy engine, rewriting
its own journal, running `rm -rf` — are refused before the skill executes, and
the tree is byte-identical afterwards.

## The seven things

### 1. Source

Ordinary Python under `src/ourob/`, src-layout, no required dependencies.
Optional `httpx` for the LLM planner.

### 2. Skills

A skill declares a name, a description, a parameter schema and whether it
mutates the tree. The registry discovers them by **scanning**, not from a table:
`skills/builtin/` is a package, and anything in `skills/contrib/` is imported by
file path. A skill the runtime writes during a run is live on the next discovery
pass — no install step, no restart. That is what makes self-extension mechanical.

A half-written contrib skill is quarantined and reported, not fatal.

`run_command` and `run_python` are the dangerous pair, so neither trusts a
wall-clock timeout on its own. Every child is started in its own session and
capped with `setrlimit` **after fork, before exec** — address space, per-file
size, optionally CPU seconds and process count, core dumps always off — so the
code never gets a chance to raise its own ceiling. The timeout kills the whole
process group, not just the direct child, so a grandchild cannot outlive the
run that spawned it:

```toml
[policy.limits]        # optional; the defaults are already applied
address_space_mb = 2048
file_size_mb     = 256
cpu_seconds      = 0   # 0 = do not set this limit
processes        = 0
```

A wall-clock timeout is not a sandbox. Without the ceiling, generated code can
exhaust memory long before the timeout fires, fill the disk, or fork.

### 3. Policies

Seven rules, each about one thing, combined deny-wins. Every verdict — including
the allows — is journalled.

| policy | what it stops |
|---|---|
| `path-confinement` | any path leaving the repository |
| `protected-paths` | writing the bootstrap, policies, verifier or config without an amendment |
| `journal-integrity` | writing to the runtime's own history |
| `budget` | unbounded runs |
| `payload-size` | absurd writes |
| `command-allowlist` | commands outside `ourob.toml` |
| `loop-breaker` | retrying the same failing call forever |

Path confinement is a **declared contract**, not a guess. A skill marks its
path-bearing parameters with `"path": true` in its schema; `path-confinement`
confines exactly those, with the conventional key names kept as a net underneath.
A string parameter whose name looks like a path but does *not* declare itself is
rejected by the `skill-contract` gate, so the gap is closed at registration time
rather than warned about at call time.

### 4. State model

`Run` → `Step` → `Invocation` / `PolicyDecision` / `SkillResult`, persisted as an
append-only JSONL journal per run. Each line carries the SHA-256 of the line
before it, plus a `.head` anchor naming the last sequence number and hash, so
edits, deletions *and* truncation are all detectable. `ourob journal --check`
verifies every chain; `ourob show <run>` replays a run from its log alone.

### 5. Verification machinery

Eight gates, each answering one question, each running in a subprocess with a
timeout so a broken tree cannot take the verifier down with it:

| gate | question |
|---|---|
| `policy-integrity` | are the guardrails still there? (re-reads the config out of process) |
| `skill-contract` | does every skill declare a valid, unique, documented schema? |
| `compile` | does every byte of Python compile? |
| `import` | does the runtime import cleanly from this tree? |
| `manifest` | has anything protected drifted without an amendment? |
| `lint` | is it readable? *(advisory unless escalated in config)* |
| `tests` | is the specification still satisfied? |
| `bootstrap` | can the tree cold-start itself? |

`policy-integrity` exists because a single edit to `ourob.toml` could otherwise
legalise editing `ourob.toml`. `[verify].blocking_gates` can escalate an advisory
gate; nothing can demote a gate that is blocking in code.

### 6. Bootstrap mechanism

`bootstrap.py` is stdlib-only and does not import `ourob` until it has checked
the tree against `bootstrap.lock.json`. The verification it performs is an
independent implementation of the same hash walk the library does — the checker
and the checked are not the same code. A hard-coded floor protects
`bootstrap.py` and the lock itself even if `ourob.toml` is edited to protect
nothing.

### 7. Amendments

Protected paths are not engineering surface; they are the parts that decide what
engineering is allowed. Changing them is a constitutional act:

```bash
python bootstrap.py amend src/ourob/policies/ --why "lower the retry threshold"
# ... a run may now write there ...
python bootstrap.py ratify amd-20260907-000000-abcd1234 --message "lower MAX_REPEATS"
```

Ratification requires green gates. If verification fails, the amendment is marked
rejected and the tree is rolled back to the pre-run snapshot — exactly, including
files the run invented.

## Commands

```
doctor      health of the runtime and its repository
skills      the skill catalogue
policies    the policy set
gates       the verification gates
verify      run the gates
manifest    inspect or rewrite the bootstrap lock
run         execute a plan from plans/
promote     verify a change set and make it the ratified state
amend       propose or list amendments
ratify      promote under an amendment
runs        list recorded runs
diff        what a recorded run actually changed, as a real diff
journal     read (or verify) the hash-chained journal
show        replay a run from its journal
coldstart   verify the tree and boot from it
snapshots   list, roll back to, or discard a snapshot
selftest    every read-only command, end to end
```

## Reviewing a change

A gate can tell you a change is *sound*. It cannot show it to you.

```bash
ourob diff <run-id>            # unified diff, pre-run snapshot vs. the tree now
ourob diff <run-id> --stat     # just the files, and what happened to each
```

The file list is not a scan of the tree — it is re-derived from the journal, from
the artifacts each skill said it touched. The pre-run side comes from the
snapshot the kernel took before the first mutating step, which is the same
snapshot `snapshots --rollback` restores from. So the diff you read, the record
of the run, and the thing rollback would undo are all the same artifact.

Every file is classified `added`, `modified`, `deleted`, `unchanged` (the change
was rolled back), `vanished` (absent before *and* after — it cancelled out) or
`unbaselined` (a read-only run took no snapshot). Promotion prints the same list,
naming protected files separately, so a ratification record is readable.

## Tests

```bash
python -m pytest            # the runtime's specification
python -m pytest -n logical # in parallel (pip install -e '.[dev]')
```

Use `-n logical`, not `-n auto`. `auto` asks xdist how many workers to start and
xdist answers with `psutil.cpu_count(logical=False)` whenever psutil happens to
be importable — *physical* cores. On a two-vCPU guest that reports one physical
core, `auto` means one worker and the suite is no faster than serial. Measured
here: `-n auto` 78s, `-n 2` 42s, three runs of each, every time.

The `tests` gate sidesteps the question by counting for itself with
`sched_getaffinity` and passing an explicit `-n`, which also respects cgroup and
affinity limits that `os.cpu_count` ignores. It probes the interpreter it is
about to run pytest under and degrades to serial when `pytest-xdist` is not
importable, because a machine without the dev extra must still be able to verify
the tree. `[verify].parallel` pins it: `-1` count the CPUs, `0` serial, `N`
exactly N. The verdict names which it used.

`.github/workflows/ci.yml` adds nothing of its own: it installs the toolchain and
runs `bootstrap.py --prove --strict`, `bootstrap.py verify`, the suite in
parallel, and a real self-engineering plan. If a change touched guardrails
without a ratified amendment, CI fails at the cold start.

The suite copies the repository into a temporary directory and lets the runtime
modify *that*: it adds skills to itself, tampers with its own guardrails, breaks
its own build, and has each change judged. The real tree is never written to.

## Layout of a change

```
plan  →  kernel loop  →  policy set  →  journal  →  skill  →  journal
                                                          ↓
                                            snapshot (before first mutation)
                                                          ↓
                                            verification suite
                                                     ↙          ↘
                                            green                red
                                              ↓                   ↓
                                     promote: lock++,        rollback,
                                     git commit,             amendment
                                     amend ratified          rejected
```

## License

MIT.
