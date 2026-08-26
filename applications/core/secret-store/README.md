# Secret store

| | |
|---|---|
| Product | `ClusterSecretStore` joining External Secrets to OpenBao |
| Upstream project | https://github.com/external-secrets/external-secrets |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | CRD supplied by the pinned External Secrets chart |
| Licence | Apache-2.0 |
| Namespace | `secrets` |
| Class | core |
| Sync wave | `20` |

## Why it exists in SaaS Fabric

[OpenBao](../openbao/) is the secrets authority and
[External Secrets](../external-secrets/) is the delivery mechanism. This is the
one object that connects them, and it is a separate Application so the
dependency is expressed rather than assumed: both halves are wave `10`, the
store is wave `20`, and Argo CD will not create it until they are Healthy.

One store for the whole platform. A workload that needs a secret references it
by name and needs no OpenBao policy of its own.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [External Secrets](../external-secrets/) | `10` | supplies the `ClusterSecretStore` CRD |
| [OpenBao](../openbao/) | `10` | what the store points at |

## Expect it to be unhealthy on a fresh cluster

The store cannot authenticate until OpenBao has been initialised, unsealed and
had its Kubernetes auth method configured — all one-time operator steps against
a new OpenBao. Until then this Application retries.

That is the [bootstrap secret boundary](../../../docs/architecture.md#the-bootstrap-secret-boundary)
made visible: the platform converges to the point where a human has to supply
the first secret, and no further.

## Configuration owned by this repository

- the OpenBao address, mount path and KV version;
- the auth method, role and service account the operator authenticates as.

## Configuration expected from outside this repository

- **the matching OpenBao role and policy**, created once at bootstrap — see
  [`../external-secrets`](../external-secrets/);
- **an environment with a different secrets backend** would replace this store
  in its overlay. None does today; the overlays exist so one can.
