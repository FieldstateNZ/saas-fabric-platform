# Perses

| | |
|---|---|
| Product | Perses |
| Upstream project | https://github.com/perses/perses |
| Helm chart source | https://perses.github.io/helm-charts |
| Chart version (pinned) | `0.23.2` |
| Application version | `0.54.0` |
| Licence | Apache-2.0 |
| Namespace | `catalogue` |
| Grouping | `catalogue` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `40` |
| Plane | **operator** |

## Why Perses and not Grafana

Grafana is AGPL-3.0. Every other runtime this platform deploys is Apache-2.0 or
compatible with it, and SaaS Fabric intends to embed dashboard rendering in its
own React application rather than link to a separate one — which turns "what
licence does the dashboard runtime carry" from a deployment question into a
question about the product. Perses is Apache-2.0, a CNCF sandbox project, and
publishes the React packages that embedding actually needs.

The platform therefore takes no runtime dependency on Grafana: no server, no
dashboards, no datasource definitions, no frontend packages, no provisioning.

Nothing else about the reasoning changed, and that is the point of the
[service contract](platform-service.yaml). This is the service that retired the
repository's old `core`/`catalogue` classification, because three facts about it
are true at once and one directory name could not carry them:

| | |
|---|---|
| SaaS Fabric requires it | **no** — Fabric emits to the collector and is unaware of what reads it |
| Platform operators use it | **yes** — this is where operational visibility lives |
| It could serve clients | **plausibly** — projects are Perses' own separation unit |

The platform's observability contract is OTLP, not a dashboard. The collector is
required because everything reports through it; Perses is one way to look at
what it collects. See [`../../core/observability`](../../core/observability/).

## Git is the dashboard database

The one substantive change of shape. A dashboard runtime is normally deployed
writable, with an administrator password, and holds whatever someone last
clicked into it. Perses here is deployed **read-only**:

```yaml
config:
  security:
    readonly: true
```

`readonly` makes Perses skip registering the create, update and delete routes on
its resource endpoints entirely, and removes the UI's editing affordances.
Resources reach this instance one way — Perses' provisioning loader reads them
from a folder, and [`../perses-provisioning`](../perses-provisioning/) fills that
folder from this repository. Provisioning re-injects on an interval, so the
folder is authoritative rather than merely initial.

Reading is untouched. The flag is applied per resource endpoint, not as a blanket
method filter, so the datasource proxy that serves a panel's queries is
unaffected — which is the half of the API that Fabric and the explorer use.

That is what makes "reconciled means matches Git" true here, which for a
dashboard runtime is not the default. It is also why this application has no
credential at all: there is no write path to protect, and read is already gated
by the operator plane. One generated secret left the platform with the
substitution and nothing replaced it.

The cost is real and worth stating: an operator cannot build a dashboard by
dragging panels around in this instance. Dashboard development happens against a
local Perses — `percli` and the server both run standalone — and the result is
committed.

Nothing had to be migrated to get here. No dashboard definition was ever
committed to this repository, and production has never enabled the catalogue, so
the substitution replaced a runtime and not a body of work.

## Tenancy: intended, not built

Perses' project model is the same shape as Keycloak's realms — one runtime, a
platform administrative context, one partition per client:

```text
Perses runtime                 platform
  ├── platform project         platform
  ├── Acme project             client   ← intended
  └── Contoso project          client   ← intended
```

**Nothing client-scoped is implemented here, and the contract claims none.**
`tenancy.status` is `candidate`. The unresolved half is not really Perses: a
client project would need a datasource that returns only that client's
telemetry, and platform telemetry carries no per-client attribute to filter on.
See [the checklist](../../../docs/platform-services.md#assessing-tenancy) and the
known gap in [docs/architecture.md](../../../docs/architecture.md#known-gaps).

One thing Perses does bring to that problem: unless a datasource sets
`directUrl`, the browser queries it **through the Perses server**, which
proxies. A per-client restriction would therefore have somewhere server-side to
live, rather than depending on the frontend asking nicely — and frontend
filtering could never be the boundary. Datasources defined here use the proxy
for that reason, and that is a deliberate constraint on how they may be
written.

The intended ownership split, when it is built:

| | Owner |
|---|---|
| Deployment and runtime | this repository, via Argo CD |
| Platform project, dashboards and datasources | this repository |
| Client project | `saas-fabric-clients` |
| Client datasource definitions | client provisioning |
| Client datasource credentials | `secret/clients/<client>/*` |
| Platform datasource credentials | `secret/platform/*` |

A client project must not reach platform observability data by default. That is
a requirement of the design, not a later hardening step.

## Enabling it

Enabled per environment by including `applications/catalogue` in that
environment's kustomization. LucentRoot deploys it; production does not yet —
which does not make it a different kind of thing in each. See
[environments/README.md](../../../environments/README.md).

## Exposure

Operator plane only. Perses reads platform telemetry and is an operations
surface, not a product one, so it has no `HTTPRoute` and never appears on the
product edge — see
[docs/architecture.md](../../../docs/architecture.md#exposure-planes). That also
means it is reachable only in an environment that runs an operator plane;
LucentRoot does, production does not yet.

The chart renders its own `Ingress` rather than
[`operator-access`](../../core/operator-access/) doing it, because `catalogue` is
deliberately absent from the platform project's destinations — so nothing running
in the platform project can write into it.

## Endpoint contract

The intended shape is that SaaS Fabric renders operational views from Perses'
API rather than linking out to it, so the in-cluster address is part of the
contract:

```text
perses.catalogue.svc.cluster.local:8080
```

The Fabric side of that — the observability module, its adapter, and the
components that render a dashboard inside the Fabric shell — is application
source and is not in this repository. What this repository owes it is a
separately deployable Perses with a stable address, which is the boundary
either way:

```text
Fabric UI
    │  HTTP / API
    ▼
  Perses
```

and never a Perses embedded in the Fabric deployment. The standalone UI stays
published for operators; that does not make it the client experience.

## Dependencies

None hard. In practice it is only useful once
[`observability`](../../core/observability/) exports to a queryable backend,
which is why its data sources are configured per environment rather than here —
and why no environment configures one yet.

## Configuration owned by this repository

- deployment, persistence, service and RBAC scope;
- the read-only posture and the provisioning folder it reads;
- per-environment tailnet hostname and storage class.

## Configuration expected from outside this repository

- **Data source endpoints and credentials** for the environment's telemetry
  backend.
- **Client-shaped anything** — a project scoped to one client, or a dashboard
  over one client's telemetry — belongs to the client layer.
