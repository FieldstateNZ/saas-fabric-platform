# Migrating LucentRoot

LucentRoot is an existing k3s cluster currently managed by
[`FieldstateNZ/infrastructure`](https://github.com/FieldstateNZ/infrastructure).
This document turns it into the first real environment managed by
`saas-fabric-platform`.

**LucentRoot is expendable.** Nothing on it is production, and it can be torn
down and rebuilt. That decision shapes everything below: the migration is a
**rebuild**, not a workload-by-workload handover.

Once LucentRoot runs from `saas-fabric-platform/main`, this repository stops
being a definition of a platform and becomes Fieldstate's platform.

---

## Why rebuild rather than hand over

An in-place handover is the harder option, not the safer one. It means a period
where two Argo CD control loops point at one cluster, and the rule that keeps
that safe — *a resource is never managed by both repositories at once* — has to
hold for every resource, through every intermediate state.

It also does not avoid the hard part. The two OpenBao definitions differ in
namespace (`openbao` → `secrets`) and storage engine (`file` → Raft), so its
data has to be exported and re-imported either way. A handover buys nothing and
adds a window in which two controllers with `selfHeal: true` take turns
reverting each other — a failure that presents as a workload flapping with no
failing sync to point at.

A rebuild removes the window entirely:

```text
infrastructure stops          →  cluster is wiped  →  platform bootstraps
```

There is never a moment when both repositories own anything, because there is
never a moment when both are running.

## What a rebuild actually costs

Wiping k3s is cheap. These are the things that do **not** come back on their
own, and every one of them lives outside Kubernetes:

| Lost | Consequence | Recovery |
|---|---|---|
| OpenBao contents | `secret/superset`, `secret/tailscale` | Re-created at bootstrap. The Tailscale OAuth client can be regenerated in the tailnet admin console |
| Superset's database | dashboards and charts | Accepted. Superset is [evaluated, not adopted](../applications/catalogue/superset/); it simply does not come back |
| Tailnet devices | stale `ts-*` and operator devices linger | Remove them in the tailnet admin console. They do not expire promptly and will collide with new registrations by name |
| Cluster CA | `LUCENTROOT_KUBECONFIG` becomes invalid | Regenerate the org secret from the new `/etc/rancher/k3s/k3s.yaml`. Any workflow in `infrastructure` using it fails until then |
| Node-local PVCs | everything on `local-path` | Accepted; that is the whole point of calling the box expendable |

The self-hosted GitHub Actions runner is installed on the host, not in the
cluster, so a k3s teardown leaves it alone.

**Confirm the tailnet cleanup and the kubeconfig rotation before starting.**
They are the two that turn a fifteen-minute rebuild into an afternoon.

---

## Inventory

Everything `infrastructure` owns on LucentRoot, and what happens to it.

### Workloads

| Workload | Namespace | Treatment |
|---|---|---|
| OpenBao 2.6.1 | `openbao` | **Replaced.** The platform defines OpenBao in `secrets`, on Raft. Contents are re-created, not migrated |
| Superset | `superset` | **Retire.** Catalogue candidate, [not adopted](../applications/catalogue/superset/) — its bundled PostgreSQL would compete with CloudNativePG |
| External Secrets 2.9.0 | `external-secrets` | **Replaced.** Now [`applications/core/external-secrets`](../applications/core/external-secrets/) in `secrets` |
| Tailscale operator 1.98.4 | `tailscale` | **Replaced.** Now [`applications/core/tailscale`](../applications/core/tailscale/), chart `1.102.3` |
| `ts-proxy-watchdog` | `tailscale` | **Retire, then re-evaluate.** See [The watchdog](#the-watchdog) |

### Platform machinery

| Component | Treatment |
|---|---|
| Argo CD 10.1.3, self-managing | **Do not carry across.** Argo CD's installation becomes `saas-fabric-hosting`'s; this repository owns only [its runtime configuration](../argocd/runtime/) |
| Root Application `fieldstate-platform` | **Retire**, replaced by `platform-root` |
| ApplicationSets `applications`, `chart-applications` | **Retire** for platform services — the platform expresses ordering with sync waves, which ApplicationSets cannot |
| `AppProject applications` | **Retire**, replaced by `saas-fabric-platform` and `saas-fabric-catalogue` |
| Shared workload chart | **Retire or evolve** — see [The shared chart](#the-shared-chart) |
| `publish-application.yml` | **Preserve the pattern**, not the file — see [Image delivery](#image-delivery) |
| GHCR pull credential refresh | **Retire for now.** This repository deploys no private images yet; it returns with the first one |

### What the platform gains that LucentRoot has never had

| | |
|---|---|
| Envoy Gateway and the product plane | LucentRoot has only ever had Tailscale |
| CloudNativePG | Superset ran a bundled PostgreSQL |
| Keycloak | no identity provider at all |
| OpenTelemetry collector | no telemetry boundary |
| Sync-wave dependency ordering | flat, retry-driven |

---

## The rebuild

### 1. Confirm what is being destroyed

Run through [what a rebuild costs](#what-a-rebuild-actually-costs). If anything
in OpenBao is not reproducible, export it now:

```bash
kubectl -n openbao exec openbao-0 -- bao kv get -format=json secret/superset
kubectl -n openbao exec openbao-0 -- bao kv get -format=json secret/tailscale
```

Everything else on the box is reproducible or expendable.

### 2. Stop `infrastructure` managing the cluster

Delete its root Application **without cascade**, so retiring the control loop
does not start deleting workloads out from under the teardown:

```bash
kubectl -n argocd patch application fieldstate-platform \
  -p '{"metadata":{"finalizers":null}}' --type=merge
kubectl -n argocd delete application fieldstate-platform
```

Then disable the scheduled workflows in `infrastructure` — `bootstrap-platform.yml`
runs twice an hour on the box's own runner and will happily keep reconciling a
cluster you are trying to replace.

### 3. Tear down

```bash
/usr/local/bin/k3s-uninstall.sh
```

### 4. Clean up the tailnet

In the tailnet admin console, remove the stale `tailscale-operator` device and
every `ts-*` proxy device. Skipping this is the single most common way the
rebuild goes wrong: name collisions leave the new operator unable to register a
device it thinks already exists.

### 5. Rebuild and bootstrap

Follow [bootstrap.md](bootstrap.md#k3s--lucentroot) from the top. It is the
normal LucentRoot bootstrap; nothing about it is migration-specific.

Rotate `LUCENTROOT_KUBECONFIG` in GitHub once the new cluster is up.

### 6. Verify

```bash
kubectl -n argocd get applications
```

Every Application `Synced` and `Healthy`, with two expected exceptions until the
one-time OpenBao steps are done:

- `secret-store` retries until OpenBao's Kubernetes auth method exists;
- `saas-fabric` reports Healthy with **zero replicas** — no image is published
  yet, and that is [the first milestone working as designed](architecture.md#first-milestone),
  not the application running.

### 7. Hollow out `infrastructure`

Remove the migrated registrations and reduce the repository to whatever
genuinely remains outside SaaS Fabric. Do this only after LucentRoot has been
running from this repository long enough to trust it.

## Rollback

Rebuild the old way: reinstall k3s and run `infrastructure`'s
`bootstrap-platform.yml` with a manual dispatch. It is a full rebuild in the
other direction, which is the honest cost of choosing rebuild over handover.

The window where rollback is cheap closes at step 3. Before it, nothing has
happened that a `git revert` cannot undo.

---

## Decisions taken during this migration

### External Secrets

**Included, and bootstrapped with the platform.**

It was originally deferred on the grounds that it depends on OpenBao having
moved, and that moving OpenBao was the riskiest step. A rebuild removes that
reason: OpenBao is not moved, it is created new, so there is no migrated
database to debug a new secret path against.

That leaves the opposite argument. A rebuild is a single, one-shot bootstrap,
and the OpenBao configuration that External Secrets depends on — the Kubernetes
auth method, a policy, a role — is done by hand at bootstrap either way. Doing it
once, in the shape we intend to keep, beats doing it twice.

**What this establishes is the mechanism, not a migration of existing secrets.**
Every platform credential is still created by hand at bootstrap, and that is
correct for the ones on the bootstrap side of the boundary:

| Secret | Still manual because |
|---|---|
| `keycloak-admin` | Keycloak will not start without it, and on a fresh cluster OpenBao is still sealed when Keycloak first syncs |
| `operator-oauth` | deliberately permanent — the operator plane is how you reach OpenBao when OpenBao is not reachable |
| `platform-tls` (production) | the product edge terminates TLS before anything behind it is up |
| `grafana-admin` | **not** on the bootstrap side. Grafana is wave `40`, long after OpenBao could serve it. It is the first credential that should move, and it has not yet |

`grafana-admin` is the honest gap. Moving it needs an `ExternalSecret` in the
`catalogue` namespace, which no catalogue Application currently renders — the
Grafana chart has no template for one. That is a small, self-contained follow-up,
not a reason to hold the mechanism back.

The pattern is `infrastructure`'s, because it was already the right one: a
single `ClusterSecretStore` authenticating with the Kubernetes auth method, and
`dataFrom.extract` so that adding a variable means writing it to OpenBao and
changing nothing in Git. What is **not** `infrastructure`'s is the scope — see
below.

See [`applications/core/external-secrets`](../applications/core/external-secrets/)
and [`applications/core/secret-store`](../applications/core/secret-store/).

### The store is bounded to platform namespaces

`infrastructure`'s store is cluster-wide, authenticated as one service account
holding read across the entire `secret/` mount. On a single-tenant box that is
fine. As the contract for a multi-tenant platform it is wrong, because the
security boundary it establishes is "anyone who can create an `ExternalSecret`
can read anything".

So the platform store is bounded on both sides:

```text
conditions.namespaceSelector    fieldstate.nz/layer: platform
OpenBao policy                  read on secret/platform/* only
```

and client secret delivery is reserved as a separate mechanism — a `SecretStore`
in the client's own namespace, over `secret/clients/<client>/*`, created by
client provisioning alongside the client's realm, database and routes.

This also promotes `fieldstate.nz/layer: platform` from inventory metadata to
part of a security boundary. Applying it to a namespace this repository does not
own now grants that namespace access to platform secrets.

### The Tailscale OAuth credential stays a bootstrap secret

Even with External Secrets present, `operator-oauth` is created directly rather
than projected from OpenBao. The operator plane is how you reach OpenBao when
OpenBao is not reachable; sourcing its credentials from OpenBao inverts that and
makes a broken OpenBao unobservable.

See [the bootstrap secret boundary](architecture.md#the-bootstrap-secret-boundary).

### The watchdog

`ts-proxy-watchdog` restarts Tailscale proxies that silently lose their netmap —
the device shows offline and Funnel dies while the application still answers
from cached routing. It is a real workaround for a real fault.

It is **not** carried across, because it is not yet known whether the fault
still occurs. `infrastructure` pins Tailscale `1.98.4`; the platform pins
`1.102.3`. Porting a workaround for a bug that may be fixed would install a
CronJob holding `pods/exec` in the `tailscale` namespace for no reason.

Watch for it after the rebuild. If proxies still go offline, it comes back — and
then it belongs in this repository as a platform-owned resource, not applied by
a workflow.

### The shared chart

`infrastructure/charts/application` renders a Fieldstate workload from a single
registration file. It is a good chart. It is not carried across, because the two
repositories answer different questions:

- `infrastructure` deploys *applications*, and a shared chart is the right shape;
- `saas-fabric-platform` deploys *platform components*, each with a genuine
  upstream chart or genuine hand-written manifests.

Its natural home is whatever eventually renders SaaS Fabric's client-facing
workloads — likely `saas-fabric-clients`. Its guardrails are worth taking now;
see below.

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

The implementation ideas transfer too: a GitHub App token scoped to one
repository, validation that the target exists and is enabled, and concurrency
handling that rebases rather than failing when two deployments race.

This repository has no such workflow yet because it deploys no first-party
images yet. When [SaaS Fabric publishes one](../applications/core/saas-fabric/),
that is the moment — and it should update an image tag in an environment
overlay, nothing more.

---

## Guardrails adopted from `infrastructure`

Fieldstate platform conventions now, not one repository's habits. Where a
convention is already enforced here, the enforcing check is named.

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

`infrastructure` guarantees its bootstrap script and its self-managing
Application cannot pin different Argo CD versions: the script reads the version
out of the Application manifest, and CI asserts it.

The lesson transfers even though the ownership does not. This repository does
not install Argo CD, so the discipline belongs in `saas-fabric-hosting`: one
authoritative declaration of the Argo CD version, with whatever bootstrap
mechanism it uses derived from that declaration rather than restating it.

What this repository owns is the *floor* — Argo CD 2.13 or later, and the
Application health assessment — recorded in
[the Argo CD runtime contract](architecture.md#argo-cd-runtime-contract) and
checked by `check_argocd_runtime_configuration`.
