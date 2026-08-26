# Catalogue applications

Workloads SaaS Fabric can offer as platform capabilities, but which SaaS Fabric
does not need in order to run.

## The test

> Does SaaS Fabric itself require this service in order to operate?

If **yes**, it may be core. If **no**, it belongs here. A component is not core
because it is useful, and it is not core because it happens to be in this
repository.

## Current contents

| Application | Status | Notes |
|---|---|---|
| [Grafana](grafana/) | deployed | reads platform telemetry; enabled in LucentRoot |
| [Superset](superset/) | evaluated, not adopted | bundled Bitnami PostgreSQL competes with CloudNativePG |
| [Airflow](airflow/) | evaluated, not adopted | same, plus DAG ownership is undecided |

Superset and Airflow are directories with a documented assessment and no
`application.yaml`. That is deliberate: the evaluation is worth keeping, and
adding an Application would mean shipping a database this platform does not
intend to own.

## Enablement

The catalogue is enabled per environment. An environment that includes
`../../applications/catalogue` in its kustomization gets every catalogue
application; one that omits it gets none, and is still a complete platform.

| Environment | Catalogue |
|---|---|
| LucentRoot | enabled |
| Production | not enabled |

Catalogue applications run in the `catalogue` namespace under the
`saas-fabric-catalogue` project, which grants no cluster-scoped resources at
all. A catalogue chart that demands cluster scope is a signal to re-examine
whether it belongs here.

## Where this is going

The catalogue is expected to become part of SaaS Fabric's product capability
model: SaaS Fabric decides which capabilities a client gets, and this directory
becomes the platform-side definition of what those capabilities are. Until then
it is a plain per-environment opt-in.
