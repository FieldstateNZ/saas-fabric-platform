# Bootstrap

The smallest set of resources that turns a Kubernetes cluster into a SaaS Fabric
platform. Three files, one command.

| File | What it is |
|---|---|
| [`project.yaml`](project.yaml) | the `saas-fabric-platform` `AppProject`, which bounds what the platform may read and write |
| [`root-application.yaml`](root-application.yaml) | the root Application — the single resource that hands the cluster to Argo CD |
| [`components/environment-config`](components/environment-config/kustomization.yaml) | binds the root Application to one environment |

## Applying it

```bash
kubectl apply -k environments/lucentroot/bootstrap
```

or, for production, from the release tag being promoted:

```bash
kubectl apply -k environments/production/bootstrap
```

The full prerequisites — Argo CD, repository access, and the external secrets
that cannot originate from OpenBao yet — are in
[docs/bootstrap.md](../docs/bootstrap.md).

## Why the command lives under `environments/`

The root Application differs between environments in exactly two fields: the
path it watches, and the revision it tracks. Both are already declared in
`environments/<environment>/config/platform.yaml`, which is the one place an
environment describes itself.

Rather than restate them, each environment has a small `bootstrap/`
kustomization that combines this directory with its own ConfigMap and copies the
two values across. `kubectl apply -k environments/production/bootstrap` reads as
what it does: bootstrap this cluster as production.

Building this directory on its own is valid and yields the LucentRoot defaults.

## After this

Nothing else is applied by hand. `selfHeal: true` means an imperative change to
a platform resource is reverted, which is the system working. Platform changes
go through Git.

## What is deliberately not here

- **Argo CD itself.** Installed before this runs, by `saas-fabric-hosting` or by
  an operator. A platform cannot install the thing that installs it.
- **Secrets.** `keycloak-admin` and any TLS secrets are created out of band and
  referenced by name.
- **The catalogue project.** `saas-fabric-catalogue` is strictly narrower than
  the project that creates it, so Argo CD reconciles it from Git. The platform
  project is not self-managed — see
  [`argocd/projects/README.md`](../argocd/projects/README.md).
