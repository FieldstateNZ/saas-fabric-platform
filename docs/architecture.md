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
                  ┌─────────┬───────┼────────┬─────────┐
                  │         │       │        │         │
                Envoy    Keycloak  OpenBao  CNPG    OTel
                Gateway      │       │        │         │
                  └─────────┴───────┼────────┴─────────┘
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
Argo CD installs Envoy Gateway   OpenTofu creates a client's routes
Argo CD installs Keycloak        OpenTofu creates a client's Keycloak realm
Argo CD installs CloudNativePG   OpenTofu creates a client's database
Argo CD installs OpenBao         OpenTofu creates a client's OpenBao namespace
Argo CD installs SaaS Fabric     SaaS Fabric manages client definitions
```

Routing follows the same shape as everything else. This repository owns the
Envoy Gateway runtime, the `GatewayClass`, the shared `Gateway` and the routes
for platform hostnames such as `fabric.<domain>` and `auth.<domain>`. Routes for
client hostnames such as `acme.<domain>` are created by OpenTofu in the client's
own namespace and attach to the same Gateway. There is deliberately only one
routing authority in the cluster; see
[`applications/core/platform-gateway`](../applications/core/platform-gateway/).

No resource may have competing ownership. If both an Argo CD Application and
OpenTofu could plausibly reconcile something, the boundary is wrong and must be
moved before the resource is created — not resolved afterwards by convention.

## Ownership contract

| Resource | Owner |
|---|---|
| AKS cluster | `saas-fabric-hosting` |
| Azure network | `saas-fabric-hosting` |
| Registry | `saas-fabric-hosting` |
| Argo CD installation | `saas-fabric-hosting` |
| Argo CD runtime configuration the platform depends on | `saas-fabric-platform` |
| Argo applications | `saas-fabric-platform` |
| Envoy Gateway runtime, `GatewayClass`, `Gateway` | `saas-fabric-platform` |
| Keycloak deployment | `saas-fabric-platform` |
| OpenBao deployment | `saas-fabric-platform` |
| CNPG operator | `saas-fabric-platform` |
| SaaS Fabric deployment | `saas-fabric-platform` |
| Client definition | `saas-fabric-clients` |
| Keycloak realm | client OpenTofu |
| OpenBao namespace | client OpenTofu |
| Client database | client OpenTofu |
| Client hostname / vhost / `HTTPRoute` | client OpenTofu |
| Client module enablement | `saas-fabric-clients` |

## How a change reaches a cluster

```text
feature branch → pull request → main → LucentRoot → tag → production branch
```

Each environment follows a Git ref, and a normal release moves a ref rather than
touching a cluster:

| Environment | Follows | Moves when |
|---|---|---|
| LucentRoot | `refs/heads/main` | a pull request merges |
| Production | `refs/heads/production` | that branch is fast-forwarded to a release tag's commit |

`kubectl` is used to bootstrap a cluster, for disaster recovery and for
deliberate break-glass work. It is not part of the release path. See
[releases.md](releases.md).

Everything Argo CD applies descends from one root Application, which points at
one environment directory:

```text
environments/<environment>/bootstrap        applied once, by kubectl
        ↓
Application platform-root                   → environments/<environment>
        ↓
argocd/runtime/                             → Argo CD behaviour the platform needs
applications/core/*/application.yaml        → Helm charts, Kustomize overlays
applications/catalogue/*/application.yaml
```

## Argo CD runtime contract

The platform depends on two things about Argo CD that are **not** defaults.
Both are owned by this repository, in
[`argocd/runtime`](../argocd/runtime/), applied at bootstrap and reconciled
thereafter. Neither is left as an assumption.

### Application health assessment

Out of the box, Argo CD reports a child `Application` resource as Healthy the
moment it exists, whatever state the application it names is actually in. Under
that default **sync waves in an app-of-apps sequence nothing**: every wave
proceeds immediately, and the ordering described below would be decorative.

[`argocd/runtime/application-health.yaml`](../argocd/runtime/application-health.yaml)
adds the custom health assessment for `argoproj.io/Application` that makes a
parent wait for its children's real health. This is what turns the sync waves in
the next section into actual ordering.

`scripts/check.py` fails the build if it is missing from either the bootstrap
set or the reconciled environment, so it cannot quietly go away.

### Argo CD version

Argo CD **2.13 or later**, for `oci://` Helm source repositories. Envoy Gateway
publishes its chart only to an OCI registry.

## Why app-of-apps rather than ApplicationSets

Given the health assessment above, an app-of-apps gives real sequencing: a child
`Application` counts as Healthy only when its application is, so wave `10` does
not start until wave `0` is genuinely running.

ApplicationSet-generated Applications are created by the ApplicationSet
controller, not by a parent's sync, so their sync waves do not sequence them
against each other at all — and no health assessment changes that. Ordering them
requires progressive syncs, which is a feature-gated beta.

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
| `-20` | Argo CD runtime configuration | must be active before any wave is ordered |
| `-10` | environment ConfigMap, catalogue `AppProject` | must exist before anything references them |
| `0` | Envoy Gateway, CloudNativePG operator | CRDs, control planes |
| `10` | platform `Gateway`, OpenTelemetry collector, OpenBao, Keycloak database | routing, data, secrets and telemetry foundations |
| `20` | Keycloak | needs its database Healthy and the Gateway to attach a route to |
| `30` | SaaS Fabric | needs routing, identity, secrets and telemetry |
| `40` | catalogue applications | optional, last |

This ordering is only real because of the
[Application health assessment](#application-health-assessment). Wave `-20` is
applied at bootstrap as well as reconciled here, so the assessment is active
before the root Application first syncs rather than racing it.

## Environment binding

Application definitions are shared across environments. Two things vary, and
they are deliberately kept apart because they are different kinds of fact:

| Varies | Where it is declared | Why there |
|---|---|---|
| which environment's config directory an Application reads | `environments/<env>/config/platform.yaml`, copied into every Application by [a Kustomize component](../environments/components/environment-config/kustomization.yaml) | the environment's identity is an environmental fact |
| which Git ref the environment follows | `environments/<env>/kustomization.yaml` and `environments/<env>/bootstrap/kustomization.yaml` | Argo binding, not runtime configuration |

The environment ConfigMap describes the environment — its name, domain, storage
class and replica profile — and is applied to the cluster, so a cluster can
always be asked what it is. It deliberately does **not** carry the Git ref: that
is promotion state, and mixing it into runtime configuration confuses "what this
environment is" with "what it is currently running".

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
| `platform-system` | Envoy Gateway, the platform `Gateway`, SaaS Fabric |
| `identity` | Keycloak and its database |
| `secrets` | OpenBao |
| `data-system` | CloudNativePG operator |
| `observability` | OpenTelemetry collector |
| `catalogue` | optional catalogue workloads |

Client namespaces (`client-acme`, `client-example`) are created by the client
layer and never appear here. `scripts/check.py` fails the build if one does.

## First milestone

> A clean Kubernetes cluster can converge into the complete SaaS Fabric platform
> **substrate** through Argo CD.

Stated precisely, because the distinction matters. Applying one bootstrap set to
an empty cluster and letting Argo CD reconcile proves:

- platform bootstrap from Git alone;
- dependency installation in the right order;
- GitOps reconciliation, with drift corrected by self-heal;
- platform configuration, including the routing, identity, secrets and data
  foundations;
- that every subsequent change can be made through Git.

It does **not** prove that SaaS Fabric is operational. No SaaS Fabric image is
published yet, so its Deployment ships with `replicas: 0`; the Application
reports Healthy because zero replicas is a correct reconciliation of what Git
says, not because the application is running.

An Argo Application reporting Healthy is a statement about reconciliation, not
about the workload behind it. SaaS Fabric becoming operational is a separate
milestone that starts when the application repository publishes a tag — see
[`applications/core/saas-fabric`](../applications/core/saas-fabric/).

## Known gaps

Recorded rather than hidden. None blocks a cluster from converging.

| Gap | Consequence | Where it would go |
|---|---|---|
| No certificate automation | The production Gateway listener references a TLS secret that must be injected by hand | a `cert-manager` core application; it has a genuine platform requirement once public hostnames are served |
| No telemetry backend | All three OTLP pipelines terminate in the `debug` exporter | an exporter in `environments/<env>/config/observability.yaml` |
| No OpenBao auto-unseal | A restarted OpenBao pod must be unsealed by an operator | a seal stanza in `environments/production/config/openbao.yaml`, against a key vault from `saas-fabric-hosting` |
| No database backups | The Keycloak `Cluster` has no `barmanObjectStore` | `applications/core/keycloak-database`, against storage from `saas-fabric-hosting` |
| SaaS Fabric has no image | The Deployment ships with `replicas: 0`, so the platform substrate converges but SaaS Fabric does not run — see [First milestone](#first-milestone) | a real tag in each environment overlay, once the application repository publishes one |
| Airflow DAG ownership undecided | Airflow cannot be adopted into the catalogue | [`applications/catalogue/airflow`](../applications/catalogue/airflow/) |
