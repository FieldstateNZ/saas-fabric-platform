# Architecture

## The stack

```text
Hosting
   ↓
Kubernetes
   ↓
Argo CD
   ↓
Shared Platform
   ↓
SaaS Fabric
   ↓
Client reconciliation
```

Each layer is owned by exactly one repository, and each hands a completed
substrate to the next.

```text
                     GitHub
                       │
          ┌────────────┴────────────┐
          │                         │
saas-fabric-hosting        saas-fabric-platform
          │                         │
       OpenTofu                   Argo CD
          │                         │
          ▼                         ▼
      AKS / k3s              Shared Platform
                                    │
                       ┌────────────┼────────────┐
                       │            │            │
                    Keycloak     OpenBao      CNPG
                       │            │            │
                       └────────────┼────────────┘
                                    │
                                    ▼
                              SaaS Fabric
                                    │
                                    ▼
                         saas-fabric-clients
                                    │
                                    ▼
                               OpenTofu
                                    │
                                    ▼
                               Clients
```

## The ownership rule

> Platform application lifecycle is owned by Argo CD.
> Client resource lifecycle is owned by SaaS Fabric / OpenTofu.

Stated as pairs:

```text
Argo CD installs Keycloak        OpenTofu creates a client's Keycloak realm
Argo CD installs CloudNativePG   OpenTofu creates a client's database
Argo CD installs OpenBao         OpenTofu creates a client's OpenBao namespace
Argo CD installs SaaS Fabric     SaaS Fabric manages client definitions
```

No resource may have competing ownership. If both an Argo CD Application and
OpenTofu could plausibly reconcile something, the boundary is wrong and must be
moved before the resource is created — not resolved afterwards by convention.

## Ownership contract

| Resource | Owner |
|---|---|
| AKS cluster | `saas-fabric-hosting` |
| Azure network | `saas-fabric-hosting` |
| Registry | `saas-fabric-hosting` |
| Argo CD bootstrap | hosting / bootstrap |
| Argo applications | `saas-fabric-platform` |
| Keycloak deployment | `saas-fabric-platform` |
| OpenBao deployment | `saas-fabric-platform` |
| CNPG operator | `saas-fabric-platform` |
| SaaS Fabric deployment | `saas-fabric-platform` |
| Client definition | `saas-fabric-clients` |
| Keycloak realm | client OpenTofu |
| OpenBao namespace | client OpenTofu |
| Client database | client OpenTofu |
| Client hostname / vhost | client OpenTofu |
| Client module enablement | `saas-fabric-clients` |

## How a change reaches a cluster

```text
feature branch → pull request → main → LucentRoot → release tag → production
```

Everything Argo CD applies descends from one root Application, which points at
one environment directory:

```text
environments/<environment>/bootstrap        applied once, by kubectl
        ↓
Application platform-root                   → environments/<environment>
        ↓
applications/core/*/application.yaml        → Helm charts, Kustomize overlays
applications/catalogue/*/application.yaml
```

## Why app-of-apps rather than ApplicationSets

Argo CD orders resources inside a single Application by sync wave, and an
`Application` resource is Healthy only when the application it names is Healthy.
An app-of-apps therefore gives real sequencing: wave `10` does not start until
everything in wave `0` is actually running.

ApplicationSet-generated Applications are created by the ApplicationSet
controller, not by a parent's sync, so their sync waves do not sequence them
against each other. Ordering them requires progressive syncs, which is a
feature-gated beta.

The platform has hard ordering requirements — the CloudNativePG CRDs must exist
before a `Cluster`, and that `Cluster` must be running before Keycloak starts —
so it uses app-of-apps. Per-environment repetition is removed by Kustomize
replacements instead (see [Environment binding](#environment-binding)), which
leaves nothing for an ApplicationSet to deduplicate. See
[`argocd/applicationsets/README.md`](../argocd/applicationsets/README.md).

## Dependency ordering

Waves are kept few and meaningful:

| Wave | Contents | Why |
|---|---|---|
| `-10` | environment ConfigMap, catalogue `AppProject` | must exist before anything references them |
| `0` | ingress-nginx, CloudNativePG operator | CRDs and the cluster edge |
| `10` | OpenTelemetry collector, OpenBao, Keycloak database | data, secrets and telemetry foundations |
| `20` | Keycloak | needs its database Healthy |
| `30` | SaaS Fabric | needs identity, secrets and telemetry |
| `40` | catalogue applications | optional, last |

## Environment binding

Application definitions are shared across environments. Exactly two facts vary:
which environment's config directory an Application reads, and which revision of
this repository it tracks. Both are declared once per environment in
`environments/<environment>/config/platform.yaml` and copied into every
Application by a Kustomize component
([`environments/components/environment-config`](../environments/components/environment-config/kustomization.yaml)).

That ConfigMap is also applied to the cluster, so a cluster can always be asked
which environment it is and which revision it is meant to be running.

Everything else that differs between environments is a per-application values
file under `environments/<environment>/config/`, read directly from Git by Argo
CD as a second Helm values source. An environment with nothing to say about an
application simply has no file for it.

## Privilege boundaries

The app-of-apps model confers substantial cluster privilege: whoever can change
this repository can change what runs in the cluster. Three things narrow that.

1. **The platform project is not self-managed.** `saas-fabric-platform` is
   applied by an administrator at bootstrap and is not reconciled by the root
   Application, so a change here cannot widen the privileges of the thing
   applying it.
2. **Cluster-scoped kinds are enumerated, not wildcarded.** A new chart that
   wants a `ClusterRole` beyond the listed kinds fails to sync rather than
   quietly acquiring it.
3. **The catalogue project is strictly narrower.** One namespace, no
   cluster-scoped resources at all.

## Deletion semantics

Automated sync with `prune: true` and `selfHeal: true` is the default. Two
deliberate exceptions:

- **The root Application has no cascade finalizer.** Deleting it detaches Argo
  CD from the platform; it does not delete the platform.
- **Stateful resources are pruned only on purpose.** The Keycloak database
  `Cluster` carries `Prune=false,Delete=false`, and OpenBao's volumes are
  retained by `persistentVolumeClaimRetentionPolicy: Retain`. These are listed
  in each application's README under *Resources unsafe to prune*.

Child Applications *do* carry the cascade finalizer, so removing an application
from Git removes its resources. That is the intended behaviour for stateless
components, and the two stateful exceptions above are protected individually
rather than by disabling pruning across the platform.

## Namespaces

| Namespace | Contents |
|---|---|
| `argocd` | Argo CD, projects, Applications, environment ConfigMap |
| `platform-system` | ingress controller, SaaS Fabric |
| `identity` | Keycloak and its database |
| `secrets` | OpenBao |
| `data-system` | CloudNativePG operator |
| `observability` | OpenTelemetry collector |
| `catalogue` | optional catalogue workloads |

Client namespaces (`client-acme`, `client-example`) are created by the client
layer and never appear here. `scripts/check.py` fails the build if one does.

## Known gaps

Recorded rather than hidden. None blocks a cluster from converging.

| Gap | Consequence | Where it would go |
|---|---|---|
| No certificate automation | Platform Ingresses reference TLS secrets that must be injected by hand | a `cert-manager` core application; it has a genuine platform requirement once public hostnames are served |
| No telemetry backend | All three OTLP pipelines terminate in the `debug` exporter | an exporter in `environments/<env>/config/observability.yaml` |
| No OpenBao auto-unseal | A restarted OpenBao pod must be unsealed by an operator | a seal stanza in `environments/production/config/openbao.yaml`, against a key vault from `saas-fabric-hosting` |
| No database backups | The Keycloak `Cluster` has no `barmanObjectStore` | `applications/core/keycloak-database`, against storage from `saas-fabric-hosting` |
| SaaS Fabric has no image | The Deployment ships with `replicas: 0` | a real tag in each environment overlay, once the application repository publishes one |
| Airflow DAG ownership undecided | Airflow cannot be adopted into the catalogue | [`applications/catalogue/airflow`](../applications/catalogue/airflow/) |
