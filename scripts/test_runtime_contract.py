#!/usr/bin/env python3
"""Regression tests for scripts/runtime_contract.py and the runtime document
invariant scripts/check.py takes from it.

    python3 -m unittest discover -s scripts -p 'test_*.py'

The scenarios under fixtures/runtime-readiness/ are synthetic: invented
tenants, a `.invalid` issuer host, no credential, no registration. Each test
loads one, changes exactly one thing, and states which refusal or unmet gate
that one change must produce. No network and no cluster.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check  # noqa: E402
import runtime_contract  # noqa: E402

REPOSITORY = SCRIPTS.parent
FIXTURES = REPOSITORY / "fixtures" / "runtime-readiness"
CONTRACT = runtime_contract.load_contract()


def scenario(name: str) -> dict:
    return copy.deepcopy(runtime_contract.load_scenario(FIXTURES / name))


def evaluate(loaded: dict, contract: dict = CONTRACT) -> runtime_contract.Evaluation:
    return runtime_contract.evaluate(contract, loaded)


def published(loaded: dict, name: str) -> dict:
    return next(c for c in loaded["published"] if c["metadata"]["name"] == f"fabric-runtime-{name}")


def deployment(loaded: dict) -> dict:
    return next(d for d in loaded["runtime"] if d["kind"] == "Deployment")


def config_map(loaded: dict) -> dict:
    return next(d for d in loaded["runtime"] if d["kind"] == "ConfigMap")


def pod(loaded: dict) -> dict:
    return deployment(loaded)["spec"]["template"]["spec"]


def record(loaded: dict) -> dict:
    return json.loads(published(loaded, "tenants")["data"]["publication.json"])


def set_record(loaded: dict, value) -> None:
    text = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
    published(loaded, "tenants")["data"]["publication.json"] = text


def registry(loaded: dict) -> dict:
    return json.loads(published(loaded, "tenants")["data"]["issuer-registry.json"])


def set_registry(loaded: dict, value) -> None:
    """Publish `value` as issuer-registry.json and name its exact bytes in the record, as the publisher does."""
    text = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
    published(loaded, "tenants")["data"]["issuer-registry.json"] = text
    current = record(loaded)
    try:
        generation = json.loads(text)["generation"]
    except (ValueError, KeyError, TypeError):
        generation = current["registry"]["generation"]
    current["registry"] = {"generation": generation, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    set_record(loaded, current)


def restamp(loaded: dict) -> None:
    """Rewrite publication.json the way the publisher does after a change.

    Every document's revision and bytes, and the resource history raised --
    never lowered -- to cover every published tenant binding and DataSource
    (`ResourceHistory::including` in saas-fabric #138).
    """
    current = record(loaded)
    for name, key in (("tenants", "tenant"), ("data-sources", "id")):
        found = [c for c in loaded["published"] if c["metadata"]["name"] == f"fabric-runtime-{name}"]
        try:
            resources = json.loads(found[0]["data"][f"{name}.json"]) if found else []
        except ValueError:
            resources = []
        history = current["resources"][name]
        for resource in resources if isinstance(resources, list) else []:
            if isinstance(resource, dict) and isinstance(resource.get("revision"), int):
                history[resource[key]] = max(history.get(resource[key], 0), resource["revision"])
        current["resources"][name] = dict(sorted(history.items()))
    for name in current["documents"]:
        found = [c for c in loaded["published"] if c["metadata"]["name"] == f"fabric-runtime-{name}"]
        if not found:
            continue
        data = found[0]["data"]
        try:
            revision = json.loads(data[f"{name}.manifest.json"])["revision"]
        except (KeyError, ValueError):
            continue
        current["documents"][name] = {
            "revision": revision,
            "sha256": hashlib.sha256(data[f"{name}.json"].encode()).hexdigest(),
        }
    set_record(loaded, current)


def set_manifest(loaded: dict, name: str, **fields) -> None:
    data = published(loaded, name)["data"]
    manifest = json.loads(data[f"{name}.manifest.json"])
    manifest.update(fields)
    data[f"{name}.manifest.json"] = json.dumps(manifest)
    restamp(loaded)


def set_payload(loaded: dict, name: str, payload) -> None:
    published(loaded, name)["data"][f"{name}.json"] = json.dumps(payload, indent=2) + "\n"
    restamp(loaded)


def payload(loaded: dict, name: str):
    return json.loads(published(loaded, name)["data"][f"{name}.json"])


class Assertions(unittest.TestCase):
    def assertRefused(self, result: runtime_contract.Evaluation, *fragments: str) -> None:
        self.assertTrue(result.problems, "expected a refusal, got none")
        for fragment in fragments:
            self.assertTrue(
                any(fragment in problem for problem in result.problems),
                f"no refusal mentions {fragment!r}: {result.problems}",
            )

    def assertClean(self, result: runtime_contract.Evaluation) -> None:
        self.assertEqual(result.problems, [])


class Scenarios(Assertions):
    """The three checked-in scenarios hold exactly as their names say."""

    def test_awaiting_inputs_is_a_valid_incomplete_state(self) -> None:
        result = evaluate(scenario("awaiting-inputs"))
        self.assertClean(result)
        self.assertEqual(result.replicas, 0)
        self.assertTrue(all(result.unmet[gate] for gate in runtime_contract.GATES), result.unmet)
        self.assertEqual(len([r for r in result.unmet["documents"] if "missing required document" in r]), 3)

    def test_ready_at_zero_meets_every_gate_and_still_waits_for_the_operator(self) -> None:
        result = evaluate(scenario("ready-at-zero"))
        self.assertClean(result)
        self.assertEqual(result.replicas, 0)
        self.assertEqual(result.unmet, {gate: [] for gate in runtime_contract.GATES})

    def test_activated_meets_every_gate(self) -> None:
        result = evaluate(scenario("activated"))
        self.assertClean(result)
        self.assertEqual(result.replicas, 1)
        self.assertEqual(result.unmet, {gate: [] for gate in runtime_contract.GATES})

    def test_command_line_exit_status_follows_refusals(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runtime_contract.main([str(FIXTURES / "activated")]), 0)
        self.assertIn("connectors: met", output.getvalue())
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "activated"
            shutil.copytree(FIXTURES / "activated", copied)
            held = yaml.safe_load((copied / "held.yaml").read_text())
            held["registry"]["generation"] = 9
            (copied / "held.yaml").write_text(yaml.safe_dump(held))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(runtime_contract.main([str(copied)]), 1)
        self.assertIn("refused: issuer-registry.json: generation 2 regresses held generation 9", output.getvalue())


class Inert(Assertions):
    """Nothing this change adds can reach a cluster or carry a credential."""

    def kustomize_and_application_sources(self) -> list[str]:
        sources = []
        for path in REPOSITORY.rglob("*.yaml"):
            if ".git" in path.parts or ".render" in path.parts[len(REPOSITORY.parts):][:1]:
                continue
            for document in yaml.safe_load_all(path.read_text()):
                if not isinstance(document, dict):
                    continue
                if document.get("kind") == "Kustomization":
                    base = path.parent.relative_to(REPOSITORY)
                    for entry in (document.get("resources") or []) + (document.get("components") or []):
                        sources.append(str((base / entry)))
                if document.get("kind") == "Application":
                    spec = document.get("spec") or {}
                    for source in [spec.get("source") or {}] + list(spec.get("sources") or []):
                        if source.get("path"):
                            sources.append(source["path"])
        return sources

    def test_no_kustomization_or_application_reaches_fixtures_or_contracts(self) -> None:
        sources = self.kustomize_and_application_sources()
        self.assertTrue(sources)
        for source in sources:
            normalised = Path(source).as_posix()
            resolved = (REPOSITORY / normalised).resolve()
            for inert in ("fixtures", "contracts"):
                self.assertFalse(
                    resolved == REPOSITORY / inert or (REPOSITORY / inert) in resolved.parents,
                    f"{source} reaches {inert}/",
                )

    def test_fixtures_and_contract_carry_no_plaintext_secret(self) -> None:
        problems: list[str] = []
        check._DOCUMENTS.clear()
        check.check_no_plaintext_secrets(REPOSITORY / "fixtures", problems)
        check.check_no_plaintext_secrets(REPOSITORY / "contracts", problems)
        self.assertEqual(problems, [])

    def test_fixture_issuers_use_a_reserved_host(self) -> None:
        found = 0
        for name in ("ready-at-zero", "activated"):
            for registration in registry(scenario(name))["issuers"]:
                found += 1
                self.assertTrue(registration["issuer"].startswith("https://identity.example.invalid/"))
        self.assertTrue(found)


class Contract(Assertions):
    def test_repository_contract_is_supported(self) -> None:
        self.assertEqual(runtime_contract.contract_problems(CONTRACT), [])

    def test_contract_matches_the_base_deployment(self) -> None:
        base = REPOSITORY / "applications" / "core" / "saas-fabric" / "base"
        documents = [
            document
            for name in ("configmap.yaml", "deployment.yaml")
            for document in yaml.safe_load_all((base / name).read_text())
        ]
        self.assertEqual(runtime_contract.runtime_mount_problems(CONTRACT, documents, "base"), [])
        self.assertEqual(runtime_contract.static_replica_problems(CONTRACT, documents, "base"), [])

    def test_a_revision_policy_other_than_roll_forward_is_unsupported(self) -> None:
        contract = copy.deepcopy(CONTRACT)
        contract["documents"][0]["revision"] = "rollback"
        problems = runtime_contract.contract_problems(contract)
        self.assertTrue(any("revision policy 'rollback'" in p for p in problems), problems)

    def test_a_contract_missing_a_document_is_refused(self) -> None:
        contract = copy.deepcopy(CONTRACT)
        del contract["documents"][2]
        problems = runtime_contract.contract_problems(contract)
        self.assertTrue(any("exactly the documents" in p for p in problems), problems)

    def test_two_documents_on_one_mount_path_are_refused(self) -> None:
        contract = copy.deepcopy(CONTRACT)
        contract["documents"][1]["mountPath"] = contract["documents"][0]["mountPath"]
        problems = runtime_contract.contract_problems(contract)
        self.assertTrue(any("is shared" in p for p in problems), problems)

    def test_an_unknown_contract_key_is_unsupported(self) -> None:
        contract = copy.deepcopy(CONTRACT)
        contract["documents"][0]["optional"] = True
        problems = runtime_contract.contract_problems(contract)
        self.assertTrue(any(runtime_contract.UNSUPPORTED in p or "exactly" in p for p in problems), problems)

    def test_an_unreadable_contract_stops_the_evaluation(self) -> None:
        contract = copy.deepcopy(CONTRACT)
        contract["schemaVersion"] = 2
        self.assertRefused(evaluate(scenario("activated"), contract), "schemaVersion 2")


class RuntimeDocuments(Assertions):
    """Ownership, version and shape of each published document (D01-1, D01-27)."""

    def test_missing_document_at_zero_replicas_is_an_unmet_gate_not_a_refusal(self) -> None:
        loaded = scenario("ready-at-zero")
        # The publisher writes tenants and its record last, so a pass interrupted
        # before them leaves data sources and catalogue without tenants.
        loaded["published"] = [c for c in loaded["published"] if c["metadata"]["name"] != "fabric-runtime-tenants"]
        result = evaluate(loaded)
        self.assertClean(result)
        self.assertIn("missing required document tenants (fabric-runtime-tenants)", result.unmet["documents"])

    def test_missing_document_with_replicas_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["published"] = [c for c in loaded["published"] if c["metadata"]["name"] != "fabric-runtime-tenants"]
        self.assertRefused(evaluate(loaded), "documents gate is unmet: missing required document tenants")

    def test_a_document_owned_by_another_writer_is_refused(self) -> None:
        loaded = scenario("activated")
        published(loaded, "tenants")["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "argocd"
        self.assertRefused(evaluate(loaded), "managed-by is 'argocd', contract owner is 'saas-fabric'")

    def test_a_document_without_an_owner_label_is_refused(self) -> None:
        loaded = scenario("activated")
        del published(loaded, "catalog")["metadata"]["labels"]
        self.assertRefused(evaluate(loaded), "managed-by is None")

    def test_a_document_in_another_namespace_is_refused(self) -> None:
        loaded = scenario("activated")
        published(loaded, "data-sources")["metadata"]["namespace"] = "default"
        self.assertRefused(evaluate(loaded), "namespace 'default', contract says 'platform-system'")

    def test_an_extra_data_key_is_an_unsupported_shape(self) -> None:
        loaded = scenario("activated")
        published(loaded, "catalog")["data"]["catalog.v2.json"] = "{}"
        self.assertRefused(evaluate(loaded), "data keys must be exactly catalog.json, catalog.manifest.json --")

    def test_a_missing_manifest_is_an_unsupported_shape(self) -> None:
        loaded = scenario("activated")
        del published(loaded, "tenants")["data"]["tenants.manifest.json"]
        self.assertRefused(
            evaluate(loaded), "data keys must be exactly publication.json, tenants.json, tenants.manifest.json")

    def test_binary_data_is_unsupported(self) -> None:
        loaded = scenario("activated")
        published(loaded, "tenants")["binaryData"] = {"other": "AA=="}
        self.assertRefused(evaluate(loaded), "binaryData")

    def test_a_manifest_describing_another_document_is_refused(self) -> None:
        loaded = scenario("activated")
        set_manifest(loaded, "catalog", document="tenants")
        self.assertRefused(evaluate(loaded), "manifest describes 'tenants', not 'catalog'")

    def test_another_contract_version_is_unsupported(self) -> None:
        loaded = scenario("activated")
        set_manifest(loaded, "tenants", contract_version=3)
        self.assertRefused(evaluate(loaded), "manifest contract_version 3, contract accepts [1, 2]")

    def test_a_manifest_with_an_extra_field_is_unsupported(self) -> None:
        loaded = scenario("activated")
        set_manifest(loaded, "tenants", published_at="2026-10-11T00:00:00Z")
        self.assertRefused(evaluate(loaded), "manifest keys must be exactly")

    def test_a_non_positive_revision_is_refused(self) -> None:
        loaded = scenario("activated")
        set_manifest(loaded, "data-sources", revision=0)
        self.assertRefused(evaluate(loaded), "manifest revision must be a positive integer")

    def test_a_payload_that_is_not_json_is_unsupported(self) -> None:
        loaded = scenario("activated")
        published(loaded, "data-sources")["data"]["data-sources.json"] = "id: alpha-primary\n"
        self.assertRefused(evaluate(loaded), "data-sources.json is not JSON")

    def test_an_unknown_tenant_binding_field_is_unsupported(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[0]["issuer"] = "https://identity.example.invalid/realms/alpha"
        set_payload(loaded, "tenants", tenants)
        self.assertRefused(evaluate(loaded), "tenant binding keys must be within")

    def test_an_unmodelled_isolation_kind_is_unsupported(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[0]["data"]["primary"]["isolation"] = {"kind": "row_level_security"}
        set_payload(loaded, "tenants", tenants)
        self.assertRefused(evaluate(loaded), "isolation -- unsupported")

    def test_a_tenant_bound_to_an_unpublished_data_source_is_refused(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[1]["data"]["primary"]["data_source"] = "gamma-primary"
        set_payload(loaded, "tenants", tenants)
        self.assertRefused(evaluate(loaded), "'gamma-primary', which fabric-runtime-data-sources does not declare")

    def test_an_empty_catalogue_is_refused(self) -> None:
        loaded = scenario("activated")
        set_payload(loaded, "catalog", {})
        self.assertRefused(evaluate(loaded), "empty catalogue")

    def test_an_undeclared_runtime_configmap_is_unsupported(self) -> None:
        loaded = scenario("activated")
        extra = copy.deepcopy(published(loaded, "catalog"))
        extra["metadata"]["name"] = "fabric-runtime-registry"
        loaded["published"].append(extra)
        self.assertRefused(evaluate(loaded), "'fabric-runtime-registry' is not a document the contract declares")

    def test_a_document_published_twice_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["published"].append(copy.deepcopy(published(loaded, "catalog")))
        self.assertRefused(evaluate(loaded), "published twice")

    def test_a_document_revision_regression_is_refused(self) -> None:
        loaded = scenario("activated")
        set_manifest(loaded, "tenants", revision=2)
        self.assertRefused(evaluate(loaded), "revision 2 regresses held revision 3", "roll forward only")

    def test_rewriting_a_held_revision_in_place_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["documents"]["tenants"]["revision"] = 4
        self.assertRefused(evaluate(loaded), "revision 4 is already held with different content")

    def test_republishing_a_held_revision_unchanged_is_accepted(self) -> None:
        loaded = scenario("activated")
        self.assertEqual(loaded["held"]["documents"]["data-sources"]["revision"], 3)
        self.assertClean(evaluate(loaded))

    def test_restoring_earlier_content_under_a_new_revision_is_accepted(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["documents"]["data-sources"]["revision"] = 2
        self.assertClean(evaluate(loaded))

    def test_a_tenant_revision_regression_is_refused(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[0]["revision"] = 1
        set_payload(loaded, "tenants", tenants)
        self.assertRefused(evaluate(loaded), "'alpha' revision 1 regresses held revision 2", "resource revisions")

    def test_a_data_source_revision_regression_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["resources"]["data-sources"]["alpha-primary"] = 5
        self.assertRefused(evaluate(loaded), "'alpha-primary' revision 2 regresses held revision 5")

    def test_a_held_record_outside_the_modelled_shape_is_unsupported(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["documents"]["tenants"]["published_at"] = "yesterday"
        self.assertRefused(evaluate(loaded), "documents.tenants must be a revision and a sha256")


class PublicationRecord(Assertions):
    """publication.json, the record saas-fabric #136 writes into the tenants ConfigMap."""

    def test_the_publisher_shaped_tenants_configmap_passes(self) -> None:
        loaded = scenario("activated")
        tenants = published(loaded, "tenants")
        self.assertEqual(
            set(tenants["data"]), {"tenants.json", "tenants.manifest.json", "publication.json", "issuer-registry.json"})
        text = tenants["data"]["publication.json"]
        current = json.loads(text)
        self.assertEqual(list(current), [
            "contract_version", "minimum_reader_contract", "minimum_publisher_contract", "generation", "documents",
            "resources", "registry",
        ])
        self.assertEqual(list(current["resources"]), ["tenants", "data-sources"])
        self.assertEqual(current["resources"]["tenants"], {"alpha": 3, "beta": 1})
        self.assertEqual(current["resources"]["data-sources"], {"alpha-primary": 2, "beta-primary": 1})
        self.assertEqual(list(current["documents"]), ["tenants", "data-sources", "catalog"])
        self.assertEqual(text, json.dumps(current, indent=2) + "\n")
        for name, recorded in current["documents"].items():
            self.assertEqual(
                recorded["sha256"], hashlib.sha256(published(loaded, name)["data"][f"{name}.json"].encode()).hexdigest())
        self.assertClean(evaluate(loaded))

    def test_a_tenants_configmap_without_the_record_is_refused(self) -> None:
        loaded = scenario("activated")
        del published(loaded, "tenants")["data"]["publication.json"]
        self.assertRefused(evaluate(loaded), "data keys must be exactly publication.json, tenants.json")

    def test_the_record_key_on_another_document_is_unsupported(self) -> None:
        loaded = scenario("activated")
        published(loaded, "catalog")["data"]["publication.json"] = published(loaded, "tenants")["data"]["publication.json"]
        self.assertRefused(evaluate(loaded), "data keys must be exactly catalog.json, catalog.manifest.json --")

    def test_an_arbitrary_extra_key_beside_the_record_is_unsupported(self) -> None:
        loaded = scenario("activated")
        published(loaded, "tenants")["data"]["notes.json"] = "{}"
        self.assertRefused(evaluate(loaded), "data keys must be exactly publication.json, tenants.json")

    def test_a_record_that_is_not_json_is_refused(self) -> None:
        loaded = scenario("activated")
        set_record(loaded, "{ torn")
        self.assertRefused(evaluate(loaded), "publication.json: not a JSON object")

    def test_a_record_with_an_unmodelled_field_is_unsupported(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["published_at"] = "2026-10-11T00:00:00Z"
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "publication.json: keys must be exactly")

    def test_a_record_missing_a_document_is_unsupported(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        del current["documents"]["catalog"]
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "documents must name exactly")

    def test_a_malformed_digest_is_unsupported(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["documents"]["catalog"]["sha256"] = current["documents"]["catalog"]["sha256"].upper()
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "documents.catalog must be a positive revision and a lowercase hex sha256")

    def test_a_document_rewritten_after_its_record_is_an_incomplete_generation(self) -> None:
        loaded = scenario("activated")
        sources = payload(loaded, "data-sources")
        sources[0]["labels"] = {"tier": "gold"}
        published(loaded, "data-sources")["data"]["data-sources.json"] = json.dumps(sources, indent=2) + "\n"
        set_manifest_without_restamp = json.loads(published(loaded, "data-sources")["data"]["data-sources.manifest.json"])
        set_manifest_without_restamp["revision"] = 4
        published(loaded, "data-sources")["data"]["data-sources.manifest.json"] = json.dumps(set_manifest_without_restamp)
        self.assertRefused(
            evaluate(loaded),
            "names data-sources revision 3, its manifest says 4 -- incomplete generation",
            "names data-sources bytes that are not the published ones",
        )

    def test_a_record_naming_an_unpublished_document_is_an_incomplete_generation(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["published"] = [c for c in loaded["published"] if c["metadata"]["name"] != "fabric-runtime-catalog"]
        self.assertRefused(evaluate(loaded), "generation 5 names catalog, which is not published -- incomplete generation")

    def test_a_reader_floor_above_the_runtime_is_refused(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["minimum_reader_contract"] = 2
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "minimum_reader_contract 2 is above the runtime's reader contract 1")

    def test_a_publisher_floor_above_its_own_contract_is_unsupported(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["minimum_publisher_contract"] = 3
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "minimum_publisher_contract 3 is above the publisher contract 2")

    def test_a_publisher_below_the_held_rollback_floor_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["publication"]["minimumPublisherContract"] = 3
        self.assertRefused(evaluate(loaded), "held record's rollback floor is publisher contract 3, above this publisher's 2")

    def test_another_record_contract_version_is_unsupported(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["contract_version"] = 3
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "publication.json: contract_version 3, contract says 2")

    def test_a_generation_regression_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["publication"]["generation"] = 6
        self.assertRefused(evaluate(loaded), "generation 5 regresses held generation 6")

    def test_a_held_generation_rewritten_in_place_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["publication"]["generation"] = 5
        self.assertRefused(evaluate(loaded), "generation 5 is already held with a different record")

    def test_a_non_positive_generation_is_refused(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["generation"] = 0
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "generation must be a positive integer")

    def test_the_contract_must_place_the_record_in_the_tenants_document(self) -> None:
        contract = copy.deepcopy(CONTRACT)
        contract["publicationRecord"]["document"] = "catalog"
        problems = runtime_contract.contract_problems(contract)
        self.assertTrue(any("publication.json in the tenants document" in p for p in problems), problems)


class ResourceHistory(Assertions):
    """`resources` in publication.json (saas-fabric #138): the highest revision each resource was ever published at."""

    def history(self, loaded: dict, name: str, entries: dict) -> None:
        current = record(loaded)
        current["resources"][name] = entries
        set_record(loaded, current)

    def test_a_record_without_resources_is_refused(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        del current["resources"]
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "publication.json: keys must be exactly")

    def test_a_history_with_an_unmodelled_document_is_unsupported(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["resources"]["catalog"] = {}
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "resources must map exactly ['data-sources', 'tenants']")

    def test_a_non_positive_history_revision_is_unsupported(self) -> None:
        loaded = scenario("activated")
        self.history(loaded, "tenants", {"alpha": 3, "beta": 0})
        self.assertRefused(evaluate(loaded), "resources.tenants must map ids to positive revisions")

    def test_a_history_revision_that_is_not_an_integer_is_unsupported(self) -> None:
        loaded = scenario("activated")
        self.history(loaded, "data-sources", {"alpha-primary": "2", "beta-primary": 1})
        self.assertRefused(evaluate(loaded), "resources.data-sources must map ids to positive revisions")

    def test_a_history_key_that_is_not_a_tenant_id_is_unsupported(self) -> None:
        loaded = scenario("activated")
        self.history(loaded, "tenants", {"alpha": 3, "beta": 1, "Not A Tenant": 1})
        self.assertRefused(evaluate(loaded), "resources.tenants must map ids to positive revisions")

    def test_a_published_binding_missing_from_the_history_is_refused(self) -> None:
        loaded = scenario("activated")
        self.history(loaded, "tenants", {"alpha": 3})
        self.assertRefused(evaluate(loaded), "resources.tenants has no entry for published 'beta'")

    def test_a_history_below_a_published_revision_is_refused(self) -> None:
        loaded = scenario("activated")
        self.history(loaded, "data-sources", {"alpha-primary": 1, "beta-primary": 1})
        self.assertRefused(evaluate(loaded), "resources.data-sources['alpha-primary'] = 1 is below its published revision 2")

    def test_a_history_lowering_a_held_entry_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["resources"]["tenants"]["alpha"] = 7
        self.assertRefused(evaluate(loaded), "resources.tenants['alpha'] = 3 lowers the held 7")

    def test_a_history_dropping_a_held_entry_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["resources"]["data-sources"]["retired-primary"] = 4
        self.assertRefused(
            evaluate(loaded), "resources.data-sources drops 'retired-primary', held at 4 -- history entries are never removed")

    def test_a_retired_resource_kept_in_the_history_passes(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["resources"]["data-sources"]["retired-primary"] = 4
        self.history(loaded, "data-sources", {"alpha-primary": 2, "beta-primary": 1, "retired-primary": 4})
        self.assertClean(evaluate(loaded))

    def test_a_history_above_the_published_revision_passes(self) -> None:
        loaded = scenario("activated")
        self.history(loaded, "tenants", {"alpha": 9, "beta": 1})
        self.assertClean(evaluate(loaded))

    def test_raising_a_resource_revision_raises_its_history(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[1]["revision"] = 2
        set_payload(loaded, "tenants", tenants)
        set_manifest(loaded, "tenants", revision=5)
        self.assertEqual(record(loaded)["resources"]["tenants"]["beta"], 2)
        self.assertClean(evaluate(loaded))


U64_MAX = 2**64 - 1


class ExactValues(Assertions):
    """Ids match whole strings and numbers fit the width Fabric reads them into.

    `re.match` with `$` accepts a trailing newline, and Python ints are
    unbounded, so either flaw let a record Fabric cannot read pass as ready.
    """

    def history(self, loaded: dict, name: str, entries: dict) -> None:
        current = record(loaded)
        current["resources"][name] = entries
        set_record(loaded, current)

    def test_a_tenant_history_id_with_a_trailing_newline_is_refused(self) -> None:
        loaded = scenario("activated")
        self.history(loaded, "tenants", {"alpha": 3, "beta": 1, "gamma\n": 1})
        self.assertRefused(evaluate(loaded), "resources.tenants must map ids to positive revisions")

    def test_a_data_source_history_id_with_a_trailing_newline_is_refused(self) -> None:
        loaded = scenario("activated")
        self.history(loaded, "data-sources", {"alpha-primary": 2, "beta-primary": 1, "gamma-primary\n": 1})
        self.assertRefused(evaluate(loaded), "resources.data-sources must map ids to positive revisions")

    def test_a_history_revision_of_two_to_the_sixty_four_is_refused(self) -> None:
        for name, entries in (("tenants", {"alpha": 2**64, "beta": 1}),
                              ("data-sources", {"alpha-primary": 2**64, "beta-primary": 1})):
            with self.subTest(name=name):
                loaded = scenario("activated")
                self.history(loaded, name, entries)
                self.assertRefused(evaluate(loaded), f"resources.{name} must map ids to positive revisions")

    def test_a_history_revision_of_u64_max_is_accepted(self) -> None:
        loaded = scenario("activated")
        self.history(loaded, "tenants", {"alpha": U64_MAX, "beta": 1})
        self.history(loaded, "data-sources", {"alpha-primary": U64_MAX, "beta-primary": 1})
        self.assertClean(evaluate(loaded))

    def test_history_revisions_that_are_not_positive_ints_are_refused(self) -> None:
        for value in (True, 2.0, 0, -1):
            with self.subTest(value=value):
                loaded = scenario("activated")
                self.history(loaded, "tenants", {"alpha": 3, "beta": value})
                self.assertRefused(evaluate(loaded), "resources.tenants must map ids to positive revisions")

    def test_a_tenant_binding_id_with_a_trailing_newline_is_refused(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[1]["tenant"] = "beta\n"
        set_payload(loaded, "tenants", tenants)
        self.assertRefused(evaluate(loaded), "tenant 'beta\\n' is not a tenant id")

    def test_a_data_source_id_with_a_trailing_newline_is_refused(self) -> None:
        loaded = scenario("activated")
        sources = payload(loaded, "data-sources")
        sources[1]["id"] = "beta-primary\n"
        set_payload(loaded, "data-sources", sources)
        self.assertRefused(evaluate(loaded), "data source id 'beta-primary\\n' is not an identifier")

    def test_a_binding_naming_a_data_source_with_a_trailing_newline_is_refused(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[1]["data"]["primary"]["data_source"] = "beta-primary\n"
        set_payload(loaded, "tenants", tenants)
        self.assertRefused(evaluate(loaded), "is not an identifier pair")

    def test_a_record_digest_with_a_trailing_newline_is_refused(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["documents"]["catalog"]["sha256"] += "\n"
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "documents.catalog must be a positive revision and a lowercase hex sha256")

    def test_a_resource_revision_of_two_to_the_sixty_four_is_refused(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[1]["revision"] = 2**64
        set_payload(loaded, "tenants", tenants)
        self.assertRefused(evaluate(loaded), "tenant 'beta' revision must be a positive integer")

    def test_a_manifest_revision_of_two_to_the_sixty_four_is_refused(self) -> None:
        loaded = scenario("activated")
        set_manifest(loaded, "catalog", revision=2**64)
        self.assertRefused(evaluate(loaded), "manifest revision must be a positive integer")

    def test_a_record_generation_of_two_to_the_sixty_four_is_refused(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["generation"] = 2**64
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "generation must be a positive integer")

    def test_a_record_generation_of_u64_max_is_accepted(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["generation"] = U64_MAX
        set_record(loaded, current)
        self.assertClean(evaluate(loaded))

    def test_a_reader_floor_beyond_u32_is_refused(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["minimum_reader_contract"] = 2**32
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "minimum_reader_contract must be a positive integer")

    def test_a_boolean_contract_version_is_refused(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["contract_version"] = True
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "publication.json: contract_version True, contract says 2")

    def test_a_boolean_manifest_contract_version_is_refused(self) -> None:
        loaded = scenario("activated")
        set_manifest(loaded, "catalog", contract_version=True)
        self.assertRefused(evaluate(loaded), "manifest contract_version True, contract accepts [1, 2]")

    def test_a_registry_issuer_with_a_trailing_newline_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        changed = registry(loaded)
        changed["issuers"][1]["issuer"] += "\n"
        set_registry(loaded, changed)
        loaded["held"]["registry"] = None
        self.assertRefused(evaluate(loaded), "is not https://<host>[:<port>]/realms/<realm>")

    def test_a_composition_client_with_a_trailing_newline_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["compositions"]["beta.yaml"]["client"] = "beta\n"
        self.assertRefused(evaluate(loaded), "client 'beta\\n' is not a tenant id")

    def test_replicas_beyond_int32_are_unsupported(self) -> None:
        loaded = scenario("activated")
        deployment(loaded)["spec"]["replicas"] = 2**31
        self.assertRefused(evaluate(loaded), "replica count is not a literal non-negative integer")


class PublisherContractTwo(Assertions):
    """saas-fabric #139: the publisher contract is 2, carried as the record's contract and rollback floor.

    `minimum_reader_contract` and the runtime's reader contract stay 1. A
    manifest keeps the contract it was written at until its document changes,
    so 1 and 2 are both accepted there.
    """

    def stamp(self, loaded: dict, contract_version: int, floor: int) -> None:
        current = record(loaded)
        current["contract_version"] = contract_version
        current["minimum_publisher_contract"] = floor
        set_record(loaded, current)

    def test_the_contract_declares_publisher_two_and_reader_one(self) -> None:
        self.assertEqual(CONTRACT["publicationContractVersion"], 2)
        self.assertEqual(CONTRACT["manifestContractVersions"], [1, 2])
        self.assertEqual(CONTRACT["publicationRecord"]["readerContract"], 1)

    def test_a_record_written_by_the_139_publisher_passes(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        self.assertEqual(
            (current["contract_version"], current["minimum_publisher_contract"], current["minimum_reader_contract"]),
            (2, 2, 1),
        )
        self.assertClean(evaluate(loaded))

    def test_the_floor_raise_over_a_held_contract_one_record_passes_with_a_contract_one_manifest(self) -> None:
        loaded = scenario("ready-at-zero")
        self.assertEqual(loaded["held"]["publication"]["minimumPublisherContract"], 1)
        self.assertEqual(json.loads(published(loaded, "data-sources")["data"]["data-sources.manifest.json"])["contract_version"], 1)
        self.assertClean(evaluate(loaded))

    def test_a_138_era_record_against_a_held_floor_of_two_is_refused(self) -> None:
        loaded = scenario("activated")
        self.assertEqual(loaded["held"]["publication"]["minimumPublisherContract"], 2)
        self.stamp(loaded, 1, 1)
        self.assertRefused(
            evaluate(loaded),
            "minimum_publisher_contract 1 lowers the held rollback floor 2 -- the floor rolls forward only",
            "contract_version 1, contract says 2 -- the deployed publisher has not rewritten this record",
        )

    def test_a_138_era_record_not_yet_rewritten_is_refused_even_over_a_floor_of_one(self) -> None:
        loaded = scenario("ready-at-zero")
        self.stamp(loaded, 1, 1)
        result = evaluate(loaded)
        self.assertRefused(result, "contract_version 1, contract says 2 -- the deployed publisher has not rewritten")
        self.assertFalse(any("lowers the held rollback floor" in p for p in result.problems), result.problems)

    def test_a_floor_that_differs_from_the_record_contract_is_refused(self) -> None:
        loaded = scenario("activated")
        self.stamp(loaded, 2, 1)
        self.assertRefused(evaluate(loaded), "minimum_publisher_contract 1 differs from contract_version 2")

    def test_a_reader_floor_raised_to_two_is_refused(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["minimum_reader_contract"] = 2
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "minimum_reader_contract 2 is above the runtime's reader contract 1")

    def test_versions_above_the_bounds_are_refused(self) -> None:
        cases = (
            ("contract_version", 3, "contract_version 3, contract says 2"),
            ("minimum_publisher_contract", 3, "minimum_publisher_contract 3 is above the publisher contract 2"),
            ("contract_version", 2**32, "contract_version 4294967296, contract says 2"),
            ("minimum_publisher_contract", 2**32, "minimum_publisher_contract must be a positive integer"),
        )
        for field, value, fragment in cases:
            with self.subTest(field=field, value=value):
                loaded = scenario("activated")
                current = record(loaded)
                current[field] = value
                set_record(loaded, current)
                self.assertRefused(evaluate(loaded), fragment)

    def test_a_manifest_above_the_accepted_versions_is_refused(self) -> None:
        for value in (3, 0, 2**32):
            with self.subTest(value=value):
                loaded = scenario("activated")
                set_manifest(loaded, "catalog", contract_version=value)
                self.assertRefused(evaluate(loaded), "contract accepts [1, 2]")

    def test_a_contract_whose_manifest_versions_do_not_end_at_the_publisher_is_refused(self) -> None:
        for versions in ([1], [1, 2, 3], [2, 1], [1, 1, 2], [], [0, 2]):
            with self.subTest(versions=versions):
                contract = copy.deepcopy(CONTRACT)
                contract["manifestContractVersions"] = versions
                problems = runtime_contract.contract_problems(contract)
                self.assertTrue(any("manifestContractVersions" in p for p in problems), problems)


class IssuerRegistryInTheRecord(Assertions):
    """saas-fabric #140: issuer-registry.json in the tenants ConfigMap, named by the record over its exact bytes."""

    def without_registry(self, loaded: dict) -> None:
        del published(loaded, "tenants")["data"]["issuer-registry.json"]
        current = record(loaded)
        del current["registry"]
        set_record(loaded, current)

    def test_the_publisher_shaped_registry_passes(self) -> None:
        loaded = scenario("activated")
        text = published(loaded, "tenants")["data"]["issuer-registry.json"]
        document = json.loads(text)
        self.assertEqual(list(document), ["schemaVersion", "generation", "issuers"])
        self.assertEqual(text, json.dumps(document, indent=2) + "\n")
        self.assertEqual(
            [(r["issuer"], r["tenant"]) for r in document["issuers"]],
            sorted((r["issuer"], r["tenant"]) for r in document["issuers"]),
        )
        self.assertEqual(
            record(loaded)["registry"],
            {"generation": document["generation"], "sha256": hashlib.sha256(text.encode()).hexdigest()},
        )
        self.assertClean(evaluate(loaded))

    def test_a_record_naming_a_registry_the_configmap_lacks_is_refused(self) -> None:
        loaded = scenario("activated")
        del published(loaded, "tenants")["data"]["issuer-registry.json"]
        self.assertRefused(evaluate(loaded), "names registry generation 2, and the tenants ConfigMap holds no issuer-registry.json")

    def test_a_registry_the_record_does_not_name_is_unsupported(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["held"]["registry"] = None
        current = record(loaded)
        del current["registry"]
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "holds issuer-registry.json, which the record does not name")

    def test_the_registry_key_on_another_document_is_unsupported(self) -> None:
        loaded = scenario("activated")
        published(loaded, "catalog")["data"]["issuer-registry.json"] = published(loaded, "tenants")["data"]["issuer-registry.json"]
        self.assertRefused(evaluate(loaded), "data keys must be exactly catalog.json, catalog.manifest.json --")

    def test_registry_bytes_that_differ_from_the_record_are_an_incomplete_generation(self) -> None:
        loaded = scenario("activated")
        text = published(loaded, "tenants")["data"]["issuer-registry.json"]
        published(loaded, "tenants")["data"]["issuer-registry.json"] = json.dumps(json.loads(text)) + "\n"
        self.assertRefused(evaluate(loaded), "registry generation 2 names issuer-registry.json bytes that are not the published ones")

    def test_a_record_generation_that_differs_from_the_registry_is_refused(self) -> None:
        loaded = scenario("activated")
        current = record(loaded)
        current["registry"]["generation"] = 3
        set_record(loaded, current)
        self.assertRefused(evaluate(loaded), "issuer-registry.json: generation 2, and the publication record names generation 3")

    def test_a_changed_registry_reusing_the_held_generation_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["registry"] = {"generation": 2, "sha256": "0" * 64}
        self.assertRefused(evaluate(loaded), "generation 2 is already held with different bytes -- a changed registry takes a new generation")

    def test_the_held_registry_republished_unchanged_passes(self) -> None:
        loaded = scenario("activated")
        text = published(loaded, "tenants")["data"]["issuer-registry.json"]
        loaded["held"]["registry"] = {"generation": 2, "sha256": hashlib.sha256(text.encode()).hexdigest()}
        self.assertClean(evaluate(loaded))

    def test_a_registry_generation_regression_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["registry"]["generation"] = 5
        self.assertRefused(evaluate(loaded), "issuer-registry.json: generation 2 regresses held generation 5")

    def test_a_held_registry_cannot_be_withdrawn(self) -> None:
        loaded = scenario("activated")
        self.without_registry(loaded)
        self.assertRefused(evaluate(loaded), "names no registry, and registry generation 1 is held")

    def test_no_registry_ever_published_leaves_identity_unmet_at_zero(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["held"]["registry"] = None
        self.without_registry(loaded)
        result = evaluate(loaded)
        self.assertClean(result)
        self.assertIn("Fabric has not published a valid issuer registry generation", result.unmet["identity"])

    def test_a_malformed_recorded_registry_is_unsupported(self) -> None:
        for value in (
            {"generation": 0, "sha256": "0" * 64},
            {"generation": 2**64, "sha256": "0" * 64},
            {"generation": True, "sha256": "0" * 64},
            {"generation": 2, "sha256": "A" * 64},
            {"generation": 2, "sha256": "0" * 64 + "\n"},
            {"generation": 2, "sha256": "0" * 64, "issuers": 2},
            None,
        ):
            with self.subTest(value=value):
                loaded = scenario("activated")
                current = record(loaded)
                current["registry"] = value
                set_record(loaded, current)
                self.assertRefused(evaluate(loaded), "registry must be exactly a positive u64 generation and a lowercase hex sha256")

    def test_registry_generations_are_u64(self) -> None:
        loaded = scenario("activated")
        changed = registry(loaded)
        changed["generation"] = 2**64
        set_registry(loaded, changed)
        self.assertRefused(evaluate(loaded), "registry must be exactly a positive u64 generation")

    def test_a_u64_max_registry_generation_applied_at_every_edge_passes(self) -> None:
        loaded = scenario("activated")
        changed = registry(loaded)
        changed["generation"] = U64_MAX
        set_registry(loaded, changed)
        for composition in loaded["compositions"].values():
            composition["edge"]["registryGeneration"] = U64_MAX
        self.assertClean(evaluate(loaded))

    def test_a_new_registry_generation_the_edge_has_not_applied_is_refused(self) -> None:
        loaded = scenario("activated")
        changed = registry(loaded)
        changed["generation"] = 3
        set_registry(loaded, changed)
        self.assertRefused(evaluate(loaded), "edge applies registry generation 2, Fabric's registry is at 3")

    def test_registrations_out_of_canonical_order_are_unsupported(self) -> None:
        loaded = scenario("activated")
        changed = registry(loaded)
        changed["issuers"].reverse()
        set_registry(loaded, changed)
        self.assertRefused(evaluate(loaded), "issuers are not in the publisher's canonical order")

    def test_a_boolean_registry_schema_version_is_unsupported(self) -> None:
        loaded = scenario("activated")
        changed = registry(loaded)
        changed["schemaVersion"] = True
        set_registry(loaded, changed)
        self.assertRefused(evaluate(loaded), "issuer-registry.json: schemaVersion True")

    def test_an_issuer_with_a_path_after_the_realm_is_refused(self) -> None:
        loaded = scenario("activated")
        changed = registry(loaded)
        changed["issuers"][0]["issuer"] += "/account"
        set_registry(loaded, changed)
        self.assertRefused(evaluate(loaded), "is not https://<host>[:<port>]/realms/<realm>")

    def test_a_held_registry_digest_that_is_not_hex_is_unsupported(self) -> None:
        loaded = scenario("activated")
        loaded["held"]["registry"]["sha256"] = "not-a-digest"
        self.assertRefused(evaluate(loaded), "registry must be null or a generation and a lowercase hex sha256")

    def test_the_contract_names_the_registry_key(self) -> None:
        self.assertEqual(CONTRACT["publicationRecord"]["registryKey"], "issuer-registry.json")
        contract = copy.deepcopy(CONTRACT)
        contract["publicationRecord"]["registryKey"] = "registry.json"
        problems = runtime_contract.contract_problems(contract)
        self.assertTrue(any("registryKey must be issuer-registry.json" in p for p in problems), problems)


class Placement(Assertions):
    """Only a 'shared' data source serves more than one client; every published source has a serving composition."""

    def test_a_cross_client_dedicated_binding_is_refused(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[1]["data"]["primary"]["data_source"] = "alpha-primary"
        tenants[1]["revision"] = 2
        set_payload(loaded, "tenants", tenants)
        set_manifest(loaded, "tenants", revision=5)
        self.assertRefused(
            evaluate(loaded),
            "'beta' 'primary' -> 'alpha-primary': cross-client placement",
            "'alpha-primary' is 'dedicated' and bound by ['alpha', 'beta']",
        )

    def test_a_cross_client_binding_at_zero_replicas_is_still_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        tenants = payload(loaded, "tenants")
        tenants[1]["data"]["primary"]["data_source"] = "alpha-primary"
        set_payload(loaded, "tenants", tenants)
        set_manifest(loaded, "tenants", revision=5)
        self.assertRefused(evaluate(loaded), "cross-client placement")

    def shared(self, loaded: dict) -> None:
        sources = payload(loaded, "data-sources")
        sources[0]["placement"] = "shared"
        set_payload(loaded, "data-sources", sources)
        set_manifest(loaded, "data-sources", revision=4)

    def bind_shared(self, loaded: dict, beta_value: str = "beta") -> None:
        tenants = payload(loaded, "tenants")
        tenants[0]["data"]["primary"]["isolation"] = {"kind": "discriminator", "column": "tenant_key", "value": "alpha"}
        tenants[1]["data"]["primary"] = {
            "data_source": "alpha-primary",
            "isolation": {"kind": "discriminator", "column": "tenant_key", "value": beta_value},
        }
        set_payload(loaded, "tenants", tenants)
        set_manifest(loaded, "tenants", revision=5)

    def test_an_intentional_shared_placement_serves_two_clients(self) -> None:
        loaded = scenario("activated")
        self.shared(loaded)
        self.bind_shared(loaded)
        self.assertClean(evaluate(loaded))

    def test_a_shared_source_bound_without_a_discriminator_is_refused(self) -> None:
        loaded = scenario("activated")
        self.shared(loaded)
        self.assertRefused(evaluate(loaded), "'alpha' 'primary' -> 'alpha-primary': a 'shared' data source requires discriminator")

    def test_a_discriminator_value_bound_twice_is_refused(self) -> None:
        loaded = scenario("activated")
        self.shared(loaded)
        self.bind_shared(loaded, beta_value="alpha")
        self.assertRefused(evaluate(loaded), "discriminator value 'alpha' is already bound on 'alpha-primary'")

    def test_discriminator_isolation_on_a_dedicated_source_is_refused(self) -> None:
        loaded = scenario("activated")
        tenants = payload(loaded, "tenants")
        tenants[0]["data"]["primary"]["isolation"] = {"kind": "discriminator", "column": "tenant_key", "value": "alpha"}
        set_payload(loaded, "tenants", tenants)
        set_manifest(loaded, "tenants", revision=5)
        self.assertRefused(evaluate(loaded), "discriminator isolation on a 'dedicated' data source")

    def test_an_unmodelled_placement_class_is_unsupported(self) -> None:
        loaded = scenario("activated")
        sources = payload(loaded, "data-sources")
        sources[0]["placement"] = "pooled"
        set_payload(loaded, "data-sources", sources)
        set_manifest(loaded, "data-sources", revision=4)
        self.assertRefused(evaluate(loaded), "placement 'pooled' -- unsupported")

    def unserved(self, name: str) -> dict:
        loaded = scenario(name)
        sources = payload(loaded, "data-sources")
        sources.append(dict(copy.deepcopy(sources[0]), id="unserved-primary"))
        set_payload(loaded, "data-sources", sources)
        set_manifest(loaded, "data-sources", revision=4)
        return loaded

    def test_a_published_source_no_composition_serves_is_refused_with_replicas(self) -> None:
        self.assertRefused(
            evaluate(self.unserved("activated")),
            "connectors gate is unmet: data source 'unserved-primary' is published but no composition serves it",
        )

    def test_a_published_source_no_composition_serves_leaves_connectors_unmet_at_zero(self) -> None:
        result = evaluate(self.unserved("ready-at-zero"))
        self.assertClean(result)
        self.assertIn(
            "data source 'unserved-primary' is published but no composition serves it", result.unmet["connectors"])

    def test_a_source_served_by_two_compositions_is_refused(self) -> None:
        loaded = scenario("activated")
        beta = loaded["compositions"]["beta.yaml"]
        beta["database"]["dataSources"].append("alpha-primary")
        beta["connectors"][0]["dataSources"].append("alpha-primary")
        self.assertRefused(evaluate(loaded), "'alpha-primary' is served by compositions/alpha.yaml, compositions/beta.yaml")


class Mounts(Assertions):
    """Each document reaches the runtime at the contract's mount path, whole."""

    def mount(self, loaded: dict, name: str) -> dict:
        container = pod(loaded)["containers"][0]
        return next(m for m in container["volumeMounts"] if m["name"] == f"state-{name}")

    def volume(self, loaded: dict, name: str) -> dict:
        return next(v for v in pod(loaded)["volumes"] if v["name"] == f"state-{name}")

    def test_a_mismatched_mount_path_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        self.mount(loaded, "tenants")["mountPath"] = "/etc/fabric/state/tenant"
        self.assertRefused(
            evaluate(loaded),
            "tenants: mounted at '/etc/fabric/state/tenant', contract says '/etc/fabric/state/tenants'",
        )

    def test_a_config_key_outside_the_contract_mount_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        config = config_map(loaded)["data"]["config.toml"]
        config_map(loaded)["data"]["config.toml"] = config.replace(
            "/etc/fabric/state/catalog/catalog.json", "/etc/fabric/state/catalog/resources.json")
        self.assertRefused(evaluate(loaded), "catalog_path = '/etc/fabric/state/catalog/resources.json'")

    def test_a_volume_of_another_configmap_leaves_the_document_unmounted(self) -> None:
        loaded = scenario("ready-at-zero")
        self.volume(loaded, "catalog")["configMap"]["name"] = "fabric-runtime-catalogue"
        self.assertRefused(evaluate(loaded), "catalog: missing required document -- no single volume")

    def test_an_optional_document_volume_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        self.volume(loaded, "data-sources")["configMap"]["optional"] = True
        self.assertRefused(evaluate(loaded), "optional volume mounts empty")

    def test_a_sub_path_document_mount_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        self.mount(loaded, "tenants")["subPath"] = "tenants.json"
        self.assertRefused(evaluate(loaded), "subPath mount pins the document")

    def test_a_projected_document_volume_is_unsupported(self) -> None:
        loaded = scenario("ready-at-zero")
        volume = self.volume(loaded, "catalog")
        volume["projected"] = {"sources": [{"configMap": volume.pop("configMap")}]}
        self.assertRefused(evaluate(loaded), "projected volume -- unsupported")

    def test_a_runtime_without_its_deployment_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        deployment(loaded)["metadata"]["name"] = "saas-fabric-runtime"
        self.assertRefused(evaluate(loaded), "exactly one Deployment/saas-fabric is required")


class Composition(Assertions):
    """Per-client platform composition (D01-3): synthetic, credential-free, exactly the modelled shape."""

    def composition(self, loaded: dict, client: str = "alpha") -> dict:
        return loaded["compositions"][f"{client}.yaml"]

    def test_a_credential_shaped_key_is_refused(self) -> None:
        loaded = scenario("activated")
        self.composition(loaded)["database"]["superuserPassword"] = "{{ generated }}"
        self.assertRefused(evaluate(loaded), "credential-shaped key .database.superuserPassword")

    def test_a_connector_credential_reference_is_still_refused(self) -> None:
        loaded = scenario("activated")
        self.composition(loaded)["connectors"][0]["secretRef"] = "clients/alpha/postgres"
        self.assertRefused(evaluate(loaded), "credential-shaped key .connectors[0].secretRef")

    def test_a_registration_in_a_composition_is_unsupported(self) -> None:
        loaded = scenario("activated")
        self.composition(loaded)["registrations"] = [{"issuer": "https://identity.example.invalid/realms/alpha"}]
        self.assertRefused(evaluate(loaded), "compositions/alpha.yaml: keys must be exactly")

    def test_a_composition_outside_the_clients_namespace_is_refused(self) -> None:
        loaded = scenario("activated")
        self.composition(loaded)["namespace"] = "platform-system"
        self.assertRefused(evaluate(loaded), "namespace must be client-alpha")

    def test_an_issuer_for_another_realm_is_refused(self) -> None:
        loaded = scenario("activated")
        self.composition(loaded)["identity"]["realm"] = "alpha-staff"
        self.assertRefused(evaluate(loaded), "issuer must be https://<host>/realms/alpha-staff")

    def test_an_unmodelled_database_kind_is_unsupported(self) -> None:
        loaded = scenario("activated")
        self.composition(loaded)["database"]["kind"] = "azure-flexible-server"
        self.assertRefused(evaluate(loaded), "database kind 'azure-flexible-server' -- unsupported")

    def test_a_connector_serving_an_unbacked_data_source_is_refused(self) -> None:
        loaded = scenario("activated")
        self.composition(loaded)["connectors"][0]["dataSources"].append("beta-primary")
        self.assertRefused(evaluate(loaded), "serves ['beta-primary'], which this composition's database does not back")

    def test_one_connector_composed_twice_is_refused(self) -> None:
        loaded = scenario("activated")
        self.composition(loaded, "beta")["connectors"][0]["id"] = "postgres-alpha"
        self.assertRefused(evaluate(loaded), "connector 'postgres-alpha' is already composed by alpha.yaml")

    def test_a_connector_serving_a_data_source_published_for_another_connector_is_refused(self) -> None:
        loaded = scenario("activated")
        sources = payload(loaded, "data-sources")
        sources[0]["connector"] = "postgres-beta"
        set_payload(loaded, "data-sources", sources)
        loaded["held"]["documents"]["data-sources"]["revision"] = 2
        self.assertRefused(
            evaluate(loaded),
            "connector 'postgres-alpha' serves 'alpha-primary', which Fabric publishes against connector 'postgres-beta'",
        )


class RegistryHandoff(Assertions):
    """One canonical registry generation, applied at the edge and in the runtime (D01-4)."""

    def test_an_edge_generation_behind_the_registry_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["compositions"]["beta.yaml"]["edge"]["registryGeneration"] = 1
        self.assertRefused(
            evaluate(loaded),
            "compositions/beta.yaml: edge applies registry generation 1, Fabric's registry is at 2",
        )

    def test_an_edge_generation_ahead_of_the_registry_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["compositions"]["alpha.yaml"]["edge"]["registryGeneration"] = 3
        self.assertRefused(evaluate(loaded), "edge applies registry generation 3, Fabric's registry is at 2")

    def test_an_edge_admitting_an_unregistered_issuer_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        changed = registry(loaded)
        changed["issuers"] = changed["issuers"][:1]
        set_registry(loaded, changed)
        loaded["held"]["registry"] = None
        self.assertRefused(evaluate(loaded), "edge admits https://identity.example.invalid/realms/beta")

    def test_an_issuer_bound_to_another_tenant_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        changed = registry(loaded)
        changed["issuers"][1]["tenant"] = "alpha"
        set_registry(loaded, changed)
        loaded["held"]["registry"] = None
        self.assertRefused(evaluate(loaded), "client 'beta' admits", "which the registry binds to tenant 'alpha'")

    def test_a_registered_issuer_no_edge_applies_leaves_identity_unmet(self) -> None:
        loaded = scenario("ready-at-zero")
        del loaded["compositions"]["beta.yaml"]
        result = evaluate(loaded)
        self.assertTrue(
            any("no composition applies https://identity.example.invalid/realms/beta" in r for r in result.unmet["identity"]),
            result.unmet,
        )

    def test_a_registry_generation_regression_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["held"]["registry"]["generation"] = 3
        self.assertRefused(evaluate(loaded), "generation 2 regresses held generation 3")

    def test_changing_a_held_generation_in_place_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["held"]["registry"]["generation"] = 2
        self.assertRefused(evaluate(loaded), "generation 2 is already held with different bytes")

    def test_an_issuer_registered_twice_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        changed = registry(loaded)
        changed["issuers"].append(dict(changed["issuers"][0]))
        set_registry(loaded, changed)
        self.assertRefused(evaluate(loaded), "registered twice")

    def test_a_registry_tenant_the_tenants_document_does_not_bind_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        tenants = payload(loaded, "tenants")[:1]
        set_payload(loaded, "tenants", tenants)
        self.assertRefused(evaluate(loaded), "names tenant 'beta', which fabric-runtime-tenants does not bind")

    def test_runtime_trusted_issuers_that_differ_from_the_registry_are_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        config = config_map(loaded)["data"]["config.toml"]
        config_map(loaded)["data"]["config.toml"] = config.replace('tenant = "beta"', 'tenant = "alpha"')
        self.assertRefused(evaluate(loaded), "[identity].trusted_issuers differs from issuer registry generation 2")

    def test_an_unmodelled_registry_field_is_unsupported(self) -> None:
        loaded = scenario("ready-at-zero")
        changed = registry(loaded)
        changed["audience"] = "saas-fabric"
        set_registry(loaded, changed)
        self.assertRefused(evaluate(loaded), "issuer-registry.json: keys must be exactly")

    def test_no_registry_with_replicas_is_refused(self) -> None:
        loaded = scenario("activated")
        del published(loaded, "tenants")["data"]["issuer-registry.json"]
        current = record(loaded)
        del current["registry"]
        set_record(loaded, current)
        loaded["held"]["registry"] = None
        self.assertRefused(
            evaluate(loaded), "identity gate is unmet: Fabric has not published a valid issuer registry generation")


class Readiness(Assertions):
    """Replicas stay at zero until documents, identity and connectors are present (D01-18)."""

    def test_scaling_the_awaiting_state_is_refused_for_every_gate(self) -> None:
        loaded = scenario("awaiting-inputs")
        deployment(loaded)["spec"]["replicas"] = 1
        self.assertRefused(
            evaluate(loaded),
            "documents gate is unmet",
            "identity gate is unmet",
            "connectors gate is unmet",
            "scaling alone cannot supply it",
        )

    def test_an_absent_replica_count_is_one_and_is_gated(self) -> None:
        loaded = scenario("awaiting-inputs")
        del deployment(loaded)["spec"]["replicas"]
        result = evaluate(loaded)
        self.assertEqual(result.replicas, 1)
        self.assertRefused(result, "replicas 1 while the documents gate is unmet")

    def test_a_non_literal_replica_count_is_unsupported(self) -> None:
        loaded = scenario("awaiting-inputs")
        deployment(loaded)["spec"]["replicas"] = "1"
        self.assertRefused(evaluate(loaded), "replica count is not a literal non-negative integer")

    def test_activation_without_runtime_connectors_is_refused(self) -> None:
        loaded = scenario("activated")
        config = config_map(loaded)["data"]["config.toml"]
        config_map(loaded)["data"]["config.toml"] = config.split("\n[[connectors]]")[0]
        self.assertRefused(evaluate(loaded), "connectors gate is unmet: the runtime config has no [[connectors]]")

    def test_activation_with_a_connector_nothing_composes_is_refused(self) -> None:
        loaded = scenario("activated")
        del loaded["compositions"]["beta.yaml"]
        changed = registry(loaded)
        changed["issuers"] = changed["issuers"][:1]
        set_registry(loaded, changed)
        loaded["held"]["registry"] = None
        self.assertRefused(evaluate(loaded), "connector 'postgres-beta' is configured but no composition deploys it")

    def test_a_connector_endpoint_the_composition_does_not_deploy_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        config = config_map(loaded)["data"]["config.toml"]
        config_map(loaded)["data"]["config.toml"] = config.replace(
            "ndc-postgres-alpha.client-alpha", "ndc-postgres-alpha.platform-system")
        self.assertRefused(evaluate(loaded), "connector 'postgres-alpha' endpoint")

    def test_a_published_data_source_whose_connector_is_not_configured_is_unmet(self) -> None:
        loaded = scenario("ready-at-zero")
        config = config_map(loaded)["data"]["config.toml"]
        config_map(loaded)["data"]["config.toml"] = config.split('\n[[connectors]]\nid = "postgres-beta"')[0]
        result = evaluate(loaded)
        self.assertClean(result)
        self.assertTrue(
            any("needs connector 'postgres-beta', which the runtime does not configure" in r for r in result.unmet["connectors"]),
            result.unmet,
        )


class RenderedRuntime(Assertions):
    """check.py's render invariant: the contract against every environment's real Deployment."""

    def render_problems(self, replicas=0, config_suffix: str = "") -> list[str]:
        base = REPOSITORY / "applications" / "core" / "saas-fabric" / "base"
        documents = [
            document
            for name in ("configmap.yaml", "deployment.yaml")
            for document in yaml.safe_load_all((base / name).read_text())
        ]
        for document in documents:
            if document["kind"] == "Deployment":
                if replicas is None:
                    del document["spec"]["replicas"]
                else:
                    document["spec"]["replicas"] = replicas
            if document["kind"] == "ConfigMap":
                document["data"]["config.toml"] += config_suffix
        with tempfile.TemporaryDirectory() as temporary:
            render = Path(temporary)
            for environment in check.ENVIRONMENTS:
                (render / environment / "applications").mkdir(parents=True)
                (render / environment / "applications" / "saas-fabric.yaml").write_text(yaml.safe_dump_all(documents))
            problems: list[str] = []
            check._DOCUMENTS.clear()
            check.check_runtime_document_contract(render, problems)
            return problems

    def test_the_repository_runtime_at_zero_passes(self) -> None:
        self.assertEqual(self.render_problems(), [])

    def test_raising_replicas_without_connectors_or_issuers_is_refused(self) -> None:
        problems = self.render_problems(replicas=1)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("no [[connectors]] and no [identity].trusted_issuers" in p for p in problems), problems)

    def test_removing_the_replica_count_is_refused(self) -> None:
        problems = self.render_problems(replicas=None)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("replicas 1 with" in p for p in problems), problems)

    def test_replicas_with_connectors_and_issuers_pass_the_static_gate(self) -> None:
        connector = (
            '\n[[connectors]]\nid = "postgres-alpha"\n'
            'endpoint = "http://ndc-postgres-alpha.client-alpha.svc.cluster.local:8080"\n'
        )
        issuer = '\n[[identity.trusted_issuers]]\nissuer = "https://identity.example.invalid/realms/alpha"\ntenant = "alpha"\n'
        self.assertEqual(self.render_problems(replicas=1, config_suffix=connector + issuer), [])

    def test_replicas_with_connectors_but_no_issuers_are_refused(self) -> None:
        connector = (
            '\n[[connectors]]\nid = "postgres-alpha"\n'
            'endpoint = "http://ndc-postgres-alpha.client-alpha.svc.cluster.local:8080"\n'
        )
        problems = self.render_problems(replicas=1, config_suffix=connector)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("with no [identity].trusted_issuers" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
