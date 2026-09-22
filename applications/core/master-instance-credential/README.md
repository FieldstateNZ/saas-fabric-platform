# Master-instance credential

| | |
|---|---|
| Product | External Secrets `Password` generator |
| Upstream project | https://github.com/external-secrets/external-secrets |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | generator supplied by the pinned External Secrets chart |
| Licence | Apache-2.0 |
| Namespace | `operator-system` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `10` |

## Why it exists in SaaS Fabric

The master realm's confidential client `saas-fabric-gateway` — the client Envoy
redeems an authorization code with (ADR 0024) — needs a secret before
[`../master-instance`](../master-instance/) can set it. That secret is
**arbitrary**: nobody needs to choose it, so nobody does.

Before this Application, the plan was a person creating `saas-fabric-gateway` in
Keycloak by hand and writing its secret into OpenBao. The product owner's rule,
recorded in ADR 0025 of the application repository, is that nothing in the
master realm is made by hand — the same rule this repository already applies to
its own OpenBao seal
([`../openbao-seal`](../openbao-seal/)) and its own Keycloak administrator
([`../keycloak-credentials`](../keycloak-credentials/)). This Application is
that rule applied a third time, to a third value with the same shape.

## Reading it

```bash
kubectl -n operator-system get secret saas-fabric-gateway-oidc \
  -o jsonpath='{.data.client-secret}' | base64 -d
```

## `refreshInterval: "0"` is load-bearing

Not a default. [`../master-instance`](../master-instance/) sets this exact
value on the Keycloak client once, through OpenTofu. If this Secret refreshed
later, the value here would rotate while the client in Keycloak kept the
original — leaving a secret that looks correct and does not authenticate,
exactly the failure `../keycloak-credentials` documents for the admin password.

Rotating it is a convergence re-run against a regenerated value, not a
Kubernetes refresh — and not something that happens on its own. Deleting
this generator's Secret changes no manifest in Git, so Argo CD has nothing
to reconcile and `master-instance`'s Job does not re-run by itself. The real
path is two steps: delete the generated Secret here, then force a refresh
and sync of `master-instance` (`Replace=true,Force=true` together on its
Job — not `Replace=true` alone, which cannot change an immutable
`spec.template` on its own — is what makes that sync actually delete and
re-create the Job rather than finding an already-Synced one and leaving it
alone). See `../master-instance/README.md`, "The Job" and "Rotation", for
the full mechanism.

## One Secret, three readers

| Reader | Does |
|---|---|
| This `ExternalSecret` | writes `saas-fabric-gateway-oidc` |
| [`../master-instance`](../master-instance/)'s convergence Job | reads it as `TF_VAR_gateway_secret` and sets it on the Keycloak client |
| [`../saas-fabric-control-plane`](../saas-fabric-control-plane/)'s `SecurityPolicy` | reads it to redeem Keycloak's authorization code |

No OpenBao path. The value never leaves the cluster's own generated-secret
boundary — see
[the bootstrap secret boundary](../../../docs/architecture.md#the-bootstrap-secret-boundary).

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [External Secrets](../external-secrets/) | `0` | supplies the generator and the `ExternalSecret` CRD |

Wave `10`, the same wave as [`../keycloak-credentials`](../keycloak-credentials/)
and for the same reason: the generator is local, so unlike
[the secret store](../secret-store/) it does not wait on OpenBao being
initialised — there is nothing here for OpenBao to be initialised before.

## Environments

LucentRoot only, listed in `environments/lucentroot/kustomization.yaml`
alongside `../tailscale` and `../operator-access` rather than in
`applications/core/kustomization.yaml` — production has no operator plane yet
(see [`../saas-fabric-control-plane`](../saas-fabric-control-plane/)), so it has
no gateway sign-in for this client's secret to protect.

## Configuration owned by this repository

- the secret's length and character classes.

## Configuration expected from outside this repository

Nothing. That is the point.
