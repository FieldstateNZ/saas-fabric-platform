# Observability

| | |
|---|---|
| Product | OpenTelemetry Collector (contrib distribution) |
| Upstream project | https://github.com/open-telemetry/opentelemetry-collector-contrib |
| Helm chart source | https://open-telemetry.github.io/opentelemetry-helm-charts |
| Chart version (pinned) | `0.171.0` |
| Application version | `0.158.0` |
| Licence | Apache-2.0 |
| Namespace | `observability` |
| Class | core |
| Sync wave | `10` |

## Why it exists in SaaS Fabric

The platform needs a *capability boundary* for telemetry, not a telemetry
product. Every platform component and, later, every client workload emits OTLP
to one well-known endpoint; what consumes that data is an environment decision
that can change without touching a single application definition.

This is why the collector is core and Grafana is catalogue. Grafana may read
platform telemetry, but the architecture must not be defined by it.

## The three signals

`metrics`, `logs` and `traces` each have a pipeline configured in
[`values.yaml`](values.yaml). All three currently terminate in the `debug`
exporter. That is the honest state of the platform: the ingress path,
attribution and pipeline shape are real; the storage backend is not chosen yet.

Wiring a backend is a per-environment change in
`environments/<env>/config/observability.yaml` — add an exporter and name it in
the pipelines. Nothing else in the platform moves.

## Endpoint contract

Platform workloads should send OTLP to:

```text
observability-collector.observability.svc.cluster.local:4317   # gRPC
observability-collector.observability.svc.cluster.local:4318   # HTTP
```

## Dependencies

None hard. Placed at wave `10` so it is available before SaaS Fabric starts
emitting.

## Configuration owned by this repository

- collector deployment, receivers, processors and pipeline shape;
- Kubernetes attribute enrichment and its RBAC;
- per-environment exporters and resource sizing.

## Configuration expected from outside this repository

- **Backend endpoints and credentials** (Azure Monitor, Loki/Tempo/Mimir, or a
  hosted vendor) are environment infrastructure. Endpoints belong in the
  environment config file; credentials are injected as a Secret and referenced
  by name.
- **Client-scoped telemetry routing** — per-client sampling, tenancy headers or
  export destinations — belongs to the client layer, not here.
