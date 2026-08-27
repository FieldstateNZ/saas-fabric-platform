# Grafana credentials

| | |
|---|---|
| Product | External Secrets `Password` generator |
| Upstream project | https://github.com/external-secrets/external-secrets |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | generator supplied by the pinned External Secrets chart |
| Licence | Apache-2.0 |
| Namespace | `catalogue` |
| Class | catalogue |
| Sync wave | `40` |

## Why it exists in SaaS Fabric

Grafana's admin password is arbitrary, so it is generated rather than chosen.
The reasoning is set out once in
[`applications/core/keycloak-credentials`](../../core/keycloak-credentials/) and
applies unchanged here.

```bash
kubectl -n catalogue get secret grafana-admin \
  -o jsonpath='{.data.password}' | base64 -d
```

`refreshInterval: "0"` for the same reason: Grafana writes the admin user into
its own database at first start, so a later refresh would rotate this Secret
while Grafana kept the original.

## Why it is a separate Application from Keycloak's

Not organisation — the project boundary. `catalogue` is deliberately absent from
the `saas-fabric-platform` project's destinations, so nothing running in that
project can write a Secret into it. This Application runs in
`saas-fabric-catalogue` instead.

That is the tenancy boundary doing its job rather than an inconvenience: if one
Application could deliver credentials into every namespace, the project
restrictions would not mean much.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [External Secrets](../../core/external-secrets/) | `10` | supplies the generator and the `ExternalSecret` CRD |

Same wave as Grafana itself. Within a wave Argo CD applies everything before
waiting on health, so the Secret is created alongside the Deployment that reads
it and the pod retries until it appears.

## Configuration owned by this repository

- password length and character classes;
- the `username` the template pairs with the generated password.

## Configuration expected from outside this repository

Nothing.
