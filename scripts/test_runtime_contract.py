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


def set_manifest(loaded: dict, name: str, **fields) -> None:
    data = published(loaded, name)["data"]
    manifest = json.loads(data[f"{name}.manifest.json"])
    manifest.update(fields)
    data[f"{name}.manifest.json"] = json.dumps(manifest)


def set_payload(loaded: dict, name: str, payload) -> None:
    published(loaded, name)["data"][f"{name}.json"] = json.dumps(payload, indent=2) + "\n"


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
        for path in FIXTURES.rglob("issuer-registry.json"):
            for registration in json.loads(path.read_text())["issuers"]:
                self.assertTrue(registration["issuer"].startswith("https://identity.example.invalid/"))


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
        loaded["published"] = [c for c in loaded["published"] if c["metadata"]["name"] != "fabric-runtime-catalog"]
        result = evaluate(loaded)
        self.assertClean(result)
        self.assertIn("missing required document catalog (fabric-runtime-catalog)", result.unmet["documents"])

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
        self.assertRefused(evaluate(loaded), "data keys must be exactly catalog.json and catalog.manifest.json")

    def test_a_missing_manifest_is_an_unsupported_shape(self) -> None:
        loaded = scenario("activated")
        del published(loaded, "tenants")["data"]["tenants.manifest.json"]
        self.assertRefused(evaluate(loaded), "data keys must be exactly tenants.json and tenants.manifest.json")

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
        set_manifest(loaded, "tenants", contract_version=2)
        self.assertRefused(evaluate(loaded), "contract_version 2, contract says 1")

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
        loaded["registry"]["issuers"] = loaded["registry"]["issuers"][:1]
        loaded["held"]["registry"] = None
        self.assertRefused(evaluate(loaded), "edge admits https://identity.example.invalid/realms/beta")

    def test_an_issuer_bound_to_another_tenant_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["registry"]["issuers"][1]["tenant"] = "alpha"
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
        self.assertRefused(evaluate(loaded), "generation 2 is already held with different registrations")

    def test_an_issuer_registered_twice_is_refused(self) -> None:
        loaded = scenario("ready-at-zero")
        loaded["registry"]["issuers"].append(dict(loaded["registry"]["issuers"][0]))
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
        loaded["registry"]["audience"] = "saas-fabric"
        self.assertRefused(evaluate(loaded), "issuer-registry.json: keys must be exactly")

    def test_no_registry_with_replicas_is_refused(self) -> None:
        loaded = scenario("activated")
        loaded["registry"] = None
        self.assertRefused(
            evaluate(loaded), "identity gate is unmet: Fabric has not produced an issuer registry generation")


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
        loaded["registry"]["issuers"] = loaded["registry"]["issuers"][:1]
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
