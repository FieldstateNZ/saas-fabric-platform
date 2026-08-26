# Ingress

| | |
|---|---|
| Product | ingress-nginx |
| Upstream project | https://github.com/kubernetes/ingress-nginx |
| Helm chart source | https://kubernetes.github.io/ingress-nginx |
| Chart version (pinned) | `4.15.1` |
| Application version | `1.15.1` |
| Licence | Apache-2.0 |
| Namespace | `platform-system` |
| Class | core |
| Sync wave | `0` |

## Why it exists in SaaS Fabric

Every externally reachable platform service — Keycloak, OpenBao's UI, SaaS
Fabric itself — needs a single, environment-independent way to be published.
The ingress controller is the platform's north/south edge, so it is a hard
prerequisite rather than a convenience.

## Dependencies

None. This is a wave `0` component; nothing in the platform is required before
it.

## Configuration owned by this repository

- the `nginx` `IngressClass` and controller deployment;
- proxy buffer sizing and forwarded-header handling required by Keycloak;
- controller replica count, service type and resources per environment.

## Configuration expected from outside this repository

- **DNS.** Records pointing at the controller's load balancer are created by
  `saas-fabric-hosting` (production) or resolved locally (LucentRoot).
- **TLS certificates.** Platform Ingresses reference a TLS secret by name; the
  secret itself is injected externally. Automated certificate issuance
  (cert-manager) is a known gap — see [docs/architecture.md](../../../docs/architecture.md#known-gaps).
- **Client hostnames.** `acme.fieldstate.nz` and similar are created by client
  provisioning, never here. Only platform hostnames such as
  `fabric.fieldstate.nz` and `auth.fieldstate.nz` belong in this repository.

## Notes

k3s ships Traefik by default. LucentRoot clusters should be installed with
`--disable=traefik`, or the two controllers will both attempt to claim
`LoadBalancer` ports. See [docs/bootstrap.md](../../../docs/bootstrap.md).
