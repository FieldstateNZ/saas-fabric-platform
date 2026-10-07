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
# so no secret reaches the log). Nothing listed means a genuinely first run or
# lost state, and the two converge differently -- see this Application's
# README, "State" -- so the log has to say which one the run below planned
# against. With the `kubernetes` backend an absent state is not an error:
# opening it creates an empty state Secret (the backend's own `StateMgr`,
# briefly taking the lock to do it), so `state list` prints nothing and exits
# 0. That also means the state Secret's existence proves only that some run
# got this far, not that anything was ever applied. A failure here is a real
# backend error -- including a lock held by a killed run when no state
# existed yet -- and is fatal.
addresses=$(tofu -chdir=/work state list)
echo "master-instance: state holds:"
echo "${addresses:-  (nothing)}"

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
#
# The plan is saved and the apply executes exactly it: what was checked is
# what is written, and if another run changed the state in between, OpenTofu
# refuses the saved plan as stale rather than applying it. The file holds the
# gateway client secret in clear; it lives only in this Pod's `work` emptyDir,
# which is deleted with the Pod, beside a provider cache that already sees
# the same value in memory.
set +e
tofu -chdir=/work plan -input=false -no-color -lock-timeout=60s -detailed-exitcode -out=/work/tfplan
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
    echo "  'user with username ... not found': a name in master-instance-config's 'operators'" >&2
    echo "  is not a user in the master realm. This module grants roles to existing accounts" >&2
    echo "  and creates none (README, 'Who the operators are')." >&2
    echo "  'Resource instance cannot be destroyed' on keycloak_user_roles.operator: the plan would" >&2
    echo "  revoke an operator's fabric-operator and admin roles -- a name was removed or renamed" >&2
    echo "  in 'operators', or a different existing account now holds a declared username." >&2
    echo "  Refused; restore it, or follow README, 'Who the operators are', to retire it." >&2
    echo "  (An account deleted and re-created under the same name is not refused: its new" >&2
    echo "  account is granted. README, 'Who the operators are'.)" >&2
    echo "  '401' or 'invalid_grant': the mirrored keycloak-admin credential is not the one" >&2
    echo "  Keycloak holds (docs/master-instance-convergence.md, cause 9)." >&2
    echo "  A state lock in the error: README, 'A Job replaced mid-run'." >&2
    exit 1
    ;;
esac

if ! tofu -chdir=/work apply -input=false -no-color -lock-timeout=60s /work/tfplan; then
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
