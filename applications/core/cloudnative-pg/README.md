# CloudNativePG operator

| | |
|---|---|
| Product | CloudNativePG |
| Upstream project | https://github.com/cloudnative-pg/cloudnative-pg |
| Helm chart source | https://cloudnative-pg.github.io/charts |
| Chart version (pinned) | `0.29.0` |
| Application version | `1.30.0` |
| Licence | Apache-2.0 |
| Namespace | `data-system` |
| Class | core |
| Sync wave | `0` |

## Why it exists in SaaS Fabric

PostgreSQL is the platform's system of record and the substrate for client
databases. The *operator* is shared infrastructure — it must exist before any
`Cluster` resource, platform or client, can be reconciled — so it belongs to the
platform layer and to Argo CD.

## Ownership boundary

Argo CD owns the operator, its CRDs and its RBAC. It does **not** own database
instances beyond the platform's own.

| Resource | Owner |
|---|---|
| CloudNativePG operator, CRDs, webhooks | this repository |
| `Cluster` for platform Keycloak | this repository, [`applications/core/keycloak-database`](../keycloak-database/) |
| `Cluster`, `Database`, roles for a client | client OpenTofu, in a `client-*` namespace |

The operator deliberately watches all namespaces, because client `Cluster`
resources are reconciled into namespaces this repository never sees.

## Dependencies

None. Wave `0`.

## Configuration owned by this repository

- operator deployment, CRDs, RBAC and webhook configuration;
- operator resource requests and replica count per environment.

## Configuration expected from outside this repository

- **Backup object storage.** Production `Cluster` resources are expected to
  reference a storage account or bucket provisioned by `saas-fabric-hosting`,
  and credentials injected as a Secret. No backup destination is configured
  here.
- **Client database topology.** Instance counts, storage sizing and roles for
  client databases are decided by the client layer.

## Notes

The CRDs are applied with `ServerSideApply=true`; they are too large for the
`kubectl.kubernetes.io/last-applied-configuration` annotation used by
client-side apply.
