# V1–V6 evidence (issue #44, RollingSync candidate)

Recorded run: 2026-11-15 16:51–16:56 NZDT (03:51–03:56 UTC).
Operator: Cursor cloud agent, work item W2.
Cluster: kind `w2-ordered-updates` on this VM, kube-proxy `nftables`, deleted after the run.
Not LucentRoot. Stand-in workloads only. No cloud resources, DNS, or secret values.

Argo CD is Helm chart **10.11.6**, app **v3.5.6** (`quay.io/argoproj/argocd:v3.5.6`, git `c2b1e301`).
That pin is `ARGOCD_CHART_VERSION` / `ARGOCD_APP_VERSION` in `FieldstateNZ/lucentroot-gitops` `versions.env`.
`saas-fabric-hosting` is not a repository this account can see. `FieldstateNZ/infrastructure` pins chart `10.1.3`, which `docs/migrating-lucentroot.md` says not to carry across.
`lucentroot-gitops` sets `applicationSet.replicas: 0`. This run set replicas to 1 and `applicationsetcontroller.enable.progressive.syncs: "true"`.

| Test | Result | What was observed |
|---|---|---|
| V1 | pass | Adoption changed no resource UID. Owner is ApplicationSet `operator-sign-in`. Labels `fieldstate.nz/rollout-step` are 1, 2, 3. `syncPolicy.automated` is absent. |
| V2 | pass | 10/10 joint commits. The control-plane marker changed only once step 2 was Healthy and Synced at the new revision, including a Progressing sample on every commit. |
| V3 | pass | Step 2 went Degraded. Two later commits to step 3 did not start a sync. ApplicationSet status: step 3 `Waiting` ("Application has pending changes"), condition `ApplicationSet is performing rollout of step 2`. |
| V4 | pass | A live edit to `v4-mutated` was still there after repeated refreshes. Step 3's operation did not start. |
| V5 | pass | A fix commit synced step 2 to Healthy, then step 3 synced to the git marker `v3-b`. State Secret UID unchanged from V1. |
| V6 | pass | After owner references were removed, restoring hand-written Applications changed no UID. Deleting the master-instance Application removed the Job and left `tfstate-default-master-instance` at UID `ef0db8e6-f001-4d8f-bc0f-05ec134c50d5`. |

State namespace UID `3e9be30a-5ee0-4c00-98bd-b20c1f8d0a6a` was the same before migration, after rollback, and after the Application was deleted. The stand-in carries `Prune=false,Delete=false`. The live LucentRoot namespace still has only `Prune=false`.

Controller sync (E5), from the first V2 operation: initiated by `applicationset-controller`, `syncOptions: [CreateNamespace=true]`, `retry.limit: 2` (the stand-in template, not the controller default of 5). `prune` was unset. The candidate drops `automated`, and this controller copies prune only from an enabled automated policy.

History with timestamps is `argocd-app-history.txt` (Application `.status.history`, the record `argocd app history` prints). Per-commit samples are `v2-rollouts.json`.

## Not in the recorded pass

- One earlier attempt on this same v3.5.6, abandoned before the recorded ten commits, applied step 3's new marker while step 2 was still OutOfSync. Step 3's operation started at 03:25:24Z and step 2's at 03:25:25Z for revision `165f8e00`. It did not reproduce in the recorded ten. The gate was observed, not shown to be impossible to race.
- Deleting the ApplicationSet without clearing owner references first let Kubernetes garbage-collect two of the three Applications. `preserveResourcesOnDeletion` and `applicationsSync: create-update` are supposed to clear those references, and on this version that lost the race. The recorded V6 removes the references, then deletes the set.

RollingSync is still a candidate. O1 is open. Nothing under `environments/` was changed.
