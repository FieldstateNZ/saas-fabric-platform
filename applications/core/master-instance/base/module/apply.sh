#!/bin/sh
# Converge the master realm's instance resources, then prove convergence
# rather than merely claim it -- the same shape
# hosting/src/SaaSFabric.Aspire.Hosting/Templates/apply.sh already uses in the
# application repository, without the brand and Vault steps a client realm
# needs and this module does not.
set -eu
export TF_IN_AUTOMATION=1 TF_INPUT=0

# KEYCLOAK_USER and KEYCLOAK_PASSWORD arrive as ordinary container env vars,
# from a Secret ../../master-instance-credential's sibling ExternalSecret
# mirrors into this namespace (see ../job.yaml) -- nothing to read from a
# file here.

# /module is a ConfigMap mount and is read-only regardless of how it is
# declared; `tofu init` writes a provider cache and a lock file into the
# directory it runs in, so the module is copied to a writable one first.
# `*.tf` does not match `.terraform.lock.hcl` -- a leading dot is invisible to
# a plain glob in `sh`, so the lock file is copied explicitly, not swept up
# by the wildcard above it.
mkdir -p /work
cp /module/*.tf /work/
cp /module/.terraform.lock.hcl /work/

tofu -chdir=/work init -input=false -no-color
tofu -chdir=/work validate -no-color

# -lock-timeout=60s: this Job can be replaced mid-apply (Replace=true, see
# ../job.yaml) -- Argo CD deletes the Pod that held the kubernetes backend's
# state lock without releasing it first, so the next run's first apply would
# otherwise fail immediately on a lock nobody is coming back to release. A
# fixed wait, rather than an immediate failure, gives an in-flight apply from
# moments ago a chance to finish and release the lock on its own; if it does
# not -- the previous run was truly killed mid-write, not merely slow -- this
# still fails, loudly, and the state Lease needs a manual
# `tofu force-unlock` before the next sync can proceed. See this
# Application's README for that recovery step.
tofu -chdir=/work apply -input=false -auto-approve -no-color -lock-timeout=60s

# Exit 2 means drift remains after the apply that was just supposed to remove
# it -- fail loudly rather than report a convergence that did not converge.
# This is what "the drift check is the proof" (this Application's README)
# means concretely: not that the apply exited zero, but that a plan
# immediately afterward finds nothing left to do.
tofu -chdir=/work plan -input=false -no-color -lock-timeout=60s -detailed-exitcode

echo "master-instance converged for $TF_VAR_public_base_url; second plan has no changes."
