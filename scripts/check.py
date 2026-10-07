#!/usr/bin/env python3
"""Repository invariants that a schema validator cannot express.

    scripts/check.py [render-directory]

Checks, in order of how much damage they prevent:

  1. no plaintext secret material, in the sources or in the rendered output;
  2. no duplicate Kubernetes resource across a single environment;
  3. every chart repository an Application uses is allowed by its AppProject;
  4. every chart version is pinned exactly, never a range;
  5. every Application's destination namespace is allowed by its AppProject;
  6. every namespaced resource an Application actually renders lands in a
     namespace that Application's project permits, not merely the one
     namespace it declares as its own destination;
  7. no client-scoped resource has crept into a platform environment;
  8. the two exposure planes stay separate: product traffic on Gateway API
     routes attached to a listener that exists from a namespace allowed to,
     operator traffic on Tailscale Ingresses, and no third routing authority;
  9. no administrative surface on the product plane;
 10. the Argo CD runtime configuration the platform depends on is present;
 11. the platform secret store is bounded to platform namespaces, and nothing
     reads a client secret path, or SaaS Fabric's own instance partition,
     through it;
 12. LucentRoot's OpenBao initialises and unseals itself, against a seal that
     does not depend on OpenBao;
 13. LucentRoot's master realm converges its own instance resources the same
     way -- a generated client secret and a convergence Job, never a person
     creating a client in Keycloak and writing its secret into OpenBao
     (ADR 0025, application repository) -- and every lookup that convergence
     makes is read at plan time, so a missing operator account fails it
     before anything is written rather than after, and no roster edit can
     plan a revocation of an operator's grants;
 14. every in-cluster service reference resolves to something this repository
     actually deploys;
 15. the telemetry pipelines only reference components that exist;
 16. every application directory carries the required documentation;
 17. a service whose only protection is the operator plane stays on it;
 18. `data-sources.yaml` declares only what its own schema and ADR 0006's
     shared-needs-discriminator rule permit, and never a credential;
 19. `placements.yaml` records only a placement whose data source is
     declared, whose isolation agrees with that data source's placement
     class, and that does not collide with another placement on the same
     data source;
 20. no rendered manifest declares a `fabric-runtime-*` ConfigMap in
     `platform-system` -- the runtime publisher owns those, and Git must not
     be able to revert a publication (ADR 0023 §4, application repository).
"""
from __future__ import annotations

import posixpath
import re
import sys
import tomllib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

ENVIRONMENTS = ("lucentroot", "production")

# The `components.yaml` shapes this checker understands. Moved together with
# the readers, so a manifest that has moved on fails loudly here rather than
# being half-understood. Schema 3 adds the `described` artifact type -- a
# component read through the component descriptor attached to its primary image
# (ADR 0026, application repository) -- and a file may be either version: Fabric
# writes back the version it read, so a schema 2 file stays schema 2 until the
# pull request that switches a component to `described` moves the whole file.
COMPONENTS_SCHEMA_VERSIONS = (2, 3)
# The artifact types this checker reads, and the schema each first appears in.
# Both render images by role and digest, so both are held to the same pins and
# the same rendered output. A Helm component is read by Fabric but not yet by
# this checker, and is refused here rather than waved through.
COMPONENT_ARTIFACT_TYPES = {"oci": 2, "described": 3}

# The `environments/<environment>/data-sources.yaml` shape this checker
# understands (ADR 0023 part 1, application repository). Its own version,
# independent of COMPONENTS_SCHEMA_VERSION -- it is a different document with
# its own history, and the two happen to start a generation apart.
DATA_SOURCES_SCHEMA_VERSION = 1

# `placement`, spelled exactly as the wire's `PlacementClassDocument` spells
# it -- the reason entries in this file are snake_case where its own envelope,
# like components.yaml, is camelCase.
DATA_SOURCE_PLACEMENTS = (
    "shared", "dedicated", "high_availability", "regulated", "development", "ephemeral",
)

# A `fabric_core::DataSourceId` (`crates/fabric-core/src/ids/data_source_id.rs`,
# application repository): parsed with `parse_identifier`, not the DNS-label
# rule -- an ASCII letter, then up to 62 more ASCII letters, digits, hyphens,
# or underscores. Mixed case and underscores are both legal; `sql-au-east-03`
# and `Sql_AU_East_03` are both valid ids.
DATA_SOURCE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")

DATA_SOURCE_ENVELOPE_KEYS = {"schemaVersion", "environment", "dataSources"}
DATA_SOURCE_ENTRY_KEYS = {
    "id", "revision", "connector", "connection", "placement", "residency",
    "pool", "capabilities", "discriminator", "labels",
}
DATA_SOURCE_REQUIRED_ENTRY_KEYS = {
    "id", "revision", "connector", "connection", "placement", "residency",
}
# A connection names what it holds, never a value: `named` points at a
# connection the connector process already has, `secret` carries a reference
# path into wherever secrets live. Keyed by `kind` so a third shape -- or a
# `value` key beside either -- cannot be smuggled in.
CONNECTION_KEYS_BY_KIND = {"named": {"kind", "name"}, "secret": {"kind", "reference"}}
RESIDENCY_KEYS = {"region", "jurisdiction"}
# Each defaults (20 / 300 / 5) when absent, so only a value someone actually
# wrote can be checked against the positive-pool rule.
POOL_KEYS = ("max_connections", "idle_timeout_seconds", "acquire_timeout_seconds")
CAPABILITIES_KEYS = ("writable", "accepts_new_tenants")
DISCRIMINATOR_KEYS = {"column"}

# The `environments/<environment>/placements.yaml` shape this checker
# understands (ADR 0023 part 2, application repository). Its own version,
# independent of the other two schema versions beside it.
PLACEMENTS_SCHEMA_VERSION = 1

PLACEMENT_ENVELOPE_KEYS = {"schemaVersion", "environment", "placements"}
PLACEMENT_REQUIRED_ENTRY_KEYS = {"tenant", "logical", "data_source", "isolation", "placed_at"}
# `revision` is the one optional entry key: a break-glass edit to a record
# that predates it, or one nobody has ever hand-edited, still parses -- the
# application repository defaults it to 1 when absent
# (`fabric_core::BindingRevision`'s `first_revision`), so this checker must
# accept its absence the same way rather than demanding a field the runtime
# does not.
PLACEMENT_ENTRY_KEYS = PLACEMENT_REQUIRED_ENTRY_KEYS | {"revision"}

# A `fabric_core::TenantId`: lowercase and DNS-label-like -- `parse_dns_label`,
# not `parse_identifier`, so it does not share `DATA_SOURCE_ID`'s character
# class even though the two regexes once happened to be identical.
PLACEMENT_TENANT_ID = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# A `fabric_core::LogicalDataSourceName`: `parse_identifier`, the same rule
# as `DATA_SOURCE_ID` -- an ASCII letter, then letters, digits, hyphens, or
# underscores -- `primary`, `audit`, `analytics`.
LOGICAL_DATA_SOURCE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")

# `isolation`, spelled exactly as the wire's `IsolationModelDocument` --
# keyed by `kind` so a field belonging to a different kind cannot be smuggled
# in beside it.
ISOLATION_KEYS_BY_KIND = {
    "database": {"kind"},
    "schema": {"kind", "schema"},
    "discriminator": {"kind", "column", "value"},
}

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
# SaaS Fabric's control plane keeps its own integration credentials -- its Git
# applications' private keys and the registry tokens an operator registers --
# in this partition, and delivers none of them to anything (ADR 0026 section 6,
# application repository). External Secrets reads everything else under
# `platform/`, so this prefix is the one place beneath it that the platform
# store must never reach: not by key, and not by a `find` over a prefix that
# contains it.
FABRIC_INSTANCE_PREFIX = "platform/saas-fabric/instances/"
# The same partition as path segments, which is how every comparison against
# it is made. A string prefix is the wrong instrument: `instances-public` is a
# sibling of `instances`, not a path beneath it, and `startswith` cannot tell
# the two apart. See _reaches_fabric_instances.
FABRIC_INSTANCE_SEGMENTS = tuple(
    segment for segment in FABRIC_INSTANCE_PREFIX.split("/") if segment
)
# The OpenBao ACL policy External Secrets' role is bound to, and the self-init
# request that writes it. The deny on the partition is verified *inside this
# request's policy text*, never by searching the configuration as a whole --
# a deny in a comment, or in some other policy, is not a deny the External
# Secrets token is subject to.
EXTERNAL_SECRETS_POLICY_NAME = "platform-secrets"
EXTERNAL_SECRETS_POLICY_REQUEST_PATH = f"sys/policies/acl/{EXTERNAL_SECRETS_POLICY_NAME}"
EXTERNAL_SECRETS_ROLE_REQUEST_PATH = "auth/kubernetes/role/external-secrets"
# Self-init operations that write the resource a request names. Anything else
# -- `read`, `delete`, a typo -- does not establish a policy and so cannot
# satisfy the deny.
WRITING_INITIALIZE_OPERATIONS = ("update", "create")
# The two paths the policy must deny: the partition's data and its metadata.
# Exactly the glob the policy spells, because the verification compares path
# labels literally -- a policy that denied `instances/master/*` instead would
# leave the rest of the partition readable.
FABRIC_INSTANCE_DENIED_PATHS = tuple(
    f"secret/{mount}/{FABRIC_INSTANCE_PREFIX}*" for mount in ("data", "metadata")
)
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


def check_no_runtime_publication_configmaps(render: Path, problems: list[str]) -> None:
    """Nothing in Git may declare a `fabric-runtime-*` ConfigMap in `platform-system`.

    `fabric-runtime-tenants`, `fabric-runtime-data-sources` and
    `fabric-runtime-catalog` are written directly to the API server by the
    control plane's runtime publisher (ADR 0023 §4, application repository) --
    created and replaced over plain HTTPS, never through a commit. A rendered
    manifest that declared one of the three, even with content that happened
    to match, would hand Argo CD's self-heal a reason to overwrite whatever
    the publisher last wrote with whatever this repository last said, which is
    exactly the revert this decision exists to make structurally impossible.
    The absence of a Git source is the guarantee, so this checker looks for
    one and fails the build if it ever finds it.
    """
    for environment in ENVIRONMENTS:
        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                if document.get("kind") != "ConfigMap":
                    continue
                metadata = document.get("metadata") or {}
                name = metadata.get("name")
                if (
                    metadata.get("namespace") == "platform-system"
                    and isinstance(name, str)
                    and name.startswith("fabric-runtime-")
                ):
                    fail(
                        problems,
                        f"{environment}: {path.name} declares ConfigMap {name!r} in "
                        "platform-system -- the runtime publisher owns fabric-runtime-* "
                        "and Git must not be able to revert its writes",
                    )


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


def check_namespaced_resources_stay_in_project_destinations(render: Path, problems: list[str]) -> None:
    """An Application can render into more than the one namespace it declares.

    check_applications_match_their_project, immediately above, checks only an
    Application's own `spec.destination.namespace` -- the one namespace it
    states. check_projects_permit_what_apps_deploy separately covers
    cluster-scoped kinds. Neither looks at where an Application's own rendered,
    namespaced resources actually land -- a `Role` or a `Secret` crossing into
    a namespace this Application's project does not list in `destinations`
    (`identity`, `master-instance-state`, and so on) is exactly the shape this
    platform uses deliberately and repeatedly, and exactly the shape a typo in
    a namespace can turn into an application that syncs cleanly in this script
    and is refused by the cluster.

    A namespace an Application renders into but its project does not permit is
    an Argo CD sync failure today, not a CI one -- discovered only once a
    cluster refuses the resource. This is what makes it one here instead.
    """
    for environment in ENVIRONMENTS:
        projects: dict[str, dict] = {}
        applications = []
        for name in ("bootstrap.yaml", "platform.yaml"):
            for document in load_all(render / environment / name, problems):
                if document.get("kind") == "AppProject":
                    projects[document["metadata"]["name"]] = document["spec"]
                elif document.get("kind") == "Application":
                    applications.append(document)

        destinations = _destination_namespaces(render, environment, problems)

        for application in applications:
            name = application["metadata"]["name"]
            spec = application["spec"]
            project = projects.get(spec["project"])
            if project is None:
                continue  # reported by check_applications_match_their_project

            permitted = {d.get("namespace") for d in project.get("destinations", [])}
            if "*" in permitted:
                continue

            rendered = render / environment / "applications" / f"{name}.yaml"
            if not rendered.is_file():
                continue

            reported: set[str] = set()
            for document in load_all(rendered, problems):
                group = _group_of(document.get("apiVersion", ""))
                kind = document.get("kind", "")
                if not kind or (group, kind) in CLUSTER_SCOPED_KINDS:
                    continue
                namespace = _resource_namespace(document, rendered, destinations)
                if not namespace or namespace in permitted or namespace in reported:
                    continue
                reported.add(namespace)
                fail(
                    problems,
                    f"{environment}: {name} renders resources into namespace"
                    f" {namespace}, which project {spec['project']} does not"
                    " permit -- an Argo CD sync failure today, not a CI one,"
                    " discovered only once a cluster refuses the resource",
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


def check_forwarded_proto_is_asserted_once(render: Path, problems: list[str]) -> None:
    """Only Keycloak's operator route, and a listener-wide policy on the
    operator listener itself, may restate the scheme.

    That route sets `X-Forwarded-Proto: https` because Envoy is an edge proxy
    and overwrites the header the operator ingress set, leaving Keycloak told
    `http` and answering `HTTPS required`. The value is the gateway's statement
    about a listener that only serves the operator hostname behind a
    TLS-terminating ingress -- not the request's claim about itself.

    That premise is false anywhere else. The product listener's downstream is a
    LAN client that may genuinely be on plain HTTP, and the same filter there
    would label it `https` -- satisfying Keycloak's HTTPS requirement over
    plaintext, which is the check the header exists to preserve. So this
    asserts both halves: nothing else sets it, and this route still does.
    Losing it is silent until an operator cannot sign in.

    A `ClientTrafficPolicy` earns the identical exception, for the identical
    reason, when it targets the `platform` Gateway's `operator` listener: that
    listener's downstream is *always* the same TLS-terminating ingress, so
    asserting `https` there states the same fact this file already lets
    Keycloak's route state. One targeting any other listener or any other
    Gateway is exactly the product-plane mistake above, one level up -- a
    LAN client's connection can genuinely be plaintext, and a policy is a
    blunter instrument than a route for making that mistake cluster-wide.

    The policy must also live in `platform-system` itself. A `targetRefs`
    entry carries no `namespace` -- Gateway API's local policy attachment
    resolves a target within the policy's own namespace only -- so a `name:
    platform` targeting a Gateway from some *other* namespace would resolve
    to whatever that namespace's own "platform" happens to be, not to this
    one. Requiring the policy's namespace is what makes "the platform
    Gateway's operator listener" mean the one this repository actually
    deploys, not any object a coincidental name collision could produce.
    """
    for environment in ENVIRONMENTS:
        asserted = []
        present = False

        for path in sorted((render / environment).rglob("*.yaml")):
            for document in load_all(path, problems):
                kind = document.get("kind")
                if kind == "HTTPRoute":
                    metadata = document["metadata"]
                    if (metadata["name"], metadata["namespace"]) == ("keycloak-operator", "identity"):
                        present = True
                    for rule in document["spec"].get("rules", []):
                        for filter_ in rule.get("filters", []):
                            modifier = filter_.get("requestHeaderModifier") or {}
                            for header in (modifier.get("set") or []) + (modifier.get("add") or []):
                                if header.get("name", "").lower() == "x-forwarded-proto":
                                    asserted.append((path, document, header.get("value")))
                elif kind == "ClientTrafficPolicy":
                    early = ((document.get("spec") or {}).get("headers") or {}).get(
                        "earlyRequestHeaders"
                    ) or {}
                    for header in (early.get("set") or []) + (early.get("add") or []):
                        if header.get("name", "").lower() == "x-forwarded-proto":
                            asserted.append((path, document, header.get("value")))

        for path, resource, value in asserted:
            metadata = resource["metadata"]
            kind = resource["kind"]
            where = f"{environment}/{path.name}: {kind}/{metadata['name']}"

            if kind == "HTTPRoute":
                if (metadata["name"], metadata["namespace"]) != ("keycloak-operator", "identity"):
                    fail(
                        problems,
                        f"{where} sets X-Forwarded-Proto; only keycloak-operator"
                        " may, because only its listener is reached through an"
                        " ingress that has already terminated TLS",
                    )
                    continue

                sections = {
                    parent.get("sectionName")
                    for parent in resource["spec"].get("parentRefs", [])
                }
                if sections != {OPERATOR_LISTENER}:
                    fail(
                        problems,
                        f"{where} sets X-Forwarded-Proto but attaches to"
                        f" {sorted(str(s) for s in sections)}; on any listener but"
                        f" '{OPERATOR_LISTENER}' that labels a plaintext client"
                        " https",
                    )

            else:  # ClientTrafficPolicy
                targets = list(resource["spec"].get("targetRefs") or [])
                single = resource["spec"].get("targetRef")
                if single:
                    targets.append(single)
                on_operator_listener = (
                    metadata["namespace"] == "platform-system"
                    and any(
                        target.get("kind") == "Gateway"
                        and target.get("name") == "platform"
                        and target.get("sectionName") == OPERATOR_LISTENER
                        for target in targets
                    )
                )
                if not on_operator_listener:
                    fail(
                        problems,
                        f"{where} sets X-Forwarded-Proto; only a"
                        " ClientTrafficPolicy in platform-system targeting the"
                        f" platform Gateway's '{OPERATOR_LISTENER}' listener"
                        " may, because only that listener is reached through"
                        " an ingress that has already terminated TLS",
                    )
                    continue

            if value != "https":
                fail(problems, f"{where} sets X-Forwarded-Proto to '{value}', not https")

        # Conditional on the route existing, not decreed for every
        # environment: production publishes no operator plane yet, so it has no
        # Keycloak route and nothing here to assert. An environment that grows
        # one is held to this from its first render.
        if present and not asserted:
            fail(
                problems,
                f"{environment}: keycloak-operator sets no X-Forwarded-Proto;"
                " Keycloak answers 'HTTPS required' to every operator sign-in"
                " without it, and nothing else reports that",
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


def _path_segments(remote: str) -> tuple[str, ...]:
    """A remote path as the segments OpenBao resolves, empty ones dropped.

    `/platform//x/` and `platform/x` name the same place to a KV mount, so
    the comparison is made on segments rather than characters. Dropping empty
    segments can only make a path look *more* like the partition, never less,
    which is the direction a boundary check is allowed to err in.
    """
    return tuple(segment for segment in str(remote).split("/") if segment)


def _is_within_fabric_instances(segments: tuple[str, ...]) -> bool:
    """Whether a path is the partition root itself or anything beneath it.

    The root is included on purpose. The ACL glob
    `secret/data/platform/saas-fabric/instances/*` matches what lies *beneath*
    the root and not a secret written at the bare root path; nothing legitimate
    is written there -- the control plane's grant is `instances/master/*` --
    so this checker refuses the root as well rather than leave a path the
    policy does not cover as the one path the checker also ignores. Stricter
    than the ACL, never wider. Documented in
    applications/core/external-secrets/README.md, "Exact-root semantics".
    """
    depth = len(FABRIC_INSTANCE_SEGMENTS)
    return len(segments) >= depth and segments[:depth] == FABRIC_INSTANCE_SEGMENTS


def _is_ancestor_of_fabric_instances(segments: tuple[str, ...]) -> bool:
    """Whether a `find` over this path would enumerate the partition.

    The whole store (no segments at all) and every prefix of the partition --
    `platform`, `platform/saas-fabric` -- contain it. The root itself is
    handled by _is_within_fabric_instances.
    """
    depth = len(segments)
    return depth < len(FABRIC_INSTANCE_SEGMENTS) and FABRIC_INSTANCE_SEGMENTS[:depth] == segments


def _reaches_fabric_instances(entry: dict) -> bool:
    """Whether one data or dataFrom entry can select SaaS Fabric's partition.

    An exact key (`remoteRef.key`, `extract.key`) reaches it by naming the
    partition root or a path beneath it. A `find` reaches it when its path is
    the root or beneath it, when its path is an ancestor whose subtree
    contains it, and when it has no path at all -- a find with no path
    searches the whole store, and the partition is part of the store.

    Compared segment by segment, so a sibling such as
    `platform/saas-fabric/instances-public` is what it is -- a different path
    -- rather than a string that happens to start the same way.
    """
    for key in (
        (entry.get("remoteRef") or {}).get("key"),
        (entry.get("extract") or {}).get("key"),
    ):
        if key and _is_within_fabric_instances(_path_segments(key)):
            return True

    find = entry.get("find")
    if find is None:
        return False
    if not isinstance(find, dict):
        # Not a shape ESO accepts, so not one this checker can bound. Refuse.
        return True
    segments = _path_segments(find.get("path") or "")
    if not segments:
        return True
    return _is_within_fabric_instances(segments) or _is_ancestor_of_fabric_instances(segments)


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

                    if _reaches_fabric_instances(entry):
                        fail(
                            problems,
                            f"{environment}/{path.name}:"
                            f" {document['kind']}/{name} {field}[{index}]"
                            f" can read '{FABRIC_INSTANCE_PREFIX}' through the"
                            " platform store. That partition holds SaaS Fabric's"
                            " own integration credentials, and nothing is"
                            " delivered from it (ADR 0026, application repository)",
                        )

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


class ConfigSyntaxError(ValueError):
    """OpenBao's configuration could not be read far enough to verify anything.

    Raised, never swallowed: a configuration this reader cannot follow is one
    whose policy it cannot vouch for, and the caller turns it into a failure.
    """


# A bounded reader for the configuration language OpenBao shares with HCL,
# covering exactly what the `initialize` stanza and an ACL policy use: blocks
# with string labels, `name = value` attributes, objects, lists, quoted
# strings, `<<MARKER` / `<<-MARKER` heredocs, and `#`, `//`, `/* */` comments.
#
# It exists so the deny on the instance partition is verified in the policy
# that External Secrets' role is actually bound to, inside the request that
# actually writes it. A regex over the whole configuration was satisfied by a
# deny inside a comment, and by a deny inside some other policy. Anything this
# reader does not understand raises ConfigSyntaxError, and the check fails --
# it never guesses.
_HCL_IDENT_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789-")
_HCL_IDENT_CHARS = _HCL_IDENT_START | frozenset(".")
_HCL_PUNCTUATION = frozenset("{}[]=,")


# The backslash escapes a quoted string may contain, and what each means.
# These five are the whole list on purpose. HCL defines others -- `\uNNNN`
# and `\UNNNNNNNN` -- and this reader does not implement them, so it refuses
# them rather than approximating: an escape it does not understand is a
# string whose value it cannot vouch for, and a policy name or path label it
# cannot vouch for is one it must not credit. Dropping the backslash and
# keeping the next character, which is what an unbounded reader does, would
# let `"d\eny"` read as `deny`.
_HCL_STRING_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def _read_hcl_quoted(text: str, start: int) -> tuple[str, int]:
    """A double-quoted string beginning at `start`; returns (value, next index).

    Only the escapes in _HCL_STRING_ESCAPES are decoded. Any other backslash
    sequence is a ConfigSyntaxError, never silently unescaped.
    """
    out: list[str] = []
    i = start + 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            if i + 1 >= len(text):
                break
            escaped = text[i + 1]
            if escaped not in _HCL_STRING_ESCAPES:
                raise ConfigSyntaxError(
                    f"unsupported string escape '\\{escaped}'; this reader decodes"
                    " only \\n \\r \\t \\\" and \\\\"
                )
            out.append(_HCL_STRING_ESCAPES[escaped])
            i += 2
            continue
        if c == '"':
            return "".join(out), i + 1
        if c == "\n":
            break
        out.append(c)
        i += 1
    raise ConfigSyntaxError("unterminated quoted string")


def _read_hcl_heredoc(text: str, start: int) -> tuple[str, int]:
    """A heredoc beginning at `start` (`<<MARKER` or `<<-MARKER`).

    Returns the body and the index after the terminator line. `<<-` strips the
    common leading whitespace, as HCL does; the terminator must stand alone on
    its own line. A heredoc that never terminates is a syntax error.
    """
    i = start + 2
    indented = i < len(text) and text[i] == "-"
    if indented:
        i += 1
    line_end = text.find("\n", i)
    if line_end == -1:
        raise ConfigSyntaxError("heredoc marker without a body")
    marker = text[i:line_end].strip()
    if not marker or any(c not in _HCL_IDENT_CHARS for c in marker):
        raise ConfigSyntaxError("heredoc marker is not an identifier")

    lines: list[str] = []
    position = line_end + 1
    while position <= len(text):
        next_end = text.find("\n", position)
        line = text[position:] if next_end == -1 else text[position:next_end]
        if line.strip() == marker:
            body = lines
            if indented:
                indents = [len(l) - len(l.lstrip()) for l in body if l.strip()]
                strip = min(indents) if indents else 0
                body = [l[strip:] if l.strip() else l.strip() for l in body]
            return "\n".join(body) + "\n", (len(text) if next_end == -1 else next_end + 1)
        lines.append(line)
        if next_end == -1:
            break
        position = next_end + 1
    raise ConfigSyntaxError(f"heredoc {marker!r} never terminates")


def _tokenize_hcl(text: str) -> list[tuple[str, str]]:
    """(kind, value) tokens: 'string', 'ident', or one of {}[]=, as itself."""
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "#" or text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise ConfigSyntaxError("unterminated block comment")
            i = end + 2
        elif c == '"':
            value, i = _read_hcl_quoted(text, i)
            tokens.append(("string", value))
        elif text.startswith("<<", i):
            value, i = _read_hcl_heredoc(text, i)
            tokens.append(("string", value))
        elif c in _HCL_PUNCTUATION:
            tokens.append((c, c))
            i += 1
        elif c in _HCL_IDENT_START:
            j = i + 1
            while j < n and text[j] in _HCL_IDENT_CHARS:
                j += 1
            tokens.append(("ident", text[i:j]))
            i = j
        else:
            raise ConfigSyntaxError(f"unexpected character {c!r}")
    return tokens


def _parse_hcl_expression(tokens: list[tuple[str, str]], i: int):
    """One attribute value: a string, a bare word, an object or a list."""
    if i >= len(tokens):
        raise ConfigSyntaxError("attribute without a value")
    kind, value = tokens[i]
    if kind == "string":
        return value, i + 1
    if kind == "ident":
        # A bare word -- `true`, `8200`, `var.x` -- is not a string literal,
        # and must not be mistaken for one: a policy that is a reference is a
        # policy this checker cannot read.
        return ("bare", value), i + 1
    if kind == "{":
        body, i = _parse_hcl_body(tokens, i + 1)
        obj: dict = {}
        for item in body:
            if item[0] != "attr":
                raise ConfigSyntaxError("a block where an object attribute was expected")
            if item[1] in obj:
                raise ConfigSyntaxError(f"object key {item[1]!r} is set more than once")
            obj[item[1]] = item[2]
        return obj, i
    if kind == "[":
        items: list = []
        i += 1
        while i < len(tokens) and tokens[i][0] != "]":
            if tokens[i][0] == ",":
                i += 1
                continue
            item, i = _parse_hcl_expression(tokens, i)
            items.append(item)
        if i >= len(tokens):
            raise ConfigSyntaxError("unterminated list")
        return items, i + 1
    raise ConfigSyntaxError(f"unexpected {value!r} where a value was expected")


def _parse_hcl_body(tokens: list[tuple[str, str]], i: int, top_level: bool = False):
    """Items until the closing brace: ('attr', name, value) and
    ('block', type, labels, body)."""
    items: list[tuple] = []
    n = len(tokens)
    while i < n:
        kind, value = tokens[i]
        if kind == "}":
            if top_level:
                raise ConfigSyntaxError("closing brace with nothing open")
            return items, i + 1
        if kind == "ident" or kind == "string":
            if i + 1 < n and tokens[i + 1][0] == "=":
                expression, i = _parse_hcl_expression(tokens, i + 2)
                items.append(("attr", value, expression))
                continue
            if kind == "ident":
                j = i + 1
                labels: list[str] = []
                while j < n and tokens[j][0] == "string":
                    labels.append(tokens[j][1])
                    j += 1
                if j < n and tokens[j][0] == "{":
                    body, i = _parse_hcl_body(tokens, j + 1)
                    items.append(("block", value, labels, body))
                    continue
        if kind == ",":
            i += 1
            continue
        raise ConfigSyntaxError(f"unexpected {value!r}")
    if not top_level:
        raise ConfigSyntaxError("block never closes")
    return items, i


def parse_hcl(text: str) -> list[tuple]:
    """The top-level items of an HCL-shaped document. Raises ConfigSyntaxError."""
    return _parse_hcl_body(_tokenize_hcl(text), 0, top_level=True)[0]


def _hcl_blocks(items: list[tuple], block_type: str) -> list[tuple]:
    return [item for item in items if item[0] == "block" and item[1] == block_type]


def _hcl_attribute(items: list[tuple], name: str):
    """The value of a body attribute, or None. A repeated attribute is a
    syntax error here even where HCL would take the last one: two `policy`
    lines in one request is an ambiguity, not a choice this checker makes."""
    values = [item[2] for item in items if item[0] == "attr" and item[1] == name]
    if len(values) > 1:
        raise ConfigSyntaxError(f"attribute {name!r} is set more than once")
    return values[0] if values else None


def _initialize_requests(config: str, request_path: str) -> list[list[tuple]]:
    """The bodies of every `initialize { request { path = <request_path> } }`.

    All of them, not the first: requests run in order and the last writer
    wins, so each one is held to the same standard rather than guessing
    which is active.
    """
    found: list[list[tuple]] = []
    for stanza in _hcl_blocks(parse_hcl(config), "initialize"):
        for request in _hcl_blocks(stanza[3], "request"):
            path = _hcl_attribute(request[3], "path")
            if isinstance(path, str) and path.strip("/") == request_path:
                found.append(request[3])
    return found


def _policy_rules(policy_text: str) -> dict[str, list[str]]:
    """path label -> declared capabilities, from an OpenBao ACL policy document.

    Accepts the modern `capabilities = [...]` list of string literals, or the
    legacy `policy = "<word>"` spelling, which OpenBao still honours -- but
    never both in one rule, and never the same path declared twice. Those are
    declarations whose combination OpenBao resolves by rules this checker does
    not implement, and it is not going to guess which half wins: an ambiguous
    declaration is a ConfigSyntaxError, so the check fails rather than passes.
    This is not an ACL interpreter; it reads what one policy declares.
    """
    rules: dict[str, list[str]] = {}
    for block in _hcl_blocks(parse_hcl(policy_text), "path"):
        if len(block[2]) != 1:
            raise ConfigSyntaxError("a path rule needs exactly one label")
        label = block[2][0]
        if label in rules:
            raise ConfigSyntaxError(f"path {label!r} is declared more than once")
        capabilities = _hcl_attribute(block[3], "capabilities")
        legacy = _hcl_attribute(block[3], "policy")
        if capabilities is not None and legacy is not None:
            raise ConfigSyntaxError(
                f"path {label!r} declares both capabilities and the legacy policy"
                " field; which one applies is not something this checker decides"
            )
        if capabilities is not None:
            if not isinstance(capabilities, list) or not all(
                isinstance(item, str) for item in capabilities
            ):
                raise ConfigSyntaxError(f"path {label!r}: capabilities is not a list of strings")
            rules[label] = list(capabilities)
        elif legacy is not None:
            if not isinstance(legacy, str):
                raise ConfigSyntaxError(f"path {label!r}: policy is not a string")
            rules[label] = [legacy]
        else:
            rules[label] = []
    return rules


def _rule_denies(capabilities: list[str]) -> bool:
    """Whether a rule's declared capabilities are a deny, and only a deny.

    `["deny"]` and the legacy `policy = "deny"` both arrive here as a
    one-item list. `["deny", "read"]` does not count: OpenBao may well treat
    it as a deny, but this checker verifies the declaration, not OpenBao's
    resolution of a contradictory one.
    """
    return capabilities == ["deny"]


# The role-data fields that name a token's policies. `token_policies` is the
# current field; `policies` is its deprecated alias, still accepted
# (https://openbao.org/docs/api/auth/kubernetes/). Each takes a comma-separated
# string or a list of strings. A request that sets both is refused rather than
# resolved: which one OpenBao honours when they disagree is a precedence rule
# this checker does not implement, and guessing it would be guessing which
# policy the External Secrets token actually carries.
ROLE_POLICY_FIELDS = ("token_policies", "policies")


class _NonWritingRequest(Exception):
    """An initialize request whose operation establishes nothing."""


def _writing_request_data(request: list[tuple], request_path: str) -> dict | None:
    """The `data` object of an initialize request that writes `request_path`.

    Raises _NonWritingRequest when the request does not write -- `read`,
    `delete`, a typo -- and returns None when it carries no data object. A
    request has to write for anything in its data to exist on the instance,
    so the same rule applies to the policy request and the role request.
    """
    operation = _hcl_attribute(request, "operation")
    if operation not in WRITING_INITIALIZE_OPERATIONS:
        raise _NonWritingRequest(
            f"the request for {request_path} has operation {operation!r},"
            " which does not write anything"
        )
    data = _hcl_attribute(request, "data")
    return data if isinstance(data, dict) else None


def _bound_policy_names(role_data: dict) -> list[str]:
    """The policy names a role request literally binds.

    Exactly one of ROLE_POLICY_FIELDS, holding a string of comma-separated
    names or a list of string literals. Anything else -- both fields at once,
    a bare reference such as `var.x`, a list holding one -- is a
    ConfigSyntaxError: a value this reader cannot resolve to names is not
    stringified into one that happens to match.
    """
    present = [field for field in ROLE_POLICY_FIELDS if field in role_data]
    if len(present) > 1:
        raise ConfigSyntaxError(
            f"the role sets both {' and '.join(present)}; which binds its token"
            " is not something this checker decides"
        )
    if not present:
        return []
    value = role_data[present[0]]
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        # ("bare", "var.x"): a reference, which is not a string literal.
        raise ConfigSyntaxError(f"{present[0]} is a reference, not a string literal")
    else:
        raise ConfigSyntaxError(f"{present[0]} is neither a string nor a list")
    names: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ConfigSyntaxError(f"{present[0]} holds a value that is not a string literal")
        if item.strip():
            names.append(item.strip())
    return names


def _external_secrets_policy_denies_partition(config: str) -> list[str]:
    """Why the stanza would not establish a partition deny for External Secrets.

    Empty when, as far as the two declarations go, it would: the request that
    writes `sys/policies/acl/platform-secrets` declares `deny`, alone, on
    both partition paths, and the request that writes the External Secrets
    role literally binds `platform-secrets`. Only those two requests count: a
    deny in a comment, in another policy, or in a request that does not write
    counts for nothing here.

    What this does *not* establish, deliberately: the effective ACL of the
    token. OpenBao resolves a token's capabilities across every policy it
    holds, and a more specific path outranks a glob -- which is why the deny
    on `instances/*` outranks `platform/*`, and equally why an exact allow
    deeper than `instances/*` in any other policy the role binds would
    outrank the deny. This function reads two declarations; it is not an ACL
    evaluator, and a pass from it is not a statement that the running
    identity is denied. That is verified against the instance, in
    applications/core/external-secrets/README.md, "Updating the policy on an
    initialised instance".

    Fails closed. No request, a request that does not write, a policy or a
    binding that is not a literal, a document the reader cannot follow -- each
    is reported rather than passed over.
    """
    reasons: list[str] = []
    try:
        requests = _initialize_requests(config, EXTERNAL_SECRETS_POLICY_REQUEST_PATH)
        if not requests:
            return [
                f"no initialize request writes {EXTERNAL_SECRETS_POLICY_REQUEST_PATH},"
                " so self-init establishes no policy for External Secrets"
            ]
        for request in requests:
            try:
                data = _writing_request_data(request, EXTERNAL_SECRETS_POLICY_REQUEST_PATH)
            except _NonWritingRequest as why:
                reasons.append(f"{why}, so it establishes no policy")
                continue
            policy = data.get("policy") if data else None
            if not isinstance(policy, str):
                reasons.append(
                    f"the request for {EXTERNAL_SECRETS_POLICY_REQUEST_PATH} carries"
                    " no literal data.policy, so its rules cannot be verified"
                )
                continue
            rules = _policy_rules(policy)
            for denied_path in FABRIC_INSTANCE_DENIED_PATHS:
                if not _rule_denies(rules.get(denied_path, [])):
                    reasons.append(
                        f"policy {EXTERNAL_SECRETS_POLICY_NAME} does not deny"
                        f" {denied_path}"
                    )

        roles = _initialize_requests(config, EXTERNAL_SECRETS_ROLE_REQUEST_PATH)
        if not roles:
            reasons.append(
                f"no initialize request writes {EXTERNAL_SECRETS_ROLE_REQUEST_PATH},"
                f" so nothing binds External Secrets to {EXTERNAL_SECRETS_POLICY_NAME}"
            )
        for role in roles:
            try:
                role_data = _writing_request_data(role, EXTERNAL_SECRETS_ROLE_REQUEST_PATH)
            except _NonWritingRequest as why:
                reasons.append(f"{why}, so it binds no policy")
                continue
            if EXTERNAL_SECRETS_POLICY_NAME not in _bound_policy_names(role_data or {}):
                reasons.append(
                    f"the External Secrets role is not bound to"
                    f" {EXTERNAL_SECRETS_POLICY_NAME}, so that policy's deny does not"
                    " apply to its token"
                )
    except ConfigSyntaxError as error:
        reasons.append(f"the configuration could not be read ({error}), so the deny is unverified")
    return reasons


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

    # External Secrets reads the platform prefix, and SaaS Fabric's instance
    # partition sits beneath it. The policy its role is bound to must deny
    # that partition outright, on both the data and metadata paths, or the
    # store could project the control plane's own credentials into a
    # Kubernetes Secret.
    #
    # Verified in the policy text of the request that writes
    # `sys/policies/acl/platform-secrets`, read with a parser that knows what
    # a comment, a string and a heredoc are -- not by searching the whole
    # configuration, which a commented-out deny or a deny in an unrelated
    # policy would satisfy. This asserts what two declarations in the stanza
    # say: that the policy declares the deny and that the role binds the
    # policy. It does not compute the token's effective ACL across every
    # policy the role binds, and it says nothing about an instance
    # initialised before the deny existed. Both are verified against the
    # instance, not here (applications/core/external-secrets/README.md,
    # "Updating the policy on an initialised instance").
    for reason in _external_secrets_policy_denies_partition(config):
        fail(
            problems,
            f"{environment}: OpenBao self-init does not deny"
            f" {FABRIC_INSTANCE_PREFIX} to External Secrets -- {reason} -- so the"
            " platform store could read SaaS Fabric's own integration credentials",
        )


def check_master_realm_bootstraps_itself(render: Path, problems: list[str]) -> None:
    """The master realm's own instance resources must need no human either.

    ADR 0025 (application repository) applies check_openbao_bootstraps_itself's
    rule a second time: LucentRoot's master realm -- the gateway's confidential
    client, its secret, the fabric-operator role, each operator's grants --
    converges itself, using the Keycloak bootstrap administrator the platform
    already generates. The plan this replaced had a person create
    saas-fabric-gateway in Keycloak by hand and write its secret into OpenBao;
    if either half of that reappears, a human is back in the master realm's
    lifecycle exactly where the product owner ruled nobody may be.

    None of this is checkable by a schema, and all of it is silently absent
    when wrong: a render missing the generator or the convergence Job still
    produces a working-looking SecurityPolicy right up until the Secret it
    reads turns out to be one nobody created.
    """
    environment = DISPOSABLE_OPENBAO_ENVIRONMENT
    documents: list[tuple[Path, dict]] = [
        (path, document)
        for path in sorted((render / environment).rglob("*.yaml"))
        for document in load_all(path, problems)
    ]

    # Requires the generator wiring itself, not merely the absence of a store
    # reference -- an ExternalSecret with neither a secretStoreRef nor a
    # generatorRef is not a credential source at all, and a render that
    # dropped the dataFrom block by accident would otherwise pass this check
    # while producing an empty Secret.
    generator_found = False
    for _, document in documents:
        if document.get("kind") != "ExternalSecret":
            continue
        metadata = document.get("metadata", {})
        if metadata.get("name") != "saas-fabric-gateway-oidc":
            continue
        if metadata.get("namespace") != "operator-system":
            continue
        spec = _external_secret_spec(document)
        if (spec.get("secretStoreRef") or {}).get("name"):
            continue
        has_generator_ref = any(
            (entry.get("sourceRef") or {}).get("generatorRef")
            for entry in spec.get("dataFrom") or []
        )
        if has_generator_ref:
            generator_found = True

    if not generator_found:
        fail(
            problems,
            f"{environment}: no generated saas-fabric-gateway-oidc credential"
            " (applications/core/master-instance-credential) -- without it,"
            " a person has to create the saas-fabric-gateway client's secret"
            " by hand and put it somewhere this platform can read it",
        )

    # A normal Job, matched by name and namespace -- not a sync hook. An
    # earlier draft of this check looked for an
    # `argocd.argoproj.io/hook: Sync` annotation, which was wrong for the
    # same reason the Job itself stopped using one (see
    # applications/core/master-instance/base/job.yaml): a hook is excluded
    # from Argo CD's own health rollup, so matching on it here would have
    # certified a shape that provably cannot gate anything.
    job_found = False
    for _, document in documents:
        if document.get("kind") != "Job":
            continue
        metadata = document.get("metadata", {})
        if metadata.get("name") == "master-instance-converge" and metadata.get(
            "namespace"
        ) == "operator-system":
            job_found = True

    if not job_found:
        fail(
            problems,
            f"{environment}: no master-instance convergence Job"
            " (applications/core/master-instance) -- without it, a person has"
            " to create the master realm's saas-fabric-gateway and"
            " saas-fabric-console clients, the fabric-operator role, and each"
            " operator's role grant by hand in the Keycloak admin console",
        )

    # The hand path this replaces: an ExternalSecret in operator-system
    # reading a gateway-shaped client secret out of OpenBao. Matched broadly
    # -- any remote path under `platform/` that mentions "gateway" -- rather
    # than the one exact key this platform happens to use today, because the
    # thing this check exists to catch is a person writing to OpenBao and
    # pointing an ExternalSecret at it, and a slightly different key name is
    # still exactly that regression. The trade-off taken deliberately: a
    # legitimate future ExternalSecret reading some unrelated
    # platform/*gateway*-shaped OpenBao path in operator-system would also
    # fail here, wrongly -- accepted, because the cost of a false positive
    # (someone reads this message and renames their key, or narrows this
    # match) is far smaller than the cost of a false negative letting the
    # hand path back in unnoticed.
    for path, document in documents:
        if document.get("kind") != "ExternalSecret":
            continue
        if document.get("metadata", {}).get("namespace") != "operator-system":
            continue
        spec = _external_secret_spec(document)
        entries = list(spec.get("data") or []) + list(spec.get("dataFrom") or [])
        for entry in entries:
            for remote in _remote_paths(entry):
                normalised = remote.lstrip("/")
                if normalised.startswith("platform/") and "gateway" in normalised.lower():
                    fail(
                        problems,
                        f"{environment}/{path.name}: an ExternalSecret reads"
                        f" '{remote}' from OpenBao -- that is the hand path"
                        " this platform replaced: a person creating"
                        " saas-fabric-gateway in Keycloak and writing its"
                        " secret into OpenBao by hand. The secret must come"
                        " from master-instance-credential instead",
                    )


MASTER_INSTANCE_MODULE = Path("applications/core/master-instance/base/module/main.tf")

# A managed-resource reference: `<provider>_<type>.<name>`, not preceded by
# `data.` (that is a lookup, not a resource) or by any other identifier
# character. Every managed resource type carries its provider's prefix and an
# underscore, which `var.x`, `each.value` and `local.y` do not.
_MANAGED_RESOURCE_REFERENCE = re.compile(r"(?<![\w.])([a-z][a-z0-9]*_[a-z0-9_]+)\.([A-Za-z_][\w-]*)")

# The root of any traversal (`local.x`, `module.y.z`, `var.v`), and the roots
# a lookup may use. Anything else -- a local, a module output -- can carry a
# managed resource's value one step removed, which the direct-reference scan
# above cannot see, so it is refused rather than traced.
_REFERENCE_ROOT = re.compile(r"(?<![\w.])([A-Za-z_][\w-]*)\.(?=[A-Za-z_])")
_LOOKUP_REFERENCE_ROOTS = frozenset({"var", "each", "data", "count"})


def _hcl_without_comments(text: str) -> str:
    """`text` with `#`, `//` and `/* */` comments blanked, strings kept."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"' or c == "\n":
                in_string = False
            i += 1
        elif c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif c == "#" or text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _hcl_without_string_literals(text: str) -> str:
    """Comment-free `text` with the literal part of every string blanked.

    `${...}` interpolations are kept: a reference written inside one is still
    a reference. Everything else between quotes is text, and must not read as
    one -- `"keycloak_realm.master"` as a name is not a dependency.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    depth = 0
    while i < n:
        c = text[i]
        if not in_string:
            out.append(c)
            if c == '"':
                in_string = True
            i += 1
        elif depth:
            out.append(c)
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        elif text.startswith("${", i):
            out.append("${")
            depth = 1
            i += 2
        elif c == "\\" and i + 1 < n:
            out.append("  ")
            i += 2
        elif c == '"' or c == "\n":
            out.append(c)
            in_string = False
            i += 1
        else:
            out.append(" ")
            i += 1
    return "".join(out)


def _hcl_brace_body(text: str, open_brace: int) -> str:
    """The text between the brace at `open_brace` and its match, ignoring
    braces inside strings."""
    depth, i, in_string = 1, open_brace + 1, False
    while i < len(text) and depth:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return text[open_brace + 1:i - 1]


def _hcl_top_level_blocks(text: str, block_type: str) -> list[tuple[str, str, str]]:
    """(type, name, body) for every top-level `<block_type> "type" "name"` block
    in comment-free HCL.

    Braces inside strings -- `"${var.x}"` -- are not block structure, so the
    scan tracks strings rather than counting every brace it sees.
    """
    header = re.compile(rf'(?m)^{re.escape(block_type)}\s+"([^"]+)"\s+"([^"]+)"\s*\{{')
    return [
        (match.group(1), match.group(2), _hcl_brace_body(text, match.end() - 1))
        for match in header.finditer(text)
    ]


def master_instance_lookup_problems(module_text: str, where: str) -> list[str]:
    """Why a lookup in the master-instance module could be read at apply time.

    OpenTofu reads a data source during plan unless it depends on a managed
    resource with changes pending, in which case the read moves into apply --
    after every resource ahead of it in the graph has already been written.
    `keycloak_realm.master` has changes pending on every run that imports the
    realm or re-converges `frontendUrl`, so a lookup referencing it (or naming
    any managed resource in `depends_on`) turns "a missing operator account
    fails the convergence before anything is written" into "fails after the
    clients and role were already changed". A literal or a variable keeps it
    a plan-time read, unconditionally.

    Only roots in _LOOKUP_REFERENCE_ROOTS are accepted. A reference through a
    local is not a `depends_on` edge in OpenTofu 1.12.6 (`nodeDependencies`
    in internal/tofu/transform_reference.go keeps only direct managed-resource
    subjects), but it still defers the read whenever the value it carries is
    unknown at plan time, and a data block with custom conditions waits on
    every transitive dependency (`dependenciesHavePendingChanges` uses
    `n.Dependencies` then). Refusing the indirection is simpler and stricter
    than tracing it.
    """
    problems: list[str] = []
    for kind, name, block in _hcl_top_level_blocks(_hcl_without_comments(module_text), "data"):
        body = _hcl_without_string_literals(block)
        if re.search(r"(?m)^\s*depends_on\s*=", body):
            problems.append(
                f"{where}: data.{kind}.{name} declares depends_on -- a lookup"
                " with dependencies can be read at apply time, after the master"
                " realm was already changed, instead of failing the plan"
            )
        for reference in sorted({m.group(0) for m in _MANAGED_RESOURCE_REFERENCE.finditer(body)}):
            problems.append(
                f"{where}: data.{kind}.{name} references the managed resource"
                f" {reference} -- while that resource has changes pending,"
                " OpenTofu reads this lookup at apply time, so a missing"
                " operator account fails after the master realm was already"
                " changed instead of failing the plan. Use a literal or a"
                " variable (the realm id is its name, \"master\")"
            )
        managed_roots = {m.group(1) for m in _MANAGED_RESOURCE_REFERENCE.finditer(body)}
        for root in sorted({m.group(1) for m in _REFERENCE_ROOT.finditer(body)}):
            if root in _LOOKUP_REFERENCE_ROOTS or root in managed_roots:
                continue
            problems.append(
                f"{where}: data.{kind}.{name} references {root}.* -- a lookup"
                " may use only literals, var.*, each.*, count.* and data.*;"
                " a local or module output can carry a managed resource's"
                " value and move this read into apply"
            )
    return problems


def master_instance_grant_problems(module_text: str, where: str) -> list[str]:
    """Why removing a name from the master-instance roster could revoke roles.

    A `keycloak_user_roles` instance leaves the plan as a destroy when its key
    leaves `for_each`, and the provider's delete removes every role in
    `role_ids` from the user regardless of `exhaustive`. With master-realm
    `admin` among them, and the bootstrap administrator a valid roster entry,
    one edit to the roster could strip the convergence's own credential of
    its authority. `prevent_destroy = true` makes that plan fail instead.
    """
    problems: list[str] = []
    for kind, name, body in _hcl_top_level_blocks(_hcl_without_comments(module_text), "resource"):
        if kind != "keycloak_user_roles":
            continue
        guarded = any(
            re.search(r"(?m)^\s*prevent_destroy\s*=\s*true\s*$", _hcl_brace_body(body, match.end() - 1))
            for match in re.finditer(r"(?m)^\s*lifecycle\s*\{", body)
        )
        if not guarded:
            problems.append(
                f"{where}: resource.{kind}.{name} has no lifecycle prevent_destroy"
                " = true -- removing a name from the roster would destroy the"
                " grant, and the provider's delete revokes fabric-operator and"
                " master-realm admin from that account, exhaustive = false or not"
            )
    return problems


def check_master_instance_lookups_precede_apply(root: Path, problems: list[str]) -> None:
    """Section 13, the convergence's own failure modes: a lookup fails the
    plan rather than the apply, and a roster edit never revokes a grant."""
    module = root / MASTER_INSTANCE_MODULE
    if not module.is_file():
        fail(problems, f"{MASTER_INSTANCE_MODULE}: missing -- the master-instance convergence has no module")
        return
    text = module.read_text()
    for problem in master_instance_lookup_problems(text, str(MASTER_INSTANCE_MODULE)):
        fail(problems, problem)
    for problem in master_instance_grant_problems(text, str(MASTER_INSTANCE_MODULE)):
        fail(problems, problem)


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


def _image_reference(reference: str) -> tuple[str, str, str]:
    """Split `repository[:tag][@digest]` into its three parts.

    Kustomize emits all three when an `images:` entry carries both a `newTag`
    and a `digest`, which is the shape a promoted pin has.
    """
    name, _, digest = reference.partition("@")
    head, separator, tail = name.rpartition(":")
    # No colon, or a colon that belongs to a registry port rather than a tag.
    if not separator or "/" in tail:
        return name, "", digest
    return head, tail, digest


def _deployed_images(document, found: list[str]) -> None:
    """Every `image:` string anywhere in a rendered document.

    Recursive rather than reaching into `spec.template.spec.containers`,
    because an image reference also appears in init containers, in sidecars an
    upstream chart injects, and in custom resources whose shape this repository
    does not model. The callers filter by repository, so a key called `image`
    that is not one cannot produce a false positive.
    """
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "image" and isinstance(value, str):
                found.append(value)
            else:
                _deployed_images(value, found)
    elif isinstance(document, list):
        for entry in document:
            _deployed_images(entry, found)


def check_components_match_what_deploys(root: Path, render: Path, problems: list[str]) -> None:
    """`components.yaml` is the desired state, and the manifests must agree with it.

    `environments/<environment>/components.yaml` says what an environment is
    asked to run of a component, where it came from, and which files pin each
    image. SaaS Fabric's Platform Management writes it, and writes the files it
    declares -- so three things have to hold, and nothing else stops any of
    them going wrong quietly.

    **`pinnedIn` has to be true.** It is the only reason Fabric knows which
    files to write, and this repository owns its own layout: moving an overlay
    without updating the manifest would leave Fabric writing to a path that no
    longer pins anything, or refusing a promotion outright. So every declared
    path must exist and must actually carry an `images:` entry for that
    repository.

    **The rendered manifests have to match.** Checked against rendered output
    rather than against the overlays the manifest describes, because that is
    the artifact Argo CD applies and it is the only thing that settles the
    question.

    **A preview must not escape.** A version carrying a SemVer prerelease part
    may appear only in the environment whose manifest declares it. Without
    this, promoting a preview into production is a one-line edit that renders,
    validates and deploys.
    """
    for environment in ENVIRONMENTS:
        manifest = root / "environments" / environment / "components.yaml"
        if not manifest.is_file():
            continue

        documents = load_all(manifest, problems)
        if not documents:
            continue

        declared = documents[0]
        schema = declared.get("schemaVersion")
        if schema not in COMPONENTS_SCHEMA_VERSIONS:
            fail(problems, f"{manifest.relative_to(root)}: schemaVersion is not one of"
                           f" {', '.join(str(version) for version in COMPONENTS_SCHEMA_VERSIONS)}")
            continue

        roots = declared.get("managedRoots") or []
        if not roots:
            fail(problems, f"{manifest.relative_to(root)}: declares no managedRoots")
            continue

        # A root must be a real directory prefix ending in `/`. Empty is the
        # hole worth naming: `"".startswith("")` is true of every path, so one
        # blank entry would turn the whole guard off while still looking like a
        # list of roots. The trailing slash is what stops `applications` also
        # admitting `applications-something/`.
        if any(not str(managed).strip() or not str(managed).endswith("/") for managed in roots):
            fail(problems, f"{manifest.relative_to(root)}: a managedRoot is empty or does not end in '/'")
            continue

        # And it must be inside the repository. `/` ends in a slash and is not
        # empty, so it passes the rule above and would then admit every path on
        # the machine. Fabric refuses an absolute path outright, which is the
        # defence that matters -- but a contract that permits a root its only
        # consumer will never accept is a contract describing something that
        # cannot work.
        if any(str(managed).startswith(("/", "\\")) or ".." in Path(str(managed)).parts for managed in roots):
            fail(problems, f"{manifest.relative_to(root)}: a managedRoot is not inside the repository")
            continue

        for component, spec in (declared.get("components") or {}).items():
            version = str((spec.get("desired") or {}).get("version", ""))
            artifact = spec.get("artifact") or {}
            images = artifact.get("images") or {}

            # Schema 2 separates what a component is published as from where a
            # version is written. The image kinds render images, so only they
            # have rendered output to agree with.
            kind = artifact.get("type")
            if kind not in COMPONENT_ARTIFACT_TYPES:
                fail(problems, f"{manifest.relative_to(root)}: {component} declares no artifact this checker reads")
                continue

            if schema < COMPONENT_ARTIFACT_TYPES[kind]:
                # The version is what lets an older Fabric refuse the file by
                # naming it, rather than failing on a type it has never seen.
                fail(problems, f"{manifest.relative_to(root)}: {component} is '{kind}', which needs"
                               f" schemaVersion {COMPONENT_ARTIFACT_TYPES[kind]} or later")
                continue

            # A described component is found by the image its component
            # descriptor is attached to, so that role has to be one of its own.
            # Its descriptor's digest is deliberately not recorded here: a digest
            # typed into this file is a fact nothing proved, and Fabric names it
            # in the commit that advances the component instead.
            if kind == "described" and artifact.get("primary") not in images:
                fail(problems, f"{manifest.relative_to(root)}: {component} names primary"
                               f" {artifact.get('primary')!r}, which is not one of its images")
                continue

            if not version or not images or not artifact.get("sourceRevision"):
                fail(problems, f"{manifest.relative_to(root)}: {component} declares no version, images or sourceRevision")
                continue

            pinned = {}
            for role, image in images.items():
                repository, digest = image.get("repository"), image.get("digest")
                if not repository or not digest:
                    fail(problems, f"{manifest.relative_to(root)}: {component}/{role} names no repository or digest")
                    continue
                pinned[repository] = digest

            _check_pinned_in(root, manifest, component, spec, images, roots, problems)
            _check_rendered_images(root, render, manifest, environment, version, pinned, problems)


def _check_pinned_in(root: Path, manifest: Path, component: str, spec: dict, images: dict,
                     roots: list[str], problems: list[str]) -> None:
    """Every declared file must be one Fabric is allowed to write, and must really pin that image.

    Six rules, and they are the same six Fabric applies before it writes. Not
    because this file is untrusted -- it is desired state in the repository
    Fabric is writing to -- but because a mistake in it would otherwise make
    Fabric a confused deputy, editing a workflow file or a README on the
    strength of a trusted document asking it to.

    The last rule is the one worth having in both places. This proves the
    repository is coherent at the commit CI ran on; Fabric applies it again
    against whatever it actually read, which may be a state no CI has seen.
    """
    declared = spec.get("pinnedIn") or []
    named = f"{manifest.relative_to(root)}: {component}"

    if not declared:
        fail(problems, f"{named} declares no pinnedIn")
        return

    for pin in declared:
        # The renderer is the pin's kind, and each kind carries exactly the
        # fields it uses. An unknown one is refused rather than guessed at --
        # the same rule Fabric applies, where the renderer is an enum variant
        # and a name it does not know parses as nothing.
        renderer = pin.get("renderer")
        if renderer != "kustomize-image":
            fail(problems, f"{named} pinnedIn names renderer {renderer!r}, which this checker does not read")
            continue

        role = pin.get("image")
        if role not in images:
            fail(problems, f"{named} pinnedIn pins {role!r}, which {component} does not publish")
            continue

        repository = images[role]["repository"]
        relative = str(pin.get("path", ""))

        if relative.startswith("/") or ".." in Path(relative).parts:
            fail(problems, f"{named} pinnedIn names {relative}, which is not a plain repository-relative path")
            continue

        if not any(relative.startswith(managed) for managed in roots):
            fail(problems, f"{named} pinnedIn names {relative}, which is under no managedRoot")
            continue

        if Path(relative).suffix not in (".yaml", ".yml"):
            fail(problems, f"{named} pinnedIn names {relative}, which is not a manifest")
            continue

        path = root / relative
        if not path.is_file():
            fail(problems, f"{named} pinnedIn names {relative}, which does not exist")
            continue

        entries = [
            entry
            for document in load_all(path, problems)
            for entry in (document.get("images") or [])
            if isinstance(entry, dict)
        ]
        if not any(entry.get("name") == repository for entry in entries):
            fail(problems, f"{relative}: does not pin {repository}, which {manifest.relative_to(root)} says it does")


def _check_rendered_images(root: Path, render: Path, manifest: Path, environment: str, version: str,
                           pinned: dict, problems: list[str]) -> None:
    """What renders must be exactly what the manifest asks for, and only here."""
    seen: set[str] = set()

    for other in ENVIRONMENTS:
        for path in sorted((render / other).rglob("*.yaml")):
            references: list[str] = []
            for rendered in load_all(path, problems):
                _deployed_images(rendered, references)

            for reference in references:
                repository, tag, digest = _image_reference(reference)
                if repository not in pinned:
                    continue

                if other != environment:
                    # Another environment may run a different version of the
                    # same component -- but never a prerelease of it.
                    if "-" in tag:
                        fail(problems, f"{other}/{path.name}: {repository} is pinned to '{tag}', a preview."
                                       f" Only {environment} runs previews; see docs/releases.md")
                    continue

                seen.add(repository)
                want = f"{repository}:{version}@{pinned[repository]}"
                if reference != want:
                    fail(problems, f"{environment}/{path.name}: {repository} deploys '{reference}',"
                                   f" and {manifest.relative_to(root)} asks for '{want}'")

    for missing in sorted(set(pinned) - seen):
        fail(problems, f"{manifest.relative_to(root)}: asks for {missing}, which nothing in {environment} deploys")


def check_data_sources(root: Path, problems: list[str]) -> None:
    """`data-sources.yaml` is environment desired state for where a tenant's data may live.

    ADR 0023 part 1 (application repository) makes
    `environments/<environment>/data-sources.yaml` a second machine-managed
    file beside `components.yaml`: Fabric's Platform Management writes it, a
    human may edit it under break-glass, and each entry is spelled exactly as
    the runtime wire spells `data-sources.json`'s `DataSourceDocument` --
    parsed with the wire's own type rather than a third declaration of the
    same shape. So a field the wire would refuse is refused here too, and
    ADR 0006's rule -- a shared data source is the only kind that may carry a
    discriminator column, and it must -- is checked before anything is
    written, not discovered when the runtime starts.

    A missing file is not a problem: it reads as nothing declared yet, and the
    first declaration creates it (see environments/README.md).
    """
    for environment in ENVIRONMENTS:
        manifest = root / "environments" / environment / "data-sources.yaml"
        if not manifest.is_file():
            continue

        documents = load_all(manifest, problems)
        if not documents:
            continue

        declared = documents[0]
        named = manifest.relative_to(root)

        if declared.get("schemaVersion") != DATA_SOURCES_SCHEMA_VERSION:
            fail(problems, f"{named}: schemaVersion is not {DATA_SOURCES_SCHEMA_VERSION}")
            continue

        if declared.get("environment") != environment:
            fail(problems, f"{named}: environment is {declared.get('environment')!r}, not {environment!r}")
            continue

        unknown = set(declared) - DATA_SOURCE_ENVELOPE_KEYS
        if unknown:
            fail(problems, f"{named}: unknown key(s) {', '.join(sorted(unknown))}")
            continue

        entries = declared.get("dataSources")
        if not isinstance(entries, list):
            fail(problems, f"{named}: dataSources is not a list")
            continue

        seen_ids: set[str] = set()
        for entry in entries:
            _check_one_data_source(named, entry, seen_ids, problems)


def _check_one_data_source(named: Path, entry, seen_ids: set[str], problems: list[str]) -> None:
    """One `dataSources` entry: field shape, then the discriminator rule."""
    if not isinstance(entry, dict):
        fail(problems, f"{named}: a dataSources entry is not a mapping")
        return

    id_ = entry.get("id")
    label = id_ if isinstance(id_, str) and id_ else "<data source with no id>"

    def bad(message: str) -> None:
        fail(problems, f"{named}: {label}: {message}")

    unknown = set(entry) - DATA_SOURCE_ENTRY_KEYS
    if unknown:
        bad(f"declares unknown key(s) {', '.join(sorted(unknown))}")

    missing = DATA_SOURCE_REQUIRED_ENTRY_KEYS - set(entry)
    if missing:
        bad(f"is missing {', '.join(sorted(missing))}")
        return

    if not isinstance(id_, str) or not DATA_SOURCE_ID.match(id_):
        bad("id is not a valid data source identifier (an ASCII letter, then letters, digits, hyphens, or underscores)")
    elif id_ in seen_ids:
        bad("id is declared more than once")
    else:
        seen_ids.add(id_)

    revision = entry.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        bad("revision must be an integer of 1 or more")

    connector = entry.get("connector")
    if not isinstance(connector, str) or not connector:
        bad("connector must be a non-empty string")

    _check_connection(entry.get("connection"), bad)

    placement = entry.get("placement")
    if placement not in DATA_SOURCE_PLACEMENTS:
        bad(f"placement {placement!r} is not one of {', '.join(DATA_SOURCE_PLACEMENTS)}")

    _check_residency(entry.get("residency"), bad)
    _check_pool(entry.get("pool"), bad)
    _check_capabilities(entry.get("capabilities"), bad)
    _check_labels(entry.get("labels"), bad)

    discriminator = entry.get("discriminator")
    if placement == "shared":
        if discriminator is None:
            bad("placement is 'shared' but no discriminator is declared (ADR 0006)")
        else:
            _check_discriminator(discriminator, bad)
    elif discriminator is not None:
        bad(f"placement {placement!r} is not 'shared' but a discriminator is declared (ADR 0006)")


def _check_connection(connection, bad) -> None:
    """`{kind: named, name}` or `{kind: secret, reference}` -- never a value."""
    if not isinstance(connection, dict):
        bad("connection must be a mapping")
        return

    kind = connection.get("kind")
    allowed = CONNECTION_KEYS_BY_KIND.get(kind)
    if allowed is None:
        bad(f"connection.kind {kind!r} is not 'named' or 'secret'")
        return

    unknown = set(connection) - allowed
    if unknown:
        bad(f"connection declares unknown key(s) {', '.join(sorted(unknown))} -- "
            "a connection carries a name or a reference, never a value")
        return

    value_key = "name" if kind == "named" else "reference"
    value = connection.get(value_key)
    if not isinstance(value, str) or not value:
        bad(f"connection.{value_key} must be a non-empty string")


def _check_residency(residency, bad) -> None:
    if not isinstance(residency, dict):
        bad("residency must be a mapping")
        return

    unknown = set(residency) - RESIDENCY_KEYS
    if unknown:
        bad(f"residency declares unknown key(s) {', '.join(sorted(unknown))}")

    region = residency.get("region")
    if not isinstance(region, str) or not region:
        bad("residency.region must be a non-empty string")

    jurisdiction = residency.get("jurisdiction")
    if jurisdiction is not None and (not isinstance(jurisdiction, str) or not jurisdiction):
        bad("residency.jurisdiction must be a non-empty string when present")


def _check_pool(pool, bad) -> None:
    if pool is None:
        return
    if not isinstance(pool, dict):
        bad("pool must be a mapping")
        return

    unknown = set(pool) - set(POOL_KEYS)
    if unknown:
        bad(f"pool declares unknown key(s) {', '.join(sorted(unknown))}")

    for field in POOL_KEYS:
        if field not in pool:
            continue
        value = pool[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            bad(f"pool.{field} must be an integer of 1 or more")


def _check_capabilities(capabilities, bad) -> None:
    if capabilities is None:
        return
    if not isinstance(capabilities, dict):
        bad("capabilities must be a mapping")
        return

    unknown = set(capabilities) - set(CAPABILITIES_KEYS)
    if unknown:
        bad(f"capabilities declares unknown key(s) {', '.join(sorted(unknown))}")

    for field in CAPABILITIES_KEYS:
        if field in capabilities and not isinstance(capabilities[field], bool):
            bad(f"capabilities.{field} must be true or false")


def _check_labels(labels, bad) -> None:
    if labels is None:
        return
    if not isinstance(labels, dict):
        bad("labels must be a mapping")
        return

    for key, value in labels.items():
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value:
            bad(f"labels entry {key!r} must be a non-empty string key and value")


def _check_discriminator(discriminator, bad) -> None:
    if not isinstance(discriminator, dict):
        bad("discriminator must be a mapping")
        return

    unknown = set(discriminator) - DISCRIMINATOR_KEYS
    if unknown:
        bad(f"discriminator declares unknown key(s) {', '.join(sorted(unknown))}")

    column = discriminator.get("column")
    if not isinstance(column, str) or not column:
        bad("discriminator.column must be a non-empty string")


def check_placements(root: Path, problems: list[str]) -> None:
    """`placements.yaml` is the recorded fact of where a tenant's data lives.

    ADR 0023 part 2 (application repository) makes
    `environments/<environment>/placements.yaml` a third machine-managed file
    beside `components.yaml` and `data-sources.yaml`: Fabric writes it when a
    tenant is placed, and publication copies the record into the tenant
    binding rather than recomputing it (ADR 0007). A human may edit it under
    break-glass, and that edit is honoured as written -- this check validates
    the shape and the cross-file facts, never recomputes a placement.

    Every entry names a `data_source` declared in this environment's
    `data-sources.yaml`, and its `isolation` must agree with that data
    source's placement class: `discriminator` exactly when the data source is
    `shared`, and then with the same `column`; at most one non-discriminator
    placement per data source, since a `dedicated` data source (or any other
    non-`shared` class) is one tenant's; and no two discriminator placements
    on one data source sharing a `value`.

    A missing `placements.yaml` is not a problem. A missing sibling
    `data-sources.yaml` is, the moment `placements.yaml` names one: a
    placement with nothing declared to place it on is a dangling reference.
    """
    for environment in ENVIRONMENTS:
        manifest = root / "environments" / environment / "placements.yaml"
        if not manifest.is_file():
            continue

        documents = load_all(manifest, problems)
        if not documents:
            continue

        declared = documents[0]
        named = manifest.relative_to(root)

        if declared.get("schemaVersion") != PLACEMENTS_SCHEMA_VERSION:
            fail(problems, f"{named}: schemaVersion is not {PLACEMENTS_SCHEMA_VERSION}")
            continue

        if declared.get("environment") != environment:
            fail(problems, f"{named}: environment is {declared.get('environment')!r}, not {environment!r}")
            continue

        unknown = set(declared) - PLACEMENT_ENVELOPE_KEYS
        if unknown:
            fail(problems, f"{named}: unknown key(s) {', '.join(sorted(unknown))}")
            continue

        entries = declared.get("placements")
        if not isinstance(entries, list):
            fail(problems, f"{named}: placements is not a list")
            continue

        data_sources = _data_sources_by_id(root, environment, problems)

        seen_tenant_logical: set[tuple[str, str]] = set()
        data_source_state: dict[str, dict] = defaultdict(lambda: {"non_discriminator": 0, "values": set()})
        for entry in entries:
            _check_one_placement(named, entry, seen_tenant_logical, data_source_state, data_sources, problems)


def _data_sources_by_id(root: Path, environment: str, problems: list[str]):
    """The declared data sources for `environment`, by id, as (`placement`,
    `discriminator.column` or `None`).

    Returns `None` -- a sentinel, not an empty mapping -- when
    `data-sources.yaml` does not exist: an empty mapping would mean "declares
    nothing", which is a real state a checked file can be in, so a missing
    file must read differently. `check_data_sources` validates this same file
    on its own terms; this only reads the two facts a placement needs to be
    checked against it.
    """
    manifest = root / "environments" / environment / "data-sources.yaml"
    if not manifest.is_file():
        return None

    documents = load_all(manifest, problems)
    if not documents:
        return {}

    entries = documents[0].get("dataSources")
    if not isinstance(entries, list):
        return {}

    by_id: dict[str, tuple] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        id_ = entry.get("id")
        if not isinstance(id_, str) or not id_:
            continue
        discriminator = entry.get("discriminator")
        column = discriminator.get("column") if isinstance(discriminator, dict) else None
        by_id[id_] = (entry.get("placement"), column)
    return by_id


def _check_one_placement(named: Path, entry, seen: set, data_source_state: dict, data_sources, problems: list[str]) -> None:
    """One `placements` entry: field shape, then agreement with its data source."""
    if not isinstance(entry, dict):
        fail(problems, f"{named}: a placements entry is not a mapping")
        return

    tenant = entry.get("tenant")
    logical = entry.get("logical")
    if isinstance(tenant, str) and tenant and isinstance(logical, str) and logical:
        label = f"{tenant}/{logical}"
    else:
        label = "<placement with no tenant/logical>"

    def bad(message: str) -> None:
        fail(problems, f"{named}: {label}: {message}")

    unknown = set(entry) - PLACEMENT_ENTRY_KEYS
    if unknown:
        bad(f"declares unknown key(s) {', '.join(sorted(unknown))}")

    missing = PLACEMENT_REQUIRED_ENTRY_KEYS - set(entry)
    if missing:
        bad(f"is missing {', '.join(sorted(missing))}")
        return

    if "revision" in entry:
        revision = entry.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            bad("revision must be an integer of 1 or more")

    tenant_ok = isinstance(tenant, str) and bool(PLACEMENT_TENANT_ID.match(tenant))
    if not tenant_ok:
        bad("tenant is not a lowercase, DNS-label-like identifier")

    logical_ok = isinstance(logical, str) and bool(LOGICAL_DATA_SOURCE_NAME.match(logical))
    if not logical_ok:
        bad("logical is not a valid logical data source name")

    if tenant_ok and logical_ok:
        key = (tenant, logical)
        if key in seen:
            bad("tenant and logical are declared more than once")
        else:
            seen.add(key)

    data_source = entry.get("data_source")
    source_info = None
    if not isinstance(data_source, str) or not data_source:
        bad("data_source must be a non-empty string")
    elif data_sources is None:
        bad(f"data_source {data_source!r} is not declared -- this environment has no data-sources.yaml")
    elif data_source not in data_sources:
        bad(f"data_source {data_source!r} is not declared in this environment's data-sources.yaml")
    else:
        source_info = data_sources[data_source]

    placed_at = entry.get("placed_at")
    if not isinstance(placed_at, str) or not _is_rfc3339(placed_at):
        bad("placed_at is not an RFC 3339 timestamp")

    isolation = entry.get("isolation")
    kind = _check_isolation(isolation, bad)

    if source_info is None or kind is None:
        return

    placement_class, discriminator_column = source_info
    if placement_class == "shared":
        if kind != "discriminator":
            bad(f"data_source {data_source!r} is 'shared', which requires isolation.kind 'discriminator', not {kind!r}")
        elif isolation.get("column") != discriminator_column:
            bad(f"isolation.column {isolation.get('column')!r} does not match data_source {data_source!r}'s "
                f"discriminator column {discriminator_column!r}")
    elif kind == "discriminator":
        bad(f"isolation.kind is 'discriminator' but data_source {data_source!r}'s placement is "
            f"{placement_class!r}, not 'shared'")

    state = data_source_state[data_source]
    if kind == "discriminator":
        value = isolation.get("value")
        if isinstance(value, str) and value:
            if value in state["values"]:
                bad(f"another placement already uses discriminator value {value!r} on data_source {data_source!r}")
            else:
                state["values"].add(value)
    else:
        state["non_discriminator"] += 1
        if state["non_discriminator"] > 1:
            bad(f"data_source {data_source!r} already has a non-discriminator placement -- a "
                f"{placement_class!r} data source serves one tenant")


def _check_isolation(isolation, bad):
    """`{kind: database}` | `{kind: schema, schema}` | `{kind: discriminator, column, value}`.

    Returns the validated `kind`, or `None` when the shape itself is wrong --
    the caller cannot check agreement with a data source against an isolation
    that is not even well formed.
    """
    if not isinstance(isolation, dict):
        bad("isolation must be a mapping")
        return None

    kind = isolation.get("kind")
    allowed = ISOLATION_KEYS_BY_KIND.get(kind)
    if allowed is None:
        bad(f"isolation.kind {kind!r} is not 'database', 'schema', or 'discriminator'")
        return None

    unknown = set(isolation) - allowed
    if unknown:
        bad(f"isolation declares unknown key(s) {', '.join(sorted(unknown))} for kind {kind!r}")
        return None

    if kind == "schema":
        schema = isolation.get("schema")
        if not isinstance(schema, str) or not schema:
            bad("isolation.schema must be a non-empty string")
            return None
    elif kind == "discriminator":
        column = isolation.get("column")
        if not isinstance(column, str) or not column:
            bad("isolation.column must be a non-empty string")
            return None
        value = isolation.get("value")
        if not isinstance(value, str) or not value:
            bad("isolation.value must be a non-empty string")
            return None

    return kind


def _is_rfc3339(value: str) -> bool:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return True


RUNTIME_DOCUMENT_KEYS = ("tenants_path", "data_sources_path", "catalog_path")

KUBELET_DEFAULT_FILE_MODE = 0o644
UNSUPPORTED = "unsupported by this check"


def check_runtime_config_document_paths(render: Path, problems: list[str]) -> None:
    """The runtime's document paths are top-level keys on mounted ConfigMap volumes.

    A key written after a TOML table header belongs to that table. The runtime
    rejects unknown keys inside `[token]`, so `tenants_path` placed below it
    parses cleanly and still stops the process at start. For every container
    that names its config file in `FABRIC_CONFIG`, the ConfigMap supplying that
    file is read with the real TOML parser, and `tenants_path`,
    `data_sources_path` and `catalog_path` must each be a top-level string
    whose directory is exactly the `mountPath` of a whole-volume ConfigMap
    mount of that container.

    The kubelet's volume semantics are modelled for a small, explicit set of
    shapes and anything else is refused as "unsupported by this check" rather
    than guessed at, so a pass is never silent about an input it did not
    understand. Supported:

      * the config file comes from a whole-volume (no `subPath`/`subPathExpr`)
        mount of a `configMap` volume, or of a `projected` volume whose
        sources are `configMap` sources; sources apply in order and the last
        writer of a filename wins, with `items` selecting and renaming keys and
        an absent or empty `items` mounting every key;
      * a `downwardAPI`, `secret`, `serviceAccountToken` or
        `clusterTrustBundle` source in that projected volume is accepted only
        when its paths provably do not touch the config file; any other
        source kind, or a `secret` without `items`, is refused;
      * readability by the container's UID is derived from `runAsUser`,
        `runAsGroup`, `supplementalGroups` and `fsGroup` with the item or
        volume `mode` (default 0644); the kubelet gives files group `fsGroup`
        and adds group read, files are otherwise root-owned. An unset
        `runAsUser` or an unresolvable `runAsGroup` is refused;
      * each document directory is a whole-volume mount of a plain `configMap`
        volume without `items`; the publisher owns those ConfigMaps, so
        nothing finer can be verified and projections of them are refused.
    """
    for environment in ENVIRONMENTS:
        documents = [
            document
            for path in sorted((render / environment).rglob("*.yaml"))
            if path.name != "bootstrap.yaml"
            for document in load_all(path, problems)
        ]
        configmaps = {
            ((d.get("metadata") or {}).get("namespace"), (d.get("metadata") or {}).get("name")): d
            for d in documents
            if d.get("kind") == "ConfigMap"
        }
        for deployment in (d for d in documents if d.get("kind") == "Deployment"):
            metadata = deployment.get("metadata") or {}
            pod = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
            volumes = {v.get("name"): v for v in pod.get("volumes") or [] if isinstance(v, dict)}
            for container in pod.get("containers") or []:
                config_file = next(
                    (e.get("value") for e in container.get("env") or [] if e.get("name") == "FABRIC_CONFIG"),
                    None,
                )
                if not isinstance(config_file, str):
                    continue
                where = f"{environment}: Deployment/{metadata.get('name')} container {container.get('name')!r}"
                _check_runtime_container(
                    pod, container, volumes, configmaps, metadata.get("namespace"), config_file, where, problems
                )


def _check_runtime_container(
    pod: dict, container: dict, volumes: dict, configmaps: dict, namespace, config_file: str,
    where: str, problems: list[str],
) -> None:
    mounts = [m for m in container.get("volumeMounts") or [] if isinstance(m, dict)]
    if any(m.get("mountPath") == config_file for m in mounts):
        fail(problems, f"{where}: {config_file} is itself a mount point -- {UNSUPPORTED}")
        return

    config_dir = posixpath.dirname(config_file)
    name = posixpath.basename(config_file)
    supplied = None
    for mount in mounts:
        if mount.get("mountPath") != config_dir or _is_sub_path_mount(mount):
            continue
        volume = volumes.get(mount.get("name")) or {}
        projected = _project_file(volume, configmaps, namespace, name)
        if isinstance(projected, str):
            fail(problems, f"{where}: {config_file}: {projected} -- {UNSUPPORTED}")
            return
        if projected is not None:
            supplied = projected
    if supplied is None:
        fail(problems, f"{where}: FABRIC_CONFIG={config_file} is not supplied by a mounted ConfigMap in the render")
        return
    text, mode = supplied
    refusal, readable = _runtime_can_read(mode, pod, container)
    if refusal:
        fail(problems, f"{where}: {config_file}: {refusal} -- {UNSUPPORTED}")
        return
    if not readable:
        fail(problems, f"{where}: {config_file} (mode {mode:04o}) is not readable by the container's user")
        return
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        fail(problems, f"{where}: {config_file} is not valid TOML: {error}")
        return

    document_dirs: dict[str, str | None] = {}
    for mount in mounts:
        if _is_sub_path_mount(mount):
            continue
        volume = volumes.get(mount.get("name")) or {}
        if "configMap" not in volume and "projected" not in volume:
            continue
        reason = None
        if "projected" in volume or volume["configMap"].get("items"):
            reason = "documents must come from a plain configMap volume without items"
        else:
            document_mode = volume["configMap"].get("defaultMode")
            refusal, readable = _runtime_can_read(
                KUBELET_DEFAULT_FILE_MODE if document_mode is None else document_mode, pod, container
            )
            if refusal:
                reason = f"{refusal} -- {UNSUPPORTED}"
            elif not readable:
                reason = "its defaultMode leaves the documents unreadable by the container's user"
        document_dirs[mount.get("mountPath")] = reason
    for key in RUNTIME_DOCUMENT_KEYS:
        _check_runtime_document_path(parsed, key, document_dirs, where, problems)


def _is_sub_path_mount(mount: dict) -> bool:
    return "subPath" in mount or "subPathExpr" in mount


def _paths_overlap(written: object, target: str) -> bool:
    """Whether a volume-relative `written` path is, contains or sits inside `target`."""
    if not isinstance(written, str):
        return True
    written = posixpath.normpath(written)
    return written == target or target.startswith(written + "/") or written.startswith(target + "/")


def _project_file(volume: dict, configmaps: dict, namespace, target: str):
    """The final (content, mode) of `target` in a volume, None if absent, or a refusal string.

    Mirrors the kubelet: a projected volume applies its sources in order and a
    later write to the same filename replaces an earlier one, mode included.
    """
    if isinstance(volume.get("configMap"), dict):
        sources = [({"configMap": volume["configMap"]}, volume["configMap"].get("defaultMode"))]
    elif isinstance(volume.get("projected"), dict):
        projected = volume["projected"]
        sources = [(s, projected.get("defaultMode")) for s in projected.get("sources") or []]
    else:
        return None

    final = None
    for source, default_mode in sources:
        if not isinstance(source, dict):
            return "a projected source is not a mapping"
        kinds = sorted(source)
        if kinds == ["configMap"]:
            reference = source["configMap"] or {}
            data = (configmaps.get((namespace, reference.get("name"))) or {}).get("data") or {}
            items = reference.get("items")
            entries = (
                [(i.get("key"), i.get("path"), i.get("mode")) for i in items if isinstance(i, dict)]
                if items
                else [(key, key, None) for key in data]
            )
            for key, path, mode in entries:
                if not isinstance(path, str):
                    return "a configMap item has no path"
                if posixpath.normpath(path) != target:
                    if _paths_overlap(path, target):
                        return f"configMap item path {path!r} nests with the config file"
                    continue
                if key in data:
                    effective = default_mode if mode is None else mode
                    final = (data[key], KUBELET_DEFAULT_FILE_MODE if effective is None else effective)
        elif kinds == ["downwardAPI"]:
            if any(_paths_overlap(i.get("path") if isinstance(i, dict) else None, target)
                   for i in (source["downwardAPI"] or {}).get("items") or []):
                return "a later downwardAPI source writes to the config file"
        elif kinds == ["secret"]:
            items = (source["secret"] or {}).get("items")
            if not items:
                return "a secret source without items could write any filename"
            if any(_paths_overlap(i.get("path") if isinstance(i, dict) else None, target) for i in items):
                return "a secret source writes to the config file"
        elif kinds in (["serviceAccountToken"], ["clusterTrustBundle"]):
            if _paths_overlap((source[kinds[0]] or {}).get("path"), target):
                return f"a {kinds[0]} source writes to the config file"
        else:
            return f"projected source {'/'.join(kinds) or '(empty)'} is not modelled"
    return final


def _runtime_can_read(mode: int, pod: dict, container: dict) -> tuple[str | None, bool]:
    """(refusal, readable) for a ConfigMap file of `mode` as the kubelet materialises it.

    Files are owned by root. With `fsGroup` the kubelet sets the file's group to
    it and ORs in group read; without it the group is root's.
    """
    pod_context = pod.get("securityContext") or {}
    container_context = container.get("securityContext") or {}
    uid = container_context.get("runAsUser", pod_context.get("runAsUser"))
    gid = container_context.get("runAsGroup", pod_context.get("runAsGroup"))
    fs_group = pod_context.get("fsGroup")
    if not isinstance(uid, int):
        return "runAsUser is not set, so the container's user comes from the image", False
    if uid == 0:
        return None, True
    if fs_group is not None:
        mode |= 0o040
    file_group = 0 if fs_group is None else fs_group
    if mode & 0o004:
        return None, True
    if not mode & 0o040:
        return None, False
    groups = {fs_group} | set(pod_context.get("supplementalGroups") or [])
    if isinstance(gid, int):
        groups.add(gid)
    elif file_group not in groups:
        return "runAsGroup is not set, so group-read access cannot be decided", False
    return None, file_group in groups


def _check_runtime_document_path(
    parsed: dict, key: str, document_dirs: dict, where: str, problems: list[str]
) -> None:
    value = parsed.get(key)
    if value is None:
        nested = [name for name, table in parsed.items() if isinstance(table, dict) and key in table]
        if nested:
            fail(
                problems,
                f"{where}: {key} is under [{nested[0]}], not top level -- a key after a "
                "table header belongs to that table and the runtime refuses to start",
            )
        else:
            fail(problems, f"{where}: config has no top-level {key}")
    elif not isinstance(value, str):
        fail(problems, f"{where}: {key} must be a string path")
    elif posixpath.dirname(value) not in document_dirs:
        fail(
            problems,
            f"{where}: {key} = {value!r} is not inside a whole-volume ConfigMap mountPath of that container",
        )
    elif document_dirs[posixpath.dirname(value)] is not None:
        fail(problems, f"{where}: {key} = {value!r}: {document_dirs[posixpath.dirname(value)]}")


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
    check_no_runtime_publication_configmaps(render, problems)
    check_runtime_config_document_paths(render, problems)
    check_applications_match_their_project(render, problems)
    check_namespaced_resources_stay_in_project_destinations(render, problems)
    check_no_client_resources(render, problems)
    check_service_references(render, problems)
    check_exposure_planes(render, problems)
    check_admin_off_the_product_plane(render, problems)
    check_routes_attach(render, problems)
    check_control_plane_is_operator_only(render, problems)
    check_forwarded_proto_is_asserted_once(render, problems)
    check_argocd_runtime_configuration(render, problems)
    check_secret_store_is_bounded(render, problems)
    check_projects_permit_what_apps_deploy(render, problems)
    check_platform_secrets_stay_platform(render, problems)
    check_openbao_bootstraps_itself(render, problems)
    check_master_realm_bootstraps_itself(render, problems)
    check_master_instance_lookups_precede_apply(root, problems)
    check_seal_key_does_not_need_openbao(render, problems)
    check_collector_pipelines(render, problems)
    check_application_documentation(root, problems)
    check_service_capabilities(root, problems)
    check_control_plane_surfaces(root, render, problems)
    check_operator_only_services(root, render, problems)
    check_components_match_what_deploys(root, render, problems)
    check_data_sources(root, problems)
    check_placements(root, problems)

    if problems:
        print(f"{len(problems)} problem(s):\n")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print("All repository invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
