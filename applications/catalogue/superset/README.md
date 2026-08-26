# Superset — evaluated, not yet adopted

| | |
|---|---|
| Product | Apache Superset |
| Upstream project | https://github.com/apache/superset |
| Helm chart source | https://apache.github.io/superset |
| Chart version (evaluated) | `0.22.4` |
| Application version | `6.1.0` |
| Licence | Apache-2.0 |
| Intended namespace | `catalogue` |
| Class | catalogue |
| Status | **not deployed** — no `application.yaml` in this directory |

## Why it exists as a directory

Superset is a strong candidate for a SaaS Fabric analytics capability, and the
evaluation that has already been done is worth keeping. Adding an Application
here later should not require repeating it.

Nothing in the platform depends on Superset, and per
[docs/adding-an-application.md](../../../docs/adding-an-application.md) an
application is not added merely because it has been discussed.

## What blocks adoption

The upstream chart bundles Bitnami PostgreSQL and Redis subcharts for its
metadata database and cache. Two problems:

1. **Ownership.** A bundled PostgreSQL competes with CloudNativePG, which is
   already the platform's PostgreSQL owner. Two things reconciling databases in
   one cluster is exactly the competing-ownership situation this repository
   exists to avoid.
2. **Image distribution.** Bitnami's image catalogue moved to a subscription
   model during 2025, so the subchart's default images are not a safe
   production dependency.

## What adoption requires

- a CloudNativePG `Cluster` for Superset's metadata database, defined the same
  way as [`keycloak-database`](../../core/keycloak-database/), with
  `supersetNode.connections` pointing at it and `postgresql.enabled: false`;
- a decision on Redis: an in-cluster deployment the platform owns, or a managed
  cache from `saas-fabric-hosting`;
- OIDC configuration against the platform Keycloak, with the client record owned
  by the client layer rather than defined here;
- `https://apache.github.io/superset` added to the `saas-fabric-catalogue`
  project's `sourceRepos`.
