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
  fabric / applications           Argo CD / OpenBao UI /
  client hostnames                Keycloak admin / Grafana
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
| Keycloak administration (`/admin`) | ❌ | ✅ |
| OpenBao API used by workloads | cluster-local, neither plane | — |
| OpenBao operator UI and API | ❌ | ✅ |
| Grafana | ❌ | ✅ |
| Argo CD | ❌ | ✅ |
| Airflow UI, when adopted | ❌ | ✅ |
| Client hostnames (`acme.<domain>`) | ✅, owned by the client layer | **never** |

**Keycloak is the case worth understanding.** It is not an internal service:
applications genuinely need its authentication endpoints on the product edge.
It is the *administrative* surface that has no reason to be there. So its
product-plane route matches only the OIDC paths, and the whole application —
admin console included — is exposed on the operator plane where only the tailnet
can reach it.

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
| `0` | Envoy Gateway, Tailscale operator, CloudNativePG operator | CRDs, control planes, ingress classes |
| `10` | platform `Gateway`, operator access, OpenTelemetry collector, OpenBao, External Secrets, Keycloak database | routing, data, secrets and telemetry foundations |
| `20` | Keycloak, the OpenBao secret store | Keycloak needs its database Healthy and a Gateway to attach to; the store needs both halves it joins |
| `30` | SaaS Fabric | needs routing, identity, secrets and telemetry |
| `40` | catalogue applications | optional |
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
| `secrets` | OpenBao and External Secrets — the secrets authority and its delivery path |
| `data-system` | CloudNativePG operator |
| `observability` | OpenTelemetry collector |
| `tailscale` | Tailscale operator and the `ts-*` proxies it creates |
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

So there is a small, explicit boundary. A short list of credentials is injected
externally at bootstrap; everything else is expected to come from OpenBao once
it is up.

```text
hosting / environment bootstrap
        ↓
minimum bootstrap credentials      ← injected externally, never in Git
platform comes alive
        ↓
OpenBao
        ↓
workload secret projection         ← everything else
```

### What is on the bootstrap side today

Only what is **issued by something outside the cluster**. A credential that is
merely arbitrary is not a bootstrap secret — it is generated.

| Secret | Namespace | Why it cannot originate in-cluster |
|---|---|---|
| `operator-oauth` | `tailscale` | Tailscale issues it. It is also the one credential that must not come from OpenBao: the operator plane is how you reach OpenBao when OpenBao is not reachable |
| `platform-tls` (production) | `platform-system` | a certificate authority issues it, and the product edge terminates TLS before anything behind it is up |

Delivered by [`inject-bootstrap-secrets.yaml`](../.github/workflows/inject-bootstrap-secrets.yaml)
from a GitHub environment with required reviewers, so applying a credential to a
cluster is an approval rather than a command anyone with push access can run.

The workflow holds no cluster credential of its own. It runs on the node — the
API is tailnet-only, so nothing else could reach it — and uses the kubeconfig
already on that host. Storing a copy in GitHub would add a cluster-admin
credential in a larger blast radius for no capability gain.

### What is generated instead

`keycloak-admin` and `grafana-admin` used to be on the list above. They are not
credentials anyone needs to *choose*, so nobody does: an External Secrets
`Password` generator creates them in-cluster.

| Secret | Generated by |
|---|---|
| `keycloak-admin` | [`applications/core/keycloak-credentials`](../applications/core/keycloak-credentials/) |
| `grafana-admin` | [`applications/catalogue/grafana-credentials`](../applications/catalogue/grafana-credentials/) |

A password a person picks is worse than one a machine generates, and a password
that has to travel from a person to a cluster can leak on the way. Generating
removes the choice and the journey together.

Both use `refreshInterval: "0"`, which is load-bearing: Keycloak and Grafana each
write the admin account into their own database at first start, so a later
refresh would rotate the Secret while the application kept the original.

### What is on the other side

The mechanism for everything else.
[External Secrets](../applications/core/external-secrets/) reads OpenBao and
materialises Kubernetes Secrets, joined to it by a single, deliberately bounded
[`ClusterSecretStore`](../applications/core/secret-store/). A workload declares
an `ExternalSecret` and its values live in OpenBao, never here — adding a
variable means writing it to OpenBao and changing nothing in Git.

**The mechanism exists; platform credentials have not moved onto it yet.** Every
secret in the table above is still created by hand, which is correct for the
three that genuinely cannot come from OpenBao. `grafana-admin` is the exception
and the first candidate to move — see
[migrating-lucentroot.md](migrating-lucentroot.md#external-secrets).

The boundary is visible on a fresh cluster: `secret-store` retries until OpenBao
has been initialised, unsealed and given its Kubernetes auth method. The
platform converges to exactly the point where a human must supply the first
secret, and no further.

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

Longer term the bootstrap side is expected to shrink rather than grow. On AKS,
`saas-fabric-hosting` can supply a key vault as the bootstrap trust root, with
OpenBao remaining the authority for SaaS and client secrets. LucentRoot needs an
equivalent minimal mechanism; today that mechanism is "an operator runs
`kubectl create secret` once", which is honest but not a destination.

## Known gaps

Recorded rather than hidden. None blocks a cluster from converging.

| Gap | Consequence | Where it would go |
|---|---|---|
| No certificate automation | The production Gateway listener references a TLS secret that must be injected by hand | a `cert-manager` core application; it has a genuine platform requirement once public hostnames are served |
| No telemetry backend | All three OTLP pipelines terminate in the `debug` exporter | an exporter in `environments/<env>/config/observability.yaml` |
| No OpenBao auto-unseal | A restarted OpenBao pod must be unsealed by an operator | a seal stanza in `environments/production/config/openbao.yaml`, against a key vault from `saas-fabric-hosting` |
| No operator plane in production | Production administrative surfaces are reachable only by `kubectl port-forward` | a tailnet for production, then the same two lines LucentRoot uses |
| Secret injection runs as cluster-admin | The bootstrap workflow uses the node kubeconfig, which can do anything, to write one Secret in one namespace | a platform-owned ServiceAccount with a Role permitting `create`/`patch` on `operator-oauth` in `tailscale` and nothing else |
| No database backups | The Keycloak `Cluster` has no `barmanObjectStore` | `applications/core/keycloak-database`, against storage from `saas-fabric-hosting` |
| SaaS Fabric has no image | The Deployment ships with `replicas: 0`, so the platform substrate converges but SaaS Fabric does not run — see [First milestone](#first-milestone) | a real tag in each environment overlay, once the application repository publishes one |
| Airflow DAG ownership undecided | Airflow cannot be adopted into the catalogue | [`applications/catalogue/airflow`](../applications/catalogue/airflow/) |
