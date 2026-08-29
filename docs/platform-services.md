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

Perses is where it broke. SaaS Fabric runs without Perses, so Perses was
catalogue, so Perses read as peripheral. All three of these are true at once:

- SaaS Fabric does not require it;
- platform operators use it daily, and it is where operational visibility lives;
- its project model is a plausible client partition.

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
| `controlPlane` | does SaaS Fabric administer it, and is its own admin UI published? |

Two inferences that look reasonable and are not:

- **Optional does not mean peripheral.** Perses is optional and
  operator-critical.
- **Partitionable does not mean offered.** Keycloak partitions strongly per
  client, and no client selects it — every client has identity by virtue of
  being a client.

### `operatorUsage` does not mean "use its UI"

A fifth property, separate on purpose. `operatorUsage: true` says operators use
the service. It does not say they should use *that service's own console*.

> **SaaS Fabric is the administrative control plane for the services it
> manages.** A shared service may expose the runtime endpoints applications
> need; it should not expose its upstream administrative UI as part of normal
> operation.

The distinction that makes this workable: **some upstream UIs are themselves the
capability; others are merely vendor administration surfaces.**

| Service | `managed` | `upstreamAdminSurface` | Why |
|---|---|---|---|
| Keycloak | true | `not-exposed` | Fabric owns identity management; the console is vendor administration |
| OpenFGA | true | `none` | API-only upstream, nothing to withhold |
| Perses | partial | `exposed` | exploration **is** the capability operators want |
| OpenBao | partial | `break-glass` | published for diagnostics, outside the normal contract |

Perses is the case that stops this becoming a blanket ban. Hiding its UI would
remove the thing operators came for. Hiding Keycloak's removes a login screen
they should not have needed.

Perses sharpens the distinction rather than blurring it. Most of what an
operator would once have done in a dashboard console is now Git's: definitions
are committed, and the instance is deployed read-only. What stays published is
exploration — following a metric into a log line into a trace — which is exactly
the part no control plane replaces.

`not-exposed` names the Kubernetes Services that would front the console in
`adminBackends`, and `check.py` proves no `Ingress` publishes them — so the rule
survives someone adding four convenient lines of YAML later. See
[architecture.md](architecture.md#the-administrative-control-plane).

## The reference pattern

Keycloak is the model every other shared service is measured against, because
its boundary is the one upstream actually enforces.

```text
Keycloak runtime            platform
  ├── master / admin        platform
  ├── Acme realm            client
  └── Contoso realm         client
```

Perses is the same shape, one step less proven:

```text
Perses runtime              platform
  ├── platform project      platform
  ├── Acme project          client   ← intended, not built
  └── Contoso project       client   ← intended, not built
```

The pattern generalises to: **one runtime, one platform administrative context,
one partition per client** — and it applies only where the upstream project
supplies a real isolation boundary.

## Who owns what

| Service | Platform owns | Client provisioning owns |
|---|---|---|
| Keycloak | deployment, master/admin | realm |
| Perses | deployment, platform project and dashboards | client project *(intended)* |
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
controlPlane:
  managed: true              # true | partial | false
  upstreamAdminSurface: not-exposed   # none | not-exposed | break-glass | exposed
  adminBackends: [keycloak-http]      # required when not-exposed
exposure:                    # optional; declared where it is a constraint
  plane: operator            # operator | product | both
  backends:                  # required when operator
    - name: perses           # a backendRef resolves to (namespace, name),
      namespace: catalogue   # so both are named
  rationale: |               # required when operator
    ...
clientPartitioning:
  mode: strong               # unknown | none | logical | strong
  unit: realm                # named only once the mechanism is settled
  provisioning: supported    # supported | unsupported
  owner: saas-fabric-clients
clientCapability:
  available: false
tenancy:
  status: accepted           # accepted | candidate | unresolved | rejected | not-applicable
  rationale: |
    ...
```

### `exposure` is a constraint, not a description

Most services need no `exposure` block: which plane they answer on is visible in
the routes that exist, and a route is reviewed like anything else. It is
declared only where the plane is *load-bearing* — where a service is safe
because of where it sits rather than because of what it enforces.

Perses is the case. It runs unauthenticated, which is coherent while the
operator plane is the whole boundary and stops being coherent the moment it is
not. A client-reachable route would be a change of security posture wearing the
clothes of a routing change.

```text
tailnet / operator plane   →   unauthenticated Perses   →   read-only
```

Remove the first term and the other two stop reassuring anybody.

So `plane: operator` names, in `backends`, the Services validation must prove
stay off the product plane — for exactly the reason `not-exposed` names
`adminBackends`. A claim about a cluster needs something to be checked against,
and `check_operator_only_services` fails the build on a route that could reach
one of them from the product plane. The namespace happening not to carry
`gateway-access` is what stops it today; this repository has twice been wrong
about an absent label being a guarantee.

The check resolves two things the way Kubernetes resolves them rather than the
way a string comparison would, because an invariant that encodes an
architectural truth should not rest on a naming convention:

| | |
|---|---|
| **Identity** | a `backendRef` addresses `(namespace, name)`, and its namespace defaults to the route's own. Matching on the name alone would depend on nobody reusing a Service name in another namespace, and would misfire on a cross-namespace `backendRef`. A ref to any other kind or group is not a Service and is not matched against one |
| **Plane** | what is refused is the *product* plane, not routing. The listener the route names decides it, or — when it names none — the grant its namespace carries. An operator-plane route to the same Service is permitted, which is the whole point |

`rationale` is required for the same reason a tenancy position is: a constraint
nobody can read the reason for is one somebody will lift.

Lifting it is not editing three lines. It is building the authentication,
authorization and tenancy the constraint is standing in for, and then changing
this block along with them.

### `mode` is a claim, and tenancy licenses it

`mode` states the *strength* of a boundary, so it cannot be asserted ahead of
the assessment that establishes it. An earlier version of this model allowed
`mode: strong` beside `status: candidate`, which read as *"we know this is a
strong tenant boundary, but we have not established whether it is a boundary"* —
and undermined the one rule the model exists to enforce.

The two fields are therefore a state machine:

| `tenancy.status` | means | valid `mode` |
|---|---|---|
| `accepted` | we rely on this as a tenant boundary | `logical` or `strong` |
| `candidate` | a plausible upstream mechanism, assessment incomplete | `unknown` |
| `unresolved` | partitioning intended, mechanism not settled | `unknown` |
| `rejected` | assessed, and unsuitable as a boundary | `none` |
| `not-applicable` | partitioning is not part of this service's role | `none` |

A settled mechanism is named in `unit`; a proposed one in `candidateUnit`, which
`candidate` requires — a candidate is a specific proposal, not a general
intention.

**A candidate unit is a grouping until it is a boundary, and the words are not
interchangeable.** Perses' `candidateUnit: project` is the live example, and the
one most likely to be misread:

```text
Perses project    =  a namespace for observability resources
Perses project    ≠  a Fabric tenant authority
Tenant isolation  =  enforced at the telemetry datasource, by a mechanism
                     that does not exist yet
```

One project per client would be a filing arrangement. `mode: unknown` is the
model saying exactly that, and it is why `candidateUnit` is a separate field
from `unit` rather than the same field used optimistically. Anyone reading a
`candidateUnit` as an isolation model has read the field the state machine above
exists to prevent.

The last two states are deliberately distinct. *"We assessed this and it is not
a boundary"* and *"this service does not partition clients at all"* are
different facts, and collapsing them loses the assessment.

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
| Perses | no | yes | unknown — project proposed | **candidate** | yes |
| OpenFGA | **yes** | yes | unknown | unresolved | **planned** |
| Superset | no | yes | unknown | unresolved | assessed |
| Airflow | no | yes | none | rejected | assessed |
| OpenTelemetry | yes | no | none | not-applicable | yes |
| SaaS Fabric | yes | no | none | not-applicable | yes |
| Tailscale | no | yes | none | not-applicable | yes |

Read the bottom four rows carefully, because they are four different facts.
Airflow was assessed and found unsuitable. Tailscale deliberately carries no
client traffic at all. SaaS Fabric owns the definition of a client, so asking
whether it is a tenant boundary inverts the relationship. OpenTelemetry is a
transport boundary with no per-client object inside it — true of its role today,
and a real partitioning question the moment per-client telemetry is wanted.

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
| Keycloak, OpenBao, CloudNativePG, Envoy Gateway | **accepted** — relied on as boundaries |
| Perses | project model **promising**, unproven — and platform telemetry carries no per-client attribute to scope a datasource by |
| OpenFGA | partitioning strategy **undecided** |
| Superset | **requires explicit assessment** |
| Airflow | assessed and **rejected** as a tenant isolation boundary |
| Tailscale, SaaS Fabric, OpenTelemetry | **not applicable** — partitioning is not part of their role |

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
  provider: Perses
  requires:  shared Perses runtime
  provisioning:
    create client project
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
LucentRoot enables Perses because dogfooding the platform is its job;
production does not enable it yet. Perses is the same platform service in both.

See [`environments/README.md`](../environments/README.md).
