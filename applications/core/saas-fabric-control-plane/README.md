# SaaS Fabric control plane

| | |
|---|---|
| Product | SaaS Fabric (control plane) |
| Upstream project | https://github.com/FieldstateNZ/saas-fabric |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Container image | `ghcr.io/fieldstatenz/saas-fabric-control-plane` |
| | `ghcr.io/fieldstatenz/saas-fabric-control-plane-ui` |
| | versions and digests in [LucentRoot's components](../../../environments/lucentroot/components.yaml) |
| Licence | Fieldstate |
| Namespace | `operator-system` |
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

**A namespace of its own, carrying `operator-gateway-access` and not
`gateway-access`.** That is what makes "operator plane only" a boundary rather
than a habit.

It was `platform-system` with the label merely omitted from this Application,
which read like a guarantee and was not: that namespace carries `gateway-access`
from its other tenants, so a route in it was eligible for the product listener
regardless. The route naming `sectionName: operator` was then a choice the route
made about itself — convention, not isolation.

A namespace with one grant and not the other is enforced by the `Gateway`'s own
selector: a route here that named the product listener is refused, by
Kubernetes, without anybody reviewing it. `scripts/check.py` asserts the
namespace never acquires the other label.

Published through [`../operator-access`](../operator-access/):

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

The base file is not deployable: its issuer, its reachable address and its
public base URL are placeholders.

`public_base_url` is the one value that must be an externally reachable
address rather than a cluster-local one. GitHub returns an operator's browser
to it after each approval in the connection flow, so it is the operator-plane
hostname — and it is stated rather than taken from a request, because a
redirect target read from a `Host` header is one the caller chose.

## This deployment supplies no credential

There is no `ExternalSecret` here and no `envFrom`. That is the whole of it,
and it took two changes to get to:

| Against | How authority is obtained |
|---|---|
| GitHub | the platform creates its own application when an operator connects it, and writes the key into its own secret partition |
| Keycloak | the platform holds none — it acts as the operator who asked, with the bearer they presented |

External Secrets could not have done the first job anyway: projecting a secret
into a pod is one-way, and the platform now *generates* credential material.
The second is not a delivery problem at all — permission to create a realm
belongs to a person in the master realm, and a service account standing in for
them was the thing worth removing.

What the pod still holds is a **service-account token**, which is an identity
rather than a credential somebody issued. See below.

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
  bound_service_account_namespaces=operator-system \
  policies=saas-fabric-control-plane ttl=1h
```

`delete` is on the metadata path rather than the data path on purpose: deleting
through `data` marks the latest version deleted and leaves earlier ones
readable, which for a private key is not deletion at all.

## Sign-in moves to the gateway

ADR 0024 ("An instance is a realm, signed in at the gateway, and product
surfaces are federated modules") makes this the first instance the gateway
signs in for. A `SecurityPolicy` on this Application's `HTTPRoute`
(`overlays/lucentroot/oidc.yaml`) runs Envoy's OIDC filter against the master
realm in front of both backends it routes to — `/api` and `/` alike — so
nothing behind it, API or console, is reachable by a browser Envoy has not
already put through the authorization-code flow.

**The browser never holds a token.** Envoy holds the session in its own
HMAC-signed cookies and forwards the verified access token upstream as
`Authorization: Bearer`, which is exactly the shape the operator posture
already verifies — issuer, `azp`, the `fabric-operator` realm role. Nothing
about that verification changes; what changes is who redeems the code.

The master realm's access tokens are short-lived, so a signed-in operator's
session depends on Envoy refreshing them often — `oidc.yaml` states
`refreshToken: true` for exactly this. A refresh failure — the refresh token
itself expiring from disuse, or Keycloak being briefly unreachable — drops
the operator back to sign-in on their next request rather than surfacing as
an error partway through one.

**Two paths belong to the filter now, not to this deployment.**
`/oauth2/callback` is where Keycloak returns the browser after login, and
`/logout` clears the session; both are matched ahead of this Application's own
rules, on the same route, and neither reaches the `saas-fabric-control-plane`
API or the `saas-fabric-control-plane-ui` bundle.

`/logout` has a nuisance worth knowing rather than a bug worth fixing: the
filter clears the session cookies on *any* request that reaches that path,
not only one an operator meant as a sign-out — there is no CSRF token or
confirmation step in front of it. It is reachable only from the tailnet, on
an instance with one operator today, which is why this is documented rather
than mitigated.

### Hand-made master-realm prerequisites

The platform automates none of the realm today (see [Identity and bootstrap
prerequisites](#identity-and-bootstrap-prerequisites) below for the rest of
it). Before this policy can complete a sign-in, the master realm needs, by
hand:

- A confidential client `saas-fabric-gateway` — standard flow on, client
  authentication on, valid redirect URI
  `https://fabric-lucentroot.tail5a7546.ts.net/oauth2/callback`, valid
  post-logout redirect URI `https://fabric-lucentroot.tail5a7546.ts.net/*`,
  and realm roles left in the access token (the default — this is what puts
  `fabric-operator` in `realm_access.roles` for the operator posture to
  check). No web origins entry: this client is driven server-side, by Envoy,
  never by a script running in a browser on some other origin.
- The `fabric-operator` realm role assigned to the operator's own user — the
  client being configured right does not by itself grant anyone the role the
  operator posture checks for.
- The client's default client scopes left holding Keycloak's own defaults,
  which include `profile` — `oidc.yaml` requests it, and it is the one of the
  policy's three scopes that is load-bearing rather than convenient: it is
  what puts `preferred_username` in the token at all. Without it, every audit
  record this deployment writes falls back to the subject's UUID, which is
  correct but unreadable.
- Its secret written to OpenBao at
  `secret/platform/saas-fabric-gateway-oidc` under key `client-secret`, which
  `oidc.yaml`'s `ExternalSecret` projects into the Secret the `SecurityPolicy`
  reads.

**Envoy Gateway does not fail open, but an unresolved reference at sync time
is not a silent `500` either.** If the `ExternalSecret` above has not yet
produced that Secret, or produced one without a `client-secret` key, the
`SecurityPolicy`'s `clientSecret` reference is unresolved — Envoy Gateway
marks the policy `Accepted: False, reason: Invalid`, and Argo CD surfaces
that as a Degraded Application at sync time, loudly, before any operator is
affected. `oidc.yaml` orders the grant and the `ExternalSecret` a wave ahead
of the policy within this Application for exactly this reason: not to avoid
a `500`, but to avoid the policy ever landing unresolved and Degraded in the
first place.

The `500` failure mode is real, but reached a different way: if this Secret
is deleted or its key corrupted *after* the policy has already synced
successfully once, Envoy already has a working policy programmed rather than
one waiting to resolve — so losing the secret it depends on breaks something
that was working, on every route the policy targets, `/api` and `/` alike.
That is the state worth watching for once this is running (an
`ExternalSecret` going unhealthy), not the state the pre-merge check below
guards against.

The pre-merge check is against OpenBao, not Kubernetes — the Secret above
does not exist until this PR's `ExternalSecret` has synced, so checking the
Kubernetes Secret before merging is circular. Confirm the OpenBao value
directly:

```console
$ bao kv get secret/platform/saas-fabric-gateway-oidc
```

and look for a `client-secret` key in the output. After merging and syncing,
confirm the Secret it produced, and the policy's own health:

```console
$ kubectl -n operator-system get secret saas-fabric-gateway-oidc \
    -o jsonpath='{.data.client-secret}' | wc -c
$ kubectl -n operator-system get securitypolicy saas-fabric-control-plane-oidc \
    -o jsonpath='{.status.ancestors[*].conditions}'
```

A count of `0` on the first means the `ExternalSecret` has not synced.
Anything but `Accepted: True` on the second means Argo CD should already be
reporting this Application Degraded — check there first.

### Rollout order

**The operator listener's own scheme-assertion policy
(`platform-gateway`'s `overlays/lucentroot/operator-scheme.yaml`, a separate
change) has to merge first, and its own observation made — before this
one.** That observation does not need a sign-in and so does not need this
PR: it is Envoy's own config dump, showing the operator listener's
`HttpConnectionManager` with `use_remote_address: false` and the early
`X-Forwarded-Proto: https` mutation actually loaded (see that Application's
README for the exact check). Confirming that first is what breaks the
circularity a sign-in-only verification would otherwise create — this PR
cannot itself prove the scheme policy works, since a sign-in exercises both
changes at once.

Once that is confirmed, merge this PR. **The first sign-in through the
console after that is the end-to-end observation of both changes together —
there has been no earlier point at which it could be tested.** It either
returns to an `https://` URL, proving both, or it returns to `http://`. If
it returns to `http://` despite the config dump above having shown the
right configuration, the scheme policy is still the suspect — revert this
`SecurityPolicy`, not it, first: without this policy the operator route
returns to exactly what it is today, reachable only from the tailnet, with
no sign-in in front of it at all. An absent sign-in is the safe direction to
fail in while the scheme policy gets debugged; a broken one, with operators
mid-flow, is not. Merging this PR before the scheme policy's own
observation is not a smaller version of that risk — without it `:scheme` on
this listener is `http` from the start, and Envoy's OAuth2 filter builds
both the post-login return URL and `post_logout_redirect_uri` from
`:scheme`, so this `SecurityPolicy` would sync healthy and break every
sign-in at runtime regardless. This is a hard ordering, not a
recommendation.

**This PR also changes what a secret problem can block.** Before it, an
issue with `secret/platform/saas-fabric-gateway-oidc` affected only this
`SecurityPolicy`'s own health. After it, because the grant and the
`ExternalSecret` sync at wave `-1` and Argo CD waits for a wave to be
Healthy before starting the next, a missing, mis-keyed or sealed OpenBao
value now blocks wave `0` — the Deployments, Services, ConfigMap and route
in this Application, not only the policy. On a fresh rebuild the control
plane would not deploy at all; on a running cluster, an image bump would not
roll. Deliberate: the alternative is a window with the operator route
unprotected (see "Envoy Gateway does not fail open" above), and the
pre-merge `bao kv get` check is what keeps this theoretical rather than
something discovered at sync time.

**Once the scheme policy is merged and observed, and this PR has followed it:
the console build carrying the gateway short-circuit has to be the one
LucentRoot is already running before this merges — its overlay's image tag
will have advanced past
`0.3.0-preview.14` (see `overlays/lucentroot/kustomization.yaml`).**
`client_id` in `control-plane.toml` fires the moment this merges and syncs,
whether or not the console has caught up: it now names `saas-fabric-gateway`,
not `saas-fabric-console` — the client every verified bearer is actually
issued to, once the gateway is what redeems codes. An old console build does
not know that. It still starts its own PKCE sign-in on load, which would ask
Keycloak to authorize the code against `saas-fabric-gateway` — a confidential
client — and this deployment holds no secret for it to redeem the code with
(see [This deployment supplies no credential](#this-deployment-supplies-no-credential)
above). That sign-in would fail every time, for every operator, regardless of
whether the `SecurityPolicy` above is even healthy. The ADR's own build order
(item 1, "what is built first") exists for exactly this: land the console
build that finds `GET /api/operator` already answering and skips redoing its
own sign-in, running on LucentRoot, before this merges.

## Identity and bootstrap prerequisites

| Against | Identity | Permission | Established by |
|---|---|---|---|
| Keycloak | the signed-in operator's bearer | `fabric-operator` for Fabric access; master-realm `admin` for realm administration | one-time operator bootstrap |
| OpenBao | the pod's Kubernetes service account | this instance's partition | bootstrap role and policy above |
| GitHub | installations of the applications Fabric creates | selected client/platform repositories | an operator connects each integration in Fabric |

The current Keycloak adapter borrows the operator's token. It does not mint a
service-account token. `create-realm` alone cannot support first-pass
reconciliation: grants earned by creating a realm appear only in later tokens.

Before using the console, configure the master realm with:

- A public `saas-fabric-console` client requiring S256 PKCE and the exact
  redirect `https://fabric-lucentroot.tail5a7546.ts.net/`.
- A `fabric-operator` realm role assigned to the operators, and master-realm
  `admin` authority for operators who reconcile realms.
- The realm attribute `frontendUrl` set to
  `https://fabric-lucentroot.tail5a7546.ts.net` (the origin, without `/realms/master`).

The last setting is required on LucentRoot because the browser and the adapter
reach Keycloak at different addresses. Without a canonical master-realm URL,
Keycloak rejected a valid operator token on the internal Admin API with 401,
while accepting the same token with the public origin. Pinning the realm URL
made internal administrative calls succeed without any permission changes.
This is a master-realm setting; do not substitute the operator origin for the
issuers of client realms.

These are currently one-time Keycloak state changes, not resources reconciled
by this repository. Include them when rebuilding LucentRoot. An authenticated
console alone does not prove the administrative path: run reconciliation and
verify its per-client outcome.

The `saas-fabric-console` client above is what the console used to sign
itself in with, directly. [Sign-in moves to the
gateway](#sign-in-moves-to-the-gateway) above adds a second, confidential
client the gateway signs in with instead; the console's own PKCE flow against
this one retires in ADR 0024 slice 2, alongside `/api/session`.

## Verified baseline

On 2026-09-17, LucentRoot ran `0.3.0-preview.11` with both operator Deployments
ready and the Argo CD Application Synced/Healthy. Normal authorization-code +
PKCE sign-in and the client, catalogue, operator and platform APIs succeeded.
Acme's exact test callback is `http://acme.lucentroot.internal/`; its former
wildcard was incompatible with the private-network redirect strategy.
Reconciliation reached Applied, and a second pass required no further changes.

Automatic updates were resumed through the console. Fabric's platform GitHub
App wrote commit `921298aee1b98388283c919f36698bcca8aa3e67`, removing the hold;
the following platform status reported automatic policy and a successful check.
The upgrade itself was a coordinated repository change, not proof of a new
release being advanced by the automatic updater in this verification session.

See [the readiness record](../../../docs/fabric-readiness.md) for evidence,
recovery constraints and remaining acceptance work.

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
