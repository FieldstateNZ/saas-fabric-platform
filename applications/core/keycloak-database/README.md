# Keycloak database

| | |
|---|---|
| Product | PostgreSQL, via CloudNativePG `Cluster` |
| Upstream project | https://github.com/cloudnative-pg/cloudnative-pg |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | PostgreSQL image selected by the pinned CNPG operator |
| Licence | PostgreSQL Licence (server), Apache-2.0 (operator) |
| Namespace | `identity` |
| Class | core |
| Sync wave | `10` |

## Why it exists in SaaS Fabric

Keycloak is stateless only in the sense that its state lives in PostgreSQL. The
shared platform Keycloak therefore needs a shared platform database, and that
database is a platform resource: it is created once per cluster, holds no client
data of its own, and its lifecycle matches Keycloak's.

It is a separate Application from Keycloak so that the dependency is expressed
rather than assumed — the database is wave `10`, Keycloak is wave `20`, and Argo
CD will not begin Keycloak until this Application reports Healthy.

## This is not a client database

| Resource | Owner |
|---|---|
| `Cluster` `keycloak-db` in `identity` | this repository |
| `Cluster` for a client, in `client-acme` | client OpenTofu |
| Realms, users, client records inside Keycloak | client OpenTofu |

## Credential interface

CloudNativePG generates the `keycloak` role's password and publishes it as:

```yaml
secretRef:
  name: keycloak-db-app
```

with keys `username`, `password`, `dbname`, `host`, `port` and `uri`. Keycloak
consumes `password` from that secret by reference — see
[`../keycloak/values.yaml`](../keycloak/values.yaml). No password is stored in
Git, and none has to be injected by hand.

## Dependencies

The CloudNativePG operator and its CRDs
([`../cloudnative-pg`](../cloudnative-pg/), wave `0`).

## Configuration owned by this repository

- instance count, storage size and storage class per environment;
- database and owner role names;
- `max_connections` and resource requests.

## Configuration expected from outside this repository

- **Backup destination.** Production should add a `backup.barmanObjectStore`
  block pointing at storage provisioned by `saas-fabric-hosting`, with
  credentials injected as a Secret. This is a known gap — see
  [docs/architecture.md](../../../docs/architecture.md#known-gaps).

## Resources unsafe to prune

The `Cluster` carries `argocd.argoproj.io/sync-options: Prune=false,Delete=false`.
Removing this Application from Git will *not* delete the database, and a
cascading delete of the root Application will not reach it. Decommissioning is a
deliberate, manual act.
