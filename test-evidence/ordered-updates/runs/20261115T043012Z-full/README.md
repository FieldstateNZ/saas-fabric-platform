# V1–V6 evidence

- When: 2026-11-15 18:00:28 NZDT (2026-11-15T05:00:28.202582Z)
- Operator: Cursor cloud agent, work item W2. Throwaway kind cluster on this VM. Not LucentRoot.
- Argo CD: chart 10.11.6, app v3.5.6, from FieldstateNZ/lucentroot-gitops versions.env.
- Stand-ins only. No cloud resources, no DNS, no LucentRoot API.

| Test | Result |
|---|---|
| V1 | pass |
| V2 | fail |
| V3 | pass |
| V4 | pass |
| V5 | pass |
| flag-off | pass |
| V6-orphan | pass |

Structured records: `results.json`, `v1-migration.json`, `v2-rollouts.json`,
`v3-failed-step.json`, `v4-drift.json`, `v5-recovery.json`, `v6-rollback.json`,
`argocd-app-history.txt`.

