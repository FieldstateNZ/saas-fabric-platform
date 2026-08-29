# Perses provisioning

| | |
|---|---|
| Product | Perses provisioning, via Kubernetes ConfigMaps |
| Upstream project | https://github.com/perses/perses |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | provisioning loader supplied by the pinned Perses chart |
| Licence | Apache-2.0 |
| Namespace | `catalogue` |
| Grouping | `catalogue` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `40` |

## Why it exists in SaaS Fabric

Because a dashboard is platform configuration, and configuration lives in Git.

[Perses](../perses/) is deployed read-only, so nothing can be created through
its API or its UI. Everything it holds arrives here instead:

```text
this directory
    │  kustomize
    ▼
ConfigMap  (labelled perses.dev/resource=true)
    │  the chart's provisioning sidecar
    ▼
/etc/perses/provisioning
    │  Perses' provisioning loader, every 5 minutes
    ▼
Perses
```

Provisioning **re-injects** on that interval rather than loading once, so the
folder is authoritative and not merely initial. Combined with `readonly`, that
closes the loop this repository asks of everything else: what Argo CD reports as
synced is what the runtime holds.

A writable dashboard runtime has no equivalent of this, and that is the one real
change of shape the platform took on rather than an addition for its own sake.

## What is here

```text
base/
  project.yaml     the platform Perses project
overlays/
  lucentroot/      the datasources and dashboards this environment can support
```

One resource, and that is the honest state of the platform rather than an
unfinished directory. Every kind of Perses resource travels this same path —
`Project`, `GlobalDatasource`, `Datasource`, `Dashboard` — and today exactly one
of them is something this platform can support.

## Datasources do not touch the deployment

The property worth naming, because it is what makes the mechanism complete
independently of any telemetry backend: **a datasource is added here, not to
Perses' chart values.** Supplying one changes no part of the Perses deployment,
requires no restart, and is a file in an environment overlay:

```yaml
kind: GlobalDatasource
metadata:
  name: platform-metrics
spec:
  default: true
  plugin:
    kind: PrometheusDatasource
    spec:
      proxy:
        kind: HTTPProxy
        spec:
          url: http://<backend>.<namespace>.svc.cluster.local:9090
```

`proxy` rather than `directUrl` deliberately: the browser then queries through
the Perses server, which is the only place a per-client restriction could ever
be enforced. See [`../perses`](../perses/#tenancy-intended-not-built).

The three signals map to three datasource plugins — `PrometheusDatasource`,
`LokiDatasource` and `TempoDatasource` — and are otherwise the same shape.

## A dashboard arrives with its datasource

**No dashboard is provisioned, on purpose.** The platform deploys no metrics,
logs or traces backend — all three OTLP pipelines still terminate in the `debug`
exporter — so no environment declares a datasource, and a dashboard provisioned
now would be guaranteed to render errors on every panel.

That is not configuration waiting to become useful. It is future configuration
activated early, and it would be worse than nothing: a canonical platform
dashboard that is permanently broken teaches operators to ignore broken panels.

So the rule is a pairing rather than a wish:

> A dashboard is provisioned by the environment overlay that also provides the
> datasource it queries. Neither arrives alone.

When a backend lands, both arrive together — and the first dashboard is already
decided: the collector's own intake and export health, over `otelcol_*`. The
OpenTelemetry collector is the only observability component this platform
deploys, and *is telemetry arriving and leaving* is the question an operator asks
before any dashboard about a service means anything.

The mechanism itself is not waiting for that. `project.yaml` is a real Perses
resource, reconciled from Git through the whole path above, and Perses refuses
to provision a project-scoped resource whose project does not exist — so the
project has to be here before a dashboard could be.

## Authoring

A definition committed here is platform configuration, so it is reviewed like
any other change. That applies to *generated* definitions too — a dashboard
converted from another tool's format is a starting point, not a canonical
artefact, and is read before it is merged rather than after. Nothing arrives
here by export.

The instance itself is read-only, so authoring happens against a local Perses —
`percli` and the server both run standalone — and the result is committed.

## Module dashboards do not live here

A module that declares operational dashboards ships them with its own
definition, in the module catalogue:

```text
modules/
  appointments/
    module.yaml
    observability/
      overview.yaml
      api-health.yaml
```

They reach Perses the same way — as a labelled ConfigMap in `catalogue` — and
under the same pairing rule. This repository owns platform resources only.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [Perses](../perses/) | `40` | supplies the sidecar that reads these ConfigMaps |

Same wave. Within a wave Argo CD applies everything before waiting on health,
and the sidecar picks up a ConfigMap whenever it appears, so neither ordering
matters.

## Configuration owned by this repository

- the platform Perses project;
- platform-owned dashboard definitions, when an environment can serve them;
- per-environment datasource definitions.

## Configuration expected from outside this repository

- **A telemetry backend to point a datasource at.** Recorded as a known gap in
  [docs/architecture.md](../../../docs/architecture.md#known-gaps), and the
  reason this directory holds one file.
- **Module dashboards**, from the module catalogue.
- **Client projects, datasources and role bindings**, from client provisioning —
  none of which exist yet, and none of which may until Perses' tenancy
  assessment is complete.
