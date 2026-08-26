# Operator access

| | |
|---|---|
| Product | operator-plane `Ingress` resources |
| Upstream project | https://github.com/tailscale/tailscale |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | supplied by the pinned Tailscale operator |
| Licence | BSD-3-Clause (the operator that fulfils these) |
| Namespace | `argocd`, and any namespace holding an operator surface |
| Class | core |
| Sync wave | `10` |
| Plane | **operator** |

## Why it exists in SaaS Fabric

Most platform services describe their own operator-plane exposure through their
chart's values — OpenBao, Keycloak and Grafana all render a Tailscale `Ingress`
from `environments/<env>/config/<app>.yaml`. This Application is for the ones
that cannot.

Today that is Argo CD, which is installed by `saas-fabric-hosting` and so has no
values file in this repository. How Argo CD is *reached* is still a platform
decision, and the platform's answer is: over the tailnet, never from the product
edge.

## What belongs here, and what does not

| | |
|---|---|
| **Belongs here** | operator-plane access to a service this repository does not otherwise render — currently Argo CD |
| **Does not belong here** | access to a service whose chart can render its own `Ingress`. Put it in that service's environment config, next to the rest of its configuration |
| **Never belongs here** | anything on the product plane, and anything client-scoped |

Keeping the exposure next to the service is the default because it keeps one
service's configuration in one place. This directory is the exception, not a
central registry of ingresses.

## Dependencies

[Tailscale operator](../tailscale/) at wave `0`, for the `tailscale`
IngressClass. Without it these Ingress resources are created and simply never
fulfilled.

## Configuration owned by this repository

- which platform services are reachable on the operator plane;
- their tailnet hostnames, per environment.

## Configuration expected from outside this repository

- **the tailnet ACL policy**, which decides who can actually reach these
  hostnames. An Ingress here makes a service reachable *from the tailnet*; it
  does not decide who is on it.

## Environments

Enabled on LucentRoot. Production does not run an operator plane yet — it has no
tailnet — so it has no overlay here. Adding one is a new overlay directory plus
two lines in that environment's kustomization.
