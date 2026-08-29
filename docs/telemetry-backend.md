# Telemetry backend selection

> **Status: proposed.** This document exists to be argued with, and has been
> once: the first draft proved tenant isolation on the **read** path and was
> silent on the **write** path, which is the half that may actually decide it.
> It now answers what evidence can answer, states what is the product's to
> answer, and ends with one recommendation — explicitly split into a settled
> half and a provisional one — plus one option rejected for being too small and
> one for being too large, so that over- and under-building are both visible.

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

Which yields the criterion that does the most work in this document. Stated as
the invariant rather than as one mechanism, because more than one mechanism
satisfies it:

> **Tenant scope must be completely determined by immutable datasource
> configuration and enforced by the backend. It must not depend on the query
> carrying the correct filter.**

Valid implementations, all equivalent under that rule:

```text
URL path                  /select/<accountID>/…
HTTP header               AccountID: 7
credential / principal    datasource authenticates as a role the backend
                          scopes — a ClickHouse row policy, for instance
```

What it excludes is the thing worth excluding: a shared endpoint where
correctness depends on every dashboard remembering `tenant_id="acme"`.

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

**Its shape decides what can be enforced — not what it costs.**

| Shape | What it gives you |
|---|---|
| A metrics **label** (`tenant_id="acme"`) | a filter every query must remember to apply |
| A backend **tenant** (an `AccountID`, a header, a database principal) | isolation that is structural: the query cannot reach across it, whatever it says |

The second is **safer**. It is not cheaper, and an earlier draft of this
document said it was. VictoriaMetrics is explicit that "the database performance
and resource usage do not depend on the number of tenants. It depends mostly on
the total number of active time series in all the tenants" — so if Acme and
Contoso each emit `http_requests_total{service="api"}`, that is two series
either way. Backend tenancy buys a boundary, not a discount, and selling it as
performance would be selling it dishonestly.

### The write path is the hard half, and it is a selection criterion

The read path is settled by gate 1: a static selector on a per-tenant datasource.
The write path is not, and it does not follow from it.

**OTLP exporter headers are exporter configuration.** They are static. There is
no `AccountID: ${resource.attributes.tenant_id}`, so a single collector holding
an interleaved batch —

```text
Acme metric │ Contoso metric │ Acme log │ Contoso log
```

— cannot be split into tenants by a header the exporter sets once. What each
candidate does about that differs sharply, and it is the difference this
document was previously silent on.

**VictoriaMetrics answers it natively, and well.** Its multitenant ingest
endpoint takes the tenant from `vm_account_id` / `vm_project_id` labels **on
each sample**, and strips them before storage:

```text
k8s metadata ─▶ collector stamps tenant_id ─▶ transform to vm_account_id
                                                      │
                                            one mixed stream
                                                      ▼
                              /insert/multitenant/…  ─▶  tenant partitions
```

One exporter, any number of clients. No per-client collector configuration.

**VictoriaLogs and VictoriaTraces do not.** Both identify a tenant by
`(AccountID, ProjectID)` **request headers** — per request, not per entry. There
is a mixed-tenant endpoint, `/insert/multitenant/native`, but it is documented
as the path `vlagent` writes to in the native protocol, not as something an
arbitrary OTLP batch can use. So the options are:

| Option | Cost |
|---|---|
| Route by `tenant_id` in the collector to one exporter per tenant, via the `routing` connector | **generated per-client collector configuration**, growing with the client list, on an `alpha` component. A materially different operational shape from one static exporter |
| Put `vlagent` in front and write to `/insert/multitenant/native` | another runtime, and it is unverified whether it can derive tenancy from OTLP-sourced fields |
| Something cleaner nobody has verified | unknown |

**ClickHouse answers it trivially**, because the tenant is a column value rather
than a routing decision. One exporter, mixed batches, tenant carried in the row.
Its cost sits on the read side instead, where isolation is a row policy this
platform designs.

This asymmetry may decide the recommendation, so it is spike step 1 rather than
a footnote.

**Recommendation for gate 2:** stamp a tenant identifier at the collector, from
Kubernetes metadata, and require of any candidate that a **single** collector
configuration can deliver a mixed-tenant stream into the right partitions.
Carry the label too if platform-wide aggregate views want it, but do not let a
label be the isolation mechanism.

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
| C1 | gate 1 | **read isolation** — tenant scope fixed by immutable datasource configuration and enforced by the backend, never by a filter in the query |
| C2 | platform | Apache-2.0 or compatible — the constraint that replaced Grafana |
| C3 | Perses | a datasource plugin exists in `perses/plugins` (Apache-2.0) |
| C4 | gate 4 | operational retention; not required to be a warehouse |
| C5 | platform | component count this platform must run, secure and reason about |
| C6 | gate 2 | **write isolation** — one collector configuration delivers a mixed-tenant stream into the right partitions, without per-client pipelines |

C1 and C6 are separate criteria on purpose. A backend can pass one and fail the
other, and the recommendation below turns on precisely that.

## The candidates

Licences verified against each project's repository, not recalled.

| Candidate | Licence | Perses plugin | C1 read isolation | C6 mixed-tenant ingest | Runtimes |
|---|---|---|---|---|---|
| Prometheus | Apache-2.0 | `prometheus` | **none** | n/a | 1 |
| Loki | **AGPL-3.0** | `loki` | `X-Scope-OrgID` header | per-request header | 1+ |
| Tempo | **AGPL-3.0** | `tempo` | `X-Scope-OrgID` header | per-request header | 1+ |
| Mimir | **AGPL-3.0** | via `prometheus` | `X-Scope-OrgID` header | per-request header | several |
| Pyroscope | **AGPL-3.0** | `pyroscope` | header | — | 1 |
| VictoriaMetrics (cluster) | Apache-2.0 | `prometheus`, documented upstream | `accountID` in URL or header | **yes** — `vm_account_id` label per sample, stripped before storage | 3 |
| VictoriaLogs | Apache-2.0 | `victorialogs` | `AccountID` / `ProjectID` headers | **per-request header.** Mixed-tenant endpoint documented only for `vlagent` native protocol | 1 (+ `vlagent`?) |
| VictoriaTraces | Apache-2.0 | *(unverified)* | `AccountID` / `ProjectID` headers | same as VictoriaLogs | 1 |
| ClickHouse | Apache-2.0 | `clickhouse` | row policy bound to the datasource's principal | **yes** — the tenant is a column value | 1 |
| GreptimeDB | Apache-2.0 | `greptimedb` | *(unverified)* | *(unverified)* | 1 |
| Jaeger | Apache-2.0 | `jaeger` | *(unverified)* | *(unverified)* | 1 |
| OpenObserve | **AGPL-3.0** | — | — | — | — |
| SigNoz | mixed / no single SPDX | — | — | — | — |

One property is shared by every Victoria component and worth stating once:
**none of them performs per-tenant authorization.** Upstream says so plainly and
points at `vmauth`. In this architecture that role is Perses' proxy plus
per-tenant datasources — which is why the authentication caveat under gate 1 is
load-bearing rather than cosmetic.

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

### Adopt: VictoriaMetrics for metrics. VictoriaLogs for logs, pending one test

```text
OTLP ──▶ collector ──┬──▶ VictoriaMetrics   metrics, ~30d, tenant per sample label
                     ├──▶ VictoriaLogs      logs, ~30d — if C6 holds; see below
                     └──▶ debug             traces, until a Fabric surface needs them
```

| Criterion | |
|---|---|
| C1 read | **the strongest fit available.** Both fix tenant scope in datasource configuration — a URL segment or a header — with nothing left to the query |
| C2 licence | Apache-2.0 throughout. No re-litigation of the decision that produced Perses |
| C3 Perses | metrics through the `prometheus` plugin, integration documented by VictoriaMetrics itself; logs through the first-party `victorialogs` plugin |
| C4 retention | sized for an operational window, with the analytical path left where it already is |
| C5 cost | VictoriaLogs is one process. VictoriaMetrics **cluster** is three, and single-node has no tenancy — so tenancy costs real components, and that is the honest price of C1 |
| C6 write | **metrics: yes. Logs: unproven, and this is the open question** |

### The metrics half is settled; the logs half is provisional

**VictoriaMetrics passes C6 outright.** Mixed-tenant ingest from one exporter,
tenant taken per sample from a label the collector stamps. That is as clean a
fit with a shared collector as this architecture could ask for.

**VictoriaLogs has not been shown to.** Its tenant is a per-request header, and
the mixed-tenant endpoint is documented for `vlagent`'s native protocol rather
than for OTLP. If the only route is one exporter per client, the platform
acquires generated per-client collector configuration on an `alpha` connector —
which is a different operational proposition from what this recommendation
otherwise describes, and not obviously the right trade.

So the recommendation is deliberately split:

| | |
|---|---|
| **VictoriaMetrics for metrics** | recommended, and unlikely to change |
| **VictoriaLogs for logs** | **provisional**, pending spike step 1 |
| **Traces** | deferred either way — see gate 3 |

**What would flip it:** if VictoriaLogs cannot take a mixed-tenant OTLP stream
without per-client collector configuration or an extra runtime, ClickHouse
becomes the better answer for logs — and at that point running ClickHouse for
both signals is more coherent than running VictoriaMetrics beside it. A
better-looking read path does not win an argument the write path decides.

Also stated so it is not discovered later: **this commits to two stores rather
than one.** The single-store alternative is immediately below, and it is a
genuine contender.

### Runner-up: ClickHouse as a single store

Apache-2.0, one runtime, a first-party Perses datasource plugin, an excellent
bulk read path for Airflow, and it collapses the operational and analytical
stores into one thing.

It **satisfies C1 by a different mechanism**, not by failing it: a row policy
bound to the principal the datasource authenticates as is exactly as immutable,
from the query's point of view, as a header. An earlier draft scored this as a
loss, which was wrong — the criterion is that the query cannot widen its own
scope, not that the mechanism is a header.

It **satisfies C6 trivially**, because the tenant is a column value rather than
a routing decision. One exporter, mixed batches, no per-client configuration.

Its real cost is ownership: the platform designs and operates the schema, the
row policies and the per-tenant principals, rather than configuring a store that
already understands tenants. The collector's ClickHouse exporter is also `beta`
for traces and logs and `alpha` for metrics, which is a maturity gap worth
weighing against VictoriaMetrics' native path.

It becomes the recommendation if spike step 1 goes against VictoriaLogs, and it
is worth revisiting anyway if retention turns out to exceed the operational
window, because at that point two stores start duplicating what one does.

### Rejected as too simple: single-node Prometheus, no log store

The smallest thing that could plausibly be called done. Apache-2.0, one process,
a Perses plugin, and the platform could show a dashboard this week.

It fails two criteria outright:

- **C1.** Prometheus has no tenancy at all. Scope would live in the query as a
  label filter — precisely what C1 exists to forbid, because the query is the
  one thing the reader controls. Tenant A would be one edited URL away from
  Tenant B's telemetry, and nothing in the architecture would object.
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
| **C6 for logs** — whether a mixed-tenant OTLP stream can reach VictoriaLogs tenants without per-client collector configuration | **blocks the logs half of the recommendation** |
| Gate 2 — the tenant attribute, its name and where it is stamped | platform; blocks any tenancy claim |
| Gate 3 — whether traces are stored now or later | product |
| Retention numbers | product and cost, not architecture |
| Whether GreptimeDB or Jaeger tenancy meets C1, and whether Perses can query VictoriaTraces | unverified above; a spike, not a debate |

## Proving it before adopting it

Deliberately small, and answering the questions that would actually change the
recommendation:

1. **The one that decides it.** Send interleaved Acme and Contoso metrics *and*
   logs through a single collector instance, deriving tenant identity from
   Kubernetes metadata, and prove each signal lands in the correct backend
   tenant **without maintaining an exporter or pipeline per client.** Metrics
   are expected to pass via `vm_account_id`. Logs are the question. If logs
   cannot, run the same test against ClickHouse before concluding anything.
2. Provision two Perses datasources differing **only** by tenant selector, and
   confirm from the browser that one cannot return the other's data. This is the
   C1 claim, tested rather than reasoned about.
3. Confirm what a tenant costs: series overhead, storage, and whether
   VictoriaMetrics cluster's three processes are proportionate at LucentRoot's
   size.
4. Have Airflow read a range and build one projection. Confirms the bulk path
   nobody notices until it is missing.

Step 1 comes first because it can change the answer. Steps 2–4 refine a choice;
step 1 makes it.

Each is a platform service candidate with its own contract and tenancy
assessment before it may be adopted — the process in
[platform-services.md](platform-services.md#assessing-tenancy) applies here
unchanged, and nothing in this document short-circuits it.
