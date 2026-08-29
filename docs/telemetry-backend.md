# Telemetry backend selection

> **Status: proposed.** This document exists to be argued with. It answers the
> two gates that can be answered from evidence, states the two that are the
> product's to answer, and ends with one recommendation, one option rejected for
> being too small and one rejected for being too large — so that over- and
> under-building are both visible rather than only the middle.

The platform emits OTLP to a collector whose three pipelines terminate in the
`debug` exporter. Nothing stores telemetry, so
[Perses](../applications/catalogue/perses/) currently has nothing to query. This
is the decision that changes that, and it is deliberately *not* framed as
"install Prometheus, Loki and Tempo".

## The gates, in order

The order matters. Gate 1 collapses or expands everything after it, and gate 2
determines whether any tenancy claim is a fact or a hypothesis.

---

## Gate 1 — can Perses enforce tenant scope at the proxy layer?

**No. But it is the right place to carry the tenant selector, and that is a
different answer from "no".**

Established by reading Perses v0.54.0 rather than its documentation, because the
whole shape of the decision rests on it.

### What the proxy actually does

`internal/api/impl/proxy/proxy.go` authorises the **datasource object** and then
forwards the request:

```go
func (e *endpoint) checkPermission(ctx, projectName, scope, action) error
...
reverseProxy := httputil.NewSingleHostReverseProxy(h.config.URL.URL)
reverseProxy.ServeHTTP(res, req)
```

It rewrites `URL.Path` to the datasource's path and sets `Host`, `X-Real-IP` and
`X-Forwarded-Proto`. **The query string and the request body pass through
untouched.** There is no hook that inspects, rewrites or constrains PromQL,
LogQL or TraceQL.

The entire configurable surface of an HTTP datasource is four fields:

```go
type Config struct {
    URL              *common.URL
    AllowedEndpoints []AllowedEndpoint   // {endpointPattern regex, method}
    Headers          map[string]string   // static
    Secret           string
}
```

### So what can it enforce?

| | |
|---|---|
| **Can** | who may use a given `Datasource` — RBAC on the resource, per project |
| **Can** | which endpoints and HTTP methods that datasource exposes |
| **Can** | a fixed URL and a fixed set of headers, per datasource |
| **Cannot** | anything about what a query *says* |

`Headers` is a static map on the datasource, not a per-request template. There
is no request-time substitution of the calling user's identity.

### The consequence, which is the useful part

Tenant isolation is enforceable through Perses only in one shape:

```text
one datasource per tenant
    │  carrying a static tenant selector: a URL path or a header
    ▼
backend enforces scope from that selector
    │
Perses RBAC decides who may use which datasource
```

Which yields the criterion that does the most work in this document:

> **A candidate must express tenancy as something addressable by a static URL or
> a static header. A backend whose only tenancy story is "put a filter in the
> query" cannot be isolated by anything in this architecture.**

Two honest caveats:

- The RBAC half **does not exist yet**. Perses runs with `enable_auth: false`,
  bounded by the `exposure.plane: operator` constraint in its
  [service contract](../applications/catalogue/perses/platform-service.yaml).
  Gate 1's answer is therefore *conditional on* closing the authentication gap
  already recorded in [architecture.md](architecture.md#known-gaps). Choosing a
  backend does not close it.
- Per-tenant datasources are generated configuration. They belong to client
  provisioning, not to this repository — the same split Keycloak realms already
  have.

---

## Gate 2 — what tenant attribute will exist on telemetry?

**Unanswered, and it is a prerequisite rather than a detail.** Today telemetry
carries no client dimension at all, which is recorded as a known gap and is why
the collector's contract says `tenancy.status: not-applicable`.

What the decision needs from the answer:

**It must be applied at ingest, by the collector, not by the emitter.** An
attribute a workload sets about itself is a claim; an attribute the collector
stamps from Kubernetes metadata is a fact. The platform already runs the
`k8sattributes` processor, and client workloads run in client-owned namespaces,
so the raw material exists. Adopting it makes the collector's tenancy status
`unresolved` rather than `not-applicable`, which is a real change to that
contract.

**Its shape decides how much it costs.** This is where gate 2 meets gate 4:

| Shape | Cost |
|---|---|
| A metrics **label** (`tenant_id="acme"`) | multiplies active series by tenant count, on top of the `module` / `service` / `environment` / `region` / `version` dimensions the telemetry convention already proposes |
| A backend **tenant** (an `AccountID`, a header) | partitions storage instead of multiplying series, and is the only shape gate 1 can enforce |

These are not equivalent, and the second is both cheaper and safer. A label is
a filter someone can forget to apply. A tenant is a partition the query cannot
reach across.

**Recommendation for gate 2:** stamp a tenant identifier at the collector *and*
map it to a backend tenant at export. Carry the label too if it is useful for
platform-wide aggregate views, but do not let it be the isolation mechanism.

---

## Gate 3 — what signals do we actually need?

**Partly the product's to answer.** What can be said from what already exists:

| Signal | Position | Why |
|---|---|---|
| **Metrics** | **needed** | service status, request rate, error rate, latency and SLO state are the operational surface Fabric describes. Nothing else supplies them |
| **Logs** | **needed** | "recent application failures" and "recent logs" are named drill-downs. A metrics-only platform makes every incident end at a dashboard with nowhere to go |
| **Traces** | **deliberately open** | the intake exists and Perses can explore them, but no Fabric surface has yet been specified that consumes a trace. This is the one where demand is asserted rather than demonstrated |
| **Profiles** | **no** | nothing asks for it, and the mature option is AGPL-3.0 |

The recommendation is to **keep trace intake and defer trace storage**. The
collector already accepts them; the pipeline can keep terminating in `debug`
until a Fabric surface needs one. Storing a signal nobody queries is the
cheapest possible way to overbuild.

---

## Gate 4 — retention and query shape

The structural fact that makes this decision much smaller than it looks:

> **This platform already has an analytical path, and it is not this one.**

Airflow builds purpose-specific projections into an analytics database, and
Superset renders them. Operational telemetry is explicitly *not* to be copied
wholesale into client databases. So the operational store is not the historical
record, and does not have to behave like one.

```text
operational                            analytical
Perses                                 Superset
  │  hot window, high selectivity        │  months to years, aggregated
  ▼                                      ▼
telemetry store  ──  Airflow  ──▶  analytics database
                     reads and projects
```

| | Operational | Analytical |
|---|---|---|
| Retention | hours to ~30 days | already someone else's job |
| Query shape | point-in-time and short range, per service, per tenant | scheduled, aggregate, bulk |
| Reader | a person, during an incident | a DAG, on a schedule |

Two consequences:

- **Retention is the largest cost lever and it points downward.** A 30-day
  operational store is a materially different — and cheaper — proposition than a
  multi-year metrics warehouse. Any candidate justified by long retention is
  being justified against a requirement this platform does not have.
- **Airflow needs a bulk read path**, not just a dashboard query API. That is a
  selection criterion in its own right, and it is easy to forget because it is
  not the path a human uses.

---

## Criteria, derived

| | From | Criterion |
|---|---|---|
| C1 | gate 1 | tenancy addressable by static URL or header |
| C2 | platform | Apache-2.0 or compatible — the constraint that replaced Grafana |
| C3 | Perses | a datasource plugin exists in `perses/plugins` (Apache-2.0) |
| C4 | gate 4 | operational retention; not required to be a warehouse |
| C5 | platform | component count this platform must run, secure and reason about |

## The candidates

Licences verified against each project's repository, not recalled.

| Candidate | Licence | Perses plugin | Tenancy addressing | Runtimes |
|---|---|---|---|---|
| Prometheus | Apache-2.0 | `prometheus` | **none** | 1 |
| Loki | **AGPL-3.0** | `loki` | `X-Scope-OrgID` header | 1+ |
| Tempo | **AGPL-3.0** | `tempo` | `X-Scope-OrgID` header | 1+ |
| Mimir | **AGPL-3.0** | via `prometheus` | `X-Scope-OrgID` header | several |
| Pyroscope | **AGPL-3.0** | `pyroscope` | header | 1 |
| VictoriaMetrics (cluster) | Apache-2.0 | `prometheus`, documented by upstream | `accountID` in URL **or header** | 3 |
| VictoriaLogs | Apache-2.0 | `victorialogs` | `AccountID` / `ProjectID` headers | 1 |
| VictoriaTraces | Apache-2.0 | *(unverified — see spike)* | *(unverified)* | 1 |
| ClickHouse | Apache-2.0 | `clickhouse` | row policies bound to a user, one datasource per tenant | 1 |
| GreptimeDB | Apache-2.0 | `greptimedb` | *(unverified)* | 1 |
| Jaeger | Apache-2.0 | `jaeger` | *(unverified)* | 1 |
| OpenObserve | **AGPL-3.0** | — | — | — |
| SigNoz | mixed / no single SPDX | — | — | — |

### The finding that reframes this

**Two thirds of "Prometheus + Loki + Tempo" are AGPL-3.0.**

This platform removed Grafana specifically and only because it was AGPL-3.0, and
replaced it with Perses at the cost of a full substitution. Adopting Loki and
Tempo would reintroduce that exact constraint weeks later, with two components
instead of one, from the same vendor.

That is not a licensing footnote. It is the same decision, asked again, and
answering it differently the second time would mean the first answer was wrong.

### The finding that widens it

`perses/plugins` is Apache-2.0 and already ships datasource schemas for
`clickhouse`, `victorialogs`, `greptimedb`, `jaeger`, `opensearch` and `splunk`
alongside `prometheus`, `loki` and `tempo`. Choosing something outside the
Grafana stack does **not** mean writing a Perses plugin.

---

## Recommendation

### Adopt: VictoriaMetrics for metrics, VictoriaLogs for logs, traces deferred

```text
OTLP ──▶ collector ──┬──▶ VictoriaMetrics   metrics, ~30d, tenant per accountID
                     ├──▶ VictoriaLogs      logs, ~30d, AccountID/ProjectID headers
                     └──▶ debug             traces, until a Fabric surface needs them
```

| Criterion | |
|---|---|
| C1 tenancy | **the strongest fit available.** Both express a tenant as a header or URL segment — precisely and only what the Perses proxy can carry. VictoriaLogs documents "thousands of tenants in a single instance" as normal |
| C2 licence | Apache-2.0 throughout. No re-litigation of the decision that produced Perses |
| C3 Perses | metrics through the `prometheus` plugin, integration documented by VictoriaMetrics itself; logs through the first-party `victorialogs` plugin |
| C4 retention | sized for an operational window, with the analytical path left where it already is |
| C5 cost | VictoriaLogs is one process. VictoriaMetrics **cluster** is three, and single-node has no tenancy — so tenancy costs real components, and that is the honest price of C1 |

Two things this does not do, stated so they are not discovered later:

- **VictoriaMetrics does not authenticate tenants.** Upstream is explicit that
  auth tokens and tenant mapping belong to a service in front of it. Here that
  service is Perses' proxy plus per-tenant datasources — which is exactly why
  gate 1's authentication caveat is load-bearing rather than cosmetic.
- **It commits to two stores rather than one.** The single-store alternative is
  below, and it is a genuine contender rather than a straw man.

### Runner-up, and the reason it is not the recommendation

**ClickHouse as a single store for all three signals.** Apache-2.0, one runtime,
a first-party Perses datasource plugin, an excellent bulk read path for Airflow,
and it collapses the operational and analytical stores into one thing.

It loses on C1. Tenancy would be row policies bound to database users, with one
Perses datasource per tenant carrying that tenant's credentials — workable, but
it is a schema and access-control design this platform would own and operate,
rather than a header the store already understands. It also makes the platform
responsible for a telemetry schema, which is a larger commitment than running a
purpose-built store.

Worth revisiting if the answer to gate 3 turns out to be "metrics and logs, with
retention longer than 30 days", because at that point the two stores start
duplicating what ClickHouse does in one.

### Rejected as too simple: single-node Prometheus, no log store

The smallest thing that could plausibly be called done. Apache-2.0, one process,
a Perses plugin, and the platform could show a dashboard this week.

It fails two gates outright:

- **C1.** Prometheus has no tenancy. Isolation would be a label filter written
  into each query — exactly the shape gate 1 established Perses cannot enforce.
  Tenant A would be one edited URL away from Tenant B's telemetry.
- **Gate 3.** No log store means every incident ends at a graph. The drill-down
  from a metric to a log line is the thing operational observability is *for*.

Named here so the floor is visible: this is what under-building looks like, and
it is not far below the recommendation in effort.

### Rejected as too complete: Mimir + Loki + Tempo + Pyroscope

Every tenancy mechanism we want, mature, well documented, and the arrangement
most teams would reach for without thinking.

- **Four AGPL-3.0 components**, re-opening the decision that produced Perses.
- **Four more runtimes** to deploy, secure, upgrade and reason about, each with
  its own tenancy configuration.
- **Mimir is a metrics warehouse.** Its central advantage is long retention at
  scale — which is the requirement gate 4 established this platform does not
  have, because Airflow and Superset already own history.
- **Pyroscope stores a signal nothing asks for.**

Named here so the ceiling is visible: this is what overbuilding looks like, and
it is expensive in exactly the dimensions this platform has been careful about.

---

## What this document cannot decide

| Open | Owner |
|---|---|
| Gate 2 — the tenant attribute, its name and where it is stamped | platform; blocks any tenancy claim |
| Gate 3 — whether traces are stored now or later | product |
| Retention numbers | product and cost, not architecture |
| Whether VictoriaTraces, GreptimeDB or Jaeger tenancy meets C1 | unverified above; a spike, not a debate |

## Proving it before adopting it

Deliberately small, and answering the questions that would actually change the
recommendation:

1. Stand up VictoriaMetrics and VictoriaLogs on LucentRoot; point the collector's
   exporters at them. Confirms the intake path end to end.
2. Provision two Perses datasources differing **only** by tenant selector, and
   confirm from the browser that one cannot return the other's data. This is the
   gate 1 claim, tested rather than reasoned about.
3. Confirm what a tenant costs: series overhead, storage, and whether
   VictoriaMetrics cluster's three processes are proportionate at LucentRoot's
   size.
4. Have Airflow read a range and build one projection. Confirms the bulk path
   nobody notices until it is missing.

Each is a platform service candidate with its own contract and tenancy
assessment before it may be adopted — the process in
[platform-services.md](platform-services.md#assessing-tenancy) applies here
unchanged, and nothing in this document short-circuits it.
