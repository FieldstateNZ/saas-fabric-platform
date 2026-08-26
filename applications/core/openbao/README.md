# OpenBao

| | |
|---|---|
| Product | OpenBao |
| Upstream project | https://github.com/openbao/openbao |
| Helm chart source | https://openbao.github.io/openbao-helm |
| Chart version (pinned) | `0.29.2` |
| Application version | `v2.6.2` |
| Licence | MPL-2.0 |
| Namespace | `secrets` |
| Class | core |
| Sync wave | `10` |

## Why it exists in SaaS Fabric

OpenBao is the platform's secrets capability. SaaS Fabric needs somewhere to
hold and issue per-client credentials that is not Git and not a static
Kubernetes Secret, and every other platform component eventually sources its
credentials from it. It is core because the platform has no credible secret
story without it.

## Ownership boundary

Argo CD owns the OpenBao *deployment*. It does not own its contents.

| Resource | Owner |
|---|---|
| StatefulSet, storage, service, UI, RBAC | this repository |
| Initialisation, unseal keys, root token | operator / external, never Git |
| Kubernetes auth method for External Secrets | one-time bootstrap step, see [docs/bootstrap.md](../../../docs/bootstrap.md) |
| Auth methods and policies for a client | client OpenTofu |
| Per-client namespace inside OpenBao | client OpenTofu |

## Bootstrap: initialise and unseal

A freshly reconciled OpenBao is **uninitialised and sealed**, and the readiness
probe is configured to accept that state so the Application still reports
Healthy. Initialisation is a one-time operator action because it produces
material that must never reach this repository:

```bash
kubectl -n secrets exec -it openbao-0 -- bao operator init
```

Store the unseal shares and root token in the organisation's break-glass
location. For production, replace manual unsealing with an auto-unseal seal
stanza backed by a key vault provisioned by `saas-fabric-hosting`, configured in
`environments/production/config/openbao.yaml`. Until that is wired, a restarted
pod requires `bao operator unseal`.

No root token, unseal share or recovery key is permitted in this repository.

## How secrets reach workloads

OpenBao is the authority; it is not the delivery mechanism.
[External Secrets](../external-secrets/) reads it and materialises Kubernetes
Secrets, joined by a single [`ClusterSecretStore`](../secret-store/). A workload
puts its values at `secret/<name>` and declares an `ExternalSecret`; nothing
about those values enters this repository.

## Dependencies

None hard. Wave `10`, alongside the other foundations.

## Configuration owned by this repository

- StatefulSet topology (Raft, replica count per environment);
- data and audit volume sizing, storage class and retention policy;
- service, UI exposure and health probing;
- the seal configuration *reference* for auto-unseal.

## Configuration expected from outside this repository

- **Unseal / auto-unseal material** — a key vault key and an identity permitted
  to use it, created by `saas-fabric-hosting`.
- **Tailnet access**, if operators need the UI. Workloads reach OpenBao
  cluster-locally at `openbao.secrets.svc.cluster.local:8200` and need no
  ingress at all; LucentRoot additionally exposes it on the operator plane. It
  is never on the product edge. See
  [docs/architecture.md](../../../docs/architecture.md#exposure-planes).
- **Policies, auth backends and secret engines**, which are reconciled by the
  client layer per client.

## Resources unsafe to prune

The Raft data and audit PVCs are retained on scale-down and delete via
`persistentVolumeClaimRetentionPolicy: Retain`. Removing this Application from
Git deletes the StatefulSet but leaves those volumes behind by design; they must
be removed deliberately.
