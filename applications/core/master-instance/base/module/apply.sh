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

# What this run starts from, by resource address only (no attribute values,
# so no secret reaches the log). An empty answer is either a genuinely first
# run or lost state, and the two converge differently -- see this
# Application's README, "State" -- so the log has to say which one the run
# below planned against. `state list` exits non-zero when no state exists yet;
# that is reported, not fatal, because the plan below reads the same backend
# and fails loudly on any real backend error.
if addresses=$(tofu -chdir=/work state list 2>&1); then
  echo "master-instance: state holds:"
  echo "${addresses:-  (nothing)}"
else
  echo "master-instance: no prior state read (${addresses}); planning as a first run."
fi

# -lock-timeout=60s on every command that takes the state lock: this Job can
# be replaced mid-run (Replace=true,Force=true, see ../job.yaml) -- Argo CD
# deletes the Pod that held the kubernetes backend's state lock without
# releasing it first, so the next run would otherwise fail immediately on a
# lock nobody is coming back to release. A fixed wait, rather than an
# immediate failure, gives an in-flight run from moments ago a chance to
# finish and release the lock on its own; if it does not -- the previous run
# was truly killed mid-write, not merely slow -- this still fails, loudly,
# and the state Lease needs a manual `tofu force-unlock` before the next sync
# can proceed. See this Application's README for that recovery step.
#
# The plan is its own step, ahead of the apply, so a failure here can be
# reported as what it is: nothing was written to the master realm. Every
# lookup in main.tf is read at plan time (scripts/check.py holds them to
# that), so an operator username the realm does not hold fails here, not
# partway through the apply. Exit 0 is a re-run against state that already
# matches main.tf: nothing is applied, and this plan is itself the proof.
set +e
tofu -chdir=/work plan -input=false -no-color -lock-timeout=60s -detailed-exitcode
planned=$?
set -e
case "$planned" in
  0)
    echo "master-instance converged for $TF_VAR_public_base_url; nothing to apply."
    exit 0
    ;;
  2) ;;
  *)
    echo "master-instance: plan failed; nothing was applied to the master realm." >&2
    echo "  data.keycloak_user.operator in the error: a username in master-instance-config's" >&2
    echo "  'operators' is not a user in the master realm. This module grants roles to existing" >&2
    echo "  accounts and creates none (README, 'Who the operators are')." >&2
    echo "  'Instance cannot be destroyed' on keycloak_user_roles.operator: a name was removed" >&2
    echo "  from 'operators', which would revoke that account's fabric-operator and admin roles;" >&2
    echo "  refused until retirement is decided (README, 'Who the operators are')." >&2
    echo "  A state lock in the error: README, 'A Job replaced mid-run'." >&2
    exit 1
    ;;
esac

# Re-plans against the same state: if another run changed it in between, this
# plan reflects that rather than replaying a stale one.
if ! tofu -chdir=/work apply -input=false -auto-approve -no-color -lock-timeout=60s; then
  echo "master-instance: apply failed. Any change that completed first is recorded in state," >&2
  echo "  and the next sync re-plans from it, so do not revert anything in Keycloak by hand" >&2
  echo "  (docs/master-instance-convergence.md, 'Recovery after a partial apply')." >&2
  exit 1
fi

# Exit 2 means drift remains after the apply that was just supposed to remove
# it -- fail loudly rather than report a convergence that did not converge.
# This is what "the drift check is the proof" (this Application's README)
# means concretely: not that the apply exited zero, but that a plan
# immediately afterward finds nothing left to do.
tofu -chdir=/work plan -input=false -no-color -lock-timeout=60s -detailed-exitcode

echo "master-instance converged for $TF_VAR_public_base_url; second plan has no changes."
