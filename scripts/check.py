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
  7. the two exposure planes stay separate: product traffic on Gateway API
     routes attached to a listener that exists from a namespace allowed to,
     operator traffic on Tailscale Ingresses, and no third routing authority;
  8. no administrative surface on the product plane;
  9. the Argo CD runtime configuration the platform depends on is present;
 10. the platform secret store is bounded to platform namespaces, and nothing
     reads a client secret path through it;
 11. every in-cluster service reference resolves to something this repository
     actually deploys;
 12. the telemetry pipelines only reference components that exist;
 13. every application directory carries the required documentation.
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
# A PEM header alone is not evidence of a key: upstream CRDs document expected
# credential formats in their OpenAPI descriptions, placeholder and all. Require
# a run of real base64 body before the END marker.
PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:(?!-----END)[\s\S]){0,64}?"
    r"[A-Za-z0-9+/]{40}"
)
CLIENT_SCOPED = re.compile(r"^client-[a-z0-9-]+$")
CLUSTER_DNS = re.compile(r"\b([a-z0-9][a-z0-9-]*)\.([a-z0-9][a-z0-9-]*)\.svc\.cluster\.local\b")
# CloudNativePG creates <cluster>-rw, -ro and -r Services for each Cluster it
# reconciles, so those names are legitimate without appearing in rendered output.
CNPG_SERVICE = re.compile(r"^(?P<cluster>.+)-(rw|ro|r)$")
# The operator plane's only ingress class. Anything else is a third routing
# authority; see docs/architecture.md#exposure-planes.
OPERATOR_INGRESS_CLASS = "tailscale"
# Services whose product-plane route must not reach an administrative surface.
ADMIN_BEARING_BACKENDS = {"keycloak-http"}
# Argo CD behaviour this platform depends on and Argo CD does not default to.
# Both must survive to the cluster or something silently stops working: wave
# ordering in the first case, operator-plane access to Argo CD in the second.
REQUIRED_ARGOCD_RUNTIME = (
    ("argocd-cm", "resource.customizations.health.argoproj.io_Application"),
    ("argocd-cmd-params-cm", "server.insecure"),
)
# The label that marks a namespace as platform-owned. It gates access to the
# platform secret store, so it is a security boundary rather than inventory.
PLATFORM_NAMESPACE_LABEL = "fieldstate.nz/layer"
# The platform secret store, and the OpenBao path prefix reserved for clients.
# A platform ExternalSecret reaching into the client space is a tenancy
# violation even though the OpenBao policy would also refuse it at runtime.
PLATFORM_SECRET_STORE = "openbao"
CLIENT_SECRET_PREFIX = "clients/"
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
            fail(problems, f"{relative}: contains private key material")

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


def check_exposure_planes(render: Path, problems: list[str]) -> None:
    """Two planes, and only two.

    Product traffic goes through Gateway API routes on the platform Gateway.
    Operator traffic goes through Tailscale Ingresses. An Ingress on any other
    class is a third routing authority -- a second place a product hostname can
    be claimed -- which is the situation the split exists to prevent.

    See docs/architecture.md#exposure-planes.
    """
    for environment in ENVIRONMENTS:
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                kind = document.get("kind")
                name = document.get("metadata", {}).get("name")
                if kind == "IngressClass" and name != OPERATOR_INGRESS_CLASS:
                    fail(
                        problems,
                        f"{environment}/{path.name}: IngressClass/{name} is a"
                        " routing authority outside the two planes",
                    )
                if kind != "Ingress":
                    continue
                ingress_class = document.get("spec", {}).get("ingressClassName")
                if ingress_class != OPERATOR_INGRESS_CLASS:
                    fail(
                        problems,
                        f"{environment}/{path.name}: Ingress/{name} uses class"
                        f" '{ingress_class}'. Operator-plane exposure is"
                        f" '{OPERATOR_INGRESS_CLASS}'; product traffic uses an"
                        " HTTPRoute on the platform Gateway",
                    )


def check_admin_off_the_product_plane(render: Path, problems: list[str]) -> None:
    """Keycloak is on both planes, and the split has to actually hold.

    Applications need Keycloak's OIDC endpoints on the product edge. Its admin
    console and admin API do not belong there, and a bare "/" PathPrefix on the
    product plane silently puts them back. Administration is operator-plane.
    """
    for environment in ENVIRONMENTS:
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") != "HTTPRoute":
                    continue
                backends = {
                    backend.get("name")
                    for rule in document["spec"].get("rules", [])
                    for backend in rule.get("backendRefs", [])
                }
                if not backends & ADMIN_BEARING_BACKENDS:
                    continue
                name = document["metadata"]["name"]
                for rule in document["spec"].get("rules", []):
                    for match in rule.get("matches", []):
                        value = match.get("path", {}).get("value", "")
                        if value == "/" or value.startswith("/admin"):
                            fail(
                                problems,
                                f"{environment}/{path.name}: HTTPRoute/{name}"
                                f" matches '{value}' on the product plane,"
                                " which exposes the admin console",
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
    """Argo CD behaviour the platform depends on and Argo CD does not default to.

    Both settings are invisible when missing rather than loud: sync waves stop
    ordering anything, and operator-plane access to Argo CD becomes a redirect
    loop. Each must be present in the bootstrap set, so it is active before the
    first wave-ordered sync, and in the environment, so it cannot drift.
    See argocd/runtime/README.md.
    """
    for environment in ENVIRONMENTS:
        for config_map, key in REQUIRED_ARGOCD_RUNTIME:
            for name, described in (
                ("bootstrap.yaml", "the bootstrap set"),
                ("platform.yaml", "the reconciled environment"),
            ):
                found = any(
                    document.get("kind") == "ConfigMap"
                    and document["metadata"]["name"] == config_map
                    and key in (document.get("data") or {})
                    for document in load_all(render / environment / name, problems)
                )
                if not found:
                    fail(
                        problems,
                        f"{environment}: {described} does not set"
                        f" {key} in {config_map}",
                    )


def check_secret_store_is_bounded(render: Path, problems: list[str]) -> None:
    """A cluster-wide secret store with no conditions is a tenancy hole.

    Without conditions, anything able to create an ExternalSecret in any
    namespace -- including a future client namespace -- can ask External Secrets
    to fetch whatever the store's credentials can read. The platform store is
    restricted to namespaces this repository owns; client secret delivery is a
    separate mechanism. See applications/core/secret-store/README.md.
    """
    for environment in ENVIRONMENTS:
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") != "ClusterSecretStore":
                    continue
                name = document["metadata"]["name"]
                conditions = document["spec"].get("conditions")
                if not conditions:
                    fail(
                        problems,
                        f"{environment}/{path.name}: ClusterSecretStore/{name}"
                        " has no conditions, so any namespace may use it",
                    )
                    continue
                for condition in conditions:
                    labels = (condition.get("namespaceSelector") or {}).get(
                        "matchLabels", {}
                    )
                    named = condition.get("namespaces")
                    if PLATFORM_NAMESPACE_LABEL in labels or named:
                        break
                else:
                    fail(
                        problems,
                        f"{environment}/{path.name}: ClusterSecretStore/{name}"
                        " is restricted, but not to platform namespaces:"
                        f" no condition names them or matches"
                        f" {PLATFORM_NAMESPACE_LABEL}",
                    )


def check_platform_secrets_stay_platform(render: Path, problems: list[str]) -> None:
    """The platform store serves platform secrets, not client ones.

    The namespace bound on the store looks like a location rule, but the split
    is about purpose: one workload can legitimately need both scopes. A
    catalogue application's own admin credential is a platform secret; the
    credentials it uses to reach one client's data are that client's, and come
    through that client's own store.

    OpenBao's policy refuses secret/clients/* to the platform token anyway, so
    this is defence in depth -- but it fails at build time with a clear reason
    rather than at runtime with a permission denial.
    """
    for environment in ENVIRONMENTS:
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") not in ("ExternalSecret", "ClusterExternalSecret"):
                    continue
                spec = document["spec"]
                store = spec.get("secretStoreRef", {}).get("name")
                if store != PLATFORM_SECRET_STORE:
                    continue

                keys = [
                    entry.get("extract", {}).get("key")
                    for entry in spec.get("dataFrom", [])
                ] + [
                    entry.get("remoteRef", {}).get("key")
                    for entry in spec.get("data", [])
                ]
                name = document["metadata"]["name"]
                for key in filter(None, keys):
                    if key.lstrip("/").startswith(CLIENT_SECRET_PREFIX):
                        fail(
                            problems,
                            f"{environment}/{path.name}:"
                            f" {document['kind']}/{name} reads '{key}' through"
                            f" the platform store. Client secrets come from a"
                            " client-scoped store, not this one",
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
    check_exposure_planes(render, problems)
    check_admin_off_the_product_plane(render, problems)
    check_routes_attach(render, problems)
    check_argocd_runtime_configuration(render, problems)
    check_secret_store_is_bounded(render, problems)
    check_platform_secrets_stay_platform(render, problems)
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
