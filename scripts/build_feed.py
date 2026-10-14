#!/usr/bin/env python3
"""
build_feed.py — 从各插件的 GitHub Release 拉取 ipk，生成 OpenWrt opkg 软件源索引。

流程：
  1. 读取 config/feed.json（插件仓库列表 + 目标架构列表）
  2. 对每个插件调用 GitHub Releases API，扫描最近 N 个 release，
     按 asset_regex 过滤 *.ipk 资产，按包名去重保留版本最高的
  3. 下载到 work/all/
  4. 解析每个 ipk（ar 容器 -> control.tar.gz -> control），
     生成 Packages 条目（追加 Filename/Size/MD5Sum/SHA256sum/Installed-Size）
  5. 生成 all/Packages，并为每个目标架构生成 <arch>/Packages
     （架构索引内包含全部 Architecture: all 的包，all 包 Filename 指向 all/ 目录）
  6. gzip 生成各 Packages.gz

用法：
  python scripts/build_feed.py --config config/feed.json --out dist --work .work
可选环境变量 GH_TOKEN：用于提升 GitHub API 速率限制（未设置时用匿名请求）。
"""

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.request
import urllib.error
import zlib

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def api_get(url, token_env="GH_TOKEN"):
    """GET 一个 URL，返回响应字节。带可选的 GitHub token。"""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "zwrt-opkg-builder"})
    token = os.environ.get(token_env)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def download(url, dest, token_env="GH_TOKEN"):
    req = urllib.request.Request(url, headers={"User-Agent": "zwrt-opkg-builder"})
    token = os.environ.get(token_env)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    return dest


def read_container(path):
    """读取 ipk 容器，返回 {member_name: bytes}。

    兼容两种打包格式：
      - 经典 ar 容器（magic: !<arch>）
      - 新式 gzip-tar 容器（magic: 1f 8b，解压后为含 debian-binary/
        control.tar.gz / data.tar.gz 的 tar，部分新构建工具以此打包）
    """
    with open(path, "rb") as f:
        head = f.read(8)
    members = {}
    if head.startswith(b"\x1f\x8b"):  # gzip-wrapped tar
        with gzip.open(path, "rb") as gz:
            payload = gz.read()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    nm = m.name[2:] if m.name.startswith("./") else m.name
                    members[nm] = tf.extractfile(m).read()
        return members
    if head[:8] == b"!<arch>\n":  # 经典 ar
        with open(path, "rb") as f:
            f.read(8)
            while True:
                header = f.read(60)
                if len(header) < 60:
                    break
                if header[58:60] != b"`\n":
                    raise ValueError(f"{path}: ar 成员头损坏")
                name = header[0:16].decode("utf-8", "replace").strip(" /")
                try:
                    size = int(header[48:58].decode().strip())
                except ValueError:
                    raise ValueError(f"{path}: 无法解析 ar 成员大小")
                data = f.read(size)
                members[name] = data
                if size % 2 == 1:  # ar 数据按 2 字节对齐
                    f.read(1)
            return members
    raise ValueError(f"{path}: 不是合法的 ipk 容器（既非 ar 也非 gzip-tar）")


def extract_control(ipk_path):
    """从 ipk 提取 control 文本。返回 (字段有序列表 [(key,value)], 内容 bytes)。"""
    members = read_container(ipk_path)
    ctrl_tar = members.get("control.tar.gz") or members.get("control.tar")
    if ctrl_tar is None:
        raise ValueError(f"{ipk_path}: 未找到 control.tar.gz")
    mode = "r:gz" if members.get("control.tar.gz") is not None else "r:"
    with tarfile.open(fileobj=io.BytesIO(ctrl_tar), mode=mode) as tf:
        for name in ("./control", "control"):
            try:
                member = tf.getmember(name)
            except KeyError:
                continue
            raw = tf.extractfile(member).read()
            return parse_control(raw), raw
    raise ValueError(f"{ipk_path}: control.tar.gz 内未找到 control 文件")


def parse_control(raw):
    """解析 'Key: Value' 形式的 control 文本，保留顺序。"""
    fields = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fields.append((k.strip(), v.strip()))
    return fields


def compute_installed_size(ipk_path):
    """估算安装大小（KB）：data.tar.gz 解压后字节数 / 1024。"""
    members = read_container(ipk_path)
    data_tar = members.get("data.tar.gz") or members.get("data.tar")
    if data_tar is None:
        return 0
    mode = "r:gz" if members.get("data.tar.gz") is not None else "r:"
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data_tar), mode=mode) as tf:
        for m in tf.getmembers():
            if m.isfile():
                total += m.size
    return max(1, total // 1024)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# apk（Alpine 包）相关工具
# ---------------------------------------------------------------------------

def gzip_members(data):
    """把一个可能由多个 gzip 流拼接而成的字节串，切成独立的 gzip 成员。"""
    members = []
    rest = data
    while len(rest) >= 2 and rest[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(31)  # gzip window
        d.decompress(rest)
        consumed = len(rest) - len(d.unused_data)
        members.append(rest[:consumed])
        rest = d.unused_data
    return members


def apk_checksum(path):
    """apk 的 C: 校验和 = control 段（含 .PKGINFO 的那个 gzip 流）的 SHA1，Q1+base64。

    依据 Alpine Apk spec：包校验和是包内 control gzip 流的 SHA1，base64 编码后前缀 Q1。
    """
    with open(path, "rb") as f:
        raw = f.read()
    for m in gzip_members(raw):
        try:
            with tarfile.open(fileobj=io.BytesIO(m), mode="r:gz") as tf:
                if any(x.name.endswith(".PKGINFO") for x in tf.getmembers()):
                    return "Q1" + base64.b64encode(hashlib.sha1(m).digest()).decode()
        except Exception:
            continue
    raise ValueError(f"{path}: 未找到 apk 的 control 段（含 .PKGINFO）")


def parse_pkginfo(path):
    """解析 apk 内的 .PKGINFO（'key = value' 形式，key 可重复）。返回 {key: [values]}。"""
    members = read_container(path)
    raw = members.get(".PKGINFO")
    if raw is None:
        raise ValueError(f"{path}: 未找到 .PKGINFO")
    d = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            d.setdefault(k.strip(), []).append(v.strip())
    return d


def build_apkindex_text(apk_files):
    """apk_files: [(filename, abspath)]。返回 APKINDEX 文件文本（utf-8 字节）。

    按 pkgname 去重保留最高 pkgver；条目字段对齐 Alpine/OpenWrt 官方格式。
    """
    entries = {}  # pkgname -> dict
    for filename, abspath in apk_files:
        try:
            pi = parse_pkginfo(abspath)
        except Exception as e:
            print(f"[warn] {filename} 解析 .PKGINFO 失败: {e}", file=sys.stderr)
            continue
        pkg = pi.get("pkgname", [None])[0]
        ver = pi.get("pkgver", [""])[0]
        if not pkg:
            print(f"[warn] {filename} 缺少 pkgname，跳过", file=sys.stderr)
            continue
        if pkg in entries and ver <= entries[pkg]["ver"]:
            continue
        entries[pkg] = dict(pi=pi, ver=ver, filename=filename,
                            size=os.path.getsize(abspath),
                            checksum=apk_checksum(abspath))
    blocks = []
    for pkg in sorted(entries):
        e = entries[pkg]
        pi = e["pi"]
        def g(k):
            return pi.get(k, [""])[0]
        block = [
            f"C:{e['checksum']}",
            f"P:{pkg}",
            f"V:{e['ver']}",
            f"A:{g('arch') or 'noarch'}",
            f"S:{e['size']}",
            f"I:{g('size') or '0'}",
            f"T:{g('pkgdesc')}",
            f"U:{g('url')}",
            f"L:{g('license')}",
            f"o:{g('origin') or pkg}",
            f"m:{g('packager')}",
            f"t:{g('builddate') or '0'}",
        ]
        for dep in pi.get("depend", []):
            block.append(f"D:{dep}")
        blocks.append("\n".join(block))
    return ("\n\n".join(blocks) + "\n").encode("utf-8")


def write_apkindex(dir_path, apk_files):
    """在 dir_path 写入 APKINDEX.tar.gz（内含单个 APKINDEX 文本文件，gzip tar）。"""
    data = build_apkindex_text(apk_files)
    tarbuf = io.BytesIO()
    with tarfile.open(fileobj=tarbuf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("APKINDEX")
        ti.size = len(data)
        ti.mtime = 0
        tf.addfile(ti, io.BytesIO(data))
    out = os.path.join(dir_path, "APKINDEX.tar.gz")
    with open(out, "wb") as f:
        f.write(tarbuf.getvalue())
    print(f"  生成 {out} ({len(data)} B 索引)")


def package_stem(filename):
    """去掉 '_版本_架构.ipk' 后缀，得到包规范名（用于去重）。"""
    base = os.path.basename(filename)
    # 形如 xxx_1.2.3_all.ipk -> 取最后一个 '_' 之前的 xxx
    return base.rsplit("_", 2)[0]


def gzip_bytes(data, mtime=0):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=mtime) as gz:
        gz.write(data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 拉取 + 构建
# ---------------------------------------------------------------------------

def collect_assets(config, work_dir):
    """返回 {'ipk': {包规范名: (来源repo, 资产名, url)}, 'apk': {资产名: (...)} }。

    ipk 按包名去重保留版本最高；apk 先按资产名收集（构建时按 pkgname 去重）。
    """
    want = {"ipk": {}, "apk": {}}
    for plugin in config["plugins"]:
        if plugin.get("type") == "per_arch_tar":
            continue  # 分架构 tar.gz 插件（如 nikki）由 collect_nikki_tar 单独处理
        repo = plugin["repo"]
        n = int(plugin.get("scan_releases", 1))
        url = f"https://api.github.com/repos/{repo}/releases?per_page={n}"
        try:
            data = api_get(url)
        except urllib.error.HTTPError as e:
            print(f"[warn] 拉取 {repo} releases 失败: HTTP {e.code}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"[warn] 拉取 {repo} releases 失败: {e}", file=sys.stderr)
            continue
        releases = json.loads(data)
        aprx = plugin.get("apk_regex")
        for rel in releases:
            for asset in rel.get("assets", []):
                name = asset["name"]
                if name.endswith(".ipk") and re.match(plugin["asset_regex"], name):
                    stem = package_stem(name)
                    cur = want["ipk"].get(stem)
                    if cur and cur[1] >= name:
                        continue
                    want["ipk"][stem] = (plugin["name"], name, asset["browser_download_url"])
                elif name.endswith(".apk") and aprx and re.match(aprx, name):
                    # 资产名唯一即可；构建时按 pkgname 去重
                    if name not in want["apk"]:
                        want["apk"][name] = (plugin["name"], name, asset["browser_download_url"])
    return want


def collect_nikki_tar(config, work_dir):
    """处理 type=per_arch_tar 的插件（nikki）：按架构下载 24.10(ipk)/25.12(apk) tar.gz 并解压。

    返回 {arch: {"ipk": [(plugin, filename, abspath)], "apk": [...]}}。
    tar.gz 内 ipk/apk 解压到 work/nikki/<arch>/。
    """
    result = {}
    for plugin in config["plugins"]:
        if plugin.get("type") != "per_arch_tar":
            continue
        repo = plugin["repo"]
        url = f"https://api.github.com/repos/{repo}/releases?per_page={plugin.get('scan_releases', 1)}"
        try:
            data = json.loads(api_get(url))
        except Exception as e:
            print(f"[warn] 拉取 {repo} releases 失败: {e}", file=sys.stderr)
            continue
        asset_urls = {}
        for rel in data:
            for a in rel.get("assets", []):
                asset_urls[a["name"]] = a["browser_download_url"]
        for arch in plugin.get("archs", []):
            iarch = {"ipk": [], "apk": []}
            for kind, pat_key, ext in (("ipk", "ipk_pattern", ".ipk"),
                                       ("apk", "apk_pattern", ".apk")):
                pat = plugin.get(pat_key)
                if not pat:
                    continue
                name = pat.format(arch=arch)
                if name not in asset_urls:
                    print(f"[warn] {name} 不存在于 {repo} release，跳过", file=sys.stderr)
                    continue
                tarpath = os.path.join(work_dir, "nikki_tar", name)
                os.makedirs(os.path.dirname(tarpath), exist_ok=True)
                if not os.path.exists(tarpath) or os.path.getsize(tarpath) == 0:
                    try:
                        download(asset_urls[name], tarpath)
                    except Exception as e:
                        print(f"[warn] 下载 {name} 失败: {e}", file=sys.stderr)
                        continue
                arch_work = os.path.join(work_dir, "nikki", arch)
                os.makedirs(arch_work, exist_ok=True)
                try:
                    with tarfile.open(tarpath, "r:gz") as tf:
                        for m in tf.getmembers():
                            if not m.isfile():
                                continue
                            fn = os.path.basename(m.name)
                            if not fn.endswith(ext):
                                continue
                            dest = os.path.join(arch_work, fn)
                            with open(dest, "wb") as fh:
                                fh.write(tf.extractfile(m).read())
                            if kind == "apk":
                                # nikki 的 luci-app/i18n 是 noarch(all)，主程序/内核按来源架构编译
                                aarch = "all" if (fn.startswith("luci-app-nikki")
                                                  or fn.startswith("luci-i18n-nikki")) else arch
                                iarch["apk"].append((plugin["name"], fn, dest, aarch))
                            else:
                                iarch["ipk"].append((plugin["name"], fn, dest))
                except Exception as e:
                    print(f"[warn] 解压 {name} 失败: {e}", file=sys.stderr)
                    continue
            result[arch] = iarch
    return result


def build(config, work_dir, out_dir):
    all_dir = os.path.join(out_dir, "all")
    os.makedirs(all_dir, exist_ok=True)

    # 1) 收集并下载普通插件（all 包）的 ipk + apk
    assets = collect_assets(config, work_dir)
    os.makedirs(work_dir, exist_ok=True)
    ipk_files = []  # (plugin, filename, abspath)
    apk_files = []  # (plugin, filename, abspath)
    for kind, dest_list in (("ipk", ipk_files), ("apk", apk_files)):
        for stem, (plugin, name, url) in assets[kind].items():
            dest = os.path.join(work_dir, name)
            if not os.path.exists(dest) or os.path.getsize(dest) == 0:
                try:
                    download(url, dest)
                except Exception as e:
                    print(f"[warn] 下载 {name} 失败: {e}", file=sys.stderr)
                    continue
            dest_list.append((plugin, name, dest))
    if not ipk_files:
        raise SystemExit("没有任何 ipk 下载成功，停止构建。")

    # 2) nikki：分架构 tar.gz 下载并解压
    nikki = collect_nikki_tar(config, work_dir)
    nikki_ipks, nikki_apks = [], []
    for arch, d in nikki.items():
        nikki_ipks += d["ipk"]
        nikki_apks += d["apk"]

    # 3) 解析每个 ipk -> entries[(pkg, arch)] = (lines, arch, abspath)
    #    用 (pkg, arch) 作 key：nikki 各架构主程序同名不同架构不会互相覆盖。
    entries = {}

    def add_ipk(filename, abspath):
        fields, _ = extract_control(abspath)
        pkg_name = dict(fields).get("Package")
        if not pkg_name:
            print(f"[warn] {filename} 缺少 Package 字段，跳过", file=sys.stderr)
            return
        arch = dict(fields).get("Architecture", "all")
        size = os.path.getsize(abspath)
        inst = dict(fields).get("Installed-Size")
        if inst is None:
            inst = str(compute_installed_size(abspath))
        lines = [f"{k}: {v}" for k, v in fields]
        lines.append(f"Size: {size}")
        lines.append(f"MD5Sum: {md5_file(abspath)}")
        lines.append(f"SHA256sum: {sha256_file(abspath)}")
        lines.append(f"Installed-Size: {inst}")
        entries[(pkg_name, arch)] = (lines, arch, abspath)

    for plugin, filename, abspath in ipk_files:
        add_ipk(filename, abspath)
    for plugin, filename, abspath in nikki_ipks:
        add_ipk(filename, abspath)

    order = sorted(entries.keys())

    def blocks_for(arch):
        """给定架构的 opkg 索引块：all 包 + 本架构专用包，Filename 用纯文件名。"""
        out = []
        for key in order:
            lines, parch, abspath = entries[key]
            if parch == "all" or parch == arch:
                out.append("\n".join(lines + [f"Filename: {os.path.basename(abspath)}"]))
        return out

    def mirror_into(dir_path, keep):
        """把 ipk/apk 复制进 dir_path，并清理该目录内本次未生成的过期 ipk/apk。"""
        os.makedirs(dir_path, exist_ok=True)
        keep = set(keep)
        for old in os.listdir(dir_path):
            if old.endswith((".ipk", ".apk")) and old not in keep:
                os.remove(os.path.join(dir_path, old))

    # 4) apk 分档：all apk 镜像进所有目录；分架构 apk 只进对应架构目录
    #    现有插件 apk 均 noarch(all)；nikki 的 apk 架构在解压时已按来源架构+包名判定
    #    （python 侧不解析 v3 apk 的 .PKGINFO，避免 ADBd 魔数头解析不可靠）
    all_apk = [(name, abspath) for plugin, name, abspath in apk_files]
    arch_apk = {}
    for plugin, name, abspath, aarch in nikki_apks:
        if aarch == "all":
            all_apk.append((name, abspath))
        else:
            arch_apk.setdefault(aarch, []).append((name, abspath))

    # 5) all/ 目录：全部 all 架构 ipk + all apk
    all_ipks = []
    for key in order:
        lines, parch, abspath = entries[key]
        if parch == "all":
            shutil.copy2(abspath, os.path.join(all_dir, os.path.basename(abspath)))
            all_ipks.append(os.path.basename(abspath))
    all_apk_names = [n for n, _ in all_apk]
    for n, abspath in all_apk:
        shutil.copy2(abspath, os.path.join(all_dir, n))
    mirror_into(all_dir, all_ipks + all_apk_names)
    write_index(os.path.join(all_dir, "Packages"), blocks_for("all"))
    # apk 的 packages.adb 由 CI 中 apk-tools v3 生成

    # 6) 每个架构目录：all 包镜像 + 本架构 ipk + all apk + 本架构 apk
    for arch in config.get("architectures", []):
        if arch == "all":
            continue
        arch_dir = os.path.join(out_dir, arch)
        os.makedirs(arch_dir, exist_ok=True)
        keep = []
        for key in order:
            lines, parch, abspath = entries[key]
            if parch == "all" or parch == arch:
                shutil.copy2(abspath, os.path.join(arch_dir, os.path.basename(abspath)))
                keep.append(os.path.basename(abspath))
        for n, abspath in all_apk:
            shutil.copy2(abspath, os.path.join(arch_dir, n))
            keep.append(n)
        for n, abspath in arch_apk.get(arch, []):
            shutil.copy2(abspath, os.path.join(arch_dir, n))
            keep.append(n)
        mirror_into(arch_dir, keep)
        write_index(os.path.join(arch_dir, "Packages"), blocks_for(arch))
        # apk 的 packages.adb 由 CI 中 apk-tools v3 生成

    print(f"构建完成：{len(entries)} 个 ipk（含 nikki {len(nikki_ipks)}）、"
          f"apk {len(all_apk)}（all）+ {sum(len(v) for v in arch_apk.values())}（分架构），"
          f"all/ + {len(config.get('architectures', [])) - 1} 个架构目录已生成于 {out_dir}")
    return entries


def write_index(path, blocks):
    """写入 Packages 与 Packages.gz（空行分隔，按稳定排序）。"""
    text = ("\n\n".join(blocks) + "\n").encode("utf-8")
    with open(path, "wb") as f:
        f.write(text)
    with open(path + ".gz", "wb") as f:
        f.write(gzip_bytes(text))
    print(f"  生成 {path} ({len(text)} B) + .gz")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="生成 OpenWrt opkg 软件源")
    ap.add_argument("--config", default="config/feed.json")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--work", default=".work")
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    build(config, args.work, args.out)


if __name__ == "__main__":
    main()
