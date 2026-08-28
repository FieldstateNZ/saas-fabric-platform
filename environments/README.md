# Environments

Environment configuration is deliberately thin. Application definitions are
shared; an environment describes only what is genuinely different about it.

```text
environments/
├── components/environment-config/   binds shared Applications to one environment
├── lucentroot/
│   ├── kustomization.yaml           which applications this environment runs
│   ├── bootstrap/                   kubectl apply -k environments/lucentroot/bootstrap
│   └── config/
│       ├── platform.yaml            the environment contract
│       └── <application>.yaml       Helm values overrides, only where they differ
└── production/
    └── ... the same shape
```

## The environment contract

`config/platform.yaml` is the one place an environment describes itself:

```yaml
environment: lucentroot          production
domain: lucentroot.internal      platform.fieldstate.nz
replicaProfile: development      production
storageClass: local-path         managed-csi
```

Environmental facts only. It is a real ConfigMap, applied to the cluster, so a
cluster can always be asked what it is. It is also the source for the Kustomize
replacement that points every Application at this environment's configuration —
see [`components/environment-config`](components/environment-config/kustomization.yaml).

`environment` is consumed mechanically. `domain`, `replicaProfile` and
`storageClass` are the declared contract that the per-application files below
must agree with.

## The Git ref is not environment configuration

Which ref Argo CD follows is deliberately **not** here. It is Argo binding —
promotion state, not a fact about the environment — and putting it in a runtime
ConfigMap would conflate "what this environment is" with "what it is currently
running".

It appears in exactly two files per environment:

```text
<environment>/kustomization.yaml            the ref child Applications follow
<environment>/bootstrap/kustomization.yaml  the ref the root Application follows
```

| Environment | Follows |
|---|---|
| LucentRoot | `main` |
| Production | `production`, a branch only ever fast-forwarded to a release tag |

See [docs/releases.md](../docs/releases.md).

## Per-application overrides

`config/<application>.yaml` is a Helm values file, read directly from Git by
Argo CD as a second values source on top of the application's shared
`values.yaml`. It should contain only real environmental differences: replica
counts, storage classes, hostnames, resource sizing.

An environment with nothing to say about an application has no file for it —
Argo CD is configured with `ignoreMissingValueFiles`, so the shared values apply
unchanged. LucentRoot has no `saas-fabric.yaml` for that reason; production has
no `grafana.yaml` because it does not deploy Grafana yet.

## What must not go here

Do not copy an application's full configuration into each environment and change
a few lines. This shape is what this repository is structured to avoid:

```text
environments/
  lucentroot/
    keycloak/
    grafana/
    openbao/
  production/
    keycloak/
    grafana/
    openbao/
```

An application needing genuinely *structural* differences per environment — not
just different values — is the exception, and it is expressed as a Kustomize
overlay inside the application's own directory, where the rest of its definition
lives. See
[`applications/core/saas-fabric/overlays`](../applications/core/saas-fabric/overlays/).

## Which applications an environment runs

`kustomization.yaml` lists what this environment reconciles, and sets the Git
ref above. Core and [`argocd/runtime`](../argocd/runtime/) are always included.
The `catalogue` tier is one line, and omitting it yields a complete platform:

| Environment | `applications/core` | operator plane | `applications/catalogue` |
|---|---|---|---|
| LucentRoot | yes | yes | yes |
| Production | yes | no — no tailnet yet | no |

**Enablement is independent of what a service is.** Grafana is the same platform
service in both environments; LucentRoot deploys it and production does not yet.
A service does not change architectural type by being switched on somewhere.

### LucentRoot dogfoods the platform

Its purpose is not only to prove that mandatory dependencies start. It exists to
exercise the services SaaS Fabric expects to manage, which is why it enables
things production does not:

```text
LucentRoot
  Envoy Gateway      CloudNativePG      OpenBao
  External Secrets   Keycloak           OpenTelemetry
  Grafana            SaaS Fabric
  OpenFGA    [when adopted]
  Superset   [when adopted]
  Airflow    [when adopted]
```

The bracketed three are not enabled to satisfy that list. Each still has to
solve its deployment contract first — see
[docs/platform-services.md](../docs/platform-services.md#the-register).

The operator plane is `applications/core/tailscale` and
`applications/core/operator-access`. Both are core, but an environment without a
tailnet cannot run them, so they are listed by each environment rather than by
`applications/core/kustomization.yaml`. See
[docs/architecture.md](../docs/architecture.md#exposure-planes).

## Adding an environment

The structure supports a third environment without reorganisation. See
[docs/adding-an-application.md](../docs/adding-an-application.md#adding-an-environment).
