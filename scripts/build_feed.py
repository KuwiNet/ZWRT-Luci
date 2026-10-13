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
                    members[m.name.lstrip("./")] = tf.extractfile(m).read()
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
    """返回 {包规范名: (来源repo, 资产名, 下载url)}，去重保留版本最高。"""
    want = {}
    for plugin in config["plugins"]:
        repo = plugin["repo"]
        regex = re.compile(plugin["asset_regex"])
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
        for rel in releases:
            for asset in rel.get("assets", []):
                name = asset["name"]
                if not name.endswith(".ipk"):
                    continue
                if not regex.match(name):
                    continue
                stem = package_stem(name)
                # 版本越高，文件名排序越靠后；保留字符串排序最高者
                if stem in want and want[stem][1] >= name:
                    continue
                want[stem] = (plugin["name"], name, asset["browser_download_url"])
    return want


def build(config, work_dir, out_dir):
    all_dir = os.path.join(out_dir, "all")
    os.makedirs(all_dir, exist_ok=True)

    # 1) 收集并下载
    assets = collect_assets(config, work_dir)
    os.makedirs(work_dir, exist_ok=True)
    downloaded = []  # (plugin, filename, abspath)
    for stem, (plugin, name, url) in assets.items():
        dest = os.path.join(work_dir, name)
        if not os.path.exists(dest) or os.path.getsize(dest) == 0:
            try:
                download(url, dest)
            except Exception as e:
                print(f"[warn] 下载 {name} 失败: {e}", file=sys.stderr)
                continue
        downloaded.append((plugin, name, dest))
    if not downloaded:
        raise SystemExit("没有任何 ipk 下载成功，停止构建。")

    # 2) 解析每个 ipk，收集 (字段行, 架构, 文件名)；Filename 按目录单独追加
    entries = {}  # pkg_name -> (lines_without_filename, arch, filename)
    for plugin, filename, abspath in downloaded:
        fields, _ = extract_control(abspath)
        pkg_name = dict(fields).get("Package")
        if not pkg_name:
            print(f"[warn] {filename} 缺少 Package 字段，跳过", file=sys.stderr)
            continue
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
        entries[pkg_name] = (lines, arch, filename)

    order = sorted(entries)

    def blocks_for(arch):
        """给定架构的索引块：all 包 + 本架构专用包，Filename 用纯文件名。"""
        out = []
        for pkg in order:
            lines, parch, filename = entries[pkg]
            if parch == "all" or parch == arch:
                out.append("\n".join(lines + [f"Filename: {filename}"]))
        return out

    def mirror_into(dir_path, keep):
        """把 ipk 复制进 dir_path，并清理该目录内本次未生成的过期 ipk。"""
        os.makedirs(dir_path, exist_ok=True)
        keep = set(keep)
        for old in os.listdir(dir_path):
            if old.endswith(".ipk") and old not in keep:
                os.remove(os.path.join(dir_path, old))

    # 3) all/ 目录：存放全部 all 架构 ipk（供 all 源使用）
    all_ipks = []
    for pkg in order:
        lines, parch, filename = entries[pkg]
        if parch == "all":
            shutil.copy2(os.path.join(work_dir, filename), os.path.join(all_dir, filename))
            all_ipks.append(filename)
    mirror_into(all_dir, all_ipks)
    write_index(os.path.join(all_dir, "Packages"), blocks_for("all"))

    # 4) 每个目标架构目录：镜像 all 架构 ipk + 本架构专用 ipk，并写索引
    for arch in config.get("architectures", []):
        if arch == "all":
            continue
        arch_dir = os.path.join(out_dir, arch)
        os.makedirs(arch_dir, exist_ok=True)
        arch_ipks = []
        for pkg in order:
            lines, parch, filename = entries[pkg]
            if parch == "all" or parch == arch:
                shutil.copy2(os.path.join(work_dir, filename), os.path.join(arch_dir, filename))
                arch_ipks.append(filename)
        mirror_into(arch_dir, arch_ipks)
        write_index(os.path.join(arch_dir, "Packages"), blocks_for(arch))

    print(f"构建完成：共 {len(entries)} 个包，all/ + {len(config.get('architectures',[]))-1} 个架构索引已生成于 {out_dir}")
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
