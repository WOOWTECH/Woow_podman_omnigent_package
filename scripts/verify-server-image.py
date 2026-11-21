#!/usr/bin/env python3
"""Verify the package pin against a build receipt, optionally against an actual image.

config/server-image.json must be populated from the artifact producer's verified receipt.
No development placeholder or tag-derived provenance is accepted. See docs/mcp-upgrade-rollback.md.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IMAGE = re.compile(r"ghcr\.io/woowtech/[a-z0-9]+(?:[._-][a-z0-9]+)*@sha256:[0-9a-f]{64}")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def setting(path, prefix):
    lines = [line[len(prefix):] for line in path.read_text().splitlines() if line.startswith(prefix)]
    require(len(lines) == 1, f"expected one {prefix} in {path.name}")
    return lines[0]


def check_receipt(repo, receipt):
    version = (repo / "VERSION").read_text().strip()
    require(re.fullmatch(r"\d+\.\d+\.\d+-\d+", version), "invalid package VERSION")
    core = version.rsplit("-", 1)[0]
    require(setting(repo / "Containerfile.runner", "ARG OMNIGENT_VERSION=") == core,
            "runner core version differs from package upstream version")
    require(setting(repo / "Containerfile.runner", "ARG PI_CODING_AGENT_VERSION=") == "0.85.1",
            "Pi compatibility requires a separate reviewed upgrade")
    require(setting(repo / "quadlet/omnigent-runner.container", "Image=") ==
            f"localhost/woow-omnigent-runner:{version}", "runner tag differs from package revision")
    require(receipt.get("coreVersion") == core, "receipt coreVersion differs from package upstream version")
    require(receipt.get("schemaVersion") == 1 and receipt.get("publishable") is True,
            "receipt must be a publishable source build, never a local snapshot")
    # Both variants may share a package repository. The receipt's explicit variant
    # and generic base image determine compatibility, never the repository's name.
    require(receipt.get("component") == "server" and receipt.get("variant") == "server",
            "Podman requires the generic server component and variant")
    require(IMAGE.fullmatch(receipt.get("image", "")), "receipt image must be a WOOW registry digest")
    repository = receipt["image"].split("@", 1)[0]
    for key, length in (("sourceCommit", 40), ("wheelSha256", 64), ("uiSha256", 64),
                        ("contentSha256", 64), ("contentManifestSha256", 64)):
        require(re.fullmatch(f"[0-9a-f]{{{length}}}", receipt.get(key, "")), f"missing/invalid {key}")
    require(receipt.get("imageDigest") == receipt["image"].split("@")[1], "imageDigest differs from image")
    for key in ("runId", "runAttempt"):
        require(re.fullmatch(r"[1-9][0-9]*", receipt.get(key, "")), f"missing/invalid {key}")
    tag = (f"{repository}:{core}-mcp.1-server-sha-{receipt['sourceCommit']}"
           f"-run-{receipt['runId']}-{receipt['runAttempt']}")
    require(receipt.get("tag") == tag, "candidate tag differs from the source/run identity")
    require(setting(repo / "quadlet/omnigent-server.container", "Image=") == receipt["tag"] + "@" + receipt["imageDigest"],
            "server Quadlet differs from receipt tag@digest")
    require(re.fullmatch(r"ghcr\.io/omnigent-ai/omnigent-server:v" + re.escape(core) + r"@sha256:[0-9a-f]{64}",
                         receipt.get("baseImage", "")), "baseImage must pin the matching generic server base")
    require(set(receipt.get("architecture", [])) == {"linux/amd64", "linux/arm64"},
            "receipt must cover amd64 and arm64")
    platforms = receipt.get("platforms", [])
    require(len(platforms) == 2 and {p.get("architecture") for p in platforms} == {"linux/amd64", "linux/arm64"},
            "both architecture image receipts are required")
    require(all(IMAGE.fullmatch(p.get("image", "")) and p["image"].split("@", 1)[0] == repository
                for p in platforms), "per-architecture image digest must use the manifest repository")
    ci = receipt.get("ci", {})
    require(ci.get("GITHUB_REPOSITORY") == "WOOWTECH/omnigent" and ci.get("GITHUB_SHA") == receipt["sourceCommit"]
            and ci.get("GITHUB_RUN_ID") == receipt["runId"] and ci.get("GITHUB_RUN_ATTEMPT") == receipt["runAttempt"],
            "CI source/run identity differs from image receipt")


# Run inside the image with its venv, independent of the startup entrypoint/database.
# The producer's verifier checks wheel, content manifest, UI and installed file hashes,
# imports, source identity and the existing security fixes before returning a receipt.
RUNTIME_PROBE = r'''
import importlib.metadata, json, pathlib, subprocess, sys
subprocess.run([sys.executable, "/opt/woow-artifact/verify.py", "runtime",
                "--artifact", "/opt/woow-artifact", "--source-commit", sys.argv[1],
                "--debian-security"], check=True, stdout=subprocess.DEVNULL)
receipt = json.loads(pathlib.Path("/opt/woow-artifact/WOOW_BUILD_RECEIPT.json").read_text())
print(json.dumps({"coreVersion": importlib.metadata.version("omnigent"), "receipt": receipt}))
'''


def check_runtime(expected, info, runtime):
    require(info.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") ==
            expected["sourceCommit"], "image OCI revision differs from sourceCommit")
    require(info.get("Config", {}).get("Labels", {}).get("io.woowtech.omnigent.base-image") ==
            expected["baseImage"], "image OCI base differs from exact receipt baseImage")
    arch = "linux/" + info.get("Architecture", "")
    require(arch in expected["architecture"], "unverified runtime architecture")
    # Engines can expose either the pulled index or its selected child as RepoDigests.
    accepted_digests = {expected["image"]} | {p["image"] for p in expected["platforms"] if p["architecture"] == arch}
    require(bool(accepted_digests.intersection(info.get("RepoDigests", []))),
            "running image does not have the pinned registry digest")
    require(runtime.get("coreVersion") == expected["coreVersion"], "installed core version differs from receipt")
    for key in ("sourceCommit", "wheelSha256", "uiSha256", "contentSha256", "contentManifestSha256"):
        require(runtime.get("receipt", {}).get(key) == expected[key], f"runtime receipt differs: {key}")


def command(*args, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, check=False)
    # Container startup errors can include deployment details: keep them out of public CI receipts.
    require(result.returncode == 0, f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=REPO / "config/server-image.json")
    parser.add_argument("--engine", choices=("podman", "docker"), default="podman")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--container", help="inspect an existing container without changing it")
    mode.add_argument("--image", action="store_true", help="pull and probe the pinned image in an isolated container")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    args = parser.parse_args()
    try:
        require(args.receipt.is_file(), "verified config/server-image.json is required; finalize it from the artifact build receipt")
        receipt = json.loads(args.receipt.read_text())
        check_receipt(REPO, receipt)
        if args.container or args.image:
            engine = args.engine
            if args.container:
                image_id = command(engine, "container", "inspect", "--format", "{{.Image}}", args.container).strip()
                probe = command(engine, "exec", "-i", args.container, "/opt/venv/bin/python", "-", receipt["sourceCommit"], input=RUNTIME_PROBE)
            else:
                platform = ["--platform", args.platform] if args.platform else []
                command(engine, "pull", *platform, receipt["image"])
                image_id = receipt["image"]
                probe = command(engine, "run", "--rm", "-i", *platform, "--network=none", "--read-only",
                                "--entrypoint=/opt/venv/bin/python", receipt["image"], "-", receipt["sourceCommit"], input=RUNTIME_PROBE)
            info = json.loads(command(engine, "image", "inspect", image_id))[0]
            check_runtime(receipt, info, json.loads(probe))
            if args.platform:
                require(info["Architecture"] == args.platform.split("/")[1], "runtime architecture differs from requested platform")
            print("ok   pinned image, OCI revision, installed MCP/history/migration/UI and runtime receipt verified")
        else:
            print("ok   package/core versions and immutable server receipt agree (runtime not inspected)")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
