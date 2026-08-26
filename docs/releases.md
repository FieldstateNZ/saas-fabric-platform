# Releases

There are two versions in this system and they must not be confused.

| | What it versions | Where it is pinned |
|---|---|---|
| **Application version** | one deployed component | `targetRevision` in an `application.yaml`, e.g. Keycloak chart `7.3.0` |
| **Platform version** | the entire platform definition, as a composition | a Git tag on this repository, e.g. `v0.3.0` |

A platform release says *these versions of these components, configured this
way, worked together*. That is the thing production runs.

## What each environment follows

| Environment | Follows | Moves when |
|---|---|---|
| LucentRoot | `refs/heads/main` | a pull request merges |
| Production | `refs/heads/production` | that branch is fast-forwarded to a release tag's commit |

Production follows a branch, but that branch is not a development stream. It
only ever moves to a commit that carries a release tag, so what production runs
is always an explicitly promoted, known composition — while promoting one stays
a Git operation.

### Why a branch rather than the tag itself

Argo CD cannot follow a tag that changes. Pointing production at `v0.2.0` means
promotion has to rewrite the Application in the cluster, which puts `kubectl` on
the normal release path and makes the cluster, not Git, the record of what is
deployed.

Pointing production at a branch that is only ever fast-forwarded to a tagged
commit keeps both properties: an explicit human promotion decision, and a
cluster that follows Git without being poked.

`kubectl` remains for three things: initial bootstrap, disaster recovery, and
deliberate break-glass work. It is not part of a normal release.

### Protecting the production branch

The promotion guarantee is only as strong as the branch. `refs/heads/production`
must be protected so it cannot be pushed to directly, cannot be force-pushed
without review, and only moves through the process below.

## The lifecycle

```text
feature branch
      ↓
pull request              CI renders every manifest; invalid output blocks merge
      ↓
main
      ↓
LucentRoot reconciliation automatic, within minutes
      ↓
validation / dogfooding   the part that takes real time
      ↓
Git tag vX.Y.Z            an explicit decision
      ↓
advance refs/heads/production to that commit
      ↓
production Argo CD sees the change and reconciles automatically
```

Not every commit on `main` becomes a release. A release is a judgement that a
composition is good, not a build artefact.

## Cutting a release

### 1. Confirm LucentRoot is actually running it

```bash
kubectl -n argocd get applications
```

Every Application `Synced` and `Healthy`, on the commit being released. An
application that has been Degraded and self-healed repeatedly is not validated.

Note that `saas-fabric` reports Healthy with zero replicas until an image is
published. That is reconciliation working, not the application running.

### 2. Tag the commit

```bash
git checkout main && git pull
git tag -a v0.3.0 -m "Platform v0.3.0"
git push origin v0.3.0
```

Use semantic versioning against the platform as a whole:

| Bump | Means |
|---|---|
| major | a breaking change to the platform contract — a namespace moves, a secret interface changes, an environment needs manual intervention |
| minor | a new capability, or a component upgrade with new behaviour |
| patch | fixes and configuration corrections with no interface change |

### 3. Promote by advancing the production branch

```bash
git checkout production && git pull
git merge --ff-only v0.3.0
git push origin production
```

`--ff-only` is the safety property, not a stylistic choice: it guarantees
production only ever moves forward along `main`'s history, and fails loudly if
someone has committed to `production` directly.

Where the branch is protected against direct pushes, do this as a pull request
from the tagged commit into `production` and merge it. Same effect, with a
review record.

### 4. Watch it land

```bash
kubectl -n argocd get applications -w
```

Argo CD picks up the branch change on its next poll — a few minutes at most, or
immediately with a webhook. Waves then apply in order. No `kubectl apply` is
involved.

## Rolling back

Move the branch back to the previous release's commit:

```bash
git push --force-with-lease origin v0.2.0^{commit}:refs/heads/production
```

This is the one place a force push is correct, because rollback is the one case
where production must move backwards along its history. `--force-with-lease`
refuses if someone else moved the branch since you last fetched.

Rollback is selecting a known composition, not reverting commits under a live
cluster.

Two caveats:

- **Data does not roll back.** A database schema migrated by a newer Keycloak is
  not undone by pointing at an older one. Treat any release containing a
  Keycloak major upgrade as forward-only.
- **Protected resources do not roll back either.** Resources marked
  `Prune=false` are not removed by returning to a revision that predates them.

## Hotfixes

Same path, shortened, never skipped: fix on a branch, merge to `main`, confirm
LucentRoot, tag a patch version, fast-forward `production`. Applying a change
directly to production makes the cluster disagree with Git, and `selfHeal: true`
will revert it anyway.

## Changing the ref an environment follows

The ref is part of an environment's Argo binding, not its runtime configuration.
It appears in exactly two files per environment:

```text
environments/<environment>/kustomization.yaml            child Applications
environments/<environment>/bootstrap/kustomization.yaml  the root Application
```

Changing it is a change to how a cluster is bootstrapped, so it takes effect for
child Applications on the next reconciliation and for the root Application when
the bootstrap set is re-applied. It is not something a normal release touches.
