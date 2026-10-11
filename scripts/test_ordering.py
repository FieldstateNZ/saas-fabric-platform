#!/usr/bin/env python3
"""Which ordering the platform's Argo CD wiring provides, on create and on
update -- and what the RollingSync candidate would change. Platform #44.

    python3 -m unittest discover -s scripts -p 'test_*.py'

scripts/validate.sh runs exactly that, so CI runs these. Standard-library
unittest, no render, no network, no cluster.

Five groups:

  * `Model` pins scripts/ordering.py against synthetic wiring: what makes
    creation ordered, why an app-of-apps never orders an update, and what
    RollingSync needs before it orders one.
  * `CurrentManifests` reads this repository's own source -- the files
    environments/<env> and its bootstrap set actually list -- and states what
    they provide: master-instance precedes the control plane at creation and
    not on an update, and the 2026-09-22 sequence (#44) is what the wiring
    permits.
  * `RollingSyncFixture` reads scripts/fixtures/rolling-sync/ and shows the
    same scenario blocked, recovery unblocked, and the fixture's template
    agreeing with the Applications it would replace.
  * `RecordedOrdering` drives check.py's section 22 against synthetic
    renders: the current wiring passes, and adopting RollingSync, enabling
    progressive syncs or losing the health assessment each fail it.
  * `FixturesAreInert` proves nothing under scripts/ or docs/ is reachable
    from what Argo CD syncs.

What this suite does not prove: how a running applicationset-controller of
the version LucentRoot runs actually behaves. The model encodes documented
semantics; docs/ordered-updates.md lists what only an isolated sync
verification can establish.
"""
from __future__ import annotations

import copy
import posixpath
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check  # noqa: E402  (after the path insert, deliberately)
import ordering  # noqa: E402

REPOSITORY = SCRIPTS.parent
FIXTURES = SCRIPTS / "fixtures" / "rolling-sync"
ENVIRONMENTS = ("lucentroot", "production")

CHAIN = ("master-instance-credential", "master-instance", "saas-fabric-control-plane")


# ---------------------------------------------------------------------------
# Factories. Every test builds its own wiring; none shares a fixture object.
# ---------------------------------------------------------------------------

def application(name: str, wave: int, *, automated: bool = True, labels: dict | None = None) -> dict:
    sync_policy: dict = {"syncOptions": ["CreateNamespace=true"]}
    if automated:
        sync_policy["automated"] = {"prune": True, "selfHeal": True}
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": name,
            "namespace": "argocd",
            "labels": labels or {},
            "annotations": {ordering.WAVE_ANNOTATION: str(wave)},
        },
        "spec": {"project": "saas-fabric-platform", "syncPolicy": sync_policy},
    }


def argocd_cm(*, health: bool = True) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "argocd-cm", "namespace": "argocd"},
        "data": {ordering.APPLICATION_HEALTH_KEY: "hs = {}\nreturn hs\n"} if health else {},
    }


def cmd_params(*, progressive: bool) -> dict:
    data = {"server.insecure": "true"}
    if progressive:
        data[ordering.PROGRESSIVE_SYNCS_KEY] = "true"
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "argocd-cmd-params-cm", "namespace": "argocd"},
        "data": data,
    }


def rolling_set(elements: list[tuple[str, str]], steps: list[list[str]], *, wave: int = 30) -> dict:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "ApplicationSet",
        "metadata": {
            "name": "chain",
            "namespace": "argocd",
            "annotations": {ordering.WAVE_ANNOTATION: str(wave)},
        },
        "spec": {
            "goTemplate": True,
            "generators": [{"list": {"elements": [{"name": n, "step": s} for n, s in elements]}}],
            "strategy": {
                "type": "RollingSync",
                "rollingSync": {
                    "steps": [
                        {"matchExpressions": [{"key": "step", "operator": "In", "values": values}]}
                        for values in steps
                    ],
                },
            },
            "template": {
                "metadata": {"name": "{{.name}}", "labels": {"step": "{{.step}}"}},
                "spec": {"syncPolicy": {"syncOptions": ["CreateNamespace=true"]}},
            },
        },
    }


def app_of_apps(*, health_bootstrap: bool = True, health_environment: bool = True) -> ordering.Wiring:
    """The platform's current shape: every child authored by the root, automated."""
    platform = [
        argocd_cm(health=health_environment),
        cmd_params(progressive=False),
        application("master-instance-credential", 10),
        application("keycloak", 20),
        application("master-instance", 30),
        application("saas-fabric", 30),
        application("saas-fabric-control-plane", 40),
    ]
    return ordering.read_wiring(platform, [argocd_cm(health=health_bootstrap)])


def rolling(*, progressive: bool = True, steps: list[list[str]] | None = None) -> ordering.Wiring:
    platform = [
        argocd_cm(),
        cmd_params(progressive=progressive),
        application("keycloak", 20),
        rolling_set(
            [("master-instance-credential", "1"), ("master-instance", "2"), ("saas-fabric-control-plane", "3")],
            steps if steps is not None else [["1"], ["2"], ["3"]],
        ),
    ]
    return ordering.read_wiring(platform, [argocd_cm()])


def load(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


# ---------------------------------------------------------------------------
# Source resolution: what an environment's kustomization actually lists.
# ---------------------------------------------------------------------------

def listed_files(directory: Path, seen: set[Path] | None = None) -> list[Path]:
    """Every file a kustomization reaches through resources and components."""
    seen = set() if seen is None else seen
    directory = directory.resolve()
    if directory in seen:
        return []
    seen.add(directory)
    kustomization = yaml.safe_load((directory / "kustomization.yaml").read_text()) or {}
    files: list[Path] = [directory / "kustomization.yaml"]
    for entry in (kustomization.get("resources") or []) + (kustomization.get("components") or []):
        target = (directory / entry).resolve()
        if target.is_dir():
            files += listed_files(target, seen)
        else:
            files.append(target)
    return files


def source_documents(directory: Path) -> list[dict]:
    documents: list[dict] = []
    for path in listed_files(directory):
        if path.name != "kustomization.yaml" and path.suffix == ".yaml":
            documents += load(path)
    return documents


def application_source_dirs(environment: str) -> list[Path]:
    """The directories each child Application of an environment syncs from."""
    directories: list[Path] = []
    for document in source_documents(REPOSITORY / "environments" / environment):
        if document.get("kind") != "Application":
            continue
        spec = document["spec"]
        if "source" in spec:
            path = spec["source"]["path"]
            # environments/components/environment-config replaces the overlay
            # segment with the environment's name; mirror it.
            segments = path.split("/")
            if len(segments) > 4 and segments[4] == "PLACEHOLDER":
                segments[4] = environment
            directories.append(REPOSITORY / "/".join(segments))
        for source in spec.get("sources") or []:
            for value_file in source.get("helm", {}).get("valueFiles", []):
                relative = value_file.split("/", 1)[1] if value_file.startswith("$") else value_file
                directories.append((REPOSITORY / relative).parent)
    return directories


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

class Model(unittest.TestCase):

    def test_app_of_apps_orders_creation_by_wave(self):
        verdict = ordering.guarantee(app_of_apps(), "master-instance", "saas-fabric-control-plane")
        self.assertTrue(verdict.create, verdict.create_because)

    def test_app_of_apps_never_orders_an_update_of_an_automated_child(self):
        verdict = ordering.guarantee(app_of_apps(), "master-instance", "saas-fabric-control-plane")
        self.assertFalse(verdict.update)
        self.assertIn("automated sync", verdict.update_because)

    def test_a_manual_child_is_ordered_by_whoever_presses_sync(self):
        wiring = ordering.read_wiring(
            [argocd_cm(), application("a", 10), application("b", 20, automated=False)], [argocd_cm()],
        )
        verdict = ordering.guarantee(wiring, "a", "b")
        self.assertFalse(verdict.update)
        self.assertIn("by hand", verdict.update_because)

    def test_creation_needs_the_health_assessment_in_the_bootstrap_set(self):
        verdict = ordering.guarantee(
            app_of_apps(health_bootstrap=False), "master-instance", "saas-fabric-control-plane",
        )
        self.assertFalse(verdict.create)
        self.assertIn("bootstrap", verdict.create_because)

    def test_creation_needs_the_health_assessment_to_stay_reconciled(self):
        verdict = ordering.guarantee(
            app_of_apps(health_environment=False), "master-instance", "saas-fabric-control-plane",
        )
        self.assertFalse(verdict.create)

    def test_one_wave_races(self):
        verdict = ordering.guarantee(app_of_apps(), "master-instance", "saas-fabric")
        self.assertFalse(verdict.create)
        self.assertIn("within one wave", verdict.create_because)

    def test_rolling_sync_orders_updates_within_its_set(self):
        verdict = ordering.guarantee(rolling(), "master-instance", "saas-fabric-control-plane")
        self.assertTrue(verdict.update, verdict.update_because)
        self.assertTrue(verdict.create, verdict.create_because)

    def test_rolling_sync_is_ignored_without_progressive_syncs(self):
        verdict = ordering.guarantee(rolling(progressive=False), "master-instance", "saas-fabric-control-plane")
        self.assertFalse(verdict.update)
        self.assertIn(ordering.PROGRESSIVE_SYNCS_KEY, verdict.update_because)

    def test_without_progressive_syncs_nothing_syncs_the_generated_applications(self):
        # The rollback hazard: the template declares no automated sync, so
        # turning the flag off leaves the chain synced by nobody.
        wiring = rolling(progressive=False)
        for name in CHAIN:
            self.assertFalse(ordering.may_sync(wiring, name, {}, {name}), name)

    def test_rolling_sync_does_not_reach_outside_its_set(self):
        verdict = ordering.guarantee(rolling(), "keycloak", "master-instance")
        self.assertTrue(verdict.create, verdict.create_because)
        self.assertFalse(verdict.update)
        self.assertIn("outside", verdict.update_because)

    def test_reversed_steps_do_not_order(self):
        wiring = rolling(steps=[["3"], ["2"], ["1"]])
        verdict = ordering.guarantee(wiring, "master-instance", "saas-fabric-control-plane")
        self.assertFalse(verdict.update)

    def test_an_application_in_no_step_is_never_synced(self):
        wiring = rolling(steps=[["1"], ["2"]])
        verdict = ordering.guarantee(wiring, "master-instance", "saas-fabric-control-plane")
        self.assertFalse(verdict.update)
        self.assertIn("no step", verdict.update_because)
        self.assertFalse(ordering.may_sync(
            wiring, "saas-fabric-control-plane",
            {name: ordering.HEALTHY for name in CHAIN}, {"saas-fabric-control-plane"},
        ))

    def test_an_unmodelled_generator_is_reported_not_guessed(self):
        document = rolling_set([("a", "1")], [["1"]])
        document["spec"]["generators"] = [{"git": {"repoURL": "x", "directories": [{"path": "*"}]}}]
        wiring = ordering.read_wiring([argocd_cm(), document], [argocd_cm()])
        self.assertIn("chain", wiring.unmodelled)
        self.assertEqual(wiring.rolling_sets, {})

    def test_a_template_patch_with_control_flow_is_reported(self):
        document = rolling_set([("a", "1")], [["1"]])
        document["spec"]["templatePatch"] = '{{- if eq .name "a" }}\nspec: {}\n{{- end }}\n'
        wiring = ordering.read_wiring([argocd_cm(), document], [argocd_cm()])
        self.assertIn("chain", wiring.unmodelled)

    def test_a_template_patch_replaces_lists_and_merges_maps(self):
        document = rolling_set([("a", "1")], [["1"]])
        document["spec"]["template"]["spec"]["syncPolicy"]["retry"] = {"limit": 5}
        document["spec"]["generators"][0]["list"]["elements"][0]["options"] = ["Validate=false"]
        document["spec"]["templatePatch"] = "spec:\n  syncPolicy:\n    syncOptions: {{ toJson .options }}\n"
        (generated,) = ordering.generate(document)
        self.assertEqual(generated["spec"]["syncPolicy"], {"syncOptions": ["Validate=false"], "retry": {"limit": 5}})

    def test_a_step_capacity_other_than_the_default_is_reported(self):
        # 0 leaves the step for a person to sync by hand upstream; an integer
        # or a smaller percentage syncs only part of the step at a time. None
        # of that is what may_sync's step rule describes.
        for capacity in (0, 1, 2, "0%", "50%", "99%"):
            with self.subTest(maxUpdate=capacity):
                document = rolling_set([("a", "1"), ("b", "2")], [["1"], ["2"]])
                document["spec"]["strategy"]["rollingSync"]["steps"][1]["maxUpdate"] = capacity
                wiring = ordering.read_wiring([argocd_cm(), cmd_params(progressive=True), document], [argocd_cm()])
                self.assertIn("chain", wiring.unmodelled)
                self.assertIn("maxUpdate", wiring.unmodelled["chain"])
                self.assertEqual(wiring.rolling_sets, {})

    def test_the_default_step_capacity_is_modelled(self):
        document = rolling_set([("a", "1"), ("b", "2")], [["1"], ["2"]])
        for step in document["spec"]["strategy"]["rollingSync"]["steps"]:
            step["maxUpdate"] = ordering.DEFAULT_MAX_UPDATE
        wiring = ordering.read_wiring([argocd_cm(), cmd_params(progressive=True), document], [argocd_cm()])
        self.assertEqual(wiring.unmodelled, {})
        self.assertTrue(ordering.guarantee(wiring, "a", "b").update)

    def test_unmodelled_strategy_shapes_are_reported(self):
        def deletion_order(strategy):
            strategy["deletionOrder"] = "Reverse"

        def unknown_type(strategy):
            strategy["type"] = "Canary"

        def unknown_rolling_key(strategy):
            strategy["rollingSync"]["pause"] = True

        def no_steps(strategy):
            strategy["rollingSync"]["steps"] = []

        def unknown_step_key(strategy):
            strategy["rollingSync"]["steps"][0]["pause"] = True

        def empty_selector(strategy):
            strategy["rollingSync"]["steps"][0]["matchExpressions"] = []

        def unknown_expression_key(strategy):
            strategy["rollingSync"]["steps"][0]["matchExpressions"][0]["valuesFrom"] = "x"

        def rolling_sync_without_its_type(strategy):
            strategy["type"] = "AllAtOnce"

        for mutate in (
            deletion_order, unknown_type, unknown_rolling_key, no_steps,
            unknown_step_key, empty_selector, unknown_expression_key, rolling_sync_without_its_type,
        ):
            with self.subTest(shape=mutate.__name__):
                document = rolling_set([("a", "1")], [["1"]])
                mutate(document["spec"]["strategy"])
                wiring = ordering.read_wiring([argocd_cm(), document], [argocd_cm()])
                self.assertIn("chain", wiring.unmodelled)
                self.assertEqual(wiring.rolling_sets, {})

    def test_an_unmodelled_selector_operator_is_reported(self):
        document = rolling_set([("a", "1")], [["1"]])
        document["spec"]["strategy"]["rollingSync"]["steps"][0]["matchExpressions"][0]["operator"] = "Exists"
        wiring = ordering.read_wiring([argocd_cm(), document], [argocd_cm()])
        self.assertIn("chain", wiring.unmodelled)


# ---------------------------------------------------------------------------
# What this repository's own manifests provide
# ---------------------------------------------------------------------------

def current_wiring(environment: str) -> ordering.Wiring:
    return ordering.read_wiring(
        source_documents(REPOSITORY / "environments" / environment),
        source_documents(REPOSITORY / "environments" / environment / "bootstrap"),
    )


class CurrentManifests(unittest.TestCase):

    def test_every_child_reconciles_itself(self):
        for environment in ENVIRONMENTS:
            wiring = current_wiring(environment)
            self.assertEqual(wiring.rolling_sets, {}, environment)
            self.assertFalse(wiring.progressive_syncs, environment)
            for child in wiring.children.values():
                self.assertTrue(child.automated, f"{environment}: {child.name}")
                self.assertIsNone(child.generated_by, f"{environment}: {child.name}")

    def test_lucentroot_chain_is_ordered_at_creation_only(self):
        wiring = current_wiring("lucentroot")
        for before, after in zip(CHAIN, CHAIN[1:]):
            verdict = ordering.guarantee(wiring, before, after)
            self.assertTrue(verdict.create, f"{before} -> {after}: {verdict.create_because}")
            self.assertFalse(verdict.update, f"{before} -> {after}: {verdict.update_because}")

    def test_production_control_plane_is_ordered_at_creation_only(self):
        verdict = ordering.guarantee(current_wiring("production"), "keycloak", "saas-fabric-control-plane")
        self.assertTrue(verdict.create, verdict.create_because)
        self.assertFalse(verdict.update)

    def test_failed_convergence_does_not_hold_back_the_control_plane(self):
        # 2026-09-22 (#44): master-instance Degraded at 09:06:22Z, the control
        # plane synced its own change at 09:06:57Z. The wiring permits exactly
        # that.
        wiring = current_wiring("lucentroot")
        health = {"master-instance-credential": "Healthy", "master-instance": "Degraded"}
        self.assertTrue(ordering.may_sync(wiring, "saas-fabric-control-plane", health, {"saas-fabric-control-plane"}))

    def test_a_running_convergence_does_not_hold_back_the_control_plane(self):
        wiring = current_wiring("lucentroot")
        health = {"master-instance-credential": "Healthy", "master-instance": "Progressing"}
        self.assertTrue(ordering.may_sync(wiring, "saas-fabric-control-plane", health, {"saas-fabric-control-plane"}))


# ---------------------------------------------------------------------------
# The RollingSync candidate, from its synthetic fixture
# ---------------------------------------------------------------------------

def fixture_wiring(*, progressive: bool = True) -> ordering.Wiring:
    """LucentRoot's current platform with the chain moved into the fixture."""
    platform = [
        d for d in source_documents(REPOSITORY / "environments" / "lucentroot")
        if not (d.get("kind") == "Application" and d["metadata"]["name"] in CHAIN)
    ]
    platform = [
        d for d in platform
        if not (d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "argocd-cmd-params-cm")
    ]
    platform += load(FIXTURES / "applicationset.yaml")
    if progressive:
        platform += load(FIXTURES / "argocd-cmd-params-cm.yaml")
    return ordering.read_wiring(
        platform, source_documents(REPOSITORY / "environments" / "lucentroot" / "bootstrap"),
    )


class RollingSyncFixture(unittest.TestCase):

    def setUp(self):
        self.application_set = load(FIXTURES / "applicationset.yaml")[0]
        self.generated = {a["metadata"]["name"]: a for a in ordering.generate(self.application_set)}

    def test_it_is_modelled(self):
        wiring = fixture_wiring()
        self.assertEqual(wiring.unmodelled, {})
        self.assertIn("operator-sign-in", wiring.rolling_sets)

    def test_it_generates_exactly_the_chain(self):
        self.assertEqual(set(self.generated), set(CHAIN))

    def test_every_generated_application_is_in_exactly_one_step(self):
        rolling_set = fixture_wiring().rolling_sets["operator-sign-in"]
        for name in CHAIN:
            self.assertEqual(sum(name in step for step in rolling_set.steps), 1, name)

    def test_it_orders_the_chain_on_update(self):
        wiring = fixture_wiring()
        for before, after in zip(CHAIN, CHAIN[1:]):
            verdict = ordering.guarantee(wiring, before, after)
            self.assertTrue(verdict.update, f"{before} -> {after}: {verdict.update_because}")
            self.assertTrue(verdict.create, f"{before} -> {after}: {verdict.create_because}")

    def test_keycloak_stays_creation_only(self):
        verdict = ordering.guarantee(fixture_wiring(), "keycloak", "master-instance")
        self.assertTrue(verdict.create, verdict.create_because)
        self.assertFalse(verdict.update)

    def test_the_root_no_longer_waits_on_the_chain(self):
        # operator-access (wave 50) waits on the control plane at creation
        # today; once the control plane is generated, the root waits on the
        # ApplicationSet object instead.
        self.assertTrue(ordering.guarantee(
            current_wiring("lucentroot"), "saas-fabric-control-plane", "operator-access",
        ).create)
        verdict = ordering.guarantee(fixture_wiring(), "saas-fabric-control-plane", "operator-access")
        self.assertFalse(verdict.create)
        self.assertIn("ApplicationSet's own health", verdict.create_because)

    def test_failed_convergence_holds_back_the_control_plane(self):
        health = {"master-instance-credential": "Healthy", "master-instance": "Degraded"}
        self.assertFalse(ordering.may_sync(
            fixture_wiring(), "saas-fabric-control-plane", health, {"saas-fabric-control-plane"},
        ))

    def test_a_running_convergence_holds_back_the_control_plane(self):
        health = {"master-instance-credential": "Healthy", "master-instance": "Progressing"}
        self.assertFalse(ordering.may_sync(
            fixture_wiring(), "saas-fabric-control-plane", health, {"saas-fabric-control-plane"},
        ))

    def test_a_fix_to_the_failed_step_still_syncs(self):
        # Recovery: a commit that repairs master-instance makes it OutOfSync,
        # and its own step is not held back by its own Degraded health.
        health = {"master-instance-credential": "Healthy", "master-instance": "Degraded"}
        self.assertTrue(ordering.may_sync(fixture_wiring(), "master-instance", health, {"master-instance"}))

    def test_the_control_plane_proceeds_once_convergence_is_healthy(self):
        health = {name: ordering.HEALTHY for name in CHAIN}
        self.assertTrue(ordering.may_sync(
            fixture_wiring(), "saas-fabric-control-plane", health, {"saas-fabric-control-plane"},
        ))

    def test_a_zero_capacity_control_plane_step_is_not_modelled(self):
        # Review 5484630763: maxUpdate 0 on the control plane's step used to
        # read as modelled, and may_sync let the control plane through once
        # master-instance was Healthy. Upstream syncs nothing in that step.
        document = copy.deepcopy(self.application_set)
        document["spec"]["strategy"]["rollingSync"]["steps"][2]["maxUpdate"] = 0
        wiring = ordering.read_wiring(
            [argocd_cm(), cmd_params(progressive=True), application("keycloak", 20), document],
            [argocd_cm()],
        )
        self.assertIn("operator-sign-in", wiring.unmodelled)
        self.assertNotIn("operator-sign-in", wiring.rolling_sets)
        health = {name: ordering.HEALTHY for name in CHAIN}
        with self.assertRaises(ordering.NotModelled) as refused:
            ordering.may_sync(wiring, "saas-fabric-control-plane", health, {"saas-fabric-control-plane"})
        self.assertIn("operator-sign-in", str(refused.exception))
        with self.assertRaises(ordering.NotModelled):
            ordering.guarantee(wiring, "master-instance", "saas-fabric-control-plane")

    def test_the_fixture_declares_no_step_capacity(self):
        for step in self.application_set["spec"]["strategy"]["rollingSync"]["steps"]:
            self.assertNotIn("maxUpdate", step)

    def test_without_the_flag_it_orders_nothing(self):
        verdict = ordering.guarantee(fixture_wiring(progressive=False), "master-instance", "saas-fabric-control-plane")
        self.assertFalse(verdict.update)

    def test_steps_follow_the_current_waves(self):
        waves = {}
        for name in CHAIN:
            document = load(REPOSITORY / "applications" / "core" / name / "application.yaml")[0]
            waves[name] = int(document["metadata"]["annotations"][ordering.WAVE_ANNOTATION])
        rolling_set = fixture_wiring().rolling_sets["operator-sign-in"]
        by_step = sorted(CHAIN, key=rolling_set.step_of)
        self.assertEqual(by_step, sorted(CHAIN, key=waves.__getitem__))

    def test_template_agrees_with_the_applications_it_replaces(self):
        for name in CHAIN:
            current = load(REPOSITORY / "applications" / "core" / name / "application.yaml")[0]
            generated = self.generated[name]
            with self.subTest(application=name):
                self.assertEqual(generated["spec"]["project"], current["spec"]["project"])
                self.assertEqual(generated["spec"]["destination"], current["spec"]["destination"])
                self.assertEqual(generated["spec"]["source"]["repoURL"], current["spec"]["source"]["repoURL"])
                self.assertEqual(
                    generated["spec"]["source"]["path"],
                    current["spec"]["source"]["path"].replace("PLACEHOLDER", "lucentroot"),
                )
                self.assertTrue((REPOSITORY / generated["spec"]["source"]["path"]).is_dir())
                self.assertEqual(generated["metadata"]["finalizers"], current["metadata"]["finalizers"])
                current_policy = dict(current["spec"]["syncPolicy"])
                current_policy.pop("automated")
                self.assertEqual(generated["spec"]["syncPolicy"], current_policy)
                for key, value in current["metadata"]["labels"].items():
                    self.assertEqual(generated["metadata"]["labels"][key], value)

    def test_master_instance_never_skips_unchanged_resources(self):
        options = self.generated["master-instance"]["spec"]["syncPolicy"].get("syncOptions") or []
        self.assertNotIn("ApplyOutOfSyncOnly=true", options)

    def test_deleting_the_set_does_not_cascade(self):
        policy = self.application_set["spec"]["syncPolicy"]
        self.assertTrue(policy["preserveResourcesOnDeletion"])
        self.assertEqual(policy["applicationsSync"], "create-update")

    def test_template_declares_no_automated_sync(self):
        for name, generated in self.generated.items():
            self.assertNotIn("automated", generated["spec"]["syncPolicy"], name)

    def test_progressive_syncs_flag_is_the_only_controller_setting(self):
        (config_map,) = load(FIXTURES / "argocd-cmd-params-cm.yaml")
        self.assertEqual(config_map["data"], {ordering.PROGRESSIVE_SYNCS_KEY: "true"})


# ---------------------------------------------------------------------------
# check.py section 22, against synthetic renders
# ---------------------------------------------------------------------------

def recorded_problems(platform: list[dict], bootstrap: list[dict]) -> list[str]:
    with tempfile.TemporaryDirectory() as directory:
        render = Path(directory)
        for environment in check.ENVIRONMENTS:
            (render / environment).mkdir()
            (render / environment / "platform.yaml").write_text(yaml.safe_dump_all(platform))
            (render / environment / "bootstrap.yaml").write_text(yaml.safe_dump_all(bootstrap))
        problems: list[str] = []
        check.check_ordering_matches_record(render, problems)
        return problems


def current_platform() -> list[dict]:
    return source_documents(REPOSITORY / "environments" / "lucentroot")


def current_bootstrap() -> list[dict]:
    return source_documents(REPOSITORY / "environments" / "lucentroot" / "bootstrap")


class RecordedOrdering(unittest.TestCase):

    def test_current_wiring_matches_the_record(self):
        self.assertEqual(recorded_problems(current_platform(), current_bootstrap()), [])

    def test_adopting_rolling_sync_fails_until_recorded(self):
        platform = [
            d for d in current_platform()
            if not (d.get("kind") == "Application" and d["metadata"]["name"] in CHAIN)
            and not (d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "argocd-cmd-params-cm")
        ]
        platform += load(FIXTURES / "applicationset.yaml") + load(FIXTURES / "argocd-cmd-params-cm.yaml")
        problems = recorded_problems(platform, current_bootstrap())
        self.assertTrue(any("is ordered on update" in p for p in problems), problems)
        self.assertTrue(any(ordering.PROGRESSIVE_SYNCS_KEY in p for p in problems), problems)

    def test_enabling_progressive_syncs_alone_fails(self):
        platform = current_platform() + load(FIXTURES / "argocd-cmd-params-cm.yaml")
        problems = recorded_problems(platform, current_bootstrap())
        self.assertTrue(any(ordering.PROGRESSIVE_SYNCS_KEY in p for p in problems), problems)
        self.assertFalse(any("on update" in p for p in problems), problems)

    def test_losing_the_health_assessment_fails_creation(self):
        bootstrap = [d for d in current_bootstrap() if d.get("metadata", {}).get("name") != "argocd-cm"]
        problems = recorded_problems(current_platform(), bootstrap)
        self.assertTrue(any("not ordered at creation" in p for p in problems), problems)

    def test_moving_the_control_plane_into_the_convergence_wave_fails_creation(self):
        platform = current_platform()
        for document in platform:
            if document.get("kind") == "Application" and document["metadata"]["name"] == "saas-fabric-control-plane":
                document["metadata"]["annotations"][ordering.WAVE_ANNOTATION] = "30"
        problems = recorded_problems(platform, current_bootstrap())
        self.assertTrue(
            any("master-instance -> saas-fabric-control-plane is not ordered at creation" in p for p in problems),
            problems,
        )

    def test_a_zero_capacity_step_fails_the_record(self):
        document = load(FIXTURES / "applicationset.yaml")[0]
        document["spec"]["strategy"]["rollingSync"]["steps"][2]["maxUpdate"] = 0
        problems = recorded_problems(current_platform() + [document], current_bootstrap())
        self.assertTrue(
            any("operator-sign-in cannot be read for ordering" in p and "maxUpdate" in p for p in problems),
            problems,
        )

    def test_an_unreadable_application_set_fails(self):
        document = rolling_set([("a", "1")], [["1"]])
        document["spec"]["generators"] = [{"clusters": {}}]
        problems = recorded_problems(current_platform() + [document], current_bootstrap())
        self.assertTrue(any("cannot be read for ordering" in p for p in problems), problems)


# ---------------------------------------------------------------------------
# Nothing here reaches what Argo CD syncs
# ---------------------------------------------------------------------------

INERT = (REPOSITORY / "scripts", REPOSITORY / "docs")


def _within(path: Path, directory: Path) -> bool:
    return posixpath.commonpath([str(path.resolve()), str(directory.resolve())]) == str(directory.resolve())


class FixturesAreInert(unittest.TestCase):

    def test_no_environment_lists_a_file_under_scripts_or_docs(self):
        for environment in ENVIRONMENTS:
            for directory in (
                REPOSITORY / "environments" / environment,
                REPOSITORY / "environments" / environment / "bootstrap",
            ):
                for path in listed_files(directory):
                    for inert in INERT:
                        self.assertFalse(_within(path, inert), f"{directory} lists {path}")

    def test_no_application_syncs_from_scripts_or_docs(self):
        for environment in ENVIRONMENTS:
            for directory in application_source_dirs(environment):
                for inert in INERT:
                    self.assertFalse(_within(directory, inert), f"{environment}: {directory}")
                if (directory / "kustomization.yaml").is_file():
                    for path in listed_files(directory):
                        for inert in INERT:
                            self.assertFalse(_within(path, inert), f"{environment}: {directory} lists {path}")

    def test_the_root_application_syncs_only_its_environment(self):
        for environment in ENVIRONMENTS:
            roots = [
                d for d in source_documents(REPOSITORY / "environments" / environment / "bootstrap")
                if d.get("kind") == "Application"
            ]
            self.assertEqual([d["metadata"]["name"] for d in roots], [ordering.ROOT_APPLICATION])
            self.assertTrue(roots[0]["spec"]["source"]["path"].startswith("environments/"))


if __name__ == "__main__":
    unittest.main()
