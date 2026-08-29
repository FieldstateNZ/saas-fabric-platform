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

Dashboards, datasources and projects all travel that path. Today it carries the
platform project and nothing else, because a dashboard with no datasource behind
it is not configuration — see
[`../perses-provisioning`](../perses-provisioning/#a-dashboard-arrives-with-its-datasource).

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

## A project is a grouping, not a boundary

Read `candidateUnit: project` narrowly, because the word invites exactly the
wrong inference. Say it once, plainly:

```text
Perses project    =  a namespace for observability resources
                     dashboards, datasources, variables, roles

Perses project    ≠  a Fabric tenant authority
Tenant isolation  =  enforced at the telemetry datasource, by a mechanism
                     that does not exist yet
```

One Perses project per client would be a filing arrangement. It would not be a
tenancy model, and nothing about creating one would make a client's telemetry
unreachable from another client's dashboard. Anyone reaching for *"we isolate
clients with Perses projects"* has skipped the part where the isolation happens.

## Tenancy: intended, not built

With that said, the shape is the same as Keycloak's realms — one runtime, a
platform administrative context, one partition per client:

```text
Perses runtime                 platform
  ├── platform project         platform
  ├── Acme project             client   ← intended
  └── Contoso project          client   ← intended
```

**Nothing client-scoped is implemented here, and the contract claims none.**
`tenancy.status` is `candidate`, and `check.py` refuses any contract that claims
client capability or provisioning while it stays that way.

The unresolved half is not really Perses. A client project would need a
datasource that returns only that client's telemetry, and platform telemetry
carries no per-client attribute to filter on — so the boundary has no dimension
to enforce even if the grouping were in place. That is the ordering: the
attribute, then the enforcement, then the grouping. Not the grouping first. See
[the checklist](../../../docs/platform-services.md#assessing-tenancy) and the
known gap in [docs/architecture.md](../../../docs/architecture.md#known-gaps).

One thing Perses does bring to the problem: unless a datasource sets
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

### That is a constraint, not a description

Perses runs with `enable_auth: false`. That is acceptable, and it is acceptable
for one reason only:

```text
tailnet / operator plane      every viewer is a platform operator
        ↓
unauthenticated Perses        nothing to sign in to
        ↓
read-only                     and nothing to change if you did
```

Take away the top line and the other two stop being reassuring. A
client-reachable route would not be a routing change; it would be a change of
security posture, made in four lines of YAML that do not look alarming.

So it is declared and checked rather than remembered. The contract carries:

```yaml
exposure:
  plane: operator
  backends: [perses]
```

and `check_operator_only_services` in [`scripts/check.py`](../../../scripts/check.py)
fails the build on any route that could reach a named Service **from the product
plane** — resolved the way the Gateway resolves it, by the listener the route
names or, when it names none, by the grant its namespace carries. An
operator-plane route is exactly what the constraint permits, so it passes; that
distinction is verified in both directions rather than assumed.

The `catalogue` namespace not carrying `gateway-access` is what stops it today —
but this repository has twice been wrong about an absent label being a
guarantee, and an absent label is nobody's decision. This is the decision.

> **Perses must not be reachable from a client-accessible route until user
> authentication, authorization and an established tenancy model exist.**

Lifting it means building those three things, and then changing this section
deliberately. It does not mean deleting it.

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

Splitting the two halves, so *done* means something on each side:

```text
this repository                       the application repository
  ✓ Perses deployed                     □ observability adapter
  ✓ API boundary at a stable address    □ Perses rendering integration
  ✓ resource provisioning mechanism     □ Fabric permission enforcement
  ✓ datasource provisioning mechanism   □ client context propagation
  ✓ dashboard-as-code mechanism
  ✓ no Grafana remains
  □ a telemetry backend to query
```

The right-hand column cannot be finished here, and the left-hand column should
not have waited for it. The one box still open on the left is the one this
change deliberately did not force: see
[`../perses-provisioning`](../perses-provisioning/#a-dashboard-arrives-with-its-datasource).

## Dependencies

None hard. In practice it is only useful once
[`observability`](../../core/observability/) exports to a queryable backend.
Datasources are supplied through
[`../perses-provisioning`](../perses-provisioning/) rather than through this
chart's values, so adding one touches no part of this deployment — and no
environment adds one yet, because there is nothing to point it at.

## Configuration owned by this repository

- deployment, persistence, service and RBAC scope;
- the read-only posture and the provisioning folder it reads;
- per-environment tailnet hostname and storage class.

## Configuration expected from outside this repository

- **A telemetry backend**, and the data source endpoints and credentials that
  address it. Its absence is the reason this service currently has nothing to
  show, and choosing it is a decision in its own right rather than an
  implementation detail of this one.
- **The Fabric observability module** and everything above the API boundary.
- **Client-shaped anything** — a project scoped to one client, or a dashboard
  over one client's telemetry — belongs to the client layer, and not before its
  tenancy is established.
