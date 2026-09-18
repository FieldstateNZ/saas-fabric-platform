# Environments

Environment configuration is deliberately thin. Application definitions are
shared; an environment describes only what is genuinely different about it.

```text
environments/
├── components/environment-config/   binds shared Applications to one environment
├── lucentroot/
│   ├── kustomization.yaml           which applications this environment runs
│   ├── bootstrap/                   kubectl apply -k environments/lucentroot/bootstrap
│   ├── components.yaml              what it is asked to run, and the policy that moves it
│   └── config/
│       ├── platform.yaml            the environment contract
│       └── <application>.yaml       Helm values overrides, only where they differ
└── production/
    └── ... the same shape
```

## The environment contract

`config/platform.yaml` is the one place an environment describes itself:

```yaml
environment: lucentroot          production
domain: lucentroot.internal      platform.fieldstate.nz
replicaProfile: development      production
storageClass: local-path         managed-csi
```

Environmental facts only. It is a real ConfigMap, applied to the cluster, so a
cluster can always be asked what it is. It is also the source for the Kustomize
replacement that points every Application at this environment's configuration —
see [`components/environment-config`](components/environment-config/kustomization.yaml).

`environment` is consumed mechanically. `domain`, `replicaProfile` and
`storageClass` are the declared contract that the per-application files below
must agree with.

## The Git ref is not environment configuration

Which ref Argo CD follows is deliberately **not** here. It is Argo binding —
promotion state, not a fact about the environment — and putting it in a runtime
ConfigMap would conflate "what this environment is" with "what it is currently
running".

It appears in exactly two files per environment:

```text
<environment>/kustomization.yaml            the ref child Applications follow
<environment>/bootstrap/kustomization.yaml  the ref the root Application follows
```

| Environment | Follows |
|---|---|
| LucentRoot | `main` |
| Production | `production`, a branch only ever fast-forwarded to a release tag |

See [docs/releases.md](../docs/releases.md).

## What an environment currently runs

`components.yaml` is what an environment is **asked** to run: the version of
each component, the commit its images were built from, their immutable digests,
the update policy, and any hold. It is desired state, and only desired state —
what versions are *available* is discovered from registries and what is
*running* comes from the cluster, so neither is written here.

It is **machine-managed**. SaaS Fabric's Platform Management writes it and
rewrites it whole, so a hand edit survives as values but not as formatting.
Editing it by hand is the break-glass path and is expected to keep working: it
is how an environment is recovered when Fabric is the thing that is broken.

The file's header says enough to orient somebody who opens it. **This is the
contract**; the header deliberately does not try to become it.

### The fields

| | |
|---|---|
| `schemaVersion` | The shape of the document. A reader written against an older one refuses rather than half-understanding a field that has moved. |
| `environment` | Which environment this describes, checked against the path it was read from. |
| `managedRoots` | The only directories any `pinnedIn` may point into. See below. |
| `channel` | The release stream newer versions are drawn from. `preview` admits SemVer prereleases, which no other environment may run. |
| `update` | `automatic` — the newest eligible version is selected without asking. `manual` — an update is surfaced and an operator chooses it. `locked` — nothing moves without changing the constraint itself. |
| `desired.version` | The version, **once**. Not repeated per image: three images claiming a version separately is three places for them to disagree, and disagreement is what makes a release unit incomplete rather than eligible. |
| `desired.sourceRevision` | The commit every image was built from, verified against each image's `org.opencontainers.image.revision` before a version is eligible. The only place Git records where an artifact came from. |
| `desired.images.<role>` | One image of the component: where it is published, the digest asked for, and where that pin is rendered. |
| `hold` | Present while automatic advancement is paused. |

### `hold` pauses advancement without changing policy

A rollback under an `automatic` policy should not quietly demote the component
to `manual` — the operator did not change what the environment should do in
general, they said *stay here until I say otherwise*. So `update` keeps saying
`automatic` and a `hold` appears beside it:

```yaml
update: automatic
hold:
  reason: rollback
  since: 2026-09-01T09:00:00Z
  note: preview.7 broke Secrets
```

Discovery keeps running and newer versions keep being reported as available;
desired state does not move until an operator clears the hold. The effective
state reads *Automatic — Paused*.

**It carries no version.** `desired.version` already is the held version, so a
break-glass edit that moves the version by hand leaves the hold correctly in
force rather than pointing at something nothing runs.

### `pinnedIn` is why this repository keeps its own layout

Each image declares which files pin it:

```yaml
runtime:
  repository: ghcr.io/fieldstatenz/saas-fabric
  digest: sha256:...
  pinnedIn:
    - applications/core/saas-fabric/overlays/lucentroot/kustomization.yaml
```

Fabric may write this manifest and exactly the paths it declares, and nothing
else. That is what keeps the platform's directory layout *here* rather than
compiled into a Fabric release — these files can move without waiting for one.
When this repository eventually renders the overlays from this manifest rather
than beside it, the list empties and Fabric writes one file, with no change to
Fabric.

### `managedRoots` bounds what Fabric may touch at all

`pinnedIn` is trusted — it is desired state in the repository Fabric writes to
— and it is still worth bounding. A mistake in it would otherwise make Fabric a
confused deputy, editing `.github/workflows/` or a README because a trusted
document asked it to. `managedRoots` says which directories any `pinnedIn` may
point into, independently of any individual path.

Six rules apply to every declared path, and `scripts/check.py` and Fabric apply
the same six:

```text
repository-relative, never absolute
no traversal
under a managedRoot
a .yaml or .yml manifest
exists
and actually pins the image that declared it
```

That last one is deliberately enforced twice. This repository's CI proves the
manifest is coherent at the commit it ran on; Fabric applies it again against
whatever it actually read, which may be a state no CI has seen.

A `managedRoot` must end in `/`, may not be empty, and must be inside the
repository. Two of those are worth naming rather than merely enforcing:

- **empty** — every path starts with the empty string, so one blank entry would
  switch the whole guard off while still looking like a list of roots;
- **absolute** — `/` ends in a slash and is not empty, so it passes both other
  rules and would then admit every path on the machine. Fabric refuses an
  absolute path outright, which is the defence that matters; this rejects it
  too, because a contract permitting a root its only consumer will never accept
  describes something that cannot work.

`scripts/check.py` also holds the rest together: the rendered manifests must
match the version and digests asked for, and a prerelease version must not
appear in any other environment.

It is not configuration. `config/` says what an environment *is*; this says what
it is asked to run, which is why it sits beside that rather than inside it — the
same distinction the Git ref makes above.

Only LucentRoot has one today, because it is the only environment whose
component versions move often enough to be worth managing rather than reviewing.

## What a tenant's data may be placed on

`data-sources.yaml` is what an environment declares of the databases a
tenant's data may live on: which connector reaches each one, how that
connector is told to reach it, how strongly it isolates a tenant, where it
resides, and the pool and capability facts the runtime is configured with. It
is desired state, and only desired state — it does not place a tenant on
anything, and nothing here is published to the runtime yet. See ADR 0023,
*Data sources are environment desired state, and placement is recorded rather
than inferred*, in the application repository (`saas-fabric`); this file is
that decision's part 1.

It is **machine-managed**, the same way `components.yaml` beside it is. SaaS
Fabric's Platform Management writes it and rewrites it whole, so a hand edit
survives as values but not as formatting — including its header comment
block, which Fabric preserves on every rewrite. Editing it by hand is the same
break-glass path `components.yaml` has, and is expected to keep working. A
file that does not exist yet reads as an environment with nothing declared;
the first declaration creates it, header included, in one commit.

### The fields

Each entry is spelled exactly as the runtime wire spells `data-sources.json`'s
`DataSourceDocument` in the application repository — `snake_case`, unlike this
file's own `schemaVersion` / `environment` / `dataSources` envelope, which
stays `camelCase` like `components.yaml`. That is deliberate: the entry is
parsed with the wire's own type, so a field this file could accept that the
wire would refuse is not a shape this checker has to imagine — it is simply
wrong here too.

| | |
|---|---|
| `id` | A unique, DNS-label-like identifier for this data source. |
| `revision` | Bumped by Fabric on every change; a correction to one data source moves one number, and no tenant placed on it. |
| `connector` | The connector process id the runtime is configured with. |
| `connection` | `{kind: named, name}` — the connection that connector process already holds — or `{kind: secret, reference}`, a reference path into wherever secrets live. Never a value. |
| `placement` | One of `shared`, `dedicated`, `high_availability`, `regulated`, `development`, `ephemeral`. |
| `residency` | `{region, jurisdiction}` — `jurisdiction` optional. |
| `pool` | `{max_connections, idle_timeout_seconds, acquire_timeout_seconds}`, each `1` or more. May be omitted; Fabric's defaults are `20` / `300` / `5`. |
| `capabilities` | `{writable, accepts_new_tenants}`, both booleans. May be omitted; both default to `false`. |
| `discriminator` | `{column}` — the column every collection on this data source carries. Required when `placement` is `shared`, and refused on every other placement. |
| `labels` | A map of non-empty strings. May be omitted. |

### A shared data source needs a discriminator; no other placement may have one

ADR 0006, in the same application repository, makes the discriminator column
the only isolation a `shared` data source may serve, so `discriminator` is required
exactly when `placement: shared` and refused otherwise — never optional either
way. It is the one field on an entry that is not on the wire's own
`DataSource`: it belongs here because it is a fact about the database, not
about a tenant, and stating it once is what lets a shared source with no
column be refused before any tenant is placed on it.

### Nothing here is a credential

`connection` never carries a value. A `named` connection points at a
connection the connector process is already configured with; a `secret`
connection carries a `reference` — a path into wherever secrets live, not the
secret itself. `check.py` refuses a `connection` that carries any key beside
`kind` and `name`, or `kind` and `reference`, so a value cannot be smuggled in
beside the reference that is meant to be there.

## Where a tenant is placed

`placements.yaml` is the record of where each tenant's data lives: which data
source it was placed on, how it is isolated there, and when. It is not
desired state — nothing here is asked for, and nothing here decides anything.
It is written once, by the act that allocated the placement, and publication
copies it into the tenant binding without recomputing it — ADR 0007, in the
same application repository, is what that meets. See ADR 0023, *Data sources
are environment desired state, and placement is recorded rather than
inferred*, in the application repository (`saas-fabric`); this file is that
decision's part 2.

It is **machine-managed**, the same way `components.yaml` and
`data-sources.yaml` beside it are. SaaS Fabric writes it when a tenant is
placed, one commit per placement, and rewrites it whole — a hand edit
survives as values but not as formatting. Editing it by hand is the same
break-glass path the other two files have, and **an edit made that way is
honoured as written**: nothing here is recomputed to check it, because the
record *is* the fact. A file that does not exist yet reads as an environment
with nothing placed.

### The fields

Entries are a list, sorted by (`tenant`, `logical`) — there is no map key that
could disagree with either field the way a mapping key can drift from a field
repeated inside its value.

| | |
|---|---|
| `tenant` | The tenant this placement belongs to, a DNS-label-like identifier. |
| `logical` | The logical data source this placement fills — `primary`, `audit`. |
| `data_source` | The id of a data source declared in this environment's `data-sources.yaml`. |
| `isolation` | `{kind: database}`, `{kind: schema, schema}`, or `{kind: discriminator, column, value}` — exactly those keys for that `kind`, spelled as the runtime wire's `IsolationModelDocument`. |
| `placed_at` | When the placement was recorded, RFC 3339. |

### A placement's isolation must agree with its data source

`isolation.kind` is `discriminator` exactly when `data_source` names a
`shared` data source, and then `isolation.column` must be that data source's
own `discriminator.column` — the two are stating the same fact from two
files, and `check.py` refuses them for disagreeing. A `dedicated` data source,
or any other non-`shared` placement class, serves one tenant, so at most one
placement naming it may carry a non-`discriminator` isolation. On a `shared`
data source, no two placements may carry the same `discriminator.value`,
since that value is what a query filters the collection on.

### A dangling reference is refused, not ignored

`data_source` must name a data source this environment declares. If
`data-sources.yaml` does not exist for this environment, every placement here
is dangling by construction, and `check.py` says so — a tenant cannot be
recorded as placed on a database this environment has never declared, even
under break-glass.

## Per-application overrides

`config/<application>.yaml` is a Helm values file, read directly from Git by
Argo CD as a second values source on top of the application's shared
`values.yaml`. It should contain only real environmental differences: replica
counts, storage classes, hostnames, resource sizing.

An environment with nothing to say about an application has no file for it —
Argo CD is configured with `ignoreMissingValueFiles`, so the shared values apply
unchanged. LucentRoot has no `saas-fabric.yaml` for that reason; production has
no `perses.yaml` because it does not deploy Perses yet.

## What must not go here

Do not copy an application's full configuration into each environment and change
a few lines. This shape is what this repository is structured to avoid:

```text
environments/
  lucentroot/
    keycloak/
    perses/
    openbao/
  production/
    keycloak/
    perses/
    openbao/
```

An application needing genuinely *structural* differences per environment — not
just different values — is the exception, and it is expressed as a Kustomize
overlay inside the application's own directory, where the rest of its definition
lives. See
[`applications/core/saas-fabric/overlays`](../applications/core/saas-fabric/overlays/).

## Which applications an environment runs

`kustomization.yaml` lists what this environment reconciles, and sets the Git
ref above. Core and [`argocd/runtime`](../argocd/runtime/) are always included.
The `catalogue` tier is one line, and omitting it yields a complete platform:

| Environment | `applications/core` | operator plane | `applications/catalogue` |
|---|---|---|---|
| LucentRoot | yes | yes | yes |
| Production | yes | no — no tailnet yet | no |

**Enablement is independent of what a service is.** Perses is the same platform
service in both environments; LucentRoot deploys it and production does not yet.
A service does not change architectural type by being switched on somewhere.

### LucentRoot dogfoods the platform

Its purpose is not only to prove that mandatory dependencies start. It exists to
exercise the services SaaS Fabric expects to manage, which is why it enables
things production does not:

```text
LucentRoot
  Envoy Gateway      CloudNativePG      OpenBao
  External Secrets   Keycloak           OpenTelemetry
  Perses             SaaS Fabric
  OpenFGA    [when adopted]
  Superset   [when adopted]
  Airflow    [when adopted]
```

The bracketed three are not enabled to satisfy that list. Each still has to
solve its deployment contract first — see
[docs/platform-services.md](../docs/platform-services.md#the-register).

The operator plane is `applications/core/tailscale` and
`applications/core/operator-access`. Both are core, but an environment without a
tailnet cannot run them, so they are listed by each environment rather than by
`applications/core/kustomization.yaml`. See
[docs/architecture.md](../docs/architecture.md#exposure-planes).

## Adding an environment

The structure supports a third environment without reorganisation. See
[docs/adding-an-application.md](../docs/adding-an-application.md#adding-an-environment).
