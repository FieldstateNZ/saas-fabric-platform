#!/usr/bin/env python3
"""Render every manifest this repository produces, the way Argo CD will.

    scripts/render.py [output-directory]

For each environment: build the bootstrap set and the environment's Applications
with Kustomize, then read those Applications and render what each one actually
points at -- `helm template` against the pinned chart with the same valueFiles
Argo CD reads, or `kustomize build` against the same overlay path.

Nothing here restates what an Application declares. If a chart version or a
values path changes, this follows it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ENVIRONMENTS = ("lucentroot", "production")
KUBE_VERSION = "1.30.0"


def kustomize(path: Path) -> str:
    exe = ["kustomize", "build"] if shutil.which("kustomize") else ["kubectl", "kustomize"]
    return run(exe + [str(path)])


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(argv)}\n{result.stderr.strip()}")
    return result.stdout


def value_files(root: Path, sources: list[dict]) -> list[str]:
    """Resolve $platform-prefixed valueFiles, skipping ones that do not exist.

    Argo CD is configured with ignoreMissingValueFiles, so an environment that
    has nothing to override simply omits the file.
    """
    resolved = []
    for entry in sources[0].get("helm", {}).get("valueFiles", []):
        relative = entry.split("/", 1)[1] if entry.startswith("$") else entry
        path = root / relative
        if path.is_file():
            resolved.append(str(path))
        else:
            print(f"       (no {relative})")
    return resolved


def render_application(root: Path, app: dict, out_dir: Path) -> None:
    name = app["metadata"]["name"]
    spec = app["spec"]
    namespace = spec["destination"]["namespace"]
    target = out_dir / f"{name}.yaml"

    if "sources" in spec:
        chart = spec["sources"][0]
        argv = [
            "helm", "template", name, chart["chart"],
            "--repo", chart["repoURL"],
            "--version", chart["targetRevision"],
            "--namespace", namespace,
            "--kube-version", KUBE_VERSION,
            "--include-crds",
        ]
        for values in value_files(root, spec["sources"]):
            argv += ["--values", values]
        print(f"    helm       {name} ({chart['chart']} {chart['targetRevision']})")
    else:
        argv = None
        print(f"    kustomize  {name} ({spec['source']['path']})")

    output = run(argv) if argv else kustomize(root / spec["source"]["path"])
    target.write_text(output)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else root / ".render"

    # Build into a scratch directory and swap it in at the end, so a failed or
    # interrupted render never leaves a half-written tree for the validators to
    # read as if it were complete.
    staging = out.with_name(f"{out.name}.{os.getpid()}")
    if staging.exists():
        shutil.rmtree(staging)

    for environment in ENVIRONMENTS:
        print(f"==> {environment}")
        env_out = staging / environment
        (env_out / "applications").mkdir(parents=True)

        (env_out / "bootstrap.yaml").write_text(
            kustomize(root / "environments" / environment / "bootstrap")
        )
        print("    kustomize  bootstrap")

        rendered = kustomize(root / "environments" / environment)
        (env_out / "platform.yaml").write_text(rendered)
        print("    kustomize  platform")

        for doc in yaml.safe_load_all(rendered):
            if doc and doc.get("kind") == "Application":
                render_application(root, doc, env_out / "applications")

    if out.exists():
        shutil.rmtree(out)
    staging.rename(out)

    print(f"\nRendered into {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
