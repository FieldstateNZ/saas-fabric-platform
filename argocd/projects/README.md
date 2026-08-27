# Argo CD projects

Every platform Application belongs to an `AppProject` that constrains which
repositories it may read and which namespaces and cluster-scoped kinds it may
write. The unrestricted `default` project is not used.

| Project | Defined in | Managed by | Scope |
|---|---|---|---|
| `saas-fabric-platform` | [`bootstrap/project.yaml`](../../bootstrap/project.yaml) | administrator, at bootstrap | core platform namespaces plus an enumerated list of cluster-scoped kinds |
| `saas-fabric-catalogue` | [`saas-fabric-catalogue.yaml`](saas-fabric-catalogue.yaml) | Argo CD, via the root Application | the `catalogue` namespace only, plus `Namespace` so it can create it |

## Why the platform project is not managed by Argo CD

`saas-fabric-platform` is the project the root Application runs in. If the root
Application also reconciled that project, a change to this repository could
widen the privileges of the thing applying the change. Argo CD's app-of-apps
model already confers substantial cluster privilege, so the project that bounds
it stays an administrator-applied resource, versioned here but applied by
`kubectl apply -k bootstrap/overlays/<environment>`.

`saas-fabric-catalogue` is strictly narrower than the project that creates it,
so it is safe to reconcile from Git.

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

The list of kinds it knows about is curated, not discovered — rendering cannot
tell scope apart, because Helm and Kustomize routinely omit
`metadata.namespace` on namespaced resources too. A genuinely new cluster-scoped
kind needs adding to `CLUSTER_SCOPED_KINDS` as well as to the project.

## Adding a chart repository

A new upstream chart repository must be added to the relevant project's
`sourceRepos` before an Application referencing it will sync. This is
intentional: it keeps the set of upstream sources reviewable in one place.
