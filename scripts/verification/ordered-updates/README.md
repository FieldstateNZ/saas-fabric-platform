# Ordered-update verification (issue #44, V1–V6)

Throwaway check of the RollingSync candidate in
`scripts/fixtures/rolling-sync/`. Nothing here is rendered or applied by an
environment. `scripts/test_ordering.py` (`FixturesAreInert`) keeps `scripts/`
off the paths Argo CD syncs.

The stand-ins are not Keycloak, OpenTofu, or the control plane. A ConfigMap
stands in for the credential and the control plane. A Job stands in for
master-instance convergence (`succeed` sleeps, then exits 0; `fail` exits 1).
A Secret named `tfstate-default-master-instance` stands in for OpenTofu state
and carries `Prune=false,Delete=false`.

Run it only against a cluster this script creates:

```bash
python3 scripts/verification/ordered-updates/run_v1_v6.py
```

The script writes its own kubeconfig, refuses any other context, and deletes
the kind cluster when the run finishes. Kind is created with kube-proxy in
nftables mode: this VM's kernel rejects kube-proxy's iptables `statistic`
rules, so ClusterIP routing does not come up otherwise. Evidence lands in
`test-evidence/ordered-updates/`.

Argo CD is Helm chart `10.11.6`, app `v3.5.6`, the pin in
`FieldstateNZ/lucentroot-gitops` `versions.env`. That install sets
`applicationSet.replicas: 0`. This run sets replicas to 1 so the candidate can
be observed. It does not point at LucentRoot.
