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
