# Keycloak

| | |
|---|---|
| Product | Keycloak |
| Upstream project | https://github.com/keycloak/keycloak |
| Helm chart source | https://codecentric.github.io/helm-charts (`keycloakx`) |
| Chart version (pinned) | `7.3.0` |
| Application version | `26.7.2` |
| Licence | Apache-2.0 |
| Namespace | `identity` |
| Class | core |
| Sync wave | `20` |

## Why it exists in SaaS Fabric

SaaS Fabric issues and manages identity per client. It needs one shared identity
provider that exists before any client does, so the identity provider itself is
platform infrastructure.

## Ownership boundary

This is the sharpest boundary in the platform. Argo CD owns the *Keycloak
deployment*. Everything inside Keycloak that is client-shaped is owned
elsewhere.

| Resource | Owner |
|---|---|
| Deployment, service, routes on both planes, probes, persistence | this repository |
| Database connection interface | this repository |
| Admin bootstrap credential *reference* | this repository (the value is external) |
| Realm for a client | client OpenTofu |
| Clients, roles, groups, identity providers within a client realm | client OpenTofu |
| Client hostname and `HTTPRoute` such as `acme.fieldstate.nz` | client OpenTofu |

No realm is defined in this repository, including a "default" one. A realm here
would compete with the client layer for ownership of the same object.

## Why `keycloakx` rather than the Bitnami chart

`keycloakx` deploys the official `quay.io/keycloak/keycloak` distribution and
tracks Keycloak's own release cadence. It avoids a dependency on the Bitnami
image catalogue, whose distribution terms changed in 2025.

## Database

Provided by [`../keycloak-database`](../keycloak-database/) — a CloudNativePG
`Cluster` in the same namespace, at wave `10`. Keycloak reads the generated
credential by reference:

```yaml
database:
  existingSecret: keycloak-db-app
  existingSecretKey: password
```

## Required external secret

One secret must exist before Keycloak first starts. It is **not** created by
this repository:

```yaml
secretRef:
  name: keycloak-admin   # namespace: identity
  keys: [username, password]
```

This is the temporary bootstrap administrator. It exists only until OpenBao can
issue platform credentials, at which point this reference is replaced rather
than the value being moved. Creating it is a documented bootstrap step — see
[docs/bootstrap.md](../../../docs/bootstrap.md).

## Exposure: both planes, deliberately

Keycloak is the clearest case in the platform of a service that belongs on both
planes, and the split is not "Keycloak is internal".

| Plane | Carries | Paths |
|---|---|---|
| Product (Envoy) | what applications call | `/realms`, `/resources`, `/.well-known` |
| Operator (Tailscale) | administration | everything, including `/admin` |

Applications genuinely need the authentication endpoints on the product edge.
The admin console and admin API do not belong there, so the product-plane route
lists its path matches explicitly rather than taking the chart's default of a
single `/` prefix — which would put `/admin` back on the public edge.

`scripts/check.py` rejects a `/` or `/admin` match on the product plane for this
service. Production currently has no operator plane, so its admin console is
reachable by `kubectl port-forward` and nothing else. See
[docs/architecture.md](../../../docs/architecture.md#exposure-planes).

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [Envoy Gateway](../envoy-gateway/) | `0` | the routing layer, and the Gateway API CRDs its `HTTPRoute` needs |
| [CloudNativePG](../cloudnative-pg/) | `0` | provides the `Cluster` CRD |
| [Platform gateway](../platform-gateway/) | `10` | the `Gateway` its route attaches to |
| [Keycloak database](../keycloak-database/) | `10` | Keycloak will not start without it |

## Configuration owned by this repository

- deployment topology, replica count and resources per environment;
- service and both routes: the product-plane `HTTPRoute` for OIDC, and the
  operator-plane `Ingress` for administration;
- health and readiness probing, and the `/health` and `/metrics` endpoints;
- proxy mode and forwarded-header handling;
- the database connection interface and the admin secret reference.

## Configuration expected from outside this repository

- **`keycloak-admin` secret**, injected at bootstrap.
- **TLS**, terminated at the platform `Gateway` rather than here.
- **DNS** for `auth.<domain>`, from `saas-fabric-hosting`.
- **All realm content**, from the client layer.
