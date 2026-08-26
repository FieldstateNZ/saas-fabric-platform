# Bootstrap

Bringing a cluster under management. The processes differ up to the point Argo
CD exists; after that they are identical.

```text
1. Kubernetes exists.
2. Argo CD is installed.
3. Argo CD receives access to this repository.
4. The environment's bootstrap set is applied.
5. Argo CD assumes management of the platform.
```

Step 4 is one command. Everything after it happens through Git.

---

## k3s / LucentRoot

### 1. Cluster

k3s ships Traefik as its ingress controller. The platform runs ingress-nginx, so
disable it — two controllers competing for the same `LoadBalancer` ports is not
a state worth debugging.

```bash
curl -sfL https://get.k3s.io | sh -s - --disable=traefik --write-kubeconfig-mode 644
```

`local-path` is k3s' default storage class and is what
`environments/lucentroot/config/platform.yaml` declares.

### 2. Argo CD

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl wait -n argocd --for=condition=available --timeout=300s deployment/argocd-repo-server
```

### 3. Repository access

Public repository, no credentials needed. Skip to step 4.

For a private repository, add credentials as an Argo CD repository secret. That
secret is created by an administrator and is never committed here.

### 4. Required external secrets

Two secrets must exist before the platform converges. Neither is created by this
repository, and neither may ever be committed to it. Create them with values
from the organisation's secret store:

```bash
kubectl create namespace identity
kubectl create secret generic keycloak-admin -n identity \
  --from-literal=username=<admin-username> \
  --from-literal=password=<admin-password>
```

For LucentRoot only, if the catalogue is enabled:

```bash
kubectl create namespace catalogue
kubectl create secret generic grafana-admin -n catalogue \
  --from-literal=username=<admin-username> \
  --from-literal=password=<admin-password>
```

These are temporary. As OpenBao takes over platform credential issuance, the
Applications' secret *references* are repointed at OpenBao rather than the
values being moved somewhere else.

### 5. Hand over the cluster

```bash
kubectl apply -k environments/lucentroot/bootstrap
```

This applies the `saas-fabric-platform` project, the environment ConfigMap and
the root Application. Argo CD takes it from there.

### 6. Hostnames

LucentRoot uses `*.lucentroot.internal`. Point them at the ingress controller's
address:

```bash
kubectl -n platform-system get svc ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

Add `auth.lucentroot.internal`, `fabric.lucentroot.internal` and
`grafana.lucentroot.internal` to local DNS or `/etc/hosts`.

---

## AKS / production

### 1. Cluster

Created by `saas-fabric-hosting`, not here. It provides the cluster, its
networking, managed identities, the container registry and the DNS zone. This
repository assumes they exist.

Production expects the `managed-csi` storage class, as declared in
`environments/production/config/platform.yaml`.

### 2. Argo CD

Same manifests as LucentRoot. Whether Argo CD is installed by
`saas-fabric-hosting` or by an operator, it must exist before step 4 and it is
not managed by this repository.

### 3. Repository access

If this repository is private, grant Argo CD read access before applying the
bootstrap set — a root Application that cannot read its source will sit
`Unknown` rather than fail loudly.

### 4. Required external secrets

The same `keycloak-admin` secret in `identity`. The catalogue is not enabled in
production, so `grafana-admin` is not needed.

Production additionally expects TLS secrets referenced by the platform
Ingresses, which are not created here:

| Secret | Namespace | Referenced by |
|---|---|---|
| `auth-tls` | `identity` | Keycloak ingress |
| `fabric-tls` | `platform-system` | SaaS Fabric ingress |

Automated issuance is a [known gap](architecture.md#known-gaps).

### 5. Hand over the cluster

Production tracks an immutable tag, so bootstrap from that tag rather than from
`main`:

```bash
git checkout v0.1.0
kubectl apply -k environments/production/bootstrap
```

The tag's own `environments/production/config/platform.yaml` names the revision,
so the root Application ends up pointing at the tag it was applied from. See
[releases.md](releases.md).

---

## After bootstrap, both environments

The two environments converge here. Every subsequent change is a Git change:
open a pull request, merge to `main`, and LucentRoot reconciles. Do not
`kubectl apply`, `kubectl edit` or `kubectl scale` platform resources —
`selfHeal: true` will revert it, which is the system working correctly.

### Verifying convergence

```bash
kubectl -n argocd get applications
```

Every Application should reach `Synced` / `Healthy`. Expect it to take a few
minutes: waves are sequential, and Keycloak waits for its database to be ready.

SaaS Fabric reports Healthy with zero pods; the Deployment ships with
`replicas: 0` until an image is published. See
[`applications/core/saas-fabric/README.md`](../applications/core/saas-fabric/README.md).

### One remaining manual step

OpenBao starts uninitialised and sealed. Initialise it once:

```bash
kubectl -n secrets exec -it openbao-0 -- bao operator init
```

Store the unseal shares and root token in the organisation's break-glass
location. They must never be committed. See
[`applications/core/openbao/README.md`](../applications/core/openbao/README.md).

### Changing which revision a cluster tracks

The tracked revision lives in `environments/<environment>/config/platform.yaml`.
LucentRoot tracks `main` and needs no intervention. Promoting production means
re-applying its bootstrap set from the new tag — see [releases.md](releases.md).
