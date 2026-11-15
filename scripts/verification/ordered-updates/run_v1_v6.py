#!/usr/bin/env python3
"""V1–V6 for the RollingSync candidate, on a throwaway kind cluster.

The cluster, kubeconfig, and git repo are created under /tmp and removed on
success. The script refuses any kube context other than the one it just
created, and refuses a server that is not loopback.

    python3 scripts/verification/ordered-updates/run_v1_v6.py
    python3 scripts/verification/ordered-updates/run_v1_v6.py --self-check
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
EVIDENCE = REPO_ROOT / "test-evidence" / "ordered-updates"
TEMPLATE = HERE / "repo"
TMP = Path("/tmp/w2-ordered-updates")
WORK = TMP / "work"
BARE_PARENT = TMP / "git"
BARE = BARE_PARENT / "repo.git"
KUBECONFIG = TMP / "kubeconfig"
CLUSTER = "w2-ordered-updates"
CONTEXT = f"kind-{CLUSTER}"
REPO_URL = "git://git.git.svc.cluster.local/repo.git"
STEP2 = "master-instance"
STEP3 = "saas-fabric-control-plane"
STEP1 = "master-instance-credential"
APPS = (STEP1, STEP2, STEP3)
NZ = timezone(timedelta(hours=13))

ENV = os.environ.copy()
ENV["KUBECONFIG"] = str(KUBECONFIG)

RESULTS: list[dict] = []


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def log(message: str) -> None:
    print(f"[{now_iso()}] {message}", flush=True)


def redact(cmd: list[str]) -> list[str]:
    out = []
    skip = False
    for part in cmd:
        if skip:
            out.append("***")
            skip = False
            continue
        if part == "--password":
            out.append(part)
            skip = True
            continue
        out.append(part)
    return out


def run(cmd: list[str], timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess[str]:
    log("+ " + " ".join(redact(cmd)))
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=ENV,
        check=False,
    )
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}")
    return proc


def kubectl_json(args: list[str]) -> dict:
    proc = run(["kubectl", *args, "-o", "json"])
    return json.loads(proc.stdout)


def save_json(name: str, payload) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def record(test: str, passed: bool, detail) -> None:
    RESULTS.append({"test": test, "result": "pass" if passed else "fail", "detail": detail})
    log(f"{test}: {'PASS' if passed else 'FAIL'}")
    save_json("results.json", RESULTS)


def step2_ready(view: dict, sha: str) -> bool:
    return view["health"] == "Healthy" and view["sync"] == "Synced" and view["revision"] == sha


def step3_sync_started(view: dict, commit_time: datetime) -> bool:
    """True when step 3 has an operation that started after this commit.

    The previous commit's operation is still in status until the next one
    replaces it. Comparing with a slack window treated that finished
    operation as a new sync and failed the following round.
    """
    started = parse_time(view.get("opStarted"))
    if started is None:
        return False
    if started < commit_time:
        return False
    return view.get("opPhase") in {"Running", "Succeeded", "Failed", "Error", "Terminating"}


def first_violation(samples: list[dict], sha: str, marker: str, commit_time: datetime) -> dict | None:
    """A sample where step 3 applied `marker` or started a sync before step 2
    was Healthy and Synced at `sha`."""
    for sample in samples:
        moved = sample["marker"] == marker or step3_sync_started(sample["step3"], commit_time)
        if moved and not step2_ready(sample["step2"], sha):
            return sample
    return None


def saw_unready(samples: list[dict], sha: str) -> bool:
    return any(
        sample["step2"]["revision"] == sha and not step2_ready(sample["step2"], sha)
        for sample in samples
    )


def self_check() -> None:
    sha = "abc"
    commit = parse_time("2026-11-15T00:00:01Z")
    assert commit is not None
    progressing = {
        "health": "Progressing",
        "sync": "Synced",
        "revision": sha,
        "opStarted": "2026-11-15T00:00:02Z",
        "opPhase": "Succeeded",
        "opRevision": sha,
    }
    ready = {
        "health": "Healthy",
        "sync": "Synced",
        "revision": sha,
        "opStarted": "2026-11-15T00:00:02Z",
        "opPhase": "Succeeded",
        "opRevision": sha,
    }
    old = {
        "health": "Healthy",
        "sync": "Synced",
        "revision": "old",
        "opStarted": "2026-11-14T00:00:00Z",
        "opPhase": "Succeeded",
        "opRevision": "old",
    }
    started = {
        "health": "Progressing",
        "sync": "OutOfSync",
        "revision": sha,
        "opStarted": "2026-11-15T00:00:05Z",
        "opPhase": "Running",
        "opRevision": "main",
    }
    clean = [
        {"step2": progressing, "step3": old, "marker": "0"},
        {"step2": ready, "step3": old, "marker": "0"},
        {"step2": ready, "step3": started, "marker": "v2-1"},
    ]
    early = [{"step2": progressing, "step3": started, "marker": "0"}]
    stale = [
        {
            "step2": ready,
            "step3": {
                "health": "Healthy",
                "sync": "Synced",
                "revision": "previous",
                "opStarted": "2026-11-15T00:00:00Z",
                "opPhase": "Succeeded",
                "opRevision": "previous",
            },
            "marker": "v2-1",
        }
    ]
    if first_violation(clean, sha, "v2-1", commit) is not None:
        raise RuntimeError("self-check rejected a clean sequence")
    if first_violation(early, sha, "v2-1", commit) is None:
        raise RuntimeError("self-check accepted an early step-3 sync")
    if first_violation(stale, sha, "v2-2", commit) is not None:
        raise RuntimeError("self-check treated the previous operation as a new sync")
    if not saw_unready(clean, sha):
        raise RuntimeError("self-check missed the unready window")
    log("ordering self-check passed")


def app_view(name: str) -> dict:
    raw = kubectl_json(["-n", "argocd", "get", "application", name])
    status = raw.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    operation_state = status.get("operationState") or {}
    operation = operation_state.get("operation") or {}
    sync_op = operation.get("sync") or {}
    result = operation_state.get("syncResult") or {}
    meta = raw.get("metadata") or {}
    return {
        "name": name,
        "uid": meta.get("uid"),
        "revision": sync.get("revision"),
        "sync": sync.get("status"),
        "health": health.get("status"),
        "opPhase": operation_state.get("phase"),
        "opStarted": operation_state.get("startedAt"),
        "opFinished": operation_state.get("finishedAt"),
        "opRevision": (result.get("revision") or sync_op.get("revision")),
        "prune": sync_op.get("prune"),
        "syncOptions": sync_op.get("syncOptions"),
        "initiatedBy": operation.get("initiatedBy"),
        "retry": operation.get("retry"),
        "labels": meta.get("labels") or {},
        "owners": meta.get("ownerReferences") or [],
        "syncPolicy": (raw.get("spec") or {}).get("syncPolicy"),
        "history": [
            {
                "id": item.get("id"),
                "revision": item.get("revision"),
                "deployedAt": item.get("deployedAt"),
                "deployStartedAt": item.get("deployStartedAt"),
            }
            for item in (status.get("history") or [])
        ],
    }


def marker() -> str | None:
    proc = run(
        [
            "kubectl",
            "-n",
            "operator-system",
            "get",
            "configmap",
            "control-plane-standin",
            "-o",
            "jsonpath={.data.marker}",
        ],
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def refresh(name: str, kind: str = "application") -> None:
    run(
        [
            "kubectl",
            "-n",
            "argocd",
            "annotate",
            kind,
            name,
            "argocd.argoproj.io/refresh=hard",
            "--overwrite",
        ],
        check=False,
    )


def sample() -> dict:
    return {
        "t": now_iso(),
        "step2": app_view(STEP2),
        "step3": app_view(STEP3),
        "marker": marker(),
    }


def uid_of(kind: str, namespace: str | None, name: str) -> str | None:
    cmd = ["kubectl", "get", kind, name, "-o", "jsonpath={.metadata.uid}"]
    if namespace:
        cmd[2:2] = ["-n", namespace]
    proc = run(cmd, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout or None


def snapshot_uids() -> dict:
    specs = {
        "application/master-instance-credential": ("application", "argocd", STEP1),
        "application/master-instance": ("application", "argocd", STEP2),
        "application/saas-fabric-control-plane": ("application", "argocd", STEP3),
        "configmap/credential-standin": ("configmap", "operator-system", "credential-standin"),
        "configmap/convergence-mode": ("configmap", "operator-system", "convergence-mode"),
        "configmap/control-plane-standin": ("configmap", "operator-system", "control-plane-standin"),
        "job/convergence": ("job", "operator-system", "convergence"),
        "namespace/master-instance-state": ("namespace", None, "master-instance-state"),
        "secret/tfstate-default-master-instance": (
            "secret",
            "master-instance-state",
            "tfstate-default-master-instance",
        ),
    }
    return {key: uid_of(*spec) for key, spec in specs.items()}


def changed_uids(before: dict, after: dict, keys: list[str] | None = None) -> dict:
    chosen = keys or list(before)
    out = {}
    for key in chosen:
        if before.get(key) != after.get(key):
            out[key] = {"before": before.get(key), "after": after.get(key)}
    return out


def set_yaml_field(path: Path, field: str, value: str) -> None:
    lines = path.read_text().splitlines(keepends=True)
    prefix = f"  {field}:"
    found = False
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{prefix} {value}\n"
            found = True
            break
    if not found:
        raise RuntimeError(f"{field} not found in {path}")
    path.write_text("".join(lines))


def add_prune_false(path: Path) -> None:
    text = path.read_text()
    if "Prune=false" in text:
        return
    lines = []
    for line in text.splitlines(keepends=True):
        lines.append(line)
        if line.strip().startswith("argocd.argoproj.io/sync-wave:"):
            lines.append("    argocd.argoproj.io/sync-options: Prune=false\n")
    path.write_text("".join(lines))


APPS_KUST = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - master-instance-credential.yaml
  - master-instance.yaml
  - saas-fabric-control-plane.yaml
"""

APPSET_KUST = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - operator-sign-in.yaml
"""


def git(*args: str, timeout: int = 60) -> str:
    return run(["git", "-C", str(WORK), *args], timeout=timeout).stdout.strip()


def commit_all(message: str) -> str:
    git("add", "-A")
    git("commit", "-m", message)
    git("push", "origin", "main")
    return git("rev-parse", "HEAD")


def prepare_git() -> None:
    if TMP.exists():
        shutil.rmtree(TMP)
    BARE_PARENT.mkdir(parents=True)
    run(["git", "init", "--bare", "-b", "main", str(BARE)])
    shutil.copytree(TEMPLATE, WORK)
    for path in WORK.rglob("*.yaml"):
        text = path.read_text()
        if "REPO_URL_PLACEHOLDER" in text:
            path.write_text(text.replace("REPO_URL_PLACEHOLDER", REPO_URL))
    run(["git", "-C", str(WORK), "init", "-b", "main"])
    git("config", "user.email", "w2-verification@example.invalid")
    git("config", "user.name", "w2-verification")
    git("remote", "add", "origin", str(BARE))
    commit_all("initial stand-ins")


def assert_loopback() -> None:
    ctx = run(["kubectl", "config", "current-context"]).stdout.strip()
    server = run(
        ["kubectl", "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"]
    ).stdout.strip()
    if ctx != CONTEXT:
        raise RuntimeError(f"refusing kube context {ctx}")
    if not (server.startswith("https://127.0.0.1:") or server.startswith("https://localhost:")):
        raise RuntimeError(f"refusing Kubernetes API {server}")
    log(f"cluster context {ctx} at {server}")


def create_cluster() -> None:
    config = TMP / "kind.yaml"
    config.write_text(
        "\n".join(
            [
                "kind: Cluster",
                "apiVersion: kind.x-k8s.io/v1alpha4",
                "networking:",
                "  kubeProxyMode: nftables",
                "nodes:",
                "  - role: control-plane",
                "    extraMounts:",
                "      - hostPath: /tmp/w2-ordered-updates/git",
                "        containerPath: /git-host",
                "",
            ]
        )
    )
    run(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            CLUSTER,
            "--kubeconfig",
            str(KUBECONFIG),
            "--config",
            str(config),
            "--wait",
            "180s",
        ],
        timeout=600,
    )
    assert_loopback()


def load_images() -> None:
    # The kind node cannot reach quay.io (dial timeout). `kind load` also fails
    # on this Docker's containerd snapshotter. Save on the host, import with ctr.
    images = [
        "quay.io/argoproj/argocd:v3.5.6",
        "ecr-public.aws.com/docker/library/redis:8.6.4-alpine",
        "registry.k8s.io/e2e-test-images/busybox:1.36.1-1",
    ]
    node = f"{CLUSTER}-control-plane"
    for image in images:
        run(["docker", "pull", image], timeout=600)
        log(f"importing {image} into the kind node")
        save = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE)
        imported = subprocess.run(
            ["docker", "exec", "-i", node, "ctr", "-n", "k8s.io", "images", "import", "-"],
            stdin=save.stdout,
            capture_output=True,
            timeout=600,
            check=False,
        )
        if save.stdout is not None:
            save.stdout.close()
        save.wait(timeout=60)
        if save.returncode != 0 or imported.returncode != 0:
            sys.stderr.write((imported.stderr or b"").decode(errors="replace"))
            raise RuntimeError(f"import failed for {image}")


def install_argocd() -> None:
    run(["helm", "repo", "add", "argo", "https://argoproj.github.io/argo-helm"], check=False)
    run(["helm", "repo", "update", "argo"], timeout=180)
    run(
        [
            "helm",
            "upgrade",
            "--install",
            "argocd",
            "argo/argo-cd",
            "--version",
            "10.11.6",
            "--namespace",
            "argocd",
            "--create-namespace",
            "--values",
            str(HERE / "argocd-values.yaml"),
            "--wait",
            "--timeout",
            "12m",
        ],
        timeout=800,
    )
    cm = kubectl_json(["-n", "argocd", "get", "configmap", "argocd-cmd-params-cm"])
    flag = (cm.get("data") or {}).get("applicationsetcontroller.enable.progressive.syncs")
    if flag != "true":
        raise RuntimeError(f"progressive syncs flag is {flag!r}")
    env_json = run(
        [
            "kubectl",
            "-n",
            "argocd",
            "get",
            "deploy",
            "argocd-applicationset-controller",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].env}",
        ]
    ).stdout
    if "ARGOCD_APPLICATIONSET_CONTROLLER_ENABLE_PROGRESSIVE_SYNCS" not in env_json:
        raise RuntimeError("applicationset controller has no progressive-syncs env")
    record_version()


def record_version() -> None:
    client = run(["argocd", "version", "--client"]).stdout.strip()
    server = run(
        ["kubectl", "-n", "argocd", "exec", "deploy/argocd-server", "--", "argocd", "version"],
        check=False,
    ).stdout.strip()
    (EVIDENCE / "argocd-version.txt").write_text(
        "\n".join(
            [
                "source: FieldstateNZ/lucentroot-gitops versions.env",
                "ARGOCD_CHART_VERSION=10.11.6",
                "ARGOCD_APP_VERSION=v3.5.6",
                "helm chart appVersion confirmed: v3.5.6",
                "install: helm upgrade --install argocd argo/argo-cd --version 10.11.6",
                "kind kube-proxy: nftables (this VM rejects kube-proxy iptables statistic rules)",
                "note: lucentroot-gitops sets applicationSet.replicas=0; this run sets 1",
                "note: saas-fabric-hosting is not a repository this account can see",
                "note: FieldstateNZ/infrastructure pins chart 10.1.3, which",
                "      docs/migrating-lucentroot.md says not to carry across",
                "",
                client,
                "",
                server,
                "",
            ]
        )
    )


def install_git_daemon() -> None:
    run(["kubectl", "apply", "-f", str(HERE / "git-daemon.yaml")])
    run(
        [
            "kubectl",
            "-n",
            "git",
            "rollout",
            "status",
            "deploy/git",
            "--timeout=180s",
        ],
        timeout=200,
    )


def assert_git_reachable() -> None:
    run(["kubectl", "-n", "git", "delete", "pod", "git-ls", "--ignore-not-found"], check=False)
    run(
        [
            "kubectl",
            "-n",
            "git",
            "run",
            "git-ls",
            "--restart=Never",
            "--image=quay.io/argoproj/argocd:v3.5.6",
            "--image-pull-policy=IfNotPresent",
            "--command",
            "--",
            "git",
            "ls-remote",
            REPO_URL,
        ]
    )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        phase = run(
            ["kubectl", "-n", "git", "get", "pod", "git-ls", "-o", "jsonpath={.status.phase}"],
            check=False,
        ).stdout
        if phase == "Succeeded":
            log("git daemon is reachable from the cluster")
            return
        if phase == "Failed":
            logs = run(["kubectl", "-n", "git", "logs", "git-ls"], check=False).stdout
            daemon = run(["kubectl", "-n", "git", "logs", "deploy/git"], check=False).stdout
            (EVIDENCE / "git-ls.log").write_text(logs + "\n" + daemon)
            raise RuntimeError("git ls-remote failed")
        time.sleep(2)
    raise RuntimeError("git ls-remote did not finish")


def apply_project_and_root() -> None:
    project = """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: default
  namespace: argocd
spec:
  sourceRepos:
    - '*'
  destinations:
    - namespace: '*'
      server: '*'
  clusterResourceWhitelist:
    - group: '*'
      kind: '*'
  namespaceResourceWhitelist:
    - group: '*'
      kind: '*'
"""
    project_path = TMP / "default-project.yaml"
    project_path.write_text(project)
    run(["kubectl", "apply", "-f", str(project_path)])
    root = (HERE / "platform-root.yaml").read_text().replace("REPO_URL_PLACEHOLDER", REPO_URL)
    root_path = TMP / "platform-root.yaml"
    root_path.write_text(root)
    run(["kubectl", "apply", "-f", str(root_path)])


def wait_until(description: str, timeout: int, predicate) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = predicate()
        if last.get("ok"):
            log(description)
            return last
        time.sleep(2)
    save_json(f"timeout-{description.replace(' ', '-')}.json", last)
    raise RuntimeError(f"timed out waiting for {description}")


def wait_initial() -> dict:
    def pred():
        try:
            views = {name: app_view(name) for name in APPS}
        except RuntimeError:
            return {"ok": False}
        ok = all(view["health"] == "Healthy" and view["sync"] == "Synced" for view in views.values())
        return {"ok": ok, "views": views, "uids": snapshot_uids()}

    return wait_until("initial automated sync healthy", 360, pred)


def appset_status() -> dict:
    raw = kubectl_json(["-n", "argocd", "get", "applicationset", "operator-sign-in"])
    status = raw.get("status") or {}
    return {
        "conditions": status.get("conditions"),
        "applicationStatus": status.get("applicationStatus"),
    }


def v1(baseline: dict) -> dict:
    before = baseline["uids"]
    started = {name: baseline["views"][name]["opStarted"] for name in APPS}
    for name in APPS:
        add_prune_false(WORK / "children" / f"{name}.yaml")
    commit_all("annotate the three Applications Prune=false")
    refresh("platform-root")

    def annotated():
        views = {name: app_view(name) for name in APPS}
        ok = all(
            "Prune=false" in json.dumps(kubectl_json(["-n", "argocd", "get", "application", name])["metadata"].get("annotations") or {})
            for name in APPS
        )
        return {"ok": ok, "views": views}

    wait_until("Prune=false is live on the three Applications", 180, annotated)
    (WORK / "children" / "kustomization.yaml").write_text(APPSET_KUST)
    commit_all("adopt the chain with the operator-sign-in ApplicationSet")
    refresh("platform-root")

    def adopted():
        try:
            status = appset_status()
        except RuntimeError:
            return {"ok": False}
        views = {name: app_view(name) for name in APPS}
        ok = True
        for name, step in ((STEP1, "1"), (STEP2, "2"), (STEP3, "3")):
            view = views[name]
            owners = [item.get("name") for item in view["owners"]]
            auto = (view["syncPolicy"] or {}).get("automated")
            enabled = None if not isinstance(auto, dict) else auto.get("enabled")
            if "operator-sign-in" not in owners:
                ok = False
            if view["labels"].get("fieldstate.nz/rollout-step") != step:
                ok = False
            if auto not in (None, {}) and enabled is not False:
                ok = False
        return {"ok": ok, "views": views, "appset": status}

    adopted_state = wait_until("ApplicationSet adopted the three Applications", 240, adopted)
    after = snapshot_uids()
    uid_changes = changed_uids(before, after)
    op_changes = {
        name: {"before": started[name], "after": adopted_state["views"][name]["opStarted"]}
        for name in APPS
        if started[name] != adopted_state["views"][name]["opStarted"]
    }
    passed = not uid_changes and not op_changes
    detail = {
        "uidChanges": uid_changes,
        "operationStartedAtChanges": op_changes,
        "labels": {name: adopted_state["views"][name]["labels"] for name in APPS},
        "owners": {name: adopted_state["views"][name]["owners"] for name in APPS},
        "syncPolicy": {name: adopted_state["views"][name]["syncPolicy"] for name in APPS},
        "uids": after,
    }
    save_json("v1-migration.json", detail)
    record("V1", passed, detail)
    if not passed:
        raise RuntimeError("V1 failed")
    return after


def watch_rollout(sha: str, expected_marker: str, commit_time: datetime, timeout: int = 180) -> dict:
    samples = []
    deadline = time.monotonic() + timeout
    index = 0
    while time.monotonic() < deadline:
        if index % 5 == 0:
            refresh(STEP2)
            refresh(STEP3)
        current = sample()
        samples.append(current)
        violation = first_violation(samples, sha, expected_marker, commit_time)
        ready = step2_ready(current["step2"], sha)
        step3 = current["step3"]
        done = (
            ready
            and current["marker"] == expected_marker
            and step3["health"] == "Healthy"
            and step3["sync"] == "Synced"
            and step3["revision"] == sha
        )
        if violation is not None or done:
            break
        index += 1
        time.sleep(1)
    else:
        pass
    return {
        "samples": samples,
        "violation": first_violation(samples, sha, expected_marker, commit_time),
        "sawUnready": saw_unready(samples, sha),
        "final": samples[-1] if samples else None,
    }


def v2() -> None:
    rounds = []
    for index in range(1, 11):
        value = f"v2-{index}"
        set_yaml_field(WORK / "workloads/master-instance/mode.yaml", "mode", "succeed")
        set_yaml_field(WORK / "workloads/master-instance/mode.yaml", "marker", value)
        set_yaml_field(WORK / "workloads/saas-fabric-control-plane/configmap.yaml", "marker", value)
        commit_time = datetime.now(timezone.utc)
        sha = commit_all(f"update steps 2 and 3 to {value}")
        watched = watch_rollout(sha, value, commit_time)
        # Keep the timeline, but drop repeated bulky policy blobs from samples.
        slim = []
        for sample_item in watched["samples"]:
            slim.append(
                {
                    "t": sample_item["t"],
                    "marker": sample_item["marker"],
                    "step2": {
                        key: sample_item["step2"][key]
                        for key in ("revision", "sync", "health", "opPhase", "opStarted", "opRevision")
                    },
                    "step3": {
                        key: sample_item["step3"][key]
                        for key in ("revision", "sync", "health", "opPhase", "opStarted", "opRevision")
                    },
                }
            )
        final = watched["final"] or {}
        step2 = (final.get("step2") or {})
        ok = (
            watched["violation"] is None
            and watched["sawUnready"]
            and final
            and final["marker"] == value
            and step2_ready(step2, sha)
        )
        rounds.append(
            {
                "marker": value,
                "sha": sha,
                "ok": ok,
                "sawUnready": watched["sawUnready"],
                "violation": watched["violation"],
                "samples": slim,
                "step2Operation": {
                    "prune": step2.get("prune"),
                    "syncOptions": step2.get("syncOptions"),
                    "initiatedBy": step2.get("initiatedBy"),
                    "retry": step2.get("retry"),
                    "opStarted": step2.get("opStarted"),
                    "opFinished": step2.get("opFinished"),
                },
            }
        )
        log(f"V2 {value}: {'ok' if ok else 'not ok'}")
        if not ok:
            settle_round(sha, value)
    passed = len(rounds) == 10 and all(item["ok"] for item in rounds)
    save_json("v2-rollouts.json", rounds)
    record("V2", passed, {"rounds": len(rounds), "ordered": sum(1 for item in rounds if item["ok"])})


def settle_round(sha: str, expected_marker: str) -> None:
    def pred():
        try:
            step2 = app_view(STEP2)
            step3 = app_view(STEP3)
        except RuntimeError:
            return {"ok": False}
        ok = (
            step2_ready(step2, sha)
            and marker() == expected_marker
            and step3["health"] == "Healthy"
            and step3["sync"] == "Synced"
            and step3["revision"] == sha
        )
        return {"ok": ok}

    try:
        wait_until(f"{expected_marker} settled", 180, pred)
    except RuntimeError:
        log(f"{expected_marker} did not settle; later rounds may be noisy")


def v3() -> str:
    set_yaml_field(WORK / "workloads/master-instance/mode.yaml", "mode", "fail")
    set_yaml_field(WORK / "workloads/master-instance/mode.yaml", "marker", "v3-fail")
    sha = commit_all("force step 2 to fail")
    refresh(STEP2)

    def degraded():
        view = app_view(STEP2)
        return {"ok": view["health"] == "Degraded" and view["sync"] == "Synced" and view["revision"] == sha, "view": view}

    failed = wait_until("step 2 Degraded at the failing revision", 180, degraded)
    blocked = []
    for value in ("v3-a", "v3-b"):
        before = app_view(STEP3)
        before_marker = marker()
        set_yaml_field(WORK / "workloads/saas-fabric-control-plane/configmap.yaml", "marker", value)
        commit_time = datetime.now(timezone.utc)
        commit_sha = commit_all(f"mutate step 3 to {value} while step 2 is Degraded")
        deadline = time.monotonic() + 25
        observed = []
        while time.monotonic() < deadline:
            refresh(STEP3)
            current = sample()
            observed.append(
                {
                    "t": current["t"],
                    "marker": current["marker"],
                    "step3": {
                        key: current["step3"][key]
                        for key in ("revision", "sync", "health", "opPhase", "opStarted")
                    },
                }
            )
            time.sleep(2)
        after = app_view(STEP3)
        held = (
            marker() == before_marker
            and after["opStarted"] == before["opStarted"]
            and not step3_sync_started(after, commit_time)
        )
        blocked.append(
            {
                "marker": value,
                "sha": commit_sha,
                "held": held,
                "beforeOperationStarted": before["opStarted"],
                "afterOperationStarted": after["opStarted"],
                "liveMarker": marker(),
                "samples": observed,
            }
        )
    status = appset_status()
    step3_row = next(
        (row for row in (status.get("applicationStatus") or []) if row.get("application") == STEP3),
        None,
    )
    messages = " ".join((item.get("message") or "") for item in (status.get("conditions") or []))
    waiting = bool(step3_row) and step3_row.get("status") == "Waiting"
    names_step = "step 2" in messages
    passed = all(item["held"] for item in blocked) and waiting and names_step and failed["view"]["health"] == "Degraded"
    detail = {
        "step2": failed["view"],
        "blocked": blocked,
        "applicationSet": status,
        "step3Waiting": waiting,
        "conditionNamesStep2": names_step,
    }
    save_json("v3-failed-step.json", detail)
    record("V3", passed, {"held": passed, "step3Status": None if not step3_row else step3_row.get("status")})
    if not passed:
        raise RuntimeError("V3 failed")
    return sha


def v4() -> str:
    before = app_view(STEP3)
    run(
        [
            "kubectl",
            "-n",
            "operator-system",
            "patch",
            "configmap",
            "control-plane-standin",
            "--type",
            "merge",
            "-p",
            '{"data":{"marker":"v4-mutated"}}',
        ]
    )
    samples = []
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        refresh(STEP3)
        current = sample()
        samples.append({"t": current["t"], "marker": current["marker"], "opStarted": current["step3"]["opStarted"]})
        time.sleep(2)
    after = app_view(STEP3)
    passed = marker() == "v4-mutated" and after["opStarted"] == before["opStarted"]
    detail = {"beforeOperationStarted": before["opStarted"], "after": after, "samples": samples}
    save_json("v4-drift.json", detail)
    record("V4", passed, {"markerStayed": marker(), "syncStarted": after["opStarted"] != before["opStarted"]})
    if not passed:
        raise RuntimeError("V4 failed")
    return before["opStarted"]


def v5(v1_uids: dict) -> None:
    set_yaml_field(WORK / "workloads/master-instance/mode.yaml", "mode", "succeed")
    set_yaml_field(WORK / "workloads/master-instance/mode.yaml", "marker", "v5-fix")
    commit_time = datetime.now(timezone.utc)
    sha = commit_all("fix step 2")
    watched = watch_rollout(sha, "v3-b", commit_time, timeout=240)
    final = watched["final"] or {}
    step2 = final.get("step2") or {}
    step3 = final.get("step3") or {}
    passed = (
        watched["violation"] is None
        and watched["sawUnready"]
        and step2_ready(step2, sha)
        and final.get("marker") == "v3-b"
        and step3.get("health") == "Healthy"
        and step3.get("sync") == "Synced"
        and step3.get("revision") == sha
    )
    uids = snapshot_uids()
    state_same = changed_uids(
        v1_uids,
        uids,
        [
            "namespace/master-instance-state",
            "secret/tfstate-default-master-instance",
        ],
    )
    passed = passed and not state_same
    detail = {
        "sha": sha,
        "violation": watched["violation"],
        "sawUnready": watched["sawUnready"],
        "finalMarker": final.get("marker"),
        "step2": {key: step2.get(key) for key in ("revision", "sync", "health", "opStarted", "opFinished")},
        "step3": {key: step3.get(key) for key in ("revision", "sync", "health", "opStarted", "opFinished")},
        "stateUidChanges": state_same,
        "applicationSet": appset_status(),
    }
    save_json("v5-recovery.json", detail)
    record("V5", passed, {"recovered": passed, "sha": sha})
    if not passed:
        raise RuntimeError("V5 failed")


def capture_history() -> None:
    """Write the history Argo CD stores on each Application.

    `argocd app history` prints `.status.history`. Reading it from the API
    avoids the admin password and a port-forward. deployStartedAt and
    deployedAt are the timestamps.
    """
    chunks = [
        "# Application .status.history",
        "# This is the record `argocd app history` prints.",
        "",
    ]
    for name in APPS:
        history = app_view(name)["history"]
        save_json(f"history-{name}.json", history)
        chunks.append(f"## {name}")
        chunks.append(f"{'ID':<6} {'DEPLOY STARTED (UTC)':<28} {'DEPLOYED (UTC)':<28} REVISION")
        for item in history:
            chunks.append(
                f"{str(item.get('id')):<6} "
                f"{(item.get('deployStartedAt') or ''):<28} "
                f"{(item.get('deployedAt') or ''):<28} "
                f"{item.get('revision')}"
            )
        chunks.append("")
    (EVIDENCE / "argocd-app-history.txt").write_text("\n".join(chunks) + "\n")
    log("recorded Application history")


def v6(v1_uids: dict) -> None:
    before = snapshot_uids()
    # Kubernetes garbage-collects owned Applications as soon as the
    # ApplicationSet is deleted. preserveResourcesOnDeletion removes those
    # owner refs from the controller, and that loses the race. Strip them
    # first so the rollback cannot delete the Applications.
    for name in APPS:
        raw = kubectl_json(["-n", "argocd", "get", "application", name])
        if (raw.get("metadata") or {}).get("ownerReferences"):
            run(
                [
                    "kubectl",
                    "-n",
                    "argocd",
                    "patch",
                    "application",
                    name,
                    "--type",
                    "json",
                    "-p",
                    '[{"op": "remove", "path": "/metadata/ownerReferences"}]',
                ]
            )
    (WORK / "children" / "kustomization.yaml").write_text(APPS_KUST)
    commit_all("roll back to hand-written Applications")
    refresh("platform-root")

    def restored():
        proc = run(["kubectl", "-n", "argocd", "get", "applicationset", "operator-sign-in"], check=False)
        if proc.returncode == 0:
            return {"ok": False, "reason": "applicationset still present"}
        try:
            views = {name: app_view(name) for name in APPS}
        except RuntimeError:
            return {"ok": False, "reason": "an Application is missing"}
        ok = True
        for view in views.values():
            auto = (view["syncPolicy"] or {}).get("automated") or {}
            if not (auto.get("prune") and auto.get("selfHeal")):
                ok = False
        return {"ok": ok, "views": views}

    restored_state = wait_until("hand-written Applications restored", 240, restored)
    after = snapshot_uids()
    keep = [
        "application/master-instance-credential",
        "application/master-instance",
        "application/saas-fabric-control-plane",
        "configmap/credential-standin",
        "configmap/convergence-mode",
        "configmap/control-plane-standin",
        "job/convergence",
        "namespace/master-instance-state",
        "secret/tfstate-default-master-instance",
    ]
    rollback_changes = changed_uids(before, after, keep)
    state_vs_v1 = changed_uids(
        v1_uids,
        after,
        ["namespace/master-instance-state", "secret/tfstate-default-master-instance"],
    )
    rollback_ok = not rollback_changes and not state_vs_v1

    # platform-root would otherwise recreate the Application. It is not itself
    # managed by Argo CD, so dropping its automated sync stays dropped.
    run(
        [
            "kubectl",
            "-n",
            "argocd",
            "patch",
            "application",
            "platform-root",
            "--type",
            "json",
            "-p",
            '[{"op": "remove", "path": "/spec/syncPolicy/automated"}]',
        ]
    )
    state_before_delete = snapshot_uids()
    run(["kubectl", "-n", "argocd", "delete", "application", STEP2, "--wait=false"])
    deadline = time.monotonic() + 120
    job_gone = False
    app_gone = False
    while time.monotonic() < deadline:
        app_gone = uid_of("application", "argocd", STEP2) is None
        job_gone = uid_of("job", "operator-system", "convergence") is None
        if app_gone and job_gone:
            break
        time.sleep(2)
    state_after = snapshot_uids()
    secret_same = (
        state_after.get("secret/tfstate-default-master-instance")
        == state_before_delete.get("secret/tfstate-default-master-instance")
        and state_after.get("secret/tfstate-default-master-instance") is not None
    )
    namespace_same = (
        state_after.get("namespace/master-instance-state")
        == state_before_delete.get("namespace/master-instance-state")
        and state_after.get("namespace/master-instance-state") is not None
    )
    probe_ok = secret_same and namespace_same and job_gone and app_gone
    detail = {
        "rollbackUidChanges": rollback_changes,
        "stateChangedSinceV1": state_vs_v1,
        "automated": {
            name: restored_state["views"][name]["syncPolicy"] for name in APPS if name in restored_state["views"]
        },
        "deleteProbe": {
            "applicationGone": app_gone,
            "jobGone": job_gone,
            "secretSame": secret_same,
            "namespaceSame": namespace_same,
            "uidsBefore": state_before_delete,
            "uidsAfter": state_after,
        },
    }
    save_json("v6-rollback.json", detail)
    record("V6", rollback_ok and probe_ok, {"rollback": rollback_ok, "deleteProbe": probe_ok})
    if not (rollback_ok and probe_ok):
        raise RuntimeError("V6 failed")


def write_summary() -> None:
    nz = datetime.now(NZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    utc = now_iso()
    lines = [
        "# V1–V6 evidence",
        "",
        f"- When: {nz} ({utc})",
        "- Operator: Cursor cloud agent, work item W2. Throwaway kind cluster on this VM. Not LucentRoot.",
        "- Argo CD: chart 10.11.6, app v3.5.6, from FieldstateNZ/lucentroot-gitops versions.env.",
        "- Stand-ins only. No cloud resources, no DNS, no LucentRoot API.",
        "",
        "| Test | Result |",
        "|---|---|",
    ]
    for item in RESULTS:
        lines.append(f"| {item['test']} | {item['result']} |")
    lines.append("")
    lines.append("Structured records: `results.json`, `v1-migration.json`, `v2-rollouts.json`,")
    lines.append("`v3-failed-step.json`, `v4-drift.json`, `v5-recovery.json`, `v6-rollback.json`,")
    lines.append("`argocd-app-history.txt`.")
    lines.append("")
    (EVIDENCE / "README.md").write_text("\n".join(lines) + "\n")


def dump_failure() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    for args in (
        ["-n", "argocd", "get", "applications,applicationsets"],
        ["-n", "argocd", "logs", "deploy/argocd-applicationset-controller", "--tail=200"],
        ["-n", "git", "logs", "deploy/git", "--tail=80"],
    ):
        proc = run(["kubectl", *args], check=False)
        name = "failure-" + "-".join(arg.replace("/", "_") for arg in args if not arg.startswith("-")) + ".log"
        (EVIDENCE / name).write_text((proc.stdout or "") + (proc.stderr or ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--keep-cluster", action="store_true")
    parser.add_argument("--only", choices=["v6"], help="continue from V6 on the live throwaway cluster; keep existing evidence")
    args = parser.parse_args()
    self_check()
    if args.self_check:
        return 0
    if args.only != "v6" and EVIDENCE.exists():
        shutil.rmtree(EVIDENCE)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    success = False
    try:
        if args.only == "v6":
            assert_loopback()
            RESULTS.extend(json.loads((EVIDENCE / "results.json").read_text()))
            v1_uids = json.loads((EVIDENCE / "v1-migration.json").read_text())["uids"]
            capture_history()
            v6(v1_uids)
            success = all(item["result"] == "pass" for item in RESULTS)
        else:
            run(["kind", "delete", "cluster", "--name", CLUSTER], check=False)
            prepare_git()
            create_cluster()
            load_images()
            install_git_daemon()
            install_argocd()
            assert_git_reachable()
            apply_project_and_root()
            baseline = wait_initial()
            save_json("baseline-uids.json", baseline["uids"])
            v1_uids = v1(baseline)
            v2()
            v3()
            v4()
            v5(v1_uids)
            capture_history()
            v6(v1_uids)
            success = all(item["result"] == "pass" for item in RESULTS)
    except Exception as exc:
        log(f"run failed: {exc}")
        try:
            dump_failure()
        except Exception as dump_exc:
            log(f"failure dump failed: {dump_exc}")
        success = False
    finally:
        write_summary()
        if success and not args.keep_cluster:
            run(["kind", "delete", "cluster", "--name", CLUSTER], check=False)
        elif not success:
            log(f"cluster left up for inspection: KUBECONFIG={KUBECONFIG}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
