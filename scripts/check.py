#!/usr/bin/env python3
"""Repository invariants that a schema validator cannot express.

    scripts/check.py [render-directory]

Checks, in order of how much damage they prevent:

  1. no plaintext secret material, in the sources or in the rendered output;
  2. no duplicate Kubernetes resource across a single environment;
  3. every chart repository an Application uses is allowed by its AppProject;
  4. every chart version is pinned exactly, never a range;
  5. every Application's destination namespace is allowed by its AppProject;
  6. no client-scoped resource has crept into a platform environment;
  7. exactly one routing authority: Gateway API, never an Ingress, and every
     route attached to a listener that exists from a namespace allowed to;
  8. the Argo CD runtime configuration the platform depends on is present;
  9. every in-cluster service reference resolves to something this repository
     actually deploys;
 10. the telemetry pipelines only reference components that exist;
 11. every application directory carries the required documentation.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ENVIRONMENTS = ("lucentroot", "production")

# Keys whose value is a credential rather than a reference to one. `existingSecret`,
# `secretName`, `secretKeyRef` and friends name a secret and are expected.
SECRET_KEYS = re.compile(
    r"^\s*-?\s*(password|passwd|adminPassword|token|apiKey|api_key|secretKey|"
    r"secret_key|clientSecret|client_secret|privateKey|private_key)\s*:\s*(\S.*)$",
    re.IGNORECASE,
)
SECRET_VALUE_IS_A_REFERENCE = re.compile(
    r"^(\"\"|''|\{\{.*\}\}|\$\{.*\}|null|~|\||>|\{\}|\[\])$"
)
PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
CLIENT_SCOPED = re.compile(r"^client-[a-z0-9-]+$")
CLUSTER_DNS = re.compile(r"\b([a-z0-9][a-z0-9-]*)\.([a-z0-9][a-z0-9-]*)\.svc\.cluster\.local\b")
# CloudNativePG creates <cluster>-rw, -ro and -r Services for each Cluster it
# reconciles, so those names are legitimate without appearing in rendered output.
CNPG_SERVICE = re.compile(r"^(?P<cluster>.+)-(rw|ro|r)$")
# An exact chart version. Ranges, wildcards and "latest" make a release
# non-reproducible: the same tag would deploy different software over time.
PINNED_VERSION = re.compile(r"^v?\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")

REQUIRED_DOC_FIELDS = (
    "Upstream project",
    "Helm chart source",
    "Chart version (pinned)",
    "Licence",
    "Namespace",
)


def fail(problems: list[str], message: str) -> None:
    problems.append(message)


def check_no_plaintext_secrets(root: Path, problems: list[str]) -> None:
    """Nothing in Git, and nothing rendered from it, may carry a credential."""
    for path in sorted(root.rglob("*.yaml")):
        if ".git" in path.parts:
            continue
        text = path.read_text(errors="replace")
        relative = path.relative_to(root)

        if PEM_BLOCK.search(text):
            fail(problems, f"{relative}: contains a private key block")

        for number, line in enumerate(text.splitlines(), start=1):
            match = SECRET_KEYS.match(line)
            if not match:
                continue
            value = match.group(2).split("#")[0].strip()
            if SECRET_VALUE_IS_A_REFERENCE.match(value):
                continue
            fail(problems, f"{relative}:{number}: literal value for '{match.group(1)}'")

        for document in load_all(path, problems):
            if document and document.get("kind") == "Secret":
                if document.get("data") or document.get("stringData"):
                    fail(problems, f"{relative}: Secret with inline data")


# Rendered output is large -- the Gateway API CRDs alone are megabytes -- and
# several checks walk all of it. Parse each file once.
_DOCUMENTS: dict[Path, list[dict]] = {}


def load_all(path: Path, problems: list[str]) -> list[dict]:
    if path not in _DOCUMENTS:
        try:
            _DOCUMENTS[path] = [
                d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)
            ]
        except yaml.YAMLError as error:
            fail(problems, f"{path}: invalid YAML: {error}")
            _DOCUMENTS[path] = []
    return _DOCUMENTS[path]


def check_no_duplicate_resources(render: Path, problems: list[str]) -> None:
    """Two Applications writing the same object is competing ownership.

    Scoped to what Argo CD reconciles. bootstrap.yaml deliberately re-states the
    environment ConfigMap so it exists before the first sync; that overlap is the
    design, not a collision.
    """
    for environment in ENVIRONMENTS:
        seen: dict[tuple, list[str]] = defaultdict(list)
        for path in sorted((render / environment).rglob("*.yaml")):
            if path.name == "bootstrap.yaml":
                continue
            for document in load_all(path, problems):
                identity = (
                    document.get("apiVersion"),
                    document.get("kind"),
                    document.get("metadata", {}).get("namespace"),
                    document.get("metadata", {}).get("name"),
                )
                if all(part is not None for part in identity[:2]):
                    seen[identity].append(path.name)
        for identity, sources in seen.items():
            if len(sources) > 1:
                where = ", ".join(sorted(set(sources)))
                fail(problems, f"{environment}: {identity[1]}/{identity[3]} defined in {where}")


def check_applications_match_their_project(render: Path, problems: list[str]) -> None:
    """An Application may only use repositories and namespaces its project allows,
    and every chart it pulls must be pinned to an exact version."""
    for environment in ENVIRONMENTS:
        projects = {}
        applications = []
        for name in ("bootstrap.yaml", "platform.yaml"):
            for document in load_all(render / environment / name, problems):
                if document.get("kind") == "AppProject":
                    projects[document["metadata"]["name"]] = document["spec"]
                elif document.get("kind") == "Application":
                    applications.append(document)

        for application in applications:
            name = application["metadata"]["name"]
            spec = application["spec"]
            project = projects.get(spec["project"])
            if project is None:
                fail(problems, f"{environment}: {name} uses undefined project {spec['project']}")
                continue

            allowed = project.get("sourceRepos", [])
            for source in spec.get("sources", [spec.get("source", {})]):
                repo = source.get("repoURL")
                if repo and repo not in allowed and "*" not in allowed:
                    fail(problems, f"{environment}: {name} uses {repo}, not in {spec['project']}")

                chart = source.get("chart")
                version = source.get("targetRevision", "")
                if chart and not PINNED_VERSION.match(str(version)):
                    fail(
                        problems,
                        f"{environment}: {name} uses chart {chart} at '{version}',"
                        " which is not an exact version",
                    )

            namespace = spec["destination"]["namespace"]
            destinations = project.get("destinations", [])
            if not any(
                d.get("namespace") in (namespace, "*") for d in destinations
            ):
                fail(
                    problems,
                    f"{environment}: {name} targets namespace {namespace},"
                    f" not allowed by {spec['project']}",
                )


def check_no_client_resources(render: Path, problems: list[str]) -> None:
    """Client namespaces belong to saas-fabric-clients, never to this repository."""
    for environment in ENVIRONMENTS:
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                namespace = document.get("metadata", {}).get("namespace") or ""
                if CLIENT_SCOPED.match(namespace):
                    fail(problems, f"{environment}/{path.name}: client namespace {namespace}")
                if document.get("kind") == "Application":
                    target = document["spec"]["destination"]["namespace"]
                    if CLIENT_SCOPED.match(target):
                        fail(problems, f"{environment}: Application targets {target}")


def check_service_references(render: Path, problems: list[str]) -> None:
    """One application addressing another by a name that does not exist.

    Cross-application service references are configuration, so nothing else
    catches them: the manifests render, validate and deploy, and the failure
    only appears at runtime as a name that will not resolve.
    """
    for environment in ENVIRONMENTS:
        services: set[tuple[str, str]] = set()
        clusters: set[tuple[str, str]] = set()
        documents = []
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                documents.append((path, document))
                name = document.get("metadata", {}).get("name")
                namespace = document.get("metadata", {}).get("namespace")
                if document.get("kind") == "Service":
                    services.add((name, namespace))
                elif document.get("kind") == "Cluster":
                    clusters.add((name, namespace))

        for path in sorted({path for path, _ in documents}):
            for name, namespace in set(CLUSTER_DNS.findall(path.read_text())):
                if (name, namespace) in services:
                    continue
                cnpg = CNPG_SERVICE.match(name)
                if cnpg and (cnpg.group("cluster"), namespace) in clusters:
                    continue
                fail(
                    problems,
                    f"{environment}/{path.name}: references {name}.{namespace},"
                    " which no rendered Service or CloudNativePG Cluster provides",
                )


def check_single_routing_authority(render: Path, problems: list[str]) -> None:
    """Envoy Gateway is the platform's only routing layer.

    An Ingress rendered anywhere means a second controller is being introduced,
    and with it a second place a hostname can be claimed. See
    applications/core/envoy-gateway/README.md.
    """
    for environment in ENVIRONMENTS:
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") in ("Ingress", "IngressClass"):
                    fail(
                        problems,
                        f"{environment}/{path.name}: renders"
                        f" {document['kind']}/{document['metadata']['name']};"
                        " routing goes through the platform Gateway",
                    )


def check_routes_attach(render: Path, problems: list[str]) -> None:
    """A route that names a Gateway or listener that does not exist.

    Gateway API fails softly: the HTTPRoute is accepted by the API server,
    reports NotAllowedByListeners or NoMatchingParent in its status, and serves
    nothing. Nothing before this catches it.

    The same applies to the namespace label. The platform Gateway admits routes
    from namespaces carrying fieldstate.nz/gateway-access, which Applications
    set through managedNamespaceMetadata; a route in a namespace no Application
    labels will never attach.
    """
    label = "fieldstate.nz/gateway-access"
    for environment in ENVIRONMENTS:
        listeners: dict[tuple[str, str], set[str]] = {}
        routes = []
        labelled: set[str] = set()

        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                kind = document.get("kind")
                metadata = document.get("metadata", {})
                if kind == "Gateway":
                    listeners[(metadata["name"], metadata["namespace"])] = {
                        listener["name"] for listener in document["spec"]["listeners"]
                    }
                elif kind in ("HTTPRoute", "GRPCRoute", "TCPRoute", "TLSRoute"):
                    routes.append((path, document))
                elif kind == "Application":
                    managed = (
                        document["spec"]
                        .get("syncPolicy", {})
                        .get("managedNamespaceMetadata", {})
                        .get("labels", {})
                    )
                    if managed.get(label) == "true":
                        labelled.add(document["spec"]["destination"]["namespace"])

        for path, route in routes:
            metadata = route["metadata"]
            namespace = metadata["namespace"]
            where = f"{environment}/{path.name}: {route['kind']}/{metadata['name']}"

            if namespace not in labelled:
                fail(
                    problems,
                    f"{where} is in namespace {namespace}, which no Application"
                    f" labels {label}; the route cannot attach",
                )

            for parent in route["spec"].get("parentRefs", []):
                key = (parent["name"], parent.get("namespace", namespace))
                if key not in listeners:
                    fail(problems, f"{where} attaches to Gateway {key[0]}.{key[1]}, which does not exist")
                    continue
                section = parent.get("sectionName")
                if section and section not in listeners[key]:
                    fail(
                        problems,
                        f"{where} attaches to listener '{section}' of"
                        f" {key[0]}.{key[1]}, which has no such listener",
                    )


def check_argocd_runtime_configuration(render: Path, problems: list[str]) -> None:
    """Sync waves only order an app-of-apps if Argo CD is told to.

    Argo CD reports a child Application as Healthy the moment it exists unless a
    custom health assessment says otherwise. Without it the platform's wave
    ordering is decorative. See argocd/runtime/README.md.
    """
    key = "resource.customizations.health.argoproj.io_Application"
    for environment in ENVIRONMENTS:
        for name, described in (
            ("bootstrap.yaml", "the bootstrap set"),
            ("platform.yaml", "the reconciled environment"),
        ):
            found = any(
                document.get("kind") == "ConfigMap"
                and document["metadata"]["name"] == "argocd-cm"
                and key in (document.get("data") or {})
                for document in load_all(render / environment / name, problems)
            )
            if not found:
                fail(
                    problems,
                    f"{environment}: {described} does not configure {key};"
                    " sync waves would not order the platform",
                )


def check_collector_pipelines(render: Path, problems: list[str]) -> None:
    """The collector config is opaque YAML inside a ConfigMap.

    A pipeline naming a receiver, processor or exporter that is not defined
    renders fine, validates fine, and then crash-loops. Since this is the
    platform's telemetry boundary, check it here instead.
    """
    for environment in ENVIRONMENTS:
        path = render / environment / "applications" / "observability.yaml"
        if not path.is_file():
            continue
        for document in load_all(path, problems):
            if document.get("kind") != "ConfigMap":
                continue
            for key, raw in (document.get("data") or {}).items():
                try:
                    config = yaml.safe_load(raw)
                except yaml.YAMLError as error:
                    fail(problems, f"{environment}: collector {key} is invalid YAML: {error}")
                    continue
                if not isinstance(config, dict) or "service" not in config:
                    continue
                for name, pipeline in config["service"].get("pipelines", {}).items():
                    for stage in ("receivers", "processors", "exporters"):
                        defined = set(config.get(stage) or {})
                        for component in pipeline.get(stage, []):
                            if component not in defined:
                                fail(
                                    problems,
                                    f"{environment}: collector pipeline '{name}' uses"
                                    f" {stage[:-1]} '{component}', which is not defined",
                                )


def check_application_documentation(root: Path, problems: list[str]) -> None:
    """Section 17: no hidden platform dependencies.

    Every application directory must be documented. A directory that actually
    deploys something must additionally record the full provenance table, so a
    dependency cannot enter the platform without its version and licence.
    """
    for klass in ("core", "catalogue"):
        for application in sorted(d for d in (root / "applications" / klass).iterdir() if d.is_dir()):
            readme = application / "README.md"
            if not readme.is_file():
                fail(problems, f"{application.relative_to(root)}: no README.md")
                continue
            if not (application / "application.yaml").is_file():
                continue
            text = readme.read_text()
            for field in REQUIRED_DOC_FIELDS:
                if field not in text:
                    fail(problems, f"{readme.relative_to(root)}: missing '{field}'")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    render = Path(sys.argv[1]) if len(sys.argv) > 1 else root / ".render"

    if not render.is_dir():
        raise SystemExit(f"{render} does not exist -- run scripts/render.py first")

    problems: list[str] = []
    for directory in ("applications", "argocd", "bootstrap", "environments"):
        check_no_plaintext_secrets(root / directory, problems)
    check_no_plaintext_secrets(render, problems)
    check_no_duplicate_resources(render, problems)
    check_applications_match_their_project(render, problems)
    check_no_client_resources(render, problems)
    check_service_references(render, problems)
    check_single_routing_authority(render, problems)
    check_routes_attach(render, problems)
    check_argocd_runtime_configuration(render, problems)
    check_collector_pipelines(render, problems)
    check_application_documentation(root, problems)

    if problems:
        print(f"{len(problems)} problem(s):\n")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print("All repository invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
