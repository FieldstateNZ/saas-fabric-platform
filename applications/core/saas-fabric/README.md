# SaaS Fabric

| | |
|---|---|
| Product | SaaS Fabric |
| Upstream project | https://github.com/FieldstateNZ (application repository) |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Container image | `ghcr.io/fieldstatenz/saas-fabric` — version and digest in [the promotion record](../../../environments/lucentroot/promotions/saas-fabric.yaml) |
| Licence | Fieldstate |
| Namespace | `platform-system` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `30` |

## What this directory is

The **deployment contract** for SaaS Fabric's **runtime plane**: the half that
serves tenant traffic on the product edge. Its other half — the control plane
that holds what a client is and reconciles that definition — is a separate
Application at [`../saas-fabric-control-plane`](../saas-fabric-control-plane/),
because they are two deployments on two networks: the runtime plane must keep
serving tenants while the control plane is down.

This directory contains no application source code and never will.

## Current state: scaled to zero

The image is published and the overlays pin it — by version *and* digest, so
the artifact cannot move under the pin — but the Deployment stays at
`replicas: 0`.

The reason is no longer a missing image. The runtime plane reads its tenants,
data sources and catalogue from files, and **none of them exists on any
environment yet** — they are a reconciliation target beside Keycloak, and the
control plane does not publish them yet. A replica would start, fail to find
them, and refuse to serve. Zero states that honestly.

**This Application reporting Healthy does not mean SaaS Fabric is serving.** It
means the cluster matches Git, and Git currently asks for zero replicas.

Going live is one edit in one environment overlay, once that state is published:

```yaml
replicas:
  - name: saas-fabric
    count: 1                 # LucentRoot; production sets its own
```

Promote to production only after LucentRoot has run the tag — see
[docs/releases.md](../../../docs/releases.md).

## What the platform owns

- Deployment, replica count and pod security context;
- Service;
- `HTTPRoute` and the platform hostname (`fabric.<domain>`), attached to the
  shared platform `Gateway`;
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

Non-secret configuration is a **TOML file**, supplied by the
`saas-fabric-config` ConfigMap and mounted at the path in `FABRIC_CONFIG`. The
application refuses to start if it is missing.

The previous `SAAS_FABRIC__*` environment keys were a contract the application
never had: it namespaces its own environment overrides `FABRIC_SETTING_*` and
takes the rest from the file. Manifests that set keys nothing reads look
configured and are not, which is worse than being unconfigured.

| Setting | Value |
|---|---|
| `token.mode` | `trusted_ingress` — the gateway authenticates, the runtime consumes the identity it established |
| `tenants_path`, `data_sources_path`, `catalog_path` | files reconciliation writes and the runtime reads |
| `[[connectors]]` | none yet, and the application refuses to start with an empty list |

That refusal is correct: a runtime plane with no connector can execute nothing,
and starting anyway would answer every request with an error that looks like a
tenant problem. It is the second reason this Deployment is held at zero.

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
| [Envoy Gateway](../envoy-gateway/) | `0` | the routing layer |
| [Platform gateway](../platform-gateway/) | `10` | the `Gateway` its route attaches to |
| [Observability](../observability/) | `10` | OTLP endpoint |
| [OpenBao](../openbao/) | `10` | secrets capability |
| [Keycloak](../keycloak/) | `20` | identity provider |

Wave `30` places SaaS Fabric after all four. The control plane follows at `40`,
after the runtime plane and after everything it administers.

## Autoscaling

Not configured. An HPA needs a load profile, and there is no running image to
measure. When one exists, a `HorizontalPodAutoscaler` belongs in the production
overlay alongside a raised `replicas` floor — that is a platform concern and it
belongs in this directory.

## TLS

TLS terminates at the platform `Gateway`, not here. The production overlay's
route selects the Gateway's `https` listener by `sectionName`; the certificate
is a single `platform-tls` secret on that listener. See
[`../platform-gateway`](../platform-gateway/).
