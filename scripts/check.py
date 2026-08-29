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
 11. LucentRoot's OpenBao initialises and unseals itself, against a seal that
     does not depend on OpenBao;
 11. every in-cluster service reference resolves to something this repository
     actually deploys;
 12. the telemetry pipelines only reference components that exist;
 13. every application directory carries the required documentation;
 14. a service whose only protection is the operator plane stays on it.
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
#
# Named for what they hold -- key *names* and a path prefix, all literals -- and
# not "SECRET_*". Nothing here is credential material, and identifiers that say
# otherwise get flagged as clear-text logging the moment one reaches a message.
CREDENTIAL_KEY_NAMES = (
    "password", "passwd", "adminPassword", "token", "apiKey", "api_key",
    "secretKey", "secret_key", "clientSecret", "client_secret",
    "privateKey", "private_key",
)
CREDENTIAL_KEY_PATTERN = re.compile(
    r"^\s*-?\s*(" + "|".join(CREDENTIAL_KEY_NAMES) + r")\s*:\s*(\S.*)$",
    re.IGNORECASE,
)
SECRET_VALUE_IS_A_REFERENCE = re.compile(
    r"^($|\"\"|''|\{\{.*\}\}|\$\{.*\}|null|~|\||>|\{\}|\[\])$"
)


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes.

    A templated reference is still a reference when it is quoted, and YAML
    routinely requires the quotes -- `password: "{{ .password }}"` starts with
    a brace, which YAML would otherwise read as a flow mapping. Stripping does
    not weaken the check: `"hunter2"` unquotes to `hunter2`, which is still not
    a reference.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
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
# The downstream range each environment trusts to have already set
# `X-Forwarded-Proto`, for the one listener an `EnvoyPatchPolicy` may touch.
#
# Per environment because it is topology, not policy. LucentRoot is a k3s box
# whose operator listener sits behind a Tailscale proxy, so the pod network is
# what can be the downstream; production runs a different ingress on a
# different network and is deliberately absent, which makes any patch policy
# there a failure until somebody states its range on purpose.
TRUSTED_DOWNSTREAM = {
    "lucentroot": "10.42.0.0",
}
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
# Cluster-scoped kinds this platform actually deploys. An AppProject enumerates
# what it permits, so a kind missing from that list is refused at sync -- which
# is the enumeration working, but only tells you once a cluster exists. This
# list lets the same mistake fail during validation instead.
#
# Curated rather than discovered: rendering cannot tell scope apart, because
# Helm and Kustomize routinely omit metadata.namespace on namespaced resources
# too.
CLUSTER_SCOPED_KINDS = {
    ("", "Namespace"),
    ("apiextensions.k8s.io", "CustomResourceDefinition"),
    ("rbac.authorization.k8s.io", "ClusterRole"),
    ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
    ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicy"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicyBinding"),
    ("networking.k8s.io", "IngressClass"),
    ("gateway.networking.k8s.io", "GatewayClass"),
    ("external-secrets.io", "ClusterSecretStore"),
    ("external-secrets.io", "ClusterExternalSecret"),
    ("scheduling.k8s.io", "PriorityClass"),
    ("storage.k8s.io", "StorageClass"),
    ("apiregistration.k8s.io", "APIService"),
}
# The environment whose OpenBao is disposable, self-initialising and
# auto-unsealed. Everywhere else keeps durable state and deliberate recovery.
DISPOSABLE_OPENBAO_ENVIRONMENT = "lucentroot"
# Seal types that depend on infrastructure a development cluster does not have.
PRODUCTION_SEAL_TYPES = (
    "awskms", "azurekeyvault", "gcpckms", "ocikms",
    "alicloudkms", "pkcs11", "kmip", "transit",
)

PLATFORM_SECRET_STORE = "openbao"
CLIENT_PATH_PREFIX = "clients/"
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

# The Gateway's two listeners, and the namespace label each admits routes from.
# A route naming neither listener is eligible for whichever grant its namespace
# carries, which is why both checks below have to consult the labels rather than
# read the route alone.
PRODUCT_LISTENER, PRODUCT_GRANT = "http", "fieldstate.nz/gateway-access"
OPERATOR_LISTENER, OPERATOR_GRANT = "operator", "fieldstate.nz/operator-gateway-access"
GATEWAY_GRANTS = {PRODUCT_LISTENER: PRODUCT_GRANT, OPERATOR_LISTENER: OPERATOR_GRANT}

# The platform service contract. See docs/platform-services.md.
SERVICE_CONTRACT = "platform-service.yaml"
# Which plane a service may be reached on. Declared rather than inferred, and
# only where it is a constraint: `operator` is a statement that publishing the
# service anywhere else would change its security posture, not merely its
# routing. See check_operator_only_services.
EXPOSURE_PLANES = ("operator", "product", "both")
DEPLOYMENT_STATES = ("adopted", "planned", "assessed")
PARTITION_MODES = ("unknown", "none", "logical", "strong")
PROVISIONING_STATES = ("supported", "unsupported")
TENANCY_STATES = ("accepted", "candidate", "unresolved", "rejected", "not-applicable")

# A boundary may be claimed only once it has been established. Anything short of
# `accepted` means the assessment in docs/platform-services.md#assessing-tenancy
# has not been completed, and intent must not be recorded as though it were a
# boundary.
TENANCY_PERMITTING_CLIENTS = ("accepted",)

# `mode` states the strength of a boundary, so it is a claim in its own right and
# tenancy has to license it. Without this pairing a contract could say `strong`
# while its own status said the mechanism was undecided -- asserting the answer to
# the question it was simultaneously recording as open.
# Whether SaaS Fabric administers a service, and whether that service's own
# administrative UI is published. These are separate questions: some upstream UIs
# *are* the capability operators want (Perses' exploration), others are vendor
# administration surfaces that SaaS Fabric replaces (Keycloak's console).
CONTROL_PLANE_MANAGEMENT = (True, False, "partial")
ADMIN_SURFACES = (
    "none",         # upstream ships no console at all
    "not-exposed",  # it ships one; SaaS Fabric replaces it and it is published nowhere
    "break-glass",  # published for diagnostics, outside the normal contract
    "exposed",      # the UI is itself the capability
)

# Both `controlPlane.adminBackends` and `exposure.backends` name Services that
# validation has to find in rendered output, and a Service is identified by
# (namespace, name). One message, because it is one rule.
UNQUALIFIED_BACKEND = (
    "every {field} entry needs a name and a namespace -- a Service is identified by "
    "(namespace, name), so a bare name would make this invariant depend on Service "
    "names being globally unique, which is a convention rather than a property of a "
    "cluster"
)

MODES_PERMITTED_BY_TENANCY = {
    "accepted": ("logical", "strong"),      # established: name the strength
    "candidate": ("unknown",),              # a mechanism is in view, unproven
    "unresolved": ("unknown",),             # partitioning intended, mechanism absent
    "rejected": ("none",),                  # assessed, and it is not a boundary
    "not-applicable": ("none",),            # partitioning is not part of its role
}


def fail(problems: list[str], message: str) -> None:
    problems.append(message)


def _reported_key_name(matched: str) -> str:
    """The key name to report, resolved back to a literal in this file.

    This function reads lines that contain credentials, so it must never put
    scanned content into a message. Resolving the match against the known list
    means the reported name provably originates here rather than in the file
    being scanned -- and it stays that way if the pattern is ever edited.

    The matched *value*, group 2, is never touched.
    """
    lowered = matched.lower()
    for known in CREDENTIAL_KEY_NAMES:
        if known.lower() == lowered:
            return known
    return "credential"


def _external_secret_destination_keys(path: Path, problems: list[str]) -> set[str]:
    """The names an `ExternalSecret` gives the Secret keys it creates.

    `spec.data[].secretKey` is a *destination key name*, never a credential:
    the value stays in OpenBao and is fetched by the `remoteRef` beside it. It
    collides with field names that really do carry a credential -- an AWS secret
    key, for one -- so the exemption is drawn from the parsed document rather
    than from the field name. Only strings this file actually declares as ESO
    destination keys are exempt, and only on a `secretKey` line.

    Without this, the check penalises the narrowest form ESO offers. `data[]`
    names one exact key, where `dataFrom.find.path` takes everything under a
    prefix -- so the shape most worth encouraging was the one that failed.
    """
    names: set[str] = set()
    for document in load_all(path, problems):
        if not document or document.get("kind") != "ExternalSecret":
            continue
        for entry in document.get("spec", {}).get("data") or []:
            if isinstance(entry, dict) and isinstance(entry.get("secretKey"), str):
                names.add(entry["secretKey"])
    return names


def check_no_plaintext_secrets(root: Path, problems: list[str]) -> None:
    """Nothing in Git, and nothing rendered from it, may carry a credential.

    Reports where a credential is and what it is called, never what it is.
    """
    for path in sorted(root.rglob("*.yaml")):
        if ".git" in path.parts:
            continue
        text = path.read_text(errors="replace")
        relative = path.relative_to(root)

        if PEM_BLOCK.search(text):
            fail(problems, f"{relative}: contains private key material")

        destination_keys = _external_secret_destination_keys(path, problems)

        for number, line in enumerate(text.splitlines(), start=1):
            match = CREDENTIAL_KEY_PATTERN.match(line)
            if not match:
                continue
            value = _unquote(match.group(2).split("#")[0].strip())
            if SECRET_VALUE_IS_A_REFERENCE.match(value):
                continue
            key_name = _reported_key_name(match.group(1))
            if key_name == "secretKey" and value in destination_keys:
                continue
            fail(problems, f"{relative}:{number}: literal value for '{key_name}'")

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

    The same applies to the namespace label, and there are now two of them.
    Each listener admits routes from namespaces carrying its own label, which
    Applications set through managedNamespaceMetadata; a route in a namespace
    carrying the wrong one will never attach.

    The two grants are deliberately independent. A namespace reachable from the
    product edge is not thereby reachable from the operator plane, and the
    reverse -- which is the only reason `check_control_plane_is_operator_only`
    below can assert anything.
    """
    grants = GATEWAY_GRANTS
    for environment in ENVIRONMENTS:
        listeners: dict[tuple[str, str], set[str]] = {}
        routes = []
        labelled: dict[str, set[str]] = {}

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
                    for granted in grants.values():
                        if managed.get(granted) == "true":
                            labelled.setdefault(granted, set()).add(
                                document["spec"]["destination"]["namespace"]
                            )

        for path, route in routes:
            metadata = route["metadata"]
            namespace = metadata["namespace"]
            where = f"{environment}/{path.name}: {route['kind']}/{metadata['name']}"

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
                    continue

                # Which grant this route needs depends on the listener it named.
                # A route naming none needs whichever the namespace has.
                needed = [grants[section]] if section in grants else list(grants.values())
                if not any(namespace in labelled.get(granted, set()) for granted in needed):
                    fail(
                        problems,
                        f"{where} is in namespace {namespace}, which no Application"
                        f" labels {' or '.join(needed)}; the route cannot attach",
                    )


def check_control_plane_is_operator_only(render: Path, problems: list[str]) -> None:
    """The control plane's namespace carries one gateway grant, not both.

    This is the invariant behind "operator plane only", and it needs asserting
    because the obvious version of it was wrong twice.

    First the control plane lived in `platform-system` with the label merely
    omitted from its own Application -- which reads like a guarantee and is
    not, because the namespace carries `gateway-access` from its other
    tenants. Then both grants were put on that one namespace, which restored
    the same problem in a new shape: a route there was eligible for either
    listener, and only `sectionName` kept it off the product edge. That is a
    choice a route makes about itself, and a boundary cannot be one of those.

    A namespace holding one grant and not the other is enforced by the
    Gateway's own selector. This check is what stops the other grant being
    added back, in any Application, for any reason.
    """
    product = "fieldstate.nz/gateway-access"
    operator = "fieldstate.nz/operator-gateway-access"

    for environment in ENVIRONMENTS:
        operator_namespaces: set[str] = set()
        product_namespaces: dict[str, str] = {}

        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") != "Application":
                    continue

                labels = (
                    document["spec"]
                    .get("syncPolicy", {})
                    .get("managedNamespaceMetadata", {})
                    .get("labels", {})
                )
                namespace = document["spec"]["destination"]["namespace"]
                name = document["metadata"]["name"]

                if labels.get(operator) == "true":
                    operator_namespaces.add(namespace)
                if labels.get(product) == "true":
                    product_namespaces[namespace] = name

        # Keycloak is the deliberate exception and is named rather than
        # inferred: applications reach it on the product edge and operators on
        # the operator plane, so `identity` genuinely holds both. Every other
        # namespace holding both is the mistake this check exists for.
        for namespace in sorted(operator_namespaces & set(product_namespaces)):
            if namespace == "identity":
                continue

            fail(
                problems,
                f"{environment}: namespace {namespace} carries both"
                f" {product} and {operator} (the second from"
                f" {product_namespaces[namespace]}); a route in it is eligible"
                " for either listener, so operator-only is a convention rather"
                " than a boundary",
            )


def check_the_patch_hatch_stays_narrow(render: Path, problems: list[str]) -> None:
    """`EnvoyPatchPolicy` rewrites generated xDS, so it is checked like a grant.

    Enabling it was a real widening: anything that can write a manifest can now
    patch any generated resource, in a CRD rather than in the Gateway everybody
    reads. It was turned on for one setting the Gateway API does not model --
    trusting the `X-Forwarded-Proto` the Tailscale proxy sets -- and this keeps
    it to that.

    Three assertions, and the second is the one that matters. The operator and
    product listeners are separate xDS resources; a patch that named the
    product one, or named the Gateway without a listener, would apply to an
    edge whose downstream is a LAN client on an RFC1918 address. Trusting
    private addresses there would let anything on the LAN assert
    `X-Forwarded-Proto: https` and satisfy Keycloak's HTTPS requirement over
    plain HTTP -- the check the patch exists to preserve, removed by the patch
    meant to preserve it.

    The trusted range is **per environment**, because it is topology rather
    than policy: a pod CIDR belongs to a cluster, and the premise that a proxy
    hop exists at all belongs to an ingress design. An environment with no
    entry here may have no patch policy, which is how production opts in
    deliberately rather than inheriting a single-node k3s box's network.
    """
    allowed_listener = "platform-system/platform/operator"

    for environment in ENVIRONMENTS:
        allowed_prefix = TRUSTED_DOWNSTREAM.get(environment)
        policies = []
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") == "EnvoyPatchPolicy":
                    policies.append((path, document))

        if policies and allowed_prefix is None:
            fail(
                problems,
                f"{environment}: has an EnvoyPatchPolicy but no trusted"
                " downstream range recorded in TRUSTED_DOWNSTREAM; an"
                " environment states its own topology rather than inheriting"
                " another's",
            )
            continue

        if len(policies) > 1:
            named = ", ".join(sorted(d["metadata"]["name"] for _, d in policies))
            fail(
                problems,
                f"{environment}: {len(policies)} EnvoyPatchPolicy resources"
                f" ({named}); the escape hatch is open for one setting and each"
                " further use needs its own argument",
            )

        for path, document in policies:
            name = document["metadata"]["name"]
            where = f"{environment}/{path.name}: EnvoyPatchPolicy/{name}"

            for patch in document["spec"].get("jsonPatches", []):
                target = patch.get("name")
                if target != allowed_listener:
                    fail(
                        problems,
                        f"{where} patches '{target}', not {allowed_listener};"
                        " a patch reaching the product listener would let a LAN"
                        " client assert its own X-Forwarded-Proto",
                    )

                value = patch.get("operation", {}).get("value") or {}
                for cidr in value.get("cidr_ranges") or []:
                    if cidr.get("address_prefix") != allowed_prefix:
                        fail(
                            problems,
                            f"{where} trusts {cidr.get('address_prefix')}/"
                            f"{cidr.get('prefix_len')}; only the pod network"
                            f" ({allowed_prefix}) may be trusted here, because"
                            " a pod is the only thing that can be the"
                            " downstream on this listener",
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


def _external_secret_spec(document: dict) -> dict:
    """The ExternalSecretSpec, wherever this kind happens to keep it.

    ClusterExternalSecret nests it under spec.externalSecretSpec rather than
    holding data/dataFrom/secretStoreRef directly, so reading spec.* works for
    one kind and silently matches nothing for the other.
    """
    spec = document.get("spec") or {}
    if document.get("kind") == "ClusterExternalSecret":
        return spec.get("externalSecretSpec") or {}
    return spec


def _remote_paths(entry: dict) -> list[str]:
    """Every remote path one data or dataFrom entry can select.

    Three shapes reach a secret, not one: an exact key, and -- for dataFrom --
    a find over a path prefix, which selects everything beneath it.
    """
    paths = [
        (entry.get("remoteRef") or {}).get("key"),
        (entry.get("extract") or {}).get("key"),
        (entry.get("find") or {}).get("path"),
    ]
    return [path for path in paths if path]


def _reaches_client_space(remote: str) -> bool:
    """Whether a remote path selects anything under the client prefix."""
    return remote.lstrip("/").startswith(CLIENT_PATH_PREFIX)


def check_platform_secrets_stay_platform(render: Path, problems: list[str]) -> None:
    """The platform store serves platform secrets, not client ones.

    The namespace bound on the store looks like a location rule, but the split
    is about purpose: one workload can legitimately need both scopes. A
    catalogue application's own admin credential is a platform secret; the
    credentials it uses to reach one client's data are that client's, and come
    through that client's own store.

    OpenBao's policy refuses secret/clients/* to the platform token anyway, so
    this is defence in depth -- it fails at build time with a clear reason
    rather than at runtime with a permission denial, and it still holds if the
    policy is widened later. That only works if it cannot be walked around
    using ordinary ESO syntax, so it normalises the spec, resolves the store
    per entry rather than once, and covers every shape that selects a path.
    """
    for environment in ENVIRONMENTS:
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") not in ("ExternalSecret", "ClusterExternalSecret"):
                    continue

                spec = _external_secret_spec(document)
                default_store = (spec.get("secretStoreRef") or {}).get("name")
                name = document.get("metadata", {}).get("name")

                entries = [
                    (field, index, entry)
                    for field in ("data", "dataFrom")
                    for index, entry in enumerate(spec.get(field) or [])
                ]
                for field, index, entry in entries:
                    # An entry may name its own store, which overrides the
                    # top-level one for that entry only.
                    source = (entry.get("sourceRef") or {}).get("storeRef") or {}
                    store = source.get("name") or default_store
                    if store != PLATFORM_SECRET_STORE:
                        continue

                    if any(_reaches_client_space(remote) for remote in _remote_paths(entry)):
                        # The offending path is identified by where it is, not
                        # by quoting it. Nothing read out of a manifest reaches
                        # this message -- see check_no_plaintext_secrets.
                        fail(
                            problems,
                            f"{environment}/{path.name}:"
                            f" {document['kind']}/{name} {field}[{index}]"
                            f" reads a path under '{CLIENT_PATH_PREFIX}'"
                            " through the platform store. Client secrets come"
                            " from a client-scoped store, not this one",
                        )


def _group_of(api_version: str) -> str:
    """The API group, which is empty for core resources like Namespace."""
    return api_version.split("/")[0] if "/" in api_version else ""


def check_projects_permit_what_apps_deploy(render: Path, problems: list[str]) -> None:
    """An AppProject refusing a kind is a sync failure, not a render failure.

    Cluster-scoped kinds are enumerated per project on purpose, so that a new
    chart cannot quietly acquire cluster-wide privilege. The cost is that
    forgetting to add one is invisible until a cluster says
    "resource X is not permitted in project Y". This says it during validation.
    """
    for environment in ENVIRONMENTS:
        projects: dict[str, set[tuple[str, str]]] = {}
        applications = []
        for name in ("bootstrap.yaml", "platform.yaml"):
            for document in load_all(render / environment / name, problems):
                if document.get("kind") == "AppProject":
                    projects[document["metadata"]["name"]] = {
                        (entry.get("group", ""), entry.get("kind", ""))
                        for entry in document["spec"].get("clusterResourceWhitelist") or []
                    }
                elif document.get("kind") == "Application":
                    applications.append(document)

        for application in applications:
            name = application["metadata"]["name"]
            spec = application["spec"]
            allowed = projects.get(spec["project"])
            if allowed is None:
                continue

            def permits(group: str, kind: str) -> bool:
                return any(
                    (g in ("*", group)) and (k in ("*", kind)) for g, k in allowed
                )

            # CreateNamespace=true makes Argo CD create the destination
            # namespace, which is a cluster-scoped write like any other.
            if "CreateNamespace=true" in (spec.get("syncPolicy", {}).get("syncOptions") or []):
                if not permits("", "Namespace"):
                    fail(
                        problems,
                        f"{environment}: {name} sets CreateNamespace=true but"
                        f" project {spec['project']} does not permit Namespace",
                    )

            rendered = render / environment / "applications" / f"{name}.yaml"
            if not rendered.is_file():
                continue
            for document in load_all(rendered, problems):
                group = _group_of(document.get("apiVersion", ""))
                kind = document.get("kind", "")
                if (group, kind) in CLUSTER_SCOPED_KINDS and not permits(group, kind):
                    fail(
                        problems,
                        f"{environment}: {name} deploys {kind}"
                        f" ({group or 'core'}), which project"
                        f" {spec['project']} does not permit",
                    )


def _openbao_config(render: Path, environment: str, problems: list[str]) -> str:
    """The rendered OpenBao server configuration, or an empty string."""
    path = render / environment / "applications" / "openbao.yaml"
    if not path.is_file():
        return ""
    for document in load_all(path, problems):
        if document.get("kind") != "ConfigMap":
            continue
        for value in (document.get("data") or {}).values():
            if "storage " in value and "listener " in value:
                return value
    return ""


def check_openbao_bootstraps_itself(render: Path, problems: list[str]) -> None:
    """LucentRoot's OpenBao must need no human in its lifecycle.

    The environment is rebuilt rather than restored, so nothing about a previous
    installation may be required to stand up the next one: no unseal shares, no
    recovery keys, no captured root token. That only holds if self-initialisation
    and auto-unseal are both configured -- self-init requires auto-unseal, and
    auto-unseal without self-init still leaves an uninitialised instance.

    None of this is checkable by a schema, and all of it is silently absent when
    wrong: the platform simply stops converging and waits for someone.
    """
    environment = DISPOSABLE_OPENBAO_ENVIRONMENT
    config = _openbao_config(render, environment, problems)
    if not config:
        return

    if 'initialize "' not in config:
        fail(
            problems,
            f"{environment}: OpenBao has no initialize stanza, so it would wait"
            " for someone to run `bao operator init`",
        )
    if 'seal "' not in config:
        fail(
            problems,
            f"{environment}: OpenBao has no seal stanza, so it would wait for"
            " someone to run `bao operator unseal` on every restart",
        )

    for seal in PRODUCTION_SEAL_TYPES:
        if f'seal "{seal}"' in config:
            fail(
                problems,
                f"{environment}: OpenBao seals against '{seal}', which is"
                " durable external infrastructure this environment does not"
                " have. Its seal is meant to be disposable",
            )

    # A transit seal would also be circular -- OpenBao unsealing against OpenBao.
    if "root_token" in config or "BAO_TOKEN" in config:
        fail(
            problems,
            f"{environment}: OpenBao configuration references a root token."
            " Self-initialisation revokes it rather than storing it",
        )

    # The tenancy boundary the initialize stanza establishes must be the same one
    # the rest of the platform documents.
    if "secret/data/platform/*" not in config:
        fail(
            problems,
            f"{environment}: OpenBao self-init does not grant"
            " secret/data/platform/*, which External Secrets needs",
        )
    if "secret/data/*" in config.replace("secret/data/platform/*", ""):
        fail(
            problems,
            f"{environment}: OpenBao self-init grants secret/data/* rather than"
            " the platform prefix, which would reach client secrets",
        )
    if "clients/" in config:
        fail(
            problems,
            f"{environment}: OpenBao self-init references the client secret"
            " space, which belongs to client provisioning",
        )


def check_seal_key_does_not_need_openbao(render: Path, problems: list[str]) -> None:
    """Nothing OpenBao needs to start may itself come from OpenBao.

    The cycle this prevents is the whole reason the seal key is generated rather
    than projected:

        OpenBao -> ExternalSecret -> ClusterSecretStore -> OpenBao

    A generated secret has no such edge; one sourced from the platform store
    would deadlock at first boot and look like a hung sync.
    """
    for environment in ENVIRONMENTS:
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") != "ExternalSecret":
                    continue
                metadata = document.get("metadata", {})
                if "seal" not in metadata.get("name", ""):
                    continue
                spec = _external_secret_spec(document)
                if (spec.get("secretStoreRef") or {}).get("name"):
                    fail(
                        problems,
                        f"{environment}/{path.name}: the OpenBao seal key is"
                        " sourced from a secret store. Anything OpenBao needs to"
                        " unseal cannot come from OpenBao",
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


def check_service_capabilities(root: Path, problems: list[str]) -> None:
    """Section 14: capability is a declared contract, not a directory name.

    `core` and `catalogue` are deployment tiers. What a service *is* -- whether
    SaaS Fabric requires it, whether operators use it, whether it can hold
    client partitions, whether it is offered as a client capability -- is
    declared per service and checked here, because four independent properties
    cannot be inferred from one filesystem location.

    The rule that does the real work is the last one: a service may not claim
    client capability or client provisioning while its tenancy status is
    anything other than `accepted`. Without it, "we intend to partition this"
    and "this is a boundary" look identical in Git.
    """
    services: dict[str, Path] = {}
    components: dict[str, tuple[str, Path]] = {}

    for klass in ("core", "catalogue"):
        for application in sorted(d for d in (root / "applications" / klass).iterdir() if d.is_dir()):
            where = application.relative_to(root)
            contract = application / SERVICE_CONTRACT
            if not contract.is_file():
                fail(problems, f"{where}: no {SERVICE_CONTRACT}")
                continue

            try:
                declared = yaml.safe_load(contract.read_text()) or {}
            except yaml.YAMLError as error:
                fail(problems, f"{where}/{SERVICE_CONTRACT}: not valid YAML -- {error}")
                continue

            if "componentOf" in declared:
                if "service" in declared:
                    fail(problems, f"{where}/{SERVICE_CONTRACT}: declares both 'service' and 'componentOf'")
                components[str(declared["componentOf"])] = (str(where), application)
                continue

            name = declared.get("service")
            if not name:
                fail(problems, f"{where}/{SERVICE_CONTRACT}: declares neither 'service' nor 'componentOf'")
                continue
            if name in services:
                fail(problems, f"{where}/{SERVICE_CONTRACT}: service '{name}' already declared by {services[name]}")
            services[name] = where

            _check_one_contract(where, declared, application, problems)

    for parent, (where, _) in components.items():
        if parent not in services:
            fail(problems, f"{where}/{SERVICE_CONTRACT}: componentOf '{parent}', which is not a declared service")


def _check_one_contract(where: Path, declared: dict, application: Path, problems: list[str]) -> None:
    """Field validity, then the cross-field rules that carry the meaning."""
    def bad(message: str) -> None:
        fail(problems, f"{where}/{SERVICE_CONTRACT}: {message}")

    deployment = declared.get("deployment")
    if deployment not in DEPLOYMENT_STATES:
        bad(f"deployment '{deployment}' is not one of {', '.join(DEPLOYMENT_STATES)}")
    for field in ("required", "operatorUsage"):
        if not isinstance(declared.get(field), bool):
            bad(f"'{field}' must be true or false")

    partitioning = declared.get("clientPartitioning") or {}
    mode = partitioning.get("mode")
    provisioning = partitioning.get("provisioning")
    if mode not in PARTITION_MODES:
        bad(f"clientPartitioning.mode '{mode}' is not one of {', '.join(PARTITION_MODES)}")
    if provisioning not in PROVISIONING_STATES:
        bad(f"clientPartitioning.provisioning '{provisioning}' is not one of {', '.join(PROVISIONING_STATES)}")

    capability = declared.get("clientCapability") or {}
    available = capability.get("available")
    if not isinstance(available, bool):
        bad("clientCapability.available must be true or false")

    control = declared.get("controlPlane") or {}
    managed = control.get("managed")
    surface = control.get("upstreamAdminSurface")
    admin_backends = control.get("adminBackends") or []
    if managed not in CONTROL_PLANE_MANAGEMENT:
        bad(f"controlPlane.managed '{managed}' is not one of true, false, partial")
    if surface not in ADMIN_SURFACES:
        bad(f"controlPlane.upstreamAdminSurface '{surface}' is not one of {', '.join(ADMIN_SURFACES)}")
    # `not-exposed` is a claim about the cluster, so it has to name what would
    # carry the console -- otherwise check_control_plane_surfaces has nothing to
    # prove the claim against and the rule silently stops applying.
    if surface == "not-exposed" and not admin_backends:
        bad("controlPlane.upstreamAdminSurface is 'not-exposed' but no adminBackends are named -- "
            "name the Services that would front the console so validation can prove none is published")
    if surface == "none" and admin_backends:
        bad("controlPlane.upstreamAdminSurface is 'none' but adminBackends are named -- "
            "a service with no console has nothing to withhold")
    if len(_qualified_backends(admin_backends)) != len(admin_backends):
        bad(UNQUALIFIED_BACKEND.format(field="controlPlane.adminBackends"))

    # Optional, and declared only where exposure is a constraint rather than a
    # description. `operator` has to name the Services it is talking about, for
    # the same reason `not-exposed` does: otherwise check_operator_only_services
    # has nothing to prove the claim against and the rule quietly stops applying.
    exposure = declared.get("exposure")
    if exposure is not None:
        plane = exposure.get("plane")
        if plane not in EXPOSURE_PLANES:
            bad(f"exposure.plane '{plane}' is not one of {', '.join(EXPOSURE_PLANES)}")
        if plane == "operator":
            backends = exposure.get("backends")
            if not backends:
                bad("exposure.plane is 'operator' but no backends are named -- name the "
                    "Services that must stay off the product plane so validation can prove "
                    "none is published there")
            # The same shape `adminBackends` uses, and for the same reason.
            if len(_qualified_backends(backends)) != len(backends or []):
                bad(UNQUALIFIED_BACKEND.format(field="exposure.backends"))
            if not exposure.get("rationale"):
                bad("exposure.plane is 'operator' but no rationale is recorded -- a constraint "
                    "nobody can read the reason for is one somebody will lift")

    tenancy = declared.get("tenancy") or {}
    status = tenancy.get("status")
    if status not in TENANCY_STATES:
        bad(f"tenancy.status '{status}' is not one of {', '.join(TENANCY_STATES)}")
        return

    # An assessment has to say something. `accepted`, `rejected` and
    # `not-applicable` are positions and need a reason; the other two are
    # admissions and need the open questions written down.
    if status in ("accepted", "rejected", "not-applicable") and not tenancy.get("rationale"):
        bad(f"tenancy.status is '{status}' but no rationale is recorded")
    if status in ("candidate", "unresolved") and not tenancy.get("unknowns"):
        bad(f"tenancy.status is '{status}' but no unknowns are recorded -- "
            "document what has not been established rather than leaving it blank")

    # The mode must be licensed by the tenancy status. This is the rule that stops
    # a contract claiming a boundary strength it has not established.
    permitted = MODES_PERMITTED_BY_TENANCY[status]
    if mode in PARTITION_MODES and mode not in permitted:
        bad(f"clientPartitioning.mode '{mode}' with tenancy.status '{status}' -- "
            f"only {' or '.join(permitted)} is valid there. A strength may not be "
            "claimed before the assessment establishing it")

    # A named unit is a claim too: state it once the mechanism is settled, and
    # mark it as a candidate while it is not.
    if mode in ("logical", "strong") and not partitioning.get("unit"):
        bad(f"clientPartitioning.mode is '{mode}' but no unit is named")
    if mode == "unknown" and partitioning.get("unit"):
        bad("clientPartitioning.mode is 'unknown' but a unit is named -- "
            "use candidateUnit for a mechanism that is proposed rather than settled")
    if status == "candidate" and not partitioning.get("candidateUnit"):
        bad("tenancy.status is 'candidate' but no candidateUnit is named -- "
            "a candidate is a specific proposed mechanism, not a general intention")

    # No premature tenancy claims.
    if available and status not in TENANCY_PERMITTING_CLIENTS:
        bad(f"claims clientCapability.available with tenancy.status '{status}' -- "
            "a capability may not be offered to clients before its isolation is established")
    if provisioning == "supported" and status not in TENANCY_PERMITTING_CLIENTS:
        bad(f"claims clientPartitioning.provisioning 'supported' with tenancy.status '{status}'")

    # The contract must match what the directory actually does.
    deploys = (application / "application.yaml").is_file()
    if deployment == "adopted" and not deploys:
        bad("deployment is 'adopted' but the directory has no application.yaml")
    if deployment in ("planned", "assessed") and deploys:
        bad(f"deployment is '{deployment}' but the directory has an application.yaml")


def check_operator_only_services(root: Path, render: Path, problems: list[str]) -> None:
    """A service whose only protection is the plane it is on.

    Perses is the case this exists for. It runs with authentication disabled,
    which is coherent for exactly as long as the operator plane is the whole
    boundary: every viewer is a platform operator, the instance is read-only,
    and there is nothing client-scoped inside it. A product-plane route would
    take all of that away in four lines of YAML, and nothing about the four
    lines would look alarming.

    So `exposure.plane: operator` is a constraint that gets checked rather than
    a note that gets read. The namespace not carrying the product grant is what
    stops it today -- but this repository has twice been wrong about an absent
    label being a guarantee, and an absent label is not a decision anybody made
    on purpose. This is the decision, written where it can fail a build.

    Two things this resolves the way Kubernetes resolves them, rather than the
    way a string comparison would:

    *Identity.* A `backendRef` addresses `(namespace, name)`, and its namespace
    defaults to the route's own. Matching on the name alone would make the
    invariant depend on nobody ever reusing a Service name in another
    namespace, which is a convention rather than a property of the cluster --
    and it would misfire the day a cross-namespace `backendRef` appears.

    *Plane.* What is refused is the product plane specifically, not routing as
    such. An operator-plane route to one of these services is exactly what the
    constraint permits, so the plane is resolved as `check_routes_attach`
    resolves it: the listener the route names, or -- when it names none -- the
    grant its namespace carries.

    Lifting the constraint means authentication, authorization and an
    established tenancy model. It does not mean editing the contract until
    validation stops complaining.
    """
    # (namespace, name) -> the service that declared it off the product plane.
    operator_only: dict[tuple[str, str], str] = {}
    for contract in sorted(root.glob("applications/*/*/platform-service.yaml")):
        declared = yaml.safe_load(contract.read_text()) or {}
        exposure = declared.get("exposure") or {}
        if exposure.get("plane") != OPERATOR_LISTENER:
            continue
        for service in _qualified_backends(exposure.get("backends")):
            operator_only[service] = declared.get("service", contract.parent.name)
    if not operator_only:
        return

    for environment in ENVIRONMENTS:
        destinations = _destination_namespaces(render, environment, problems)
        product_namespaces: set[str] = set()
        routes = []
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                kind = document.get("kind")
                if kind == "HTTPRoute":
                    routes.append((path, document))
                elif kind == "Application":
                    managed = (
                        document["spec"]
                        .get("syncPolicy", {})
                        .get("managedNamespaceMetadata", {})
                        .get("labels", {})
                    )
                    if managed.get(PRODUCT_GRANT) == "true":
                        product_namespaces.add(document["spec"]["destination"]["namespace"])

        for path, route in routes:
            # Resolved rather than read, for the reason given in
            # _destination_namespaces: a chart-rendered route need not carry one.
            namespace = _resource_namespace(route, path, destinations)
            name = route["metadata"]["name"]
            reached = {
                _backend_service(backend, namespace)
                for rule in route["spec"].get("rules", [])
                for backend in rule.get("backendRefs", [])
            } & set(operator_only)
            if not reached:
                continue

            for parent in route["spec"].get("parentRefs", []):
                section = parent.get("sectionName")
                if section == OPERATOR_LISTENER:
                    continue
                # No sectionName means the route takes whichever listener its
                # namespace is granted, so the label decides the plane.
                if section is None and namespace not in product_namespaces:
                    continue
                for service in sorted(reached):
                    fail(
                        problems,
                        f"{environment}/{path.name}: HTTPRoute/{name} can reach"
                        f" '{service[1]}.{service[0]}' from the product plane, and"
                        f" {operator_only[service]} is declared"
                        " operator-plane-only. Its contract says why, and the"
                        " answer is not to widen the route",
                    )


def _backend_service(backend: dict, route_namespace: str) -> tuple[str, str]:
    """A backendRef resolved to the Service it addresses, or a miss.

    Gateway API defaults `kind` to Service in the core group, and `namespace` to
    the route's own. A ref to something else -- a different kind, or an
    implementation-specific group -- is not a Service and must not be matched
    against one, so it resolves to a pair nothing can equal.
    """
    group = backend.get("group", "")
    kind = backend.get("kind", "Service")
    if group not in ("", None) or kind != "Service":
        return ("", "")
    return (backend.get("namespace") or route_namespace, backend.get("name") or "")


def _qualified_backends(entries: list) -> list[tuple[str, str]]:
    """Declared backends as (namespace, name), skipping malformed entries.

    Malformed entries are reported by the contract check, so skipping them here
    keeps one bad field from masking every real violation the same pass would
    otherwise have found.
    """
    resolved = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name, namespace = entry.get("name"), entry.get("namespace")
        if name and namespace:
            resolved.append((namespace, name))
    return resolved


def _destination_namespaces(render: Path, environment: str, problems: list[str]) -> dict[str, str]:
    """Where each Application's resources actually land, keyed by rendered file.

    Necessary because `metadata.namespace` is not where a resource's namespace
    reliably *is*. Helm charts routinely omit it -- the Perses chart's Ingress
    does -- and Argo CD then applies the resource into the Application's
    destination. A check that read only the document would be blind to exactly
    the chart-rendered resources most likely to publish something by accident.

    `render.py` names each rendered file after the Application that produced it,
    which is what makes the two sides joinable.
    """
    destinations: dict[str, str] = {}
    for name in ("bootstrap.yaml", "platform.yaml"):
        source = render / environment / name
        if not source.is_file():
            continue
        for document in load_all(source, problems):
            if document.get("kind") == "Application":
                destinations[document["metadata"]["name"]] = document["spec"]["destination"]["namespace"]
    return destinations


def _resource_namespace(document: dict, path: Path, destinations: dict[str, str]) -> str:
    """The namespace a rendered resource will exist in, as Argo CD resolves it."""
    return document.get("metadata", {}).get("namespace") or destinations.get(path.stem, "")


def check_control_plane_surfaces(root: Path, render: Path, problems: list[str]) -> None:
    """Section 15: SaaS Fabric is the administrative control plane.

    A service whose upstream administration SaaS Fabric has taken over must not
    have that upstream console published on any plane. The console being absent
    today is not the invariant -- nothing stopping it returning is the problem,
    and an Ingress is one line to add.

    "Upstream software ships an admin UI" is not an operational need. Services
    whose UI is itself the capability (Perses) declare `exposed`, and diagnostic
    surfaces (OpenBao) declare `break-glass`; both are left alone.

    A withheld backend is `(namespace, name)`, not a name. An Ingress backend is
    always in the Ingress's own namespace -- no defaulting rule, unlike a Gateway
    API `backendRef` -- but *which* namespace that is has to be resolved rather
    than read, because a chart may not have written one down.
    """
    withheld: dict[tuple[str, str], str] = {}
    for contract in sorted(root.glob("applications/*/*/platform-service.yaml")):
        declared = yaml.safe_load(contract.read_text()) or {}
        control = declared.get("controlPlane") or {}
        if control.get("upstreamAdminSurface") != "not-exposed":
            continue
        for service in _qualified_backends(control.get("adminBackends")):
            withheld[service] = declared.get("service", contract.parent.name)
    if not withheld:
        return

    for environment in ENVIRONMENTS:
        destinations = _destination_namespaces(render, environment, problems)
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") != "Ingress":
                    continue
                name = document["metadata"]["name"]
                namespace = _resource_namespace(document, path, destinations)
                for rule in document["spec"].get("rules", []):
                    for entry in (rule.get("http") or {}).get("paths", []):
                        backend = entry.get("backend", {}).get("service", {}).get("name")
                        service = (namespace, backend)
                        if service in withheld:
                            fail(
                                problems,
                                f"{environment}/{path.name}: Ingress/{name} publishes"
                                f" '{backend}.{namespace}', the upstream administrative"
                                f" surface of {withheld[service]}, which SaaS Fabric"
                                " administers through its API instead",
                            )


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
    check_control_plane_is_operator_only(render, problems)
    check_the_patch_hatch_stays_narrow(render, problems)
    check_argocd_runtime_configuration(render, problems)
    check_secret_store_is_bounded(render, problems)
    check_projects_permit_what_apps_deploy(render, problems)
    check_platform_secrets_stay_platform(render, problems)
    check_openbao_bootstraps_itself(render, problems)
    check_seal_key_does_not_need_openbao(render, problems)
    check_collector_pipelines(render, problems)
    check_application_documentation(root, problems)
    check_service_capabilities(root, problems)
    check_control_plane_surfaces(root, render, problems)
    check_operator_only_services(root, render, problems)

    if problems:
        print(f"{len(problems)} problem(s):\n")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print("All repository invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
