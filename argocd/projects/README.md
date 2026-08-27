# Argo CD projects

Every platform Application belongs to an `AppProject` that constrains which
repositories it may read and which namespaces and cluster-scoped kinds it may
write. The unrestricted `default` project is not used.

| Project | Defined in | Managed by | Scope |
|---|---|---|---|
| `saas-fabric-platform` | [`saas-fabric-platform.yaml`](saas-fabric-platform.yaml) | created by the bootstrap set, reconciled by Argo CD | core platform namespaces plus an enumerated list of cluster-scoped kinds |
| `saas-fabric-catalogue` | [`saas-fabric-catalogue.yaml`](saas-fabric-catalogue.yaml) | created by the bootstrap set, reconciled by Argo CD | the `catalogue` namespace only, plus `Namespace` so it can create it |

## Created once, reconciled thereafter

The root Application cannot start without the project that constrains it, so
the bootstrap set creates both projects. After that they are reconciled like
everything else — **initial creation and ongoing ownership are different
things**, and conflating them is what used to make every project change require
someone with `kubectl`.

```text
bootstrap set        creates the projects, then the root Application
root Application     adopts and reconciles them from then on
```

This replaces an earlier arrangement in which the platform project sat outside
reconciliation, on the argument that an Application able to rewrite its own
project could widen its own privileges.

**That argument was false.** `AppProject` is a namespaced `argoproj.io`
resource; this project permits `group: "*", kind: "*"` for namespaced resources
and lists `argocd` among its destinations. The root Application could already
create and update AppProjects. Keeping this one file out of Git bought no
boundary at all, and cost a manual step after every change to it.

The boundary that does exist is Git: whoever can merge to `main` can change what
runs. Protecting `main` is the control, not withholding one file from
reconciliation.

### Why the platform project is not pruned

It carries `argocd.argoproj.io/sync-options: Prune=false`. Every platform
Application runs in it, so pruning it because it left a rendered manifest would
take the platform down with it. Retiring it stays a deliberate act rather than a
reconciliation side effect.

## Adding a cluster-scoped kind

Both projects enumerate the cluster-scoped kinds they permit rather than
wildcarding them, so a new chart cannot quietly acquire cluster-wide privilege.
The cost is that forgetting one is a **sync** failure, not a render failure:

```text
resource external-secrets.io:ClusterSecretStore is not permitted
  in project saas-fabric-platform
```

`scripts/check.py` now reports that during validation instead, by comparing what
each Application renders against what its project permits. It also treats
`CreateNamespace=true` as the cluster-scoped write it is.

Because the projects are reconciled, adding a kind is a merge — no re-apply.

The list of kinds it knows about is curated, not discovered — rendering cannot
tell scope apart, because Helm and Kustomize routinely omit
`metadata.namespace` on namespaced resources too. A genuinely new cluster-scoped
kind needs adding to `CLUSTER_SCOPED_KINDS` as well as to the project.

## Adding a chart repository

A new upstream chart repository must be added to the relevant project's
`sourceRepos` before an Application referencing it will sync. This is
intentional: it keeps the set of upstream sources reviewable in one place.
