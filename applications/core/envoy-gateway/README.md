# Envoy Gateway

| | |
|---|---|
| Product | Envoy Gateway |
| Upstream project | https://github.com/envoyproxy/gateway |
| Helm chart source | `oci://docker.io/envoyproxy/gateway-helm` |
| Chart version (pinned) | `1.9.0` |
| Application version | `v1.9.0` |
| Licence | Apache-2.0 |
| Namespace | `platform-system` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `0` |
| Plane | **product** |

## Why it exists in SaaS Fabric

Envoy is the platform's **product** routing layer. Every product-facing platform
service — SaaS Fabric, Keycloak's OIDC endpoints — and, later, every client
hostname is published through it, so the data plane must exist before anything
that needs to be reached.

This Application installs the **control plane and the Gateway API CRDs only**.
The `GatewayClass` and the shared `Gateway` are platform-owned resources and
live in [`../platform-gateway`](../platform-gateway/), one wave later.

## One product routing authority

Envoy is the only thing that carries product or client traffic. There is no
second product-facing ingress controller: two of them means two places a
hostname can be claimed, and the ownership split between the platform and the
client layer stops being enforceable.

Administrative access is a separate plane entirely — the
[Tailscale operator](../tailscale/), reachable only from the tailnet, carrying
no client traffic ever. The two do not overlap, and a service is on a plane for
a stated reason. See
[docs/architecture.md](../../../docs/architecture.md#exposure-planes).

`scripts/check.py` fails the build on any `Ingress` that is not
`ingressClassName: tailscale`, and on any `IngressClass` the Tailscale operator
does not own.

## Ownership boundary

| Resource | Owner |
|---|---|
| Envoy Gateway control plane, Gateway API CRDs | this repository |
| `GatewayClass`, shared `Gateway`, listeners | this repository |
| `HTTPRoute` for `fabric.<domain>` and Keycloak's OIDC paths | this repository |
| `HTTPRoute` for `acme.<domain>` and other client hosts | client OpenTofu |
| Client namespace and its gateway-access label | client OpenTofu |
| Administrative access to anything | not this plane — see [Tailscale](../tailscale/) |

## Dependencies

None. Wave `0`; nothing in the platform precedes it.

## Configuration owned by this repository

- the Envoy Gateway control plane deployment and its resources;
- the Gateway API CRDs;
- log level and per-environment replica count.

## Configuration expected from outside this repository

- **DNS.** Records pointing at the Envoy proxy's load balancer are created by
  `saas-fabric-hosting` (production) or resolved locally (LucentRoot).
- **TLS certificates**, referenced by the Gateway listener rather than by
  individual services. See [`../platform-gateway`](../platform-gateway/).
- **Client routes**, from the client layer.

## Notes

The chart is published only to an OCI registry, so this Application needs an
Argo CD able to resolve `oci://` source repositories — 2.13 or later. That
requirement is part of the platform's
[Argo CD runtime contract](../../../docs/architecture.md#argo-cd-runtime-contract).

k3s ships Traefik by default. LucentRoot clusters should be installed with
`--disable=traefik`: it is a product-facing ingress controller, and a second one
is exactly what this plane must not have. See
[docs/bootstrap.md](../../../docs/bootstrap.md).
