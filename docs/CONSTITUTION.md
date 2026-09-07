# Constitution

These are the rules for changing the parts of `ourob` that decide what changing
`ourob` is allowed to look like.

They exist because of one specific failure mode: a system that can edit its own
guardrails has no guardrails. Editing `src/ourob/policies/rules.py` to delete the
policy that protects `src/ourob/policies/rules.py` is a one-line change, and
nothing about "the runtime can rewrite itself" prevents it. So that edit is not
an engineering act. It is a constitutional one, and it has a procedure.

## Article 1 — Protected paths

The following may not be written by a skill during a run unless an active
amendment names them. The list lives in `ourob.toml`:

```
ourob.toml                 the configuration itself
bootstrap.py               the root of trust
bootstrap.lock.json        the self-description
src/ourob/bootstrap/       cold start, manifest, snapshots, amendments, promotion
src/ourob/policies/        the guardrails
src/ourob/verify/          the gates
```

`bootstrap.py` and `bootstrap.lock.json` are *additionally* hard-coded into the
bootstrap script's own protected list. Removing them from `ourob.toml` does not
unprotect them. There is no configuration that makes the bootstrap unable to
defend itself.

## Article 2 — Amendments

An amendment is a record with an id, a path list, a rationale, a proposer and a
status. It is opened with:

```bash
python bootstrap.py amend src/ourob/policies/ --why "lower the retry threshold"
```

or, from inside a run, with the `propose_amendment` skill — which is itself an
ordinary skill subject to every other policy, and whose invocation is journalled.

An amendment authorises *writes*. It does not accept anything. Statuses:

- `proposed` — writes to the named paths are now permitted during a run.
- `ratified` — the amended tree passed the full verification suite and was
  promoted. The lock was rewritten and the amendment closed.
- `rejected` — verification failed, or the change was declined.

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

No skill may write to `.ourob/journal`, `.ourob/verify` or `.ourob/index.jsonl`.
The journal is append-only and hash-chained, with a `.head` anchor so truncation
is detectable as well as editing. `ourob journal --check` verifies every chain.

If you need to correct the record, add to it.

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
