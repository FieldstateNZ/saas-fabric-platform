# Keycloak credentials

| | |
|---|---|
| Product | External Secrets `Password` generator |
| Upstream project | https://github.com/external-secrets/external-secrets |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | generator supplied by the pinned External Secrets chart |
| Licence | Apache-2.0 |
| Namespace | `identity` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `10` |

## Why it exists in SaaS Fabric

Keycloak needs a bootstrap administrator credential before it will start. That
credential is **arbitrary** — nobody needs to choose it — so nobody does.

A password a human picks is worse than one a machine generates, and a password
that has to travel from a person to a cluster can leak on the way. Generating it
in-cluster removes both problems: there is no value to choose, no value to
transport, and no value to store anywhere else.

This is why `keycloak-admin` is no longer on
[the bootstrap secret boundary](../../../docs/architecture.md#the-bootstrap-secret-boundary).
Only credentials issued by something outside the cluster still are.

## Reading it

```bash
kubectl -n identity get secret keycloak-admin \
  -o jsonpath='{.data.password}' | base64 -d
```

## `refreshInterval: "0"` is load-bearing

Not a default, and not a tidy-up. Keycloak reads `KC_BOOTSTRAP_ADMIN_*` only on
first start and writes the account into its own database. If this Secret were
refreshed later, the value here would rotate while Keycloak kept the original —
leaving a credential that looks correct and does not work.

Rotating the Keycloak admin password is a Keycloak operation, not a Kubernetes
one.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [External Secrets](../external-secrets/) | `10` | supplies the generator and the `ExternalSecret` CRD |

Same wave as External Secrets on purpose: the generator is local, so unlike the
[secret store](../secret-store/) it does not wait on OpenBao being initialised.
Argo CD gates wave `20` on wave `10` health, so Keycloak cannot start before
this Secret exists.

## Configuration owned by this repository

- password length and character classes;
- the `username` the template pairs with the generated password.

## Configuration expected from outside this repository

Nothing. That is the point.
