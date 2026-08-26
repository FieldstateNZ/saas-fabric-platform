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

## The contract for a workload

Put the values at `secret/<name>` in OpenBao. Declare an `ExternalSecret`
referencing the `openbao` store, and the operator materialises a Kubernetes
Secret the Deployment reads with `envFrom`:

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
        key: my-app
```

`dataFrom.extract` takes every key at the path, so **adding a variable means
writing it to OpenBao and changing nothing in Git**. That is the property worth
protecting: the repository describes where secrets come from, never what they
are.

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
bao policy write external-secrets - <<'POLICY'
path "secret/data/*"   { capabilities = ["read"] }
path "secret/metadata/*" { capabilities = ["read", "list"] }
POLICY
bao write auth/kubernetes/role/external-secrets \
  bound_service_account_names=external-secrets \
  bound_service_account_namespaces=secrets \
  policies=external-secrets ttl=1h
```

The role grants read across the whole `secret/` mount, so a new workload needs
no OpenBao policy change. That is a deliberate trade: one broad read grant for
the operator, in exchange for adding a secret never touching this repository.
Narrowing it per workload is possible and would mean a policy change per
registration.

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
