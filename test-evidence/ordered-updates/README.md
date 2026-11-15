# V1–V6 evidence (issue #44, RollingSync candidate)

Throwaway kind cluster on this VM, kube-proxy `nftables`, deleted after each run.
Not LucentRoot. Stand-in workloads only. No cloud resources, DNS, or secret values.

Argo CD is Helm chart **10.11.6**, app **v3.5.6**. That pin is
`ARGOCD_CHART_VERSION` / `ARGOCD_APP_VERSION` in `FieldstateNZ/lucentroot-gitops`
`versions.env`, and it is an assumption to re-check after the LucentRoot rebuild.
`lucentroot-gitops` sets `applicationSet.replicas: 0` and does not set
`applicationsetcontroller.enable.progressive.syncs`. The throwaway runs set
replicas to 1 and the flag to `"true"`. Those two changes are not in this
repository's install manifests. They are listed for Brett under O1.

Kubernetes UIDs in these files are `uid:` plus 12 hex characters of sha256.
The writer refuses a raw UID. The same object always hashes the same way.

Runs are kept. Later runs do not delete earlier ones.

| Run | Mode | What it is |
|---|---|---|
| `runs/20261115T035119Z-recorded/` | the first recorded V1–V6 | 1-second poll. V2 held in 10 of 10 in this folder. The 11th is the earlier attempt described below, not a file in this folder. V6 cleared owner references by hand. |
| `runs/20261115T040913Z-architect/` | architect | Refresh-skew reproduction, and an orphan-delete attempt. |
| `runs/20261115T041538Z-full/` | full, aborted | Stopped during V2. |
| `runs/20261115T041942Z-full/` | full, aborted | Stopped during V2. |
| `runs/20261115T042405Z-full/` | full, aborted | Stopped before V1 wrote UIDs. |
| `runs/20261115T042618Z-full/` | full, aborted | Stopped during V2 while the hook check raced Job status. |
| `runs/20261115T043012Z-full/` | `full` | The repeated series. `run.json` records mode `full` and `scriptCommit` `c5e8a7f`. The process was the working tree on top of that commit (UID fingerprints, hook status wait). |

## Repeated series (`20261115T043012Z-full`)

| Test | Result | What was observed |
|---|---|---|
| V1 | pass | No resource fingerprint changed. Owner is ApplicationSet `operator-sign-in`. Step labels are 1, 2, 3. |
| V2 | 48 of 50 | Five sets of ten joint commits. Scored on ApplicationSet `lastTransitionTime`, plus a PreSync Job that fails unless step 2's convergence Job has succeeded. 48 held. `s5-2` and `s5-4` were scored not-ok because a step-3 operation was `Running` before step 2 was Healthy at the new revision. The control-plane marker was still the previous value in both, and no sync result for the new revision was recorded. |
| V3 | pass | Step 2's Application health stayed `Degraded`. The ApplicationSet step status for step 2 stayed `Progressing`. Step 3 stayed `Waiting` across two commits that touched only step 3. |
| V4 | pass, weak | The live edit stayed at `v4-mutated` for 90 seconds and step 2 stayed `Degraded`. There is no automated self-heal on the generated Applications, so this does not show a heal being held. |
| V5 | pass | A fix commit synced step 2, then step 3. |
| flag-off | pass | With progressive syncs removed on the throwaway cluster, the next commit did not change the live marker. |
| V6 | pass | `kubectl delete applicationset operator-sign-in --cascade=orphan`. No resources finalizer on the generated Applications. Owner references were not cleared by hand. No fingerprint changed. Restoring the hand-written Applications put `automated.prune` and `selfHeal` back. A following commit did not apply within 120 seconds. |

`prune` was unset on the syncs the controller started, including step 3 where the stand-in set `automated: {enabled: false, prune: true}`.

## The 11th commit

One earlier attempt on this same v3.5.6 applied step 3's new marker while step 2 was still OutOfSync. Step 3's operation started at 03:25:24Z and step 2's at 03:25:25Z for revision `165f8e00`. The ApplicationSet already owned the apps. Step 2 still reported Healthy and Synced at the previous revision. Count that series as held in 10 of 11, not as a pass. The reproduction is `runs/20261115T040913Z-architect/race-refresh-skew.json`.
