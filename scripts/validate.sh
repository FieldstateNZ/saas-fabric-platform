#!/usr/bin/env bash
#
# Everything CI runs, runnable locally:
#
#   scripts/validate.sh
#
# Requires helm, kubectl (or kustomize), python3 with PyYAML, kubeconform and
# yamllint. No Kubernetes cluster is needed, and none should be.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER="${ROOT}/.render"

# CustomResourceDefinition is skipped because no JSON schema is published for
# it. Every other kind, including Argo CD and CloudNativePG custom resources,
# is validated against a real schema rather than waved through.
#
# Pinned to a catalog commit, never `main`: a catalog change is a change to
# what this validates against and has to arrive through a reviewed commit
# here. fd90051 is the catalog the last green main run (40ebcf7) used. Its
# successor b7e2015 ("update external-secrets.io CRDs") ships a SecretStore
# and ClusterSecretStore schema with `additionalProperties: false` written as
# a property schema, which kubeconform cannot compile and reports as "could
# not find schema". Move the pin once upstream fixes that.
CRD_CATALOG_REVISION='fd90051867733c60d32d16450556e9cd18459aef'
CRD_CATALOG="https://raw.githubusercontent.com/datreeio/CRDs-catalog/${CRD_CATALOG_REVISION}/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
KUBERNETES_VERSION='1.30.0'

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

yamllint_command() {
  if command -v yamllint >/dev/null 2>&1; then
    yamllint "$@"
  else
    python3 -m yamllint "$@"
  fi
}

cd "${ROOT}"

step 'YAML syntax and formatting'
yamllint_command --strict .

step 'Render every Kustomize build and Helm chart'
python3 scripts/render.py "${RENDER}"

step 'Validate rendered manifests against Kubernetes schemas'
find "${RENDER}" -name '*.yaml' -print0 | xargs -0 kubeconform \
  -strict \
  -summary \
  -kubernetes-version "${KUBERNETES_VERSION}" \
  -skip CustomResourceDefinition \
  -schema-location default \
  -schema-location "${CRD_CATALOG}"

# The checker's own regression tests, before the checker is trusted with the
# render. Standard-library unittest against synthetic manifests and policy
# text: no extra dependency, no network, no cluster.
step 'Checker regression tests'
python3 -m unittest discover -s scripts -p 'test_*.py'

step 'Repository invariants'
python3 scripts/check.py "${RENDER}"

printf '\n\033[1mAll checks passed.\033[0m\n'
