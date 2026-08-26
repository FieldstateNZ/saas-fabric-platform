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

k3s ships Traefik as its ingress controller. The platform routes through Envoy
Gateway and is deliberately the cluster's only routing authority, so disable
Traefik — two controllers competing for the same `LoadBalancer` ports is not a
state worth debugging.

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

**Argo CD 2.13 or later is required**, for `oci://` Helm source repositories.
Envoy Gateway's chart is published only to an OCI registry. The platform also
depends on a non-default Argo CD health assessment, which this repository owns
and applies in step 5 — see
[`argocd/runtime/README.md`](../argocd/runtime/README.md).

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
kubectl apply --server-side --field-manager=saas-fabric-platform \
  -k environments/lucentroot/bootstrap
```

This applies the `saas-fabric-platform` project, the Argo CD runtime
configuration the platform depends on, the environment ConfigMap and the root
Application. Argo CD takes it from there.

`--server-side` is required, not cosmetic. The bootstrap set includes a partial
`argocd-cm` that adds one key to a ConfigMap Argo CD's installer owns.
Server-side apply makes this repository the field manager for that one key and
leaves the rest alone; a client-side apply would rewrite the object's
`last-applied-configuration` and put the two managers in conflict.

### 6. Hostnames

LucentRoot uses `*.lucentroot.internal`. Point them at the address Envoy Gateway
allocated for the platform `Gateway`:

```bash
kubectl -n platform-system get gateway platform \
  -o jsonpath='{.status.addresses[0].value}'
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

Production additionally expects one TLS certificate, referenced by the platform
Gateway's `https` listener and not created here:

| Secret | Namespace | Referenced by |
|---|---|---|
| `platform-tls` | `platform-system` | the platform `Gateway` |

TLS terminates at the Gateway, so this is one certificate for the whole
platform edge rather than one per service. Automated issuance is a
[known gap](architecture.md#known-gaps).

### 5. Create the production branch

Production follows `refs/heads/production`. That branch does not exist until the
first release is cut, and creating it is a one-time step with its own
prerequisites — a tagged release commit to point at, and branch protection
configured before any cluster follows it.

```bash
git branch production v0.1.0
git push origin production
```

Do not skip the protection step: the branch is the record of what production
runs, and an unprotected one silently weakens every promise made about
promotion. The full procedure is
[Initialising production](releases.md#initialising-production).

### 6. Hand over the cluster

```bash
git checkout production
kubectl apply --server-side --field-manager=saas-fabric-platform \
  -k environments/production/bootstrap
```

This is the only time `kubectl apply` is part of deploying production. From here
promotion is a Git operation: advance `refs/heads/production` and Argo CD
reconciles. See [releases.md](releases.md).

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

`saas-fabric` reports Healthy with zero pods. The Deployment ships with
`replicas: 0` until an image is published, and Healthy here means "the cluster
matches Git", not "SaaS Fabric is running". What a converged cluster proves at
this stage is set out in
[architecture.md](architecture.md#first-milestone); the Deployment itself is
described in
[`applications/core/saas-fabric/README.md`](../applications/core/saas-fabric/README.md).

Check that routing came up, since everything reachable depends on it:

```bash
kubectl -n platform-system get gateway platform
```

The `PROGRAMMED` condition must be `True` and an address must be allocated.

### One remaining manual step

OpenBao starts uninitialised and sealed. Initialise it once:

```bash
kubectl -n secrets exec -it openbao-0 -- bao operator init
```

Store the unseal shares and root token in the organisation's break-glass
location. They must never be committed. See
[`applications/core/openbao/README.md`](../applications/core/openbao/README.md).

### Changing which ref a cluster tracks

The ref an environment follows is part of its Argo binding, in
`environments/<environment>/kustomization.yaml` and
`environments/<environment>/bootstrap/kustomization.yaml`. It is not runtime
configuration and does not appear in the environment ConfigMap.

Neither environment needs it changed to deploy a release. LucentRoot follows
`main`; production follows `production`, and promotion moves that branch rather
than the platform's configuration. See [releases.md](releases.md).
