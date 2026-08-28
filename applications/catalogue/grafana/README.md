# Grafana

| | |
|---|---|
| Product | Grafana |
| Upstream project | https://github.com/grafana/grafana |
| Helm chart source | https://grafana.github.io/helm-charts |
| Chart version (pinned) | `10.5.15` |
| Application version | `12.3.1` |
| Licence | AGPL-3.0 |
| Namespace | `catalogue` |
| Grouping | `catalogue` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `40` |
| Plane | **operator** |

## A shared platform service that happens to be optional

Grafana is the case that retired this repository's old `core`/`catalogue`
classification, so it is worth being precise about what it is.

Three facts, all true at once, which one binary could not express:

| | |
|---|---|
| SaaS Fabric requires it | **no** — Fabric emits to the collector and is unaware of what reads it |
| Platform operators use it | **yes** — this is where operational visibility lives |
| It could serve clients | **plausibly** — organisations are Grafana's own separation unit |

The second is why calling it "catalogue, therefore peripheral" was wrong. It is
a platform-management capability that operators depend on daily. The directory
it sits in is a [deployment tier](../README.md) — one namespace, no
cluster-scoped resources — not a statement about its importance.

The first still matters, and the reason is unchanged: the platform's
observability contract is OTLP, not a dashboard. The collector is required
because everything reports through it; Grafana is one way to look at what it
collects. If Grafana were required, "observability" here would quietly come to
mean "Grafana". See [`../../core/observability`](../../core/observability/).

Its declared contract is in [`platform-service.yaml`](platform-service.yaml).

## Tenancy: intended, not built

Grafana's organisation model is the same shape as Keycloak's realms — one
runtime, a platform administrative context, one partition per client:

```text
Grafana runtime                platform
  ├── platform organisation    platform
  ├── Acme organisation        client   ← intended
  └── Contoso organisation     client   ← intended
```

**Nothing client-scoped is implemented here, and the contract claims none.**
`tenancy.status` is `candidate`, which is deliberate: an organisation is not
proven to be an isolation boundary until datasource scoping and administrator
escape paths between organisations have been assessed. See
[the checklist](../../../docs/platform-services.md#assessing-tenancy).

The intended ownership split, when it is built:

| | Owner |
|---|---|
| Deployment and runtime | this repository, via Argo CD |
| Platform organisation | this repository |
| Client organisation | `saas-fabric-clients` |
| Client datasource definitions | client provisioning |
| Client datasource credentials | `secret/clients/<client>/*` |
| Platform datasource credentials | `secret/platform/*` |

A client organisation must not reach platform observability data by default.
That is a requirement of the design, not a later hardening step.

## Enabling it

Enabled per environment by including `applications/catalogue` in that
environment's kustomization. LucentRoot deploys it; production does not yet —
which does not make it a different kind of thing in each. See
[environments/README.md](../../../environments/README.md).

## Admin credential

```yaml
secretRef:
  name: grafana-admin   # namespace: catalogue
  keys: [username, password]
```

**Generated in-cluster, not injected.** The chart's own password handling is
disabled in favour of an External Secrets `Password` generator — see
[`../grafana-credentials`](../grafana-credentials/).

```bash
kubectl -n catalogue get secret grafana-admin \
  -o jsonpath='{.data.password}' | base64 -d
```

## Exposure

Operator plane only. Grafana reads platform telemetry and is an operations
surface, not a product one, so it has no `HTTPRoute` and never appears on the
product edge — see
[docs/architecture.md](../../../docs/architecture.md#exposure-planes). That also
means it is reachable only in an environment that runs an operator plane;
LucentRoot does, production does not yet.

## Dependencies

None hard. In practice it is only useful once
[`observability`](../../core/observability/) exports to a queryable backend,
which is why its data sources are configured per environment rather than here.

## Configuration owned by this repository

- deployment, persistence, service and RBAC scope;
- admin credential reference;
- per-environment tailnet hostname, storage class and data sources.

## Configuration expected from outside this repository

- **Data source endpoints and credentials** for the environment's telemetry
  backend.
- **Dashboards and per-client views.** Anything client-shaped — a dashboard
  scoped to one client's namespace, for example — belongs to the client layer.
