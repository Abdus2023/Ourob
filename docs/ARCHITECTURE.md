# Architecture

The design question was not "can a program edit its own source" — any program with
a file handle can. It was: **what has to be true for that to be an engineering
discipline instead of a hazard?**

Five answers, in the order they bind.

---

## 1. Everything is a file in the repository it modifies

There is no installed runtime, no registry, no sidecar service. `src/ourob/` is
the runtime; `bootstrap.py` puts it on `sys.path`; `bootstrap.lock.json` says what
"the runtime" consists of. The consequences are structural:

- A change to the runtime *is* a change to the repository, so it goes through
  whatever process the repository already has.
- The runtime can read its own source with the same skill it uses to read
  anything else. There is no privileged introspection channel.
- A cold start needs nothing but a checkout and a Python interpreter.

## 2. The deciding part is isolated

Exactly one component chooses what to do next: the `Planner`. Everything else is
deterministic machinery. Two planners ship:

- `ScriptedPlanner` — replays a JSON plan from `plans/`. Reproducible, diffable,
  reviewable before it runs. This is what every test and every demo uses.
- `OpenAIPlanner` — optional, needs `httpx` and `OUROB_API_KEY`.

The safety machinery does not know or care which one is driving. An LLM's
invocation is judged by the same policies, journalled the same way, and gated the
same way as a scripted one. That separation is what makes the LLM planner
optional rather than load-bearing.

The planner sees a `RuntimeView`: the goal, the skill catalogue *with parameter
schemas*, the protected paths, the policy names, the remaining budget, and a
transcript of what has happened so far. It sees no more than that.

## 3. Judgement happens before execution, and is recorded either way

```
planner ──► Invocation ──► PolicySet ──► journal ──► skill ──► journal
```

`PolicySet.review` is deny-wins across eight rules. Every verdict — **including
the allows** — is written to the journal, so after the fact you can reconstruct
not just what the runtime did but what it was permitted to do and by which rule.

Policies are small on purpose. Each one inspects a single property of a single
call. They have no memory of each other and cannot negotiate. A policy that
raises an exception becomes a denial; the failure direction is always "no".

Two of the eight deserve a note:

- **`journal-integrity`** forbids writes to `.ourob/journal`, `.ourob/verify` and
  `.ourob/index.jsonl`. The record of what the runtime did must be harder to
  alter than the thing it records, or the record is worthless.
- **`path-key-coverage`** is advisory and exists because the confinement policy
  only inspects arguments whose *key* is in `PATH_KEYS`. A newly written skill
  could invent a key like `destination_file` and slip past. Rather than pretend
  that hole does not exist, it is detected and reported.

## 4. History is append-only and tamper-evident

Each journal line carries the SHA-256 of the line before it. That catches edits
and reordering. It does **not** catch truncation — dropping the tail still leaves
a valid prefix — so each journal also has a `.head` anchor naming the last
sequence number and hash, rewritten atomically on every append. A log shorter
than its anchor has been truncated.

`ourob journal --check` verifies every chain. `ourob show <run>` reconstructs a
`Run` object from its log alone, with no other state.

## 5. Acceptance is a separate act from execution

Running a plan leaves **drift**. Drift is not a version of the runtime. Only
`ourob promote` makes a change official, and it does five things in order:

1. diff the tree against the last ratified lock;
2. require an amendment for any protected path that drifted;
3. run the verification suite;
4. on failure — roll the tree back to the pre-run snapshot and mark the amendment
   rejected, leaving the repository byte-identical to how it started;
5. on success — rewrite the lock, commit, ratify the amendment.

The snapshot is taken by the kernel *before the first mutating step* and is
deliberately not git. Git is a good place to record a ratified change; rollback
has to work in a bare checkout with no history, and it has to be exact about
files a self-modifying process just invented. The snapshot also carries a copy of
the lock itself — rolling back must not un-anchor the tree.

**Rollback is exact, which means it is not selective.** `ourob snapshots
--rollback <run>` restores every file the snapshot recorded and deletes every
file it did not. Anything written after the snapshot was taken goes with it,
including unrelated work. That is the correct behaviour for "put the tree back the
way it was", but it is a sharp edge: if you have been editing while a failed run
sits un-rolled-back, roll back before you continue, not after.

---

## Why the verifier re-reads the config out of process

`policy-integrity` shells out to a fresh interpreter and asks it to load
`ourob.toml` and report the protected list, then checks that the list still covers
`bootstrap.py`, `src/ourob/bootstrap/` and `src/ourob/policies/`.

Without that gate, a single edit to `ourob.toml` could legalise editing
`ourob.toml`, and the whole scheme would be one `write_file` away from
self-lobotomy. The gate is the fixed point.

Relatedly, `[verify].blocking_gates` can escalate an advisory gate to blocking,
but nothing can demote a gate that is blocking in code. Configuration can make
the runtime stricter. It cannot make it laxer.

## Why skills are discovered rather than declared

`skills/builtin/` is imported as a package; `skills/contrib/` is imported by file
path with `importlib`. Adding a capability is therefore *writing a file* — no
registration call, no table to edit, no install step. A skill written during a run
is live on the next discovery pass.

Discovery returns problems rather than raising, because a half-written contrib
file must not stop the runtime from booting. It should stop the runtime from
*using* that skill. Duplicate names are refused and the built-in wins.

## Failure directions

Every component has a chosen direction, and they are all the same:

| component | on failure |
|---|---|
| policy | deny |
| gate | fail |
| skill discovery | quarantine the file, keep the rest |
| planner | record the error, end the run |
| bootstrap | refuse to boot |
| promotion | roll back |

Nothing in the runtime treats an unexpected exception as permission.

## What is deliberately *not* here

- **No sandboxing of child processes.** `run_python` and `run_command` run with
  the runtime's own privileges, confined only by working directory, a stripped
  environment, an allowlist and a timeout. Real isolation would need an OS-level
  mechanism (namespaces, seccomp, a container) and pretending otherwise would be
  worse than saying so.
- **No concurrent runs.** One kernel, one loop, one journal at a time.
- **No semantic understanding of changes.** The gates check that the tree
  compiles, imports, lints, passes tests, cold-starts and has not silently
  weakened its guardrails. They cannot tell you a change is a *good idea*.
