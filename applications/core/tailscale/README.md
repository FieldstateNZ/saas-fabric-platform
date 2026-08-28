# Tailscale operator

| | |
|---|---|
| Product | Tailscale Kubernetes operator |
| Upstream project | https://github.com/tailscale/tailscale |
| Helm chart source | https://pkgs.tailscale.com/helmcharts |
| Chart version (pinned) | `1.102.3` |
| Application version | `v1.102.3` |
| Licence | BSD-3-Clause |
| Namespace | `tailscale` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `0` |
| Plane | **operator** |

## Why it exists in SaaS Fabric

The platform needs a way for the people who run it to reach administrative
surfaces — Argo CD, Grafana, OpenBao's UI for break-glass — without putting any
of them on the product edge. Not every upstream console qualifies: Keycloak's is
published on no plane, because SaaS Fabric administers it through its API.

It is core rather than catalogue because it is not a capability a client
enables. It is part of how the platform is operated, and applying the
core/catalogue test to it gives the wrong answer for the wrong reason: SaaS
Fabric does not *call* the Tailscale operator, but the platform cannot be
administered safely without an equivalent. The catalogue is a product surface,
and this is not a product.

## The two planes

This is one half of a deliberate split. See
[docs/architecture.md](../../../docs/architecture.md#exposure-planes) for the
full contract.

```text
Product plane                     Operator plane
Envoy Gateway                     Tailscale
HTTPRoute on the platform Gateway ingressClassName: tailscale
public / client-reachable         tailnet-only, private
```

A hostname belongs to a plane for a stated reason, and some services appear on
both. Client routing never goes through Tailscale.

## Ownership boundary

| Resource | Owner |
|---|---|
| Operator, its CRDs, RBAC and the `tailscale` IngressClass | this repository |
| Operator-plane `Ingress` for a platform service | this repository, with that service |
| `ts-*` proxy StatefulSets | the operator, at runtime |
| Tailnet ACL policy, tag ownership, OAuth client | outside Kubernetes entirely |
| Client routing | **never here, and never Tailscale** — client `HTTPRoute`s on the product plane, owned by client OpenTofu |

## Required external secret

```yaml
secretRef:
  name: operator-oauth   # namespace: tailscale
  keys: [client_id, client_secret]
```

The chart is configured with an empty `oauth` block, so it renders no Secret and
expects this one to exist. It is injected at bootstrap and never committed — see
[docs/bootstrap.md](../../../docs/bootstrap.md).

This is deliberately a bootstrap secret rather than one delivered from OpenBao.
Sourcing it from OpenBao would make the operator plane depend on OpenBao being
up, which is the wrong way round: the operator plane is how you reach OpenBao
when it is not. See
[docs/architecture.md](../../../docs/architecture.md#the-bootstrap-secret-boundary).

## Tailnet prerequisites

None of this is Kubernetes configuration, and none of it lives here:

- an OAuth client with the `devices` scope, owning `tag:k8s-operator`;
- `tag:k8s-operator` listed as a `tagOwner` of `tag:k8s` in the tailnet ACL
  policy, or the operator cannot create proxies;
- ACL grants for whoever should reach the operator plane.

## Dependencies

None. Wave `0`.

## Configuration owned by this repository

- the operator deployment, its CRDs and RBAC;
- the `tailscale` IngressClass;
- ACL tags applied to the operator and its proxies;
- whether the API server proxy is enabled.

## Configuration expected from outside this repository

- **the `operator-oauth` secret**, injected at bootstrap;
- **the tailnet ACL policy**, including tag ownership;
- **tailnet user and group membership**, which is what actually decides who can
  reach the operator plane.

## Notes

`apiServerProxyConfig.mode: "true"` lets tailnet users with the
`tailscale.com/cap/kubernetes` grant reach the Kubernetes API without a static
kubeconfig. That is a real privilege boundary and it is enforced by the tailnet
ACL, not by this repository.
