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
CRD_CATALOG='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
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

step 'Repository invariants'
python3 scripts/check.py "${RENDER}"

printf '\n\033[1mAll checks passed.\033[0m\n'
