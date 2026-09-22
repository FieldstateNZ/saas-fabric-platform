# Platform gateway

| | |
|---|---|
| Product | Gateway API `GatewayClass` and `Gateway` |
| Upstream project | https://github.com/kubernetes-sigs/gateway-api |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | CRDs supplied by the pinned Envoy Gateway chart |
| Licence | Apache-2.0 |
| Namespace | `platform-system` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `10` |

## Why it exists in SaaS Fabric

The cluster's north/south edge. [`../envoy-gateway`](../envoy-gateway/) installs
the machinery; this defines the actual routing authority the platform and its
clients attach to.

It is a separate Application so the dependency is expressed rather than assumed:
the CRDs and controller are wave `0`, the `Gateway` is wave `10`, and the routes
that attach to it are wave `20` and later.

## The ownership boundary

This is where the platform/client split is enforced in routing.

| Resource | Owner |
|---|---|
| `GatewayClass` `saas-fabric` | this repository |
| `Gateway` `platform` and its listeners | this repository |
| TLS termination on the listener | this repository (certificate injected externally) |
| `HTTPRoute` for `fabric.<domain>`, `auth.<domain>` | this repository, alongside each service |
| `HTTPRoute` for `acme.<domain>` and other client hosts | client OpenTofu |

Platform hostnames are platform concerns. Client hostnames are not, and no
client hostname appears in this repository.

## How client routes attach

Client routes are created by OpenTofu in the client's own namespace and attach
to this Gateway across namespaces. The Gateway permits that by label rather than
by `from: Same`:

```yaml
allowedRoutes:
  namespaces:
    from: Selector
    selector:
      matchLabels:
        fieldstate.nz/gateway-access: "true"
```

A namespace may attach routes when it carries
`fieldstate.nz/gateway-access: "true"`. This is the platform-to-client
interface: OpenTofu applies that label when it creates a client namespace, and
the platform never has to be changed to admit a new client.

Platform namespaces receive the label through each Application's
`managedNamespaceMetadata`.

## Listeners

| Environment | Listeners |
|---|---|
| LucentRoot | `http` on 80 |
| Production | `http` on 80, `https` on 443 with TLS termination |

LucentRoot has no HTTPS listener because there is no certificate authority for
`*.lucentroot.internal`. Routes select their listener by `sectionName`, which is
the one place an environment's routing differs.

## The operator listener asserts its own scheme

`overlays/lucentroot/operator-scheme.yaml` puts a `ClientTrafficPolicy` on the
`operator` listener. Configuring client-IP detection on it is what makes
Envoy Gateway turn off the connection manager's default behaviour of
overwriting `X-Forwarded-Proto` with the plain-HTTP transport of its own hop
from the Tailscale ingress; an early header assertion of `https`, paired with
it, is what then actually survives into the pseudo-header `:scheme` once that
overwrite is off. Neither alone achieves the fix — see that file for why
they are one mechanism, and for the failure mode of dropping either half.
This is invisible until something reads `:scheme` rather than the header,
which is exactly what the OIDC sign-in Envoy runs for the control-plane
instance does to build its post-login and post-logout redirect URLs. See that
file and
[`../operator-access/base/keycloak-routes.yaml`](../operator-access/base/keycloak-routes.yaml)
for the full mechanism, including the side effect on client-IP detection and
why an h2c downstream would bypass this.

**Verifying this does not need the OIDC policy — reading the proxy's own
config does.** Keycloak's own route
(`operator-access/base/keycloak-routes.yaml`) asserts `X-Forwarded-Proto:
https` for itself already, independent of whether this policy does anything
at all — so fetching the master realm's discovery document through the
gateway and finding `https://` endpoints in it is not evidence this policy
works; it would read exactly the same if the policy were broken or absent.

1. **After this Application syncs, inspect what Envoy actually loaded.**
   Envoy Gateway's managed proxy Deployment for this `Gateway` runs in this
   same namespace, named `envoy-platform-system-platform-<hash>` — find it
   rather than guessing the hash, by the same owning-gateway labels
   `platform-operator-edge` (`operator-edge.yaml`) already selects on:

   ```console
   $ kubectl -n platform-system get deploy \
       -l gateway.envoyproxy.io/owning-gateway-name=platform,gateway.envoyproxy.io/owning-gateway-namespace=platform-system
   ```

   Envoy's admin interface listens on `127.0.0.1:19000` inside that pod, and
   the managed proxy image is distroless — no shell, no `wget`, no `curl` —
   so `kubectl exec` cannot fetch it. A port-forward enters the pod's network
   namespace, which is what the loopback bind requires:

   ```console
   $ kubectl -n platform-system port-forward deploy/envoy-platform-system-platform-<hash> 19000:19000
   $ curl -s 'http://localhost:19000/config_dump?resource=dynamic_listeners'
   ```

   Look at the `operator` listener's `HttpConnectionManager`:

   Correct looks like `"use_remote_address": false` on that listener's HCM,
   and an `early_header_mutation_extensions` entry
   (`envoy.http.early_header_mutation.header_mutation`) whose mutation sets
   `X-Forwarded-Proto` — that exact case; Envoy Gateway does not lowercase
   it — to `https` with `OVERWRITE_IF_EXISTS_OR_ADD`. `use_remote_address`
   appears explicitly even when false because it is a protobuf wrapper, so
   an absent field is a different finding from a false one. This confirms the mechanism is loaded —
   the thing `operator-scheme.yaml` actually claims — without needing
   anything the OIDC policy PR adds.

2. **The end-to-end proof still needs that PR.** Once the control-plane
   instance's OIDC `SecurityPolicy` (a separate change, which itself depends
   on step 1 having been done and confirmed correct — see its own README)
   is live, confirm a sign-in on the console returns the browser to an
   `https://` URL rather than failing on an `http://` one the Tailscale
   Ingress does not serve. This is what proves the mechanism works under a
   real request, not only that it is present in the config.

3. **Keycloak's own route-level filter is removed only after step 2** has
   actually been observed, not after step 1 alone and not bundled with
   introducing this policy. It stays until then as a second,
   independently-verified assertion of the same fact.

## Required external secret

Production only:

```yaml
certificateRefs:
  - kind: Secret
    name: platform-tls   # namespace: platform-system
```

One certificate on the listener, rather than one per service. It is injected
externally and never committed. Automated issuance is a known gap — see
[docs/architecture.md](../../../docs/architecture.md#known-gaps).

## Dependencies

[Envoy Gateway](../envoy-gateway/) at wave `0`, for the controller and the
Gateway API CRDs.

## Configuration owned by this repository

- the `GatewayClass` and its controller binding;
- the `Gateway`, its listeners and their ports;
- TLS termination and the certificate reference;
- which namespaces may attach routes.

## Configuration expected from outside this repository

- **the `platform-tls` certificate**, injected externally;
- **DNS** for the platform hostnames, from `saas-fabric-hosting`;
- **client routes and the labelling of client namespaces**, from the client
  layer.
