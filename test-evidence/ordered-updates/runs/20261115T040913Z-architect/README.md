# Refresh-skew reproduction, 2026-11-15 17:09 NZDT (04:09 UTC)

Argo CD chart 10.11.6 / app v3.5.6. The ApplicationSet already owned all three
Applications (`ownedByApplicationSet: true`).

A joint commit was pushed and only step 3 was hard-refreshed. At
2026-11-15T04:10:01. the control-plane marker was already `race-b`
(revision `4fb95e73`) while master-instance was still Healthy and Synced at
the previous revision `988fef80`. Step 3's operation had started at 04:10:01Z.
Step 2 had not been compared against the new commit, so its progressive-sync
status was still Healthy. The gate reads that per-Application compare. It does
not require the compared revision to be the commit step 3 is syncing.

This is not migration, and it is not a health value that was already
OutOfSync. `race-refresh-skew.json` has the samples.

The same run deleted the ApplicationSet with `--cascade=orphan` and did not
patch owner references by hand. Resource UIDs did not change
(`v6-orphan.json`). That check looked for the wrong finalizer string
(`resources-finalizer.argoproj.io`). The Applications did carry
`resources-finalizer.argocd.argoproj.io`. Orphan cascade kept the Application
objects, so the finalizer did not run. The candidate template has since
dropped that finalizer; a later run records the corrected check.
