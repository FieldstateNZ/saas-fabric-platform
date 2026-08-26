# Releases

There are two versions in this system and they must not be confused.

| | What it versions | Where it is pinned |
|---|---|---|
| **Application version** | one deployed component | `targetRevision` in an `application.yaml`, e.g. Keycloak chart `7.3.0` |
| **Platform version** | the entire platform definition, as a composition | a Git tag on this repository, e.g. `v0.3.0` |

A platform release says *these versions of these components, configured this
way, worked together*. That is the thing production runs.

## What each environment follows

| Environment | Revision | Moves when |
|---|---|---|
| LucentRoot | `refs/heads/main` | a pull request merges |
| Production | `refs/tags/vX.Y.Z` | someone decides to promote |

Production must never follow a branch. A moving target is not a release.

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
Git release tag           an explicit decision
      ↓
production promotion
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

### 2. Open a release pull request

One file changes:

```yaml
# environments/production/config/platform.yaml
data:
  revision: v0.3.0
```

This is what makes a tag self-describing: the tag contains the revision it *is*,
so a cluster bootstrapped from `v0.3.0` tracks `v0.3.0` with nothing to
remember. `main` always shows the most recently released production revision.

### 3. Merge and tag the merge commit

```bash
git checkout main && git pull
git tag -a v0.3.0 -m "Platform v0.3.0"
git push origin v0.3.0
```

Tag the commit that contains the `revision: v0.3.0` change. Tagging a different
commit produces a release whose contents disagree with what it claims to be.

Use semantic versioning against the platform as a whole:

| Bump | Means |
|---|---|
| major | a breaking change to the platform contract — a namespace moves, a secret interface changes, an environment needs manual intervention |
| minor | a new capability, or a component upgrade with new behaviour |
| patch | fixes and configuration corrections with no interface change |

### 4. Promote production

```bash
git checkout v0.3.0
kubectl apply -k environments/production/bootstrap
```

Argo CD cannot promote itself: the root Application tracks the old tag, so it
will never see a commit telling it to track a new one. Promotion is therefore
deliberately an explicit, human, out-of-band act. It is the only routine
`kubectl` command run against production.

### 5. Watch it land

```bash
kubectl -n argocd get applications -w
```

Waves apply in order. Expect several minutes.

## Rolling back

Re-apply the previous tag:

```bash
git checkout v0.2.0
kubectl apply -k environments/production/bootstrap
```

This is why production tracks tags. Rollback is selecting a known composition,
not reverting commits under a live cluster.

Two caveats:

- **Data does not roll back.** A database schema migrated by a newer Keycloak is
  not undone by pointing at an older one. Treat any release containing a
  Keycloak major upgrade as forward-only.
- **Protected resources do not roll back either.** Resources marked
  `Prune=false` are not removed by returning to a revision that predates them.

## Hotfixes

Same path, shortened, never skipped: fix on a branch, merge to `main`, confirm
LucentRoot, tag a patch version, promote. Applying a change directly to
production makes the cluster disagree with its tag, and `selfHeal: true` will
revert it anyway.
