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
| Class | core |
| Sync wave | `0` |

## Why it exists in SaaS Fabric

Envoy is the platform's routing layer. Every externally reachable platform
service — Keycloak, SaaS Fabric itself — and, later, every client hostname is
published through it, so the data plane must exist before anything that needs to
be reached.

This Application installs the **control plane and the Gateway API CRDs only**.
The `GatewayClass` and the shared `Gateway` are platform-owned resources and
live in [`../platform-gateway`](../platform-gateway/), one wave later.

## One routing authority

There is deliberately no second ingress controller. Two routing authorities in
one cluster means two places a hostname can be claimed and two answers to
"where does this request go", and the ownership split between the platform and
the client layer stops being enforceable.

`scripts/check.py` fails the build if any `Ingress` resource is rendered.

## Ownership boundary

| Resource | Owner |
|---|---|
| Envoy Gateway control plane, Gateway API CRDs | this repository |
| `GatewayClass`, shared `Gateway`, listeners | this repository |
| `HTTPRoute` for `fabric.<domain>`, `auth.<domain>` | this repository |
| `HTTPRoute` for `acme.<domain>` and other client hosts | client OpenTofu |
| Client namespace and its gateway-access label | client OpenTofu |

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
`--disable=traefik`, for the same one-routing-authority reason above. See
[docs/bootstrap.md](../../../docs/bootstrap.md).
