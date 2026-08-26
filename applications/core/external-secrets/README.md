# External Secrets

| | |
|---|---|
| Product | External Secrets Operator |
| Upstream project | https://github.com/external-secrets/external-secrets |
| Helm chart source | https://charts.external-secrets.io |
| Chart version (pinned) | `2.9.0` |
| Application version | `v2.9.0` |
| Licence | Apache-2.0 |
| Namespace | `secrets` |
| Class | core |
| Sync wave | `10` |

## Why it exists in SaaS Fabric

OpenBao is the platform's secrets *authority*. This is the mechanism that gets a
secret from OpenBao into a pod. Without it every credential has to be created by
hand with `kubectl`, which is how a platform ends up with secrets nobody can
account for.

It is core because SaaS Fabric's own credentials, and every client credential
that follows, need a delivery path that is not a person running a command.

## The contract for a platform workload

Put the values at `secret/platform/<name>` in OpenBao. Declare an
`ExternalSecret` referencing the `openbao` store, and the operator materialises
a Kubernetes Secret the Deployment reads with `envFrom`:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: my-app-env
spec:
  secretStoreRef:
    name: openbao
    kind: ClusterSecretStore
  target:
    name: my-app-env
    creationPolicy: Owner
  dataFrom:
    - extract:
        key: platform/my-app
```

`dataFrom.extract` takes every key at the path, so **adding a variable means
writing it to OpenBao and changing nothing in Git**. That is the property worth
protecting: the repository describes where secrets come from, never what they
are.

## This store is bounded, deliberately

A cluster-wide store authenticated as one service account with read across the
whole mount would make the security boundary "anyone who can create an
`ExternalSecret` can read anything". That is tolerable on a single-tenant box
and completely wrong for the client model SaaS Fabric is being built for, so it
is not the contract established here.

Two bounds, and both matter:

| Bound | Effect |
|---|---|
| `conditions` on the [`ClusterSecretStore`](../secret-store/) | only namespaces labelled `fieldstate.nz/layer: platform` may reference it |
| the OpenBao policy | the operator's token can read `secret/platform/*` and nothing else |

The namespace label is applied by each Application's
`managedNamespaceMetadata`, and platform-owned labels are deliberately never
applied to client-owned resources. A `client-acme` namespace therefore cannot
reference this store, and even if it could, the token behind it cannot read
`secret/clients/...`.

**That label is now load-bearing.** It was descriptive when it was only used for
inventory; it is part of a security boundary now. Do not apply it to a namespace
this repository does not own.

### Client secrets are a separate mechanism

Not this store with a wider policy. A client gets its own `SecretStore` in its
own namespace, bound to a client-scoped OpenBao role over its own path:

```text
client-acme namespace
        ↓
SecretStore in client-acme
        ↓
OpenBao role over secret/clients/acme/*
```

All three are created by client provisioning, alongside the client's realm,
database and routes. `secret/clients/` is reserved for exactly that and is
unreadable from the platform store.

### The split is about purpose, not about which namespace asks

This is the part that is easy to get wrong, because the namespace bound above
makes it look like a location rule. It is not. **One workload can legitimately
need secrets from both scopes**, and running in a platform namespace does not
make everything it reads a platform secret.

Superset is the clearest example, if it is ever adopted:

| Secret | Scope | Path |
|---|---|---|
| Superset's admin credential | platform | `secret/platform/superset/...` |
| Its metadata database connection | platform | `secret/platform/superset/...` |
| Its signing / secret key | platform | `secret/platform/superset/...` |
| Its OAuth client secret | platform | `secret/platform/superset/...` |
| Credentials for Superset to read **Acme's** data | **client** | `secret/clients/acme/...` |

Everything Superset needs *to be Superset* is platform. Everything it needs *to
reach one client's resources* is that client's, and comes through that client's
store — not this one, and not by widening this one.

Ask which one a secret is:

```text
does the platform need this to run the component?      → secret/platform/...
does it only exist because a particular client does?   → secret/clients/<client>/...
```

The runtime bound already enforces the answer — the platform token cannot read
`secret/clients/*` — but the design decision has to be made before that, when
someone chooses where to write the value.

## Authentication

The operator authenticates to OpenBao with the Kubernetes auth method, minting
its own service account token and exchanging it for an OpenBao token. There is
no static credential anywhere — nothing to leak, nothing to rotate.

The `secrets` namespace holds both halves, so this traffic never leaves the
node.

## Required one-time OpenBao configuration

The auth method has to exist before the store can work. It is an operator step
because it happens once, against a freshly initialised OpenBao:

```bash
bao auth enable kubernetes
bao write auth/kubernetes/config \
  kubernetes_host=https://kubernetes.default.svc
bao policy write platform-secrets - <<'POLICY'
path "secret/data/platform/*"     { capabilities = ["read"] }
path "secret/metadata/platform/*" { capabilities = ["read", "list"] }
POLICY
bao write auth/kubernetes/role/external-secrets \
  bound_service_account_names=external-secrets \
  bound_service_account_namespaces=secrets \
  policies=platform-secrets ttl=1h
```

The policy grants read on the **platform prefix only**. A new platform workload
still needs no OpenBao policy change — its secret goes under
`secret/platform/<name>` and the existing grant covers it — while
`secret/clients/` stays outside what this token can reach at all.

That is the trade worth making: convenience within the platform's own space, and
a hard wall at the tenancy boundary.

Full procedure in [docs/bootstrap.md](../../../docs/bootstrap.md).

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [OpenBao](../openbao/) | `10` | the secrets authority it reads from |
| [Secret store](../secret-store/) | `20` | the `ClusterSecretStore` joining the two |

External Secrets and OpenBao are both wave `10` on purpose: the operator does
not need OpenBao at startup, only when it reconciles an `ExternalSecret`.

## What this does not solve

It cannot deliver the credentials the platform needs *before* OpenBao is
running. That short list stays externally injected — see
[the bootstrap secret boundary](../../../docs/architecture.md#the-bootstrap-secret-boundary).

## Configuration owned by this repository

- the operator, its CRDs, webhook and cert controller;
- the service account the OpenBao role is bound to;
- resource sizing per environment.

## Configuration expected from outside this repository

- **the OpenBao auth method, policy and role**, created once at bootstrap;
- **the secret values themselves**, which live in OpenBao and never here;
- **client secret paths and policies**, owned by the client layer.
