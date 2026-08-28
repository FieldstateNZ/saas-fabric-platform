# Airflow — evaluated, not yet adopted

| | |
|---|---|
| Product | Apache Airflow |
| Upstream project | https://github.com/apache/airflow |
| Helm chart source | https://airflow.apache.org |
| Chart version (evaluated) | `1.22.0` |
| Application version | `3.2.2` |
| Licence | Apache-2.0 |
| Intended namespace | `catalogue` |
| Grouping | `catalogue` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Status | **not deployed** — no `application.yaml` in this directory |

## What it is

A **platform service**, not merely an optional extra — most likely a platform
orchestration one: provisioning workflows, platform automation, integration
workflows and scheduled operations.

```text
platform service      yes
deployment adopted    not yet
operator usage        intended
client partitioning   assessed, and rejected
client capability     not directly
```

**A shared Airflow installation is not a client isolation boundary**, and its
contract records that as `tenancy.status: rejected` rather than as an open
question. Workers execute DAG code with the installation's own credentials, so a
per-client partition inside one installation would be a convention rather than a
boundary. Airflow may well *implement* client capabilities without ever being
one a client selects — see
[docs/platform-services.md](../../../docs/platform-services.md#what-catalogue-means-now).

## Why it exists as a directory

Airflow is a candidate for a SaaS Fabric scheduled-workflow capability. This
directory records the evaluation so it does not have to be repeated.

Airflow is a good illustration of the core/catalogue rule:

```text
Bad:  Fabric requires Airflow because Airflow exists in this repo.
Good: Fabric can deploy Airflow when that capability is required.
```

Nothing in the platform depends on it.

## What blocks adoption

1. **Bundled PostgreSQL.** As with [Superset](../superset/), the chart ships a
   Bitnami PostgreSQL subchart that would compete with CloudNativePG for
   database ownership, and depends on an image catalogue whose distribution
   terms changed in 2025.
2. **DAG delivery is undecided.** Airflow needs DAGs from somewhere —
   git-sync, a baked image, or a shared volume. Whichever is chosen becomes a
   platform interface, and it is not clear yet whether DAGs would be platform
   content or client content. Adopting Airflow before answering that would
   create precisely the ownership ambiguity this repository is structured to
   prevent.

## What adoption requires

- an answer to the DAG ownership question, recorded in
  [docs/architecture.md](../../../docs/architecture.md);
- a CloudNativePG `Cluster` for the metadata database, with
  `postgresql.enabled: false` and `data.metadataConnection` pointing at it;
- an executor decision (`KubernetesExecutor` is the likely fit) and the RBAC it
  implies, which must stay within the `catalogue` namespace to remain compatible
  with the `saas-fabric-catalogue` project;
- `https://airflow.apache.org` added to that project's `sourceRepos`.
