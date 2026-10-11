#!/usr/bin/env python3
"""The tenant runtime's inputs, as one bounded model: documents, composition,
the issuer registry handoff and the readiness gate on replicas.

    scripts/runtime_contract.py <scenario-directory> [...]

Four contracts, each from the D01 dispositions on saas-fabric #108:

  * **Runtime documents** (D01-1, D01-27). `contracts/runtime-documents.yaml`
    declares each required document's owner, version policy and mount path.
    A published document must sit in the ConfigMap the contract names, carry
    its owner's managed-by label, hold exactly its payload and manifest keys,
    state the contract version, and only roll forward: a lower manifest
    revision, a lower resource revision, or the same manifest revision over
    different bytes is refused. The tenants ConfigMap also carries
    `publication.json` (saas-fabric #136), the record naming each document's
    revision and sha256 in one generation. Documents that do not match it are
    an incomplete generation. Its reader floor must not exceed the runtime's
    reader contract, and the held record's publisher floor must not exceed
    the publisher's contract (D01-21). Its `resources` history (saas-fabric
    #138) must cover every published tenant binding and DataSource at no
    less than its revision, and never lowers or drops a held entry.
  * **Per-client composition** (D01-3). Platform composition per client or
    instance: its namespace, database, connector processes and the issuer its
    edge admits. It references Fabric's data sources and tenants by id and
    never restates them, and it never carries a credential. Every published
    data source has exactly one serving composition, and only a `shared` one
    may be bound by a tenant other than that composition's client.
  * **Versioned registry handoff** (D01-4). Fabric produces one generation of
    the issuer-to-tenant registry; platform composition applies it at the
    edge. Every composition must state the registry generation it applied,
    and a mismatch is refused. Fabric is never given a gateway object to
    write: the edge is platform-composed from the registry, not by Fabric.
  * **Readiness gating** (D01-18). Three gates -- documents, identity,
    connectors. Zero replicas is always a valid, explicit incomplete state;
    any replica above zero is refused while a gate is unmet, because scaling
    alone cannot supply an absent input.

Bounded in the same way as scripts/check.py: every input outside the shapes
modelled here is refused as "unsupported by this check", never guessed at.

A scenario directory holds:

  published.yaml         the fabric-runtime-* ConfigMaps as observed (may be
                         an empty list -- the awaiting state)
  held.yaml              the revisions and generations previously held
  issuer-registry.json   Fabric's registry generation (absent until produced)
  compositions/*.yaml    one platform composition per client or instance
  runtime.yaml           the runtime Deployment and its config ConfigMap
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPOSITORY = Path(__file__).resolve().parent.parent
CONTRACT = REPOSITORY / "contracts" / "runtime-documents.yaml"

UNSUPPORTED = "unsupported by this check"
MANAGED_BY = "app.kubernetes.io/managed-by"
ENVIRONMENTS = ("lucentroot", "production")
GATES = ("documents", "identity", "connectors")

CONTRACT_KEYS = {
    "schemaVersion", "publicationContractVersion", "manifestContractVersions", "namespace", "publicationRecord",
    "runtime", "documents",
}
PUBLICATION_RECORD_CONTRACT_KEYS = {"document", "key", "readerContract"}
PUBLICATION_RECORD_FILE = "publication.json"
PUBLICATION_RECORD_KEYS = {
    "contract_version", "minimum_reader_contract", "minimum_publisher_contract", "generation", "documents", "resources",
}
RESOURCE_HISTORY_KEYS = {"tenants", "data-sources"}
RECORDED_DOCUMENT_KEYS = {"revision", "sha256"}
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
CONTRACT_DOCUMENT_KEYS = {
    "document", "configMap", "owner", "shape", "revision", "payloadKey", "manifestKey", "configKey", "mountPath",
}
SHAPE_OF_DOCUMENT = {"tenants": "tenant-bindings", "data-sources": "data-sources", "catalog": "resource-catalog"}
REVISION_POLICIES = ("roll-forward",)
MANIFEST_KEYS = {"contract_version", "document", "revision"}
PLACEMENTS = ("shared", "dedicated", "high_availability", "regulated", "development", "ephemeral")

# Every pattern in this module is applied with `fullmatch`, never `match`: `$` also
# matches before a trailing newline, so `match` would accept "alpha\n" as a
# tenant id that Fabric's own parser refuses.
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,62}")
TENANT_ID = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
ISSUER = re.compile(r"https://[a-z0-9.-]+(:[0-9]{1,5})?/realms/(?P<realm>[A-Za-z0-9_-]+)")

# The widths Fabric reads these numbers into. A value one past the width parses
# here as a Python int but makes the whole document unreadable to the runtime,
# so it must be refused rather than counted as ready.
U64_MAX = 2**64 - 1  # BindingRevision, DocumentRevision, generations
U32_MAX = 2**32 - 1  # contract versions and floors
INT32_MAX = 2**31 - 1  # Deployment.spec.replicas

TENANT_BINDING_KEYS = {"tenant", "revision", "data", "configuration", "secrets", "features", "storage"}
TENANT_DATA_BINDING_KEYS = {"data_source", "isolation"}
ISOLATION_KEYS_BY_KIND = {
    "database": {"kind"},
    "schema": {"kind", "schema"},
    "discriminator": {"kind", "column", "value"},
}
DATA_SOURCE_KEYS = {
    "id", "revision", "connector", "connection", "placement", "residency", "pool", "capabilities", "labels",
}
DATA_SOURCE_REQUIRED_KEYS = {"id", "revision", "connector", "connection", "placement", "residency"}
RESOURCE_KEYS = {"data_source", "collection", "key_field", "operations", "queryable_fields"}
OPERATIONS = {"read", "list", "create", "update", "delete"}

HELD_KEYS = {"schemaVersion", "documents", "resources", "publication", "registry"}
HELD_PUBLICATION_KEYS = {"generation", "sha256", "minimumPublisherContract"}
HELD_RESOURCE_DOCUMENTS = ("tenants", "data-sources")
REGISTRY_KEYS = {"schemaVersion", "generation", "issuers"}
REGISTRATION_KEYS = {"issuer", "tenant"}

COMPOSITION_KEYS = {
    "schemaVersion", "client", "instance", "environment", "namespace", "identity", "edge", "database", "connectors",
}
COMPOSITION_IDENTITY_KEYS = {"realm", "issuer"}
COMPOSITION_EDGE_KEYS = {"registryGeneration"}
COMPOSITION_DATABASE_KEYS = {"kind", "cluster", "dataSources"}
COMPOSITION_CONNECTOR_KEYS = {"id", "kind", "service", "port", "dataSources"}
DATABASE_KINDS = ("cloudnative-pg",)
CONNECTOR_KINDS = ("ndc-postgres",)

# Key names that hold a credential rather than name one. A composition names
# Fabric's data sources and nothing else; it has no field that may hold one.
CREDENTIAL_KEY = re.compile(r"(password|passwd|secret|token|credential|private_?key|api_?key)", re.IGNORECASE)


@dataclass
class Evaluation:
    """What a scenario establishes: refusals, and the gates it leaves unmet."""

    problems: list[str] = field(default_factory=list)
    unmet: dict[str, list[str]] = field(default_factory=lambda: {gate: [] for gate in GATES})
    replicas: int | None = None


def _is_count(value, minimum: int = 1, maximum: int = U64_MAX) -> bool:
    """An int (never a bool) in [minimum, maximum] -- by default a positive u64."""
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_registry_digest(registry: dict) -> str:
    """The digest a held registry generation is compared by: key order and whitespace never count."""
    return _digest(json.dumps(registry, sort_keys=True, separators=(",", ":")))


# ---------------------------------------------------------------------------
# The contract itself
# ---------------------------------------------------------------------------

def load_contract(path: Path = CONTRACT) -> dict:
    return yaml.safe_load(path.read_text())


def contract_problems(contract) -> list[str]:
    """The contract file is itself refused unless it is exactly the shape this module reads."""
    problems: list[str] = []
    where = "contracts/runtime-documents.yaml"
    if not isinstance(contract, dict) or set(contract) != CONTRACT_KEYS:
        return [f"{where}: keys must be exactly {sorted(CONTRACT_KEYS)} -- {UNSUPPORTED}"]
    if contract["schemaVersion"] != 1:
        problems.append(f"{where}: schemaVersion {contract['schemaVersion']!r} -- {UNSUPPORTED}")
    if not _is_count(contract["publicationContractVersion"], maximum=U32_MAX):
        problems.append(f"{where}: publicationContractVersion must be a positive integer")
    manifests = contract["manifestContractVersions"]
    if (
        not isinstance(manifests, list)
        or not manifests
        or not all(_is_count(v, maximum=U32_MAX) for v in manifests)
        or manifests != sorted(set(manifests))
        or manifests[-1] != contract["publicationContractVersion"]
    ):
        problems.append(
            f"{where}: manifestContractVersions must be ascending distinct positive integers ending at "
            "publicationContractVersion"
        )
    if not isinstance(contract["namespace"], str) or not DNS_LABEL.fullmatch(contract["namespace"]):
        problems.append(f"{where}: namespace must be a DNS label")
    record = contract["publicationRecord"]
    if not isinstance(record, dict) or set(record) != PUBLICATION_RECORD_CONTRACT_KEYS:
        problems.append(f"{where}: publicationRecord must be exactly {sorted(PUBLICATION_RECORD_CONTRACT_KEYS)}")
    else:
        if record["document"] != "tenants" or record["key"] != PUBLICATION_RECORD_FILE:
            problems.append(f"{where}: publicationRecord must be {PUBLICATION_RECORD_FILE} in the tenants document -- {UNSUPPORTED}")
        if not _is_count(record["readerContract"], maximum=U32_MAX):
            problems.append(f"{where}: publicationRecord.readerContract must be a positive integer")
    runtime = contract["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {"deployment", "container"}:
        problems.append(f"{where}: runtime must name exactly a deployment and a container")
    documents = contract["documents"]
    if not isinstance(documents, list):
        return problems + [f"{where}: documents must be a list"]
    kinds = [d.get("document") for d in documents if isinstance(d, dict)]
    if sorted(kinds) != sorted(SHAPE_OF_DOCUMENT) or len(documents) != len(SHAPE_OF_DOCUMENT):
        problems.append(f"{where}: must declare exactly the documents {sorted(SHAPE_OF_DOCUMENT)}")
    mount_paths = set()
    for entry in documents:
        if not isinstance(entry, dict) or set(entry) != CONTRACT_DOCUMENT_KEYS:
            problems.append(f"{where}: a document entry must have exactly {sorted(CONTRACT_DOCUMENT_KEYS)}")
            continue
        name = entry["document"]
        if SHAPE_OF_DOCUMENT.get(name) != entry["shape"]:
            problems.append(f"{where}: {name}: shape {entry['shape']!r} -- {UNSUPPORTED}")
        if entry["revision"] not in REVISION_POLICIES:
            problems.append(f"{where}: {name}: revision policy {entry['revision']!r} -- {UNSUPPORTED}")
        if entry["payloadKey"] != f"{name}.json" or entry["manifestKey"] != f"{name}.manifest.json":
            problems.append(f"{where}: {name}: payload and manifest keys must be {name}.json and {name}.manifest.json")
        mount = entry["mountPath"]
        if not isinstance(mount, str) or not mount.startswith("/") or posixpath.normpath(mount) != mount:
            problems.append(f"{where}: {name}: mountPath must be a normalised absolute path")
        elif mount in mount_paths:
            problems.append(f"{where}: {name}: mountPath {mount} is shared -- one ConfigMap volume per directory")
        mount_paths.add(mount)
    return problems


def _contract_documents(contract: dict) -> dict[str, dict]:
    return {entry["document"]: entry for entry in contract["documents"]}


# ---------------------------------------------------------------------------
# Runtime Deployment: mounts, config keys and the replica gate
# ---------------------------------------------------------------------------

def _runtime_deployment(contract: dict, documents: list[dict]) -> dict | None:
    name = contract["runtime"]["deployment"]
    found = [
        d for d in documents
        if d.get("kind") == "Deployment" and (d.get("metadata") or {}).get("name") == name
    ]
    return found[0] if len(found) == 1 else None


def _runtime_container(contract: dict, deployment: dict) -> dict | None:
    pod = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
    found = [c for c in pod.get("containers") or [] if c.get("name") == contract["runtime"]["container"]]
    return found[0] if len(found) == 1 else None


def runtime_config(contract: dict, documents: list[dict]) -> tuple[dict | None, str | None]:
    """(parsed TOML, refusal) for the file the runtime container names in FABRIC_CONFIG."""
    deployment = _runtime_deployment(contract, documents)
    if deployment is None:
        return None, f"exactly one Deployment/{contract['runtime']['deployment']} is required"
    container = _runtime_container(contract, deployment)
    if container is None:
        return None, f"exactly one container {contract['runtime']['container']!r} is required"
    pod = deployment["spec"]["template"]["spec"]
    config_file = next(
        (e.get("value") for e in container.get("env") or [] if e.get("name") == "FABRIC_CONFIG"), None
    )
    if not isinstance(config_file, str):
        return None, f"FABRIC_CONFIG is not a literal path -- {UNSUPPORTED}"
    mounts = [
        m for m in container.get("volumeMounts") or []
        if m.get("mountPath") == posixpath.dirname(config_file) and "subPath" not in m and "subPathExpr" not in m
    ]
    volumes = {v.get("name"): v for v in pod.get("volumes") or []}
    if len(mounts) != 1 or not isinstance((volumes.get(mounts[0].get("name")) or {}).get("configMap"), dict):
        return None, f"{config_file} is not supplied by one whole-volume configMap mount -- {UNSUPPORTED}"
    config_map_name = volumes[mounts[0]["name"]]["configMap"].get("name")
    namespace = (deployment.get("metadata") or {}).get("namespace")
    sources = [
        d for d in documents
        if d.get("kind") == "ConfigMap"
        and (d.get("metadata") or {}).get("name") == config_map_name
        and (d.get("metadata") or {}).get("namespace") == namespace
    ]
    text = ((sources[0].get("data") or {}).get(posixpath.basename(config_file))) if len(sources) == 1 else None
    if not isinstance(text, str):
        return None, f"{config_file} is not a data key of exactly one ConfigMap {config_map_name!r}"
    try:
        return tomllib.loads(text), None
    except tomllib.TOMLDecodeError as error:
        return None, f"{config_file} is not valid TOML: {error}"


def runtime_mount_problems(contract: dict, documents: list[dict], where: str) -> list[str]:
    """Each required document reaches the runtime from its own ConfigMap, at the contract's mount path.

    The ConfigMap volume must name the contract's ConfigMap, be plain (no
    `items`, not `optional`, not projected), be mounted whole -- never through
    `subPath`, which pins the file the pod started with -- at exactly the
    contract's `mountPath`, and the runtime's own config key must point at the
    contract's payload key inside that directory.
    """
    problems: list[str] = []
    deployment = _runtime_deployment(contract, documents)
    if deployment is None:
        return [f"{where}: exactly one Deployment/{contract['runtime']['deployment']} is required"]
    container = _runtime_container(contract, deployment)
    if container is None:
        return [f"{where}: exactly one container {contract['runtime']['container']!r} is required"]
    pod = deployment["spec"]["template"]["spec"]
    volumes = pod.get("volumes") or []
    mounts = container.get("volumeMounts") or []
    config, refusal = runtime_config(contract, documents)
    if refusal:
        problems.append(f"{where}: {refusal}")
    for name, entry in _contract_documents(contract).items():
        volumes_for_document = [
            v for v in volumes
            if isinstance(v.get("configMap"), dict) and v["configMap"].get("name") == entry["configMap"]
        ]
        projected = [
            v for v in volumes
            if any(
                (source.get("configMap") or {}).get("name") == entry["configMap"]
                for source in (v.get("projected") or {}).get("sources") or []
            )
        ]
        if projected:
            problems.append(f"{where}: {name}: {entry['configMap']} reaches the runtime through a projected volume -- {UNSUPPORTED}")
            continue
        if len(volumes_for_document) != 1:
            problems.append(f"{where}: {name}: missing required document -- no single volume of ConfigMap {entry['configMap']}")
            continue
        volume = volumes_for_document[0]
        reference = volume["configMap"]
        if reference.get("items") is not None or reference.get("optional"):
            problems.append(
                f"{where}: {name}: ConfigMap {entry['configMap']} volume with items or optional -- {UNSUPPORTED}; "
                "an optional volume mounts empty when the document is absent"
            )
            continue
        document_mounts = [m for m in mounts if m.get("name") == volume.get("name")]
        if len(document_mounts) != 1:
            problems.append(f"{where}: {name}: ConfigMap {entry['configMap']} is not mounted exactly once in the runtime container")
            continue
        mount = document_mounts[0]
        if "subPath" in mount or "subPathExpr" in mount:
            problems.append(f"{where}: {name}: subPath mount pins the document the pod started with -- {UNSUPPORTED}")
            continue
        if mount.get("mountPath") != entry["mountPath"]:
            problems.append(
                f"{where}: {name}: mounted at {mount.get('mountPath')!r}, contract says {entry['mountPath']!r}"
            )
            continue
        if config is None:
            continue
        expected = f"{entry['mountPath']}/{entry['payloadKey']}"
        if config.get(entry["configKey"]) != expected:
            problems.append(
                f"{where}: {name}: {entry['configKey']} = {config.get(entry['configKey'])!r}, contract says {expected!r}"
            )
    return problems


def configured_connectors(config: dict) -> list[dict]:
    connectors = config.get("connectors") or []
    return connectors if isinstance(connectors, list) else []


def configured_issuers(config: dict) -> list[dict]:
    identity = config.get("identity") or {}
    issuers = identity.get("trusted_issuers") if isinstance(identity, dict) else None
    return issuers if isinstance(issuers, list) else []


def replicas_of(contract: dict, documents: list[dict]) -> int | None:
    deployment = _runtime_deployment(contract, documents)
    if deployment is None:
        return None
    # Kubernetes defaults an absent replica count to one.
    replicas = (deployment.get("spec") or {}).get("replicas", 1)
    return replicas if _is_count(replicas, minimum=0, maximum=INT32_MAX) else None


def static_replica_problems(contract: dict, documents: list[dict], where: str) -> list[str]:
    """What Git alone can prove about the readiness gate (D01-18).

    Published documents live only in the cluster, so a render cannot see them.
    What it can see is the runtime's own configuration: a runtime above zero
    replicas with no `[[connectors]]` or no `[identity].trusted_issuers` would
    refuse to start in any case, and raising the count would only trade a
    deliberate zero for a crash loop. Zero is always accepted.
    """
    replicas = replicas_of(contract, documents)
    if replicas is None:
        return [f"{where}: runtime replica count is not a literal non-negative integer -- {UNSUPPORTED}"]
    if replicas == 0:
        return []
    config, refusal = runtime_config(contract, documents)
    if refusal:
        return [f"{where}: replicas {replicas}, and the runtime config cannot be read: {refusal}"]
    missing = []
    if not configured_connectors(config):
        missing.append("no [[connectors]]")
    if not configured_issuers(config):
        missing.append("no [identity].trusted_issuers")
    if missing:
        return [
            f"{where}: replicas {replicas} with {' and '.join(missing)} -- scaling cannot supply absent "
            "inputs; keep replicas 0 until documents, identity and connectors are ready (D01-18)"
        ]
    return []


# ---------------------------------------------------------------------------
# Published documents
# ---------------------------------------------------------------------------

def _payload_problems(shape: str, payload, where: str) -> list[str]:
    if shape == "tenant-bindings":
        return _tenant_bindings_problems(payload, where)
    if shape == "data-sources":
        return _data_sources_problems(payload, where)
    return _catalog_problems(payload, where)


def _tenant_bindings_problems(payload, where: str) -> list[str]:
    if not isinstance(payload, list):
        return [f"{where}: tenants payload must be a JSON array -- {UNSUPPORTED}"]
    problems, seen = [], set()
    for binding in payload:
        if not isinstance(binding, dict) or not set(binding) <= TENANT_BINDING_KEYS or not {"tenant", "revision", "data"} <= set(binding):
            problems.append(f"{where}: tenant binding keys must be within {sorted(TENANT_BINDING_KEYS)} "
                            f"and include tenant, revision, data -- {UNSUPPORTED}")
            continue
        tenant = binding["tenant"]
        if not isinstance(tenant, str) or not TENANT_ID.fullmatch(tenant):
            problems.append(f"{where}: tenant {tenant!r} is not a tenant id")
        elif tenant in seen:
            problems.append(f"{where}: tenant {tenant!r} bound twice")
        seen.add(tenant)
        if not _is_count(binding["revision"]):
            problems.append(f"{where}: tenant {tenant!r} revision must be a positive integer")
        data = binding["data"]
        if not isinstance(data, dict) or not data:
            problems.append(f"{where}: tenant {tenant!r} data must be a non-empty object")
            continue
        for logical, entry in data.items():
            if not isinstance(entry, dict) or set(entry) != TENANT_DATA_BINDING_KEYS:
                problems.append(f"{where}: tenant {tenant!r} data {logical!r} keys must be {sorted(TENANT_DATA_BINDING_KEYS)} -- {UNSUPPORTED}")
                continue
            if not IDENTIFIER.fullmatch(logical) or not isinstance(entry["data_source"], str) or not IDENTIFIER.fullmatch(entry["data_source"]):
                problems.append(f"{where}: tenant {tenant!r} data {logical!r} -> {entry['data_source']!r} is not an identifier pair")
                continue
            isolation = entry["isolation"]
            kind = isolation.get("kind") if isinstance(isolation, dict) else None
            if kind not in ISOLATION_KEYS_BY_KIND or set(isolation) != ISOLATION_KEYS_BY_KIND[kind]:
                problems.append(f"{where}: tenant {tenant!r} data {logical!r} isolation -- {UNSUPPORTED}")
    return problems


def _data_sources_problems(payload, where: str) -> list[str]:
    if not isinstance(payload, list):
        return [f"{where}: data-sources payload must be a JSON array -- {UNSUPPORTED}"]
    problems, seen = [], set()
    for source in payload:
        if not isinstance(source, dict) or not DATA_SOURCE_REQUIRED_KEYS <= set(source) <= DATA_SOURCE_KEYS:
            problems.append(f"{where}: data source keys must be within {sorted(DATA_SOURCE_KEYS)} "
                            f"and include {sorted(DATA_SOURCE_REQUIRED_KEYS)} -- {UNSUPPORTED}")
            continue
        identifier = source["id"]
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            problems.append(f"{where}: data source id {identifier!r} is not an identifier")
        elif identifier in seen:
            problems.append(f"{where}: data source {identifier!r} declared twice")
        seen.add(identifier)
        if not _is_count(source["revision"]):
            problems.append(f"{where}: data source {identifier!r} revision must be a positive integer")
        if not isinstance(source["connector"], str) or not IDENTIFIER.fullmatch(source["connector"]):
            problems.append(f"{where}: data source {identifier!r} connector is not an identifier")
        if source["placement"] not in PLACEMENTS:
            problems.append(f"{where}: data source {identifier!r} placement {source['placement']!r} -- {UNSUPPORTED}")
        connection = source["connection"]
        if not isinstance(connection, dict) or connection.get("kind") not in ("default", "named", "secret"):
            problems.append(f"{where}: data source {identifier!r} connection -- {UNSUPPORTED}")
    return problems


def _catalog_problems(payload, where: str) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{where}: catalog payload must be a JSON object -- {UNSUPPORTED}"]
    if not payload:
        return [f"{where}: an empty catalogue is refused, as the publisher refuses it"]
    problems = []
    for name, resource in payload.items():
        if not isinstance(resource, dict) or not {"data_source", "collection"} <= set(resource) <= RESOURCE_KEYS:
            problems.append(f"{where}: resource {name!r} keys must be within {sorted(RESOURCE_KEYS)} "
                            f"and include data_source, collection -- {UNSUPPORTED}")
            continue
        operations = resource.get("operations", ["read", "list"])
        if not isinstance(operations, list) or not all(isinstance(o, str) and o in OPERATIONS for o in operations):
            problems.append(f"{where}: resource {name!r} operations -- {UNSUPPORTED}")
    return problems


def _manifest_problems(contract: dict, name: str, text, where: str) -> tuple[list[str], int | None]:
    try:
        manifest = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return [f"{where}: manifest is not JSON -- {UNSUPPORTED}"], None
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        return [f"{where}: manifest keys must be exactly {sorted(MANIFEST_KEYS)} -- {UNSUPPORTED}"], None
    problems = []
    if not _is_count(manifest["contract_version"], maximum=U32_MAX) or manifest["contract_version"] not in contract["manifestContractVersions"]:
        problems.append(
            f"{where}: manifest contract_version {manifest['contract_version']!r}, contract accepts "
            f"{contract['manifestContractVersions']} -- {UNSUPPORTED}"
        )
    if manifest["document"] != name:
        problems.append(f"{where}: manifest describes {manifest['document']!r}, not {name!r}")
    if not _is_count(manifest["revision"]):
        problems.append(f"{where}: manifest revision must be a positive integer")
        return problems, None
    return problems, manifest["revision"]


def held_problems(held) -> list[str]:
    where = "held.yaml"
    if not isinstance(held, dict) or set(held) != HELD_KEYS or held.get("schemaVersion") != 1:
        return [f"{where}: keys must be exactly {sorted(HELD_KEYS)} at schemaVersion 1 -- {UNSUPPORTED}"]
    problems = []
    documents = held["documents"]
    if not isinstance(documents, dict) or not set(documents) <= set(SHAPE_OF_DOCUMENT):
        problems.append(f"{where}: documents must map only {sorted(SHAPE_OF_DOCUMENT)}")
    else:
        for name, record in documents.items():
            if not isinstance(record, dict) or set(record) != {"revision", "sha256"} or not _is_count(record["revision"]):
                problems.append(f"{where}: documents.{name} must be a revision and a sha256")
    resources = held["resources"]
    if not isinstance(resources, dict) or set(resources) != set(HELD_RESOURCE_DOCUMENTS):
        problems.append(f"{where}: resources must map exactly {list(HELD_RESOURCE_DOCUMENTS)}")
    else:
        for name, revisions in resources.items():
            if not isinstance(revisions, dict) or not all(_is_count(v) for v in revisions.values()):
                problems.append(f"{where}: resources.{name} must map ids to positive revisions")
    publication = held["publication"]
    if publication is not None and (
        not isinstance(publication, dict)
        or set(publication) != HELD_PUBLICATION_KEYS
        or not _is_count(publication["generation"])
        or not _is_count(publication["minimumPublisherContract"], maximum=U32_MAX)
    ):
        problems.append(f"{where}: publication must be null or a generation, a sha256 and a minimumPublisherContract")
    registry = held["registry"]
    if registry is not None and (
        not isinstance(registry, dict) or set(registry) != {"generation", "sha256"} or not _is_count(registry["generation"])
    ):
        problems.append(f"{where}: registry must be null or a generation and a sha256")
    return problems


def published_document_problems(contract: dict, configmaps, held: dict) -> tuple[list[str], dict[str, object], list[str]]:
    """(refusals, payloads of the documents that are present and valid, documents absent).

    An absent document is not a refusal here: zero replicas over absent
    documents is the explicit awaiting state. `evaluate` turns absence into a
    refusal only when replicas are above zero.
    """
    problems: list[str] = []
    payloads: dict[str, object] = {}
    declared = _contract_documents(contract)
    by_name = {entry["configMap"]: name for name, entry in declared.items()}
    if not isinstance(configmaps, list):
        return [f"published.yaml: must be a list of ConfigMaps -- {UNSUPPORTED}"], {}, sorted(declared)
    seen: set[str] = set()
    texts: dict[str, object] = {}
    revisions: dict[str, int | None] = {}
    record_text = None
    for configmap in configmaps:
        if not isinstance(configmap, dict) or configmap.get("kind") != "ConfigMap":
            problems.append(f"published.yaml: only ConfigMaps are modelled -- {UNSUPPORTED}")
            continue
        metadata = configmap.get("metadata") or {}
        object_name = metadata.get("name")
        if object_name not in by_name:
            problems.append(f"published.yaml: ConfigMap {object_name!r} is not a document the contract declares -- {UNSUPPORTED}")
            continue
        name = by_name[object_name]
        entry = declared[name]
        where = f"published {object_name}"
        if object_name in seen:
            problems.append(f"{where}: published twice")
            continue
        seen.add(object_name)
        document_problems: list[str] = []
        if metadata.get("namespace") != contract["namespace"]:
            document_problems.append(f"{where}: namespace {metadata.get('namespace')!r}, contract says {contract['namespace']!r}")
        owner = (metadata.get("labels") or {}).get(MANAGED_BY)
        if owner != entry["owner"]:
            document_problems.append(
                f"{where}: {MANAGED_BY} is {owner!r}, contract owner is {entry['owner']!r} -- "
                "a document another writer owns can be reverted under the publisher"
            )
        if configmap.get("binaryData"):
            document_problems.append(f"{where}: binaryData -- {UNSUPPORTED}")
        data = configmap.get("data") or {}
        expected_keys = {entry["payloadKey"], entry["manifestKey"]}
        if name == contract["publicationRecord"]["document"]:
            expected_keys.add(contract["publicationRecord"]["key"])
        if set(data) != expected_keys:
            document_problems.append(
                f"{where}: data keys must be exactly {', '.join(sorted(expected_keys))} -- {UNSUPPORTED}"
            )
            problems.extend(document_problems)
            continue
        if name == contract["publicationRecord"]["document"]:
            record_text = data[contract["publicationRecord"]["key"]]
        manifest_problems, revision = _manifest_problems(contract, name, data[entry["manifestKey"]], where)
        document_problems.extend(manifest_problems)
        text = data[entry["payloadKey"]]
        texts[name] = text
        revisions[name] = revision
        try:
            payload = json.loads(text) if isinstance(text, str) else None
        except json.JSONDecodeError:
            payload = None
        if payload is None:
            document_problems.append(f"{where}: {entry['payloadKey']} is not JSON -- {UNSUPPORTED}")
        else:
            document_problems.extend(_payload_problems(entry["shape"], payload, where))
        held_record = (held.get("documents") or {}).get(name)
        if revision is not None and held_record:
            if revision < held_record["revision"]:
                document_problems.append(
                    f"{where}: revision {revision} regresses held revision {held_record['revision']} -- "
                    "documents roll forward only; earlier content is a new revision (D01-1)"
                )
            elif revision == held_record["revision"] and isinstance(text, str) and _digest(text) != held_record["sha256"]:
                document_problems.append(
                    f"{where}: revision {revision} is already held with different content -- "
                    "a changed document is a new revision, never a rewrite in place"
                )
        if payload is not None and name in HELD_RESOURCE_DOCUMENTS and isinstance(payload, list):
            key = "tenant" if name == "tenants" else "id"
            held_resources = (held.get("resources") or {}).get(name) or {}
            for resource in payload:
                if not isinstance(resource, dict):
                    continue
                before = held_resources.get(resource.get(key))
                current = resource.get("revision")
                if before is not None and _is_count(current) and current < before:
                    document_problems.append(
                        f"{where}: {resource.get(key)!r} revision {current} regresses held revision {before} -- "
                        "resource revisions roll forward only (D01-1)"
                    )
        problems.extend(document_problems)
        if not document_problems:
            payloads[name] = payload
    if "tenants" in payloads and "data-sources" in payloads:
        published_ids = {source["id"] for source in payloads["data-sources"]}
        for binding in payloads["tenants"]:
            for logical, entry in binding["data"].items():
                if entry["data_source"] not in published_ids:
                    problems.append(
                        f"published fabric-runtime-tenants: {binding['tenant']!r} binds {logical!r} to "
                        f"{entry['data_source']!r}, which fabric-runtime-data-sources does not declare"
                    )
    if record_text is not None:
        problems.extend(publication_record_problems(contract, record_text, texts, revisions, held))
    absent = sorted(name for name, entry in declared.items() if entry["configMap"] not in seen)
    return problems, payloads, absent


def publication_record_problems(contract: dict, text, texts: dict, revisions: dict, held: dict) -> list[str]:
    """The generation `publication.json` names is complete, compatible and rolls forward.

    Every document must be present with exactly the revision and the bytes
    the record names. Anything else is a set between two generations, or the
    remains of an interrupted publication, which the runtime keeps refusing
    while it serves its last complete generation -- so it is refused here.
    """
    where = f"published fabric-runtime-tenants {PUBLICATION_RECORD_FILE}"
    try:
        record = json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError:
        record = None
    if not isinstance(record, dict):
        return [f"{where}: not a JSON object -- {UNSUPPORTED}"]
    if set(record) != PUBLICATION_RECORD_KEYS:
        return [f"{where}: keys must be exactly {sorted(PUBLICATION_RECORD_KEYS)} -- {UNSUPPORTED}"]
    problems = []
    publisher = contract["publicationContractVersion"]
    reader = contract["publicationRecord"]["readerContract"]
    if not _is_count(record["contract_version"], maximum=U32_MAX) or record["contract_version"] > publisher:
        problems.append(f"{where}: contract_version {record['contract_version']!r}, contract says {publisher} -- {UNSUPPORTED}")
    elif record["contract_version"] < publisher:
        problems.append(
            f"{where}: contract_version {record['contract_version']}, contract says {publisher} -- the deployed "
            "publisher has not rewritten this record, so its rollback floor is not in place (D01-21)"
        )
    if not _is_count(record["minimum_reader_contract"], maximum=U32_MAX):
        problems.append(f"{where}: minimum_reader_contract must be a positive integer")
    elif record["minimum_reader_contract"] > reader:
        problems.append(
            f"{where}: minimum_reader_contract {record['minimum_reader_contract']} is above the runtime's reader "
            f"contract {reader} -- the runtime would refuse this generation and keep its last one"
        )
    if not _is_count(record["minimum_publisher_contract"], maximum=U32_MAX):
        problems.append(f"{where}: minimum_publisher_contract must be a positive integer")
    elif record["minimum_publisher_contract"] > publisher:
        problems.append(
            f"{where}: minimum_publisher_contract {record['minimum_publisher_contract']} is above the publisher "
            f"contract {publisher} that wrote it -- {UNSUPPORTED}"
        )
    elif record["minimum_publisher_contract"] != record["contract_version"]:
        problems.append(
            f"{where}: minimum_publisher_contract {record['minimum_publisher_contract']} differs from "
            f"contract_version {record['contract_version']!r} -- a publisher stamps its own contract as the floor"
        )
    generation = record["generation"]
    if not _is_count(generation):
        problems.append(f"{where}: generation must be a positive integer")
    documents = record["documents"]
    if not isinstance(documents, dict) or set(documents) != set(SHAPE_OF_DOCUMENT):
        return problems + [f"{where}: documents must name exactly {sorted(SHAPE_OF_DOCUMENT)} -- {UNSUPPORTED}"]
    problems.extend(_resource_history_problems(record["resources"], texts, held, where))
    for name in sorted(SHAPE_OF_DOCUMENT):
        recorded = documents[name]
        if (
            not isinstance(recorded, dict)
            or set(recorded) != RECORDED_DOCUMENT_KEYS
            or not _is_count(recorded["revision"])
            or not isinstance(recorded["sha256"], str)
            or not SHA256_HEX.fullmatch(recorded["sha256"])
        ):
            problems.append(f"{where}: documents.{name} must be a positive revision and a lowercase hex sha256 -- {UNSUPPORTED}")
            continue
        if name not in texts:
            problems.append(f"{where}: generation {generation} names {name}, which is not published -- incomplete generation")
            continue
        if revisions.get(name) != recorded["revision"]:
            problems.append(
                f"{where}: generation {generation} names {name} revision {recorded['revision']}, its manifest says "
                f"{revisions.get(name)} -- incomplete generation"
            )
        if not isinstance(texts[name], str) or _digest(texts[name]) != recorded["sha256"]:
            problems.append(
                f"{where}: generation {generation} names {name} bytes that are not the published ones -- "
                "incomplete generation; the runtime keeps its last complete one"
            )
    held_publication = held.get("publication")
    if held_publication:
        if held_publication["minimumPublisherContract"] > publisher:
            problems.append(
                f"{where}: the held record's rollback floor is publisher contract "
                f"{held_publication['minimumPublisherContract']}, above this publisher's {publisher} (D01-21)"
            )
        floor = record["minimum_publisher_contract"]
        if _is_count(floor, maximum=U32_MAX) and floor < held_publication["minimumPublisherContract"]:
            problems.append(
                f"{where}: minimum_publisher_contract {floor} lowers the held rollback floor "
                f"{held_publication['minimumPublisherContract']} -- the floor rolls forward only (D01-21)"
            )
        if _is_count(generation):
            if generation < held_publication["generation"]:
                problems.append(
                    f"{where}: generation {generation} regresses held generation {held_publication['generation']} -- "
                    "generations roll forward only"
                )
            elif generation == held_publication["generation"] and _digest(text) != held_publication["sha256"]:
                problems.append(f"{where}: generation {generation} is already held with a different record")
    return problems


def _resource_history_problems(history, texts: dict, held: dict, where: str) -> list[str]:
    """`resources` (saas-fabric #138): the highest revision each resource was ever published at.

    It must cover every tenant binding and DataSource published now at no
    less than its current revision, and it rolls forward against the held
    record's history (`held.yaml` `resources`): no entry is ever lowered or
    removed, so a resource that left its document cannot be redeclared
    below a revision a reader may still hold (D01-1).
    """
    if not isinstance(history, dict) or set(history) != RESOURCE_HISTORY_KEYS:
        return [f"{where}: resources must map exactly {sorted(RESOURCE_HISTORY_KEYS)} -- {UNSUPPORTED}"]
    problems = []
    pattern = {"tenants": TENANT_ID, "data-sources": IDENTIFIER}
    for name in sorted(RESOURCE_HISTORY_KEYS):
        entries = history[name]
        if not isinstance(entries, dict) or not all(
            isinstance(key, str) and pattern[name].fullmatch(key) and _is_count(value) for key, value in entries.items()
        ):
            problems.append(f"{where}: resources.{name} must map ids to positive revisions -- {UNSUPPORTED}")
    if problems:
        return problems
    key_of = {"tenants": "tenant", "data-sources": "id"}
    for name in sorted(RESOURCE_HISTORY_KEYS):
        try:
            published = json.loads(texts[name]) if isinstance(texts.get(name), str) else []
        except json.JSONDecodeError:
            published = []
        for resource in published if isinstance(published, list) else []:
            if not isinstance(resource, dict):
                continue
            identifier, current = resource.get(key_of[name]), resource.get("revision")
            if not _is_count(current):
                continue
            highest = history[name].get(identifier)
            if highest is None:
                problems.append(f"{where}: resources.{name} has no entry for published {identifier!r}")
            elif highest < current:
                problems.append(
                    f"{where}: resources.{name}[{identifier!r}] = {highest} is below its published revision {current}"
                )
        for identifier, before in sorted(((held.get("resources") or {}).get(name) or {}).items()):
            after = history[name].get(identifier)
            if after is None:
                problems.append(
                    f"{where}: resources.{name} drops {identifier!r}, held at {before} -- history entries are never removed"
                )
            elif after < before:
                problems.append(
                    f"{where}: resources.{name}[{identifier!r}] = {after} lowers the held {before} -- history rolls forward only"
                )
    return problems


# ---------------------------------------------------------------------------
# Issuer registry and per-client composition
# ---------------------------------------------------------------------------

def registry_problems(registry, held: dict) -> list[str]:
    where = "issuer-registry.json"
    if not isinstance(registry, dict) or set(registry) != REGISTRY_KEYS or registry.get("schemaVersion") != 1:
        return [f"{where}: keys must be exactly {sorted(REGISTRY_KEYS)} at schemaVersion 1 -- {UNSUPPORTED}"]
    problems = []
    generation = registry["generation"]
    if not _is_count(generation):
        problems.append(f"{where}: generation must be a positive integer")
    issuers = registry["issuers"]
    if not isinstance(issuers, list):
        return problems + [f"{where}: issuers must be a list -- {UNSUPPORTED}"]
    seen = set()
    for registration in issuers:
        if not isinstance(registration, dict) or set(registration) != REGISTRATION_KEYS:
            problems.append(f"{where}: a registration must be exactly {sorted(REGISTRATION_KEYS)} -- {UNSUPPORTED}")
            continue
        if not isinstance(registration["issuer"], str) or not ISSUER.fullmatch(registration["issuer"]):
            problems.append(f"{where}: issuer {registration['issuer']!r} is not https://<host>/realms/<realm>")
        if not isinstance(registration["tenant"], str) or not TENANT_ID.fullmatch(registration["tenant"]):
            problems.append(f"{where}: tenant {registration['tenant']!r} is not a tenant id")
        if registration["issuer"] in seen:
            problems.append(f"{where}: issuer {registration['issuer']!r} registered twice -- which wins would depend on order")
        seen.add(registration["issuer"])
    held_registry = held.get("registry")
    if held_registry and _is_count(generation):
        if generation < held_registry["generation"]:
            problems.append(
                f"{where}: generation {generation} regresses held generation {held_registry['generation']} -- "
                "the registry rolls forward only"
            )
        elif generation == held_registry["generation"] and canonical_registry_digest(registry) != held_registry["sha256"]:
            problems.append(f"{where}: generation {generation} is already held with different registrations")
    return problems


def _credential_keys(value, path: str) -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and CREDENTIAL_KEY.search(key):
                found.append(f"{path}.{key}")
            found.extend(_credential_keys(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_credential_keys(child, f"{path}[{index}]"))
    return found


def composition_problems(composition, where: str) -> list[str]:
    """A per-client platform composition, refused unless it is exactly the modelled shape.

    It names Fabric's data sources by id and the issuer its edge admits; it
    states no tenant binding, no data source definition, no registration and
    no credential. Those belong to Fabric's documents or to the secret store.
    """
    if not isinstance(composition, dict):
        return [f"{where}: must be a mapping -- {UNSUPPORTED}"]
    credentials = _credential_keys(composition, "")
    if credentials:
        return [f"{where}: credential-shaped key {', '.join(credentials)} -- a composition never carries a credential"]
    if set(composition) != COMPOSITION_KEYS or composition.get("schemaVersion") != 1:
        return [f"{where}: keys must be exactly {sorted(COMPOSITION_KEYS)} at schemaVersion 1 -- {UNSUPPORTED}"]
    problems = []
    client = composition["client"]
    if not isinstance(client, str) or not TENANT_ID.fullmatch(client):
        problems.append(f"{where}: client {client!r} is not a tenant id")
    if not isinstance(composition["instance"], str) or not DNS_LABEL.fullmatch(composition["instance"]):
        problems.append(f"{where}: instance must be a DNS label")
    if composition["environment"] not in ENVIRONMENTS:
        problems.append(f"{where}: environment {composition['environment']!r} -- {UNSUPPORTED}")
    if composition["namespace"] != f"client-{client}":
        problems.append(f"{where}: namespace must be client-{client}, the client's own namespace")
    identity = composition["identity"]
    if not isinstance(identity, dict) or set(identity) != COMPOSITION_IDENTITY_KEYS:
        problems.append(f"{where}: identity must be exactly {sorted(COMPOSITION_IDENTITY_KEYS)} -- {UNSUPPORTED}")
    else:
        match = ISSUER.fullmatch(identity["issuer"]) if isinstance(identity["issuer"], str) else None
        if not match or match.group("realm") != identity["realm"]:
            problems.append(f"{where}: issuer must be https://<host>/realms/{identity['realm']}")
    edge = composition["edge"]
    if not isinstance(edge, dict) or set(edge) != COMPOSITION_EDGE_KEYS or not _is_count(edge["registryGeneration"]):
        problems.append(f"{where}: edge must be exactly a positive registryGeneration -- {UNSUPPORTED}")
    database = composition["database"]
    backed: set[str] = set()
    if not isinstance(database, dict) or set(database) != COMPOSITION_DATABASE_KEYS:
        problems.append(f"{where}: database must be exactly {sorted(COMPOSITION_DATABASE_KEYS)} -- {UNSUPPORTED}")
    else:
        if database["kind"] not in DATABASE_KINDS:
            problems.append(f"{where}: database kind {database['kind']!r} -- {UNSUPPORTED}")
        if not isinstance(database["cluster"], str) or not DNS_LABEL.fullmatch(database["cluster"]):
            problems.append(f"{where}: database cluster must be a DNS label")
        if not isinstance(database["dataSources"], list) or not database["dataSources"]:
            problems.append(f"{where}: database must back at least one data source")
        else:
            backed = set(database["dataSources"])
    connectors = composition["connectors"]
    if not isinstance(connectors, list) or not connectors:
        return problems + [f"{where}: connectors must be a non-empty list"]
    for connector in connectors:
        if not isinstance(connector, dict) or set(connector) != COMPOSITION_CONNECTOR_KEYS:
            problems.append(f"{where}: a connector must be exactly {sorted(COMPOSITION_CONNECTOR_KEYS)} -- {UNSUPPORTED}")
            continue
        if not isinstance(connector["id"], str) or not IDENTIFIER.fullmatch(connector["id"]):
            problems.append(f"{where}: connector id {connector['id']!r} is not an identifier")
        if connector["kind"] not in CONNECTOR_KINDS:
            problems.append(f"{where}: connector kind {connector['kind']!r} -- {UNSUPPORTED}")
        if not isinstance(connector["service"], str) or not DNS_LABEL.fullmatch(connector["service"]):
            problems.append(f"{where}: connector service must be a DNS label")
        if not _is_count(connector["port"], maximum=65535):
            problems.append(f"{where}: connector port must be 1-65535")
        served = connector["dataSources"]
        if not isinstance(served, list) or not served:
            problems.append(f"{where}: connector {connector['id']!r} serves no data source")
        elif not set(served) <= backed:
            problems.append(
                f"{where}: connector {connector['id']!r} serves {sorted(set(served) - backed)}, "
                "which this composition's database does not back"
            )
    return problems


def connector_endpoint(composition: dict, connector: dict) -> str:
    return f"http://{connector['service']}.{composition['namespace']}.svc.cluster.local:{connector['port']}"


# ---------------------------------------------------------------------------
# One scenario, all four contracts together
# ---------------------------------------------------------------------------

def _load(path: Path, problems: list[str]):
    try:
        text = path.read_text()
        return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as error:
        problems.append(f"{path.name}: unreadable: {error}")
        return None


def load_scenario(directory: Path) -> dict:
    problems: list[str] = []
    registry_path = directory / "issuer-registry.json"
    scenario = {
        "published": _load(directory / "published.yaml", problems),
        "held": _load(directory / "held.yaml", problems),
        "registry": _load(registry_path, problems) if registry_path.exists() else None,
        "compositions": {
            path.name: _load(path, problems) for path in sorted((directory / "compositions").glob("*.yaml"))
        },
        "runtime": [],
        "load_problems": problems,
    }
    runtime_path = directory / "runtime.yaml"
    try:
        scenario["runtime"] = [d for d in yaml.safe_load_all(runtime_path.read_text()) if isinstance(d, dict)]
    except (OSError, yaml.YAMLError) as error:
        problems.append(f"runtime.yaml: unreadable: {error}")
    return scenario


def evaluate(contract: dict, scenario: dict) -> Evaluation:
    result = Evaluation()
    problems, unmet = result.problems, result.unmet
    problems.extend(scenario["load_problems"])
    problems.extend(contract_problems(contract))
    if problems:
        return result

    held = scenario["held"]
    problems.extend(held_problems(held))
    if problems:
        return result

    runtime = scenario["runtime"]
    problems.extend(runtime_mount_problems(contract, runtime, "runtime.yaml"))
    result.replicas = replicas_of(contract, runtime)
    config, refusal = runtime_config(contract, runtime)
    config = config or {}

    document_problems, payloads, absent = published_document_problems(contract, scenario["published"], held)
    problems.extend(document_problems)
    for name in absent:
        unmet["documents"].append(f"missing required document {name} ({_contract_documents(contract)[name]['configMap']})")
    if document_problems:
        unmet["documents"].append("a published document is refused")

    registry = scenario["registry"]
    registrations: dict[str, str] | None = None
    if registry is None:
        unmet["identity"].append("Fabric has not produced an issuer registry generation")
    else:
        refused = registry_problems(registry, held)
        problems.extend(refused)
        if refused:
            unmet["identity"].append("the issuer registry is refused")
        else:
            registrations = {r["issuer"]: r["tenant"] for r in registry["issuers"]}
            if not registrations:
                unmet["identity"].append("the issuer registry names no issuer")

    compositions = {}
    for name, composition in scenario["compositions"].items():
        refused = composition_problems(composition, f"compositions/{name}")
        problems.extend(refused)
        if refused:
            unmet["connectors"].append(f"compositions/{name} is refused")
        else:
            compositions[name] = composition

    _check_handoff(registry, registrations, compositions, payloads, config, result)
    _check_connectors(compositions, payloads, config, result)
    _check_bindings(compositions, payloads, result)

    if result.replicas is None:
        problems.append(f"runtime.yaml: runtime replica count is not a literal non-negative integer -- {UNSUPPORTED}")
    elif result.replicas > 0:
        for gate in GATES:
            for reason in unmet[gate]:
                problems.append(
                    f"runtime.yaml: replicas {result.replicas} while the {gate} gate is unmet: {reason} -- "
                    "scaling alone cannot supply it; replicas stay 0 until every gate holds (D01-18)"
                )
    return result


def _check_handoff(registry, registrations: dict | None, compositions: dict, payloads: dict, config: dict, result: Evaluation) -> None:
    """One canonical registry generation; the edge and the runtime each apply exactly it (D01-4)."""
    problems, unmet = result.problems, result.unmet
    if registrations is None:
        for name, composition in compositions.items():
            unmet["identity"].append(f"compositions/{name} admits {composition['identity']['issuer']} with no registry to apply")
        return
    generation = registry["generation"]
    edge_issuers: dict[str, str] = {}
    for name, composition in compositions.items():
        applied = composition["edge"]["registryGeneration"]
        if applied != generation:
            problems.append(
                f"compositions/{name}: edge applies registry generation {applied}, Fabric's registry is at "
                f"{generation} -- the edge and the runtime must apply the same generation (D01-4, ADR 0019 G4a)"
            )
        issuer = composition["identity"]["issuer"]
        if issuer in edge_issuers:
            problems.append(f"compositions/{name}: issuer {issuer} is already composed by {edge_issuers[issuer]}")
        edge_issuers[issuer] = name
        if issuer not in registrations:
            problems.append(f"compositions/{name}: edge admits {issuer}, which registry generation {generation} does not register")
        elif registrations[issuer] != composition["client"]:
            problems.append(
                f"compositions/{name}: client {composition['client']!r} admits {issuer}, which the registry binds "
                f"to tenant {registrations[issuer]!r}"
            )
    for issuer, tenant in sorted(registrations.items()):
        if issuer not in edge_issuers:
            unmet["identity"].append(f"no composition applies {issuer} (tenant {tenant}) at the edge")
    if "tenants" in payloads:
        bound = {binding["tenant"] for binding in payloads["tenants"]}
        for issuer, tenant in sorted(registrations.items()):
            if tenant not in bound:
                problems.append(
                    f"issuer-registry.json: {issuer} names tenant {tenant!r}, which fabric-runtime-tenants does not "
                    "bind -- the registry and the tenants document must come from one tenant list"
                )
    configured = configured_issuers(config)
    runtime_registrations = {
        entry.get("issuer"): entry.get("tenant") for entry in configured if isinstance(entry, dict)
    }
    if not configured:
        unmet["identity"].append("the runtime config has no [identity].trusted_issuers")
    elif runtime_registrations != registrations or len(runtime_registrations) != len(configured):
        problems.append(
            "runtime.yaml: [identity].trusted_issuers differs from issuer registry generation "
            f"{generation} -- the runtime and the edge must be generated from the same registry (ADR 0019 G4a)"
        )


def _check_connectors(compositions: dict, payloads: dict, config: dict, result: Evaluation) -> None:
    problems, unmet = result.problems, result.unmet
    composed: dict[str, tuple[str, dict, dict]] = {}
    for name, composition in compositions.items():
        for connector in composition["connectors"]:
            if connector["id"] in composed:
                problems.append(f"compositions/{name}: connector {connector['id']!r} is already composed by {composed[connector['id']][0]}")
                continue
            composed[connector["id"]] = (name, composition, connector)
    configured = {}
    for entry in configured_connectors(config):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            problems.append(f"runtime.yaml: a [[connectors]] entry without a string id -- {UNSUPPORTED}")
            continue
        configured[entry["id"]] = entry
    if not configured:
        unmet["connectors"].append("the runtime config has no [[connectors]]")
    for identifier, entry in sorted(configured.items()):
        if identifier not in composed:
            unmet["connectors"].append(f"connector {identifier!r} is configured but no composition deploys it")
            continue
        name, composition, connector = composed[identifier]
        endpoint = connector_endpoint(composition, connector)
        if entry.get("endpoint") != endpoint:
            problems.append(
                f"runtime.yaml: connector {identifier!r} endpoint {entry.get('endpoint')!r}, "
                f"compositions/{name} deploys it at {endpoint!r}"
            )
    published = {source["id"]: source for source in payloads.get("data-sources") or []}
    for identifier, (name, _, connector) in sorted(composed.items()):
        if identifier not in configured:
            unmet["connectors"].append(f"connector {identifier!r} is composed but not in the runtime's [[connectors]]")
        if "data-sources" not in payloads:
            continue
        for data_source in connector["dataSources"]:
            if data_source not in published:
                unmet["connectors"].append(f"compositions/{name}: data source {data_source!r} is not yet published")
            elif published[data_source]["connector"] != identifier:
                problems.append(
                    f"compositions/{name}: connector {identifier!r} serves {data_source!r}, which Fabric publishes "
                    f"against connector {published[data_source]['connector']!r}"
                )
    for identifier, source in sorted(published.items()):
        if source["connector"] not in configured:
            unmet["connectors"].append(
                f"data source {identifier!r} needs connector {source['connector']!r}, which the runtime does not configure"
            )


def _serving_clients(compositions: dict) -> dict[str, list[tuple[str, str, str]]]:
    """Data source id -> every (composition file, client, connector id) that serves it."""
    serving: dict[str, list[tuple[str, str, str]]] = {}
    for name, composition in sorted(compositions.items()):
        for connector in composition["connectors"]:
            for data_source in connector["dataSources"]:
                serving.setdefault(data_source, []).append((name, composition["client"], connector["id"]))
    return serving


def _check_bindings(compositions: dict, payloads: dict, result: Evaluation) -> None:
    """Every published source has one serving composition, and only its client binds it unless it is shared.

    A `shared` data source is the intentional multi-tenant case: any tenant
    may bind it, through `discriminator` isolation with its own value, as
    scripts/check.py requires of placements.yaml. Every other placement class
    serves one tenant -- the client whose composition serves it -- so a
    binding from any other tenant is a cross-client placement and refused.
    """
    problems, unmet = result.problems, result.unmet
    if "data-sources" not in payloads:
        return
    serving = _serving_clients(compositions)
    owners: dict[str, str] = {}
    for identifier, source in sorted((s["id"], s) for s in payloads["data-sources"]):
        served = serving.get(identifier, [])
        if not served:
            unmet["connectors"].append(
                f"data source {identifier!r} is published but no composition serves it"
            )
            continue
        if len(served) > 1:
            problems.append(
                f"data source {identifier!r} is served by {', '.join(f'compositions/{n}' for n, _, _ in served)} "
                "-- one composition serves each data source"
            )
            continue
        name, client, connector = served[0]
        if source["connector"] != connector:
            continue
        if source["placement"] != "shared":
            owners[identifier] = client
    if "tenants" not in payloads:
        return
    published = {source["id"]: source for source in payloads["data-sources"]}
    dedicated_tenants: dict[str, set[str]] = {}
    discriminator_values: dict[str, set[str]] = {}
    for binding in payloads["tenants"]:
        tenant = binding["tenant"]
        for logical, entry in binding["data"].items():
            identifier = entry["data_source"]
            source = published.get(identifier)
            if source is None:
                continue
            kind = entry["isolation"]["kind"]
            where = f"published fabric-runtime-tenants: {tenant!r} {logical!r} -> {identifier!r}"
            if source["placement"] == "shared":
                if kind != "discriminator":
                    problems.append(f"{where}: a 'shared' data source requires discriminator isolation, not {kind!r}")
                    continue
                value = entry["isolation"]["value"]
                if value in discriminator_values.setdefault(identifier, set()):
                    problems.append(f"{where}: discriminator value {value!r} is already bound on {identifier!r}")
                discriminator_values[identifier].add(value)
                continue
            if kind == "discriminator":
                problems.append(f"{where}: discriminator isolation on a {source['placement']!r} data source, not 'shared'")
                continue
            dedicated_tenants.setdefault(identifier, set()).add(tenant)
            owner = owners.get(identifier)
            if owner is not None and owner != tenant:
                problems.append(
                    f"{where}: cross-client placement -- {identifier!r} is {source['placement']!r} and served by "
                    f"client {owner!r}'s composition; only a 'shared' data source may serve another client"
                )
    for identifier, tenants in sorted(dedicated_tenants.items()):
        if len(tenants) > 1:
            problems.append(
                f"published fabric-runtime-tenants: {identifier!r} is {published[identifier]['placement']!r} and bound "
                f"by {sorted(tenants)} -- a non-shared data source serves one tenant"
            )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    contract = load_contract()
    failed = False
    for argument in argv:
        result = evaluate(contract, load_scenario(Path(argument)))
        print(f"{argument}: replicas {result.replicas}")
        for gate in GATES:
            state = "met" if not result.unmet[gate] else "unmet"
            print(f"  {gate}: {state}")
            for reason in result.unmet[gate]:
                print(f"    - {reason}")
        for problem in result.problems:
            print(f"  refused: {problem}")
        failed = failed or bool(result.problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
