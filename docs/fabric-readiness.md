# Fabric readiness on LucentRoot

Verified 2026-09-17. LucentRoot is the live development environment. Future
Workspec and Nexus workloads may share a production Kubernetes environment,
with either or both applications assigned to a client realm.

## Working operator baseline

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

## Next acceptance work

1. **Observed deployments.** Components currently returns `running: unknown` by
   design. Define and implement a read-only observation port and deployment
   adapter. Report actual image/version evidence, rollout health, observation
   time and stale/unavailable state. A Git commit is not rollout evidence;
   mixed versions must not be reported as one healthy running version. The
   paused tenant runtime must remain distinguishable from the running operator
   deployments.
2. **Self-update proof.** Publish the next intentional application change and
   observe Fabric discover it, commit the advance, Argo deploy it, and the
   application remain usable. Resume success above proves the control/write
   path, not that entire upgrade sequence. Test recovery within compatible
   configuration versions before claiming rollback readiness.
3. **Register and deploy a service.** Define artifact/version, target
   environment, configuration, secret references and dependencies. Reconcile
   them into deployed resources and show observed health. Keep this distinct
   from assigning an application to a realm: a service may serve many realms.
4. **Realm/application integration.** Resolve private-network catalogue
   assignments; verify creation, assignment and configuration against real
   desired state. Define how applications consume shared identity, secrets,
   authorization, messaging, data and storage. Application-shell federation is
   a later consumer of these contracts.

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
