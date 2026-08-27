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
| Class | **catalogue** |
| Sync wave | `40` |
| Plane | **operator** |

## Why it is catalogue, not core

Apply the test: *does SaaS Fabric itself require this service in order to
operate?* No. SaaS Fabric emits telemetry to the OpenTelemetry collector and is
entirely unaware of what reads it. Grafana is a way to look at platform data, not
a thing the platform depends on.

This distinction is the reason the collector is core and Grafana is not — see
[`../../core/observability`](../../core/observability/). If Grafana were core,
"observability" in this platform would quietly come to mean "Grafana".

## Enabling it

Catalogue applications are enabled per environment by including
`applications/catalogue` in that environment's kustomization. LucentRoot enables
the catalogue; production does not yet. See
[docs/adding-an-application.md](../../../docs/adding-an-application.md).

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
