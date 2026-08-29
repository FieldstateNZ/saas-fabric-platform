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

- the definition of the shared platform: the Envoy Gateway product routing
  layer, the Tailscale operator plane, CloudNativePG, OpenBao and its secret
  projection, Keycloak, the observability boundary, and SaaS Fabric's
  deployment;
- the Argo CD projects, root Application and child Applications that reconcile
  them, plus the Argo CD runtime behaviour the platform depends on;
- environment configuration for LucentRoot and production;
- the shared platform services, and how each may serve operators or clients.

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
Argo CD installs Tailscale       operator access only, never client traffic
Argo CD installs Keycloak        OpenTofu creates a client's Keycloak realm
Argo CD installs CloudNativePG   OpenTofu creates a client's database
Argo CD installs OpenBao         OpenTofu creates a client's OpenBao namespace
Argo CD installs SaaS Fabric     SaaS Fabric manages client definitions
```

No resource has competing ownership between the two. The full matrix is in
[docs/architecture.md](docs/architecture.md#ownership-contract).

## Two network planes

Not one routing layer with exceptions — two, with disjoint jobs.

```text
        Product plane                 Operator plane
             │                             │
       Envoy Gateway                    Tailscale
             │                             │
  client and platform HTTP        direct internal / admin
             │                             │
  fabric / applications           Argo CD / Perses /
  client hostnames                OpenBao UI (break-glass)
```

Envoy carries product and client traffic. Tailscale carries private operational
access and **never** client traffic. A hostname is on a plane for a stated
reason. Keycloak's OIDC endpoints are product-plane; its admin console is on
neither plane, because SaaS Fabric is the administrative control plane and
reaches Keycloak through its Admin API. Full contract in
[docs/architecture.md](docs/architecture.md#the-administrative-control-plane).

## Environments

| | LucentRoot | Production |
|---|---|---|
| Runtime | k3s | Azure Kubernetes Service |
| Follows | `refs/heads/main` | `refs/heads/production` |
| Moves when | a pull request merges | that branch is fast-forwarded to a release tag |
| Domain | `lucentroot.internal` | `platform.fieldstate.nz` |
| Storage | `local-path` | `managed-csi` |
| `catalogue` tier | enabled | not enabled |
| Operator plane | enabled | not enabled — no tailnet yet |

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
  core/           deployment tier: several namespaces, enumerated cluster scope
  catalogue/      deployment tier: one namespace, no cluster scope
environments/     the thin per-environment differences
docs/             architecture, bootstrap, releases, contributing
scripts/          render and validate everything, offline
```

Application definitions are shared across environments. Two things vary, kept
deliberately apart: the environment's identity, which is an environmental fact
declared in `environments/<environment>/config/platform.yaml`; and the Git ref it
follows, which is Argo binding and lives in that environment's kustomizations.

## Platform services

The platform owns shared service runtimes. A service may be required by SaaS
Fabric, used by platform operators, partitionable for clients, offered as a
client capability — or any combination. **These are independent**, and each is
declared in a `platform-service.yaml` beside the application.

| Service | Required | Operator | Client partitioning | Tenancy | Deployed |
|---|---|---|---|---|---|
| [Envoy Gateway](applications/core/envoy-gateway/) | yes | no | logical — routes | accepted | yes |
| [CloudNativePG](applications/core/cloudnative-pg/) | yes | no | strong — `Cluster` | accepted | yes |
| [External Secrets](applications/core/external-secrets/) | yes | no | logical — store | accepted | yes |
| [OpenBao](applications/core/openbao/) | yes | yes | strong — path prefix | accepted | yes |
| [Keycloak](applications/core/keycloak/) | yes | yes | strong — realm | accepted | yes |
| [Perses](applications/catalogue/perses/) | no | **yes** | unknown — project proposed | candidate | yes |
| [OpenFGA](applications/core/openfga/) | **yes** | yes | unknown | unresolved | **planned** |
| [Superset](applications/catalogue/superset/) | no | yes | unknown | unresolved | assessed |
| [Airflow](applications/catalogue/airflow/) | no | yes | none | rejected | assessed |
| [OpenTelemetry](applications/core/observability/) | yes | no | none | not-applicable | yes |
| [SaaS Fabric](applications/core/saas-fabric/) | yes | no | none | not-applicable | yes |
| [Tailscale](applications/core/tailscale/) | no | yes | none | not-applicable | yes |

A boundary strength is only named once it has been established: `unknown` means
the mechanism is proposed or undecided, and `check.py` refuses a contract that
claims `logical` or `strong` before its tenancy assessment says `accepted`.

Keycloak is the reference shape — one runtime, one platform administrative
context, one partition per client — and the client-facing rule generalises from
it:

> Platform owns the shared runtime. Client provisioning owns the client-scoped
> partitions inside it.

`core/` and `catalogue/` are **deployment tiers**, not classifications:
`catalogue` gets one namespace and no cluster-scoped resources. Perses lives
there because SaaS Fabric does not require it, which says nothing about whether
operators depend on it — they do.

See [docs/platform-services.md](docs/platform-services.md) for the full model,
the register, and the isolation checklist a service must pass before it may
claim client partitioning.

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
- the two exposure planes kept separate — product traffic on Gateway API routes
  attached to a listener that exists from a namespace the Gateway admits,
  operator traffic on Tailscale `Ingress` resources, and no third routing
  authority;
- no administrative surface reachable from the product plane, and no
  product-plane route to a service whose contract says the operator plane is the
  only thing protecting it;
- the non-default Argo CD behaviour the platform depends on present in both the
  bootstrap set and the reconciled environment, so neither wave ordering nor
  operator-plane access can quietly stop working;
- the platform secret store bounded to platform namespaces, so a client
  namespace cannot reach platform secrets through it;
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
| [platform-services.md](docs/platform-services.md) | the service capability model, the register, and the tenancy checklist |
| [telemetry-backend.md](docs/telemetry-backend.md) | choosing what stores telemetry: the gates, the candidates, and one recommendation with its floor and ceiling |
| [adding-an-application.md](docs/adding-an-application.md) | how to classify, place and add a service |
| [migrating-lucentroot.md](docs/migrating-lucentroot.md) | rebuilding LucentRoot onto this repository, and what that costs |

## Licence

Apache-2.0. See [LICENSE](LICENSE).
