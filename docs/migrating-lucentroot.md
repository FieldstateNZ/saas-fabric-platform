# Migrating LucentRoot

LucentRoot is an existing, working k3s cluster currently managed by the
[`FieldstateNZ/infrastructure`](https://github.com/FieldstateNZ/infrastructure)
repository. This document turns it into the first real environment managed by
`saas-fabric-platform`.

**The box is not rebuilt.** Ownership is handed over one workload at a time,
with a rule that is not negotiable:

> A Kubernetes resource is never managed by both repositories at the same time.

Once LucentRoot runs from `saas-fabric-platform/main`, this repository stops
being a definition of a platform and becomes Fieldstate's platform.

---

## The two repositories during migration

| | `infrastructure` | `saas-fabric-platform` |
|---|---|---|
| Model | ApplicationSets over `applications/<name>.yaml` registrations | app-of-apps with sync waves |
| Argo CD | manages itself from `argocd/platform/argocd.yaml` | installed by hosting; this repo owns only its runtime configuration |
| Routing | Tailscale for everything | product plane (Envoy) and operator plane (Tailscale) |
| Environments | one, `main` | LucentRoot and production |

Both point Argo CD at the same cluster. That is safe only while their resource
sets are disjoint, which is what the handover procedure below enforces.

---

## Inventory

Everything `infrastructure` owns on LucentRoot, and what happens to it.

### Workloads

| Workload | Namespace | Owned by | Treatment |
|---|---|---|---|
| OpenBao 2.6.1 | `openbao` | `infrastructure/applications/openbao.yaml` | **Migrate** — the platform already defines OpenBao, in `secrets` |
| Superset | `superset` | `infrastructure/applications/superset.yaml` | **Temporarily retain**, then adopt as catalogue |
| External Secrets 2.9.0 | `external-secrets` | `infrastructure/applications/external-secrets.yaml` | **Migrate next** — see [External Secrets](#external-secrets) |
| Tailscale operator 1.98.4 | `tailscale` | `infrastructure/applications/tailscale.yaml` | **Migrate** — now `applications/core/tailscale`, chart `1.102.3` |
| `ts-proxy-watchdog` | `tailscale` | `infrastructure/system/`, applied by a workflow | **Temporarily retain** — see [The watchdog](#the-watchdog) |

### Platform machinery

| Component | Owned by | Treatment |
|---|---|---|
| Argo CD 10.1.3 (self-managing) | `infrastructure/argocd/platform/argocd.yaml` | **Do not migrate as-is.** Argo CD's installation becomes `saas-fabric-hosting`'s; this repository owns only [its runtime configuration](../argocd/runtime/) |
| Root Application `fieldstate-platform` | `infrastructure/argocd/platform/root.yaml` | **Retire** at cutover, replaced by `platform-root` |
| ApplicationSets `applications`, `chart-applications` | `infrastructure/argocd/platform/` | **Retire** for platform services — the platform expresses ordering with sync waves, which ApplicationSets cannot |
| `AppProject applications` | `infrastructure/argocd/platform/project.yaml` | **Retire**, replaced by `saas-fabric-platform` and `saas-fabric-catalogue` |
| Shared workload chart | `infrastructure/charts/application` | **Retire or evolve** — see [The shared chart](#the-shared-chart) |
| `publish-application.yml` | `infrastructure/.github/workflows/` | **Preserve the pattern**, not the file — see [Image delivery](#image-delivery) |
| GHCR pull credential refresh | `infrastructure/.github/workflows/bootstrap-platform.yml` | **Temporarily retain** until a workload registration exists in this repository that needs it |

### Not present here yet

| Missing | Consequence |
|---|---|
| Superset | catalogue candidate, [already assessed](../applications/catalogue/superset/) and blocked on its bundled PostgreSQL |
| External Secrets | every platform secret is injected by hand |
| GHCR pull credentials | this repository deploys no private images yet |

---

## Handover procedure

Per workload, in this order. The middle step is the one that matters.

```text
OLD OWNER stops
        ↓
verify the resource is no longer reconciled
        ↓
NEW OWNER adopts or creates
```

Never:

```text
infrastructure ──┐
                 ├── both reconciling OpenBao
platform ────────┘
```

Two controllers with `selfHeal: true` on one resource do not settle. They take
turns reverting each other, and the symptom is a workload that flaps with no
failing sync to point at.

### 1. Disable in `infrastructure`

Set `enabled: false` in `applications/<name>.yaml` and merge. The
ApplicationSet's generator selects on `enabled: "true"`, so the generated
Application disappears.

Removing the file achieves the same thing, but disabling keeps the registration
readable during the migration and makes the revert one word.

### 2. Verify it is no longer reconciled

```bash
kubectl -n argocd get applications
kubectl -n argocd get applicationset applications      -o yaml | grep -c <name>
kubectl -n argocd get applicationset chart-applications -o yaml | grep -c <name>
```

The Application must be **gone**, not merely `OutOfSync`. Its
`resources-finalizer` means deletion also removes the workload — which is why
step 3 differs for stateful and stateless workloads.

### 3. Adopt or recreate

**Stateless** (Tailscale operator, External Secrets): let it go, then let the
platform create it. Downtime is a reconciliation cycle.

**Stateful** (OpenBao): do not let the finalizer delete it. Remove the finalizer
first, so disabling the registration orphans the workload instead of destroying
it:

```bash
kubectl -n argocd patch application <name> \
  -p '{"metadata":{"finalizers":null}}' --type=merge
```

Then disable the registration, confirm the Application is gone and the pods are
still running, and let the platform adopt the live resources. Argo CD adopts by
name: the new Application takes ownership of anything matching its rendered
manifests.

Expect a first sync that is `OutOfSync` in visible ways — namespace, labels and
chart version all differ between the two definitions. Read the diff before
syncing.

### 4. Confirm ownership moved

```bash
kubectl -n <namespace> get all \
  -o custom-columns=NAME:.metadata.name,INSTANCE:.metadata.labels.app\\.kubernetes\\.io/instance
```

Every resource should carry the new Application's instance label and nothing
should carry the old one.

---

## Order of migration

Dependencies decide this, not convenience.

| Step | Workload | Why here |
|---|---|---|
| 1 | Tailscale operator | Nothing depends on it, and the operator plane is how the rest is watched. Stateless: safe to move first |
| 2 | Argo CD ownership | Stop `infrastructure` self-managing Argo CD before its root Application is retired |
| 3 | OpenBao | Stateful and the riskiest. Move it while the operator plane already works |
| 4 | External Secrets | After OpenBao, because it reads from it |
| 5 | Superset | Last, or retire it. It is catalogue, not core |

### OpenBao is not a clean move

The two definitions differ in ways that matter:

| | `infrastructure` | `saas-fabric-platform` |
|---|---|---|
| Namespace | `openbao` | `secrets` |
| Storage | `file`, standalone | Raft, HA-shaped with one replica |
| Unsealing | sidecar loop reading a Secret in the same namespace | documented manual step |
| Chart | `0.29.0` | `0.29.2` |

A different namespace and a different storage engine mean this is a **migration,
not an adoption**. The data has to be exported and re-imported, and the platform
definition deliberately does not carry the auto-unseal sidecar across: it keeps
the unseal key in the same namespace as the thing it protects, which makes the
seal decorative.

Do not attempt this until the operator plane works and there is a verified
export. Treat it as its own change with its own rollback.

---

## Decisions taken during this migration

### External Secrets

**Not in this change. Immediately after.**

The platform has no secret projection today; every secret is injected at
bootstrap. Bringing External Secrets across is worth doing and the
`infrastructure` pattern is proven — a `ClusterSecretStore` authenticating to
OpenBao with the Kubernetes auth method, and per-workload `ExternalSecret`
resources.

It is deferred one PR because it depends on OpenBao having moved, and moving
OpenBao is the riskiest step here. Doing both at once would mean debugging a new
secret path against a database that just changed storage engines.

The interim rule is the one that keeps this safe: **the Tailscale operator's
OAuth credential is a bootstrap secret in this repository, not an
External-Secrets-delivered one.** `infrastructure` currently delivers
`operator-oauth` from OpenBao via External Secrets. At handover that
`ExternalSecret` is removed and the secret is created directly. Otherwise the
platform would own the operator while the other repository owned its
credentials — dual ownership of exactly the kind this document exists to
prevent.

See [the bootstrap secret boundary](architecture.md#the-bootstrap-secret-boundary).

### The watchdog

`ts-proxy-watchdog` restarts Tailscale proxies that silently lose their netmap —
the device shows offline and Funnel dies while the application still answers
from cached routing. It is a real workaround for a real fault.

**Temporarily retained in `infrastructure`.** It is applied by a workflow rather
than Argo CD, and it targets proxies the platform's operator now creates. Before
it moves, two things need deciding: whether the fault still occurs on Tailscale
`1.102.3`, and whether a CronJob with `pods/exec` in the `tailscale` namespace is
the shape we want in a platform this repository is responsible for.

Retaining it is a conscious exception to the one-owner rule, and it is safe only
because it manages its own resources — a CronJob, a ServiceAccount and a Role —
that this repository does not define. Nothing else in `infrastructure`'s
`system/` directory is in that position.

### The shared chart

`infrastructure/charts/application` renders a Fieldstate workload from a single
registration file. It is a good chart. It is not carried across as-is, because
the two repositories answer different questions:

- `infrastructure` deploys *applications*, and a shared chart is the right shape;
- `saas-fabric-platform` deploys *platform components*, each with a genuine
  upstream chart or genuine hand-written manifests.

The right home for that chart is whatever eventually renders SaaS Fabric's
client-facing workloads — likely `saas-fabric-clients`. Its guardrails, though,
are worth taking now; see below.

### Image delivery

`publish-application.yml` has a security property worth preserving exactly:

```text
application repository
     ✗ no kubeconfig
     ✗ no Helm access
     ✗ no cluster credentials
       ↓
requests a desired-state change
       ↓
Git changes
       ↓
Argo CD deploys
```

What changes is where the desired state lives. For a platform component it is
this repository; for client configuration it will be `saas-fabric-clients`.

The implementation ideas are worth keeping too: a GitHub App token scoped to one
repository, validation that the registration exists and is enabled, and
concurrency handling that rebases rather than failing when two deployments race.

This repository has no such workflow yet because it deploys no first-party
images yet. When [SaaS Fabric publishes one](../applications/core/saas-fabric/),
that is the moment to add it — and it should update an image tag in an
environment overlay, nothing more.

---

## Guardrails adopted from `infrastructure`

These are now Fieldstate platform conventions, not one repository's habits.
Where they are already enforced here, the enforcing check is named.

| Guardrail | Status |
|---|---|
| Reject `latest` and placeholder versions | **Adopt** — see below |
| Pins are mandatory | Enforced: `check_applications_match_their_project` |
| Secret scanning | Enforced: `check_no_plaintext_secrets`, plus gitleaks in CI |
| Render everything CI can render | Enforced: `scripts/render.py` |
| Bootstrap and declarative config cannot disagree on a version | **Adopt** — see below |
| Fail on retired configuration rather than ignoring it | **Adopt** — see below |
| Comments explain the historical reason | Existing convention |
| A *Known gaps* section describing real operational risk | [architecture.md](architecture.md#known-gaps) |
| Application repositories never deploy directly | Preserved; see [Image delivery](#image-delivery) |

### Tombstones

The best idea in `infrastructure`'s chart. A retired configuration key does not
become silently ignored — it fails with a pointer:

```text
database: was retired with the shared Postgres; see docs/lucentroot.md
```

Silently ignored configuration is worse than a broken deployment, because the
person who wrote it believes it is doing something. When this platform retires a
property, an old declaration must say so.

### Argo CD version discipline

`infrastructure` guarantees that its bootstrap script and its self-managing
Application cannot pin different Argo CD versions: the script reads the version
out of the Application manifest, and CI asserts it.

The lesson transfers even though the ownership does not. This repository does
not install Argo CD, so the discipline belongs in `saas-fabric-hosting`: one
authoritative declaration of the Argo CD version, and whatever bootstrap
mechanism it uses derived from that declaration rather than restating it.

What this repository owns is the *floor* — Argo CD 2.13 or later, and the
Application health assessment — recorded in
[the Argo CD runtime contract](architecture.md#argo-cd-runtime-contract) and
checked by `check_argocd_runtime_configuration`.

---

## Cutover

When steps 1–5 are done and the platform is running LucentRoot:

1. Confirm every Argo CD Application in this repository is `Synced` and
   `Healthy`, and that `infrastructure`'s root Application manages nothing that
   this repository also defines.
2. Delete `infrastructure`'s root Application **without cascade**, so retiring
   its control loop does not delete workloads:

   ```bash
   kubectl -n argocd patch application fieldstate-platform \
     -p '{"metadata":{"finalizers":null}}' --type=merge
   kubectl -n argocd delete application fieldstate-platform
   ```

3. Remove the migrated registrations from `infrastructure` and hollow the
   repository out to whatever genuinely remains outside SaaS Fabric.
4. Update [architecture.md](architecture.md#known-gaps) — several gaps in this
   document stop being hypothetical once a real cluster depends on them.

Argo CD itself is not deleted. Its installation becomes `saas-fabric-hosting`'s
responsibility, and the transfer is a separate change: stop `infrastructure`
self-managing it, then have hosting declare it.

## Rollback

Until cutover, rollback per workload is the handover procedure in reverse: set
`enabled: true` in `infrastructure`, confirm this repository no longer defines
the resource, and let the old ApplicationSet recreate it.

After cutover there is no rollback to `infrastructure`, because its root
Application is gone. That is the point at which the migration is finished.
