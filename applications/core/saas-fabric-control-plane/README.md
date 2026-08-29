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

The base file is not deployable: its operator allowlist and public base URL are
placeholders.

`public_base_url` is the one value that must be an externally reachable
address rather than a cluster-local one. GitHub returns an operator's browser
to it after each approval in the connection flow, so it is the operator-plane
hostname — and it is stated rather than taken from a request, because a
redirect target read from a `Host` header is one the caller chose.

## The one secret this deployment supplies

```yaml
secretRef:
  name: saas-fabric-control-plane-secrets   # namespace: platform-system
```

| OpenBao path | Property | Becomes |
|---|---|---|
| `platform/saas-fabric/keycloak` | `client-secret` | `FABRIC_SECRET_KEYCLOAK_SAAS_FABRIC` |

It used to be two. **The GitHub App's private key is no longer delivered here
because it is no longer delivered at all** — the platform creates its own
application when an operator connects it and writes the key straight into its
own secret partition. External Secrets could not do that job: projecting a
secret into a pod is one-way, and the platform now generates credential
material of its own.

What remains is *issued elsewhere and consumed here*, which is what makes this
the right delivery path for it — unlike Keycloak's admin password, generated
in-cluster because nobody needs to choose or transport it.

Named with `data[]` rather than `dataFrom.extract`, because the environment
variable name is derived from a reference in the application's configuration
(`keycloak/saas-fabric` → `FABRIC_SECRET_KEYCLOAK_SAAS_FABRIC`), and `data[]`
names one exact key where `find.path` would take everything under a prefix.

**Nothing secret is in this directory**, and after this change nothing about a
Git host is either.

## The instance's own secret partition

The control plane writes as well as reads, which is new. It keeps two things
under `secret/platform/saas-fabric/instances/master/`:

| Name | Holds |
|---|---|
| `git/app-private-key` | the application's private key, which GitHub returns exactly once |
| `git/integration` | the record: application id, slug, installation, repository |

It authenticates with the **pod's own Kubernetes identity**, so there is still
no static credential for anybody to create, transport or rotate. That is why
the API deployment sets `automountServiceAccountToken: true` where it used to
be `false`; the console keeps it `false`, because a static file server has no
reason to hold an identity.

### The OpenBao role and policy

The existing `platform-secrets` policy grants **read** on `secret/platform/*`,
which is not enough — this is the first workload that writes. Widening that
policy would give every reader write access to every platform secret, so this
gets a role of its own and the grant is deliberately *narrower*: write inside
one instance's partition, nothing outside it.

**On LucentRoot it is declarative and automatic.** It is part of
`environments/lucentroot/config/openbao.yaml`, applied by OpenBao's
self-initialisation at first start, so a rebuilt cluster has it without anybody
running a command.

**Two cases still need the commands below**: production, whose OpenBao is
initialised deliberately with recovery material rather than self-initialising,
and any *already-initialised* instance — the stanza runs once at first start,
so an OpenBao that came up before this change will not have picked it up.

```bash
bao policy write saas-fabric-control-plane - <<'POLICY'
path "secret/data/platform/saas-fabric/instances/master/*" {
  capabilities = ["create", "read", "update"]
}
path "secret/metadata/platform/saas-fabric/instances/master/*" {
  capabilities = ["read", "list", "delete"]
}
POLICY

bao write auth/kubernetes/role/saas-fabric-control-plane \
  bound_service_account_names=saas-fabric-control-plane \
  bound_service_account_namespaces=platform-system \
  policies=saas-fabric-control-plane ttl=1h
```

`delete` is on the metadata path rather than the data path on purpose: deleting
through `data` marks the latest version deleted and leaves earlier ones
readable, which for a private key is not deletion at all.

## Identity: two machine identities, no human credential

| Against | Identity | Permission | Established by |
|---|---|---|---|
| Keycloak | service account on the `saas-fabric` client, `master` realm | `create-realm` only | one-time bootstrap |
| OpenBao | the pod's own Kubernetes service account | write within this instance's partition | one-time bootstrap, above |
| GitHub | an installation of an application the platform created | `contents: write`, `metadata: read` | **an operator, in the product** |

`create-realm` is sufficient on its own: creating a realm makes the service
account that realm's administrator, so no further grant is bootstrapped.

The third row is the change. There is no GitHub credential in this repository,
in OpenBao ahead of time, or in anyone's hands: an operator connects the
integration through the console, GitHub returns a private key exactly once, and
the platform writes it into its own partition. Nothing about it is a
deployment-time prerequisite.

Operators authenticate to the console through the tailnet. The control plane
consumes the identity the operator-plane proxy established
(`Tailscale-User-Login`) and checks it against an allowlist that **may not be
empty** — the tailnet establishes who someone is, the allowlist establishes that
they administer this platform.

## Current state

The Keycloak half is proven against this cluster: realm, roles and OIDC client
created, idempotent across repeated sweeps, and drift detected and corrected.

**No Git integration exists yet, and that is now a supported state rather than
a broken one.** The control plane starts, serves, reports itself as not
connected, and offers an operator the flow to connect one. Nothing here has to
be edited for that to happen.

Two things do still need doing before this deployment is fully healthy, and
both are one-time operator steps against a running cluster rather than changes
to this repository:

1. **The OpenBao role and policy above.** Without them the control plane starts
   and serves, but cannot store what a connection produces.
2. **The Keycloak master realm's console client and `fabric-operator` role**,
   before the operator posture can move from `trusted_header` to `oidc`. The
   LucentRoot overlay carries the replacement block in a comment, and flipping
   it before the realm has both would lock every operator out of the console —
   including the console that would be used to fix it.

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
