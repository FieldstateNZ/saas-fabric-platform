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

Both settings share a property that makes them worth owning explicitly: when
missing they fail *quietly*. Nothing errors. Sync waves simply stop ordering
anything, and operator-plane access to Argo CD simply loops.

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

### Server TLS termination

[`server-insecure.yaml`](server-insecure.yaml) sets `server.insecure: "true"` in
`argocd-cmd-params-cm`.

The operator plane terminates TLS at the Tailscale proxy and forwards plain HTTP
to `argocd-server`. By default `argocd-server` serves HTTPS and redirects port 80
to it, so that arrangement produces a redirect loop. Argo CD's guidance for a
TLS-terminating ingress is to run the server with TLS disabled.

This is why the setting belongs to the platform rather than to hosting: it is
required by *how the platform routes to Argo CD*, and hosting has no way to know
that. The ownership split is exactly:

```text
hosting     installs Argo CD
platform    owns the Argo CD behaviour its own design depends on
              - Application health, for wave ordering
              - server.insecure, for operator-plane routing
```

Two things to know:

- **It is a different ConfigMap.** Server flags live in `argocd-cmd-params-cm`,
  not `argocd-cm`, and the keys are literal dotted strings rather than a nested
  map. Nesting them renders a `server: {insecure: true}` entry Argo CD never
  reads, and the real setting is silently never applied.
- **It needs a restart.** Command-line parameters are read at startup, not
  watched. After the first bootstrap, or any change to this file:

  ```bash
  kubectl -n argocd rollout restart deployment/argocd-server
  ```

Without it, `argocd-server` is only reachable by port-forward and the
[operator-plane Ingress](../../applications/core/operator-access/) loops.

## How it is applied

Each manifest is a **partial** ConfigMap: it declares one key and nothing else.
They are applied twice, deliberately:

1. **At bootstrap**, by `kubectl apply --server-side`, so the behaviour is
   active before the root Application exists and before the first wave-ordered
   sync happens.
2. **By Argo CD thereafter**, as part of the environment, so it cannot drift.

Server-side apply matters here. It makes this repository the field manager for
these two keys and leaves every other key to whoever installed Argo CD. A
client-side apply would rewrite each object's `last-applied-configuration` and
put the two managers in conflict.

`scripts/check.py` fails the build if either setting is missing from the
bootstrap set or the reconciled environment.

## Argo CD version requirement

| Requirement | Why |
|---|---|
| Argo CD **2.13 or later** | `oci://` Helm source repositories, used by [Envoy Gateway](../../applications/core/envoy-gateway/), whose chart is published only to an OCI registry |
| Server-side apply support | how this ConfigMap and the platform's CRD-bearing charts are applied |

## What is deliberately not here

Argo CD's installation, its RBAC, its SSO and its own upgrade lifecycle. Those
belong to `saas-fabric-hosting`. Only behaviour the platform depends on is owned
here.

Argo CD's *ingress* is the near miss. It is not installed here, but how it is
reached is a platform decision, so the operator-plane Ingress lives in
[`applications/core/operator-access`](../../applications/core/operator-access/)
and the server setting it requires lives here.
