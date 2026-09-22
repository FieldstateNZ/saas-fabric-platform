# Fabric readiness on LucentRoot

Verified 2026-09-17. LucentRoot is the live development environment. Future
Workspec and Nexus workloads may share a production Kubernetes environment,
with either or both applications assigned to a client realm.

## Bootstrap operator baseline — preview.11

- Application source: `69dc8a7fc561bbb21b2c57e13a5929fc3c1b2017`, released as
  `v0.3.0-preview.11`. Release gates and all three image jobs passed.
- Platform upgrade: `61e3c4e173c62618b665257e4305d89fea606635`. Image digests
  and the required identity-provider audience landed together.
- Full local platform validation passed: 389 rendered resources; zero invalid
  resources or schema errors; all repository invariants passed. PR validation,
  secret scanning and CodeQL passed.
- Argo CD reported the control-plane Application Synced/Healthy; both operator
  Deployments had one ready replica on the published preview.11 digests.
- Normal PKCE sign-in succeeded in the browser and through the application
  session API. Authenticated client, catalogue, operator and platform reads
  returned 200.
- Acme is test data. Client desired-state commit
  `20b12f591e30666803b586a4cc3ce010087af405` replaced its wildcard callback
  with its exact site root. The client list and identity detail now load.
- The master realm's canonical frontend URL was missing. An otherwise valid
  operator token failed internal Admin API reads with 401 but passed with the
  public origin. Setting the master realm's `frontendUrl` to the configured
  operator origin made the internal request return 200. No roles were added.
- Acme reconciliation reached Applied. A second pass required no changes.
- Resume was exercised through Fabric's Components page. Its own platform
  GitHub App removed the hold in commit
  `921298aee1b98388283c919f36698bcca8aa3e67`; subsequent observation showed
  Automatic, no hold and a successful sweep.

The canonical URL is a one-time Keycloak prerequisite, not a Git-managed realm
resource today. Rebuild requirements are in the
[control-plane README](../applications/core/saas-fabric-control-plane/README.md).
No credentials or access tokens belong in this record.

**Superseded 2026-09-22.** `applications/core/master-instance`'s module now
owns the master realm's `frontendUrl` attribute (ADR 0025), so a rebuilt
LucentRoot no longer needs the step above performed separately. The
observation stands as recorded — this note describes what changed
afterward, not what was verified on 2026-09-17. See
[`master-instance`'s own README](../applications/core/master-instance/README.md),
"The realm itself".

## Verified automatic upgrade and observation

Application source `3d9c542837463f8329378740a48cedc405090397` published as
`v0.3.0-preview.12` after all release gates passed. Fabric's own updater wrote
platform commit `3fa06bcbd48530e683aa265d5b4d816b17258377`, advancing the
three-image release unit. Argo automatically applied it; both API and console
rollouts completed. No image pins were manually edited for this upgrade.

Only after that upgrade was healthy, configuration commit
`41d0f571f73917ac776da641a069e95c0cdec484` enabled observation. The live
platform API returned desired and running `0.3.0-preview.12`, health `healthy`,
a current observation timestamp, API and console each 1/1 ready, and the tenant
runtime `stopped` at 0/0. Acme reconverged to Applied after the API restart.
Authenticated platform, clients, identity, catalogue and operator reads passed.

Live Kubernetes SubjectAccessReviews confirmed the API account can get its
named Deployments and list workload evidence, cannot read Secrets or patch
Deployments, and the UI account cannot list Pods. Use SubjectAccessReview
bodies naming the account for this check: an impersonation flag through an
authenticating proxy need not represent the requested account.

Application CI passed the workspace tests, real connector acceptance,
architecture/dependency/file-size checks, clippy, rustdoc and 140 console tests.
Platform validation passed locally and in CI: 393 rendered resources, zero
invalid resources or schema errors, and all repository invariants passing.
Local broad Rust compilation exhausted disk space; generated build artifacts
were cleared and the independent full CI suite supplied the Rust result.

## Session recovery follow-up

The final browser check exposed an existing session-lifecycle defect: an API
401 cleared the token but did not notify the shell, leaving it visibly signed
in with a permission error. LucentRoot issues 60-second operator access tokens.
The API continued to refuse the expired token correctly.

Source `f4a39d1c804abd3ebb329f55d05a0317e0c7b098`, published as
`v0.3.0-preview.13`, connects session rejection to the shell. A rejected current
token returns to sign-in; no failed request is replayed. A 403 does not end the
session, and a late rejection of an older token cannot clear a newer sign-in.
All 143 console tests, type checking, lint and production build passed locally;
all CI and tagged release gates passed too.

Fabric automatically advanced the release in platform commit
`ac0985f6e65b59ca9d10682087e8f21dd85ada58`. Before Argo applied it, the live API
reported desired `0.3.0-preview.13` and running `0.3.0-preview.12`: direct
evidence that running state is independent of the requested Git version.

Argo then completed both rollouts. The live API and visible Components page
reported desired and running `0.3.0-preview.13`, healthy API and console at 1/1,
and the intentionally stopped runtime at 0/0. Acme reconverged to Applied and
all authenticated management reads returned 200. The loaded console asset
matched the tested production build.

The browser token was allowed to expire naturally, including the verifier's
clock-skew allowance. Refresh then displayed “Your session ended. Sign in again
to continue.” Clicking Sign in restored the existing SSO session without a
password prompt, returned to Components, and showed the current healthy running
release. No token lifetime or authentication policy was changed.

## Next acceptance work

1. **Register and deploy a service.** Define artifact/version, target
   environment, configuration, secret references and dependencies. Reconcile
   them into deployed resources and show observed health. Keep this distinct
   from assigning an application to a realm: a service may serve many realms.
2. **Realm/application integration.** Resolve private-network catalogue
   assignments; verify creation, assignment and configuration against real
   desired state. Define how applications consume shared identity, secrets,
   authorization, messaging, data and storage. Application-shell federation is
   a later consumer of these contracts.
3. **Recovery and durable status.** Test live rollback within compatible
   configuration versions before claiming rollback readiness. Identity
   reconciliation observations currently reset to Pending when the API
   restarts; the provider configuration remains in place, and an operator
   convergence restores Applied. Persisting those observations is separate work.

This record does not claim the tenant runtime is ready. It remains scaled to
zero pending publication, connector configuration and the identity edge. It
also does not claim that service deployment, storage or messaging orchestration
is implemented.

## Recovery boundary

Preview.10 rejects the audience key required by preview.11. Returning across
that boundary requires restoring the old image pins and removing the audience
configuration together, with automatic updates held. Image-only rollback is
not sufficient. Review any newly written catalogue/client document shapes
before returning to older application code. Do not weaken identity validation
to make the legacy wildcard document parse.

## Deployment observation rollout

The observer-capable application release must be running before enabling the
LucentRoot observation bindings. They identify the API, console and stopped
runtime, and use namespaced read-only Roles. The API can get only the named
Deployments and list Pods/ReplicaSets in operator-system and platform-system.
The UI service account has no binding. No Secret reads or Kubernetes writes
are granted. Production has no observation binding or additional permission.

After synchronization, Components must report the active API/console release,
a healthy deployment sample with a timestamp, and the runtime as stopped.
Refresh requests new evidence; desired state alone never populates Running.
A mixed rollout, unavailable API or digest mismatch must not claim convergence.
A rollback to preview.11 or earlier must remove the observation configuration
alongside reverting images, because those binaries reject the new section.
