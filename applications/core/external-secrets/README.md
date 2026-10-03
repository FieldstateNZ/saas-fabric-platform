# External Secrets

| | |
|---|---|
| Product | External Secrets Operator |
| Upstream project | https://github.com/external-secrets/external-secrets |
| Helm chart source | https://charts.external-secrets.io |
| Chart version (pinned) | `2.9.0` |
| Application version | `v2.9.0` |
| Licence | Apache-2.0 |
| Namespace | `secrets` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `10` |

## Why it exists in SaaS Fabric

OpenBao is the platform's secrets *authority*. This is the mechanism that gets a
secret from OpenBao into a pod. Without it every credential has to be created by
hand with `kubectl`, which is how a platform ends up with secrets nobody can
account for.

It is core because SaaS Fabric's own credentials, and every client credential
that follows, need a delivery path that is not a person running a command.

## The contract for a platform workload

Put the values at `secret/platform/<name>` in OpenBao. Declare an
`ExternalSecret` referencing the `openbao` store, and the operator materialises
a Kubernetes Secret the Deployment reads with `envFrom`:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: my-app-env
spec:
  secretStoreRef:
    name: openbao
    kind: ClusterSecretStore
  target:
    name: my-app-env
    creationPolicy: Owner
  dataFrom:
    - extract:
        key: platform/my-app
```

`dataFrom.extract` takes every key at the path, so **adding a variable means
writing it to OpenBao and changing nothing in Git**. That is the property worth
protecting: the repository describes where secrets come from, never what they
are.

## This store is bounded, deliberately

A cluster-wide store authenticated as one service account with read across the
whole mount would make the security boundary "anyone who can create an
`ExternalSecret` can read anything". That is tolerable on a single-tenant box
and completely wrong for the client model SaaS Fabric is being built for, so it
is not the contract established here.

Two bounds, and both matter:

| Bound | Effect |
|---|---|
| `conditions` on the [`ClusterSecretStore`](../secret-store/) | only namespaces labelled `fieldstate.nz/layer: platform` may reference it |
| the OpenBao policy | the operator's token can read `secret/platform/*` and nothing else |

The namespace label is applied by each Application's
`managedNamespaceMetadata`, and platform-owned labels are deliberately never
applied to client-owned resources. A `client-acme` namespace therefore cannot
reference this store, and even if it could, the token behind it cannot read
`secret/clients/...`.

**That label is now load-bearing.** It was descriptive when it was only used for
inventory; it is part of a security boundary now. Do not apply it to a namespace
this repository does not own.

### Client secrets are a separate mechanism

Not this store with a wider policy. A client gets its own `SecretStore` in its
own namespace, bound to a client-scoped OpenBao role over its own path:

```text
client-acme namespace
        ↓
SecretStore in client-acme
        ↓
OpenBao role over secret/clients/acme/*
```

All three are created by client provisioning, alongside the client's realm,
database and routes. `secret/clients/` is reserved for exactly that and is
unreadable from the platform store.

### The split is about purpose, not about which namespace asks

This is the part that is easy to get wrong, because the namespace bound above
makes it look like a location rule. It is not. **One workload can legitimately
need secrets from both scopes**, and running in a platform namespace does not
make everything it reads a platform secret.

Superset is the clearest example, if it is ever adopted:

| Secret | Scope | Path |
|---|---|---|
| Superset's admin credential | platform | `secret/platform/superset/...` |
| Its metadata database connection | platform | `secret/platform/superset/...` |
| Its signing / secret key | platform | `secret/platform/superset/...` |
| Its OAuth client secret | platform | `secret/platform/superset/...` |
| Credentials for Superset to read **Acme's** data | **client** | `secret/clients/acme/...` |

Everything Superset needs *to be Superset* is platform. Everything it needs *to
reach one client's resources* is that client's, and comes through that client's
store — not this one, and not by widening this one.

Ask which one a secret is:

```text
does the platform need this to run the component?      → secret/platform/...
does it only exist because a particular client does?   → secret/clients/<client>/...
```

The runtime bound already enforces the answer — the platform token cannot read
`secret/clients/*` — but the design decision has to be made before that, when
someone chooses where to write the value.

`scripts/check.py` also refuses it at build time, which is only worth anything
if it cannot be walked around using ordinary ESO syntax. It covers:

| Shape | Why it needs covering |
|---|---|
| `ExternalSecret` and `ClusterExternalSecret` | the latter nests its spec under `externalSecretSpec`, so reading `spec.*` matches nothing |
| `data[].remoteRef.key` | an exact key |
| `dataFrom[].extract.key` | an exact key |
| `dataFrom[].find.path` | selects **everything** beneath a prefix, so it is the widest of the three |
| `sourceRef.storeRef` per entry | an entry may name its own store, overriding the top-level one for that entry alone |

A client path reached through a *client* store is fine and is not flagged —
that is the intended pattern. What is refused is reaching client paths through
**this** store.

The same five shapes are refused for SaaS Fabric's own instance partition,
`platform/saas-fabric/instances/`, compared **as a path, segment by segment**:

| Remote path | Verdict | Why |
|---|---|---|
| `platform/saas-fabric/instances/master/…` | refused | beneath the partition |
| `platform/saas-fabric/instances` (the root itself) | refused | see below |
| `find` over `platform/saas-fabric`, `platform`, or no path | refused | the subtree contains the partition |
| `platform/saas-fabric/instances-public/…` | allowed | a sibling, not a descendant — a string prefix would have refused it |
| `platform/my-app` | allowed | an ordinary platform secret |

### Exact-root semantics

The policy's glob `secret/data/platform/saas-fabric/instances/*` matches what
lies *beneath* the root. It does not match a secret written at the bare root
path `secret/platform/saas-fabric/instances` itself. Nothing legitimate is
written there — the control plane's own grant is `instances/master/*` — so
rather than leave that one path as the one the checker also ignores, the
checker refuses the root as well: stricter than the ACL, never wider. Do not
write a secret at the partition root; if one ever appears there, the ACL does
not protect it and only this check does.

## Authentication

The operator authenticates to OpenBao with the Kubernetes auth method, minting
its own service account token and exchanging it for an OpenBao token. There is
no static credential anywhere — nothing to leak, nothing to rotate.

The `secrets` namespace holds both halves, so this traffic never leaves the
node.

## Required one-time OpenBao configuration

The auth method has to exist before the store can work. It is an operator step
because it happens once, against a freshly initialised OpenBao:

```bash
bao auth enable kubernetes
bao write auth/kubernetes/config \
  kubernetes_host=https://kubernetes.default.svc
bao policy write platform-secrets - <<'POLICY'
path "secret/data/platform/*"     { capabilities = ["read"] }
path "secret/metadata/platform/*" { capabilities = ["read", "list"] }
path "secret/data/platform/saas-fabric/instances/*"     { capabilities = ["deny"] }
path "secret/metadata/platform/saas-fabric/instances/*" { capabilities = ["deny"] }
POLICY
bao write auth/kubernetes/role/external-secrets \
  bound_service_account_names=external-secrets \
  bound_service_account_namespaces=secrets \
  policies=platform-secrets ttl=1h
```

The policy grants read on the **platform prefix only**. A new platform workload
still needs no OpenBao policy change — its secret goes under
`secret/platform/<name>` and the existing grant covers it — while
`secret/clients/` stays outside what this token can reach at all.

One path beneath the platform prefix is denied: SaaS Fabric's own instance
partition, `secret/platform/saas-fabric/instances/`. The control plane keeps its
integration credentials there — its Git applications' keys, and the registry
tokens an operator registers — and none of them is ever delivered to a workload
(ADR 0026, application repository). OpenBao ranks a more specific path above
a glob, so within this one policy the `deny` on `instances/*` outranks the
`read` on `platform/*`. The same rule cuts the other way: a path deeper
than `instances/*` granted with any capability but `deny` would outrank the
deny — whether that rule sits in `platform-secrets` itself, in another policy
the role binds, or in a policy the token carries through identity or group
membership that the role never mentions. The deny therefore holds for the
External Secrets identity only if nothing in its *effective* policy set
grants anything more specific beneath the partition, and that is a property
of the running instance, not of this text.

`scripts/check.py` asserts two things about this, and it is worth being exact
about what they are. It verifies that LucentRoot's `initialize` stanza
declares this deny — `deny`, alone, on both partition paths, inside the
`platform-secrets` policy text of a *writing* request for
`sys/policies/acl/platform-secrets`, with a writing request for the External
Secrets role that literally binds `platform-secrets` (through
`token_policies` or its deprecated alias `policies`, never both, never a
reference); a deny in a comment, in some other policy, or in a request that
does not write does not count, and a declaration it cannot read without
guessing — an unsupported string escape, a path rule mixing the legacy
`policy` field with `capabilities`, a path declared twice — fails the check
rather than passing it. And it refuses any `ExternalSecret` in this
repository that could select a path beneath the partition. **Neither is the
runtime boundary, and neither is a statement about the effective ACL.** The
checker reads two declarations; it does not evaluate the token's
capabilities across its effective policy set — the policies the role binds,
`platform-secrets` itself, or any attached through identity or group
membership — it cannot see inside a
running OpenBao, and it cannot bind an `ExternalSecret` created by hand or by
anything outside this repository. That is a deliberate boundary of the
checker, not a check it passes. The running ACL is the boundary; the checker
is defence in depth; whether the running identity is actually denied is
established only by the verification in the procedure below.

That is the trade worth making: convenience within the platform's own space, and
a hard wall at the tenancy boundary.

Full procedure in [docs/bootstrap.md](../../../docs/bootstrap.md).

## Updating the policy on an initialised instance

OpenBao runs the `initialize` stanza **once, at first start**. An instance
initialised before the partition deny was added to `platform-secrets` is still
running whatever policy first start wrote. Editing Git changes nothing there,
Argo CD reports `Synced` regardless, and `scripts/check.py` cannot tell — it
verifies the stanza, not the instance. Until the running policy denies the
partition, anyone able to create an `ExternalSecret` in a platform namespace
could read it through this store, whatever this repository's checker says.

Whether a given instance is in that state is **not known from this
repository**. The procedure below starts by finding out, and it is the way to
correct it without a rebuild: an in-place write of one policy, under its
existing name, read back and verified. It is a separately authorised,
hands-on operation — not part of CI, and not something merging a change to the
stanza performs.

This procedure never authorises destroying or rebuilding the instance. The
design notes that call LucentRoot's OpenBao disposable describe how it was
designed to be rebuilt; they are not evidence that a rebuild or a restore of
this instance has been proven, and nothing below relies on either.

### Prerequisites

- **Authorisation recorded**: who, when, which instance, and the commit whose
  stanza is the intended policy.
- **Administrative access through the existing path only**: the `operator`
  role and `platform-admin` policy, reached by exchanging a short-lived
  ServiceAccount token as described in
  [OpenBao: Logging in as an operator](../openbao/README.md#logging-in-as-an-operator).
  `sys/policies/acl/*` is within that policy's grant. No root token is
  generated, recovered or needed, and nothing about the role or its policy is
  changed. No token of any other identity is created by this procedure.
- **A way to inspect the real External Secrets identity without creating
  authority**, established beforehand by a separately authorised operator
  using the organisation's existing, approved procedure for obtaining and
  validating the *accessor* of a token that identity already holds. Steps 2
  and 5 use that accessor only to read what the token is
  (`bao token lookup -accessor`) and what it may do
  (`bao token capabilities -accessor`); the lookup output names the role,
  service account and policies, never the token value. The accessor is
  operational security metadata: it is held for the duration of this
  procedure, protected like any other handle onto a live identity, and not
  pasted into tickets, chat or the record beyond the fact that one was
  validated. This document deliberately gives no recipe for finding
  accessors. What is not acceptable: enumerating accessors broadly, copying
  a token value from anywhere, creating a new token, recovering a token, or
  any step that expands what any identity can do.
- **Backup that is actually a backup.** A Raft snapshot
  (`bao operator raft snapshot save <file>`) counts only when all of the
  following hold, and each is recorded: it is written to an approved,
  protected, off-host destination, not left on the node or the operator's
  laptop; its readability and integrity are verified after it is written, by
  whatever means the recovery plan specifies; the seal and recovery
  prerequisites for restoring it are known and available — on LucentRoot the
  snapshot is encrypted under the static seal key in the `openbao-seal`
  Secret, and a snapshot without the key that sealed it restores nothing; and
  a restore plan for it exists and is approved. Taking a snapshot is not
  proof that recovery works. If these cannot all be met, this procedure does
  not proceed; the gap is recorded and escalated.
- **The current policy text saved** (step 1 — policy text contains no
  credential), with this understood: if that text lacks the deny, writing it
  back later *reopens the partition*. See "If something goes wrong".
- **The intended policy text**, taken verbatim from the `platform-policy`
  request in `environments/lucentroot/config/openbao.yaml` — the text CI
  verifies — and saved as `intended.hcl`. Not retyped.
- **Unique names for the step 6 probe**, chosen beforehand: two
  `ExternalSecret` names in namespace `secrets`, each also the name of its
  own target Secret, and one OpenBao marker path beneath the partition. Make
  them unmistakably the probe's — a date and a random suffix, for example
  `probe-20261004-k7x2q` and `probe-20261004-k7x2q-find`, with the marker at
  `secret/platform/saas-fabric/instances/probe-20261004-k7x2q/marker`. Step 6
  verifies each one is absent before anything is created, keeps a receipt of
  what it created, and deletes only what the receipt names. Names are never
  reused across runs.

### Procedure

1. **Read the current policy and the role's binding.** All non-secret.

   ```bash
   bao policy read platform-secrets > current.hcl
   bao read auth/kubernetes/role/external-secrets
   ```

   The role output shows `token_policies` and, if it was written through the
   deprecated field, `policies`. Record every policy name in either. The
   list must include `platform-secrets`; if it does not, stop — the role is
   bound to something else and this procedure is aimed at the wrong policy.

2. **Determine the identity's effective policy set, and review all of it.**
   This is the policy review the checker cannot do, and it is wider than
   step 1's list. The role names what a *new* login receives; the token
   External Secrets actually holds can carry more. Using the accessor from
   "Prerequisites", run `bao token lookup -accessor` and take the complete
   set from its output: `policies` (the token policies, expected to match
   step 1) and `identity_policies` (attached through an entity or group that
   the Kubernetes auth mount's alias belongs to, which the role never
   mentions), plus any other policy-bearing field the output shows.
   `default` is one of them and is reviewed like the rest. If that output
   cannot be obtained, the accessor does not resolve to the
   `external-secrets` role and service account in namespace `secrets`, or
   the policy fields are absent or ambiguous, **stop**: the effective set is
   unknown, and reviewing an unknown set proves nothing.

   Read every policy in the set — `platform-secrets` **itself included**
   (`bao policy read <name>`) — and look for any path rule beneath
   `secret/data/platform/saas-fabric/instances/` or
   `secret/metadata/platform/saas-fabric/instances/` that is more specific
   than `instances/*` and carries any capability other than `deny`. OpenBao
   ranks the more specific path whichever policy it is in: such a rule in
   `platform-secrets` alongside the deny, or in an identity or group policy,
   outranks the glob deny this procedure writes. Expected: none. Record the
   complete set, every name reviewed and the result. If one is found, stop:
   writing `platform-secrets` will not close the partition, and the finding
   goes back to whoever authorised this.

   This is a reading of policy text, not a proof over every path. Step 5
   samples capabilities on representative paths and cannot stand in for it;
   the two together are the verification this document offers, and
   `scripts/check.py` is narrower than either — it reads the stanza in Git
   and evaluates no ACL.

3. **Review the diff.** Expected: exactly the two `deny` lines added, and
   nothing else.

   ```bash
   diff current.hcl intended.hcl
   ```

   Any other difference means the instance has diverged from Git in a way
   worth understanding before writing over it. Stop and record it. If the
   diff is empty, the running policy already carries the deny; skip to step 5
   to prove it rather than assume it.

4. **Write the policy in place, then read it back.** Same name, no new
   policy, no role change.

   ```bash
   bao policy write platform-secrets intended.hcl
   bao policy read platform-secrets | diff intended.hcl -
   ```

   Empty output or stop.

5. **Read the effective capabilities of the real External Secrets token**, by
   the accessor established under "Prerequisites". The token value is never
   printed, no token is created, and nothing is revoked.

   ```bash
   bao token lookup -accessor "$accessor"      # confirm role external-secrets, SA external-secrets, ns secrets
   for p in \
     secret/data/platform/saas-fabric/instances/master/git/app-private-key \
     secret/data/platform/saas-fabric/instances/master/integrations/registries/example \
     secret/metadata/platform/saas-fabric/instances/master/git/app-private-key \
     secret/metadata/platform/saas-fabric/instances/master/integrations/registries/example \
     secret/data/platform/example \
     secret/metadata/platform/example
   do printf '%s: ' "$p"; bao token capabilities -accessor "$accessor" "$p"; done
   ```

   Expected: `deny` for each of the four partition paths and `read` (and
   `list` on the metadata path) for the two platform paths. This reads
   capabilities, not values, and it is a *representative* sample: a `deny`
   on these paths does not prove every path beneath the partition, which is
   why step 2's review of the whole effective policy set is not optional. The
   token External Secrets holds was issued before the write; a token carries
   policy *names*, and the capability output is expected to reflect the
   policy text as it is now. If it still shows `read` on a partition path
   after step 4 read back cleanly, stop and record it rather than reason
   about caching.

6. **Probe the denial end to end** with the real External Secrets
   identity, against synthetic, non-secret markers — never against a real
   credential, and never under `instances/master/`, which is the control
   plane's own grant. One marker is tested by exact key (the data path) and
   by prefix search (the metadata list path).

   This step is a plan, not a script, on purpose. It creates and deletes
   objects in a live cluster and a live secrets store, so its execution is
   separately authorised, and the safety of its cleanup rests on ownership
   checks — UID and resourceVersion, not name alone — that a one-line
   command does not make. The examples use the names
   `probe-20261004-k7x2q` and `probe-20261004-k7x2q-find` and the marker
   path `secret/platform/saas-fabric/instances/probe-20261004-k7x2q/marker`;
   a run uses the names chosen under "Prerequisites".

   - **Verify absence, one object at a time.** Before anything is created,
     read each of the five objects the probe will own, individually and by
     exact name: the `ExternalSecret` `probe-20261004-k7x2q` in `secrets`,
     the `ExternalSecret` `probe-20261004-k7x2q-find` in `secrets`, the
     Secret `probe-20261004-k7x2q` in `secrets`, the Secret
     `probe-20261004-k7x2q-find` in `secrets`, and the marker's metadata at
     its OpenBao path. The only acceptable answer for each is a definite
     not-found from the API that owns it. A permission error, a timeout, a
     connection failure or a malformed response is **not** absence; it is a
     stop. Any of the five already existing is also a stop: choose new
     names, and never adopt, overwrite or delete what was found. Never check
     two names in one request — a combined query can report one missing
     while the other exists, and a cleanup built on that answer deletes
     something the probe did not create.
   - **Create, and keep the receipt.** Create the marker at its exact path
     using KV v2 create-only semantics (`cas=0`), with one non-secret key,
     for example `probe=denied-access-check`. Create, never apply or replace,
     the two `ExternalSecret`s in namespace `secrets`, and never commit them
     (the checker would rightly refuse them). Confirm that `secrets` has the
     platform label the store's conditions admit; otherwise stop and revise
     the approved probe plan before creating anything. Both probes use
     the `openbao` store, each with `target.name` equal to its own name and
     `creationPolicy: Owner`, never `Merge` or `Orphan`: one with
     `dataFrom[].extract.key: platform/saas-fabric/instances/probe-20261004-k7x2q/marker`
     named `probe-20261004-k7x2q`, and one with
     `dataFrom[].find.path: platform/saas-fabric/instances/probe-20261004-k7x2q`
     named `probe-20261004-k7x2q-find`. Record, from the create response of
     each `ExternalSecret`, its `metadata.uid` and
     `metadata.resourceVersion`, and for the marker the version OpenBao
     reports. That record is the probe's ownership receipt, and cleanup is
     scoped to it. A create that fails because the object already exists is
     a stop, not a retry with `apply` or `replace`.
   - **Observe.** Expected within a refresh interval: both `ExternalSecret`s
     report `SecretSyncedError` with a permission-denied message from
     OpenBao, and **no** Secret named `probe-20261004-k7x2q` or
     `probe-20261004-k7x2q-find` exists. If a Secret does appear, read its
     `metadata.ownerReferences` and record whether it names a receipted
     `ExternalSecret` UID; it is the probe's only if it does.
   - **Clean up only what the receipt names, whether the probe denied or
     synced.** For each `ExternalSecret`, read it back and confirm its UID
     matches the receipt. Use Kubernetes delete preconditions for that UID
     and the freshly checked resourceVersion, by exact name and namespace;
     a mismatch is a stop, not a retry without preconditions. Delete a Secret
     only if its `ownerReferences` name a receipted UID, using the same
     precondition checks. Delete the marker's metadata at its exact path
     only after confirming its version matches the receipt and that the
     approved plan excludes concurrent writers to that unique path. If
     ownership or concurrency cannot be established, retain it and escalate;
     never delete a prefix above it. No
     `--ignore-not-found`, no label selectors, no wildcards, no `--all`.
   - **Confirm cleanup independently.** Separately from the delete commands
     and their exit status, read each of the five objects again by exact
     name, one at a time, and record a definite not-found for every one. The
     absence rule applies again: an error is not confirmation. Only then is
     the probe closed.

   Never delete an object that existed before the probe. The names are
   unique and the receipt exists for exactly this reason; do not substitute
   an existing name, and do not widen cleanup to anything that merely looks
   like a probe.

7. **Non-regression for everything legitimate.**

   - Every existing `ExternalSecret` in a platform namespace is still
     `Ready` (`kubectl get externalsecrets -A`), and one ordinary platform
     secret still refreshes on demand
     (`kubectl annotate externalsecret <name> force-sync=$(date +%s)`) and
     returns to `Ready`.
   - The control plane's own access is untouched — its policy is
     `saas-fabric-control-plane`, which this procedure never writes — and its
     pods log no permission errors; the console's integrations still list.
     Nothing here prints a value from its partition.

8. **Record the outcome**: the effective policy set and every name reviewed
   in step 2 and what was found, the diff from step 3, the capability output
   from step 5, the probes' error conditions, ownership receipt and
   independent cleanup confirmation from step 6, and the time.

### If something goes wrong

- **Read-back differs, or an ordinary platform `ExternalSecret` goes to
  error after the write.** Rolling back is writing `current.hcl` under the
  same name. Before doing so, understand what it means: if `current.hcl`
  lacks the deny, rolling back **reopens the partition** to anyone able to
  create an `ExternalSecret` in a platform namespace. That is a security
  decision, not a technical one. If rollback is taken: record it as reopening
  the partition, prohibit onboarding any new credential into the partition
  (no new Git applications connected, no registries registered through the
  console) until the deny is written and re-verified with steps 5 to 7, and
  escalate to whoever authorised this procedure.

  ```bash
  bao policy write platform-secrets current.hcl
  ```

- **The write itself fails, or leaves OpenBao in a state a policy write
  cannot explain.** Stop. Do not retry blindly, do not restore from the
  snapshot, and do not delete the StatefulSet or its volumes. Preserve the
  evidence — `bao status`, the OpenBao pod's logs, `current.hcl`,
  `intended.hcl`, the exact command and its output — and escalate. Restoring
  a Raft snapshot is a whole-store operation that discards every write made
  since it was taken, by every identity, not only this policy; it is a
  separately approved recovery decision made with that understood, under the
  restore plan from "Prerequisites", never a reflex response to an
  unexplained failure. A rebuild is likewise a separate decision with its own
  authorisation; nothing in this document grants it.

- **A probe in step 6 syncs.** The running policy does **not** deny the
  partition for the External Secrets identity, whatever the checker says.
  Run step 6's cleanup, scoped to the receipt, so no projected marker
  remains. Treat the partition as readable: prohibit credential onboarding
  into it as above, record which probe synced, re-review the whole effective
  policy set (step 2 — a more specific grant, in `platform-secrets` itself
  or in any identity or group policy, is the first thing to look for), and
  escalate. Do not proceed to anything else.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [OpenBao](../openbao/) | `10` | the secrets authority it reads from |
| [Secret store](../secret-store/) | `20` | the `ClusterSecretStore` joining the two |

External Secrets and OpenBao are both wave `10` on purpose: the operator does
not need OpenBao at startup, only when it reconciles an `ExternalSecret`.

## What this does not solve

It cannot deliver the credentials the platform needs *before* OpenBao is
running. That short list stays externally injected — see
[the bootstrap secret boundary](../../../docs/architecture.md#the-bootstrap-secret-boundary).

## Configuration owned by this repository

- the operator, its CRDs, webhook and cert controller;
- the service account the OpenBao role is bound to;
- resource sizing per environment.

## Configuration expected from outside this repository

- **the OpenBao auth method, policy and role**, created once at bootstrap;
- **the secret values themselves**, which live in OpenBao and never here;
- **client secret paths and policies**, owned by the client layer.
