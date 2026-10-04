# Ourob Remediation Evaluation

- **Evaluation date:** 2026-10-04
- **Branch:** `arena/01a1070e-ourob`
- **Requested scope:** independently remediate and adversarially re-verify Result 001 findings against Issue #7 acceptance criteria R-01–R-06
- **Overall result:** **PARTIALLY_VERIFIED**
- **Endorsement:** none offered; this is not a third-party security audit
- **Machine-readable evidence:** [Ourob-remediation-evaluation-evidence.json](Ourob-remediation-evaluation-evidence.json)

> **Evidence rule:** no evidence, no verified claim. “Prevented” means the attempted mutation was denied before the protected artifact changed. Detection after mutation is not prevention. Application policy, child-process filesystem controls, and general OS isolation are distinct claims.

## Scope and revision boundary

The requested Result 001 revision, `71464d365c41da9c74ed3f943d031b135a316beb`, was not present during the initial local inspection. It was subsequently fetched from `origin/arena/01a1070e-ourob`; the remediation branch now descends from that exact commit, whose parent is `b212a233ccca4cbafc6112928a3548aa35c4f7f3`. We created a disposable archive from the requested SHA and reproduced the baseline mutation failures there. The requested revision is now available, and the baseline failures were directly reproduced there.

The historical Result 001 files were preserved byte-for-byte:

- `docs/Ourob-independent-evaluation.md` — SHA-256 `d19eb4e122f8ed4096a9a82f92d627ae24a119b64668bedc3f751a35d12a74d9`
- `docs/Ourob-independent-evaluation-evidence.json` — SHA-256 `bf4d52475045a24d4dede58b1efcdaa05a81c1f35ca67f094b26260d8bf1858f`

The detailed baseline probes and challenge-by-challenge evidence are in the JSON file. Baseline reproduction ran from a `git archive` of the exact requested commit:

| Finding | Exact-revision observation |
|---|---|
| R-01 — child mutation | Run `run-20261004-181154-10b6cbbd` completed with 0 denials; protected `rules.py` SHA-256 changed from `ff51eca1…` to `7f78a94b…`. |
| R-02 — child journal | Run `run-20261004-181154-feca02fb` appended to a prior run journal; its hash changed and chain check failed at line 10 (`prev` mismatch). |
| R-02 — system history path | Run `run-20261004-181154-63c4a7cf` used ordinary `write_file` to truncate a 30-byte `.ourob/system.jsonl` sentinel from 30 bytes to 0; baseline checks did not include that path. The sentinel was not asserted to be semantically valid promotion history. |
| R-03 — forged amendment | Run `run-20261004-181155-f9e547c5` created a proposal-shaped file then changed protected `rules.py`; both operations were allowed and denials were 0. |
| R-04/R-06 — discovery/example | `rot13.py`, its test, and the old `plans/self_extend.json` target were already present; the old example was not a fresh extension. |
| R-05 — clean development install | A fresh `[dev]` install at the requested commit left `httpx` absent; pytest collection exited 2 with `ModuleNotFoundError: httpx` (327 collected, one collection error). |

The baseline archive and clean development environment are identified in the evidence JSON. These observations reproduce the baseline failures at `71464d…`, not just at its parent.

## Remediation evidence

### R-01 — child-process mutation of protected paths

Two control layers were tested separately:

1. **Application policy:** direct declared `write_file`, `edit_file`, and `delete_file` attempts against protected paths are denied before dispatch/mutation in the retained regressions.
2. **Child-process boundary:** on the tested Linux host, Landlock rules are installed before `exec` and descendants inherit them. The rules block the tested protected content/namespace writes; a narrow seccomp filter denies selected ownership/xattr syscalls. This is a child-process boundary, not a general OS sandbox.

In disposable Kernel run `run-20261004-180325-0bad73cd`, `run_python` was **allowed to dispatch** because the `child-filesystem` policy found Landlock ABI 2. The child's protected content write then failed with `EACCES` (errno 13); the protected file SHA-256 was unchanged. An ordinary `docs/` control write succeeded. The run journal had 10 events with an intact chain; the index chain was intact and system-event semantics were intact.

The adversarial child regression covers relative and absolute paths, traversal, symlinked file and directory aliases, unlink/delete, creation under a protected directory, rename, replace, hard-link, and an ordinary-write control. It also records an important limit rather than hiding it: `chmod` and `utime` are allowed. A separate disposable `run_child` probe changed `src/ourob/policies/rules.py` from mode `0644` to `0600` and set its mtime to `1600000000000000000` ns. The content SHA-256 remained `d24ac572e5f14b1fdf244bedc6c6b581698eb6860860293fa8ec3ac33f2f8812`; the disposable parent restored the original mode and timestamps. The retained pytest regression asserts both metadata operations succeed and restores its fixture afterward.

**Result: PARTIALLY_VERIFIED.** Tested content/namespace mutations are blocked, but the broad R-01 claim—prevent child-process mutation of protected paths—is not established because protected metadata can be changed. This is an observed capability gap, not merely an untested theoretical limitation. The metadata probe was a direct child-boundary invocation rather than a Kernel run, so it has no Kernel journal; the content-write Kernel run and regression-test journal evidence are recorded separately in JSON.

### R-02 — journals, anchors, promotion history, and semantic state

In disposable Kernel run `run-20261004-180325-0a69379d`, five direct built-in attempts to overwrite/delete run history, edit `.head`, truncate system history, or delete a proposal were denied by `journal-integrity` before the target operation. The five target SHA-256 values matched before and after. Run/index/system chains remained intact and system-event semantics passed.

In child run `run-20261004-180325-e0c59471`, Python attempted write, truncate, delete, and replace against five journal/anchor/history/proposal targets: **20/20 operations were denied with errno 13**. Target hashes remained unchanged; post-run chains and system semantics were intact. Separate retained tests exercise semantic forgery, replay, stale anchors, and invalid transitions through the state APIs.

**Result: PARTIALLY_VERIFIED.** The tested built-in APIs, state APIs, and child paths prevent the listed operations. Ourob does not confine arbitrary raw filesystem calls made by malicious in-process Python running with the checkout user's privileges. The broader claim that every runtime capability is prevented from touching history is therefore not proved; post-mutation detection is not counted as prevention.

### R-03 — proposal evidence is separate from amendment authority

Proposal JSON no longer grants write authority. The tested path requires a separate, current `amendment.authorized` system-journal event whose authorization ID, proposal digest, base revision, lock digest, and canonical path set match. Tests cover forged or modified proposals, stale revision/digest, wrong pins and paths, duplicates, replay after terminal state, malformed records, unauthorized paths, and runtime-generated authorization attempts. The Result 001 forged-file case is denied and leaves its protected target unchanged.

The CLI requires an interactive terminal and exact typed proposal ID. `--by` remains a self-asserted label, not cryptographic proof of a human identity; a same-privilege operator can still edit the checkout and its journal directly. Within the tested application ledger, policy, and API paths, the authority-separation claim is **PROVED**.

### R-04 — deterministic discovery/list/invocation lifecycle

A registry holds one startup discovery snapshot. Repeated discovery is idempotent; a contribution added, changed, or removed mid-run is reported as pending, not hot-loaded by that registry. Listing reports `restart_required`, and invoking a pending skill through the stale registry fails with a fresh-runtime instruction.

A fresh disposable probe began with 15 skills, then added `src/ourob/skills/contrib/arena_fresh_skill.py`. The existing registry reported the exact path pending; list output reported a stale snapshot; stale invocation failed; a new registry discovered and invoked the skill, returning `HELLO`. Retained regression tests cover fresh startup, pending changes, listing, refusal, and fresh-registry success. **PROVED** for the flat contribution discovery path tested; no nested-directory claim is made.

### R-05 — clean development installation and `httpx`

`httpx>=0.27` is included in the documented `[dev]` extra because `tests/test_llm_planner.py` imports it during collection. It remains optional for a normal runtime install; `[llm]` remains the feature extra.

A fresh `/tmp/ourob-clean-dev-venv` installed the documented editable `.[dev]` extra, including `httpx==0.28.1`, `pytest==9.1.1`, `pytest-xdist==3.8.0`, and `ruff==0.16.10`. On the current working tree, the full suite passed: **432 passed in 224.77s**. The LLM planner test module was collected and executed. **PROVED** for Python 3.11.2/Linux x86_64 and the documented install command.

### R-06 — new self-extension through verification and promotion

The existing `rot13` contribution is not used as evidence of a new extension. The tested plan adds the previously absent `src/ourob/skills/contrib/text_metrics.py` and `tests/test_contrib_text_metrics.py`.

The current disposable run was `run-20261004-180516-7ae2575a`; both target paths were absent from the starting manifest and recorded in the run's touched set. Kernel verification passed all eight configured gates. Its candidate `tree_digest` was `d40ed00486e836391c410debc73f1bf0f354fb224fa741c0292590d4f68c2212`. Promotion verification `promote-20261004-180526-914a6d73` passed six gates, including the generated tests (**4 passed**), and promotion was accepted. The pre-promotion tree digest, Kernel report tree digest, resulting lock digest, promotion verification tree digest, and `promotion.accepted` event digests were all exactly `d40ed00486e836391c410debc73f1bf0f354fb224fa741c0292590d4f68c2212`. The promoted fixture manifest was clean. This test uses `use_git=False`; it does not claim to have made a Git commit. **PROVED** for the named disposable self-extension and promotion path.

## Verification notes

- Fresh `[dev]` full suite on the current code/test tree: **432 passed in 224.77s**.
- `ruff check src tests bootstrap.py`: passed.
- `git diff --check`: passed.
- `ruff format --check src tests bootstrap.py`: **18 files would be reformatted**. This is a formatter check, not a configured verification gate; no repository-wide formatting sweep was applied.
- The recorded exact-artifact run on lock version 26 (71 files) passed strict proof (`TRUSTED`) and `bootstrap.py verify -v` passed **8/8 gates**, including the full **432-test** TestGate run.
- These report/evidence files participate in the manifest. Recording the exact requested-revision baseline and updating the revision boundary changes their hashes; lock version 30 is rebuilt and the frozen final artifact is re-verified afterward. That last run's outcome is summarized in the delivery response to avoid an endless self-referential report update.

## Result

**Overall: PARTIALLY_VERIFIED.** R-01 remains partial because a child changed protected mode and timestamp metadata despite content/namespace protections. R-02 remains partial because arbitrary in-process raw filesystem calls are outside the tested application and child-process paths. R-03 through R-06 meet their stated, tested acceptance scopes. The historical failures were reproduced at the exact requested baseline commit `71464d365c41da9c74ed3f943d031b135a316beb`.

### Claims table

| Claim | Status | Evidence boundary |
|---|---|---|
| R-01: tested child content/namespace operations and direct declared file-skill attempts are denied before protected content changes | **PARTIALLY_VERIFIED** | Linux x86_64, kernel 6.1.158+, Landlock ABI 2. `chmod` and `utime` of protected files were observed to succeed; therefore all protected-path mutation is not prevented. |
| R-02: tested built-in APIs and child processes cannot rewrite journals/anchors/history; semantic forgery is rejected | **PARTIALLY_VERIFIED** | Five direct denials, 20/20 child denials, matching hashes and intact checks. Arbitrary raw in-process filesystem operations remain outside scope. |
| R-03: proposal files cannot self-authorize tested protected changes | **PROVED** | Forgery/staleness/replay/API regressions; no cryptographic identity claim. |
| R-04: contribution discovery/list/invocation has a deterministic fresh-runtime lifecycle | **PROVED** | Temporary mid-run skill probe plus retained registry tests; flat contrib path only. |
| R-05: documented `[dev]` install suffices for collection and full suite, including `httpx` | **PROVED** | Fresh editable install; `httpx==0.28.1`; 432 tests passed. |
| R-06: a genuinely new skill is verified and promoted with matching candidate/lock/evidence digests | **PROVED** | New `text_metrics` target; all candidate digest fields matched exactly; no Git commit claim. |
| Historical Result 001 failures reproduced at exact requested commit `71464d…` | **PROVED** | Disposable archive reproductions covered R-01 child writes, both R-02 history paths, R-03 forged amendment, R-04/R-06 existing target, and R-05 missing dev dependency. |
| General OS isolation, same-user/operator resistance, or arbitrary in-process code confinement | **OUT_OF_SCOPE** | Ourob disclaims a general OS sandbox; these properties are not established. |

### Remaining failures

1. Child processes can mutate protected file modes and timestamps (`chmod`/`utime` succeeded); R-01 is not fully proved.
2. Arbitrary raw filesystem operations from malicious in-process Python remain outside the application/child boundary; R-02 is partial.
3. Operator labels and typed confirmation are not cryptographic identity/signature evidence.
4. `ruff format --check` reports 18 files requiring formatting; Ruff lint and configured verification gates are separate.

### Evidence boundary

Evidence consists of distinct baseline reproductions, policy decisions, child syscall outcomes, before/after content and journal hashes, explicit observed metadata changes, retained adversarial tests, a clean `[dev]` install/full-suite run, and a disposable new-skill promotion with matching candidate and evidence digests. The JSON provides the requested per-challenge fields: revision, environment, preconditions, claim, action, policy input/decision, before/after artifacts, journal/verification evidence, observed/expected result, discrepancy, status, scope, and reproduction.

Application policy denies only tested declared direct operations. The Linux child boundary blocks tested content/namespace writes and selected ownership/xattr syscalls, but the observed `chmod` and `utime` successes demonstrate that it is not a complete protected-path mutation boundary. Neither layer confines arbitrary malicious in-process code or a same-privilege checkout owner. No general OS isolation, external trust anchor, or human identity proof is claimed.

### Release identity

- **Branch:** `arena/01a1070e-ourob`
- **Base commit:** `b212a233ccca4cbafc6112928a3548aa35c4f7f3`
- **Requested Result 001 commit:** `71464d365c41da9c74ed3f943d031b135a316beb` — fetched, preserved as an ancestor, and tested in a disposable archive
- **Remediation source-tree SHA-256:** `5904f60d1882e6aa703905485e12f5b2a62124ca2c436bfcaf4d7110d98b5c6d` (sorted relative path / file SHA-256 pairs; excludes the lock and these two self-referential evaluation files)
- **Bootstrap lock:** version 30 after including these updated evaluation files. The machine-readable evidence identifies the recorded version-26 verification and final version-30 re-verification boundary.
- **Commit identity:** not embedded; embedding a commit hash would change the committed report itself.
