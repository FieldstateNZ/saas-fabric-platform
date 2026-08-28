# OpenBao seal key

| | |
|---|---|
| Product | External Secrets `Password` generator |
| Upstream project | https://github.com/external-secrets/external-secrets |
| Helm chart source | none — platform-owned manifests |
| Chart version (pinned) | n/a |
| Application version | generator supplied by the pinned External Secrets chart |
| Licence | Apache-2.0 |
| Namespace | `secrets` |
| Grouping | `core` — a deployment tier, not a classification |
| Service contract | [`platform-service.yaml`](platform-service.yaml) |
| Sync wave | `0` |

## Why it exists in SaaS Fabric

LucentRoot's OpenBao initialises and unseals itself, which requires an
auto-unseal mechanism. This generates the key it seals against.

The key is **deliberately disposable**. LucentRoot is rebuilt rather than
restored, so after a rebuild there is no prior data to decrypt — an external key
vault would be protecting nothing while adding a dependency the environment
cannot satisfy. What the key *must* do is survive a pod restart within one
installation, or every restart would strand the data written before it.

```text
survives a pod restart      required — it is a Kubernetes Secret
survives a cluster rebuild  deliberately not — the data is gone too
```

## Why it is generated, not projected

This is the one secret in the platform that cannot come from OpenBao, because
OpenBao cannot start without it. Generating it locally is what keeps the
dependency graph acyclic:

```text
External Secrets → seal key → OpenBao → ClusterSecretStore → workloads
```

and never:

```text
OpenBao → ExternalSecret → ClusterSecretStore → OpenBao
```

The generator has no OpenBao dependency of its own, which is the property that
makes this work. `scripts/check.py` fails the build if this `ExternalSecret`
ever acquires a `secretStoreRef`.

## Why `refreshInterval: "0"`

Load-bearing, and more so here than elsewhere. Rotating this value would leave
every already-sealed byte undecryptable — on a disposable instance that means a
permanently sealed OpenBao rather than a rotated one. Rotating a seal key is a
`sys/sealwrap/rewrap` operation, not a Kubernetes one.

## Dependencies

| Dependency | Wave | Why |
|---|---|---|
| [External Secrets](../external-secrets/) | `0` | supplies the generator and reconciles the Secret |

Wave `0`, ahead of OpenBao at wave `10`. The key has to exist before OpenBao
starts, and wave gating is what guarantees that rather than luck.

## Environments

LucentRoot only. Production seals against durable external key material with
real recovery semantics — see
[`environments/production/config/openbao.yaml`](../../../environments/production/config/openbao.yaml).
Listing this in production would not merely be unnecessary; it would replace a
recoverable seal with one that is thrown away.

## Configuration owned by this repository

- the key's length and character classes.

## Configuration expected from outside this repository

Nothing.
