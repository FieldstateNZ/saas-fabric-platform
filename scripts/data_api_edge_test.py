#!/usr/bin/env python3
"""Prove the Data API JWT edge on a disposable kind cluster.

Applies this repo's HTTPRoutes and SecurityPolicy, with the sentinel issuer
replaced by two pinned test issuers, in front of a stand-in backend. The
runtime Deployment is not applied and its replica count is not changed.

The Lua filter, which Envoy Gateway 1.9 orders after jwt_authn, rejects a
verified token when exp is missing, when now > exp + 30, when nbf is more
than 30s in the future, or when the header alg is outside the JWKS allow-list.
The test dumps the live HTTP filter chain and stops if Lua is not after
jwt_authn. It exits non-zero when any REQUIREMENT NOT MET line was recorded.

A BackendTrafficPolicy responseOverride tries to map a local 500 to 503 and
to replace local 401/403 bodies with text that does not name the check. The
test records the status that actually comes back. An unreachable JWKS and a
wrong audience must not be forwarded: the stand-in request counter stays put.

    python3 scripts/data_api_edge_test.py

Requires kind, kubectl, helm, openssl and Docker. Set DATA_API_EDGE_KEEP=1 to
leave the cluster up.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "applications" / "core" / "saas-fabric" / "base"
CLUSTER = "data-api-edge"
NAMESPACE = "platform-system"
FOREIGN = "other-product"
HOST = "fabric.invalid"
GATEWAY = ROOT / "applications" / "core" / "platform-gateway" / "base" / "gateway.yaml"
AUDIENCE = "saas-fabric-data-api"
ISSUER_ALPHA = "https://identity.example.invalid/realms/alpha"
ISSUER_BETA = "https://identity.example.invalid/realms/beta"
ISSUER_GAMMA = "https://identity.example.invalid/realms/gamma"
EG_CHART = "oci://docker.io/envoyproxy/gateway-helm"
EG_VERSION = "v1.9.0"
EG_NAMESPACE = "platform-system"
CALICO_VERSION = "v3.29.3"
LOCAL_PORT = 18080

# ADR 0019 G8/G13. Not what the test expects the gateway to do.
ADR_JWKS_FAILURE = 503

GAPS: list[str] = []
OBSERVATIONS: list[str] = []


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, text=True, **kwargs)


def capture(command: list[str]) -> str:
    return subprocess.check_output(command, text=True)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def i2b(value: int) -> bytes:
    length = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(length, "big")


def jwk(pem: Path, kid: str, alg: str | None = "RS256") -> dict:
    text = capture(["openssl", "rsa", "-in", str(pem), "-noout", "-modulus"])
    modulus = int(text.strip().split("=", 1)[1], 16)
    document = {
        "kty": "RSA",
        "use": "sig",
        "kid": kid,
        "n": b64u(i2b(modulus)),
        "e": b64u(i2b(65537)),
    }
    if alg is not None:
        document["alg"] = alg
    return document


def mint(header: dict, payload: dict, key: Path | None = None, secret: bytes | None = None, digest: str = "sha256") -> str:
    header = {key: value for key, value in header.items() if value is not None}
    payload = {key: value for key, value in payload.items() if value is not None}
    signing = ".".join(
        b64u(json.dumps(document, separators=(",", ":")).encode())
        for document in (header, payload)
    )
    if secret is not None:
        signature = hmac.new(secret, signing.encode(), hashlib.sha256).digest()
    elif key is not None:
        signature = subprocess.check_output(
            ["openssl", "dgst", f"-{digest}", "-sign", str(key)], input=signing.encode()
        )
    else:
        signature = b""
    return signing + "." + b64u(signature)


def _raw_token(key: Path, claims_body: str) -> str:
    """Sign a payload whose claim text is written literally, not by json.dumps."""
    header = json.dumps({"alg": "RS256", "kid": "alpha", "typ": "JWT"}, separators=(",", ":"))
    payload = (
        '{"iss":"' + ISSUER_ALPHA + '","aud":"' + AUDIENCE + '",'
        + claims_body + ',"sub":"user"}'
    )
    return sign_json(header, payload, key)


def sign_json(header_json: str, payload_json: str, key: Path, digest: str = "sha256") -> str:
    """Sign already-encoded JSON so a test can repeat a key or nest one."""
    signing = ".".join(b64u(part.encode()) for part in (header_json, payload_json))
    signature = subprocess.check_output(
        ["openssl", "dgst", f"-{digest}", "-sign", str(key)], input=signing.encode()
    )
    return signing + "." + b64u(signature)


def kube(*args: str, input_text: str | None = None) -> str:
    return subprocess.check_output(["kubectl", *args], input=input_text, text=True)


def kube_apply(documents: list[dict]) -> None:
    kube("apply", "-f", "-", input_text=yaml.safe_dump_all(documents))


def kind_node() -> str:
    return f"{CLUSTER}-control-plane"


def docker_base() -> list[str]:
    if subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return ["docker"]
    return ["sudo", "docker"]


def preload(images: list[str]) -> None:
    """The kind node in this environment cannot reach Docker Hub. Load from the host."""
    base = docker_base()
    node = kind_node()
    for image in images:
        subprocess.run([*base, "pull", image], check=True)
        saved = subprocess.run([*base, "save", image], check=True, stdout=subprocess.PIPE).stdout
        subprocess.run(
            ["sudo", "docker", "exec", "-i", node, "ctr", "--namespace=k8s.io", "images", "import", "-"],
            input=saved,
            check=True,
        )


def ensure_cluster() -> None:
    names = capture(["kind", "get", "clusters"]).split()
    reuse = False
    if CLUSTER in names:
        kubeconfig = Path(tempfile.mkdtemp()) / "kubeconfig"
        kubeconfig.write_text(capture(["kind", "get", "kubeconfig", "--name", CLUSTER]))
        os.environ["KUBECONFIG"] = str(kubeconfig)
        try:
            kube("get", "ds", "-n", "kube-system", "calico-node")
            reuse = True
        except subprocess.CalledProcessError:
            subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER], check=False)
    if not reuse:
        config = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        config.write(
            "kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\n"
            "networking:\n  disableDefaultCNI: true\n  podSubnet: 192.168.0.0/16\n"
            "nodes:\n- role: control-plane\n  image: kindest/node:v1.32.2\n"
        )
        config.close()
        run(["kind", "create", "cluster", "--name", CLUSTER, "--config", config.name])
        kubeconfig = Path(tempfile.mkdtemp()) / "kubeconfig"
        kubeconfig.write_text(capture(["kind", "get", "kubeconfig", "--name", CLUSTER]))
        os.environ["KUBECONFIG"] = str(kubeconfig)
        install_calico()
    kube("wait", "--for=condition=Ready", "node", "--all", "--timeout=240s")
    ensure_service_routing()


def install_calico() -> None:
    manifest = capture(["curl", "-fsSL", f"https://raw.githubusercontent.com/projectcalico/calico/{CALICO_VERSION}/manifests/calico.yaml"])
    images = sorted(set(re_images(manifest)))
    preload(images)
    kube("apply", "-f", "-", input_text=manifest)
    # This kernel rejects ipset, so Calico's iptables dataplane cannot program
    # policy. The nftables dataplane does not use ipset. Kernels that do have
    # ipset still enforce policy in this mode.
    kube(
        "set", "env", "ds/calico-node", "-n", "kube-system",
        "FELIX_NFTABLESMODE=Enabled", "CALICO_IPV4POOL_IPIP=Never",
    )
    kube("rollout", "status", "ds/calico-node", "-n", "kube-system", "--timeout=240s")


def re_images(manifest: str) -> list[str]:
    import re
    return re.findall(r"image:\s*(\S+)", manifest)


def ensure_service_routing() -> None:
    """kind's iptables proxy needs the statistic match. This kernel does not have it.

    The failure shows up in the kube-proxy log only after the first sync, which
    is later than this check on a fresh node. Switch whenever the mode is not
    already nftables.
    """
    config = yaml.safe_load(kube("get", "cm", "-n", "kube-system", "kube-proxy", "-o", "jsonpath={.data.config\\.conf}"))
    if config.get("mode") == "nftables":
        return
    config["mode"] = "nftables"
    patch = json.dumps({"data": {"config.conf": yaml.safe_dump(config)}})
    kube("patch", "cm", "-n", "kube-system", "kube-proxy", "--type", "merge", "-p", patch)
    kube("delete", "pod", "-n", "kube-system", "-l", "k8s-app=kube-proxy")
    kube("rollout", "status", "ds/kube-proxy", "-n", "kube-system", "--timeout=120s")


def ensure_gateway() -> None:
    listed = capture(["helm", "list", "-n", EG_NAMESPACE, "-o", "json"])
    if "eg" not in {item.get("name") for item in json.loads(listed or "[]")}:
        preload([
            "envoyproxy/gateway:v1.9.0",
            "envoyproxy/envoy:distroless-v1.39.0",
            "python:3.12-slim",
        ])
    # upgrade --install so a cluster left from an earlier run picks up enableLua.
    run(
        [
            "helm", "upgrade", "--install", "eg", EG_CHART, "--version", EG_VERSION,
            "-n", EG_NAMESPACE, "--create-namespace",
            "--set", "config.envoyGateway.extensionApis.enableLua=true",
            "--wait", "--timeout", "10m",
        ]
    )
    kube("rollout", "status", f"deployment/{gateway_deployment()}", "-n", EG_NAMESPACE, "--timeout=240s")


def gateway_deployment() -> str:
    data = json.loads(kube("get", "deploy", "-n", EG_NAMESPACE, "-o", "json"))
    names = []
    for item in data["items"]:
        containers = item["spec"]["template"]["spec"].get("containers") or []
        if any(container.get("name") == "envoy-gateway" for container in containers):
            names.append(item["metadata"]["name"])
    if len(names) != 1:
        raise SystemExit(f"envoy gateway deployments: {names}")
    return names[0]


def repo_gateway() -> dict:
    """The base Gateway, including the fabric listener and its namespace selector."""
    document = yaml.safe_load(GATEWAY.read_text())
    document.setdefault("metadata", {})["namespace"] = NAMESPACE
    listeners = document["spec"]["listeners"]
    fabric = next(listener for listener in listeners if listener["name"] == "fabric")
    if fabric.get("hostname") != HOST:
        raise SystemExit(f"base fabric listener hostname is {fabric.get('hostname')}, test pins {HOST}")
    allowed = fabric["allowedRoutes"]
    selector = allowed["namespaces"]
    if selector.get("from") != "Selector":
        raise SystemExit(f"fabric listener allowedRoutes is {selector.get('from')}")
    labels = (selector.get("selector") or {}).get("matchLabels") or {}
    if labels.get("kubernetes.io/metadata.name") != NAMESPACE:
        raise SystemExit(f"fabric listener selector is {labels}")
    kinds = [item.get("kind") for item in allowed.get("kinds") or []]
    if kinds != ["HTTPRoute"]:
        raise SystemExit(f"fabric listener kinds are {kinds}")
    return document


def repo_routes() -> list[dict]:
    documents = [item for item in yaml.safe_load_all(BASE.joinpath("httproute.yaml").read_text()) if item]
    for document in documents:
        document.setdefault("metadata", {})["namespace"] = NAMESPACE
        for rule in document["spec"]["rules"]:
            for backend in rule["backendRefs"]:
                backend["name"] = "data-api-stand-in"
    return documents


def repo_document(name: str) -> dict:
    document = yaml.safe_load((BASE / name).read_text())
    document.setdefault("metadata", {})["namespace"] = NAMESPACE
    return document


def repo_policy(alpha_uri: str, beta_uri: str) -> dict:
    document = yaml.safe_load(BASE.joinpath("securitypolicy.yaml").read_text())
    document.setdefault("metadata", {})["namespace"] = NAMESPACE
    sample = document["spec"]["jwt"]["providers"][0]
    if sample.get("audiences") != [AUDIENCE]:
        raise SystemExit(f"repo policy audience is {sample.get('audiences')}, test pins {[AUDIENCE]}")
    extract = sample["extractFrom"]
    document["spec"]["jwt"]["providers"] = [
        provider("alpha", ISSUER_ALPHA, alpha_uri, extract),
        provider("beta", ISSUER_BETA, beta_uri, extract),
    ]
    return document


def provider(name: str, issuer: str, uri: str, extract: dict, service: str = "data-api-jwks") -> dict:
    return {
        "name": name,
        "issuer": issuer,
        "audiences": [AUDIENCE],
        "extractFrom": extract,
        "remoteJWKS": {
            "uri": uri,
            # Short cache so a reused cluster does not keep verifying against
            # the previous run's keys.
            "cacheDuration": "1s",
            "backendRefs": [{"name": service, "port": 80}],
        },
    }


def stand_in_manifests(jwks_alpha: list[dict], jwks_beta: list[dict]) -> list[dict]:
    server = textwrap.dedent(
        """
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            count = 0
            def do_GET(self):
                Handler.count += 1
                body = json.dumps({
                    "path": self.path,
                    "count": Handler.count,
                    "headers": {key: value for key, value in self.headers.items()},
                }).encode()
                status = 500 if self.path.split("?", 1)[0].endswith("/boom") else 200
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                return
        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
        """
    )
    jwks = textwrap.dedent(
        """
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                name = {"/jwks/alpha": "alpha.json", "/jwks/beta": "beta.json"}.get(self.path.split("?", 1)[0])
                if name is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = Path("/keys", name).read_bytes()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                return
        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
        """
    )
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": NAMESPACE,
                "labels": {
                    "fieldstate.nz/gateway-access": "true",
                    # The API server also sets this to the namespace name. Stating
                    # it keeps the fabric listener's selector true on apply.
                    "kubernetes.io/metadata.name": NAMESPACE,
                },
            },
        },
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "GatewayClass",
            "metadata": {"name": "saas-fabric"},
            "spec": {"controllerName": "gateway.envoyproxy.io/gatewayclass-controller"},
        },
        repo_gateway(),
        configmap("data-api-stand-in", {"server.py": server}),
        configmap("data-api-jwks", {"alpha.json": json.dumps({"keys": jwks_alpha}), "beta.json": json.dumps({"keys": jwks_beta}), "server.py": jwks}),
        workload(
            "data-api-stand-in",
            "python:3.12-slim",
            ["python", "/app/server.py"],
            {"app": "data-api-stand-in", "app.kubernetes.io/name": "saas-fabric"},
        ),
        workload("data-api-jwks", "python:3.12-slim", ["python", "/app/server.py"], {"app": "data-api-jwks"}, keys=True),
        service("data-api-stand-in"),
        service("data-api-jwks"),
        service("data-api-jwks-down", selector={"app": "data-api-jwks-down"}),
    ]


def configmap(name: str, data: dict) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": NAMESPACE}, "data": data}


def service(name: str, selector: dict | None = None) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"selector": selector or {"app": name}, "ports": [{"name": "http", "port": 80, "targetPort": 8080}]},
    }


def workload(name: str, image: str, command: list[str], labels: dict, keys: bool = False) -> dict:
    volume_mounts = [{"name": "app", "mountPath": "/app"}]
    volumes = [{"name": "app", "configMap": {"name": name}}]
    if keys:
        volume_mounts.append({"name": "keys", "mountPath": "/keys"})
        volumes.append({"name": "keys", "configMap": {"name": name}})
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": image,
                            "command": command,
                            "ports": [{"containerPort": 8080}],
                            "volumeMounts": volume_mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def response_policy_for(name: str, route: str) -> dict:
    """Same overrides as the repo policy, attached to one route by name."""
    document = repo_document("responsepolicy.yaml")
    document["metadata"]["name"] = name
    document["spec"].pop("targetSelectors", None)
    document["spec"]["targetRefs"] = [
        {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": route}
    ]
    return document


def unreachable_route(extract: dict) -> list[dict]:
    return [
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": "jwks-unreachable", "namespace": NAMESPACE},
            "spec": {
                "parentRefs": [{"name": "platform", "namespace": NAMESPACE, "sectionName": "http"}],
                "hostnames": ["jwks-down.invalid"],
                "rules": [
                    {
                        "matches": [{"path": {"type": "PathPrefix", "value": "/v1/data"}}],
                        "backendRefs": [{"name": "data-api-stand-in", "port": 80}],
                    }
                ],
            },
        },
        {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "SecurityPolicy",
            "metadata": {"name": "jwks-unreachable", "namespace": NAMESPACE},
            "spec": {
                "targetRefs": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": "jwks-unreachable"}],
                "jwt": {
                    "providers": [
                        provider(
                            "gamma",
                            ISSUER_GAMMA,
                            "http://data-api-jwks-down.platform-system.svc.cluster.local/jwks",
                            extract,
                            service="data-api-jwks-down",
                        )
                    ]
                },
            },
        },
        response_policy_for("jwks-unreachable-responses", "jwks-unreachable"),
    ]


def wait_one_pod(name: str) -> None:
    """One process owns the stand-in counter. A terminating pod still accepts pooled connections."""
    deadline = time.time() + 120
    while time.time() < deadline:
        pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-l", f"app={name}", "-o", "json"))["items"]
        terminating = [pod["metadata"]["name"] for pod in pods if pod["metadata"].get("deletionTimestamp")]
        if terminating:
            kube("delete", "pod", "-n", NAMESPACE, "--force", "--grace-period=0", *terminating)
            time.sleep(1)
            continue
        ready = [
            pod for pod in pods
            if all(container.get("ready") for container in pod.get("status", {}).get("containerStatuses") or [])
        ]
        endpoints = json.loads(kube("get", "endpoints", "-n", NAMESPACE, name, "-o", "json"))
        addresses = [
            address
            for subset in endpoints.get("subsets") or []
            for address in subset.get("addresses") or []
        ]
        if len(pods) == 1 and len(ready) == 1 and len(addresses) == 1:
            return
        time.sleep(1)
    raise SystemExit(f"{name} did not settle to one ready endpoint")


def recycle_proxy() -> None:
    """Drop Envoy's upstream pool so it cannot keep talking to a deleted stand-in."""
    kube("delete", "pod", "-n", EG_NAMESPACE, "-l", "app.kubernetes.io/component=proxy", "--wait=true")
    deadline = time.time() + 180
    while time.time() < deadline:
        pods = json.loads(
            kube("get", "pods", "-n", EG_NAMESPACE, "-l", "app.kubernetes.io/component=proxy", "-o", "json")
        )["items"]
        live = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
        ready = [
            pod for pod in live
            if all(container.get("ready") for container in pod.get("status", {}).get("containerStatuses") or [])
        ]
        if len(live) == 1 and len(ready) == 1:
            return
        time.sleep(2)
    raise SystemExit("envoy proxy did not return after the pool was dropped")


def foreign_route() -> list[dict]:
    """A gateway-access namespace, not platform-system, claiming a longer Data API path.

    This is the attachment the product listener used to allow: same hostname as
    the fabric routes, a longer `/v1/data/x` prefix, and a backend that would
    see the bearer. It names both the open `http` listener and the `fabric`
    listener. Neither may accept it.
    """
    server = textwrap.dedent(
        """
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        HITS = {"n": 0}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/__count":
                    body = str(HITS["n"]).encode()
                    self.send_response(200)
                    self.send_header("content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                HITS["n"] += 1
                body = json.dumps({"sink": "foreign", "path": self.path}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                return
        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
        """
    )
    labels = {"app": "token-sink"}
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": FOREIGN,
                "labels": {"fieldstate.nz/gateway-access": "true"},
            },
        },
        configmap("token-sink", {"server.py": server}) | {"metadata": {"name": "token-sink", "namespace": FOREIGN}},
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "token-sink", "namespace": FOREIGN},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": "token-sink",
                                "image": "python:3.12-slim",
                                "command": ["python", "/app/server.py"],
                                "ports": [{"containerPort": 8080}],
                                "volumeMounts": [{"name": "app", "mountPath": "/app"}],
                            }
                        ],
                        "volumes": [{"name": "app", "configMap": {"name": "token-sink"}}],
                    },
                },
            },
        },
        service("token-sink") | {"metadata": {"name": "token-sink", "namespace": FOREIGN}},
    ]


def sink_count() -> int:
    raw = kube(
        "exec", "-n", FOREIGN, "deploy/token-sink", "--",
        "python", "-c",
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/__count').read().decode())",
    )
    return int(raw.strip())


def route_acceptance(namespace: str, name: str) -> list[tuple[str, str, str]]:
    route = json.loads(kube("get", "httproute", name, "-n", namespace, "-o", "json"))
    rows = []
    for parent in route.get("status", {}).get("parents") or []:
        section = (parent.get("parentRef") or {}).get("sectionName") or ""
        for condition in parent.get("conditions") or []:
            if condition.get("type") == "Accepted":
                rows.append((section, str(condition.get("status") or ""), str(condition.get("reason") or "")))
    return rows


def foreign_http_route(
    name: str,
    *,
    hostname: str | None,
    path: str,
    sections: list[str | None],
) -> dict:
    parents = []
    for section in sections:
        parent = {"name": "platform", "namespace": NAMESPACE}
        if section is not None:
            parent["sectionName"] = section
        parents.append(parent)
    spec: dict = {
        "parentRefs": parents,
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": path}}],
                "backendRefs": [{"name": "token-sink", "port": 80}],
            }
        ],
    }
    if hostname is not None:
        spec["hostnames"] = [hostname]
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": FOREIGN},
        "spec": spec,
    }


def wait_parents(name: str, predicate) -> list[tuple[str, str, str]]:
    deadline = time.time() + 90
    accepted: list[tuple[str, str, str]] = []
    while time.time() < deadline:
        accepted = route_acceptance(FOREIGN, name)
        if predicate(accepted):
            return accepted
        time.sleep(2)
    return route_acceptance(FOREIGN, name)


def both_rejected(accepted: list[tuple[str, str, str]]) -> bool:
    """http is refused because the fabric listener owns the host; fabric refuses the namespace."""
    by = {section: (status, reason) for section, status, reason in accepted}
    return (
        by.get("http") == ("False", "NoMatchingListenerHostname")
        and by.get("fabric") == ("False", "NotAllowedByListeners")
        and all(status == "False" for _, status, _ in accepted)
    )


def stand_in_response(status: int, body: str, path: str) -> None:
    if status != 200:
        raise SystemExit(f"{path}: expected 200 from the stand-in, got {status} body={body[:180]!r}")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path}: stand-in body is not JSON: {error}; body={body[:180]!r}") from error
    if parsed.get("path") != path or "headers" not in parsed or "sink" in parsed:
        raise SystemExit(f"{path}: body is not the stand-in echo: {body[:180]!r}")


def require_sink_unchanged(before: int, path: str, token: str) -> None:
    """The legitimate stand-in handles the request, and the foreign sink does not.

    stand_in_count() itself is one request, so the delta minus that probe is 1
    only when `path` was forwarded.
    """
    before_stand = stand_in_count()
    status, _, body = request(path, token)
    time.sleep(1)
    after = sink_count()
    forwarded = stand_in_count() - before_stand - 1
    stand_in_response(status, body, path)
    if after != before:
        raise SystemExit(f"{path}: foreign sink received {after - before} request(s); body={body[:180]!r}")
    if forwarded != 1:
        raise SystemExit(f"{path}: stand-in forwarded {forwarded} requests, expected 1; body={body[:180]!r}")


def reject_foreign_route(token: str) -> None:
    """A gateway-access namespace must not receive the fabric host's bearer.

    Both parents of the two-parent route have to report Accepted=False. A
    missing status is a failure. The request still has to be the legitimate
    /v1/data route's 200, with the stand-in's body. The same namespace must
    be able to attach a route for its own hostname, or the rejection is just
    a broken namespace.
    """
    kube_apply(foreign_route())
    kube("rollout", "status", "deployment/token-sink", "-n", FOREIGN, "--timeout=180s")
    baseline = sink_count()
    kube(
        "exec", "-n", FOREIGN, "deploy/token-sink", "--",
        "python", "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/probe')",
    )
    counted = sink_count()
    if counted != baseline + 1:
        raise SystemExit(f"foreign sink did not count a direct request: {baseline} -> {counted}")

    owned = foreign_http_route(
        "owned-host",
        hostname="other-product.invalid",
        path="/owned",
        sections=["http"],
    )
    kube_apply([owned])
    owned_status = wait_parents("owned-host", lambda rows: any(status == "True" for _, status, _ in rows))
    if not any(status == "True" for _, status, _ in owned_status):
        raise SystemExit(f"positive control was not Accepted: {owned_status}")
    before_owned = sink_count()
    owned_http, _, owned_body = request("/owned", host="other-product.invalid")
    time.sleep(1)
    if owned_http != 200 or '"sink"' not in owned_body or sink_count() != before_owned + 1:
        raise SystemExit(
            f"positive control did not reach the foreign sink: status={owned_http} "
            f"count={sink_count() - before_owned} body={owned_body[:180]!r}"
        )
    tested("foreign namespace attaches for its own hostname", owned_http, "Accepted and sink received /owned")

    overlap = foreign_http_route(
        "fabric-overlap",
        hostname=HOST,
        path="/v1/data/x",
        sections=["http", "fabric"],
    )
    kube_apply([overlap])
    accepted = wait_parents("fabric-overlap", both_rejected)
    print(f"foreign HTTPRoute fabric-overlap parents={accepted}")
    if not both_rejected(accepted):
        raise SystemExit(f"fabric-overlap needs both parents Accepted=False, got {accepted}")
    require_sink_unchanged(sink_count(), "/v1/data/x", token)
    tested(
        "foreign namespace HTTPRoute /v1/data/x on the fabric hostname",
        "Accepted=False, stand-in 200",
        "both parents False; gateway-access namespace other-product",
    )

    unsectioned = foreign_http_route(
        "fabric-unsectioned",
        hostname=HOST,
        path="/v1/data/nosection",
        sections=[None],
    )
    kube_apply([unsectioned])
    # No sectionName: Envoy Gateway reports one parent for the Gateway, with an
    # empty section, rather than one entry per listener. Accepted must be
    # False. A missing status is not a pass.
    unsectioned_status = wait_parents(
        "fabric-unsectioned",
        lambda rows: bool(rows) and all(status == "False" for _, status, _ in rows),
    )
    print(f"foreign HTTPRoute fabric-unsectioned parents={unsectioned_status}")
    if not unsectioned_status or any(status != "False" for _, status, _ in unsectioned_status):
        raise SystemExit(f"unsectioned fabric route was not Accepted=False: {unsectioned_status}")
    require_sink_unchanged(sink_count(), "/v1/data/nosection", token)
    tested("foreign route with no sectionName on the fabric hostname", "Accepted=False, stand-in 200", "http and fabric")

    catch_all = foreign_http_route(
        "fabric-catchall",
        hostname=None,
        path="/",
        sections=["http"],
    )
    kube_apply([catch_all])
    catch_status = wait_parents(
        "fabric-catchall",
        lambda rows: any(section == "http" for section, _, _ in rows),
    )
    print(f"foreign hostname-less route parents={catch_status}")
    require_sink_unchanged(sink_count(), "/v1/data/x", token)
    tested(
        "foreign hostname-less route on the product listener",
        "stand-in 200, sink 0",
        f"parents={catch_status}",
    )


def wait_ready() -> None:
    kube("rollout", "status", "deployment/data-api-stand-in", "-n", NAMESPACE, "--timeout=180s")
    kube("rollout", "status", "deployment/data-api-jwks", "-n", NAMESPACE, "--timeout=180s")
    # kind assigns no load-balancer address, so the Gateway's own Programmed
    # condition stays False. The listener and the proxy are what actually serve.
    deadline = time.time() + 300
    while time.time() < deadline:
        pods = json.loads(kube("get", "pods", "-n", EG_NAMESPACE, "-l", "app.kubernetes.io/component=proxy", "-o", "json"))
        proxy_ready = any(
            all(container.get("ready") for container in pod.get("status", {}).get("containerStatuses") or [])
            for pod in pods["items"]
        )
        routes = json.loads(kube("get", "httproute", "-n", NAMESPACE, "-o", "json"))
        accepted = [
            condition.get("status") == "True"
            for route in routes["items"]
            for parent in route.get("status", {}).get("parents", [])
            for condition in parent.get("conditions", [])
            if condition.get("type") == "Accepted"
        ]
        if proxy_ready and accepted and all(accepted):
            return
        time.sleep(2)
    raise SystemExit("envoy proxy or HTTPRoutes did not become ready")


def port_forward() -> subprocess.Popen:
    services = json.loads(kube("get", "svc", "-A", "-l", "gateway.envoyproxy.io/owning-gateway-name=platform", "-o", "json"))
    if not services["items"]:
        raise SystemExit("envoy service for the platform gateway was not created")
    service = services["items"][0]
    namespace = service["metadata"]["namespace"]
    name = service["metadata"]["name"]
    process = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", f"svc/{name}", f"{LOCAL_PORT}:80"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            raise SystemExit(process.stderr.read())
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{LOCAL_PORT}/", timeout=1)
        except urllib.error.HTTPError:
            return process
        except Exception:
            time.sleep(0.5)
    raise SystemExit("port-forward did not open")


def request(
    path: str,
    token: str | None = None,
    host: str = HOST,
    extra: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", LOCAL_PORT, timeout=10)
    # skip_host: the connection target is 127.0.0.1 via port-forward. The route
    # matches fabric.invalid, and a second Host header would not replace the first.
    connection.putrequest("GET", path, skip_host=True)
    connection.putheader("Host", host)
    if token is not None:
        connection.putheader("Authorization", f"Bearer {token}")
    for key, value in (extra or {}).items():
        connection.putheader(key, value)
    connection.endheaders()
    response = connection.getresponse()
    headers = {key.lower(): value for key, value in response.getheaders()}
    body = response.read().decode()
    connection.close()
    return response.status, headers, body


def claims(now: int, issuer: str, **extra) -> dict:
    payload = {"iss": issuer, "aud": AUDIENCE, "exp": now + 600, "nbf": now - 10, "sub": "user"}
    payload.update(extra)
    return payload


def tested(row: str, status, note: str) -> None:
    line = f"Tested | {row} | {status} | {note}"
    OBSERVATIONS.append(line)
    print(line)


def record(name: str, status: int, header: str, body: str, required: int | None, note: str) -> None:
    line = f"{name}: status={status} www-authenticate={header!r} body={body!r}"
    OBSERVATIONS.append(line)
    print(line)
    if required is not None and status != required:
        GAPS.append(f"REQUIREMENT NOT MET — {name}: got HTTP {status}, ADR 0019 requires HTTP {required}. {note}")


def challenge_ok(header: str) -> bool:
    return 'error="invalid_token"' in header and header.startswith("Bearer ")


def stand_in_count() -> int:
    status, _, body = request("/health")
    if status // 100 != 2:
        raise SystemExit(f"stand-in count probe failed: {status} {body[:240]}")
    return int(json.loads(body)["count"])


def live_http_filter_chains() -> list[list[str]]:
    node = kind_node()
    script = (
        "pid=$(ps -eo pid,cmd | awk '/envoy --service-cluster/ && !/awk/ {print $1; exit}'); "
        "test -n \"$pid\" || exit 1; "
        "nsenter -t \"$pid\" -n curl -sS http://127.0.0.1:19000/config_dump"
    )
    raw = subprocess.check_output([*docker_base(), "exec", node, "sh", "-c", script])
    found: list[list[str]] = []

    def walk(node_value) -> None:
        if isinstance(node_value, dict):
            filters = node_value.get("http_filters")
            if (
                isinstance(filters, list)
                and filters
                and isinstance(filters[0], dict)
                and isinstance(filters[0].get("name"), str)
            ):
                found.append([item.get("name", "") for item in filters])
            for value in node_value.values():
                walk(value)
        elif isinstance(node_value, list):
            for value in node_value:
                walk(value)

    walk(json.loads(raw))
    return found


def assert_lua_after_jwt() -> None:
    """STOP if Lua is not after jwt_authn. Do not reach for ext_authz."""
    deadline = time.time() + 40
    chains: list[list[str]] = []
    while time.time() < deadline:
        try:
            chains = live_http_filter_chains()
        except subprocess.CalledProcessError:
            chains = []
        jwt_chains = [chain for chain in chains if any("jwt_authn" in name for name in chain)]
        if jwt_chains:
            break
        time.sleep(2)
    else:
        raise SystemExit(
            "STOP: config dump has no jwt_authn filter, so Lua-after-jwt_authn "
            "is not confirmed. Not using ext_authz."
        )
    chain = max(jwt_chains, key=lambda item: sum("jwt_authn" in name or name.endswith("lua") for name in item))
    jwt_at = next(index for index, name in enumerate(chain) if "jwt_authn" in name)
    lua_at = [index for index, name in enumerate(chain) if ".lua" in name]
    tested("http filter order", " > ".join(chain), "Lua must follow jwt_authn")
    if not lua_at or min(lua_at) <= jwt_at:
        raise SystemExit(
            "STOP: Lua does not run after jwt_authn in Envoy Gateway v1.9. "
            f"chain={chain}. Not using ext_authz."
        )


def main() -> int:
    keys = Path(tempfile.mkdtemp())
    alpha, beta, unknown = keys / "alpha.pem", keys / "beta.pem", keys / "unknown.pem"
    for path in (alpha, beta, unknown):
        run(["openssl", "genrsa", "-out", str(path), "2048"])
    alpha_jwk, beta_jwk = jwk(alpha, "alpha"), jwk(beta, "beta")
    # Same RSA key, no alg pin, so jwt_authn will verify RS384. The Lua
    # allow-list is only the algs the other keys declare (RS256).
    alpha_open = jwk(alpha, "alpha-open", alg=None)
    declared = sorted({key["alg"] for key in (alpha_jwk, beta_jwk, alpha_open) if "alg" in key})
    lua = (BASE / "headerpolicy.yaml").read_text()
    if "local allowed_algs = { RS256 = true }" not in lua:
        raise SystemExit(f"Lua allow-list is not the JWKS alg set {declared}")
    if declared != ["RS256"]:
        raise SystemExit(f"JWKS declares {declared}; the Lua allow-list is RS256 only")
    ensure_cluster()
    ensure_gateway()
    extract = yaml.safe_load(BASE.joinpath("securitypolicy.yaml").read_text())["spec"]["jwt"]["providers"][0]["extractFrom"]
    alpha_uri = "http://data-api-jwks.platform-system.svc.cluster.local/jwks/alpha"
    beta_uri = "http://data-api-jwks.platform-system.svc.cluster.local/jwks/beta"
    kube_apply(stand_in_manifests([alpha_jwk, alpha_open], [beta_jwk]))
    kube_apply(repo_routes())
    kube_apply([
        repo_policy(alpha_uri, beta_uri),
        repo_document("headerpolicy.yaml"),
        repo_document("responsepolicy.yaml"),
    ])
    kube("rollout", "restart", "deployment/data-api-stand-in", "deployment/data-api-jwks", "-n", NAMESPACE)
    wait_ready()
    wait_one_pod("data-api-stand-in")
    wait_one_pod("data-api-jwks")
    recycle_proxy()
    forward = port_forward()
    try:
        now = int(time.time())
        valid = mint({"alg": "RS256", "kid": "alpha", "typ": "JWT"}, claims(now, ISSUER_ALPHA), alpha)
        deadline = time.time() + 120
        last = (0, {}, "")
        while time.time() < deadline:
            last = request("/v1/data", valid)
            if last[0] == 200:
                break
            time.sleep(2)
        else:
            raise SystemExit(f"valid token never reached the stand-in: {last}")
        echoed = {key.lower(): value for key, value in json.loads(last[2])["headers"].items()}
        if echoed.get("authorization") != f"Bearer {valid}":
            raise SystemExit(f"Authorization was not forwarded verbatim: {echoed.get('authorization')!r}")
        assert_lua_after_jwt()

        beta_token = mint({"alg": "RS256", "kid": "beta", "typ": "JWT"}, claims(now, ISSUER_BETA), beta)
        cases = {
            "valid-alpha": (valid, 200),
            "valid-beta": (beta_token, 200),
            "unsigned": (mint({"alg": "RS256", "kid": "alpha"}, claims(now, ISSUER_ALPHA)), 401),
            "alg-none": (mint({"alg": "none", "kid": "alpha"}, claims(now, ISSUER_ALPHA)), 401),
            "hs256": (mint({"alg": "HS256", "kid": "alpha"}, claims(now, ISSUER_ALPHA), secret=b"not-a-tenant-key"), 401),
            "disallowed-alg": (mint({"alg": "RS384", "kid": "alpha-open", "typ": "JWT"}, claims(now, ISSUER_ALPHA), alpha, digest="sha384"), 401),
            "cross-issuer-key": (mint({"alg": "RS256", "kid": "alpha", "typ": "JWT"}, claims(now, ISSUER_BETA), alpha), 401),
            "cross-issuer-kid": (mint({"alg": "RS256", "kid": "beta", "typ": "JWT"}, claims(now, ISSUER_ALPHA), beta), 401),
            "wrong-issuer": (mint({"alg": "RS256", "kid": "alpha"}, claims(now, "https://identity.example.invalid/realms/other"), alpha), 401),
            "wrong-audience": (mint({"alg": "RS256", "kid": "alpha"}, claims(now, ISSUER_ALPHA, aud="not-the-api"), alpha), None),
            "expired": (lambda: mint({"alg": "RS256", "kid": "alpha"}, claims(int(time.time()), ISSUER_ALPHA, exp=int(time.time()) - 120), alpha), 401),
            "expired-20s": (lambda: mint({"alg": "RS256", "kid": "alpha"}, claims(int(time.time()), ISSUER_ALPHA, exp=int(time.time()) - 20), alpha), 200),
            "expired-31s": (lambda: mint({"alg": "RS256", "kid": "alpha"}, claims(int(time.time()), ISSUER_ALPHA, exp=int(time.time()) - 31), alpha), 401),
            "expired-45s": (lambda: mint({"alg": "RS256", "kid": "alpha"}, claims(int(time.time()), ISSUER_ALPHA, exp=int(time.time()) - 45), alpha), 401),
            "missing-bearer": (None, 401),
            "missing-iss": (mint({"alg": "RS256", "kid": "alpha"}, claims(now, ISSUER_ALPHA, iss=None), alpha), 401),
            "missing-exp": (lambda: mint({"alg": "RS256", "kid": "alpha"}, claims(int(time.time()), ISSUER_ALPHA, exp=None), alpha), 401),
            "nested-exp": (lambda: sign_json(
                json.dumps({"alg": "RS256", "kid": "alpha", "typ": "JWT"}, separators=(",", ":")),
                json.dumps({
                    "x": {"exp": 9999999999},
                    "iss": ISSUER_ALPHA,
                    "aud": AUDIENCE,
                    "nbf": int(time.time()) - 10,
                    "sub": "user",
                }, separators=(",", ":")),
                alpha,
            ), 401),
            "nested-exp-spoof": (lambda: sign_json(
                json.dumps({"alg": "RS256", "kid": "alpha", "typ": "JWT"}, separators=(",", ":")),
                json.dumps({
                    "x": {"exp": 9999999999},
                    "iss": ISSUER_ALPHA,
                    "aud": AUDIENCE,
                    "exp": int(time.time()) - 45,
                    "nbf": int(time.time()) - 10,
                    "sub": "user",
                }, separators=(",", ":")),
                alpha,
            ), 401),
            "duplicate-alg": (lambda: sign_json(
                '{"alg":"RS256","kid":"alpha","alg":"RS256"}',
                json.dumps(claims(int(time.time()), ISSUER_ALPHA), separators=(",", ":")),
                alpha,
            ), 401),
            "nbf-string": (lambda: sign_json(
                json.dumps({"alg": "RS256", "kid": "alpha", "typ": "JWT"}, separators=(",", ":")),
                json.dumps({
                    "iss": ISSUER_ALPHA,
                    "aud": AUDIENCE,
                    "exp": int(time.time()) + 600,
                    "nbf": "not-a-time",
                    "sub": "user",
                }, separators=(",", ":")),
                alpha,
            ), 401),
            "exp-string": (lambda: sign_json(
                json.dumps({"alg": "RS256", "kid": "alpha", "typ": "JWT"}, separators=(",", ":")),
                json.dumps({
                    "iss": ISSUER_ALPHA,
                    "aud": AUDIENCE,
                    "exp": "not-a-time",
                    "sub": "user",
                }, separators=(",", ":")),
                alpha,
            ), 401),
            # Integers only. A float inside the lifetime window is still 401.
            "exp-float": (lambda: _raw_token(alpha, f'"exp":{int(time.time()) + 100}.5'), 401),
            "exp-exponent": (lambda: _raw_token(alpha, '"exp":1e18'), 401),
            "exp-negative": (lambda: _raw_token(alpha, '"exp":-1'), 401),
            "exp-1e18": (lambda: _raw_token(alpha, '"exp":1000000000000000000'), 401),
            "exp-year-3000": (lambda: _raw_token(alpha, '"exp":32503680000'), 401),
            "exp-lifetime-3600": (lambda: mint(
                {"alg": "RS256", "kid": "alpha", "typ": "JWT"},
                claims(int(time.time()), ISSUER_ALPHA, exp=int(time.time()) + 3600),
                alpha,
            ), 200),
            "exp-lifetime-3630": (lambda: mint(
                {"alg": "RS256", "kid": "alpha", "typ": "JWT"},
                claims(int(time.time()), ISSUER_ALPHA, exp=int(time.time()) + 3630),
                alpha,
            ), 200),
            "exp-lifetime-3631": (lambda: mint(
                {"alg": "RS256", "kid": "alpha", "typ": "JWT"},
                claims(int(time.time()), ISSUER_ALPHA, exp=int(time.time()) + 3631),
                alpha,
            ), 401),
            "nbf-float": (lambda: _raw_token(alpha, f'"exp":{int(time.time()) + 600},"nbf":1.5'), 401),
            "iat-now": (lambda: mint(
                {"alg": "RS256", "kid": "alpha", "typ": "JWT"},
                claims(int(time.time()), ISSUER_ALPHA, exp=int(time.time()) + 600, iat=int(time.time())),
                alpha,
            ), 200),
            "iat-future-31s": (lambda: mint(
                {"alg": "RS256", "kid": "alpha", "typ": "JWT"},
                claims(int(time.time()), ISSUER_ALPHA, exp=int(time.time()) + 600, iat=int(time.time()) + 31),
                alpha,
            ), 401),
            "iat-string": (lambda: _raw_token(alpha, f'"exp":{int(time.time()) + 600},"iat":"not-a-time"'), 401),
            "escaped-exp-key": (lambda: _raw_token(
                alpha, f'"\\u0065xp":9999999999,"exp":{int(time.time()) + 600}'
            ), 401),
            "nbf-within-15s": (lambda: mint({"alg": "RS256", "kid": "alpha"}, claims(int(time.time()), ISSUER_ALPHA, nbf=int(time.time()) + 15), alpha), 200),
            "nbf-31s": (lambda: mint({"alg": "RS256", "kid": "alpha"}, claims(int(time.time()), ISSUER_ALPHA, nbf=int(time.time()) + 31), alpha), 401),
            "nbf-beyond-120s": (lambda: mint({"alg": "RS256", "kid": "alpha"}, claims(int(time.time()), ISSUER_ALPHA, nbf=int(time.time()) + 120), alpha), 401),
            "unknown-kid": (mint({"alg": "RS256", "kid": "missing"}, claims(now, ISSUER_ALPHA), unknown), None),
        }
        # Drop claims that were set to None.
        results = {}
        zero_forward = {
            "wrong-audience", "missing-exp", "expired-31s", "expired-45s", "nbf-31s",
            "disallowed-alg", "cross-issuer-key", "cross-issuer-kid",
            "nested-exp", "nested-exp-spoof", "duplicate-alg", "nbf-string", "exp-string",
            "exp-float", "exp-exponent", "exp-negative", "exp-1e18", "exp-year-3000",
            "exp-lifetime-3631", "nbf-float", "iat-future-31s", "iat-string", "escaped-exp-key",
        }
        must_forward = {
            "valid-alpha", "valid-beta", "expired-20s", "nbf-within-15s",
            "exp-lifetime-3600", "exp-lifetime-3630", "iat-now",
        }
        for name, (token, required) in cases.items():
            watch = name in zero_forward or name in must_forward
            before = stand_in_count() if watch else None
            if name == "missing-bearer":
                status, headers, body = request("/v1/data")
            else:
                if callable(token):
                    token = token()
                status, headers, body = request("/v1/data", token)
            www = headers.get("www-authenticate", "")
            results[name] = (status, www, body, headers.get("retry-after", ""))
            if name == "wrong-audience":
                if status not in (401, 403):
                    raise SystemExit(f"wrong-audience: expected 401 or 403, got {status} body={body!r}")
                if not challenge_ok(www):
                    raise SystemExit(f"wrong-audience challenge: {www!r}")
            elif required is not None and status != required:
                raise SystemExit(f"{name}: expected {required}, got {status} header={www!r} body={body!r}")
            if required == 401 and name != "missing-bearer" and not challenge_ok(www):
                raise SystemExit(f"{name}: WWW-Authenticate missing error=\"invalid_token\": {www!r} body={body!r}")
            if before is not None:
                forwarded = stand_in_count() - before - 1
                tested(f"{name} requests forwarded", forwarded, "stand-in count delta")
                expect = 0 if name in zero_forward else 1
                if forwarded != expect:
                    raise SystemExit(f"{name}: stand-in received {forwarded} requests, expected {expect}")
            record(name, status, www, body, required, "observed" if required is None else "met")

        health, health_headers, _ = request("/health")
        ready, ready_headers, _ = request("/ready")
        other, _, _ = request("/not-the-data-api")
        tested("unauthenticated GET /health", health, "no JWT; WWW-Authenticate not required")
        tested("unauthenticated GET /ready", ready, "no JWT; WWW-Authenticate not required")
        if health_headers.get("www-authenticate") or ready_headers.get("www-authenticate"):
            raise SystemExit("probes carried a JWT challenge")
        if health == 401 or ready == 401:
            raise SystemExit("probes returned 401; the JWT policy is covering /health or /ready")
        if health // 100 != 2 or ready // 100 != 2:
            raise SystemExit(f"probes were not forwarded to the stand-in: health={health} ready={ready}")
        if other != 404:
            raise SystemExit(f"path outside the Data API was forwarded: {other}")
        tested("expired 20s (control, within 30s)", results["expired-20s"][0], "200 required")
        tested("expired 31s", results["expired-31s"][0], "401 required; Lua cap now > exp + 30")
        tested("expired 45s", results["expired-45s"][0], "401 required; Lua cap now > exp + 30")
        tested("nbf +15s (control, within 30s)", results["nbf-within-15s"][0], "200 required")
        tested("nbf +31s", results["nbf-31s"][0], "401 required; Lua cap nbf - 30 <= now")
        reject_foreign_route(valid)
        tested(
            "nbf not-yet-valid (nbf=now+120s, beyond clock skew)",
            results["nbf-beyond-120s"][0],
            "401 required",
        )
        tested("nested exp, no top-level exp", results["nested-exp"][0], "401 required; one root exp")
        tested("nested exp spoofing an expired exp", results["nested-exp-spoof"][0], "401 required; exp occurs twice")
        tested("duplicate alg", results["duplicate-alg"][0], "401 required; alg occurs twice")
        tested("nbf is a string", results["nbf-string"][0], "401 required; non-numeric nbf fails closed")
        tested("exp is a string", results["exp-string"][0], "401 required; non-numeric exp fails closed")
        tested("exp float within the window", results["exp-float"][0], "401; JSON integers only, floats are rejected")
        tested("exp exponent 1e18", results["exp-exponent"][0], "401 required")
        tested("exp negative", results["exp-negative"][0], "401 required")
        tested("exp 1e18 integer digits", results["exp-1e18"][0], "401 required; over MAX_TOKEN_LIFETIME")
        tested("exp year 3000", results["exp-year-3000"][0], "401 required; over MAX_TOKEN_LIFETIME")
        tested("exp now+3600", results["exp-lifetime-3600"][0], "200; within 3630s")
        tested("exp now+3630", results["exp-lifetime-3630"][0], "200; ceiling includes 30s skew")
        tested("exp now+3631", results["exp-lifetime-3631"][0], "401 required; over 3630s")
        tested("nbf float", results["nbf-float"][0], "401 required; nbf must be an integer")
        tested("iat now", results["iat-now"][0], "200 required")
        tested("iat now+31s", results["iat-future-31s"][0], "401 required; iat <= now + 30")
        tested("iat string", results["iat-string"][0], "401 required; non-numeric iat fails closed")
        tested("escaped exp key", results["escaped-exp-key"][0], "401 required; backslash in a key")
        boom_before = stand_in_count()
        boom_status, boom_headers, boom_body = request("/v1/data/boom", valid)
        boom_forwarded = stand_in_count() - boom_before - 1
        tested("upstream 500 from the stand-in", boom_status, f"forwarded {boom_forwarded}; body={boom_body[:80]!r}")
        if boom_status != 500 or boom_forwarded != 1 or boom_body == "unavailable":
            raise SystemExit(
                f"upstream 500 was rewritten or not forwarded: status={boom_status} "
                f"forwarded={boom_forwarded} body={boom_body[:160]!r} "
                f"retry-after={boom_headers.get('retry-after')!r}"
            )
        tested("disallowed alg RS384", results["disallowed-alg"][0], "401 required; allow-list is RS256")
        tested("iss=beta signed with alpha key", results["cross-issuer-key"][0], "401 required")
        tested("iss=alpha kid=beta", results["cross-issuer-kid"][0], "401 required")

        # Applied after the live-JWKS cases so a dead JWKS cluster cannot
        # disturb the providers that have to accept a valid token.
        kube_apply(unreachable_route(extract))
        time.sleep(5)
        gamma = mint({"alg": "RS256", "kid": "missing"}, claims(now, ISSUER_GAMMA), unknown)
        before_unreachable = stand_in_count()
        request("/v1/data", gamma, host="jwks-down.invalid")
        time.sleep(3)
        unreachable = request("/v1/data", gamma, host="jwks-down.invalid")
        unreachable_forwarded = stand_in_count() - before_unreachable - 1
        tested("jwks-unreachable requests forwarded", unreachable_forwarded, "stand-in must see zero")
        tested(
            "jwks-unreachable",
            unreachable[0],
            f"Retry-After={unreachable[1].get('retry-after', '')!r} body={unreachable[2]!r}",
        )
        if unreachable_forwarded != 0:
            raise SystemExit(f"jwks-unreachable: stand-in received {unreachable_forwarded} requests")
        www = unreachable[1].get("www-authenticate", "")
        retry_after = unreachable[1].get("retry-after", "")
        record(
            "jwks-unreachable",
            unreachable[0],
            www,
            unreachable[2],
            ADR_JWKS_FAILURE,
            "G8/G13 requires 503, Retry-After, and a body that does not describe the credential. "
            f"Retry-After={retry_after!r}. BackendTrafficPolicy responseOverride source Local "
            "was applied to map a local 500 to 503.",
        )
        kid_status, kid_header, kid_body, _ = results["unknown-kid"]
        record("unknown-kid-fresh-jwks", kid_status, kid_header, kid_body, 401, "fresh snapshot")
        if kid_status == 401:
            GAPS.append(
                "KNOWN RISK — G8 unknown kid: HTTP 401 with the JWKS reachable and the kid absent. "
                "That is the fresh-snapshot outcome G8 allows. Stock jwt_authn cannot return 503 "
                "when the snapshot is not a fresh proof the key is absent."
            )
        tested(
            "wrong audience",
            results["wrong-audience"][0],
            "403 accepted; zero requests forwarded",
        )
        named = [
            f"{name} body={body!r}"
            for name, (status, _header, body, _retry) in results.items()
            if status in (401, 403) and any(phrase in body for phrase in ("Jwt ", "Audiences", "Jwks "))
        ]
        if named:
            GAPS.append(
                "KNOWN RISK — responseOverride did not replace jwt_authn local replies. "
                "Bodies still name the failed check: " + "; ".join(named)
            )
        else:
            tested("generic rejection body", "replaced", "local 401/403 bodies do not name the check")
        if results["missing-bearer"][1] and not challenge_ok(results["missing-bearer"][1]):
            GAPS.append(
                "NOTE — missing bearer WWW-Authenticate is "
                f"{results['missing-bearer'][1]!r}. Envoy omits error=\"invalid_token\" when the JWT is absent "
                "(JwtMissed) and includes it on an invalid token."
            )
        header_checks(valid)
        network_checks(valid)
        record_http2_coalescing()
        versions()
        print("\n===== ADR 0019 G8/G13 =====")
        unmet = [gap for gap in GAPS if gap.startswith("REQUIREMENT NOT MET")]
        if GAPS:
            for gap in GAPS:
                print(gap)
        else:
            print("No gaps recorded.")
        print("===== observations =====")
        for line in OBSERVATIONS:
            print(line)
        if unmet:
            print(f"{len(unmet)} REQUIREMENT NOT MET")
            return 1
        return 0
    finally:
        forward.terminate()
        if os.environ.get("DATA_API_EDGE_KEEP") != "1":
            subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER], check=False)


def echoed_headers(body: str) -> dict[str, str]:
    raw = json.loads(body)["headers"]
    return {key.lower(): value for key, value in raw.items()}


def header_checks(valid: str) -> None:
    """G9 strips caller identity headers, G9a adds none, G10 keeps Authorization."""
    policy = yaml.safe_load((BASE / "securitypolicy.yaml").read_text())
    if "claimToHeaders" in json.dumps(policy):
        raise SystemExit("G9a: SecurityPolicy contains claimToHeaders")
    extra = {
        "X-Tenant-Id": "acme",
        "X-Jwt-Claim-Tenant-Id": "globex",
        "X-Jwt-Claim-Extra": "spoofed",
        "X-Forwarded-User": "operator",
        "X-Forwarded-Email": "operator@example.invalid",
        "X-Forwarded-Groups": "operators",
        "X-Forwarded-Preferred-Username": "operator",
        "X-Auth-Request-User": "operator",
        "X-Auth-Request-Email": "operator@example.invalid",
        "X-Operator-Id": "operator",
        "X-Trace-Demo": "keep-me",
        "X-Forwarded-For": "203.0.113.9",
    }
    status, _, body = request("/v1/data", valid, extra=extra)
    if status != 200:
        raise SystemExit(f"G9 request status {status}: {body[:240]}")
    headers = echoed_headers(body)
    stripped = [
        "x-tenant-id",
        "x-jwt-claim-tenant-id",
        "x-jwt-claim-extra",
        "x-forwarded-user",
        "x-forwarded-email",
        "x-forwarded-groups",
        "x-forwarded-preferred-username",
        "x-auth-request-user",
        "x-auth-request-email",
        "x-operator-id",
    ]
    leaked = [name for name in stripped if name in headers]
    if leaked:
        raise SystemExit(f"G9: identity headers reached the stand-in: {leaked}")
    if headers.get("x-trace-demo") != "keep-me":
        raise SystemExit(f"G9: unrelated header was removed: {headers.get('x-trace-demo')!r}")
    projected = [name for name in headers if name.startswith("x-jwt-claim-") or name.startswith("x-auth-request-")]
    if projected:
        raise SystemExit(f"G9a: gateway projected claim headers: {projected}")
    if headers.get("authorization") != f"Bearer {valid}":
        raise SystemExit("G10: Authorization was rewritten")
    probe_status, probe_response, probe_body = request("/health", extra=extra)
    if probe_status != 200:
        raise SystemExit(f"G9 probe strip status {probe_status}: {probe_body[:240]}")
    if probe_response.get("www-authenticate"):
        raise SystemExit("G9: /health carried a JWT challenge")
    probe_leaked = [name for name in stripped if name in echoed_headers(probe_body)]
    if probe_leaked:
        raise SystemExit(f"G9: identity headers reached the stand-in on /health: {probe_leaked}")
    print("G9/G9a/G10: identity headers stripped on /v1/data and /health, Authorization unchanged")


def runtime_selector() -> dict[str, str]:
    """The labels the real runtime Deployment selects its pods with."""
    deployment = next(
        document for document in yaml.safe_load_all((BASE / "deployment.yaml").read_text())
        if document and document.get("kind") == "Deployment"
    )
    return dict(deployment["spec"]["selector"]["matchLabels"])


def http_client(name: str, namespace: str, labels: dict[str, str], pod_ip: str, service_ip: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "probe",
                    "image": "python:3.12-slim",
                    "command": [
                        "python", "-c",
                        "import urllib.request\n"
                        "def hit(url):\n"
                        "    try:\n"
                        "        response = urllib.request.urlopen(url, timeout=3)\n"
                        "        print(f'HTTP {response.status} {url}')\n"
                        "    except Exception as error:\n"
                        "        print(f'BLOCKED {url} {type(error).__name__}: {error}')\n"
                        f"hit('http://{pod_ip}:8080/health')\n"
                        f"hit('http://{service_ip}/health')\n",
                    ],
                }
            ],
        },
    }


def pod_log(namespace: str, name: str) -> str:
    deadline = time.time() + 90
    while time.time() < deadline:
        phase = kube("get", "pod", "-n", namespace, name, "-o", "jsonpath={.status.phase}").strip()
        if phase in ("Succeeded", "Failed"):
            return kube("logs", "-n", namespace, name)
        time.sleep(2)
    raise SystemExit(f"{namespace}/{name} did not finish")


def network_checks(valid: str) -> None:
    """G11. Deny is only a pass after the same pod answers from the gateway namespace."""
    policy = repo_document("networkpolicy.yaml")
    selector = policy["spec"]["podSelector"]["matchLabels"]
    deployment_labels = runtime_selector()
    print(f"G11 NetworkPolicy podSelector: {selector}")
    print(f"G11 runtime Deployment selector: {deployment_labels}")
    if selector != deployment_labels:
        raise SystemExit(
            "G11: NetworkPolicy does not select the runtime Deployment's labels "
            f"({selector} != {deployment_labels})"
        )
    if "data-api-stand-in" in json.dumps(selector):
        raise SystemExit("G11: NetworkPolicy selects a test-only label")
    kube_apply([policy])
    time.sleep(2)
    stand_in = json.loads(
        kube("get", "pod", "-n", NAMESPACE, "-l", "app.kubernetes.io/name=saas-fabric", "-o", "json")
    )["items"][0]
    pod_labels = stand_in["metadata"]["labels"]
    print(f"G11 stand-in labels: {pod_labels}")
    missing = {key: value for key, value in deployment_labels.items() if pod_labels.get(key) != value}
    if missing:
        raise SystemExit(f"G11: stand-in is missing the runtime Deployment labels {missing}")
    pod_ip = stand_in["status"]["podIP"]
    service_ip = kube(
        "get", "svc", "-n", NAMESPACE, "data-api-stand-in", "-o", "jsonpath={.spec.clusterIP}"
    ).strip()
    admitted = {
        "app.kubernetes.io/component": "proxy",
        "app.kubernetes.io/managed-by": "envoy-gateway",
        "gateway.envoyproxy.io/owning-gateway-name": "platform",
    }
    kube("delete", "pod", "-n", NAMESPACE, "gateway-reach", "--ignore-not-found")
    kube_apply([http_client("gateway-reach", NAMESPACE, admitted, pod_ip, service_ip)])
    control = pod_log(NAMESPACE, "gateway-reach")
    print(f"G11 gateway-namespace control:\n{control}")
    if control.count("HTTP 200") < 2:
        raise SystemExit(
            "G11: the stand-in did not answer HTTP from the gateway namespace, "
            "so a scratch-pod timeout would not prove the policy"
        )
    kube_apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "edge-probe"}}])
    kube("delete", "pod", "-n", "edge-probe", "edge-probe", "--ignore-not-found")
    kube_apply([http_client("edge-probe", "edge-probe", {"app": "edge-probe"}, pod_ip, service_ip)])
    blocked = pod_log("edge-probe", "edge-probe")
    print(f"G11 scratch pod in edge-probe:\n{blocked}")
    if "HTTP " in blocked or blocked.count("BLOCKED") < 2:
        raise SystemExit(f"G11: direct access was not blocked:\n{blocked}")
    image = kube(
        "get", "ds", "-n", "kube-system", "calico-node",
        "-o", "jsonpath={.spec.template.spec.containers[0].image}",
    ).strip()
    print(
        f"G11 enforced by Calico Felix nftables dataplane ({image}) on kind. "
        "kindnet does not enforce NetworkPolicy, and this kernel rejects ipset, "
        "so the iptables dataplane cannot."
    )
    tested("G11 pod IP and Service from another namespace", "blocked", f"control HTTP 200; CNI {image}")
    kube("delete", "pod", "-n", NAMESPACE, "namespace-peer", "--ignore-not-found")
    kube_apply([http_client("namespace-peer", NAMESPACE, {"app": "not-the-proxy"}, pod_ip, service_ip)])
    peer = pod_log(NAMESPACE, "namespace-peer")
    print(f"G11 non-proxy pod in {NAMESPACE}:\n{peer}")
    if "HTTP " in peer or peer.count("BLOCKED") < 2:
        raise SystemExit(f"G11: a non-proxy pod in the gateway namespace was not blocked:\n{peer}")
    tested("G11 non-proxy pod in platform-system", "blocked", "same namespace, not the gateway proxy")
    through, _, through_body = request("/v1/data", valid)
    if through != 200:
        raise SystemExit(f"G11: gateway path after the policy was {through}: {through_body[:240]}")
    tested("G11 request through the gateway proxy", through, "positive path after the NetworkPolicy is applied")


def record_http2_coalescing() -> None:
    """Shared-certificate reuse across two HTTPS listeners, on EG 1.9.

    The probe opens TLS with SNI for one hostname and sends a request whose
    Host is the other, in both directions. 421 Misdirected Request would be
    the signal that a client must open a new connection. Envoy Gateway 1.9
    advertises only http/1.1, so an HTTP/2 connection cannot be negotiated;
    the probe records that and the HTTP/1.1 status of the cross-host request.
    """
    keys = Path(tempfile.mkdtemp())
    cert, key = keys / "tls.crt", keys / "tls.key"
    run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=shared.invalid",
            "-addext", "subjectAltName=DNS:fabric.invalid,DNS:other.invalid",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    secret = subprocess.check_output(
        [
            "kubectl", "-n", NAMESPACE, "create", "secret", "tls", "shared-tls",
            f"--cert={cert}", f"--key={key}", "--dry-run=client", "-o", "yaml",
        ],
        text=True,
    )
    kube("apply", "-f", "-", input_text=secret)
    gateway = json.loads(kube("get", "gateway", "platform", "-n", NAMESPACE, "-o", "json"))
    gateway.pop("status", None)
    metadata = gateway.get("metadata") or {}
    for field in ("resourceVersion", "uid", "generation", "managedFields", "creationTimestamp"):
        metadata.pop(field, None)
    present = {listener["name"] for listener in gateway["spec"]["listeners"]}
    for listener in (
        {
            "name": "other",
            "protocol": "HTTPS",
            "port": 443,
            "hostname": "other.invalid",
            "tls": {"mode": "Terminate", "certificateRefs": [{"kind": "Secret", "name": "shared-tls"}]},
            "allowedRoutes": {"namespaces": {"from": "All"}},
        },
        {
            "name": "fabric-tls",
            "protocol": "HTTPS",
            "port": 443,
            "hostname": HOST,
            "tls": {"mode": "Terminate", "certificateRefs": [{"kind": "Secret", "name": "shared-tls"}]},
            "allowedRoutes": {"namespaces": {"from": "All"}},
        },
    ):
        if listener["name"] not in present:
            gateway["spec"]["listeners"].append(listener)
    kube("apply", "-f", "-", input_text=yaml.safe_dump(gateway))
    kube_apply([
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": "h2-other", "namespace": NAMESPACE},
            "spec": {
                "parentRefs": [{"name": "platform", "sectionName": "other"}],
                "hostnames": ["other.invalid"],
                "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                           "backendRefs": [{"name": "data-api-stand-in", "port": 80}]}],
            },
        },
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": "h2-fabric", "namespace": NAMESPACE},
            "spec": {
                "parentRefs": [{"name": "platform", "sectionName": "fabric-tls"}],
                "hostnames": [HOST],
                "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                           "backendRefs": [{"name": "data-api-stand-in", "port": 80}]}],
            },
        },
    ])
    deadline = time.time() + 120
    while time.time() < deadline:
        pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-l", "app.kubernetes.io/component=proxy", "-o", "json"))
        ready = any(
            all(container.get("ready") for container in pod.get("status", {}).get("containerStatuses") or [])
            for pod in pods["items"]
        )
        if ready:
            break
        time.sleep(2)
    else:
        raise SystemExit("proxy was not ready for the shared-certificate probe")
    services = json.loads(
        kube("get", "svc", "-A", "-l", "gateway.envoyproxy.io/owning-gateway-name=platform", "-o", "json")
    )
    service = services["items"][0]
    port = 18443
    forward = subprocess.Popen(
        [
            "kubectl", "-n", service["metadata"]["namespace"],
            "port-forward", f"svc/{service['metadata']['name']}", f"{port}:443",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2)
    try:
        outcomes = []
        for sni, host in (
            ("other.invalid", "other.invalid"),
            ("other.invalid", HOST),
            (HOST, HOST),
            (HOST, "other.invalid"),
        ):
            alpn, status = _tls_status(port, sni, host, ["h2", "http/1.1"])
            line = f"SNI={sni} Host={host} alpn={alpn} status={status}"
            outcomes.append(line)
            print(line)
        h2_only = _tls_status(port, "other.invalid", "other.invalid", ["h2"])
        print(f"ALPN offering only h2: {h2_only[0]}")
        crossed = [line for line in outcomes if line.split()[0] != line.split()[1].replace("Host=", "SNI=")]
        saw_421 = any("status=421" in line for line in outcomes)
        tested(
            "shared-cert cross-host request",
            "421" if saw_421 else "not 421",
            "; ".join(outcomes) + f"; h2-only alpn={h2_only[0]}",
        )
        if not any("SNI=other.invalid Host=other.invalid" in line and "status=200" in line for line in outcomes):
            raise SystemExit(f"same-host TLS request was not served: {outcomes}")
        if saw_421:
            print("EG 1.9 returned 421 for a shared-certificate cross-host request")
        else:
            print(
                "EG 1.9 did not return 421. HTTPS listeners advertise ALPN http/1.1 only, "
                f"so HTTP/2 is not negotiated (h2-only ALPN={h2_only[0]}). "
                "A TLS connection opened for one host and reused for the other is "
                + "; ".join(crossed)
            )
    finally:
        forward.terminate()


def _tls_status(port: int, sni: str, host: str, alpn: list[str]) -> tuple[str | None, int]:
    import socket
    import ssl

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(alpn)
    raw = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        tls = context.wrap_socket(raw, server_hostname=sni)
    except ssl.SSLError:
        raw.close()
        return None, 0
    negotiated = tls.selected_alpn_protocol()
    if negotiated != "http/1.1":
        tls.close()
        return negotiated, 0
    tls.sendall(
        f"GET /h2-probe HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
    )
    data = b""
    while True:
        chunk = tls.recv(4096)
        if not chunk:
            break
        data += chunk
    tls.close()
    status_line = data.split(b"\r\n", 1)[0].decode(errors="replace")
    parts = status_line.split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return negotiated, status


def versions() -> None:
    image = kube(
        "get", "deploy", gateway_deployment(), "-n", EG_NAMESPACE,
        "-o", "jsonpath={.spec.template.spec.containers[0].image}",
    ).strip()
    policy = json.loads(kube("get", "securitypolicy", "saas-fabric-data", "-n", NAMESPACE, "-o", "json"))
    proxies = json.loads(kube("get", "pods", "-n", EG_NAMESPACE, "-l", "app.kubernetes.io/name=envoy", "-o", "json"))
    proxy_images = sorted({
        container["image"]
        for pod in proxies["items"]
        for container in pod["spec"]["containers"]
        if container.get("name") == "envoy"
    })
    print(f"envoy-gateway image: {image}")
    print(f"envoy proxy image: {proxy_images}")
    print(f"SecurityPolicy apiVersion: {policy['apiVersion']}")
    print(f"SecurityPolicy resourceVersion: {policy['metadata']['resourceVersion']}")
    print(f"SecurityPolicy generation: {policy['metadata']['generation']}")
    print(f"chart: {EG_CHART} {EG_VERSION}")
    overlay = yaml.safe_load(
        (ROOT / "applications/core/saas-fabric/overlays/lucentroot/kustomization.yaml").read_text()
    )
    digest = overlay["images"][0]["digest"]
    tested("runtime image digest", digest, "lucentroot pin ghcr.io/fieldstatenz/saas-fabric; replicas stay 0")


if __name__ == "__main__":
    raise SystemExit(main())
