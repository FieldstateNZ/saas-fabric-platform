# Bootstrap

The smallest set of resources that turns a Kubernetes cluster into a SaaS Fabric
platform. Three files, one command.

| File | What it is |
|---|---|
| [`project.yaml`](project.yaml) | the `saas-fabric-platform` `AppProject`, which bounds what the platform may read and write |
| [`root-application.yaml`](root-application.yaml) | the root Application — the single resource that hands the cluster to Argo CD |
| [`components/environment-config`](components/environment-config/kustomization.yaml) | points the root Application at one environment |

The environment's bootstrap kustomization adds two more: that environment's
ConfigMap, and [`argocd/runtime`](../argocd/runtime/) — the Argo CD behaviour
the platform depends on, which must be active before the first wave-ordered
sync.

## Applying it

```bash
kubectl apply --server-side --field-manager=saas-fabric-platform \
  -k environments/lucentroot/bootstrap
```

or, for production:

```bash
kubectl apply --server-side --field-manager=saas-fabric-platform \
  -k environments/production/bootstrap
```

`--server-side` is required. The bootstrap set includes a partial `argocd-cm`
that adds one key to a ConfigMap Argo CD's installer owns; server-side apply
co-owns that key and leaves the rest alone.

The full prerequisites — Argo CD, repository access, and the external secrets
that cannot originate from OpenBao yet — are in
[docs/bootstrap.md](../docs/bootstrap.md).

## Why the command lives under `environments/`

The root Application differs between environments in two fields: the path it
watches, and the Git ref it follows. The path comes from
`environments/<environment>/config/platform.yaml`, the one place an environment
describes itself. The ref is set alongside it in that environment's `bootstrap/`
kustomization, next to the ref its child Applications follow — it is Argo
binding rather than an environmental fact, so it is deliberately not in the
ConfigMap.

Each environment therefore has a small `bootstrap/` kustomization that combines
this directory, `argocd/runtime` and its own configuration.
`kubectl apply -k environments/production/bootstrap` reads as what it does:
bootstrap this cluster as production.

Building this directory on its own is valid and yields the LucentRoot defaults.

## After this

Nothing else is applied by hand — including releases. Production follows
`refs/heads/production`, and promotion advances that branch rather than
re-applying anything. `selfHeal: true` means an imperative change to a platform
resource is reverted, which is the system working. See
[docs/releases.md](../docs/releases.md).

## What is deliberately not here

- **Argo CD itself.** Installed before this runs, by `saas-fabric-hosting` or by
  an operator. A platform cannot install the thing that installs it. The Argo CD
  *behaviour* the platform depends on is owned here — see
  [`argocd/runtime/README.md`](../argocd/runtime/README.md).
- **Secrets.** `keycloak-admin` and the `platform-tls` certificate are created
  out of band and referenced by name.
- **The catalogue project.** `saas-fabric-catalogue` is strictly narrower than
  the project that creates it, so Argo CD reconciles it from Git. The platform
  project is not self-managed — see
  [`argocd/projects/README.md`](../argocd/projects/README.md).
