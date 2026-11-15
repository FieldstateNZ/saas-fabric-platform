#!/usr/bin/env python3
"""The scaled-runtime gate for the Data API JWT policy.

Zero replicas is the held state and must pass even with no policy. Above zero,
a route that forwards to the runtime and is not only a probe must be targeted
by a JWT SecurityPolicy, and a route that also matches /health or /ready must
not be. A policy left on a PathPrefix `/` catch-all fails that second rule.
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check  # noqa: E402


def deployment(replicas: int) -> dict:
    labels = {"app.kubernetes.io/name": "saas-fabric"}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "saas-fabric", "namespace": "platform-system"},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": "saas-fabric",
                            "livenessProbe": {"httpGet": {"path": "/health", "port": "http"}},
                            "readinessProbe": {"httpGet": {"path": "/ready", "port": "http"}},
                        }
                    ]
                }
            },
        },
    }


def route(name: str, path: str, path_type: str = "PathPrefix", label: str | None = None, extra: list | None = None) -> dict:
    labels = {"fieldstate.nz/data-api": label} if label else {}
    matches = [{"path": {"type": path_type, "value": path}}]
    for item in extra or []:
        matches.append({"path": {"type": "Exact", "value": item}})
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": "platform-system", "labels": labels},
        "spec": {
            "rules": [
                {"matches": matches, "backendRefs": [{"name": "saas-fabric", "port": 80}]}
            ]
        },
    }


def network_policy(selector: dict | None = None, peer: dict | None = None, namespace: str | None = "platform-system") -> dict:
    source: dict = {
        "podSelector": {
            "matchLabels": peer or {
                "app.kubernetes.io/component": "proxy",
                "app.kubernetes.io/managed-by": "envoy-gateway",
                "gateway.envoyproxy.io/owning-gateway-name": "platform",
            }
        }
    }
    if namespace is not None:
        source["namespaceSelector"] = {"matchLabels": {"kubernetes.io/metadata.name": namespace}}
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "saas-fabric-from-gateway", "namespace": "platform-system"},
        "spec": {
            "podSelector": {"matchLabels": selector or {"app.kubernetes.io/name": "saas-fabric"}},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [source]}],
        },
    }


def extension(names: list[str]) -> dict:
    return {
        "apiVersion": "gateway.envoyproxy.io/v1alpha1",
        "kind": "EnvoyExtensionPolicy",
        "metadata": {"name": "saas-fabric-data-headers", "namespace": "platform-system"},
        "spec": {
            "targetRefs": [
                {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": name}
                for name in names
            ],
            "lua": [
                {"type": "Inline", "inline": "x-tenant-id"},
                {"type": "Inline", "inline": 'root_key(payload, "exp")'},
            ],
        },
    }


def policy(labels: dict | None = None, names: list[str] | None = None) -> dict:
    spec: dict = {"jwt": {"providers": [{"name": "alpha"}]}}
    if names is not None:
        spec["targetRefs"] = [
            {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": name} for name in names
        ]
    else:
        spec["targetSelectors"] = [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "matchLabels": labels or {"fieldstate.nz/data-api": "tenant"},
            }
        ]
    return {
        "apiVersion": "gateway.envoyproxy.io/v1alpha1",
        "kind": "SecurityPolicy",
        "metadata": {"name": "saas-fabric-data", "namespace": "platform-system"},
        "spec": spec,
    }


def render(documents: list[dict]) -> Path:
    root = Path(tempfile.mkdtemp())
    for environment in ("lucentroot", "production"):
        directory = root / environment / "applications"
        directory.mkdir(parents=True)
        payload = yaml.safe_dump_all(documents)
        (directory / "saas-fabric.yaml").write_text(payload)
    return root


def problems_of(documents: list[dict]) -> list[str]:
    found: list[str] = []
    check.check_data_api_edge_when_scaled(render(documents), found)
    return found


class DataApiEdgeGate(unittest.TestCase):
    def test_zero_replicas_passes_with_no_policy(self) -> None:
        self.assertEqual(problems_of([deployment(0), route("saas-fabric", "/")]), [])

    def test_scaled_without_a_policy_fails(self) -> None:
        found = problems_of([deployment(1), route("saas-fabric", "/v1/data", label="tenant")])
        self.assertTrue(any("no JWT SecurityPolicy" in item for item in found), found)

    def test_scaled_split_passes(self) -> None:
        documents = [
            deployment(1),
            route("saas-fabric", "/v1/data", label="tenant"),
            route("saas-fabric-probes", "/health", "Exact", extra=["/ready"]),
            policy(),
            network_policy(),
            extension(["saas-fabric", "saas-fabric-probes"]),
        ]
        self.assertEqual(problems_of(documents), [])

    def test_policy_on_the_catch_all_fails(self) -> None:
        documents = [
            deployment(1),
            route("saas-fabric", "/", label="tenant"),
            route("saas-fabric-probes", "/health", "Exact", extra=["/ready"]),
            policy(),
        ]
        found = problems_of(documents)
        self.assertTrue(any("matches /health" in item for item in found), found)
        self.assertTrue(any("matches /ready" in item for item in found), found)

    def test_unlabelled_data_route_fails_when_scaled(self) -> None:
        documents = [
            deployment(1),
            route("saas-fabric", "/v1/data", label="tenant"),
            route("saas-fabric-extra", "/v1/other"),
            route("saas-fabric-probes", "/health", "Exact", extra=["/ready"]),
            policy(),
        ]
        found = problems_of(documents)
        self.assertTrue(any("HTTPRoute/saas-fabric-extra" in item for item in found), found)

    def test_policy_on_the_probe_route_fails(self) -> None:
        documents = [
            deployment(1),
            route("saas-fabric", "/v1/data", label="tenant"),
            route("saas-fabric-probes", "/health", "Exact", extra=["/ready"], label="tenant"),
            policy(),
        ]
        found = problems_of(documents)
        self.assertTrue(any("leaves alone" in item for item in found), found)

    def test_network_policy_on_a_test_label_fails(self) -> None:
        documents = [
            deployment(1),
            route("saas-fabric", "/v1/data", label="tenant"),
            route("saas-fabric-probes", "/health", "Exact", extra=["/ready"]),
            policy(),
            network_policy(selector={"app": "data-api-stand-in"}),
        ]
        found = problems_of(documents)
        self.assertTrue(any("NetworkPolicy" in item for item in found), found)

    def test_network_policy_that_also_admits_the_namespace_fails(self) -> None:
        loose = network_policy()
        loose["spec"]["ingress"].append({"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "platform-system"}}}]})
        documents = [
            deployment(1),
            route("saas-fabric", "/v1/data", label="tenant"),
            route("saas-fabric-probes", "/health", "Exact", extra=["/ready"]),
            policy(),
            loose,
        ]
        found = problems_of(documents)
        self.assertTrue(any("NetworkPolicy" in item for item in found), found)

    def test_target_ref_covers_a_named_route(self) -> None:
        documents = [
            deployment(1),
            route("saas-fabric", "/v1/data"),
            route("saas-fabric-probes", "/health", "Exact", extra=["/ready"]),
            policy(names=["saas-fabric"]),
            network_policy(),
            extension(["saas-fabric", "saas-fabric-probes"]),
        ]
        self.assertEqual(problems_of(documents), [])

    def test_missing_lua_on_a_forwarding_route_fails(self) -> None:
        documents = [
            deployment(1),
            route("saas-fabric", "/v1/data", label="tenant"),
            route("saas-fabric-probes", "/health", "Exact", extra=["/ready"]),
            policy(),
            network_policy(),
            extension(["saas-fabric"]),
        ]
        found = problems_of(documents)
        self.assertTrue(any("saas-fabric-probes" in item and "identity headers" in item for item in found), found)
        self.assertTrue(any("saas-fabric-probes" in item and "exp" in item for item in found), found)

    def test_selector_form_lua_covers_labelled_routes(self) -> None:
        data = route("saas-fabric", "/v1/data", label="tenant")
        probes = route("saas-fabric-probes", "/health", "Exact", extra=["/ready"])
        data["metadata"]["labels"]["fieldstate.nz/strip-identity"] = "edge"
        probes["metadata"]["labels"] = {"fieldstate.nz/strip-identity": "edge"}
        selected = extension(["saas-fabric", "saas-fabric-probes"])
        selected["spec"].pop("targetRefs")
        selected["spec"]["targetSelectors"] = [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "matchLabels": {"fieldstate.nz/strip-identity": "edge"},
            }
        ]
        documents = [deployment(1), data, probes, policy(), network_policy(), selected]
        self.assertEqual(problems_of(documents), [])

    def test_comment_only_lua_marker_fails(self) -> None:
        noted = extension(["saas-fabric", "saas-fabric-probes"])
        noted["spec"]["lua"] = [
            {"type": "Inline", "inline": "-- x-tenant-id"},
            {"type": "Inline", "inline": '-- root_key(payload, "exp")'},
        ]
        documents = [
            deployment(1),
            route("saas-fabric", "/v1/data", label="tenant"),
            route("saas-fabric-probes", "/health", "Exact", extra=["/ready"]),
            policy(),
            network_policy(),
            noted,
        ]
        found = problems_of(documents)
        self.assertTrue(any("identity headers" in item for item in found), found)
        self.assertTrue(any("exp" in item for item in found), found)


def gateway_with_fabric_host(hostname: str) -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": "platform", "namespace": "platform-system"},
        "spec": {
            "gatewayClassName": "saas-fabric",
            "listeners": [{"name": "fabric", "hostname": hostname, "protocol": "HTTP", "port": 80}],
        },
    }


def named_route(name: str, *, section: str, host: str, namespace: str = "identity") -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "parentRefs": [{"name": "platform", "namespace": "platform-system", "sectionName": section}],
            "hostnames": [host],
            "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}]}],
        },
    }


def fabric_problems(documents: list[dict]) -> list[str]:
    found: list[str] = []
    check.check_fabric_host_is_only_the_runtime_routes(render(documents), found)
    return found


class FabricHostIsOnlyTheRuntimeRoutes(unittest.TestCase):
    def test_the_two_fabric_routes_pass(self) -> None:
        documents = [
            gateway_with_fabric_host("fabric.from-overlay.test"),
            named_route("saas-fabric", section="fabric", host="fabric.from-overlay.test", namespace="platform-system"),
            named_route(
                "saas-fabric-probes", section="fabric", host="fabric.from-overlay.test", namespace="platform-system"
            ),
            named_route("keycloak", section="https", host="auth.fieldstate.nz"),
        ]
        self.assertEqual(fabric_problems(documents), [])

    def test_another_route_on_the_fabric_listener_fails(self) -> None:
        documents = [
            named_route("saas-fabric", section="fabric", host="fabric.fieldstate.nz", namespace="platform-system"),
            named_route("saas-fabric-probes", section="fabric", host="fabric.fieldstate.nz", namespace="platform-system"),
            named_route("keycloak", section="fabric", host="auth.fieldstate.nz"),
        ]
        found = fabric_problems(documents)
        self.assertTrue(any("attaches to listener 'fabric'" in item and "keycloak" in item for item in found), found)

    def test_another_route_naming_a_fabric_hostname_fails(self) -> None:
        documents = [
            named_route("client-shell", section="http", host="fabric.lucentroot.internal", namespace="acme"),
        ]
        found = fabric_problems(documents)
        self.assertTrue(any("fabric.lucentroot.internal" in item and "client-shell" in item for item in found), found)

    def test_a_hostname_the_overlay_sets_on_the_listener_fails_on_another_route(self) -> None:
        documents = [
            gateway_with_fabric_host("fabric.from-overlay.test"),
            named_route("client-shell", section="http", host="fabric.from-overlay.test", namespace="acme"),
        ]
        found = fabric_problems(documents)
        self.assertTrue(any("fabric.from-overlay.test" in item and "client-shell" in item for item in found), found)


if __name__ == "__main__":
    unittest.main()
