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
own namespace and attach to the same Gateway. See
[Exposure planes](#exposure-planes) and
[`applications/core/platform-gateway`](../applications/core/platform-gateway/).

No resource may have competing ownership. If both an Argo CD Application and
OpenTofu could plausibly reconcile something, the boundary is wrong and must be
moved before the resource is created — not resolved afterwards by convention.

## Platform services

The ownership rule above says who owns a *resource*. This says what a *service*
is, which the repository used to answer far too narrowly.

Applications were classified by one question — does SaaS Fabric require this to
operate? — into `core` and `catalogue`. That conflated two unrelated things:
whether SaaS Fabric depends on a service, and whether the service can offer
client-scoped capability. Perses is optional, operator-critical, and plausibly
client-partitionable, and one binary could not say so.

Four independent properties now, declared per service in a
`platform-service.yaml` beside the application:

| Property | Asks |
|---|---|
| `required` | does SaaS Fabric fail to operate without it? |
| `operatorUsage` | do the people running the platform use it? |
| `clientPartitioning` | can one runtime hold separated client partitions? |
| `clientCapability` | is it offered to a client as a selectable capability? |

Keycloak is the reference shape, and generalises to *one runtime, one platform
administrative context, one partition per client*:

```text
Keycloak runtime            platform
  ├── master / admin        platform
  ├── Acme realm            client
  └── Contoso realm         client
```

Which makes the client-facing half of the ownership rule a general statement
rather than a list of special cases:

> **Platform owns the shared runtime. Client provisioning owns the client-scoped
> partitions inside it.**

Two consequences worth stating explicitly, because both were previously implied
the other way:

- **`core` and `catalogue` are deployment groupings** — a privilege tier and a
  namespace — not architectural identity. `catalogue` grants no cluster-scoped
  resources; that is the entire distinction.
- **Required versus optional is a deployment dependency property.** It does not
  determine whether operators depend on a service, or whether clients can.

The full model, the register of every service and its classification, and the
isolation checklist a service must pass before claiming client partitioning are
in [platform-services.md](platform-services.md).

## Ownership contract

| Resource | Owner |
|---|---|
| AKS cluster | `saas-fabric-hosting` |
| Azure network | `saas-fabric-hosting` |
| Registry | `saas-fabric-hosting` |
| Argo CD installation | `saas-fabric-hosting` |
| Argo CD runtime configuration the platform depends on | `saas-fabric-platform` |
| Operator-plane access to Argo CD | `saas-fabric-platform` |
| Argo applications | `saas-fabric-platform` |
| Envoy Gateway runtime, `GatewayClass`, `Gateway` | `saas-fabric-platform` |
| Tailscale operator and operator-plane access | `saas-fabric-platform` |
| Tailnet ACL policy, tag ownership, OAuth client | outside Kubernetes; tailnet administration |
| Keycloak deployment | `saas-fabric-platform` |
| OpenBao deployment | `saas-fabric-platform` |
| Secret projection into workloads | `saas-fabric-platform` |
| Secret *values* | OpenBao, never Git |
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

## Exposure planes

The platform has **two** network planes with disjoint responsibilities. This is
not one routing authority and a workaround; it is a deliberate split, and a
hostname belongs to a plane for a stated reason.

```text
                        Cluster
                            │
             ┌──────────────┴──────────────┐
             │                             │
        Product plane                 Operator plane
             │                             │
       Envoy Gateway                    Tailscale
             │                             │
  client and platform HTTP        direct internal / admin
             │                             │
  fabric / applications           Argo CD / Perses /
  client hostnames                OpenBao UI (break-glass)
```

| | Product plane | Operator plane |
|---|---|---|
| Implemented by | Envoy Gateway | Tailscale operator |
| Kubernetes resource | `HTTPRoute` on the platform `Gateway` | `Ingress`, `ingressClassName: tailscale` |
| Reachable from | the internet, or wherever DNS points | the tailnet only |
| Carries | product and client traffic | administration and operations |
| Client traffic | **yes** | **never** |
| Owned by | this repository (platform hosts), client OpenTofu (client hosts) | this repository only |

There is no third routing authority. `scripts/check.py` fails the build on any
`Ingress` that is not `tailscale`, and on any `IngressClass` other than the one
the Tailscale operator owns.

### Which plane a service belongs on

| Service | Product | Operator |
|---|---|---|
| SaaS Fabric runtime endpoint | ✅ | — |
| Keycloak OIDC endpoints (`/realms`, `/resources`, `/.well-known`) | ✅ | — |
| Keycloak administration (`/admin`) | ❌ | ❌ — see below |
| OpenBao API used by workloads | cluster-local, neither plane | — |
| OpenBao operator UI and API | ❌ | ✅, break-glass |
| Perses | ❌ | ✅ |
| Argo CD | ❌ | ✅ |
| Airflow UI, when adopted | ❌ | ✅ |
| Client hostnames (`acme.<domain>`) | ✅, owned by the client layer | **never** |

**Keycloak is the case worth understanding.** It is not an internal service:
applications genuinely need its authentication endpoints on the product edge.
Its *administrative* surface has no reason to be there — and, since SaaS Fabric
became the administrative control plane, no reason to be on the operator plane
either. Its product-plane route matches only the OIDC paths, and its admin
console is published nowhere at all.

That is a deliberate permanent absence rather than a hostname waiting to be
assigned. See [The administrative control plane](#the-administrative-control-plane).

A bare `/` PathPrefix on the product plane would quietly undo that, so
`scripts/check.py` rejects it for any route whose backend carries an admin
surface.

### Services that need neither plane

Most in-cluster traffic is neither. OpenBao is reached by workloads at
`openbao.secrets.svc.cluster.local:8200` and the OpenTelemetry collector at
`observability-collector.observability.svc.cluster.local:4317`. Cluster-local is
the default; a plane is something a service has to earn.

### The operator plane must not gate the product plane

Argo CD reports an `Ingress` with no load-balancer address as Progressing. An
operator-plane Ingress rendered by a service's own chart therefore makes that
service's health depend on the Tailscale operator, and because waves gate on
health, a broken operator plane stops the product plane deploying at all.

That is not hypothetical: on LucentRoot's first bootstrap, Keycloak rendering
its own tailnet Ingress was enough to hold back SaaS Fabric.

So every operator-plane `Ingress` for a platform service lives in
[`applications/core/operator-access`](../applications/core/operator-access/) at
wave `50`, which nothing depends on. A broken operator plane costs
administrative access and nothing else.

The one exception is a catalogue application: `catalogue` is not in the platform
project's destinations, and catalogue is already terminal, so its chart may
render its own.

### Enabling the operator plane

It is core, and it is enabled per environment:
[`applications/core/tailscale`](../applications/core/tailscale/) and
[`applications/core/operator-access`](../applications/core/operator-access/) are
listed by each environment that runs one. LucentRoot does. Production does not
yet — it has no tailnet — so its administrative surfaces are reachable by
`kubectl port-forward` and nothing else.

## The administrative control plane

> **SaaS Fabric is the administrative control plane for the services it manages.
> A shared platform service may expose the runtime endpoints applications and
> clients need. It should not expose its upstream administrative UI as part of
> normal platform operation.**

Operators manage tenants and platform capability through SaaS Fabric:

```text
operator → SaaS Fabric UI → SaaS Fabric API → platform service API
```

and not by logging in to each upstream product in turn:

```text
operator ─┬─ Keycloak console
          ├─ OpenBao UI
          └─ service-specific consoles
```

The second shape is what a platform accretes by default, because every upstream
project ships a console and publishing it is one line of YAML. **"Upstream
software ships an admin UI" is not an operational need.** Operator-plane
exposure requires a reason of its own.

### Not every upstream UI is the same kind of thing

The distinction that makes this tractable: some upstream UIs *are* the
capability operators want; others are vendor administration surfaces that SaaS
Fabric replaces. `operatorUsage` says operators use the service. It does not
say they should use *its* UI, so the service contract states both.

| Service | Managed by Fabric | Upstream admin UI |
|---|---|---|
| Keycloak | yes | **not exposed** — Fabric owns identity management |
| OpenFGA | yes | none — API only |
| Perses | partial | **exposed** — exploration *is* the capability |
| OpenBao | partial | break-glass only |

Published is not the same as published anywhere. A service whose contract
declares `exposure.plane: operator` may keep its console and still be refused a
product-plane route, and `check.py` enforces that separately from the admin-surface
rule above. Perses is the case: it is unauthenticated, so the plane it sits on is
the protection, and *"the namespace does not carry the grant"* is an accident
rather than a decision. See
[`applications/catalogue/perses`](../applications/catalogue/perses/#that-is-a-constraint-not-a-description).

### Keycloak

The first service to adopt the rule fully. Its runtime role is unchanged:
client-facing authentication is served through each client's own canonical
hostname by Envoy, and Keycloak needs no public hostname of its own.

```text
Client browser → https://www.example.com → Envoy → Acme realm endpoints
```

What changed is that the admin console is published on **no** plane. `/admin/*`
stays off the product plane — now a permanent rule rather than a gap awaiting an
admin hostname — and its operator-plane `Ingress` has been removed. `check.py`
rejects both a product-plane route reaching `/admin` and any `Ingress`
publishing a service a contract names as a withheld administrative surface, so
this cannot return by accident.

Administration happens server-side, through the Keycloak Admin REST API, reached
cluster-locally. Privileged credentials never reach a browser:

```text
browser → Fabric API → Keycloak Admin API          correct
browser → Keycloak Admin API                       never
```

### Authority stays with the declarative source

The control plane must not become a second source of truth. Where client
configuration is Git-owned, the UI mutates that source and reconciliation
applies it:

```text
human intent → Fabric UI/API → declarative client state → reconciliation → Keycloak
```

What this must never become is Git saying one thing while the UI changes
Keycloak to another. The reconciliation implementation is future work; the
authority boundary is not negotiable.

### Administrative identity

SaaS Fabric needs a machine identity for the operations it owns — not a human
account, not a persistent browser login, and not the master admin account driven
from a UI. It should hold the least privilege that satisfies the client
composition contract: realms, realm roles, application clients, protocol
settings, and client-scoped groups or users where Fabric defines them.
Unrestricted master-realm administration is a last resort, and LucentRoot may be
broader than production only where justified.

That credential is a platform runtime secret at `secret/platform/*`, delivered
by External Secrets, and never committed. It does not exist yet — see
[Known gaps](#known-gaps).

### Break-glass

Direct administration remains technically possible and is deliberately awkward:

```text
operator → kubectl port-forward → Keycloak
```

No permanent ingress exists to make that convenient, because convenience is how
the exception becomes the norm. Using it bypasses the control plane, so it is
for diagnostics and recovery, not operation.

The operator plane itself stays — the Kubernetes API, Argo CD, Perses and
OpenBao diagnostics all genuinely need it. Removing a console is not the same as
removing the plane.

## Argo CD runtime contract

The platform depends on three things about Argo CD that are **not** defaults.
All are owned by this repository, in [`argocd/runtime`](../argocd/runtime/),
applied at bootstrap and reconciled thereafter. None is left as an assumption.

The two ConfigMap settings share a property worth naming: when missing they fail
*quietly*. Nothing errors — wave ordering simply stops working, and
operator-plane access to Argo CD simply loops.

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

### Server TLS termination

[`argocd/runtime/server-insecure.yaml`](../argocd/runtime/server-insecure.yaml)
sets `server.insecure: "true"` in `argocd-cmd-params-cm`.

The operator plane terminates TLS at the Tailscale proxy and forwards plain
HTTP. By default `argocd-server` serves HTTPS and redirects port 80 to it, so
that arrangement is a redirect loop.

This is the clearest case of why the split is *hosting installs, platform
configures*: the requirement comes from how the platform routes to Argo CD, and
hosting has no way to know that. It is also a different ConfigMap from the
health assessment, and it needs an `argocd-server` restart to take effect —
command-line parameters are read at startup, not watched.

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
| `0` | Envoy Gateway, Tailscale operator, CloudNativePG, External Secrets, OpenBao's seal key | CRDs, control planes — and the key OpenBao starts with |
| `10` | platform `Gateway`, operator access, OpenTelemetry collector, OpenBao, External Secrets, Keycloak database | routing, data, secrets and telemetry foundations |
| `20` | Keycloak, the OpenBao secret store | Keycloak needs its database Healthy and a Gateway to attach to; the store needs both halves it joins |
| `30` | SaaS Fabric | needs routing, identity, secrets and telemetry |
| `40` | `catalogue`-grouped services | nothing in an earlier wave depends on them |
| `50` | operator-plane access | must never gate anything — see below |

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

1. **Git is the boundary, and it is the only one.** The platform project used to
   sit outside reconciliation on the argument that an Application able to
   rewrite its own project could widen its own privileges. That was false —
   `AppProject` is namespaced, the project permits `'*'/'*'` namespaced
   resources, and `argocd` is among its destinations, so the root Application
   could already write AppProjects. The projects are reconciled from Git like
   everything else; protecting `main` is the control.
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
| `secrets` | OpenBao and External Secrets — the secrets authority and its delivery path |
| `data-system` | CloudNativePG operator |
| `observability` | OpenTelemetry collector |
| `tailscale` | Tailscale operator and the `ts-*` proxies it creates |
| `catalogue` | services deployed in the narrower `catalogue` privilege tier |

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

## The bootstrap secret boundary

OpenBao is intended to become the platform's secrets authority. It cannot be the
source of the credentials the platform needs in order to start OpenBao, and
pretending otherwise produces a cycle:

```text
platform needs a secret
        ↓
delivered from OpenBao
        ↓
OpenBao must already be running
```

So there is a small, explicit boundary — and, on the other side of it, more than
one answer. A secret in this platform comes from exactly one of three places,
and which one depends on what kind of secret it is:

```text
generated in-cluster          keycloak-admin
  nothing to choose,          an External Secrets Password generator
  nothing to transport        makes it at first sync

externally issued             operator-oauth, platform-tls
  something outside the       transported in once, through an
  cluster mints it            approval-gated workflow

OpenBao                       everything a workload reads at runtime
  the platform's secrets      External Secrets projects it into
  authority                   the namespaces allowed to ask
```

Only the middle one crosses the boundary. The first never needs to, and the
third cannot until OpenBao is running.

Asked as a question:

```text
does anything outside the cluster mint this?   → transport it, once
does anyone actually need to choose it?        → no: generate it in-cluster
otherwise                                      → OpenBao, via External Secrets
```

### What is on the bootstrap side today

Only what is **issued by something outside the cluster**. A credential that is
merely arbitrary is not a bootstrap secret — it is generated.

| Secret | Namespace | Why it cannot originate in-cluster |
|---|---|---|
| `operator-oauth` | `tailscale` | Tailscale issues it. It is also the one credential that must not come from OpenBao: the operator plane is how you reach OpenBao when OpenBao is not reachable |
| OpenBao's own seal key | `secrets` | Nothing OpenBao needs in order to start can come from OpenBao. Generated in-cluster, and on LucentRoot deliberately disposable — see [`applications/core/openbao-seal`](../applications/core/openbao-seal/) |
| `platform-tls` (production) | `platform-system` | a certificate authority issues it, and the product edge terminates TLS before anything behind it is up |

Delivered by [`inject-bootstrap-secrets.yaml`](../.github/workflows/inject-bootstrap-secrets.yaml)
from a GitHub environment with required reviewers, so applying a credential to a
cluster is an approval rather than a command anyone with push access can run.

The workflow holds no cluster credential of its own. It runs on the node — the
API is tailnet-only, so nothing else could reach it — and uses the kubeconfig
already on that host. Storing a copy in GitHub would add a cluster-admin
credential in a larger blast radius for no capability gain.

Being precise about what that bounds, because it is wider than the approval
gate: **anything that can execute on the self-hosted runner can read that
kubeconfig**, and so can reach the cluster as admin. The required reviewers gate
this workflow, not the runner. What keeps pull-request code away from it is that
[`validate.yaml`](../.github/workflows/validate.yaml) runs on `ubuntu-latest`,
so nothing proposed in a PR executes on the node. Any future workflow that
targets `[self-hosted, lucentroot]` inherits this reach and should be read with
that in mind.

### What is generated in-cluster

`keycloak-admin` used to be on the list above, and is not any more. It is not a
credential anyone needs to *choose*, so nobody does: an External Secrets
`Password` generator creates it at first sync.

| Secret | Generated by |
|---|---|
| `keycloak-admin` | [`applications/core/keycloak-credentials`](../applications/core/keycloak-credentials/) |

A password a person picks is worse than one a machine generates, and a password
that has to travel from a person to a cluster can leak on the way. Generating
removes the choice and the journey together.

It uses `refreshInterval: "0"`, which is load-bearing: Keycloak writes the admin
account into its own database at first start, so a later refresh would rotate
the Secret while Keycloak kept the original.

#### The better answer is not to need one

There used to be a second entry here, for the platform's dashboard runtime, and
removing it is worth a sentence because the reasoning generalises.
[Perses](../applications/catalogue/perses/) is deployed read-only: its
dashboards, projects and datasources are provisioned from Git, its API refuses
every write, and the operator plane already gates who can read it. So there is
no account to hold a password for.

A generated credential is better than a chosen one. No credential is better than
either, wherever the design can honestly get there.

### What comes from OpenBao

Everything a workload reads at runtime.
[External Secrets](../applications/core/external-secrets/) reads OpenBao and
materialises Kubernetes Secrets, joined to it by a single, deliberately bounded
[`ClusterSecretStore`](../applications/core/secret-store/). A workload declares
an `ExternalSecret` and its values live in OpenBao, never here — adding a
variable means writing it to OpenBao and changing nothing in Git.

This is the destination for application configuration, not for the two
categories above. A generated admin credential has no reason to make the round
trip through OpenBao, and a bootstrap credential cannot: OpenBao is not running
yet when it is needed.

On LucentRoot the boundary is invisible in practice, because OpenBao initialises
and unseals itself and establishes its own auth method at first start. Nothing
waits for anyone. On production, where the instance holds data that has to
survive, initialisation stays deliberate — see
[`applications/core/openbao`](../applications/core/openbao/).

### The store is bounded, and the label is load-bearing

The platform store is usable only from namespaces labelled
`fieldstate.nz/layer: platform`, and the OpenBao policy behind it reads
`secret/platform/*` and nothing else. Client secret delivery is a separate
mechanism — a `SecretStore` in the client's own namespace over
`secret/clients/<client>/*`, created by client provisioning.

That means the platform label is part of a security boundary, not just
inventory. The rule that platform-owned labels are never applied to client-owned
resources is what keeps a client namespace out of platform secrets.

**The split is about purpose, not location.** The namespace bound makes it look
like a location rule, and it is not: one workload can legitimately need both
scopes. A catalogue application's own admin credential, database connection and
signing key are platform secrets; the credentials it uses to reach one client's
data are that client's, and come through that client's store. Running in a
platform namespace does not make everything a workload reads a platform secret.

```text
does the platform need this to run the component?      → secret/platform/...
does it only exist because a particular client does?   → secret/clients/<client>/...
```

Longer term the bootstrap side is expected to shrink further rather than grow.
On AKS, `saas-fabric-hosting` can supply a key vault as the bootstrap trust
root, with OpenBao remaining the authority for SaaS and client secrets.
LucentRoot's equivalent today is the approval-gated workflow above — one
credential, transported once, with a reviewer in the path. That is a good deal
better than an operator typing `kubectl create secret`, and still not the
destination: a key vault the environment supplies would remove the last stored
copy.

## Known gaps

Recorded rather than hidden. None blocks a cluster from converging.

| Gap | Consequence | Where it would go |
|---|---|---|
| No certificate automation | The production Gateway listener references a TLS secret that must be injected by hand | a `cert-manager` core application; it has a genuine platform requirement once public hostnames are served |
| No telemetry backend | All three OTLP pipelines terminate in the `debug` exporter | an exporter in `environments/<env>/config/observability.yaml`, once a store is chosen — see [telemetry-backend.md](telemetry-backend.md) |
| No OpenBao auto-unseal **in production** | A restarted production pod must be unsealed by an operator. LucentRoot auto-unseals against a disposable static seal | an `azurekeyvault` seal stanza in `environments/production/config/openbao.yaml`, once `saas-fabric-hosting` supplies a vault and identity |
| No operator plane in production | Argo CD, Perses and OpenBao diagnostics are reachable only by `kubectl port-forward`. Keycloak is no longer among them — its console is deliberately published nowhere, so production needs no Keycloak admin hostname | a tailnet for production, then the same two lines LucentRoot uses |
| Secret injection runs as cluster-admin | The bootstrap workflow uses the node kubeconfig, which can do anything, to write one Secret in one namespace | a platform-owned ServiceAccount with a Role permitting `create`/`patch` on `operator-oauth` in `tailscale` and nothing else |
| No database backups | The Keycloak `Cluster` has no `barmanObjectStore` | `applications/core/keycloak-database`, against storage from `saas-fabric-hosting` |
| SaaS Fabric has no image | The Deployment ships with `replicas: 0`, so the platform substrate converges but SaaS Fabric does not run — see [First milestone](#first-milestone) | a real tag in each environment overlay, once the application repository publishes one |
| **SaaS Fabric has no Keycloak service identity** | The control-plane model depends on Fabric holding a machine identity for the Keycloak Admin API, and none exists. Nothing is broken today because Fabric runs at zero replicas, but the administrative path is declared and unimplemented | a Keycloak service-account client with least-privilege realm-management roles, its credential at `secret/platform/*` via External Secrets. Creating it needs a declarative path into Keycloak's own configuration, which the platform does not yet have |
| **OpenFGA is required and not deployed** | SaaS Fabric's intended runtime needs fine-grained authorization — *may this subject act on this object* — which neither Keycloak nor OpenBao answers. The platform is incomplete until it exists, and its contract says `required: true, deployment: planned` so this reads as a gap rather than an omission | [`applications/core/openfga`](../applications/core/openfga/); the partitioning strategy is a genuine architecture decision, not an implementation detail |
| Airflow DAG ownership undecided, and it is not an isolation boundary | Airflow cannot be adopted. Separately: a shared installation executes DAG code with its own credentials, so a per-client partition inside one installation would be convention rather than a boundary | [`applications/catalogue/airflow`](../applications/catalogue/airflow/) |
| Superset's client partitioning is unassessed | It remains a platform service candidate, but must not be offered as a client capability until the isolation checklist has actually been worked through. The bundled PostgreSQL is a separate, implementation-level blocker | [`applications/catalogue/superset`](../applications/catalogue/superset/); [the checklist](platform-services.md#assessing-tenancy) |
| **Perses has nothing to query** | The consequence of *No telemetry backend* above, stated where it bites. No environment declares a Perses datasource, and therefore no dashboard is provisioned — a dashboard arrives with the datasource it queries or not at all. The mechanism is complete and idle: a datasource is supplied through provisioning, touching no part of the Perses deployment | choosing and deploying a telemetry backend — a decision in its own right rather than an implementation detail of the dashboard runtime |
| Fabric's observability integration is a separate repository's work | The Operations experience — an observability module, its Perses adapter, permission enforcement and client-context propagation — is application source. This is recorded as a cross-repository follow-up, not a platform defect: the platform half is finished and the boundary between them is deliberate, an API rather than an embedded runtime | the application repository. The split, and what *done* means on each side, is in [`applications/catalogue/perses`](../applications/catalogue/perses/#endpoint-contract) |
| Perses client projects are intended, not built | The platform-management use is real today; client projects are a candidate. The blocker is upstream of Perses: with no per-client attribute on telemetry there is nothing for a client's datasource to filter by, so no client capability may be declared yet. A Perses project is a grouping for observability resources and **not** a tenant authority; one per client would be a filing arrangement | [`applications/catalogue/perses`](../applications/catalogue/perses/) |
| Perses has no authentication | `enable_auth` is off. This is an accepted interim state rather than an oversight, and it is bounded by an enforced constraint rather than a note: `exposure.plane: operator` in the service contract, and `check_operator_only_services` refusing any product-plane route to it. Signing in through Keycloak needs a declarative path into Keycloak's own configuration, which is the same thing blocking Fabric's service identity above | an OIDC provider in [`applications/catalogue/perses`](../applications/catalogue/perses/), once a Keycloak client can be created from Git |
| Telemetry carries no per-client attribute | The collector is a transport boundary today, so its tenancy is `not-applicable` rather than unresolved. The moment per-client telemetry is wanted this becomes a real partitioning question: it needs a client attribute enforced at ingest, and a backend whose own tenancy model has been assessed | [`applications/core/observability`](../applications/core/observability/) |
| OpenBao's `initialize` stanza runs once, upstream-by-design | Editing it changes nothing on a running instance, while Argo CD still reports `Synced`/`Healthy` — the manifest matches and the state inside OpenBao does not. The only place in the platform where reconciled does not mean matches Git | On LucentRoot, rebuild: storage is disposable and its policies are immutable-by-rebuild. Production needs continuous reconciliation instead — the same OpenTofu route the client layer already uses for per-client policies. See [`applications/core/openbao`](../applications/core/openbao/README.md) |
