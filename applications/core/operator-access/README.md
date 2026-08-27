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
| Sync wave | `50` |
| Plane | **operator** |

## Why it exists in SaaS Fabric

Every operator-plane `Ingress` for a platform service, in one Application, in a
wave nothing depends on.

It holds Argo CD's, which has no values file here because
`saas-fabric-hosting` installs it, and Keycloak's and OpenBao's, which could be
rendered by their charts but must not be — see below.

## Why operator-plane Ingresses live here rather than with their service

Keeping a service's configuration in one place is normally right, and this is
the exception, for a concrete reason found by running the platform.

Argo CD treats an `Ingress` with no load-balancer address as **Progressing**.
An operator-plane Ingress rendered by a service's own chart therefore makes that
service's health depend on the Tailscale operator — and because the platform
gates sync waves on health, a broken operator plane stops the product plane from
deploying at all. Keycloak rendering its own tailnet Ingress was enough to hold
back SaaS Fabric.

Collecting them here confines that coupling to one Application in a terminal
wave, which nothing depends on. If the operator plane is broken, you lose
administrative access and nothing else.

| | |
|---|---|
| **Belongs here** | operator-plane access to any platform service — Argo CD, Keycloak's admin surface, OpenBao's UI |
| **The one exception** | a catalogue application. `catalogue` is not in the platform project's destinations, and catalogue is a terminal wave already, so its chart may render its own. Grafana does |
| **Never belongs here** | anything on the product plane, and anything client-scoped |

## Dependencies

[Tailscale operator](../tailscale/) at wave `0`, for the `tailscale`
IngressClass. Without it these Ingress resources are created and simply never
fulfilled — which is the whole point of putting them last: that failure stays
here instead of propagating into the product plane.

## Configuration owned by this repository

- which platform services are reachable on the operator plane;
- their tailnet hostnames, per environment;
- the Ingress resources themselves, rather than each service's chart.

## Configuration expected from outside this repository

- **the tailnet ACL policy**, which decides who can actually reach these
  hostnames. An Ingress here makes a service reachable *from the tailnet*; it
  does not decide who is on it.

## Environments

Enabled on LucentRoot. Production does not run an operator plane yet — it has no
tailnet — so it has no overlay here. Adding one is a new overlay directory plus
two lines in that environment's kustomization.
