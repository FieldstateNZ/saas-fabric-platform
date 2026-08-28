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
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `20` |

## Why it exists in SaaS Fabric

[OpenBao](../openbao/) is the secrets authority and
[External Secrets](../external-secrets/) is the delivery mechanism. This is the
one object that connects them, and it is a separate Application so the
dependency is expressed rather than assumed: both halves are wave `10`, the
store is wave `20`, and Argo CD will not create it until they are Healthy.

One store for the whole platform — and **only** the platform. It is bounded on
both sides, and both bounds exist because of where SaaS Fabric is going rather
than where LucentRoot is today.

## The bounds

| Bound | Effect |
|---|---|
| `conditions.namespaceSelector` | only namespaces labelled `fieldstate.nz/layer: platform` may reference this store |
| the OpenBao policy behind it | the token can read `secret/platform/*` and nothing else |

Without the first, anyone who can create an `ExternalSecret` in any namespace —
including a future `client-acme` — could ask External Secrets to fetch anything
the operator's token can read. Without the second, that would be every secret in
the mount. Together they mean a client namespace can neither reference this
store nor reach client secret paths through it.

The label is applied by each Application's `managedNamespaceMetadata`, and
platform-owned labels are deliberately never applied to client-owned resources.
**That makes the label part of a security boundary**, not just inventory
metadata. Do not apply it to a namespace this repository does not own.

### Client secrets do not come through here

A client gets its own `SecretStore`, in its own namespace, bound to a
client-scoped OpenBao role over `secret/clients/<client>/*` — all created by
client provisioning alongside its realm, database and routes. Widening this
store to serve clients would collapse the tenancy boundary the rest of the
platform is built around.

The namespace bound makes this look like a location rule. It is not: **the split
is about what a secret is for, not about which namespace asks for it.** A
platform workload can legitimately need both — its own operational credentials
from `secret/platform/...`, and a particular client's credentials from that
client's store. Running in a platform namespace does not make everything it
reads a platform secret. See
[`../external-secrets`](../external-secrets/#the-split-is-about-purpose-not-about-which-namespace-asks).

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
- the auth method, role and service account the operator authenticates as;
- which namespaces may use the store.

## Configuration expected from outside this repository

- **the matching OpenBao role and policy**, created once at bootstrap — see
  [`../external-secrets`](../external-secrets/);
- **client `SecretStore` resources, roles and policies**, owned by client
  provisioning;
- **an environment with a different secrets backend** would replace this store
  in its overlay. None does today; the overlays exist so one can.
