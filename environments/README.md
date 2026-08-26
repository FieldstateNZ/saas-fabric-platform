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
revision: main                   v0.1.0
```

It is a real ConfigMap, applied to the cluster, so a cluster can always be asked
which environment it is and which revision it is meant to be running. It is also
the source for the Kustomize replacements that bind every Application to this
environment — see
[`components/environment-config`](components/environment-config/kustomization.yaml).

`environment` and `revision` are consumed mechanically. `domain`,
`replicaProfile` and `storageClass` are the declared contract that the
per-application files below must agree with.

## Per-application overrides

`config/<application>.yaml` is a Helm values file, read directly from Git by
Argo CD as a second values source on top of the application's shared
`values.yaml`. It should contain only real environmental differences: replica
counts, storage classes, hostnames, resource sizing.

An environment with nothing to say about an application has no file for it —
Argo CD is configured with `ignoreMissingValueFiles`, so the shared values apply
unchanged. LucentRoot has no `saas-fabric.yaml` for that reason; production has
no `grafana.yaml` because it does not run the catalogue.

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

`kustomization.yaml` lists what this environment reconciles. Core is always
included. The catalogue is one line, and omitting it yields a complete platform:

| Environment | `../../applications/core` | `../../applications/catalogue` |
|---|---|---|
| LucentRoot | yes | yes |
| Production | yes | no |

## Adding an environment

The structure supports a third environment without reorganisation. See
[docs/adding-an-application.md](../docs/adding-an-application.md#adding-an-environment).
