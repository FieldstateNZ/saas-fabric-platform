# Adding an application

## The test

> **Does SaaS Fabric itself require this service in order to operate?**

If **yes**, it may be core. If **no**, it belongs in the catalogue.

Apply it honestly. Two failure modes it exists to prevent:

- *"It's useful, so it's core."* Usefulness is not a dependency. Grafana is
  extremely useful and is catalogue.
- *"It's in this repository, so Fabric depends on it."* Presence is not a
  requirement. This is exactly the reasoning that turns a platform into a
  monolith:

```text
Bad:  Fabric requires Airflow because Airflow exists in this repo.
Good: Fabric can deploy Airflow when that capability is required.
```

If a component is core, you must be able to say what breaks without it.

## Before you add anything

Three questions, in order:

1. **Who owns it?** If OpenTofu could plausibly reconcile the same object, stop.
   Argo CD owns platform applications; OpenTofu owns infrastructure and
   client-scoped resources. Competing ownership is worse than a missing feature.
2. **Is it client-shaped?** A Keycloak realm, a client database, a client
   hostname — these belong to `saas-fabric-clients`, whatever they are made of.
3. **Does it need cluster-scoped resources?** Core applications may, within the
   kinds enumerated in `bootstrap/project.yaml`. Catalogue applications may not,
   at all.
4. **Which plane does it belong on?** See below. Getting this wrong is how an
   administrative console ends up on the public edge.
5. **Which scope are its secrets?** Its own operational credentials are platform
   secrets at `secret/platform/<name>/...`. Anything it needs only because a
   particular client exists is a client secret, and does not come through the
   platform store. See
   [`applications/core/external-secrets`](../applications/core/external-secrets/#the-split-is-about-purpose-not-about-which-namespace-asks).

---

## Which exposure plane

Every service that is reachable at all is reachable on one of two planes, and
the answer needs a reason.

| | Product plane | Operator plane |
|---|---|---|
| Ask | do applications or clients call it? | do only the people running the platform reach it? |
| Resource | `HTTPRoute` on the platform `Gateway` | `Ingress`, `ingressClassName: tailscale` |
| Where you configure it | the service's `httpRoute`/route values | the service's `ingress` values |

Three rules:

1. **Cluster-local is the default.** Most services need neither plane — OpenBao
   and the OpenTelemetry collector are reached by service DNS. A plane is
   something a service earns, not a default.
2. **A service can be on both, and then the split must be exact.** Keycloak's
   OIDC endpoints are product; its admin console is operator-only. A bare `/`
   PathPrefix on the product plane silently undoes that.
3. **Client traffic is product-plane only.** Never route a client through
   Tailscale.

Operator-plane `Ingress` resources go in
[`applications/core/operator-access`](../applications/core/operator-access/),
**not** in the service's own chart values — even when the chart supports it. An
Ingress with no load-balancer address reads as Progressing, so rendering one
from a service's chart couples that service's health to the Tailscale operator
and lets a broken operator plane gate the product plane.

`scripts/check.py` fails the build on any `Ingress` that is not `tailscale`, on
any other `IngressClass`, and on an admin path reachable from the product plane.
Full contract in [architecture.md](architecture.md#exposure-planes).

## Adding a core application

### 1. Create the directory

```text
applications/core/<name>/
├── README.md
├── application.yaml
└── values.yaml          # Helm applications only
```

Lowercase kebab-case, matching the product name: `cloudnative-pg`, not `cnpg`.

### 2. Write `application.yaml`

Copy the closest existing one —
[`observability`](../applications/core/observability/application.yaml) for a
Helm chart, [`saas-fabric`](../applications/core/saas-fabric/application.yaml)
for platform-owned manifests. Then:

- set `metadata.labels.fieldstate.nz/source` to `helm` or `kustomize`. This is
  what the environment binding uses to find the fields it must fill in; an
  Application without it will silently keep its placeholders;
- set `fieldstate.nz/class: core`;
- pin `targetRevision` on the chart to an exact version — never a range, never
  `*`. `scripts/check.py` fails the build otherwise;
- leave the two `PLACEHOLDER` values alone. They are replaced per environment;
- choose a sync wave (see below);
- set `destination.namespace` to a namespace the platform project allows.

### 3. Add the chart repository to the project

An Application whose `repoURL` is not in the project's `sourceRepos` will not
sync. Add it to [`bootstrap/project.yaml`](../bootstrap/project.yaml), and note
that this file is applied by an administrator — an existing cluster needs the
project re-applied before the new application will work.

`scripts/check.py` fails the build if you forget.

### 4. Register it

Add the Application to
[`applications/core/kustomization.yaml`](../applications/core/kustomization.yaml),
in wave order.

### 5. Choose a sync wave

| Wave | For |
|---|---|
| `0` | operators, CRDs, control planes, ingress classes |
| `10` | routing, operator access, data, secrets and telemetry foundations |
| `20` | identity |
| `30` | SaaS Fabric services |
| `40` | catalogue |
| `50` | operator-plane access |

Use an existing wave. New waves are for genuinely new layers, not for nudging
ordering — if two things in one wave race, they probably have a dependency that
should be expressed by moving one to the next wave.

Waves only order anything because of the custom Application health assessment in
[`argocd/runtime`](../argocd/runtime/). That is not Argo CD's default; see
[architecture.md](architecture.md#argo-cd-runtime-contract).

### 6. Add environment configuration, only where it differs

Put shared configuration in `values.yaml`. Put only real environmental
differences in `environments/<environment>/config/<name>.yaml` — replica counts,
storage classes, hostnames, resources. An environment with nothing to say needs
no file at all.

Do not copy the whole values file into each environment and change three lines.

### 7. Write the README

Required, and CI enforces the provenance table. It must record:

- product name, upstream project URL, Helm chart source;
- pinned chart version and application version;
- licence;
- namespace, class, sync wave;
- **why it exists in SaaS Fabric** — the answer to the test above;
- required dependencies;
- configuration owned by this repository;
- configuration expected from external or client layers;
- any resources unsafe to prune.

The last two matter most. Undocumented external dependencies are how a platform
becomes unbootstrappable, and undocumented stateful resources are how data gets
deleted by a reconciler.

### 8. Validate

```bash
./scripts/validate.sh
```

---

## Adding a catalogue application

Same shape, with four differences:

1. It lives in `applications/catalogue/<name>/`.
2. `fieldstate.nz/class: catalogue`, `project: saas-fabric-catalogue`,
   `destination.namespace: catalogue`, sync wave `40`.
3. Its chart repository goes in
   [`argocd/projects/saas-fabric-catalogue.yaml`](../argocd/projects/saas-fabric-catalogue.yaml),
   not the platform project.
4. It must not require cluster-scoped resources. The catalogue project grants
   none. A chart that insists is telling you it is not a catalogue application.

Register it in
[`applications/catalogue/kustomization.yaml`](../applications/catalogue/kustomization.yaml).

### Which environments get it

Per environment. An environment that includes `../../applications/catalogue` in
its kustomization gets every catalogue application; one that omits it gets none
and is still a complete platform. LucentRoot enables the catalogue; production
does not.

The operator plane works the same way, and is the reason two core applications
are listed by environments rather than by `applications/core/kustomization.yaml`:
`applications/core/tailscale` and `applications/core/operator-access` are core,
but an environment without a tailnet cannot run them.

### If it bundles a database

Most analytics charts ship a PostgreSQL subchart. Disable it and use a
CloudNativePG `Cluster` instead, modelled on
[`keycloak-database`](../applications/core/keycloak-database/). Two things
reconciling PostgreSQL in one cluster is the competing-ownership problem this
repository exists to prevent — see
[`applications/catalogue/superset`](../applications/catalogue/superset/) for a
worked example of an application not adopted for exactly this reason.

---

## Adding an environment

The structure supports it without reorganisation:

1. `environments/<name>/config/platform.yaml` — the environment contract:
   `environment`, `domain`, `replicaProfile`, `storageClass`, `revision`.
2. `environments/<name>/config/kustomization.yaml` — listing `platform.yaml`.
3. `environments/<name>/kustomization.yaml` — copy an existing one; decide
   whether to include the catalogue.
4. `environments/<name>/bootstrap/kustomization.yaml` — copy an existing one.
5. Add the name to `ENVIRONMENTS` in
   [`scripts/render.py`](../scripts/render.py) and
   [`scripts/check.py`](../scripts/check.py) so CI validates it.

No application definition changes. That is the point of the shared definition.

---

## Removing an application

Delete its entry from the class kustomization and delete its directory. Argo CD
prunes the resources, because child Applications carry the cascade finalizer.

Before you do: check the README's *Resources unsafe to prune* section. If it has
one, the data outlives the Application by design and must be dealt with
deliberately.
