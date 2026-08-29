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
  project.yaml                     the platform Perses project
  dashboards/
    telemetry-pipeline.yaml        the collector's own intake and export health
overlays/
  lucentroot/                      datasources, when an environment has one
```

`project.yaml` is separate from the dashboards for a reason that is not
tidiness: Perses refuses to provision a project-scoped resource whose project
does not exist. Ordering within a batch is safe — Perses applies every `Project`
first, whatever order the files were read in — but the project has to be
*present*.

## Authoring

A definition committed here is platform configuration, so it is reviewed like
any other change. That applies to *generated* definitions too — a dashboard
converted from another tool's format is a starting point, not a canonical
artefact, and is read before it is merged rather than after. Nothing arrives
here by export.

The instance itself is read-only, so authoring happens against a local Perses —
`percli` and the server both run standalone — and the result is committed.

## The dashboard queries nothing yet

`telemetry-pipeline` names no datasource. Its queries take the environment's
default `PrometheusDatasource`, which keeps one definition valid everywhere and
leaves the endpoint an environment fact.

No environment defines one. The collector's pipelines still terminate in the
`debug` exporter, so there is no queryable backend to point at — see the known
gap in [docs/architecture.md](../../../docs/architecture.md#known-gaps). The
dashboard is committed anyway, because the path from Git to Perses is the thing
worth having working before there is data flowing through it.

Adding a datasource is a file in an environment overlay:

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

They reach Perses the same way — as a labelled ConfigMap in `catalogue` — but
this repository owns platform dashboards only. Client-shaped anything belongs to
the client layer.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [Perses](../perses/) | `40` | supplies the sidecar that reads these ConfigMaps |

Same wave. Within a wave Argo CD applies everything before waiting on health,
and the sidecar picks up a ConfigMap whenever it appears, so neither ordering
matters.

## Configuration owned by this repository

- the platform Perses project;
- platform-owned dashboard definitions;
- per-environment datasource definitions.

## Configuration expected from outside this repository

- **Module dashboards**, from the module catalogue.
- **Client projects, datasources and role bindings**, from client provisioning —
  none of which exist yet, and none of which may until Perses' tenancy
  assessment is complete.
