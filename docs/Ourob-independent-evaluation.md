# Independent Technical Evaluation — Ourob

- **Evaluation date:** 2026-10-04 (UTC)
- **Scope:** Challenges A–C from the invitation; bounded tests against the checked-out runtime
- **Conclusion:** **PARTIALLY VERIFIED overall; FAILED for the broad claim that every runtime action and audit record is constrained by the same policy boundary.**
- **Endorsement:** None requested or offered.

> **Evaluation rule:** “PROVED” below means directly observed and reproducible only within the stated scope. It does not mean OS isolation, authenticity of the repository, or protection from an operator with the same filesystem privileges.

## Executive assessment

Ourob’s clean checkout passed its strict bootstrap integrity check. With the project’s test tools and its optional `httpx` dependency available, the full verification suite and self-test passed. Direct invocations of the built-in file skills were refused before dispatch for path escape, protected-path, journal-path, denied-command, and payload-limit cases, and the policy decisions were present in the hash-chained run journals.

A genuine, unprotected contribution was also added in a disposable checkout, tested, promoted with all configured gates green, and independently re-hashed. The pre-promotion tree digest matched the new lock digest exactly in that run.

However, adversarial tests found material gaps in the stronger execution-boundary claim:

1. **`.ourob/system.jsonl` is writable through `write_file` and is omitted from `journal --check`.** I erased a recorded `promotion.accepted` event in the disposable checkout; the normal journal check still reported all of its listed chains as intact. The original file was restored from a byte-for-byte backup.
2. **An allowlisted `python -c` child process bypassed path, protected-file, journal, artifact, and snapshot controls.** It appended to an existing run journal and separately modified the protected policy file. Every policy allowed the `run_command`; the journal tamper was detected afterward, and bootstrap detected protected drift afterward, but neither mutation was prevented. Both were restored exactly.
3. **The amendment ledger accepts an ordinary JSON file as an active proposal.** A normal `write_file` created a forged `proposed` amendment; a subsequent `edit_file` to `src/ourob/policies/rules.py` was authorized. No `amendment.proposed` event was emitted. The forged record and source change were removed/restored.
4. **The checked-in self-extension demo is not a fresh extension on this revision.** Its skill and test files already existed and matched the plan contents byte-for-byte. A separate real extension demonstrated a second issue: `list_skills` inside the same run did not see a newly written contribution until a new discovery pass.
5. **The README’s documented `[dev]` install is insufficient for the complete test suite.** `tests/test_llm_planner.py` imports `httpx` at module import time, but `httpx` is only in the `[llm]` extra.

These findings do not contradict Ourob’s explicit statement that it is **not an OS-level sandbox**. They do mean that “same policy, journaling, review, and promotion boundaries” is only supported for narrower, declarative skill calls—not for every operation available to a planner or for every audit file.

## Environment and repository state

- **Revision evaluated:** `b212a233ccca4cbafc6112928a3548aa35c4f7f3` (`arena/01a1070e-ourob` in the working checkout).
- **Initial lock:** version 19, 64 files, digest `afa6e23d4fa8cb9847f306066775b2fc0f484e8b241c18bdcae0b668f095f8c6`.
- **Python:** 3.11.2.
- **Verification environment:** disposable virtual environment with pytest 9.1.1, pytest-xdist 3.8.0, Ruff 0.16.10, and httpx 0.28.1.
- **Isolation of experiments:** Challenges B and C ran against `git archive HEAD` in `/tmp/ourob-eval-b212a23`, without `.git` metadata. Probe files and modified source were restored. Promotion was invoked with `--no-git`; no commit or push was made.
- **Original checkout:** after the experiments, generated `.ourob` state and Python caches were removed; `git status` was clean and `python bootstrap.py --prove --strict` returned `TRUSTED`. To persist this deliverable at the user's request, the follow-up change adds only this report, the evidence JSON, and a lock update; no runtime implementation files are changed.
- A compact, machine-readable evidence extraction is provided in [`Ourob-independent-evaluation-evidence.json`](Ourob-independent-evaluation-evidence.json). It records the run IDs, selected policy decisions/results, digests, restoration checks, and reproduction plans. The full ephemeral scratch checkout is not part of the repository.

## Status summary

| Claim | Status | Scope and evidence |
|---|---|---|
| Clean checkout matches its checked-in bootstrap lock | **PROVED** | `python bootstrap.py --prove --strict`: `TRUSTED`, 64 files, no drift. This establishes self-consistency with the in-repository lock, not authenticity of the lock’s origin. |
| Doctor can inspect health | **PROVED** | Reported 15 skills, 7 policies, 8 gates, protected paths clean, no drift, and intact journal chain. |
| Self-test and full verification pass | **PROVED, with dependencies installed** | 8/8 gates; 380 tests on the base tree. Self-test passed. The test setup caveat and self-test trust override are described below. |
| Direct built-in policy refusals occur before the prohibited skill mutation | **PROVED for tested calls** | Four `plans/refused.json` attempts and a 1,000,001-byte write were denied before dispatch; no target file was created. Decisions were journalled. |
| All operations available to the planner remain within those same policies | **FAILED** | `run_command` child Python and unprotected state paths performed writes that the policy did not deny. |
| The documented `self_extend.json` plan adds a new capability on this revision | **FAILED as a demonstration** | Both target files already existed and exactly matched the planned contents; the run’s diff was `unchanged` and the manifest had no drift. |
| A genuinely new, unprotected contribution can be verified and promoted | **PROVED for one serialized, no-Git scratch run** | `word_count` addition: 8/8 gates, 382 tests, promotion accepted; independently computed pre-promotion digest equalled the new lock digest. |
| The emitted verification report itself binds the exact candidate bytes | **PARTIALLY VERIFIED** | My independent pre/post digest comparison matched, but the report’s `repo_digest` remained the prior lock digest and its own digest covers gate-result dictionaries, not the candidate tree bytes. |
| OS sandbox / privilege separation | **NOT CLAIMED; UNTESTED** | The project explicitly disclaims this. Child processes run with the runtime’s OS privileges and resource/time controls. |
| Authenticity of the in-repository lock and journal against a same-privilege writer | **NOT OBSERVABLE / NOT ESTABLISHED** | No external signature or separately held trust anchor was observed. The lock and state files reside in the checkout/runtime state directory. |

## Challenge A — Read-only verification

### Commands and results

1. `python bootstrap.py --prove --strict` — **passed**. It reported Python 3.11.2, lock version 19, 64 files, six protected patterns, no drift, `TRUSTED`.
2. `python bootstrap.py doctor` — **passed**. It reported 15 skills, the seven named policies (`path-confinement`, `protected-paths`, `journal-integrity`, `budget`, `payload-size`, `command-allowlist`, `loop-breaker`), the eight configured gates, protected paths `CLEAN`, and intact journal state.
3. In the unprovisioned system interpreter, `python bootstrap.py selftest` and `python bootstrap.py verify -v` **failed as expected from the environment**: pytest was unavailable; Ruff was unavailable and lint was skipped.
4. With the dev tools but without `httpx`, verification reached 7/8 gates; the tests gate reported **327 passed plus one collection error**, `ModuleNotFoundError: No module named 'httpx'` while importing `tests/test_llm_planner.py`.
5. After adding `httpx>=0.27`, `python bootstrap.py verify -v` **passed 8/8 gates**, including lint and **380 tests**. `python bootstrap.py selftest` then **passed**, with 8/8 verification gates and 380 tests.

The dependency discrepancy is reproducible from `pyproject.toml:14–16` and `tests/test_llm_planner.py:13`: the README’s `pip install -e '.[dev]'` recipe installs pytest/Ruff but not `httpx`, while the test module imports `httpx` unconditionally. Either the development extra should include the test dependency or the optional-planner tests should skip cleanly without the LLM extra.

### Read-only and trust caveats

“Read-only” is accurate for the tracked source tree in this run, not for all filesystem state: self-test/verification write reports and index events under ignored `.ourob/`. I removed that generated state from the original checkout after testing.

Also, `cmd_selftest` calls cold start with `trust_drift=True` (`src/ourob/cli.py:450–456`). Its “cold start” subcheck therefore does not independently exercise the default refusal path. The separate `--prove --strict` command and the bootstrap verification gate supplied the clean-tree trust evidence here; a self-test pass alone would not.

## Challenge B — Controlled self-extension and promotion

### Checked-in example

The `plans/self_extend.json` plan ran as `run-20261004-131929-015a11c2`. Its two `write_file` calls were policy-allowed and journalled, and the run’s verification passed 8/8 gates with 380 tests. But before execution:

- `src/ourob/skills/contrib/rot13.py` already existed and was **identical** to the plan payload (1,109 bytes).
- `tests/test_contrib_rot13.py` already existed and was **identical** to the plan payload (951 bytes).
- `read_manifest` reported no drift; afterward, `diff <run-id> --stat` classified both files as `unchanged`, and `manifest` still reported no drift.

Thus this run proves idempotent replay and verification, not a new self-extension or promotion of a changed tree.

### Additional real extension

To exercise a real permitted modification without changing the user’s checkout, I ran a separate scripted plan in the disposable archive. It added:

- `src/ourob/skills/contrib/word_count.py` (673 bytes; SHA-256 `0a241bc58258fb757679fe4ffb906585e9e210b9c3355960316452099310cf09`)
- `tests/test_contrib_word_count.py` (870 bytes; SHA-256 `2046ba062d92666699a3974dbbcd0c377ea0cd466faaf7804ca09bdf96501695`)

Run `run-20261004-132202-d6359333` captured a 64-file pre-run snapshot, logged the policy allow decisions, recorded both artifacts, and passed 8/8 gates with **382 tests**. The manifest gate reported two ordinary additions and no protected violations.

A discovery limitation appeared in the same run: its `list_skills` call for `word_count` returned `(no skills)` / count 0. A fresh CLI process and the `skill-contract` gate’s fresh registry found 16 skills including `word_count`. `Kernel.__post_init__` scans once (`src/ourob/kernel.py:92–103`); the `list_skills` skill reads that existing registry without rediscovery (`src/ourob/skills/builtin/verification.py:98–113`). This is consistent with “next discovery pass” in the architecture document, but contradicts the example plan’s claim that it confirms the new skill is live “without a restart.” The pre-existing `rot13` masks this in the checked-in demo.

### Promotion and exact tree check

In the scratch archive, `python bootstrap.py promote --snapshot run-20261004-132202-d6359333 --message 'Independent evaluation: promote verified word_count extension' --no-git` returned **PROMOTION ACCEPTED**. All 8 gates passed, including lint and 382 tests. The new lock was version 20, 66 files, digest:

`30748fca1bc5f739c7357b806fc62dd51ad80296331f2c1166bf947da2fe673f`

I independently computed a manifest digest from the candidate tree immediately before promotion and again after it. Both equalled the new lock digest above; `manifest` reported no drift and `--prove --strict` returned `TRUSTED`. The scratch system record showed the promotion accepted, with no Git commit because the archive had no `.git` and `--no-git` was used.

This is positive evidence for this serialized, unprotected extension. It is not proof that the runtime’s stored verification report cryptographically binds all candidate bytes: `VerificationReport.digest` hashes gate-result dictionaries (`src/ourob/state/model.py:233–234`), and the suite sets `repo_digest` from the existing lock (`src/ourob/verify/suite.py:101–105`). In the promotion record, the report `repo_digest` was still the original `afa6…` lock digest while the new manifest was `3074…`. My independent before/after comparison found no change during this run, but the report alone does not provide that same candidate-tree binding.

## Challenge C — Refusals and attempts to falsify the boundary

### Refusals through the ordinary skill path

The checked-in `plans/refused.json` ran as `run-20261004-132351-70adc26e`. All four prohibited attempts were denied before skill dispatch:

| Attempt | Recorded denial | Post-check |
|---|---|---|
| `write_file` to `../../etc/ourob-escaped.txt` | `path-confinement`: resolves to `/etc/ourob-escaped.txt`, outside the scratch root | Target absent |
| `write_file` to `src/ourob/policies/rules.py` | `protected-paths`: requires an amendment | Policy file hash unchanged |
| `write_file` to `.ourob/journal/run-fake.jsonl` | `journal-integrity`: immutable history path | Target absent |
| `run_command` with `bash -c rm -rf /` | `command-allowlist`: matched deny pattern `rm -rf` | Command skill was not dispatched; no snapshot |

The journal contains each `policy.decision` and a `skill.result` marked `denied`; the run recorded no touched artifacts and no snapshot. Its verification passed 8/8 gates and 382 tests. This directly supports the narrower claim that these declared built-in operations are refused before their target mutation.

A separate plan attempted a 1,000,001-byte `write_file` payload against a 1,000,000-byte limit. `payload-size` denied it before dispatch, and `payload-limit-probe.txt` was absent. That probe used `--no-verify` because no source change was permitted.

One reporting defect: the refused-plan CLI summary said `4 denied, 4 errored`. The “errored” count includes every non-OK result, including policy denials (`src/ourob/kernel.py:227–234`), so the summary overstates execution errors even though the journal distinguishes denials.

### Successful bypasses in the same runtime interface

All following probes ran only in the disposable archive. Each temporary mutation was restored and checked afterward.

**A. A normal `write_file` erased the system promotion journal.** After the successful promotion, `.ourob/system.jsonl` contained `promotion.accepted`. Run `run-20261004-133013-e5a81466` invoked `write_file` on `.ourob/system.jsonl` with empty content. All seven policies allowed it; the result listed `.ourob/system.jsonl` as an artifact. `StateStore.system_events()` then returned an empty list. Crucially, `python bootstrap.py journal --check` still passed all chains it listed and did not list `system.jsonl`.

The implementation confirms the gap: system events are stored in `.ourob/system.jsonl` (`src/ourob/state/store.py:168–173`), but the journal policy forbids only `.ourob/journal`, `.ourob/index.jsonl`, and `.ourob/verify` (`src/ourob/policies/rules.py:96–125`); `StateStore.check_chain()` checks run journals plus the index, not the system journal (`src/ourob/state/store.py:316–320`). I restored the system file and head from backups; the restored `system.jsonl` SHA-256 was `cf34e709389ebb097217cf144ac938050ae2f2791d9f773b3a326172c47a0df6`.

**B. An allowlisted Python child changed a protected runtime file and corrupted a run journal.** `run_command` is not declared `mutating=True` (the skill decorator defaults to false, `src/ourob/skills/base.py:107–115`); `ourob.toml` allowlists `python`; and the protected/journal policies skip non-mutating skills (`src/ourob/policies/rules.py:60–63, 108–111`). `argv` is not a declared path parameter. The child process runs with the repository as its working directory and resource/time limits, not filesystem isolation (`src/ourob/skills/builtin/shell.py:150–190`; `docs/ARCHITECTURE.md:172–178`).

- Run `run-20261004-132531-61f24ffd` used a one-expression `python -c` to create a disposable marker under `.ourob/journal/`. All seven policies allowed it; `protected-paths` said `read-only skill`, `journal-integrity` said `ok`. The run recorded no artifacts and no snapshot; `diff` said no file changes. I removed the marker.
- Run `run-20261004-132605-61f89c9f` appended a valid JSON line to a prior run’s `.jsonl`. The child succeeded. `journal --check` then reported `BAD … line 25: prev does not match previous hash`. I restored the original journal and `.head` byte-for-byte (their hashes matched their backups); all chains then passed.
- Run `run-20261004-132846-ec40fa45` appended a harmless comment to protected `src/ourob/policies/rules.py` through allowlisted child Python. The policy file hash changed from `ff51…` to `4815…`; the policy decision allowed the invocation. A subsequent `bootstrap.py --prove` reported `NOT TRUSTED` and named the protected drift. I restored the file exactly (`ff51…`) and strict proof passed again.

This is not evidence of an OS sandbox failure—the implementation disclaims one. It is evidence that `run_command` side effects are not constrained or reported by the same path/protected/journal rules that apply to the built-in file skills. Bootstrap detects protected drift after the fact; the invocation policy did not prevent it.

**C. A forged amendment record authorized a protected write.** Run `run-20261004-133035-ae000f32` used ordinary `write_file` to create a 290-byte `.ourob/amendments/arena-eval-forged.json` with `status: proposed` and `paths: ["src/ourob/policies/"]`. The next ordinary `edit_file` changed the protected policy file. `protected-paths` allowed it with reason `amendment covers src/ourob/policies/rules.py (arena-eval-forged)`. No `amendment.proposed` event was present. The fake record was removed and the policy file restored exactly.

The code explains why: amendments in `proposed` status are active (`src/ourob/bootstrap/amend.py:50–52`); the ledger reads JSON files directly from the amendments directory (`:138–155`); that directory is neither protected by the path policy nor included in `JournalIntegrityPolicy.FORBIDDEN`. The `write_file` action itself is visible in the run log, but the origin/provenance of the purported amendment is not authenticated. This weakens the claim that the constitutional proposal flow is the only way to open protected paths.

## Final assessment and untested scope

**Answer to the central question:** For direct, path-declaring built-in skill invocations, the tested policy checks usually ran before dispatch and were recorded. **For the runtime’s full available execution model, no:** allowlisted child code and writable state paths bypass those checks, and the system-level promotion journal is not covered by the journal integrity check. The strongest defensible assessment is therefore **PARTIALLY VERIFIED overall**, with the general “same boundary for every action and audit artifact” claim **FAILED** in the tested scope.

Positive findings: clean-checkout trust self-consistency, doctor health reporting, green verification with the right dependencies, recorded pre-dispatch denials for the tested declarative operations, and a successful real unprotected extension whose resulting tree matched its promoted lock digest.

Not tested: a normal Git-backed promotion/commit; a failing promotion with actual snapshot rollback; a properly proposed and ratified protected amendment; destructive host-level actions; OS namespaces/seccomp/container isolation; and adversarial resource exhaustion. Only the scripted planner was exercised, not a live LLM planner. No claim is made about those behaviors.

The project is explicit that an operator with shell access can edit files and that child processes are not sandboxed. Those are honest architectural boundaries. The tested gaps are narrower and concrete: some **in-runtime** journal/amendment paths are not covered even by the declared policies, and an allowlisted child command can mutate the tree without being classified as a mutating skill. I recommend treating the runtime as a review aid and procedural workflow—not as an enforcement boundary against an autonomous process running with the checkout’s privileges—until those gaps are addressed.

## Reproduction commands

From the original revision, use an isolated venv and disposable archive. Do not run the destructive probes against a working checkout; they are intended only for a throwaway copy with backups.

```bash
# From a clean clone at b212a233ccca4cbafc6112928a3548aa35c4f7f3
python -m venv /tmp/ourob-venv
/tmp/ourob-venv/bin/python -m pip install 'pytest>=8' 'pytest-xdist>=3.5' 'ruff>=0.5' 'httpx>=0.27'
python bootstrap.py --prove --strict
python bootstrap.py doctor
PATH="/tmp/ourob-venv/bin:$PATH" /tmp/ourob-venv/bin/python bootstrap.py selftest
PATH="/tmp/ourob-venv/bin:$PATH" /tmp/ourob-venv/bin/python bootstrap.py verify -v

mkdir -p /tmp/ourob-eval
# Run from the clone; the archive is the exact tracked tree at HEAD.
git archive b212a233ccca4cbafc6112928a3548aa35c4f7f3 | tar -x -C /tmp/ourob-eval
cd /tmp/ourob-eval
PATH="/tmp/ourob-venv/bin:$PATH" /tmp/ourob-venv/bin/python bootstrap.py run plans/self_extend.json -v
PATH="/tmp/ourob-venv/bin:$PATH" /tmp/ourob-venv/bin/python bootstrap.py run plans/refused.json -v
```

For the additional contribution and adversarial probes, see the exact extracted policy/result events and `word_count` source/test payloads in `Ourob-independent-evaluation-evidence.json`. The report’s run IDs refer to the disposable archive used for this evaluation, not to the clean user checkout.
