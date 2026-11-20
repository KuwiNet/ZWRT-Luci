#!/usr/bin/env python3
"""Package upstream MetaCubeX/mihomo bare binaries into OpenWrt packages.

Upstream ships no .ipk/.apk assets, so we build them ourselves:
download the official release binary for each OpenWrt arch, assemble a
buildroot, then produce

  * a real apk v3 package via `apk mkpkg` (apk-tools 3.x, alpine container)
  * a classic opkg .ipk (ar container: debian-binary + control.tar.gz + data.tar.gz)

and refresh the per-arch opkg ``Packages`` index so the new .ipk is listed
(the apk index is rebuilt later by `apk mkndx` in the workflow).

Output: <out>/<arch>/<name>-<ver>-r1.apk  and  .ipk
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
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
    with urllib.request.urlopen(url) as r:
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    return os.path.getsize(dest)


# ---------------------------------------------------------------- .ipk builder

def _tar_gz(entries):
    """entries: list of (name, bytes, mode). Deterministic tar.gz."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _ar_member(name, data):
    hdr = "{:<16}{:<12}{:<6}{:<6}{:<8}{:<10}`\n".format(
        name + "/", "0", "0", "0", "100644", str(len(data))
    )
    pad = b"\n" if len(data) % 2 else b""
    return hdr.encode("ascii") + data + pad


def build_ipk(root, arch, name, pkgver, meta, out_path):
    control_lines = [
        "Package: " + name,
        "Version: " + pkgver,
        "Architecture: " + arch,
        "Section: net",
        "Category: Network",
        "Title: " + meta["description"],
        "Maintainer: " + meta["maintainer"],
        "License: " + meta["license"],
        "URL: " + meta["url"],
    ]
    if meta.get("depends"):
        control_lines.append("Depends: " + ", ".join(meta["depends"]))
    if meta.get("provides"):
        control_lines.append("Provides: " + ", ".join(meta["provides"]))
    if meta.get("replaces"):
        control_lines.append("Replaces: " + ", ".join(meta["replaces"]))
    if meta.get("conflicts"):
        control_lines.append("Conflicts: " + ", ".join(meta["conflicts"]))

    installed = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            installed += os.path.getsize(os.path.join(dirpath, fn))
    control_lines.append("Installed-Size: %d" % (installed // 1024))
    control_lines.append("Description: " + meta["description"])
    control = ("\n".join(control_lines) + "\n").encode("utf-8")

    conffiles = ""
    if meta.get("conf_files"):
        conffiles = ("\n".join(meta["conf_files"]) + "\n").encode("utf-8")

    control_entries = [("./control", control, 0o644)]
    if conffiles:
        control_entries.append(("./conffiles", conffiles, 0o644))
    control_tgz = _tar_gz(control_entries)

    data_entries = []
    for dirpath, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            data_entries.append(("./" + rel_dir.replace(os.sep, "/"), b"", 0o755))
        for fn in sorted(files):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            # derive the mode from the path instead of st_mode: on Windows
            # os.stat().st_mode reports 0666 for everything
            if rel.startswith("usr/bin/") or rel.startswith("etc/init.d/"):
                mode = 0o755
            else:
                mode = 0o644
            with open(full, "rb") as f:
                data_entries.append(("./" + rel, f.read(), mode))
    data_tgz = _tar_gz(data_entries)

    debian_binary = b"2.0\n"
    with open(out_path, "wb") as out:
        out.write(b"!<arch>\n")
        out.write(_ar_member("debian-binary", debian_binary))
        out.write(_ar_member("control.tar.gz", control_tgz))
        out.write(_ar_member("data.tar.gz", data_tgz))
    return installed


# ------------------------------------------------------------- opkg index patch

def _file_digest(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def update_opkg_index(arch_dir, name, block_fields):
    """Rewrite <arch_dir>/Packages (+ .gz) keeping other blocks, replacing ours."""
    path = os.path.join(arch_dir, "Packages")
    old_blocks = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        for b in raw.split("\n\n"):
            if b.strip():
                old_blocks.append(b.strip())

    kept = []
    for b in old_blocks:
        lines = b.split("\n")
        if any(l.strip() == "Package: " + name for l in lines):
            continue
        if any(l.startswith("Package: ") and l.split(": ", 1)[1].strip() == name for l in lines):
            continue
        kept.append(b)

    new_block = "\n".join(block_fields)
    text = "\n\n".join(kept + [new_block]) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    with open(path + ".gz", "wb") as f:
        f.write(gzip.compress(text.encode("utf-8"), 9, mtime=0))
    return len(kept) + 1


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
    maintainer = cfg.get("maintainer", "ZWRT-Feed")
    meta = {
        "description": desc,
        "maintainer": maintainer,
        "license": license_,
        "url": url,
        "depends": depends,
        "provides": cfg.get("provides"),
        "replaces": cfg.get("replaces"),
        "conflicts": cfg.get("conflicts"),
        "conf_files": cfg.get("conf_files", ["/etc/config/mihomo"]),
    }

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

            arch_dir = os.path.join(args.out, arch)
            os.makedirs(arch_dir, exist_ok=True)
            for stale in os.listdir(arch_dir):
                if stale.startswith(name + "-") and stale.endswith((".apk", ".ipk")):
                    os.remove(os.path.join(arch_dir, stale))

            base = "%s-%s" % (name, pkgver)

            # ---- apk v3
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
                    " -I maintainer:%s -I url:%s" % (maintainer, url) +
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
            dest_apk = os.path.join(arch_dir, base + ".apk")
            shutil.copy2(out_apk, dest_apk)
            log("  -> %s (%d B)" % (dest_apk, os.path.getsize(dest_apk)))

            # ---- ipk (opkg)
            dest_ipk = os.path.join(arch_dir, base + ".ipk")
            try:
                build_ipk(root, arch, name, pkgver, meta, dest_ipk)
            except Exception as e:
                log("  ipk build failed for %s: %r" % (arch, e))
                failed.append((arch, "ipk: %r" % e))
                continue
            size = os.path.getsize(dest_ipk)
            log("  -> %s (%d B)" % (dest_ipk, size))

            # ---- refresh opkg index so the new .ipk is visible
            block = [
                "Package: " + name,
                "Version: " + pkgver,
                "Depends: " + ", ".join(depends),
            ]
            if cfg.get("provides"):
                block.append("Provides: " + ", ".join(cfg["provides"]))
            if cfg.get("replaces"):
                block.append("Replaces: " + ", ".join(cfg["replaces"]))
            if cfg.get("conflicts"):
                block.append("Conflicts: " + ", ".join(cfg["conflicts"]))
            inst = 0
            for dirpath, _d, files in os.walk(root):
                for fn in files:
                    inst += os.path.getsize(os.path.join(dirpath, fn))
            block += [
                "Architecture: " + arch,
                "Section: net",
                "Category: Network",
                "Title: " + desc,
                "Maintainer: " + maintainer,
                "License: " + license_,
                "URL: " + url,
                "Installed-Size: %d" % (inst // 1024),
                "Filename: " + base + ".ipk",
                "Size: %d" % size,
                "MD5Sum: " + _file_digest(dest_ipk, "md5"),
                "SHA256sum: " + _file_digest(dest_ipk, "sha256"),
                "Description: " + desc,
            ]
            n = update_opkg_index(arch_dir, name, block)
            log("  opkg index refreshed: %s/Packages (%d blocks)" % (arch, n))
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
