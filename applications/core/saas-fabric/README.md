# SaaS Fabric

| | |
|---|---|
| Product | SaaS Fabric |
| Upstream project | https://github.com/FieldstateNZ (application repository) |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Container image | `ghcr.io/fieldstatenz/saas-fabric:<version>` |
| Licence | Fieldstate |
| Namespace | `platform-system` |
| Class | core |
| Sync wave | `30` |

## What this directory is

The **deployment contract** for SaaS Fabric: how the platform runs it, and what
it is entitled to expect from the platform around it. It is deliberately a
contract rather than a description of the application, because the application
repository does not publish an image yet.

This directory contains no application source code and never will.

## Current state: scaled to zero

`ghcr.io/fieldstatenz/saas-fabric` has no published tag. The Deployment ships
with `replicas: 0` and a `placeholder` tag so that a clean cluster still
converges and every Argo Application reports Healthy, instead of the platform's
acceptance test failing on an image that cannot be pulled.

Going live is two edits in one environment overlay:

```yaml
images:
  - name: ghcr.io/fieldstatenz/saas-fabric
    newTag: 0.1.0            # a real published tag

replicas:
  - name: saas-fabric
    count: 1                 # LucentRoot; production sets its own
```

Promote to production only after LucentRoot has run the tag — see
[docs/releases.md](../../../docs/releases.md).

## What the platform owns

- Deployment, replica count and pod security context;
- Service;
- Ingress and the platform hostname (`fabric.<domain>`);
- ServiceAccount;
- non-secret runtime configuration, and the *references* to secret
  configuration;
- autoscaling, when a load profile exists — see below.

## What the platform does not own

- application behaviour, features or migrations;
- **client definitions**, which live in `saas-fabric-clients`;
- **client feature configuration**;
- client hostnames, realms, databases or OpenBao namespaces.

## Runtime configuration interface

Non-secret configuration is supplied by the `saas-fabric-config` ConfigMap and
mounted with `envFrom`. The base declares the platform endpoints; each
environment overlay merges in that environment's identity:

| Key | Source |
|---|---|
| `SAAS_FABRIC__ENVIRONMENT` | environment overlay |
| `SAAS_FABRIC__PLATFORM_DOMAIN` | environment overlay |
| `SAAS_FABRIC__KEYCLOAK_URL` | platform service address |
| `SAAS_FABRIC__OPENBAO_ADDR` | platform service address |
| `SAAS_FABRIC__OTLP_ENDPOINT` | platform service address |

## Required external secret

```yaml
secretRef:
  name: saas-fabric-secrets   # namespace: platform-system
```

Marked `optional: true` so that the platform converges before it exists. Its
contents are whatever credentials SaaS Fabric needs that OpenBao cannot yet
issue; it is injected externally and never committed. As OpenBao takes over
platform credential issuance, this reference is expected to be replaced by an
OpenBao-sourced one rather than the values being relocated.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [Ingress](../ingress/) | `0` | publishes `fabric.<domain>` |
| [Observability](../observability/) | `10` | OTLP endpoint |
| [OpenBao](../openbao/) | `10` | secrets capability |
| [Keycloak](../keycloak/) | `20` | identity provider |

Wave `30` places SaaS Fabric after all four.

## Autoscaling

Not configured. An HPA needs a load profile, and there is no running image to
measure. When one exists, a `HorizontalPodAutoscaler` belongs in the production
overlay alongside a raised `replicas` floor — that is a platform concern and it
belongs in this directory.

## TLS

The production overlay's Ingress references `fabric-tls`, injected externally.
Automated issuance is a known gap — see
[docs/architecture.md](../../../docs/architecture.md#known-gaps).
