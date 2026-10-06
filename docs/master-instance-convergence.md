# master-instance convergence on LucentRoot

Diagnosis for platform #48, *[W2] Diagnose and prove state-aware master-instance
convergence*. It was written from source only, with no cluster access, against
platform `main` at `0ff5d66` and the application repository at `1f46788`. Every
cluster fact below is either quoted from a dated record or inferred. Nothing
here was observed directly.

**Merging the repair is a LucentRoot deployment.** LucentRoot follows `main`
(`environments/lucentroot/kustomization.yaml:37-52`), and the convergence Job
re-runs on every sync of this Application. A merge therefore needs its own
authorisation, separate from reviewing this document.

---

## What is known, and how

| When | Fact | Source | Kind |
|---|---|---|---|
| 2026-09-22 09:04Z | #43 merged as `355fe6d`, with the roster `["brett@fieldstate.nz"]` and `adoptExisting: "true"` | git, #43 | observed in Git |
| 2026-09-22 | "three Job runs failed at plan time on `data.keycloak_user.operator["brett@fieldstate.nz"]` — *user not found*". Also: "adoption worked (role and console client adopted, `saas-fabric-gateway` created, `frontendUrl` converged)" | PR #45 body | reported by its author, not re-observed |
| 2026-09-22 09:06:22Z | `master-instance` Degraded. `saas-fabric-control-plane` (wave 40) synced anyway at 09:06:57Z | PR #45, #44 | reported, not re-observed |
| 2026-09-22 08:22Z | #70 comment: "the two grants made by hand on the 17th are now converged" for `brett@fieldstate.nz` | saas-fabric #70 | contradicts PR #45 on which account holds them |
| Oct 3 | "degraded master-instance convergence", with no cause given | #48, #49, saas-fabric #108 | inventory, no logs |
| 2026-09-22 → 0ff5d66 | No commit after `355fe6d` touches `applications/core/master-instance/` | `git log -- applications/core/master-instance` (GitHub API) | observed in Git |
| 2026-09-22 | PR #45 (`fd12327`, roster → `["admin"]`) has green CI on its own base `355fe6d`. It has not been re-run on `0ff5d66` | PR #45 check runs | observed in GitHub |

## Causes

Line numbers refer to `main` at `0ff5d66` unless stated otherwise.
`M/` = `applications/core/master-instance/`.

| # | Cause | Evidence in source | How it shows | Likelihood |
|---|---|---|---|---|
| 1 | **The roster names an account the master realm does not hold.** `brett@fieldstate.nz` is looked up by username, and `keycloak_user` returns an error when nothing matches | `M/overlays/lucentroot/master-instance-config.yaml:40`; `M/base/module/main.tf:287-291`; provider 5.9.0 `data_source_keycloak_user.go:67-72` (`user with username %s not found`) | Pod log: `Error: user with username brett@fieldstate.nz not found` on `data.keycloak_user.operator`. Job `BackoffLimitExceeded` after 3 pods (`backoffLimit: 2`, `job.yaml:53`). App `Degraded` | **High.** Reported for 2026-09-22, and the input has not changed since |
| 2 | **A failed Job is not re-run on its own,** so Oct-3 most likely shows the 2026-09-22 failure, not a new one | `M/application.yaml:46-75`: `automated` sync acts on OutOfSync. `retry` covers a failed sync *operation*, not a Job that fails after the operation succeeded. No Git change to this Application since `355fe6d` | `argocd app get`: `Synced` and `Degraded`, with the last operation on 2026-09-22. Job `creationTimestamp` is 2026-09-22 | **High** (inferred from Argo CD semantics and Git history) |
| 3 | **Lookups were read at apply time on the first run,** so the missing-account failure came *after* the realm had been written. This is a latent defect, not the reason the app is Degraded today | `main.tf:150,289`: both data sources use `realm_id = keycloak_realm.master.id`. OpenTofu 1.12.6 treats a data source's reference to a managed resource as `depends_on` (`internal/tofu/transform_reference.go`, `nodeDependencies`) and defers the read while that resource has a pending change (`node_resource_abstract_instance.go`, `dependenciesHavePendingChanges` / `ReadBecauseDependencyPending`). On import the provider stores `attributes` only for keys already in state (`resource_keycloak_realm.go:1563-1570`). The first plan is therefore always an in-place update adding `frontendUrl`, which is a pending change | Run 1 logs `data.keycloak_user.operator[...] will be read during apply` (`# depends on a resource or a module with changes pending`). The realm, role, console and gateway apply, then the lookup fails. Runs 2–3, with the realm now a no-op, fail at plan. This matches PR #45 reporting both "adoption worked" and "failed at plan time" | **High as a mechanism** (source-verified). Which objects reached state on run 1 is unverified |
| 4 | **Removing a roster name revokes both roles from that account,** whatever `exhaustive` says. This is a latent hazard for PR #45's stated rollback, not a current cause | `main.tf:299-316`. Provider 5.9.0 `resource_keycloak_user_roles.go:193-216`: delete removes every role in `role_ids`. README `:115` says the opposite ("Removing a name revokes nothing") | If PR #45 merged and was later reverted, the next sync would remove `fabric-operator` **and master `admin`** from `admin`, the identity this Job authenticates as. Every run after that fails with 403, and nobody holds master `admin` any more. Recovering would need a Keycloak-side bootstrap, which is a hand step | **Not current. Severe if triggered** |
| 5 | **Stale state lock.** `Replace=true,Force=true` deletes a running pod without releasing the `kubernetes` backend's Lease | `job.yaml:51`, `apply.sh:28-38`, `state-rbac.yaml:49-50` | `Error acquiring the state lock` after 60 s, naming the Lease `lock-tfstate-default-master-instance` | **Low.** It needs a sync during a run (a manual sync, or a replace loop, #8) |
| 6 | **Drift remains after apply** (the closing `-detailed-exitcode` plan exits 2) | `apply.sh:45`. Candidates: attributes on the adopted `saas-fabric-console` / `fabric-operator` that the module declares and Keycloak normalises. Realm attributes are filtered by the provider (`resource_keycloak_realm.go:1563-1570`), so `frontendUrl` alone should not drift | The apply succeeds, then the closing plan prints `Plan: 0 to add, N to change` and exits 2 | **Low.** It is reachable only once #1 is cleared. Unverified for the adopted console client |
| 7 | **Inputs missing at pod start:** mirrored `keycloak-admin` (ESO `SecretStore` not Ready) or `saas-fabric-gateway-oidc` | `job.yaml:118-147`, `keycloak-admin-mirror.yaml:137-195` | The pod is stuck in `CreateContainerConfigError` until `activeDeadlineSeconds: 600` (`job.yaml:62`), then `DeadlineExceeded`. Or the `SecretStore`'s own health makes the app Degraded with nothing in the Job's log | **Low.** The 2026-09-22 runs got as far as planning, so the inputs resolved then |
| 8 | **Argo CD sees the Job as perpetually OutOfSync** and `selfHeal` replaces it repeatedly, killing runs mid-apply (which then leads to #5) | `application.yaml:48` `selfHeal: true` with `Replace=true,Force=true` | `argocd app history` shows many syncs. The Job's `creationTimestamp` keeps moving | **Low.** Nothing in source suggests it |
| 9 | **Admin credential mismatch:** `identity/keycloak-admin` was regenerated after Keycloak's first start, so the mirror (`refreshInterval: "0"`) or Keycloak holds a different value | `keycloak-credentials/base/password.yaml:24-38`, `keycloak-admin-mirror.yaml:165-180` | `401` / `invalid_grant` at provider configuration, at plan, before any resource | **Low** |
| 10 | **Lost state, or a rebuilt Keycloak.** State lost while Keycloak is kept: the gateway is re-created and gets a 409 (README "State"). Keycloak database rebuilt with `adoptExisting: "true"`: the provider's `import = true` finds no `fabric-operator` / `saas-fabric-console` and errors | `state-namespace.yaml:44` (`Prune=false`); provider 5.9.0 `resource_keycloak_role.go:107-125` (`GetRoleByName` error on import) | `409 Conflict` on `keycloak_openid_client.gateway`, or a role or client "not found" on create | **Low for Oct 3** (no rebuild recorded). **Medium for any future rebuild** of LucentRoot, which the platform's own docs treat as routine |
| 11 | Provider, lock-file or `init` failure; state RBAC; `frontendUrl` breaking the provider's own token | `.terraform.lock.hcl`; `state-rbac.yaml:37-53`; `main.tf:109-136` | Fails at `init`, or a 403 on the state Secret, or a 401 after the realm update | **Very low.** The 2026-09-22 runs passed `init`, read state and reached the lookup |
| — | Hook ordering, and the wave gate not holding on updates | `application.yaml`, #44 | This explains why wave 40 synced while this app was Degraded. It does not explain the Degraded state itself | Not a cause |

### Ranking, in one paragraph

LucentRoot's `master-instance` is most likely Degraded today for the same
reason it was on 2026-09-22. The roster names `brett@fieldstate.nz`, a username
the master realm does not hold (#1). Nothing has made Argo CD run the Job
since (#2).

The first of those 2026-09-22 runs most likely changed the realm before
failing, because the lookups were deferred into apply (#3). Later runs failed
at plan. So the realm probably already holds the adopted role and console
client, the gateway client and `frontendUrl`, and the state probably records
them. That is inferred. The runbook's steps 4–5 confirm it.

Everything else on the list is low likelihood. The steps below rule each one
in or out.

## Read-only runbook

Every step below reads and nothing writes. It needs cluster read access, and
an authorised operator runs it. Redact before pasting output anywhere. Step 5
handles secret material even though it prints none, so it needs authorisation
of its own.

1. **Application state and age.** This tests #2 and #8.
   ```console
   $ argocd app get master-instance --show-operation
   $ argocd app history master-instance
   $ kubectl -n argocd get application master-instance \
       -o jsonpath='{.status.health.status} {.status.sync.status} {.status.operationState.finishedAt}{"\n"}'
   ```
   Expect `Degraded Synced 2026-09-22T…`, with no history entry after the
   `355fe6d` revision. Many entries, or a recent `finishedAt`, point to #8.
2. **The Job and its pods.** This tests #1, #7 and #9.
   ```console
   $ kubectl -n operator-system get job master-instance-converge \
       -o jsonpath='{.metadata.creationTimestamp} {.status.conditions[*].reason}{"\n"}'
   $ kubectl -n operator-system get pods -l job-name=master-instance-converge \
       --sort-by=.metadata.creationTimestamp
   $ kubectl -n operator-system describe pods -l job-name=master-instance-converge | grep -E 'Reason|Message|State'
   ```
   Interpret the results as follows:
   - `BackoffLimitExceeded` with three pods means #1, #5, #6 or #9. Read their logs.
   - `DeadlineExceeded` with `CreateContainerConfigError` means #7.
3. **Logs, oldest pod first.** This tests #1, #3, #5, #6, #9 and #10.
   ```console
   $ for p in $(kubectl -n operator-system get pods -l job-name=master-instance-converge \
         --sort-by=.metadata.creationTimestamp -o name); do
       echo "== $p"; kubectl -n operator-system logs "$p" | grep -nE \
         'will be read during apply|not found|state lock|409|401|invalid_grant|Plan:|Apply complete|cannot be destroyed|Error:'
     done
   ```
   | Log signature | Cause |
   |---|---|
   | pod 1: `will be read during apply` then `Apply complete` lines or creations, then `user with username … not found`. Pods 2–3: the same error with no apply | #1 + #3 (expected) |
   | `user with username … not found` in every pod, no apply anywhere | #1 alone (pod 1 was already a re-run) |
   | `Error acquiring the state lock` | #5 |
   | final plan `Plan: 0 to add, N to change` after `Apply complete` | #6 |
   | `401` / `invalid_grant` before any plan output | #9 |
   | `409` on `keycloak_openid_client.gateway`, or `not found` on `keycloak_role.fabric_operator` / `keycloak_openid_client.console` create | #10 |
   Pods may already have been garbage-collected. If they have, the Job's
   conditions and the Argo CD operation message are what remain.
4. **State and lock: existence only, no contents.** This tests #3, #5 and #10.
   ```console
   $ kubectl -n master-instance-state get secret tfstate-default-master-instance \
       -o jsonpath='{.metadata.creationTimestamp} rv={.metadata.resourceVersion}{"\n"}'
   $ kubectl -n master-instance-state get lease lock-tfstate-default-master-instance -o yaml
   ```
   - A state Secret created 2026-09-22 means run 1 wrote state, which confirms #3's partial apply.
   - No state Secret means no apply ever completed. The next run is a first run.
   - A Lease that holds a lock ID with no pod running means #5.
5. **What state records: addresses only.** This needs separate authorisation,
   because the state contains the gateway client secret, even though this
   pipeline prints only resource addresses:
   ```console
   $ kubectl -n master-instance-state get secret tfstate-default-master-instance \
       -o jsonpath='{.data.tfstate}' | base64 -d | gunzip \
     | jq -r '.resources[] | "\(.mode).\(.type).\(.name)"'
   ```
   Once the repair below is merged, every Job log starts with this same list,
   printed by `apply.sh`, and this step is no longer needed.
6. **What the realm holds.** This needs separate authorisation, because it
   uses the bootstrap administrator. Each command is a `GET`:
   ```console
   $ kcadm.sh get users -r master --fields username
   $ kcadm.sh get clients -r master -q clientId=saas-fabric-gateway --fields clientId
   $ kcadm.sh get-roles -r master --uusername admin
   ```
   This resolves the conflict between #70 and PR #45: whether
   `brett@fieldstate.nz` exists, and which account holds `fabric-operator`.
7. **Blast radius on the wave order** (#44):
   ```console
   $ kubectl -n argocd get applications -o custom-columns=NAME:.metadata.name,WAVE:.metadata.annotations.argocd\.argoproj\.io/sync-wave,HEALTH:.status.health.status,SYNC:.status.sync.status
   ```

## The repair

Branch `claude/48-master-instance-convergence`. It does not change the roster.

1. **Every lookup is read at plan time** (`M/base/module/main.tf`).
   `data.keycloak_role.admin` and `data.keycloak_user.operator` take
   `realm_id = "master"`, not `keycloak_realm.master.id`. The value is the
   same, because the provider's realm id is the realm name. With no reference
   to a managed resource, OpenTofu has nothing to defer on. A missing account
   now fails the plan on every run, the first included, before anything is
   written. Managed resources keep their references, so the change plans no
   difference against existing state.
2. **Grants cannot be revoked by a roster edit** (`main.tf`,
   `keycloak_user_roles.operator`). `lifecycle { prevent_destroy = true }`
   turns the removal of a name into a plan-time refusal, instead of a
   provider delete that strips `fabric-operator` and master `admin`. How an
   operator is retired is still D01-6b, the product owner's decision. This
   change keeps that from being decided by accident.
3. **The Job says what it started from and how it failed**
   (`M/base/module/apply.sh`).
   - It logs the state's resource addresses before planning. That tells a
     first run apart from re-runs and from lost state.
   - It runs a separate plan gate with `-detailed-exitcode`:
     - Exit 0 is an already-converged re-run. Nothing is applied, and no
       state write or lock-holding apply happens.
     - Exit 1 prints that nothing was applied, and names the roster, removal
       and lock cases.
     - Exit 2 proceeds to apply and then to the existing drift check.
   - An apply failure says the apply was partial and points here.
4. **The checker holds both properties** (`scripts/check.py`,
   `check_master_instance_lookups_precede_apply`, with regression tests in
   `scripts/test_check.py`). Two things fail the checker:
   - any `data` block in the module that names a managed resource or
     declares `depends_on`;
   - any `keycloak_user_roles` without `prevent_destroy = true`.

   Both properties fail against the module on `main`.
5. **Docs.** The module README's two false claims are corrected: that a
   missing account always fails at plan, and that removing a name revokes
   nothing. It also gains the note that a failed Job is not retried on its own.

### What a sync does after merge, by roster

None of these cases includes a destroy. `prevent_destroy` is on every managed
resource.

| Roster on `main` | Next run | Health |
|---|---|---|
| `["brett@fieldstate.nz"]` (today) | It logs state, then the plan fails on the lookup. Nothing is written, and the message names the roster | Degraded, now with an explanation |
| `["admin"]` (PR #45) | The plan adds `keycloak_user_roles.operator["admin"]` (two roles PR #45 says `admin` already holds) plus whatever run 1 did not finish. Apply, then a clean second plan | Healthy, if #6 does not apply |
| `[]` | The plan touches no grant. Apply covers only what run 1 did not finish | Healthy, if #6 does not apply. The hand-made `fabric-operator` grant stays unmanaged |

**Choosing among these is D01-6, and belongs to Brett.** This branch works
with all three, and with PR #45 in either merge order. Two notes for when
PR #45 is rebased onto it:

- **README conflict.** PR #45 edits the module README's "Who the operators
  are" next to the paragraph this branch rewrites, so the README will
  conflict textually.
- **Stale comment.** PR #45's new comment in `master-instance-config.yaml`
  repeats "Removing a name revokes nothing", which is false without this
  branch's guard. It should say what the README now says.

Merge this branch **before or together with** PR #45. On its own, PR #45's
stated rollback ("revert to the earlier roster") would remove master `admin`
from the only administrator (#4).

### Recovery after a partial apply

Reverting in Git does not undo what an apply already wrote. The state records
every operation that completed, and the next run plans from it. So:

- **Do not delete or edit** anything in Keycloak by hand to "reset" a run. A
  hand-deleted object that state still records is re-created. A hand-created
  one that state does not record ends in a 409.
- **Interrupted mid-write (#5):** the documented `tofu force-unlock` against
  the Lease (module README, "A Job replaced mid-run"). This is a write, and
  needs authorisation.
- **Lost state (#10):** add a temporary OpenTofu `import` block for exactly
  the resource that 409s, apply once, then remove the block (module README,
  "State"). Never re-create the object.
- **Rebuilt Keycloak with `adoptExisting: "true"`:** adoption cannot succeed
  on a realm that has no hand-made objects. `adoptExisting` must not change
  for an environment, because `import` is `ForceNew`. A rebuilt Keycloak is
  therefore a new history for LucentRoot, and recovering it is a separate,
  authorised decision: drop the state and set `false` for the new realm. It
  is not something to improvise.

### Rollback

- **Revert the merge commit.** The revert takes `prevent_destroy` off the
  grants and puts back the `keycloak_realm.master.id` references. Neither
  changes any managed resource's planned value, so the revert's own run plans
  nothing new beyond what the roster already implies.
- **What the revert brings back:** the deferred-lookup behaviour (#3) and the
  revocation hazard (#4).
- **The revert is a deployment too.** It is a merge to `main`, so it syncs
  LucentRoot and re-runs the Job (`Replace=true,Force=true`). It needs the
  same authorisation as the merge.

## Validation done, and not done

- `python3 -m unittest discover -s scripts -p 'test_*.py'`: 67 tests pass,
  including 14 new ones. Each new check fails against the module on `main`.
- `scripts/check.py` was run against a partial render: every Kustomize-sourced
  Application, with Helm charts skipped because the authoring sandbox could
  not reach the chart repositories.
  - Seven problems on both `main` and this branch, the same seven. All of them
    are references to the skipped Helm-rendered Services (`keycloak-http`,
    `openbao`).
  - The only rendered difference is the `master-instance-module` ConfigMap,
    meaning its contents and its hash suffix.
  - CI runs the full render.
- `python3 -m yamllint --strict .` passes.
- **Not run: `tofu fmt`, `tofu validate`, `tofu plan`.** No OpenTofu binary
  was available offline. The HCL change is limited to two attribute values
  and one `lifecycle` block, and the Job runs `tofu validate` before planning,
  so a syntax error would fail the Job at `validate`, before any plan.

## What remains unverified

- Every LucentRoot fact after 2026-09-22, including that Oct 3's Degraded is
  the 2026-09-22 Job (#1, #2). Steps 1–3 settle it.
- What run 1 wrote, and what state holds (#3). Steps 4–5 settle it.
- Whether `brett@fieldstate.nz` exists in the master realm, and which account
  holds the hand-made `fabric-operator` grant. #70 and PR #45 disagree. Step 6
  settles it.
- Whether the adopted `saas-fabric-console` or `fabric-operator` drifts on
  every plan (#6). The first successful run's closing plan settles it.
- That the bootstrap administrator holds master `admin` as a direct mapping
  rather than through a group. If it holds it through a group, #4's
  revocation would remove only the direct mappings.
- The repair's behaviour under OpenTofu itself. This was reasoned from the
  OpenTofu 1.12.6 and keycloak/keycloak 5.9.0 sources, not executed. The
  fault-injection tests #48 asks for need the separately authorised trial
  environment:
  - a missing user;
  - a lock held by a killed pod;
  - a roster removal against a populated state.
