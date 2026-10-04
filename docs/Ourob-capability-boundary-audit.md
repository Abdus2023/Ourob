# Ourob Capability Boundary Audit v2

- **Audit date:** 2026-10-04
- **Audited source revision:** `d2cc40583946a649452ebc203c3662c97e6ddd3e`
- **Branch:** `arena/01a1070e-ourob`
- **Result:** **FAILED for general capability containment; PARTIALLY CONTROLLED for specifically mediated paths**
- **Machine-readable companion:** [Ourob-capability-boundary-audit-evidence.json](Ourob-capability-boundary-audit-evidence.json)
- **Assurance rule:** **NO EVIDENCE → NO VERIFIED CLAIM**

> A policy decision is not capability containment. A journal record is not an external trust anchor. A verifier finding a change after it happened is detection, not prevention. The tested Linux child filesystem restriction is not general OS isolation.

## 1. Scope, distinctions, and status vocabulary

This audit asks whether a skill can exercise an operation outside the authority represented by its declaration and Ourob's normal policy path. It traces **requested → declared → granted → policy checked → execution interface → actual operation → journal/evidence → verification**. The audit is adversarial: probes used real `pathlib`, `os`, `shutil`, `tempfile`, `subprocess`, socket, Git, registry, and journal operations against synthetic fixtures in detached disposable clones. No destructive probe was run against live protected files in `/home/user/Ourob`.

The capability IDs below are this audit's taxonomy, not upstream Ourob identifiers. CB-01–CB-20 cover the initial set; CB-21 onward expand it for import-time code, declaration trust, registry internals, self-reporting, and explicitly unclaimed OS facilities.

The distinctions used throughout are:

1. **Policy authorization** — whether a Kernel-mediated invocation is permitted by application rules and any valid amendment record.
2. **Capability containment** — whether the executing code can use a lower-level interface to perform an operation outside that decision.
3. **Post-action detection** — whether a journal checker, manifest comparison, or verification gate notices an effect after it has occurred.
4. **OS isolation** — privilege separation or a broad operating-system sandbox, which is not claimed by Ourob and was not established here.

Each challenge has exactly one primary status from the required vocabulary:

- **CONTROLLED** — the specific attempted operation was refused before the target changed, in the exact tested route.
- **DETECTED_ONLY** — an effect or corruption was possible/occurred and later checking detected it; prevention is not established.
- **UNCONTROLLED** — the operation succeeded through an available interface, or the tested route has no enforcing boundary.
- **NOT_OBSERVABLE** — available evidence cannot establish the value; it is not an implicit pass.
- **OUT_OF_SCOPE** — the project explicitly does not claim to control the capability.
- **UNTESTED** — the capability was identified but not exercised.

The matrix status is a **primary status for that aggregate capability class**. Narrow successful controls are separately identified in the challenge evidence and negative controls; they do not upgrade a broader class containing an observed bypass.

## 2. Exact revision, environment, and baseline

### Revision and pre-change state

- Repository: `Abdus2023/Ourob`, checkout `/home/user/Ourob`.
- Evaluated `HEAD`: `d2cc40583946a649452ebc203c3662c97e6ddd3e`; parent/base: `b212a233ccca4cbafc6112928a3548aa35c4f7f3`.
- Branch: `arena/01a1070e-ourob`.
- Before audit-document edits, `git status --porcelain=v1 --untracked-files=all` was empty. No source/test changes were made for this audit. Probe trees were detached disposable clones at the evaluated revision.
- Baseline `bootstrap.lock.json`: version 30, 71 files, digest `f7a673aedb75fb69661015cbc62c6ee580e15b1e198c724149cb6adb47ef8f98`.
- Ignored `.ourob/` runtime state is not part of the Git tree or lock identity. The recorded baseline is the exact Git working-tree state; ignored runtime files were not separately content-hashed before the verification commands.

### Runtime and host

- Linux `x86_64`, kernel `6.1.158+`; effective UID/GID `1001/1001`.
- CPython `3.11.2`.
- `ourob.child_sandbox.landlock_status()` reported available, **Landlock ABI 2**.
- Clean verification environment `/tmp/ourob-clean-dev-venv`: `pytest 9.1.1`, `pytest-xdist 3.8.0`, `ruff 0.16.10`, `httpx 0.28.1`.
- Runtime protected list from the evaluated `ourob.toml`: `ourob.toml`, `bootstrap.py`, `bootstrap.lock.json`, `src/ourob/bootstrap/`, `src/ourob/policies/`, `src/ourob/verify/`. The child boundary additionally excludes `.ourob/` and `.git/` from writable roots.

### Baseline verification and prior evidence

Before modifying the repository:

- Strict proof, `/tmp/ourob-clean-dev-venv/bin/python bootstrap.py --prove --strict`: **TRUSTED**, lock v30, 71 files, digest `f7a673aedb75fb69661015cbc62c6ee580e15b1e198c724149cb6adb47ef8f98`.
- `bootstrap.py verify -v` with the venv omitted from `PATH`: **7/8 gates**, with the non-blocking Ruff gate skipped because `ruff` was not on that `PATH`; TestGate still ran **432 tests**. This was an environment-path issue, not a repository test failure.
- Complete command with `/tmp/ourob-clean-dev-venv/bin` on `PATH`: **8/8 gates**, no skips/failures; manifest 71 files; **432 tests passed**; Ruff passed; bootstrap cold start `TRUSTED`.
- Retained adversarial regressions in the active checkout: **22 passed** in 3.05 seconds. A corrected, separate targeted command passed **6 tests** covering protected path variants/symlink, undeclared path metadata, declared path confinement, protected reads, and authorization-event operator scope. One earlier node selector named a nonexistent test and ran zero tests; it was corrected and is not counted as a pass.

The previous remediation report/evidence were inspected and left unchanged. The report's conclusion remains **PARTIALLY_VERIFIED**. Historical Result 001 files remain byte-for-byte identical to their recorded hashes:

- `docs/Ourob-remediation-evaluation.md` — `ae694134587d493e7260846496167589e4b3286bf612ba568b14fe72eb7b376b`
- `docs/Ourob-remediation-evaluation-evidence.json` — `66b8d85f1e62d8a7c0aa128781551d873f0e80beb9aaa58652db6a39416d0d35`
- `docs/Ourob-independent-evaluation.md` — `d19eb4e122f8ed4096a9a82f92d627ae24a119b64668bedc3f751a35d12a74d9`
- `docs/Ourob-independent-evaluation-evidence.json` — `bf4d52475045a24d4dede58b1efcdaa05a81c1f35ca67f094b26260d8bf1858f`

## 3. Capability taxonomy and primary matrix

| ID | Capability / boundary tested | Primary status | Short basis |
|---|---|---|---|
| CB-01 | Read repository, protected files, `.ourob`, `.git`, and outside-repository paths | **UNCONTROLLED** | Raw Python and standard child code read synthetic `/tmp`, `.ourob`, and `.git/HEAD`; normal API path confinement is narrower. |
| CB-02 | Create/overwrite protected file content | **UNCONTROLLED** | A `mutating=False` in-process plugin wrote protected files; a pre-existing hardlink let Kernel `run_python` change a protected inode through `docs/`. |
| CB-03 | Unlink/delete protected entries | **UNCONTROLLED** | Raw in-process `Path`/`os` deletion succeeded; Kernel file API denial is route-specific. |
| CB-04 | Rename, replace, and copy-overwrite | **UNCONTROLLED** | Raw in-process `os.rename`, `os.replace`, `shutil.copyfile` create/overwrite, and tempfile-plus-replace succeeded; a separate `os.truncate` syscall was not tested. |
| CB-05 | Mode, timestamps, ownership, extended attributes | **UNCONTROLLED** | In-process metadata operations succeeded; child `chmod` and `utime` also succeeded. Child `chown`/xattr attempts were denied in the tested route. |
| CB-06 | Directory creation/removal and protected parent-directory move | **UNCONTROLLED** | In-process `mkdir`/`rmdir` succeeded; a protected parent directory was moved into `docs/`, written through, and restored; child canonical namespace writes are narrower. |
| CB-07 | Symlink/hardlink aliases and inode identity | **UNCONTROLLED** | In-process link operations succeeded; a pre-existing hardlink alias bypassed the child path boundary. |
| CB-08 | Run-journal records and append behavior | **UNCONTROLLED** | Raw append was possible. Malformed JSON was detected later; detection did not prevent append. |
| CB-09 | Journal anchors, index/head files, re-chaining | **UNCONTROLLED** | A prior run goal and full chain/head were rewritten; integrity checks still passed and replay consumed the forged goal. |
| CB-10 | Amendment proposal and authorization artifacts | **UNCONTROLLED** | A plugin wrote an authorization event/head accepted by the ledger; artifact files are writable by same-process raw Python. |
| CB-11 | Grant issuance, freshness, identity, and scope | **UNCONTROLLED** | A forged grant authorized a protected write; a syntactically valid grant with a 1900 timestamp was accepted. |
| CB-12 | Process creation and shell/command execution | **UNCONTROLLED** | A raw plugin launched an unsandboxed subprocess that wrote a protected fixture; child code could launch shell/nested Python. |
| CB-13 | Child and descendant authority inheritance | **UNCONTROLLED** | Descendants inherit the narrow filesystem rule, not a read/network/identity sandbox; a hardlink alias still bypassed it. |
| CB-14 | Environment read and mutation | **UNCONTROLLED** | `OUROB_API_KEY` reached standard child code; raw in-process code mutated `os.environ`. An arbitrary synthetic variable was filtered from the standard child environment. |
| CB-15 | Local socket creation/bind | **UNCONTROLLED** | Standard child code bound a loopback TCP socket successfully. |
| CB-16 | Git metadata/ref mutation | **UNCONTROLLED** | Raw in-process plugin created a real disposable ref; equivalent child `git update-ref` was denied at `.git/...lock`. |
| CB-17 | Alternate dispatch/API entry points | **UNCONTROLLED** | `SkillRegistry.dispatch` outside Kernel wrote a protected file without Kernel policy review or run journaling. |
| CB-18 | Raw Python and standard-library operations | **UNCONTROLLED** | The same-process plugin could use ordinary Python/stdlib OS interfaces directly; no mandatory capability broker exists. |
| CB-19 | Linux child write boundary as a whole | **UNCONTROLLED** | It blocks tested canonical content/namespace writes and selected syscalls, but misses metadata and inode aliases and does not restrict reads/network. |
| CB-20 | Temporary files, atomic replacement, and path variants | **UNCONTROLLED** | Raw in-process tempfile/replace paths succeeded; child scratch isolation works only for the tested direct outside-write route. |
| CB-21 | Module import and top-level plugin execution | **UNCONTROLLED** | A `mutating=False` contribution created a protected marker during import, before plan dispatch. |
| CB-22 | Skill-authored mutability declaration and contract schema | **UNCONTROLLED** | `mutating=False` caused “read-only skill” policy allowance; an invalid schema skill still executed despite a contract-gate finding. |
| CB-23 | Frozen-registry lifecycle and internal mutation | **UNCONTROLLED** | Public `register()` rejected post-freeze registration, but direct `_skills`/`_modules` injection made a new skill invocable. |
| CB-24 | Operation-level evidence completeness (`touched`, artifacts, journals) | **UNCONTROLLED** | Raw changes were omitted from `run.touched`/artifacts; child API dispatch logged only the outer `run_python` action. |
| CB-25 | Public/external network egress | **OUT_OF_SCOPE** | Ourob explicitly disclaims network restriction; remote egress was not attempted. |
| CB-26 | General process inspection, signals, and ptrace resistance | **OUT_OF_SCOPE** | Explicitly disclaimed; not probed. |
| CB-27 | Non-Linux and other architecture behavior | **UNTESTED** | Only Linux x86_64 with Landlock ABI 2 was exercised. |

Narrow **CONTROLLED** challenges (normal Kernel protected-write denial, ordinary outside-path API denial, canonical child protected-content denial, child `chown`/xattr denial, child scratch-root behavior, arbitrary-variable filtering, stale-registry refusal, and API-mediated journal mutation denial) are recorded separately in the JSON. They are not contradictory: a broad class is `UNCONTROLLED` when an alternate route or tested variant bypasses its control. The malformed-append case is specifically **DETECTED_ONLY**.

## 4. Enforcement topology and capability trace

### Where authority comes from

`SkillSpec` declares a name, description, JSON-like parameter hints, examples, and a Boolean `mutating`. It does not issue unforgeable per-resource capability handles. The planner's `Invocation` asks the Kernel to call a registered name; the runtime normally constructs a `PolicyContext`, computes policy verdicts, journals a decision, and then dispatches through `SkillRegistry`.

The skill receives a `SkillContext` containing the resolved repository `Path`, a `StateStore` reference, and service objects (including registry/configuration). `ctx.path()` is a useful confined helper for built-in file skills, but the raw `Path`, Python runtime, imports, process environment, and same-UID OS authority remain available. A contribution can omit `ctx.path()` and call `Path.write_text`, `os`, `shutil`, `tempfile`, or `subprocess` directly. The plugin itself supplies `mutating=False`; the policy uses that value to treat it as read-only. Thus the declaration is metadata, not attenuation.

### Trace through the normal path and the bypass

| Stage | Normal path | Observed bypass / boundary |
|---|---|---|
| Requested | Planner emits skill name and arguments to `Kernel.run`. | A raw plugin can execute code during discovery/import before an invocation; callers can also invoke `SkillRegistry.dispatch` directly. |
| Declared | `SkillSpec` provides schema hints and `mutating`. | No OS capability list or mandatory declaration of filesystem/network/process APIs; malicious declaration is self-authored. |
| Granted | Kernel policy decisions permit or deny a declared invocation; an exact path may require an amendment. | A verdict is not a restricted object. In-process code retains the host process's ambient UID 1001 authority. |
| Policy checked | `Kernel._authorise` is the normal gate; policy code checks declared path/skill context. | Direct registry dispatch, import-time code, raw stdlib calls, and arbitrary plugin operations do not invoke per-operation Kernel policy. |
| Execution interface | Built-in methods may use `ctx.path`, `fsx`, or the `run_child` wrapper. | Python object references and OS APIs are callable directly; a raw subprocess is not automatically routed through `run_child`. |
| Actual operation | Normal built-in protected writes were denied; ordinary `docs/` writes were permitted. | Protected write/delete/replace/metadata, `.git` change, journal forgery, environment mutation, and subprocess effects succeeded in disposable clones. |
| Journal/evidence | Kernel records decisions and skill results; state APIs have hash chains and semantic checks. | No operation-level mediation means no guaranteed event per OS effect. Raw changes were omitted from `touched`/artifacts; a same-process attacker can rewrite records and anchors. |
| Verification | Manifest, journal, contract, and configured gates can detect specified drift or malformed state. | Detection is post-action and can be bypassed by a re-chained log or forged semantically accepted system event. A passing gate does not contain raw Python. |

### Child boundary topology

`run_command` and `run_python` use `run_child`; on this host it installed Landlock ABI 2 before `exec`, applied resource limits, and used a seccomp filter denying selected ownership/xattr syscalls. Descendants inherit those restrictions. The write domain allows existing unprotected repository directories and a per-call scratch root; `.ourob/`, `.git/`, and configured protected paths are excluded. The scratch root is removed after the child returns.

This mechanism is narrower than the capability taxonomy:

- It limits selected **writes**, not reads. Child code read synthetic `/tmp` and `.ourob` files and `.git/HEAD`.
- `chmod` and timestamp updates are not mediated; both changed a protected child fixture. Selected `chown`/xattr operations were denied.
- It permits loopback socket bind; external egress was not tested.
- It does not change the UID or provide a separate identity.
- Path-based Landlock rules did not protect a protected inode already hard-linked into an allowed `docs/` directory. The child wrote the alias and changed the protected target.
- It does not constrain raw Python executing inside the Kernel process. A plugin can launch a subprocess without the wrapper and without Landlock.

Accordingly, **Model D (ambient authority in in-process skills) is the default actual model**. A Model-A-like policy check exists for Kernel-mediated calls. A narrow Model-B-like OS write control applies to selected child wrappers on supported Linux. There is no general Model C OS sandbox or privilege separation.

## 5. Protected-resource metadata, structure, and path variants

The strongest concrete bypass was end-to-end through Kernel `run_python` in a disposable clone:

- Protected inode: `src/ourob/policies/cb-hardlink-target.txt`.
- Before child: SHA-256 `9e743905f6c0cf3e3c0af924367bc90316ebab148abe9d0d4d71234627bbd2b7`.
- A host-side fixture hard-linked it to writable `docs/cb-hardlink-alias.txt` before sandbox setup.
- Kernel `child-filesystem` policy allowed the child because Landlock ABI 2 was present. The child wrote the `docs/` alias and exited 0.
- After child: protected target SHA-256 `7c7b975481d02d4ad3c6a988bccb693197560f1cfd33dba157d3e2f7cf4c4fc2`.
- `run.touched` was empty and the run journal chain remained intact.

The child rule is path-based and grants writes under unprotected directories; it does not establish that an allowed path refers to an unprotected inode. A direct child attempt to create a new hardlink across the boundary was denied, but that does not address an alias created before the restriction. Raw in-process Python created links directly.

Other disposable `mutating=False` plugin probes reported success for protected-path write/replace, unlink, mkdir/rmdir, symlink creation and write-through, hardlink creation, `shutil.copyfile` create/overwrite, `os.rename`, `os.replace`, tempfile-plus-replace, `chmod`, `utime`, same-owner `chown`, and `setxattr`. A separate in-process probe renamed `src/ourob/policies/cb-parent-move-probe` into `docs/`, wrote its marker through the moved parent, then restored the directory; the protected marker remained changed and `run.touched`/artifacts stayed empty. These were real Python/stdlib calls, not mocked policy decisions. The probe also wrote under `.git` and modified process environment. Policy output described the plugin as “read-only skill”; the raw effects did not appear in `run.touched` or artifacts.

The alternate child metadata results are deliberately separated: `chmod` and `utime` succeeded against a protected file; child `chown` and xattr mutation returned `EPERM`. The last two denials establish only the selected syscall filter in that child route, not general metadata protection.

## 6. Journals, anchors, authorization artifacts, and skill lifecycle

### Journal records and anchors

Application Journal/StateStore APIs enforce guarded paths and normal API mutation attempts were denied in retained controls. Direct raw Python is not required to use them.

Two separate outcomes were reproduced in disposable clones:

1. A raw plugin appended malformed bytes to a prior run journal. The write happened; a later `StateStore.check_chain()` detected unreadable JSON. This is **DETECTED_ONLY**, not prevention.
2. Another raw plugin changed a prior `run.started` goal, recomputed the entire run-journal hash chain, and rewrote the corresponding `.head`. `check_chain()` reported intact chains, and replay returned the forged goal. The journal's hash chain proves internal consistency of the current bytes, not who wrote them or whether the original bytes were retained.

The anchor is in the same writable checkout and under the same OS identity as the code that it is supposed to audit. Recomputing a chain and its local head is therefore not an external trust anchor.

### Authorization artifacts

The normal authorization API has meaningful application checks: separate proposal and `amendment.authorized` event; confirmation of the exact proposal ID/digest in a separate operator action; matching paths, base revision and lock digest; unique authorization ID; stale/terminal checks; and semantic journal validation. Prior tests cover those normal API conditions. The CLI's `--by` value remains a self-asserted label, not cryptographic identity proof.

The raw same-process path bypassed those checks:

- In `/tmp/ourob-cb-audit-authforge`, a `mutating=False` plugin forged a correctly chained `.ourob/system.jsonl` `amendment.authorized` event plus matching head. The event named actor `attacker-controlled-label`; the proposal digest, exact paths, base revision and lock digest matched. No operator confirmation or `AmendmentLedger.authorize()` call occurred. `StateStore.check_chain()` accepted system semantics; `authorising_id()` returned an active grant; a subsequent normal Kernel `write_file` to `src/ourob/policies/cb-forged-authority-write.txt` was allowed.
- In `/tmp/ourob-cb-audit-timegrant`, an otherwise valid grant with `ts: 0` and `stamp: "1900-01-01T00:00:00Z"` was accepted as active. The system journal reported one event with semantics intact. No expiry check was observed in grant consumption.
- A proposal-only file is not sufficient authority in the normal path; that narrower prior conclusion is preserved. The bypass is that raw Python can create the trusted-looking event/anchor files themselves.

The tested grant binds an exact proposal-file digest, path set, base revision and lock digest. The record does not supply an external identity/signature or independently protected timestamp, and it is not an external anchor. The event authorizes paths, not a cryptographic digest of the eventual file contents.

### Genuine discovery, contract checks, and registry state

A new contrib skill file was added only inside a disposable clone. A registry already running reported the file pending/`restart_required` and did not invoke it; a fresh registry discovered and invoked it. This is a real new-file discovery positive control, not an already-present example.

That narrow lifecycle behavior is not a sandbox:

- A discovered skill using an invalid schema type (`not-a-real-type`) still registered and executed through Kernel; `SkillContractGate` separately reported one contract problem. This is **DETECTED_ONLY** for contract validity.
- A `mutating=False` plugin performed an import-time protected marker write before a planner step was dispatched.
- After the public `register()` method correctly rejected a post-freeze skill, the plugin inserted an instance directly into `registry._skills` and `_modules`. A later plan invoked `cb_memory_injected_20261004`; discovery still reported complete/no pending changes.
- Direct `SkillRegistry.dispatch(write_file, ...)` outside Kernel wrote a protected file without Kernel policy review or Kernel run journaling.
- In a standard child, the same direct dispatch could write ordinary `docs/`; its canonical protected write was denied by Landlock. The parent Kernel recorded only the outer `run_python`; child operations were not individually journaled and `run.touched` was empty.

## 7. R-01–R-06 findings and relationship to the prior evaluation

The prior remediation report and evidence are preserved byte-for-byte. The conclusions below do not silently rewrite them; they distinguish the prior stated scope from this audit's expanded adversarial scope.

### R-01 — child-process mutation of protected paths

**Prior result: PARTIALLY_VERIFIED.** The prior report demonstrated normal API denials, child content/namespace denials, and the observed `chmod`/`utime` gap. This audit reproduced the gap and expanded it: a pre-existing hardlink under allowed `docs/` let Kernel `run_python` change a protected inode. Raw in-process mutation is outside the Landlock layer. The broad claim “child processes cannot mutate protected paths” is not supported.

### R-02 — journals, anchors, promotion history, and semantic state

**Prior result: PARTIALLY_VERIFIED.** API-mediated and tested child mutations were denied in the prior evidence. This audit shows why the same result does not extend to raw in-process code: malformed append is detected after the write; a complete re-chain plus rewritten local head passes; replay consumes altered content. The journal protects neither the same-privilege writer nor its own trust anchor.

### R-03 — proposal evidence separate from amendment authority

**Prior result: PROVED only for the named application ledger/API checks**, with the prior report's explicit same-privilege and identity caveats. Those normal checks remain real controls. The expanded probe forged an accepted authority event/head directly and used it to authorize a normal protected write; a backdated record was also accepted. Therefore no broad claim that runtime plugin code cannot issue authority is verified.

### R-04 — deterministic discovery/list/invocation lifecycle

**Prior result: PROVED for the flat contrib discovery path tested.** This audit confirms stale registries report pending files and fresh registries discover genuinely new skills. It also shows the scope limit: invalid contracts can execute, import side effects happen, and direct mutation of the frozen registry's private dictionaries bypasses the public freeze check. The narrow ordinary discovery result is not changed; malicious in-process registry integrity is not established.

### R-05 — clean development installation and `httpx`

**Prior result: PROVED for Python 3.11.2/Linux x86_64 and the documented `[dev]` install.** This audit did not repeat a fresh package installation. The pre-change environment and final complete verification are recorded here; a passing collection/test run is dependency evidence, not a capability boundary.

### R-06 — new self-extension through verification and promotion

**Prior result: PROVED for the specific new-skill verification/promotion route and digests documented in the prior report.** This audit independently confirms a genuine new skill can be discovered and invoked by a fresh registry. Neither result establishes that imported skill code is confined, that registry internals are immutable, or that a child's effects are operation-journaled.

## 8. Negative controls

Negative controls demonstrate that probes exercised real boundaries rather than a broken fixture or a universally denying sandbox:

1. **Kernel protected-write gate:** standard `write_file` to a protected path was denied by `protected-paths`; an ordinary `docs/` write succeeded and appeared in `run.touched`.
2. **Normal path confinement:** standard `read_file` could read protected `rules.py`, while an outside `/tmp` read was denied by `path-confinement`. This is API behavior, not a raw-Python read restriction.
3. **Canonical child write:** direct child content/namespace attempts at protected paths were denied by Landlock, while an ordinary `docs/` control write succeeded. The child hardlink alias is the bypass control and changed the real protected target.
4. **Child metadata split:** child `chown`/xattr attempts were denied; `chmod`/`utime` succeeded and changed metadata. This confirms the selected filter's actual scope.
5. **Journal APIs:** direct protected journal/anchor API attempts and retained child mutation tests were denied; the raw malformed/re-chained plugins exercised filesystem writes outside those APIs.
6. **Authorization API:** normal tests enforce operator-scope and proposal constraints; the forged-event probe wrote the same on-disk event shape directly and was accepted.
7. **Skill lifecycle:** the frozen public `register()` method rejected insertion; stale discovery refused a late file and a fresh registry successfully discovered it. Private dictionary mutation is a separate bypass.
8. **Child environment:** an arbitrary synthetic `CB_AUDIT_PRIVATE` variable was absent from standard child environment; synthetic `OUROB_API_KEY` was present because `_child_env` deliberately passes it.
9. **Child scratch:** a direct write to `/tmp/ourob-cb-outside-write-probe.txt` failed with `PermissionError(13)`; `tempfile` wrote successfully beneath `/tmp/ourob-child-.../tmp`, and the scratch root no longer existed after return.
10. **Git differential:** in-process `git update-ref` created a disposable ref; equivalent child invocation was denied creating its `.lock` file under `.git`.
11. **Test suite:** retained adversarial regressions and the full suite passed before edits. They support only their test cases, not a universal claim.

## 9. Alternate paths exercised

| Alternate path | Enforcement observed | Result |
|---|---|---|
| `Path.write_text`, `unlink`, `mkdir`, `rmdir`, `os.rename`, `os.replace`, `os.link`, `os.symlink`, `chmod`, `utime`, `chown`, `setxattr` inside a plugin | No per-operation Kernel policy; process has checkout UID | Protected fixture operations succeeded. |
| `shutil.copyfile`, temporary file + replace | No mandatory path broker | Protected fixture create/overwrite succeeded. |
| Raw `subprocess`/nested Python from a plugin | Not routed through `run_child` | A subprocess wrote a protected fixture. |
| `SkillRegistry.dispatch` called outside `Kernel.run` | Registry schema/coercion only; no Kernel policy or journal | Protected `write_file` succeeded. |
| `run_python` child directly constructing/dispatching registry | No per-dispatch Kernel gate; child Landlock still applies | Ordinary `docs/` write succeeded; canonical protected write denied; hardlink alias succeeded in another probe. |
| Module import during discovery | Python import runs module top-level code | Protected marker write preceded plan dispatch. |
| Private `_skills`/`_modules` mutation after freeze | Public `register()` guard bypassed | Injected skill was listed and invoked. |
| Journal/system JSONL and `.head` written as files | API guards bypassed | Malformed append detected later; valid-looking forged event and recomputed chain passed checks. |
| `git update-ref` from plugin versus child | Parent has raw `.git`; child Landlock excludes `.git` | Parent ref mutation succeeded; child ref mutation failed. |
| `socket.socket().bind()` in standard child | No network sandbox | Loopback bind succeeded. |
| Unknown/unusual path argument key | Policy cannot infer an undeclared resource key | Retained test confirms an undeclared unusual key is not path-confined. |

## 10. Limitations and untested properties

- External/public network egress was not attempted. Local loopback bind success is not proof of remote reachability or data exfiltration.
- Process inspection, signals, ptrace, `/proc` access to other processes, mount operations, namespace creation, kernel exploits, and resource-exhaustion attacks were not probed.
- No non-Linux host, other architecture, older/newer kernel, or Landlock ABI was tested.
- The probes used disposable local clones and synthetic sentinel files. They did not read unrelated user secrets or mutate live protected data.
- No concurrent filesystem race or attack on an actively changing real repository was tested. The hardlink alias was deliberately created before child sandbox setup to test the inode/path distinction.
- Some manual plugin probes did not retain a per-operation hash for every fixture. Their successful OS calls and Kernel reporting omissions are recorded; no unretained digest is invented. Exact before/after hashes are included where captured, notably the end-to-end hardlink probe.
- Normal interactive CLI authorization was not manually driven through a terminal in this audit; its application checks are supported by source inspection and retained tests. The raw forged event is a separate, directly observed bypass.
- The audit does not establish containment against a malicious checkout owner, the current process owner, a kernel administrator, or compromised host.
- This is not an endorsement, certification, or broad security audit of the host or operating system.

## 11. OS-isolation boundary

Ourob's own `child_sandbox.py`, `README.md`, and architecture documentation explicitly say the child rule is **not a general OS sandbox** and does not restrict reads, networking, or general process inspection. The observed results agree with that statement. The child runs under UID 1001, reads protected/outside files, receives `OUROB_API_KEY` when configured, and can bind a local socket. `run_child` restricts selected filesystem write operations; it does not remove ambient authority from an in-process Python skill or provide privilege separation.

No OS-isolation claim is made. Landlock availability and successful denials are evidence only for the tested child route and operations. A normal API denial, passing verification gate, intact local hash chain, or post-action drift report is not evidence of an OS sandbox.

## 12. Final capability map

| Layer | What was prevented in the tested route | What remains possible / conclusion |
|---|---|---|
| Kernel policy | Declared direct protected `write_file` and normal outside-repository API read were denied; ordinary write was permitted and recorded. | Raw plugin interfaces and alternate dispatch bypass the policy. This is route-level authorization, not containment. |
| Child Landlock | Tested canonical protected content/namespace writes, `.ourob`/`.git` child ref creation, and outside `/tmp` writes were denied; unprotected `docs/` and scratch writes worked. | Protected mode/time mutation, pre-existing hardlink alias writes, reads, local networking and key access remain. |
| Child seccomp | Tested child ownership/xattr changes were denied. | `chmod` and `utime` remain possible; selected syscalls only. |
| Journal/checker | Malformed bytes were detected; API-mediated protected journal writes were denied. | A same-process writer can rewrite records and the local anchor; a full re-chain passes and replay consumes changed data. |
| Authorization | Normal API validates proposal digest/path/revision/lock and separate operator-action state. | Same-process raw Python forged accepted authorization history; actor identity and timestamp have no external trust. |
| Skill registry | Normal stale-registry path rejects late discovery; fresh startup discovers a new file. | Import executes code; invalid contract can run; private registry injection bypasses freeze. |
| Evidence/verification | Specified gates and hashes catch some declared drift and test outcomes. | Raw actions are not necessarily represented by `touched`/artifacts; verification is not an operation broker and is not prevention. |
| Overall | **Partial, path-specific controls exist.** | **General in-process capability containment is UNCONTROLLED. No general OS isolation is established.** |

## 13. Architectural recommendations (not implemented)

1. **Treat plugin Python as hostile code.** Do not execute contrib modules in the privileged Kernel process. Run each skill in a separate unprivileged worker or use a brokered execution design. Do not pass raw repository `Path`, `StateStore`, mutable registry objects, or ambient service credentials to the worker; expose narrowly scoped operation handles/RPCs instead.
2. **Separate writable and protected inodes, not only pathnames.** Use a separate filesystem/mount or isolated worktree for writable outputs; prevent protected inodes from having aliases beneath writable roots. Test pre-existing hardlinks, not only attempts to create links after Landlock activation. Path-only allow rules are insufficient for this case.
3. **Close metadata gaps at the OS boundary.** Use a read-only mount/strong privilege separation or a reviewed syscall policy that also denies protected `chmod`/timestamp operations. Keep child process controls fail-closed and test every permitted operation class.
4. **Move journals and authority outside the writer's trust domain.** Use a broker-owned append-only store, remote signing/MAC key, or independently protected monotonic anchor. A local hash chain whose log and head are writable by the same code is tamper-evident only against accidental corruption, not a same-privilege adversary.
5. **Make authorization externally authenticated and narrowly bound.** Verify operator identity out of process; bind grant to a nonce, expiry, exact operation/content digest, paths, repository identity and one run; keep the grant store outside the plugin's write authority. Do not infer human identity from `--by` or a JSON field.
6. **Make skill declarations enforceable.** Replace Boolean `mutating` metadata with a validated capability manifest; reject unknown/untyped path parameters; require policy mediation at the operation broker, not only before dispatch. Treat schema validation as a dispatch precondition rather than an advisory gate.
7. **Make discovery immutable and side-effect-safe.** Avoid importing untrusted code in the privileged registry; validate/load in an isolated worker, publish an immutable registry snapshot, and do not treat underscore-prefixed dictionaries as security boundaries.
8. **Minimize inherited environment and network authority.** Do not place API keys in arbitrary skill child environments; use a narrowly scoped credential broker. Apply network deny-by-default only if that is a required capability boundary, then verify it with an explicit egress test.
9. **Record broker-observed operations.** Derive touched paths and journal events from the enforcing broker/OS monitor rather than trusting a skill's result or self-reported artifacts. Preserve distinction between attempted, denied, completed, and later detected effects.

No redesign or source remediation was implemented as part of this audit.

## 14. Final verification and release identity

The final audit artifacts are documentation/evidence plus the rebuilt bootstrap lock; no runtime source or test file is changed. The final complete verification is recorded in the companion JSON and was run against the frozen artifact:

- Strict bootstrap proof: `python bootstrap.py --prove --strict` — **TRUSTED**, lock v31, 73 files. The lock file is the authority for the final digest; it is not duplicated in this evidence because the evidence file itself is manifest-covered.
- Complete documented verification: `PATH=/tmp/ourob-clean-dev-venv/bin:$PATH /tmp/ourob-clean-dev-venv/bin/python bootstrap.py verify -v` — **8/8 gates passed**, no skips/failures, **432 tests passed**, Ruff passed, bootstrap cold start trusted.
- Gates: `policy-integrity`, `skill-contract`, `compile`, `import`, `manifest`, `lint`, `tests`, `bootstrap`.
- Tracked final changes: this Markdown, its JSON companion, and `bootstrap.lock.json`; prior evaluation files, historical Result 001 files, runtime source, and tests are unchanged.
- **Audited code revision:** `d2cc40583946a649452ebc203c3662c97e6ddd3e`. **Artifact branch:** `arena/01a1070e-ourob`.
- The audit report does not embed its containing commit hash because that would make the report self-referential. The enclosing VCS revision, final lock digest, and clean status are supplied in the delivery record.

The documented verification confirms repository consistency and test behavior for this exact final file set. It does **not** reverse any capability finding or establish OS isolation.
