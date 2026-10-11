# Ordered updates: master-instance before the control plane, on every sync

Design for platform #44, under D01-20 (saas-fabric #108,
[disposition](https://github.com/FieldstateNZ/saas-fabric/issues/108#issuecomment-6109518415)):

> Creation-only ordering may remain for the isolated disposable trial with
> explicit degraded-state gates; prove an ordered-update mechanism (RollingSync
> candidate) before the durable pilot. #44 may proceed as a bounded design/test
> PR. Establish controller/sync effects before any approval that would change
> the running environment.

The design was written against platform `main` at `40ebcf7`. It changes nothing
Argo CD syncs. The model, the checks and the fixtures it adds live under
`scripts/`, and the document lives under `docs/`. Neither path is reachable from
any environment, and `scripts/test_ordering.py` (`FixturesAreInert`) keeps it
that way.

**Status: proposal.** Issue #44 makes the mechanism Brett's choice, made from
evidence ("do not preselect RollingSync"). This document sets out that evidence
and names RollingSync as the candidate, as D01-20 asks. It does not adopt
RollingSync.

---

## 1. What the current manifests provide

### The mechanism

The root Application, `platform-root`, syncs `environments/<env>`. That path
renders one `Application` object per component, each with a
`argocd.argoproj.io/sync-wave`. Two Argo CD behaviours turn those waves into
ordering:

- the root applies its children wave by wave;
- `argocd/runtime/application-health.yaml` reports a child Application's health
  as the health of the application it names, so the root waits for real health
  between waves.

Without that health assessment, the waves order nothing.

Every child also carries its own `syncPolicy.automated` (`prune`, `selfHeal`).
When a child's own source changes on an environment that already exists:

- nothing the root applies changes, because the child `Application` object is
  identical;
- the root starts no sync, and no wave runs;
- the child sees itself OutOfSync and syncs on its own.

It consults no sibling.

### What holds, and when

All of the following are read from the render and asserted by `scripts/check.py`
§22. `scripts/test_ordering.py` (`CurrentManifests`) asserts them again against
the source files.

| Dependency (before → after) | Environment | Waves | On create | On update |
|---|---|---|---|---|
| `master-instance-credential` → `master-instance` | lucentroot | 10 → 30 | ordered | **not ordered** |
| `keycloak` → `master-instance` | lucentroot | 20 → 30 | ordered | **not ordered** |
| `master-instance` → `saas-fabric-control-plane` | lucentroot | 30 → 40 | ordered | **not ordered** |
| `keycloak` → `saas-fabric-control-plane` | lucentroot, production | 20 → 40 | ordered | **not ordered** |

The rendered wiring behind that table:

| Fact | lucentroot | production |
|---|---|---|
| Children authored by `platform-root` | 19 | 12 |
| Children with `automated` sync | all | all |
| Application health assessment in the bootstrap set and in the environment | yes, yes | yes, yes |
| `ApplicationSet` objects | none | none |
| `applicationsetcontroller.enable.progressive.syncs` | unset | unset |

### What does order on an update today

Three things order on an update now, and none of them is the order #44 needs:

1. **Waves inside one Application.** One sync operation applies one
   Application's resources wave by wave. So `master-instance`'s mirror at wave
   `-1` precedes its Job on every sync.
2. **Edits to the child `Application` objects themselves**, such as a changed
   path or sync policy. The root applies these wave by wave. The edited child
   then syncs its own content as soon as its object is applied, and does not
   wait for the root's later waves.
3. **Nothing between siblings.** A commit that touches both `master-instance`'s
   and `saas-fabric-control-plane`'s sources makes both OutOfSync, and both sync
   concurrently.

### The observation this reproduces

On 2026-09-22 on LucentRoot:

- 09:06:22Z: `master-instance` went Degraded.
- 09:06:57Z: `saas-fabric-control-plane` synced its own change anyway.

`CurrentManifests.test_failed_convergence_does_not_hold_back_the_control_plane`
models that moment: `master-instance` Degraded and the control plane OutOfSync.
The model reports that the control plane syncs, which is what was seen.

---

## 2. The candidate: ApplicationSet `RollingSync`

The fixture is `scripts/fixtures/rolling-sync/applicationset.yaml`, with the one
controller setting it needs in `argocd-cmd-params-cm.yaml` beside it. Neither is
rendered or applied. Both are schema-validated in CI by kubeconform, and modelled
by `scripts/ordering.py`.

### Shape

One `ApplicationSet`, `operator-sign-in`, sits in the root at wave `30`. A
`list` generator produces the same three Applications that are hand-written
today, with the same names, project, destination, finalizer and source path.
Each element carries that Application's current `syncPolicy`, less `automated`,
through a `templatePatch`.

`spec.strategy.type: RollingSync` adds three steps, selected by the label
`fieldstate.nz/rollout-step`:

| Step | Application | Wave today |
|---|---|---|
| 1 | `master-instance-credential` | 10 |
| 2 | `master-instance` | 30 |
| 3 | `saas-fabric-control-plane` | 40 |

`syncPolicy.preserveResourcesOnDeletion: true` and
`applicationsSync: create-update` keep the ApplicationSet from deleting
workloads, or Applications, as a side effect.

`RollingSyncFixture.test_template_agrees_with_the_applications_it_replaces`
checks the generated Applications against the current `application.yaml` files
field by field. That check caught `master-instance-credential`'s own
`SkipDryRunOnMissingResource=true` and its longer retry, which is why each
element carries its Application's own policy rather than sharing one.

### What it orders

Argo CD's Progressive Syncs documentation describes the semantics below. The
model encodes them; they are not observed here.

- A step's Applications are synced only once every Application in each earlier
  step is Healthy. This holds on every rollout, not only at creation.
- The applicationset-controller disables each generated Application's automated
  sync and starts syncs itself. An Application selected by no step is never
  synced by it.
- The strategy applies only while the controller runs with
  `applicationsetcontroller.enable.progressive.syncs: "true"`.

| Dependency | On create | On update |
|---|---|---|
| `master-instance-credential` → `master-instance` | ordered (step 1 → 2) | **ordered** |
| `master-instance` → `saas-fabric-control-plane` | ordered (step 2 → 3) | **ordered** |
| `keycloak` → `master-instance` | ordered (root wave 20 → 30) | not ordered |
| `keycloak` → `saas-fabric-control-plane` | ordered | not ordered |

The scenarios below are pinned in `RollingSyncFixture`:

| Scenario | Current wiring | RollingSync |
|---|---|---|
| `master-instance` Degraded (convergence failed), control plane changed | control plane syncs | control plane **held** |
| `master-instance` Progressing (convergence running), control plane changed | control plane syncs | control plane **held** |
| Fix committed to `master-instance` while it is Degraded | it syncs | it syncs. Its own step is not held by its own health |
| `master-instance` Healthy again | — | control plane proceeds |
| Progressive syncs turned off with the fixture in place | — | **nothing syncs the chain** |

### What it does not order

- **Keycloak.** RollingSync orders only what one ApplicationSet generates.
  Keycloak, at root wave 20, precedes the whole set at creation and does not on
  an update. That is the same creation-only property as today, one link earlier
  in the chain.
- **Production.** Production runs no `master-instance`, and its control plane
  stays a hand-written Application.
- **Any other step capacity.** The fixture leaves `maxUpdate` at its default,
  `100%`. That is the only capacity the model reads. A step with
  `maxUpdate: 0` is never synced by the controller: upstream leaves it for a
  person to sync by hand. Any integer, or any other percentage, syncs only part
  of a step at a time. The model reports each of these as unmodelled rather
  than answering for it, and so does any strategy key it does not read, such as
  `deletionOrder`. Section 22 then fails.

---

## 3. Alternatives weighed

| | A. Creation-only plus degraded-state gates | B. RollingSync (candidate) | C. Fold the OIDC `SecurityPolicy` into `master-instance` | D. Rely on the control plane tolerating an unconverged client |
|---|---|---|---|---|
| Orders `master-instance` → control plane on update | no | yes, the whole Application | only the policy. The control plane's Deployments still race | no |
| A failed convergence holds the dependant back | no, a person reads the gate | yes, indefinitely | yes for the policy (its wave never runs), and the old policy stays attached | no |
| Controller-wide change | none | progressive-syncs flag, plus an applicationset-controller restart | none | none |
| Ownership migration of live Applications | none | three Applications move from the root to an ApplicationSet | the policy moves between Applications, a prune-then-create window | none |
| New RBAC or credentials | none | none in this repository | none | none |
| Fit for the durable pilot (D01-20) | **no** | yes, once verified | partial | no |

**A** is what D01-20 allows for the disposable trial. Section 5 defines its gates.

**C** is the cheapest real improvement and needs no controller change. It
narrows the gap without closing it: the dependency #44 names is the whole
control plane, not only its policy.

**D** describes today's failure mode, where EG reports the policy `Accepted` and
sign-in fails at Keycloak. It is an observation, not a gate.

A fifth option was rejected outright: a PreSync hook in the control plane that
polls `master-instance`'s health. It would need RBAC to read Applications, and
no RBAC is authorised.

**Recommendation for Brett:** keep A for the disposable trial, and adopt B
before the durable pilot. Adopt B only after the isolated verification in
section 7 has established the controller effects in section 4.

---

## 4. Controller and sync effects if B were adopted

This is exactly what would change in a running environment, in adoption order.
None of it happens by merging this PR.

| # | Effect | Scope | Source |
|---|---|---|---|
| E1 | `argocd-cmd-params-cm` gains `applicationsetcontroller.enable.progressive.syncs: "true"`. This repository already co-owns that ConfigMap through server-side apply (`argocd/runtime/server-insecure.yaml`) | the whole Argo CD instance: **every** ApplicationSet in it, including any `saas-fabric-hosting` owns | upstream docs |
| E2 | `argocd-applicationset-controller` must restart to read E1, because command-line parameters are read at startup. Until it restarts, the fixture's strategy is ignored and, under it, nothing syncs the chain | the controller pod | upstream docs. The restart itself is an operational act |
| E3 | The three Applications gain the `fieldstate.nz/rollout-step` label, an owner reference to `operator-sign-in`, and **no `syncPolicy.automated`** | the three Applications | fixture plus upstream docs |
| E4 | The `selfHeal` drift correction those three have today is replaced by controller-initiated syncs of whichever is OutOfSync, in step order. A drift in step 3 while step 2 is Degraded is **not corrected** until step 2 is Healthy | the three Applications | upstream docs (**to establish**: V4) |
| E5 | The `master-instance` convergence Job still re-runs on every sync operation of its Application, because `Replace=true,Force=true` is unchanged and `ApplyOutOfSyncOnly` stays absent. Which operations the controller starts, and with which prune setting, is the controller's choice, not the template's | the Job | **to establish**: V2 |
| E6 | `platform-root` stops authoring the three Applications and authors the ApplicationSet instead. Without the step in R1, its `prune: true` deletes them, and their `resources-finalizer` cascades | the root | repository semantics (`docs/architecture.md#deletion-semantics`) |
| E7 | `platform-root`'s waves no longer gate on the chain's health. The root waits on the ApplicationSet object's health, not on the Applications it generates. `operator-access` (wave 50, LucentRoot) is the only Application after wave 40, and it would stop waiting on the control plane at creation | the root's creation order | model (`RollingSyncFixture.test_the_root_no_longer_waits_on_the_chain`), plus the built-in ApplicationSet health (**to establish**: V1) |
| E8 | A manual sync from the UI or CLI of a step-3 Application bypasses the step order | operators | upstream docs |
| E9 | `scripts/check.py` §22 fails until `UPDATES_ORDERED` and `PROGRESSIVE_SYNCS_ENABLED` are changed in the same PR that adopts B | this repository | `RecordedOrdering` tests |

---

## 5. Degraded-state gates for the disposable trial (option A)

These gates hold while creation-only ordering remains. Each is a read-only
observation, and none requires new access. Any one failing means the change is
not in effect.

| Gate | Condition | Observation (read-only) |
|---|---|---|
| G1 | After any merge that touches `applications/core/{master-instance-credential,master-instance,saas-fabric-control-plane}/` or their overlays, `master-instance` reports `Synced` **at the merged revision** and `Healthy` before the control-plane change counts as effective | `argocd app get master-instance -o json`: `.status.sync.revision`, `.status.health.status` |
| G2 | `master-instance` Degraded or Progressing means console sign-in is an **unsupported state**, whatever the control plane reports. Treat it as a trial incident, not as a control-plane fault | as G1. The Job's `.status.conditions` in `operator-system` |
| G3 | `platform-root` reports Degraded while any child is Degraded, through the custom health assessment. A Degraded root is the trial's alarm | `argocd app get platform-root` |
| G4 | No single commit changes both `master-instance`'s and `saas-fabric-control-plane`'s sources during the trial. Changes land as two merges, the second after G1 holds for the first | PR review |
| G5 | A failed convergence is not re-run on its own: automated sync acts only on OutOfSync (`docs/master-instance-convergence.md`, cause 2). Recovery is a fix commit, or an authorised manual sync, which is itself gated | as G1 |
| G6 | Exit: the durable pilot does not start on option A. B, or a mechanism Brett chooses instead, must pass section 7 first | D01-20 |

`scripts/check.py` §22 enforces what can be enforced offline: every dependency
in section 1 stays ordered at creation, in both environments. Losing the health
assessment, or moving the control plane into wave 30, fails CI
(`RecordedOrdering`).

---

## 6. Prerequisites and risks

### Prerequisites for adopting B

| # | Prerequisite | Why |
|---|---|---|
| P1 | The exact Argo CD version on LucentRoot, read from the cluster or from `saas-fabric-hosting` | Progressive syncs are feature-gated, not GA, and their semantics have moved between releases: autosync handling, `deletionOrder`, and status fields. This repository records only a floor of 2.13 |
| P2 | An inventory of every other ApplicationSet in that Argo CD | E1 is instance-wide |
| P3 | `saas-fabric-hosting`'s agreement to the E1 key and the E2 restart | Hosting installs Argo CD and owns its lifecycle (`argocd/runtime/README.md`) |
| P4 | The isolated verification in section 7 passes on P1's version | D01-20: establish controller and sync effects first |
| P5 | `Delete=false` on resources that must survive an Application deletion (R2) | Their survival is not otherwise guaranteed |
| P6 | Environment binding for the ApplicationSet. The `environments/*` patches select `kind: Application`, and so does the environment-config component | The fixture hard-codes `lucentroot` and `main`. Adoption needs the same single-source binding |
| P7 | Brett's decision on the mechanism (issue #44 acceptance) | Product ownership |

### Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **Cascade deletion during migration.** Removing the three Applications from `platform-root` prunes them, and `resources-finalizer` deletes everything they manage: the control plane's Deployments, routes and OIDC policy; the gateway client secret; the convergence Job and its RBAC | A separate, earlier sync that annotates the three Applications with `argocd.argoproj.io/sync-options: Prune=false`, so the root orphans them rather than deleting them. Then introduce the ApplicationSet to adopt them. Verify adoption in V1 before any of it touches LucentRoot |
| R2 | **`Prune=false` does not protect against Application deletion.** `master-instance-state`, holding the OpenTofu state Secret `tfstate-default-master-instance`, carries `Prune=false` but not `Delete=false` | P5. Add `Delete=false` before any step that could delete the Application |
| R3 | The gateway client secret (`master-instance-credential`, `refreshInterval: "0"`) would be regenerated if deleted, and would no longer match Keycloak until `master-instance` reconverges | R1's ordering. With RollingSync in place, step 2 then reconverges before step 3, which is the point |
| R4 | **A Degraded step blocks indefinitely**, including unrelated control-plane fixes | Intended. Recovery is a fix to the failed step, which still syncs (`test_a_fix_to_the_failed_step_still_syncs`). Any break-glass manual sync of a later step is a gated act (E8) |
| R5 | **Rollback is not "turn the flag off".** Without progressive syncs, the generated Applications have no automated sync, and nothing syncs them (`test_without_progressive_syncs_nothing_syncs_the_generated_applications`) | Rollback means returning the three Applications to the root, protected as in R1, and then removing the flag. It is its own gated change |
| R6 | Instance-wide flag (E1) changes other teams' ApplicationSets | P2, P3 |
| R7 | Root creation order loses its wait on the chain (E7) | Accept for `operator-access`, which depends on the gateway and not on the chain, or step it within the set. Decide at adoption |
| R8 | The model is not the controller. Every "upstream docs" row above may differ on P1's version | Section 7 |

---

## 7. What is proven where

| Claim | Proven by | Status |
|---|---|---|
| Current manifests order the chain at creation and not on update, in both environments | `check.py` §22 on the CI render; `test_ordering.CurrentManifests` on source | **this PR, offline** |
| The 2026-09-22 sequence is what the current wiring permits | `CurrentManifests.test_failed_convergence_*` | **this PR, offline (model)** |
| The fixture is a valid ApplicationSet, generates exactly the chain, and agrees field by field with the Applications it replaces | kubeconform in `validate.sh`; `RollingSyncFixture` | **this PR, offline** |
| Under documented RollingSync semantics, failed or running convergence holds the control plane, and recovery unblocks it | `RollingSyncFixture` | **this PR, offline (model)** |
| Nothing added here is synced | `FixturesAreInert`; the PR's render-digest evidence | **this PR, offline** |
| Adopting B or enabling the flag cannot happen silently | `RecordedOrdering` | **this PR, offline** |
| The controller behaves as modelled on P1's version | V1–V6 below | **gated** |

### Isolated sync verification (separately authorised, not LucentRoot)

Run on a disposable cluster with P1's Argo CD version, against a branch of this
repository with the chain's workloads replaced by stand-ins whose health can be
forced: a Job that fails or succeeds on demand. These are the tests #44's
acceptance asks for.

| # | Verification |
|---|---|
| V1 | Migration: the Applications are created by the root, orphaned with `Prune=false`, then adopted by the ApplicationSet. No managed resource is deleted, and the owner references and labels are as in E3 |
| V2 | Repeated update sequencing: commit to steps 2 and 3 together, ten times. Step 3's sync never starts before step 2 is Healthy at the new revision. Record which sync options and prune setting the controller uses (E5) |
| V3 | Failed child convergence: force step 2's Job to fail. Step 3 stays unsynced through repeated commits to it, and the ApplicationSet status names the wait |
| V4 | Drift while blocked: mutate a step-3 resource while step 2 is Degraded. Record whether and when it is corrected (E4) |
| V5 | Recovery: commit a fix to step 2. Step 2 syncs and becomes Healthy, then step 3 proceeds |
| V6 | Rollback: R5's sequence restores hand-written Applications with automated sync, and nothing is deleted |

---

## 8. Gated operational actions

None of these is authorised by this PR or by merging it. Each needs the approval
shown before it is carried out. "Riley" means approval of the exact head after
review and green CI. "Brett" means a product or security decision. Anything that
would change the running environment also needs that change named and
authorised explicitly, as D01-20 requires.

| # | Action | Changes the running environment? | Needs |
|---|---|---|---|
| O1 | Choose the update-ordering mechanism: B, or another | no | **Brett** (#44 acceptance) |
| O2 | Read LucentRoot's Argo CD version and its ApplicationSet inventory (P1, P2), through the established read-only route | no, read-only | the existing read-only route. No new access |
| O3 | Agree the instance-wide progressive-syncs flag and controller restart with `saas-fabric-hosting` (P3) | no, agreement only | **Brett** |
| O4 | Stand up the disposable verification cluster and run V1–V6 | not LucentRoot, but it is a new environment, which #108 does not authorise | **Brett** (environment), **Riley** (verification plan and branch) |
| O5 | Merge `Delete=false` on `master-instance-state` and on any other resource that must survive Application deletion (P5, R2) | **yes**: a LucentRoot sync | **Riley**, plus explicit authorisation of the change |
| O6 | Merge `Prune=false` sync-options on the three Applications (R1, step 1) | **yes**: the root re-applies three Application objects | **Riley**, plus explicit authorisation |
| O7 | Merge the progressive-syncs key into `argocd/runtime/` (E1) | **yes**: instance-wide controller configuration | **Riley** and **Brett**, after O3 |
| O8 | Restart `argocd-applicationset-controller` (E2) | **yes** | **Brett** (hosting-owned workload), at the time O7 lands |
| O9 | Merge the ApplicationSet, remove the three Applications from `environments/lucentroot`, and flip `UPDATES_ORDERED` / `PROGRESSIVE_SYNCS_ENABLED` in `check.py` (E3–E9) | **yes**: ownership migration on LucentRoot | **Riley** and **Brett**, after V1–V6 pass |
| O10 | Any manual sync, refresh-with-sync, or forced Job re-run on LucentRoot, including recovery under G5 and break-glass under R4 | **yes** | **Riley**, per occurrence |
| O11 | Promote any of O5–O9 to `production` (fast-forward the branch, `docs/releases.md`) | **yes**: production | **Brett** |
| O12 | Leave option A and start the durable pilot (G6) | **yes**, by decision | **Brett**, after O9 is verified on LucentRoot |
