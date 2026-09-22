# Master instance

| | |
|---|---|
| Product | OpenTofu, `keycloak/keycloak` provider |
| Upstream project | https://github.com/opentofu/opentofu, https://github.com/keycloak/terraform-provider-keycloak |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | OpenTofu `1.12.6` (`ghcr.io/opentofu/opentofu:1.12.6`), provider `5.9.0` |
| Licence | Mozilla Public License 2.0 (OpenTofu), Apache License 2.0 (the Keycloak provider) |
| Namespace | `operator-system` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `30` |

## Why it exists in SaaS Fabric

The control-plane instance signs in at the gateway (ADR 0024 in the application
repository). That needs a confidential client in the master realm with a
secret Envoy can present, a `fabric-operator` role, and master-realm `admin`
for the operators who create client realms (ADR 0012). Until this Application
existed, every one of those was a person's job: create
`saas-fabric-gateway` in Keycloak by hand, write its secret into OpenBao,
assign roles to each operator by hand.

The product owner's rule, recorded as [ADR 0025](https://github.com/FieldstateNZ/saas-fabric/blob/main/docs/decisions/0025-realm-bootstrap-is-platform-composition.md)
in the application repository: **nothing in the master realm is made by
hand.** This Application is that rule. It converges the master realm's own
instance resources every time it syncs, using OpenTofu and the Keycloak
provider — the same mechanism `hosting/src/SaaSFabric.Aspire.Hosting/Templates`
already uses to provision a client realm — driven by the Keycloak bootstrap
administrator this platform already generates
([`../keycloak-credentials`](../keycloak-credentials/)) and never types.

This repository already applies the identical rule to its own OpenBao seal
(`check_openbao_bootstraps_itself` in `scripts/check.py`, "LucentRoot's OpenBao
must need no human in its lifecycle"). `check_master_realm_bootstraps_itself`
is the same check, for the master realm.

## What it converges

| Resource | Kind | Why |
|---|---|---|
| the master realm itself | `keycloak_realm`, imported | one attribute: `frontendUrl` (see "The realm itself" below) |
| `saas-fabric-gateway` | confidential `keycloak_openid_client` | Envoy redeems an authorization code with it (ADR 0024) |
| `saas-fabric-console` | public `keycloak_openid_client`, PKCE S256 | the console's own sign-in, until ADR 0024 slice 2 retires it — removal is two applies, not one: drop `lifecycle` and apply, then remove the resource and apply again (`base/module/main.tf`'s own comment on it) |
| `fabric-operator` | `keycloak_role` | what the control plane's OIDC posture checks |
| `admin` | read, not created | the master realm's own built-in role — **composite over every client realm's own administration, full Keycloak authority**, not a scoped slice of it. This is exactly what ADR 0012's realm creation needs, and exactly why it is granted to declared operators only, never to the control plane's own service identity |
| each declared operator's role grant | `keycloak_user_roles`, `exhaustive = false` | `fabric-operator` and master-realm `admin`, without disturbing any other role mapping the operator holds |

No Vault provider: the master realm holds no client secret partition — that
is a client realm's own OpenTofu, not this.

The module itself lives in [`base/module`](base/module) and ships to the Job
as a `ConfigMap`, not baked into an image — the same repository-as-source-of-truth
shape every other Application here uses, applied to a Job for the first time.
`base/module/.terraform.lock.hcl` ships alongside the `.tf` files, generated
with `tofu providers lock -platform=linux_amd64 -platform=linux_arm64` against
a copy of the module — this Job runs on whichever architecture LucentRoot's
node happens to be, and a lock file missing a platform fails `tofu init`
outright rather than silently trusting an unverified provider build.

## The realm itself

`resource "keycloak_realm" "master"` is a late addition, and the trade-off it
carries is worth stating exactly — two different guarantees, for two
different parts of the schema, that must not be collapsed into one claim.

**Every realm argument except `attributes` is left alone.** `realm` is the
only other argument `main.tf` sets; every other argument the provider's
schema carries — 59 of them, from session lifetimes to themes to
registration policy, enumerated by name in `lifecycle.ignore_changes` — is
deliberately left alone, so nothing about session lifetimes, themes or
registration policy that an operator changes through Keycloak's own admin
console (unreachable in the ordinary course, but not physically impossible —
see [docs/architecture.md#the-administrative-control-plane](../../../docs/architecture.md#the-administrative-control-plane))
is ever reverted by this module on its next sync.

**Every realm attribute except `frontendUrl` is removed on every apply — the
one place that first claim does not hold.** `attributes` is Optional, not
Computed, in this provider's schema, and the provider replaces the realm's
*whole* attribute map on apply rather than merging into it (upstream
`keycloak/terraform-provider-keycloak#1031`) — `ignore_changes` cannot
protect a key it was never told exists, the way it protects an ignored
argument. `main.tf` sends `{ frontendUrl = var.public_base_url }` as the
entire map; whatever else the realm's attributes held before does not
survive the next apply. Anything else ever stored as a master-realm
attribute — by hand, through the admin console, or by any other tool — has
to be declared here too, or this module removes it.

It is brought into state with OpenTofu's own `import` block, unconditionally,
not this provider's `import` argument (`keycloak_realm` does not have one):
the master realm exists in every environment before this module ever runs,
so there is no "fresh vs adopted" branch to make here the way there is for
the two clients below. The id is the realm's own name, `master`, never a
UUID, and the block is a no-op once the realm is already in this module's
state — safe to leave in place on every apply, in every environment.

The one thing it actually changes: `frontendUrl`, set to this instance's
public origin. Without it, Keycloak rejected a valid operator token on the
internal Admin API with `401` while accepting the same token on the public
origin, because the browser and this Job reach Keycloak at different
addresses — see the control plane's README, "Identity and bootstrap
prerequisites", for the incident this fixed. It was the platform's last
hand-made master-realm step; this resource is what removed it.

## Who the operators are

Named in
[`overlays/lucentroot/master-instance-config.yaml`](overlays/lucentroot/master-instance-config.yaml),
by Keycloak username. One name today: `admin`, the bootstrap administrator
Keycloak created from the credential the platform generated, and the only
account the master realm holds. The hand-made `fabric-operator` grant issue
#70 described was made on that account, and since this module's first
successful run the file owns it, so it can never drift from the file. Adding
an operator is adding a name, once their account exists; the next sync
grants both roles.
Removing a name revokes nothing — the grant is `exhaustive = false`, a
partial assignment — so revocation is still an act in Keycloak, and that is
the one thing about operators this module does not yet do. An empty roster
is a valid state: it grants nothing, to nobody, and blocks no sync.

The module's `keycloak_user` lookup resolves by username only, not by email —
see `base/module/main.tf`'s comment on `data.keycloak_user.operator` for why
an email-shaped username still works, and what does not.

**A name that does not already exist as a Keycloak user fails the whole
convergence, at plan time.** `keycloak_user` is a data source, so its read
happens during `tofu plan`, before anything is applied — a typo, or an
operator declared before their account exists, fails this Job before it
changes anything else, not partway through.

**Creating that account is the one thing this module does not do.** On a
fresh environment the only account that exists is the bootstrap administrator
this Job authenticates as. Granting an operator the two roles above requires
their Keycloak user to already exist — by Keycloak's own bootstrap, or by
someone signing in once through a path this module does not provide. See
ADR 0025's own "What this does not decide": client-realm provisioning stays
with the control plane for now, and whether operator *accounts* are
provisioned the same declarative way this module provisions their grants is
a question this Application leaves open, not one it has quietly answered by
omission.

This Application learned that the expensive way. Its first roster named the
product owner, who has no master-realm account, and the first three runs on
LucentRoot (2026-09-22) failed at plan time on exactly that lookup — nothing
else was harmed, which is what failing at plan time is for, but the
Application sat `Degraded` until the roster said something true. Karo's
answer is brokering rather than creating: its master realm trusts Entra as an
OIDC identity provider, a mapper turns an upstream role into realm authority
at each sign-in, and when no upstream is configured "the bootstrap admin
remains the way in". Whether SaaS Fabric's operators arrive the same way, and
from which upstream, is the decision ADR 0025 leaves open; until it is made,
the bootstrap administrator is the operator, and the roster says so.

## Adopting LucentRoot's hand-made history

LucentRoot's master realm predates this module: its `saas-fabric-console`
client and its `fabric-operator` role were created by hand before ADR 0025,
the plan this module replaced. The provider does not adopt an existing
object on create — attempting to create either again would fail with `409`
on this Job's very first run. `var.adopt_existing`, wired to this provider's
own `import` argument on exactly those two resources (`keycloak_role.fabric_operator`,
`keycloak_openid_client.console`; never `keycloak_openid_client.gateway`,
which never existed by hand — see `base/module/main.tf`'s own comments on
each), is what tells the provider to look the existing object up and adopt
it instead of attempting a second create. LucentRoot's
[`overlays/lucentroot/master-instance-config.yaml`](overlays/lucentroot/master-instance-config.yaml)
sets `adoptExisting: "true"` with this history recorded in a comment; a fresh
environment has none of it and leaves the default, `false`, so both
resources are created outright there.

**After the first successful run the value is history, not behaviour.**
Whether a resource was imported or created, the state this module holds
afterward is identical, and every later apply manages the same object either
way — there is no ongoing branch in what this module does depending on how
an object first entered its state.

### What the first run on LucentRoot actually does

Not "converge, then the first sign-in is the observation" — that undersells
what has to happen in order on a realm with this history. In order:

1. **Adopt.** `fabric_operator` and `console` are imported from their
   existing, hand-made objects — no create, no `409`, and no revocation of
   whatever the hand-made client's redirect URIs or the hand-made role's
   description already held; the module's own declared arguments become
   authoritative for them from this run on.
2. **Create.** `saas-fabric-gateway` did not exist by hand, so this run
   creates it outright, with the secret
   [`../master-instance-credential`](../master-instance-credential/)
   generated ahead of this Application's own sync.
3. **Grant.** Each declared operator (today: the bootstrap administrator —
   see "Who the operators are" above) is looked up and, if found, granted
   `fabric-operator` and master-realm `admin`; on LucentRoot both are grants
   it already held, so this step changes nothing and owns what it finds.
4. **Sign in.** On a fresh environment the wave order holds this whole chain
   before the first sign-in: the app-of-apps does not create
   [`../saas-fabric-control-plane`](../saas-fabric-control-plane/) (wave `40`)
   until this Application (wave `30`) is Healthy, and this Application is
   Healthy only once 1–3 have succeeded and the Job is `Complete`. On an
   environment that already exists the gate is weaker — see "What the wave
   gate holds, and when" under "The Job" — so the first sign-in through the
   gateway is the end-to-end observation of the whole chain only once this
   Application reports Healthy, not merely once wave `40` has synced.

## The Job

The repository's first Job — a **normal** `batch/v1` `Job`, not a sync hook.
An earlier draft used a hook (`argocd.argoproj.io/hook: Sync`,
`hook-delete-policy: BeforeHookCreation`), and it was wrong: Argo CD excludes
hook resources from an Application's health rollup
([argo-cd#9861](https://github.com/argoproj/argo-cd/issues/9861)), and
`argocd/runtime/application-health.yaml` gates every wave on the *child
Application's* health — so a hook Job, running or even failed, would have
let this Application report Healthy the moment its RBAC and `ConfigMap`
landed, and wave `40` would have proceeded regardless. There was no gate at
all, silently.

An ordinary Job has no such gap. Argo CD's built-in health check for `Job` is
Progressing while it runs, Healthy once it succeeds, Degraded once it
exhausts `backoffLimit` — so this Application's health *is* the Job's, which
is the one thing wave `40` can gate on at all. A failed convergence leaves
this Application Degraded, loudly — the property the hook never gave it.
What that gate holds back, and when, is the next section.

### What the wave gate holds, and when

Sync waves order what one sync applies. The app-of-apps applies the child
`Application` objects, so on a fresh environment it does not create
[`../saas-fabric-control-plane`](../saas-fabric-control-plane/) (wave `40`)
until this Application (wave `30`) is Healthy — until the Job has
succeeded — and that is the order ADR 0025's "in wave order" describes. On
an environment that already exists both `Application`s already do; each
carries `automated` sync and reconciles its own source on its own, and
nothing makes one wait on a sibling's health, because a change under
`applications/core/saas-fabric-control-plane/` changes nothing in the
parent's own manifests for a wave to order. Observed on LucentRoot,
2026-09-22: this Application went Degraded at 09:06:22Z on the roster
failure described under "Who the operators are", and
`saas-fabric-control-plane` synced anyway at 09:06:57Z, attaching its OIDC
`SecurityPolicy` to a gateway client the Job's first run had already
created — harmless that day, and not a gate. A failed convergence on an
existing environment is therefore loud but does not hold wave `40` back. The
mechanism that would — an ApplicationSet `RollingSync` strategy, which
sequences the children by step and waits on each step's health on every
sync, not only at creation — is the one `argocd/applicationsets/README.md`
set aside, and is #44 rather than a claim made here.

**`argocd.argoproj.io/sync-options: Replace=true,Force=true`** — both, not
`Replace=true` alone — is what makes an ordinary Job re-run on every sync in
the hook's place. `spec.template` is immutable on a plain Job: `Replace=true`
by itself makes gitops-engine issue a `kubectl replace` (a `PUT`), which the
API server refuses on an immutable-field change exactly as it would refuse a
`PATCH`. `Force` is not a fallback tried after that refusal — gitops-engine
derives `Replace` and `Force` independently from this one annotation and,
with both set, calls `ReplaceResource` with force requested from the start:
`kubectl replace --force` deletes the object and creates it again
unconditionally, the same two steps every time, not a retry path taken only
when a plain replace would have failed. This is Argo CD's own documented
recipe for a Job meant to run every sync, and it is also, plainly, what
replaces `BeforeHookCreation` — that delete policy was a hook-only default,
gone along with the hook annotations.

**This only re-runs the Job because Argo CD applies every resource in this
Application on every sync, not only the ones that changed.** A sync that
changes nothing in Git still re-applies this Job — and, because of the
annotation above, gets a delete-and-recreate rather than a no-op patch —
alongside every other resource here. `ApplyOutOfSyncOnly=true` must never be
added to this Application's `syncPolicy`: it makes Argo CD skip resources
showing no diff, which is exactly the case this Job's reconvergence exists to
cover. See `application.yaml`'s own comment.

**`activeDeadlineSeconds: 600`** bounds a hung Keycloak. Without it, a Job
that never returns holds this Application Progressing indefinitely rather
than Degraded — which, where the wave gate holds (a fresh environment; see
"What the wave gate holds, and when"), blocks wave `40` exactly as hard as a
real failure, but without ever saying so.

**Re-running is safe because the apply is idempotent, and the drift check is
the proof.** `base/module/apply.sh` ends every run with
`tofu plan -lock-timeout=60s -detailed-exitcode` against the state the same
run just wrote. Exit `2` — drift remains — fails the Job, and with it this
Application's health. Nothing about running the module twice, or a hundred
times, moves the realm further from what `main.tf` declares; a sync that
changes nothing in Git finds nothing to apply.

**A Job replaced mid-run leaves the state Lease held.** The delete half of
`Replace=true,Force=true` removes the Pod that held the `kubernetes`
backend's state lock without releasing it first — Argo CD does not wait for
an in-flight apply to finish before replacing the Job underneath it.
`apply.sh` passes `-lock-timeout=60s` on both `apply` and the closing `plan`,
which gives an apply from moments ago a real chance to finish and release the
lock on its own; if it does not — the previous run was truly killed
mid-write, not merely slow — this run still fails, loudly, and the state
Lease needs a manual

```console
$ kubectl -n master-instance-state get lease lock-tfstate-default-master-instance
$ tofu -chdir=<a checkout of base/module, with backend "kubernetes" configured for this cluster> force-unlock <lock ID from the Lease>
```

before the next sync can proceed on its own.

### The admin credential reaches the Job as an ordinary mirrored Secret

`keycloak-admin` lives in `identity`
([`../keycloak-credentials`](../keycloak-credentials/)); this Job runs in
`operator-system`. An earlier draft bridged that with an init container: a
`Role`/`RoleBinding` in `identity` letting the Job's own `ServiceAccount`
`get` the Secret, read over the Kubernetes API by a `rancher/kubectl`
container before the real work started. That pulled a third-party image into
the render for a value External Secrets — already deployed here — can mirror
on its own, so it is gone.
[`overlays/lucentroot/keycloak-admin-mirror.yaml`](overlays/lucentroot/keycloak-admin-mirror.yaml)
replaces it with the ESO Kubernetes-provider shape
(https://external-secrets.io/latest/provider/kubernetes/): a `SecretStore` in
this namespace whose `provider.kubernetes` reads `identity`, authenticating
as a `ServiceAccount` this module creates for exactly that; a `Role`/
`RoleBinding` in `identity` granting that `ServiceAccount` `get` on the one
named Secret; a `ClusterRole`/`ClusterRoleBinding` granting it `create` on
`authorization.k8s.io` `selfsubjectrulesreviews`, which the provider's
`Validate()` calls before it will report the store `Ready` (**not** a
namespaced `Role` rule — `SelfSubjectRulesReview` is cluster-scoped, and a
namespaced Role naming it is syntactically legal but never matched by the
authorizer, since there is no namespace for the request to be evaluated
against; left that way, this `SecretStore` would never reach `Ready` and
Argo CD's own `SecretStore` health check would report this Application
Degraded at wave `30`, from a cause nothing in the Job's own logs would
explain); and an `ExternalSecret` mirroring `username`/`password` into an
ordinary Secret here, `refreshInterval: "0"` for the same reason
`../keycloak-credentials`'s own admin password uses it — the mirror should
never race a regeneration of the Secret it mirrors. The Job then reads
`KEYCLOAK_USER`/`KEYCLOAK_PASSWORD` with an ordinary `secretKeyRef` — no init
container, no `emptyDir` handoff, no extra image.

**`auth.serviceAccount`, and why the reader ServiceAccount lives beside the
`SecretStore` rather than beside the Secret.** The provider's schema does
carry a `namespace` field there, but it is honoured only when the store is a
cluster-scoped `ClusterSecretStore`; this one is an ordinary namespaced
`SecretStore`, which already has exactly one namespace, so the ServiceAccount
it authenticates as is implicitly that namespace's own. That is why the
reader `ServiceAccount` is declared in `operator-system`, beside the
`SecretStore`, and the `Role` granting it access is what crosses into
`identity` instead.

**The one RBAC surface this file does not grant is checked, not assumed.**
The External Secrets controller (`external-secrets` in `secrets`) needs its
own cluster-wide permission to mint a token for this module's `ServiceAccount`
via the `serviceaccounts/token` subresource, since the controller runs in a
namespace neither this `ServiceAccount` nor the Secret it reads is in. This
was confirmed against the pinned chart's own render
(`.render/lucentroot/applications/external-secrets.yaml`), which already
grants `ClusterRole/external-secrets-controller` unscoped `create` on
`serviceaccounts/token` — the Kubernetes provider's whole model rests on the
controller being able to impersonate any `ServiceAccount` an operator points
it at, with the real security boundary drawn by what that `ServiceAccount`
is itself authorized to do (the `Role` and `ClusterRole` above), not by which
`ServiceAccount`s the controller may impersonate.

Everything at wave `-1` within this Application (the mirror's `ServiceAccount`,
`Role`, `RoleBinding`, `SecretStore` and `ExternalSecret`) exists a wave
ahead of the Job's own default wave `0` for the same reason
`../saas-fabric-control-plane/overlays/lucentroot/oidc.yaml` used to sit a
wave ahead of its own `SecurityPolicy`: a shared wave gets resources applied
together, not resolved before each other, and only a wave boundary makes
Argo CD wait.

### State

`backend "kubernetes"` (`base/module/main.tf`), a Secret named
`tfstate-default-master-instance` — state for exactly this one module,
outliving the pod that wrote it. The `kubernetes` backend also coordinates
locking through a `coordination.k8s.io` `Lease`,
`lock-tfstate-default-master-instance`.

**In its own namespace, `master-instance-state`
([`overlays/lucentroot/state-namespace.yaml`](overlays/lucentroot/state-namespace.yaml)),
not `operator-system`.** `tofu init` calls the backend's own `Workspaces()`
unconditionally (OpenTofu 1.12.6's `meta_backend.go`, `selectWorkspace`) —
even though this module never selects a workspace — which issues a
label-selector `list` against Secrets in the backend's namespace. `list`
cannot be scoped by `resourceNames` and returns whole object bodies, not
names; granting it in `operator-system` would let this identity read the
*content* of every Secret there, including `saas-fabric-gateway-oidc` and the
mirrored `keycloak-admin`. A namespace holding nothing but this state is what
keeps that unavoidable `list` from granting anything beyond itself.

[`overlays/lucentroot/state-rbac.yaml`](overlays/lucentroot/state-rbac.yaml)
grants the Job's `ServiceAccount` (which stays in `operator-system`; the
`RoleBinding` names it across the namespace boundary, the same shape
`keycloak-admin-mirror.yaml` uses) `get`/`update`/`delete` on the two named
objects, unscoped `create` on both kinds (Kubernetes cannot scope a create by
`resourceNames`), and unscoped `list` on secrets — the one grant this whole
namespace exists to contain. No `watch`, no `patch`: the backend only ever
`get`s and `update`s whole objects, never a partial merge or a watch loop.

**Losing this state does not lose the realm, but `prevent_destroy` is not
why — it blocks a destroy, and a plan against empty state never proposes
one.** Against lost state, `main.tf` sees no prior record of any of these
objects, so the plan for each is a plain *create* — silent, and each create
either finds the object already there (a `409`) or, for the three resources
below with their own import mechanism, imports it instead of attempting to
create it at all:

- `keycloak_realm.master` recovers unconditionally. Its `import` block
  (this file's own OpenTofu `import`, unconditional — see "The realm itself"
  above) runs on every apply regardless of state, so losing state and
  re-applying imports the realm again rather than erroring.
- `keycloak_role.fabric_operator` and `keycloak_openid_client.console`
  recover **only where `var.adopt_existing` is already `true` for that
  environment** — LucentRoot today. `import = var.adopt_existing` behaves
  the same way after state loss as it does on a genuinely first run: `true`
  adopts the existing object, `false` attempts a create. An environment
  whose first successful run left `adopt_existing` at `false` (nothing
  predated it there) does **not** recover these two automatically after
  losing state — the objects it created earlier are still in Keycloak, and
  the create this plan proposes finds them and `409`s.
- `keycloak_openid_client.gateway` never recovers automatically, in any
  environment, by design: it is never adopted (see `main.tf`'s own comment
  on it), so losing state always means a `409` here.

So the honest count is: on LucentRoot, one `409` after state loss (the
gateway client) — everything else recovers on its own. On an environment
that was never adopted, three (`fabric_operator`, `console`, `gateway`).
**The fix in either case is an OpenTofu `import` block added for that one
environment and that one resource, applied once, not a person recreating or
editing anything in Keycloak's admin console** — the same declarative
mechanism `keycloak_realm.master` already uses unconditionally, added
narrowly and temporarily (or `tofu import` run directly against the same
state) rather than left in place, since these resources are not meant to be
adopted in the ordinary course. `keycloak_user_roles.operator` needs no such
step: it has no `lifecycle` block and no import concern, because a role
*grant* is not an object with an identity of its own — losing state and
re-applying simply re-grants the same two roles to the same operators, which
is idempotent, not destructive.

#### Resources unsafe to prune

`master-instance-state` (the `Namespace`, per
[`overlays/lucentroot/state-namespace.yaml`](overlays/lucentroot/state-namespace.yaml))
carries `Prune=false` for exactly this reason: removing this Application, or
dropping that manifest from Git, must not take `tfstate-default-master-instance`
with it. A pruned Namespace deletes everything inside it, state Secret
included — which is a worse version of the state loss described above, with
no Git history to recreate the manifest from.

### The image

`ghcr.io/opentofu/opentofu:1.12.6`, not the `-minimal` variant. OpenTofu
publishes its images two ways: an ordinary image with an ordinary shell, and
a distroless `-minimal` variant with none — confirmed by the fact that the
application repository's own harness
(`hosting/src/SaaSFabric.Aspire.Hosting/Templates/Dockerfile`) only ever
*copies the `tofu` binary out of* `-minimal`, into an Alpine image it builds
itself, rather than running `-minimal` directly. `base/module/apply.sh` is
invoked as `/bin/sh /module/apply.sh` — it needs the shell `-minimal` does
not carry. The tag was confirmed to exist, on both `linux/amd64` and
`linux/arm64`, with `docker manifest inspect ghcr.io/opentofu/opentofu:1.12.6`
before it was pinned here.

The image declares no user of its own — nothing in it anticipates the
non-root, read-only posture below — so `HOME`, `TMPDIR` and
`TF_PLUGIN_CACHE_DIR` are all redirected onto the one writable place this Pod
has, the `work` `emptyDir` the module is copied into, rather than left to
default onto a root filesystem this container cannot write to.

### Pod security

Matches [`../saas-fabric-control-plane/base/deployment-api.yaml`](../saas-fabric-control-plane/base/deployment-api.yaml):
pod `runAsNonRoot: true`, numeric `runAsUser`/`runAsGroup: 65532` (the
kubelet cannot check a *name*, and this image, unlike the control plane's own
distroless one, declares none at all), `seccompProfile: RuntimeDefault`;
container `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
`capabilities.drop: [ALL]`. The only writable path in the container is the
`work` volume; `/module` is the `ConfigMap` mount, read-only regardless of
how it is declared, which is why `apply.sh`'s first act is copying the
module there before running `tofu` against it.

## Rotation

Regenerating `saas-fabric-gateway-oidc` (deleting the generated Secret in
[`../master-instance-credential`](../master-instance-credential/) so its
generator produces a new value) changes no manifest in this Application, so
nothing here re-runs on its own — a generator's output changing is not a
Git change, and Argo CD reconciles Git. The real rotation path is two steps:
delete the generated Secret, then force a refresh and sync of
*this* Application (`argocd app sync master-instance`, or a hard refresh
through the UI). `Replace=true,Force=true` on the Job is what makes that
sync actually re-run the convergence rather than finding an already-Synced
Job and leaving it alone — see "The Job" above for why both annotations, not
`Replace=true` on its own, are what that requires. The Job that sync
replaces sets the new value on the Keycloak client, and the `SecurityPolicy`'s
own `ExternalSecret`
([`../master-instance-credential`](../master-instance-credential/)) already
holds the same regenerated value by the time an operator's browser next
needs it.

## Sync timing

Argo CD syncs on a manifest change, on its own reconciliation interval, or on
request — never on a Keycloak-side change by itself. An operator who edits
something inside `main.tf`'s declared surface directly in Keycloak (a
redirect URI on `saas-fabric-gateway`, say) will see it re-converged back to
what Git declares on this Application's *next* sync, not the instant they
made the change.

## Rollout order

1. [`../master-instance-credential`](../master-instance-credential/) syncs
   (wave `10`) — the gateway's secret exists.
2. Keycloak (wave `20`) is Healthy — its admin REST API answers.
3. This Application syncs (wave `30`): the mirror at wave `-1` lands first,
   then the Job runs — adopt, create, grant, in that order (see "What the
   first run on LucentRoot actually does" above) — and its own drift check
   proves convergence. Argo CD reports this Application Healthy once the
   ordinary `Job` itself reaches `Complete`.
4. [`../saas-fabric-control-plane`](../saas-fabric-control-plane/) (wave `40`)
   syncs. Its `SecurityPolicy` now targets a client that already exists, with
   a secret that already matches — nothing about its own sync waits on a
   human between steps 3 and 4. On a fresh environment step 4 does not begin
   until step 3 is Healthy; on an existing one it syncs on its own schedule
   (see "What the wave gate holds, and when").
5. **The first sign-in through the gateway is the end-to-end observation of
   the whole chain above** once this Application reports Healthy, the same
   way it is for a client instance under Karo's model — not merely of the
   gateway client's own secret matching.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [`../master-instance-credential`](../master-instance-credential/) | `10` | the gateway's secret this Job sets on the client |
| [Keycloak](../keycloak/) | `20` | the admin REST API this Job drives |
| [`../keycloak-credentials`](../keycloak-credentials/) | `10` | the bootstrap administrator this Job authenticates as |
| [External Secrets](../external-secrets/) | `0` | mirrors the admin credential into this namespace |

## Environments

LucentRoot only, listed in `environments/lucentroot/kustomization.yaml`
alongside `../tailscale` and `../operator-access` rather than in
`applications/core/kustomization.yaml`. Production has no operator plane yet
(see [`../saas-fabric-control-plane`](../saas-fabric-control-plane/)'s own
overlay), so it has no gateway sign-in for this Application to provision.

## Configuration owned by this repository

- the module itself (`base/module`): which clients, which role, which grants,
  and the one realm attribute it owns;
- the OpenTofu and provider versions, and the provider's locked checksums;
- the Job's resources, `backoffLimit`, `activeDeadlineSeconds`, security
  context, and the RBAC each identity needs.

## Configuration expected from outside this repository

Nothing. That is the point. The two inputs a person supplies —
[`overlays/lucentroot/master-instance-config.yaml`](overlays/lucentroot/master-instance-config.yaml)'s
operator roster and its `adoptExisting` history — are lines in a file this
repository already owns, read and applied by this Job, not a click in
Keycloak.
