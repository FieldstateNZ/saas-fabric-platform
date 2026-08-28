# SaaS Fabric control plane

| | |
|---|---|
| Product | SaaS Fabric (control plane) |
| Upstream project | https://github.com/FieldstateNZ/saas-fabric |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Container image | `ghcr.io/fieldstatenz/saas-fabric-control-plane:0.1.0` |
| | `ghcr.io/fieldstatenz/saas-fabric-control-plane-ui:0.1.0` |
| Licence | Fieldstate |
| Namespace | `platform-system` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `40` |

## What this directory is

The deployment contract for **the other half of SaaS Fabric**: the plane that
holds what a client *is* and reconciles that definition into the platform
services below it. [`../saas-fabric`](../saas-fabric/) deploys the runtime half,
which serves tenant traffic. They are separate Applications because they are two
deployments on two networks: the runtime plane must keep serving tenants while
this is down.

Two Deployments here, from two images:

| Deployment | Image | Serves |
|---|---|---|
| `saas-fabric-control-plane` | `saas-fabric-control-plane` | the operator API, port `8081` |
| `saas-fabric-control-plane-ui` | `saas-fabric-control-plane-ui` | the operator console, static files on `8080` |

## Operator plane only, structurally

The namespace metadata carries **no `gateway-access` label**, so this Application
cannot attach a route to the product `Gateway` even by mistake. It is published
on the operator plane and nowhere else, through
[`../operator-access`](../operator-access/):

| Path | Backend |
|---|---|
| `/api` | `saas-fabric-control-plane` |
| `/` | `saas-fabric-control-plane-ui` |

The console is a static bundle that calls the API on its own origin and does not
proxy to it — so the split above is the platform's to own, and the UI image
never learns the API's address.

This is what **replaces the Keycloak admin console**, which is published on no
plane in any environment. SaaS Fabric administers Keycloak server-side over the
Admin REST API; the vendor console was a second way to change the same objects
without the platform knowing. See [`../keycloak`](../keycloak/).

## Configuration: a file, not environment variables

The application reads TOML from `FABRIC_CP_CONFIG` and **refuses to start if it
is missing**. Each environment overlay replaces `control-plane.toml` whole
rather than patching it — a partial patch of a config file is how one ends up
half from one environment and half from another.

The base file is not deployable: its operator allowlist and GitHub App
identifiers are placeholders.

## Required external secrets

```yaml
secretRef:
  name: saas-fabric-control-plane-secrets   # namespace: platform-system
```

Two values, both **issued elsewhere and consumed here** — which is what makes
OpenBao the delivery path, unlike Keycloak's admin password, which is generated
in-cluster because nobody needs to choose or transport it.

| OpenBao path | Property | Becomes |
|---|---|---|
| `platform/saas-fabric/keycloak` | `client-secret` | `FABRIC_SECRET_KEYCLOAK_SAAS_FABRIC` |
| `platform/saas-fabric/github` | `private-key` | `FABRIC_SECRET_GIT_SAAS_FABRIC_CLIENTS_APP_KEY` |

Named with `data[]` rather than `dataFrom.extract`, because the environment
variable name is derived from a reference in the application's configuration
(`keycloak/saas-fabric` → `FABRIC_SECRET_KEYCLOAK_SAAS_FABRIC`). Mapping
explicitly keeps OpenBao's key names natural and states the application's
contract in one place — and `data[]` names one exact key where `find.path` would
take everything under a prefix.

**Nothing secret is in this directory.** The GitHub App's id and installation id
are public identifiers and sit in the config file; the private key and the
Keycloak client secret exist only in OpenBao.

## Identity: two machine identities, no human credential

| Against | Identity | Permission |
|---|---|---|
| Keycloak | service account on the `saas-fabric` client, `master` realm | `create-realm` only |
| GitHub | a GitHub App installation on `saas-fabric-clients` | contents read/write on that repository |

`create-realm` is sufficient on its own: creating a realm makes the service
account that realm's administrator, so no further grant is bootstrapped. The
GitHub App is an installation identity rather than a personal access token, so
it does not expire with a person or carry their other repositories.

Operators authenticate to the console through the tailnet. The control plane
consumes the identity the operator-plane proxy established
(`Tailscale-User-Login`) and checks it against an allowlist that **may not be
empty** — the tailnet establishes who someone is, the allowlist establishes that
they administer this platform.

## Current state

The Keycloak half is proven against this cluster: realm, roles and OIDC client
created, idempotent across repeated sweeps, and drift detected and corrected.

**The GitHub App does not exist yet**, and creating one requires a human. Until
it does, the control plane starts, serves, and reports every client as failing to
read — which is the honest state, and visible in the console rather than only in
a log. The `app_id` and `installation_id` placeholders in the LucentRoot overlay
are the two values to replace.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [External Secrets](../external-secrets/) | `10` | delivers both credentials |
| [OpenBao](../openbao/) | `10` | holds both credentials |
| [Keycloak](../keycloak/) | `20` | the system it reconciles into |
| [Operator access](../operator-access/) | `20` | the only plane it is published on |
| [SaaS Fabric](../saas-fabric/) | `30` | the runtime half of the same product |

Wave `40` places it after everything it administers.

## TLS

Terminated by the Tailscale proxy with a tailnet certificate, not here and not
at the platform `Gateway` — this service does not attach to that Gateway at all.
