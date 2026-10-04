# Constitution

These are the rules for changing the parts of `ourob` that decide what changing
`ourob` is allowed to look like.

They exist because of one specific failure mode: a system that can edit its own
guardrails has no guardrails. Editing `src/ourob/policies/rules.py` to delete the
policy that protects `src/ourob/policies/rules.py` is a one-line change, and
nothing about "the runtime can rewrite itself" prevents it. So that edit is not
an engineering act. It is a constitutional one, and it has a procedure.

## Article 1 — Protected paths

The following may not be written by an ordinary mutating skill during a run
unless a current operator grant matches the proposal, base revision, lock digest,
and exact paths. The protected list lives in `ourob.toml`:

```
ourob.toml                 the configuration itself
bootstrap.py               the root of trust
bootstrap.lock.json        the self-description
src/ourob/bootstrap/       cold start, manifest, snapshots, amendments, promotion
src/ourob/policies/        the guardrails
src/ourob/verify/          the gates
```

`.ourob` is non-amendable runtime state and is denied to ordinary mutating file
skills. Child processes are separately denied content/namespace writes to
`.ourob` and `.git` by the Linux filesystem boundary; this does not mediate
file-mode or timestamp changes. `bootstrap.py` and `bootstrap.lock.json` are
*additionally* hard-coded into the bootstrap script's own protected list.
Removing them from `ourob.toml` does not unprotect them.

## Article 2 — Amendments

An amendment proposal is a record with an id, a path list, a rationale, a
proposer, a base revision, a base lock digest, and a status. It is opened with:

```bash
python bootstrap.py amend src/ourob/policies/ --why "lower the retry threshold"
```

or, from inside a run, with the `propose_amendment` skill. The proposal skill
creates a request; a proposal file by itself never grants authority.

A separate operator action is required:

```bash
python bootstrap.py authorize <amendment-id> --by "operator identity"
```

The command requires an interactive terminal and an exact typed proposal ID. It
shows and pins the proposal SHA-256, revision, lock digest, and paths, then
records a separate `amendment.authorized` event in the system journal. The `--by`
value is self-asserted, not cryptographic proof of a human identity. The grant is
invalid if the proposal changes, the base revision or lock changes, the path set
differs, history is corrupt, or the proposal has already reached a terminal state.

Statuses:

- `proposed` — a request only; it does not permit writes without a distinct valid
  operator grant in system history.
- `ratified` — the amended tree passed verification and promotion; evidence and a
  terminal event close the grant.
- `rejected` — verification failed, or the change was declined; the grant is
  closed.

An amendment's scope is exactly the paths it names. A directory pattern
(`src/ourob/policies/`) covers everything under it and nothing beside it.

## Article 3 — Ratification requires green gates

```bash
python bootstrap.py ratify <amendment-id> --message "lower MAX_REPEATS"
```

runs the full suite. If any blocking gate fails, the amendment is marked
`rejected`, the evidence (gate names and report digest) is recorded against it,
and the tree is rolled back to the pre-run snapshot — including deleting files the
run created. The repository ends exactly as it began, and there is a record of
why.

There is no `--force`. There is `--no-rollback`, which keeps a failing tree for
inspection and still refuses to promote it.

## Article 4 — The guardrail check is independent

The `policy-integrity` gate re-reads `ourob.toml` in a **fresh interpreter** and
verifies that the protected list still covers the bootstrap, the policy engine and
the verifier. It does not consult the in-memory configuration, because the
in-memory configuration is the thing under suspicion.

This is the fixed point of the whole scheme. If it ever fails, the tree cannot be
promoted, cannot cold-start by default, and cannot be presented as a ratified
version of the runtime.

## Article 5 — History is not editable

Ordinary mutating file skills are denied access to `.ourob`, including run and
system journals, `.head` anchors, promotion history, verification reports, and
amendment proposals. Journal appends refuse corrupt chains; each JSONL file is
hash-chained and has a `.head` anchor so tail truncation, deletion, malformed
anchors, and content changes are detected. System-event replay also rejects
impossible authorization and promotion transitions. `ourob journal --check`
verifies these checks.

On supported Linux hosts, child processes and descendants receive a Landlock
write boundary that excludes `.ourob`, `.git`, and configured protected paths;
unsupported hosts refuse child execution. This is a narrow child-write control,
not an OS-level security boundary against arbitrary in-process Python, a user
with checkout access, or host-level tampering. If you need to correct the record,
add to it.

## Article 6 — Escalation, never demotion

`[verify].blocking_gates` in `ourob.toml` can promote an advisory gate to
blocking. Nothing can demote a gate that is declared blocking in code, because
the code is where "blocking" is defined and the code is protected.

## Article 7 — Drift is not a version

A tree that differs from `bootstrap.lock.json` is drift. Drift does not update the
lock, is not a ratified version, and does not boot by default:

```
$ python bootstrap.py doctor
  ...
BOOT FAILED: protected paths have drifted from bootstrap.lock.json
```

Ordinary engineering drift — new skills, new tests, new documentation — boots
fine and is reported. Only *protected* drift blocks a cold start, and only until
it is either amended and promoted, or explicitly accepted with `--trust-drift`.

---

## The honest limits

- An operator with shell access can edit anything. The constitution constrains the
  *runtime*, not the person running it.
- `--trust-drift` and `--rebuild` exist and are sometimes the right answer. They
  print what they are doing.
- The gates check that the tree works. They cannot check that a change is wise.
  Article 3 makes an unwise change expensive; it does not make it impossible.
