# Argo CD runtime configuration

Argo CD is installed by `saas-fabric-hosting`. The *behaviour* this platform
depends on is owned here, because it is part of the platform contract rather
than an installation detail — and because an undocumented dependency on how Argo
CD is configured is exactly the kind of invisible bootstrap requirement that
makes a platform unreproducible.

```text
saas-fabric-hosting     installs Argo CD
saas-fabric-platform    owns the Argo CD behaviour the platform requires
```

## What is configured, and why

### Application health assessment

[`application-health.yaml`](application-health.yaml) adds a custom health
assessment for `argoproj.io/Application`.

**This is not Argo CD's default behaviour, and the platform does not work
correctly without it.** By default a child `Application` resource is reported
Healthy as soon as it exists, whatever state the application it names is
actually in. Under that default, sync waves in an app-of-apps sequence nothing:
every wave proceeds immediately.

The platform has a real dependency chain:

```text
Envoy Gateway, CloudNativePG operator   (wave 0)
        ↓  CRDs must exist
platform Gateway, Keycloak database     (wave 10)
        ↓  the database must be accepting connections
Keycloak                                (wave 20)
        ↓
SaaS Fabric                             (wave 30)
```

With this assessment in place, wave `10` does not begin until wave `0` is
genuinely running. Without it, Keycloak starts against a database that does not
exist and the platform converges only by repeated retry — which usually works,
and is not a design.

## How it is applied

The manifest is a **partial** `argocd-cm`: it declares one key and nothing else.
It is applied twice, deliberately:

1. **At bootstrap**, by `kubectl apply --server-side`, so the behaviour is
   active before the root Application exists and before the first wave-ordered
   sync happens.
2. **By Argo CD thereafter**, as part of the environment, so it cannot drift.

Server-side apply matters here. It makes this repository the field manager for
this one key of `argocd-cm` and leaves every other key to whoever installed Argo
CD. A client-side apply would rewrite the object's
`last-applied-configuration` and put the two managers in conflict.

`scripts/check.py` fails the build if this configuration is missing from either
the bootstrap set or the reconciled environment.

## Argo CD version requirement

| Requirement | Why |
|---|---|
| Argo CD **2.13 or later** | `oci://` Helm source repositories, used by [Envoy Gateway](../../applications/core/envoy-gateway/), whose chart is published only to an OCI registry |
| Server-side apply support | how this ConfigMap and the platform's CRD-bearing charts are applied |

## What is deliberately not here

Argo CD's installation, its ingress, its RBAC, its SSO and its own upgrade
lifecycle. Those belong to `saas-fabric-hosting`. Only behaviour the platform
depends on to converge correctly is owned in this repository.
