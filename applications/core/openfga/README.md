# OpenFGA — required, not yet deployed

| | |
|---|---|
| Product | OpenFGA |
| Upstream project | https://github.com/openfga/openfga |
| Helm chart source | https://openfga.github.io/helm-charts |
| Licence | Apache-2.0 |
| Intended namespace | `platform-system` |
| Grouping | `core` — it will need the platform project when adopted |
| Status | **not deployed** — no `application.yaml` in this directory |

## Why it exists as a directory

Because leaving it out would misrepresent the platform.

SaaS Fabric's intended runtime needs fine-grained authorization — the question
*may this subject act on this object* — and neither Keycloak nor OpenBao answers
it. Keycloak authenticates and issues coarse roles; OpenBao decides which
secrets a workload may read. Neither models relationships between clients,
users, modules and records.

Its [contract](platform-service.yaml) therefore says:

```yaml
required: true
deployment: planned
```

That pairing is deliberate and slightly uncomfortable, which is the point. If
OpenFGA were recorded as optional simply because it is absent, the platform
would look complete while missing a service SaaS Fabric depends on. Instead it
reads as a gap, and appears on the known-gaps list in
[docs/architecture.md](../../../docs/architecture.md#known-gaps).

## Ownership, when it is adopted

The same shape as every other shared service — see
[docs/platform-services.md](../../../docs/platform-services.md).

| | Owner |
|---|---|
| Runtime, deployment, upgrades | this repository, via Argo CD |
| Platform authorization model and state | this repository, where the platform itself needs it |
| A client's authorization partition, model and tuples | `saas-fabric-clients` |

## What is undecided

The partitioning strategy, and it is a genuine architecture decision rather than
an implementation detail:

- **Store per client, or one shared store with client-namespaced types?**
  Separate stores give a real boundary and multiply lifecycle management. A
  shared store keeps provisioning simple and makes every isolation guarantee
  depend on the correctness of the model.
- **How does a client's authorization model relate to its Keycloak realm?** A
  subject in OpenFGA has to correspond to an identity in a realm, and nothing
  currently defines that mapping.
- **Who writes the model?** The platform owns the runtime; the shape of a
  client's relations may belong to SaaS Fabric rather than to client
  provisioning.

Until those are settled, `tenancy.status` is `candidate` and the contract claims
no client provisioning.

## What adoption requires

1. Settling the partitioning strategy above.
2. A pinned chart version, and the provenance table this README currently cannot
   complete.
3. A CloudNativePG database, following the rule the platform already applies to
   Keycloak: **CloudNativePG owns PostgreSQL, the service consumes it.** OpenFGA
   supports an external datastore, so this should not present Superset's and
   Airflow's bundled-database problem.
4. A decision on whether it needs an operator-plane surface at all.
