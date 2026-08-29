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

### 4. Required external secret

**One.** Everything else the platform needs is generated in-cluster — see
[architecture.md](architecture.md#what-is-generated-in-cluster).

The Tailscale operator's OAuth client, because Tailscale issues it and nothing
in the cluster can. It needs the `devices` scope and must own
`tag:k8s-operator`.

Store it as GitHub **environment** secrets on the `lucentroot` environment:

| Secret | Holds |
|---|---|
| `TAILSCALE_OAUTH_CLIENT_ID` | the OAuth client's ID |
| `TAILSCALE_OAUTH_CLIENT_SECRET` | its secret |

Both exist already, holding **placeholder values prefixed `replace-me`**, so the
environment is wired before the credentials are. Update them in place.

The workflow refuses to run while either still holds its placeholder, and says
which. An unreplaced placeholder that sailed through would inject a credential
that cannot authenticate, and surface much later as a Tailscale operator that
will not register, with nothing pointing at the cause.

**There is deliberately no kubeconfig secret.** The workflow can only run on the
box — the cluster API is tailnet-only, so no hosted runner could reach it with
any credential — and on the box the kubeconfig already exists at
`/etc/rancher/k3s/k3s.yaml`. A copy in GitHub would put a cluster-admin
credential somewhere with a far larger blast radius for no capability gain, and
it would go stale every time the CA rotates on a rebuild.

The runner user must be able to read that file. If it cannot, fix it on the host
— install k3s with `--write-kubeconfig-mode 644`, or give the runner a group
that can read it. Do not copy the credential into GitHub to work around it; the
workflow fails with that instruction rather than letting it pass.

Then run the **Inject bootstrap secrets** workflow against `lucentroot`. The
environment's protection rules gate it, so applying a credential is an approval
rather than a command.

This is deliberately not an OpenBao-sourced secret: the operator plane is how
you reach OpenBao when OpenBao is not reachable, so sourcing it from OpenBao
would make a broken OpenBao unobservable. See
[architecture.md](architecture.md#the-bootstrap-secret-boundary).

The tailnet ACL policy must also list `tag:k8s-operator` as a `tagOwner` of
`tag:k8s`, or the operator cannot create proxies. That is tailnet configuration
and lives outside Kubernetes entirely.

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

Two planes, two kinds of hostname.

**Product plane.** LucentRoot uses `*.lucentroot.internal`. Point them at the
address Envoy Gateway allocated for the platform `Gateway`:

```bash
kubectl -n platform-system get gateway platform \
  -o jsonpath='{.status.addresses[0].value}'
```

Add `auth.lucentroot.internal` and `fabric.lucentroot.internal` to local DNS or
`/etc/hosts`.

**Operator plane.** Nothing to configure. The Tailscale operator registers one
device per `Ingress` and each becomes `<hostname>.<tailnet>` automatically:

```bash
kubectl get ingress -A -o custom-columns=\
NAME:.metadata.name,NS:.metadata.namespace,CLASS:.spec.ingressClassName,HOST:.spec.rules[0].host
```

On LucentRoot that is `argocd-lucentroot`, `bao-lucentroot`,
`fabric-lucentroot`, `auth-lucentroot` and `perses-lucentroot`. Who can reach
them is decided by the tailnet ACL, not by this repository.

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

### 4. Required external secret

**One**, and it is not the same one as LucentRoot.

`keycloak-admin` is generated in-cluster here as everywhere else, and production
runs no operator plane yet, so it needs no `operator-oauth` — its administrative
surfaces are reachable only by `kubectl port-forward`. The catalogue is not
enabled, and nothing in it holds a credential in any case.

What production does need is a TLS certificate, referenced by the platform
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

### Reaching Argo CD

On an environment with an operator plane, Argo CD is at
`https://argocd-<environment>.<tailnet>` — see
[`applications/core/operator-access`](../applications/core/operator-access/).

**Restart `argocd-server` once after the first bootstrap.** The bootstrap set
adds `server.insecure` to `argocd-cmd-params-cm`, and command-line parameters
are read at startup rather than watched, so the running server has not picked it
up yet. Until it does, the Tailscale Ingress redirects to HTTPS in a loop:

```bash
kubectl -n argocd rollout restart deployment/argocd-server
```

Without an operator plane, port-forward is the only path:

```bash
kubectl port-forward -n argocd svc/argocd-server 8080:80
```

Argo CD is never on the product plane in either case.

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

**Only one, and only because Tailscale mints it outside the cluster.** Run the
*Inject bootstrap secrets* workflow against `lucentroot`, as described in
[step 4](#4-required-external-secret).

**OpenBao needs nothing.** It initialises and unseals itself:

```bash
kubectl -n secrets exec openbao-0 -- bao status
```

`Initialized: true`, `Sealed: false`, with nobody having run a command. The
secrets engine, Kubernetes auth method, platform policy and External Secrets
role are all established at first start, so `secret-store` reaches `Ready`
without intervention. See
[`applications/core/openbao`](../applications/core/openbao/).

Destroying LucentRoot destroys its OpenBao state and its seal key together. A
rebuild creates a new, empty instance automatically — there is nothing to
capture, and nothing from a previous installation is needed.

### Changing which ref a cluster tracks

The ref an environment follows is part of its Argo binding, in
`environments/<environment>/kustomization.yaml` and
`environments/<environment>/bootstrap/kustomization.yaml`. It is not runtime
configuration and does not appear in the environment ConfigMap.

Neither environment needs it changed to deploy a release. LucentRoot follows
`main`; production follows `production`, and promotion moves that branch rather
than the platform's configuration. See [releases.md](releases.md).
