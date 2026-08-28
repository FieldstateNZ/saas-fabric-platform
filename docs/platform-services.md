# Platform services

> **`saas-fabric-platform` owns shared platform service runtimes. A shared
> service may serve platform operations, client capabilities, or both.
> Client-specific partitions are owned by client provisioning, not by the shared
> runtime definition.**

And the corollary that motivated this document:

> **Required versus optional is a deployment dependency property. It is not the
> service's architectural identity.**

## Why the old model was wrong

The repository used to classify every application by one question — *does SaaS
Fabric require this to operate?* — and answered it into two directories, `core`
and `catalogue`. That conflated two unrelated concerns: whether SaaS Fabric
depends on a service, and whether that service can offer client-scoped
capability.

Grafana is where it broke. SaaS Fabric runs without Grafana, so Grafana was
catalogue, so Grafana read as peripheral. All three of these are true at once:

- SaaS Fabric does not require it;
- platform operators use it daily, and it is where operational visibility lives;
- its organisation model is a plausible client partition.

One binary cannot carry three independent facts. A service that is optional,
operator-critical and potentially client-partitionable is not an edge case — it
is the normal shape of a shared service.

## The four dimensions

Independent. Do not infer any one from any other.

| Dimension | Asks |
|---|---|
| `required` | does SaaS Fabric fail to operate without it? |
| `operatorUsage` | do the people running the platform use it? |
| `clientPartitioning` | can one runtime hold separated client partitions? |
| `clientCapability` | is it offered to a client as a selectable capability? |

Two inferences that look reasonable and are not:

- **Optional does not mean peripheral.** Grafana is optional and
  operator-critical.
- **Partitionable does not mean offered.** Keycloak partitions strongly per
  client, and no client selects it — every client has identity by virtue of
  being a client.

## The reference pattern

Keycloak is the model every other shared service is measured against, because
its boundary is the one upstream actually enforces.

```text
Keycloak runtime            platform
  ├── master / admin        platform
  ├── Acme realm            client
  └── Contoso realm         client
```

Grafana is the same shape, one step less proven:

```text
Grafana runtime             platform
  ├── platform organisation platform
  ├── Acme organisation     client   ← intended, not built
  └── Contoso organisation  client   ← intended, not built
```

The pattern generalises to: **one runtime, one platform administrative context,
one partition per client** — and it applies only where the upstream project
supplies a real isolation boundary.

## Who owns what

| Service | Platform owns | Client provisioning owns |
|---|---|---|
| Keycloak | deployment, master/admin | realm |
| Grafana | deployment, platform organisation | client organisation *(intended)* |
| OpenBao | deployment, `secret/platform/*` | `secret/clients/<client>/*`, policies |
| CloudNativePG | operator, shared infrastructure | client database |
| Envoy Gateway | controller, shared `Gateway` | client host routes |
| OpenFGA | runtime | client authorization partition *(undecided)* |
| OpenTelemetry | collector and pipelines | — |
| Superset | — | — *(isolation unproven)* |
| Airflow | deployment | — *(not an isolation boundary)* |

Superset and Airflow join the right-hand column only once their isolation model
is proven, not because it would be convenient.

## The contract

Every application directory carries a `platform-service.yaml`. Capability is
declared metadata, not filesystem position — a service does not become
peripheral by living in a particular directory.

```yaml
service: keycloak
deployment: adopted          # adopted | planned | assessed
required: true
operatorUsage: true
clientPartitioning:
  mode: strong               # none | logical | strong
  unit: realm
  provisioning: supported    # supported | unsupported
  owner: saas-fabric-clients
clientCapability:
  available: false
tenancy:
  status: accepted           # accepted | candidate | unresolved | rejected
  rationale: |
    ...
```

A directory that supports a service rather than being one declares that
instead, so the register stays honest about what is actually a service:

```yaml
componentOf: keycloak
```

`scripts/check.py` enforces the contract, including the rule that matters most:
**a service may not claim client capability or client provisioning while its
tenancy status is anything other than `accepted`.** Intent cannot be written
down as though it were a boundary.

## The register

| Service | Required | Operator | Partitioning | Tenancy | Deployed |
|---|---|---|---|---|---|
| Envoy Gateway | yes | no | logical — `HTTPRoute` | accepted | yes |
| CloudNativePG | yes | no | strong — `Cluster` | accepted | yes |
| External Secrets | yes | no | logical — `SecretStore` | accepted | yes |
| OpenBao | yes | yes | strong — path prefix | accepted | yes |
| Keycloak | yes | yes | strong — realm | accepted | yes |
| OpenTelemetry | yes | no | none | unresolved | yes |
| SaaS Fabric | yes | no | none | rejected — owns clients | yes |
| Tailscale | no | yes | none | rejected — by design | yes |
| Grafana | no | yes | logical — organisation | **candidate** | yes |
| OpenFGA | **yes** | yes | strong — store | candidate | **planned** |
| Superset | no | yes | none | unresolved | assessed |
| Airflow | no | yes | none | rejected | assessed |

OpenFGA is the row worth pausing on: required by the intended SaaS Fabric
runtime and not yet deployed. That pairing is deliberate — recording it as
optional would make the platform look complete when it is not. It is on the
known-gaps list for exactly that reason.

## Assessing tenancy

A service is not multi-tenant because it has users, roles or permissions.
Before `tenancy.status` becomes `accepted`, assess each of these separately and
record what is unknown rather than filling it in:

- authentication isolation;
- authorization isolation;
- data isolation;
- secret isolation;
- administrator escape paths;
- API isolation;
- background-job isolation;
- cross-client enumeration;
- lifecycle and provisioning model.

Current positions:

| | |
|---|---|
| Keycloak | client partitioning **accepted** |
| Grafana | organisation model **promising**, unproven |
| OpenFGA | partitioning strategy **undecided** |
| Superset | **requires explicit assessment** |
| Airflow | **not accepted** as a tenant isolation boundary |

## What `catalogue` means now

Two different things used to share the word. They are now separate:

| | Meaning |
|---|---|
| `applications/catalogue/` | **A deployment grouping.** Applications that run in the `catalogue` namespace under the narrower `saas-fabric-catalogue` project, which grants no cluster-scoped resources. A privilege tier, not a statement about importance. |
| The client capability catalogue | **A SaaS Fabric product concept.** Capabilities that can be enabled for a client. It does not live in this repository. |

`catalogue` is no longer a synonym for "optional platform application". A
capability in the product catalogue may be *implemented by* a platform service:

```text
Capability: Observability
  provider: Grafana
  requires:  shared Grafana runtime
  provisioning:
    create client organisation
    create datasource
    configure permissions
    contribute navigation
```

Airflow shows the other direction — it may implement capabilities internally
without ever appearing as something a client selects.

```text
platform service  !=  client capability
client capability may be implemented by a platform service
```

## Environment enablement is separate again

Which services an environment deploys says nothing about what they are.
LucentRoot enables Grafana because dogfooding the platform is its job;
production does not enable it yet. Grafana is the same platform service in both.

See [`environments/README.md`](../environments/README.md).
