#!/usr/bin/env python3
"""Package upstream MetaCubeX/mihomo bare binaries into OpenWrt apk v3 packages.

Upstream ships no .ipk/.apk assets, so we build the package ourselves:
download the official release binary for each OpenWrt arch, assemble a
buildroot, and let `apk mkpkg` (apk-tools 3.x, inside an alpine container)
produce a real v3 apk that the feed's `apk mkndx` step can index.

Output: <out>/<arch>/mihomo-<ver>-r1.apk
"""

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

INIT_SCRIPT = """#!/bin/sh /etc/rc.common
START=99
USE_PROCD=1

start_service() {
\tprocd_open_instance
\tprocd_set_param command /usr/bin/mihomo -d /etc/mihomo
\tprocd_set_param respawn
\tprocd_set_param stdout 1
\tprocd_set_param stderr 1
\tprocd_close_instance
}

service_triggers() {
\tprocd_add_reload_trigger mihomo
}
"""

CONFIG_FILE = """# mihomo core configuration placeholder.
# Put your config.yaml here: /etc/mihomo/config.yaml
"""


def log(msg):
    print(msg, flush=True)


def gh_json(url, token):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "zwrt-feed-builder")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def download(url, dest):
    log("  downloading %s" % url)
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return os.path.getsize(dest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/mihomo.json")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--version", default="", help="override version, e.g. 1.19.34")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if not cfg.get("enabled", True):
        log("mihomo: disabled in config, skip")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    repo = cfg["release_repo"]
    rel = gh_json("https://api.github.com/repos/%s/releases/latest" % repo, token)
    tag = rel["tag_name"]
    ver = args.version or re.sub(r"^v", "", tag)
    log("mihomo: upstream %s -> version %s" % (tag, ver))

    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    if not assets:
        raise SystemExit("mihomo: release %s has no assets" % tag)

    name = cfg.get("name", "mihomo")
    pkgver = "%s-r%d" % (ver, int(cfg.get("revision", 1)))
    depends = cfg.get("depends", ["ca-bundle", "kmod-tun"])
    desc = cfg.get("description", "mihomo meta core")
    license_ = cfg.get("license", "GPL-3.0")
    url = cfg.get("url", "https://github.com/" + repo)

    work = tempfile.mkdtemp(prefix="mihomo-pkg-")
    failed = []
    try:
        for arch, pattern in cfg["asset_patterns"].items():
            asset = pattern.format(v=ver)
            if asset not in assets:
                log("mihomo[%s]: asset %s missing in release, skip" % (arch, asset))
                failed.append((arch, "missing asset " + asset))
                continue

            log("mihomo[%s]: packaging from %s" % (arch, asset))
            gz_path = os.path.join(work, "bin.gz")
            dl = download(assets[asset], gz_path)

            bin_path = os.path.join(work, "bin")
            with gzip.open(gz_path, "rb") as src, open(bin_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            bin_size = os.path.getsize(bin_path)
            log("  gz=%d B, binary=%d B" % (dl, bin_size))

            # One buildroot per arch and never delete it from the host: after the
            # container chowns it to root, the runner user cannot remove it.
            safe = re.sub(r"[^0-9A-Za-z_.-]", "_", arch)
            root = os.path.join(work, "root-" + safe)
            if os.path.isdir(root):
                root = root + "-1"
            os.makedirs(os.path.join(root, "usr/bin"))
            os.makedirs(os.path.join(root, "etc/init.d"))
            os.makedirs(os.path.join(root, "etc/config"))
            os.makedirs(os.path.join(root, "etc/mihomo"))

            shutil.copy2(bin_path, os.path.join(root, "usr/bin/mihomo"))
            os.chmod(os.path.join(root, "usr/bin/mihomo"), 0o755)

            with open(os.path.join(root, "etc/init.d/mihomo"), "w", encoding="utf-8", newline="\n") as f:
                f.write(INIT_SCRIPT)
            os.chmod(os.path.join(root, "etc/init.d/mihomo"), 0o755)

            with open(os.path.join(root, "etc/config/mihomo"), "w", encoding="utf-8", newline="\n") as f:
                f.write(CONFIG_FILE)
            os.chmod(os.path.join(root, "etc/config/mihomo"), 0o644)

            with open(os.path.join(root, "etc/mihomo/README"), "w", encoding="utf-8", newline="\n") as f:
                f.write("Place your mihomo config.yaml in this directory.\n")
            os.chmod(os.path.join(root, "etc/mihomo/README"), 0o644)

            out_apk = os.path.join(work, "out-%s.apk" % safe)

            cmd = [
                "docker", "run", "--rm",
                "-v", "%s:/work" % work,
                "alpine:latest", "sh", "-c",
                " && ".join([
                    # files are staged by the runner uid; normalize ownership to root
                    # inside the container so mkpkg records root:root instead of nobody
                    "chown -R 0:0 /work/root-%s" % safe,
                    "apk mkpkg --compat 3.0.0_pre1 -F /work/root-%s" % safe +
                    " -I name:%s -I version:%s" % (name, pkgver) +
                    " -I description:%s -I arch:%s" % (json.dumps(desc), arch) +
                    " -I license:%s -I origin:%s" % (license_, name) +
                    " -I maintainer:%s -I url:%s" % (cfg.get("maintainer", "ZWRT"), url) +
                    ' -I "depends:%s"' % " ".join(depends) +
                    (' -I "provides:%s"' % " ".join(cfg["provides"]) if cfg.get("provides") else "") +
                    (' -I "replaces:%s"' % " ".join(cfg["replaces"]) if cfg.get("replaces") else "") +
                    " -o /work/out-%s.apk" % safe,
                    "apk verify --allow-untrusted /work/out-%s.apk" % safe,
                    "apk adbdump /work/out-%s.apk" % safe + " | head -20",
                ]),
            ]
            log("  running apk mkpkg for %s" % arch)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                log(proc.stdout[-3000:])
                log(proc.stderr[-3000:])
                failed.append((arch, "mkpkg exit %d" % proc.returncode))
                continue
            log(proc.stdout.strip())

            if not os.path.exists(out_apk):
                failed.append((arch, "apk not produced"))
                continue

            arch_dir = os.path.join(args.out, arch)
            os.makedirs(arch_dir, exist_ok=True)
            for stale in os.listdir(arch_dir):
                if stale.startswith(name + "-") and stale.endswith(".apk"):
                    os.remove(os.path.join(arch_dir, stale))
            dest = os.path.join(arch_dir, "%s-%s.apk" % (name, pkgver))
            shutil.copy2(out_apk, dest)
            log("  -> %s (%d B)" % (dest, os.path.getsize(dest)))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if failed:
        for arch, why in failed:
            log("mihomo[%s]: FAILED - %s" % (arch, why))
        return 1
    log("mihomo: all architectures packaged for %s" % pkgver)
    return 0


if __name__ == "__main__":
    sys.exit(main())
