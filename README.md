# saas-fabric-platform

The declarative GitOps definition of the shared Kubernetes platform that SaaS
Fabric runs on. Argo CD treats this repository as the source of truth for
platform workloads.

```bash
kubectl apply --server-side --field-manager=saas-fabric-platform \
  -k environments/lucentroot/bootstrap
```

That is the whole bootstrap. Everything after it happens through Git, including
promoting a release to production.

## What this repository owns

- the definition of the shared platform: the Envoy Gateway routing layer,
  CloudNativePG, OpenBao, Keycloak, the observability boundary, and SaaS
  Fabric's deployment;
- the Argo CD projects, root Application and child Applications that reconcile
  them, plus the Argo CD runtime behaviour the platform depends on;
- environment configuration for LucentRoot and production;
- the optional catalogue of platform capabilities.

## What it does not own

It does not provision cloud infrastructure, and it does not define clients.

- no AKS cluster, Azure networking, managed identities, DNS zones or container
  registry — those belong to **`saas-fabric-hosting`**;
- no client definitions, client feature configuration or client secrets — those
  belong to **`saas-fabric-clients`**;
- no OpenTofu, and no client provisioning of any kind;
- no application source code;
- no plaintext secrets, ever.

## The ownership rule

```text
saas-fabric-hosting
    ↓  creates external infrastructure — AKS / networking / identities / registry / DNS
saas-fabric-platform
    ↓  Argo CD reconciles shared Kubernetes platform services
saas-fabric-clients
    ↓  SaaS Fabric / OpenTofu reconciles individual client resources
```

> **Argo CD owns platform applications. OpenTofu owns infrastructure and
> client-scoped resources.**

```text
Argo CD installs Envoy Gateway   OpenTofu creates a client's routes
Argo CD installs Keycloak        OpenTofu creates a client's Keycloak realm
Argo CD installs CloudNativePG   OpenTofu creates a client's database
Argo CD installs OpenBao         OpenTofu creates a client's OpenBao namespace
Argo CD installs SaaS Fabric     SaaS Fabric manages client definitions
```

No resource has competing ownership between the two. The full matrix is in
[docs/architecture.md](docs/architecture.md#ownership-contract).

## Environments

| | LucentRoot | Production |
|---|---|---|
| Runtime | k3s | Azure Kubernetes Service |
| Follows | `refs/heads/main` | `refs/heads/production` |
| Moves when | a pull request merges | that branch is fast-forwarded to a release tag |
| Domain | `lucentroot.internal` | `platform.fieldstate.nz` |
| Storage | `local-path` | `managed-csi` |
| Catalogue | enabled | not enabled |

LucentRoot is where platform changes are exercised. It runs the same application
topology as production with lower availability — one replica instead of three,
local storage, smaller resource limits.

`refs/heads/production` is not a development stream. It only ever moves to a
commit carrying a release tag, so what production runs is always a composition
someone explicitly chose — and promoting one is a Git operation, never a change
made against the cluster.

## How a change reaches production

```text
feature branch → pull request → main → LucentRoot → validation
      → tag vX.Y.Z → fast-forward refs/heads/production → Argo CD reconciles
```

A merge to `main` reconciles LucentRoot automatically. A release is a separate,
explicit decision: not every commit becomes one. `kubectl` is for initial
bootstrap, disaster recovery and deliberate break-glass work — not for
releases. See [docs/releases.md](docs/releases.md).

## Layout

```text
bootstrap/        the project and root Application; three files, one command
argocd/           Argo CD projects and the runtime behaviour the platform needs
applications/
  core/           what SaaS Fabric requires in order to operate
  catalogue/      optional capabilities SaaS Fabric can offer
environments/     the thin per-environment differences
docs/             architecture, bootstrap, releases, contributing
scripts/          render and validate everything, offline
```

Application definitions are shared across environments. Two things vary, kept
deliberately apart: the environment's identity, which is an environmental fact
declared in `environments/<environment>/config/platform.yaml`; and the Git ref it
follows, which is Argo binding and lives in that environment's kustomizations.

## Core versus catalogue

> Does SaaS Fabric itself require this service in order to operate?

If yes, it may be core. If no, it belongs in the catalogue.

| Core | Catalogue |
|---|---|
| [Envoy Gateway](applications/core/envoy-gateway/) + [the platform Gateway](applications/core/platform-gateway/) | [Grafana](applications/catalogue/grafana/) |
| [CloudNativePG](applications/core/cloudnative-pg/) | |
| [OpenBao](applications/core/openbao/) | |
| [Keycloak](applications/core/keycloak/) + [its database](applications/core/keycloak-database/) | |
| [OpenTelemetry collector](applications/core/observability/) | |
| [SaaS Fabric](applications/core/saas-fabric/) | |

A component is not core because it is useful. Grafana is useful and is
catalogue; the platform's observability contract is OTLP, not a dashboard.

## Validation

```bash
./scripts/validate.sh
```

Lints YAML, renders every Kustomize build and Helm chart exactly as Argo CD
will, then validates the output against real Kubernetes schemas — including Argo
CD and CloudNativePG custom resources, not just built-in kinds.

It then checks the invariants a schema cannot express:

- no plaintext secret material, in the sources or in anything rendered from
  them;
- no duplicate Kubernetes resource within an environment, because two
  Applications writing the same object is competing ownership;
- exactly one routing authority — Gateway API, never an `Ingress` — with every
  route attached to a listener that exists, from a namespace the Gateway admits;
- the non-default Argo CD behaviour the platform depends on present in both the
  bootstrap set and the reconciled environment, so wave ordering cannot quietly
  stop working;
- every chart repository and destination namespace allowed by the Application's
  own `AppProject`;
- every chart version pinned exactly, never a range;
- no client-scoped resource anywhere in a platform environment;
- every in-cluster service reference resolving to something this repository
  actually deploys — cross-application addressing is configuration, so nothing
  else catches a typo in it;
- telemetry pipelines referencing only components that exist, since a bad
  collector config renders and validates perfectly and then crash-loops;
- every application directory carrying its provenance and licence.

No cluster required, and none should be. CI runs the same script, so a pull
request that renders invalid manifests cannot merge.

## Documentation

| | |
|---|---|
| [architecture.md](docs/architecture.md) | layers, ownership, the Argo CD runtime contract, dependency ordering, privilege boundaries, the first milestone, known gaps |
| [bootstrap.md](docs/bootstrap.md) | k3s / LucentRoot and AKS / production, step by step |
| [releases.md](docs/releases.md) | cutting a release, promoting production by moving a Git ref, rolling back |
| [adding-an-application.md](docs/adding-an-application.md) | core versus catalogue, and how to add either |

## Licence

Apache-2.0. See [LICENSE](LICENSE).
