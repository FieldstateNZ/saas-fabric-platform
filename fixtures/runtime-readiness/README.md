# Runtime readiness fixtures

Synthetic scenarios for `scripts/runtime_contract.py`, read by
`scripts/test_runtime_contract.py`. They use invented tenants (`alpha`, `beta`),
issuers on the reserved `identity.example.invalid` host, and no credentials or
registrations.

**They are never applied.** No Kustomization and no Argo CD Application
references `fixtures/` or `contracts/`. A test asserts this, so a future
reference fails CI.

| Scenario | Replicas | Documents | Identity | Connectors |
|---|---|---|---|---|
| `awaiting-inputs` | 0 | unmet | unmet | unmet |
| `ready-at-zero` | 0 | met | met | met |
| `activated` | 1 | met | met | met |

`awaiting-inputs` mirrors LucentRoot as observed on 2026-10-11 (platform #49).
All three scenarios are valid: zero replicas is always an explicit incomplete
state. `ready-at-zero` shows that meeting every gate does not raise replicas
by itself; that is a deliberate operator action (D01-18).

## Files in a scenario

- `published.yaml`: the `fabric-runtime-*` ConfigMaps, as the publisher writes
  them. Each has one payload key, one manifest key, and
  `app.kubernetes.io/managed-by: saas-fabric`. The tenants ConfigMap also holds
  `publication.json`, the record naming the revision and sha256 of every
  document in one generation (saas-fabric #136), and `resources`, the highest
  revision each tenant binding and DataSource was ever published at
  (saas-fabric #138).
- `held.yaml`: the document revisions, resource revisions, publication
  generation (with its rollback floor) and registry generation held before
  this publication, with a sha256 of each held payload. Its `resources` is the
  held record's resource history, which the new record may raise but never
  lower or drop.
  Revisions only roll forward (D01-1, D01-27).
- `issuer-registry.json`: the issuer-to-tenant registry generation that Fabric
  would produce (D01-4). This shape is a proposal: Fabric does not publish a
  registry yet.
- `compositions/<client>.yaml`: the platform composition for one client or
  instance (D01-3). It holds the namespace, the database, the connector
  processes, and the issuer plus registry generation that its edge applies.
- `runtime.yaml`: the runtime Deployment and its config ConfigMap.

Run one scenario by hand:

```bash
python3 scripts/runtime_contract.py fixtures/runtime-readiness/ready-at-zero
```
