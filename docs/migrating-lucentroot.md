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

It also buys nothing. A handover's whole point is preserving what is running,
and there is nothing here worth preserving: the two OpenBao definitions differ
in namespace (`openbao` → `secrets`) and storage engine (`file` → Raft), so
adopting the live one is not possible anyway, and its contents are reproducible.
All a handover adds is a window in which two controllers with `selfHeal: true`
take turns reverting each other — a failure that presents as a workload flapping
with no failing sync to point at.

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
| OpenBao contents **and its seal key** | `secret/superset`, `secret/tailscale` | Nothing to recover. A rebuilt OpenBao initialises itself with a new seal key and an empty store; the Tailscale OAuth client is regenerated in the tailnet admin console |
| Superset's database | dashboards and charts | Accepted. Superset is [evaluated, not adopted](../applications/catalogue/superset/); it simply does not come back |
| Tailnet devices | stale `ts-*` and operator devices linger | Remove them in the tailnet admin console. They do not expire promptly and will collide with new registrations by name |
| Cluster CA | any stored copy of the kubeconfig becomes invalid | `infrastructure` holds one as `LUCENTROOT_KUBECONFIG` and its workflows fail until it is regenerated. This repository deliberately stores none — its workflow reads the node's own `/etc/rancher/k3s/k3s.yaml`, which is always current |
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

### 1. Read what a rebuild costs

[What a rebuild actually costs](#what-a-rebuild-actually-costs) — not to decide
whether to proceed, but because two of the items are tailnet and GitHub
cleanup that will bite later if skipped now.

Nothing on the box needs backing up. LucentRoot is expendable and its contents
are reproducible; that is the premise the whole approach rests on.

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

Nothing in this repository needs rotating afterwards: its bootstrap workflow
reads the node's own kubeconfig rather than a stored copy. `infrastructure`'s
`LUCENTROOT_KUBECONFIG` does go stale, and its workflows fail until it is
regenerated — which matters only for as long as that repository is still in
use.

### 6. Restart `argocd-server`

Do this before anything else, and do not skip it because the cluster looks fine.

The bootstrap set adds `server.insecure` to `argocd-cmd-params-cm`, but
command-line parameters are read at startup rather than watched, so the running
server has not picked it up:

```bash
kubectl -n argocd rollout restart deployment/argocd-server
kubectl -n argocd rollout status deployment/argocd-server
```

**The symptom if you miss it** is a redirect loop on
`https://argocd-<environment>.<tailnet>` — the Tailscale proxy terminates TLS
and forwards plain HTTP, `argocd-server` is still redirecting port 80 to HTTPS,
and the browser bounces between them. Everything else looks healthy: the
Application is Synced, the proxy device is online, `kubectl` works. It reads as a
Tailscale fault and is not one.

Confirm it took:

```bash
kubectl -n argocd exec deploy/argocd-server -- \
  sh -c 'echo "$ARGOCD_SERVER_INSECURE"'
```

`true` means the operator plane will serve Argo CD. See
[argocd/runtime/README.md](../argocd/runtime/README.md#server-tls-termination).

### 7. Verify

```bash
kubectl -n argocd get applications
```

Every Application `Synced` and `Healthy`, with one expected exception. OpenBao
initialises and unseals itself, so nothing waits on it:

- `saas-fabric` reports Healthy with **zero replicas** — no image is published
  yet, and that is [the first milestone working as designed](architecture.md#first-milestone),
  not the application running.

### 8. Hollow out `infrastructure`

Remove the migrated registrations and reduce the repository to whatever
genuinely remains outside SaaS Fabric. Do this only after LucentRoot has been
running from this repository long enough to trust it.

## Rollback

There is none, and none is wanted. LucentRoot is expendable: if the rebuild goes
wrong, rebuild again rather than trying to restore what was there.

This is specific to LucentRoot. **Production rollback is a real requirement** and
is a different thing entirely — see [releases.md](releases.md#rolling-back).

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

A rebuild is also the moment the manual step disappears. Nothing is typed into
this cluster by hand:

| Secret | Comes from |
|---|---|
| `keycloak-admin` | generated in-cluster, [`applications/core/keycloak-credentials`](../applications/core/keycloak-credentials/) |
| `grafana-admin` | generated in-cluster, [`applications/catalogue/grafana-credentials`](../applications/catalogue/grafana-credentials/) |
| `operator-oauth` | transported once, by [`inject-bootstrap-secrets.yaml`](../.github/workflows/inject-bootstrap-secrets.yaml) with a reviewer in the path |
| everything a workload reads | OpenBao, via External Secrets |

The two admin credentials are arbitrary — nobody needs to *choose* them — so
nobody does. Only the credential Tailscale issues has to travel, and it travels
through an approval gate rather than a shell.

The three sources and when each applies are set out in
[architecture.md](architecture.md#the-bootstrap-secret-boundary).

The projection pattern is `infrastructure`'s, because it was already the right
one: a
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
