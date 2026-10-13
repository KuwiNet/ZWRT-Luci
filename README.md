# ZWRT-opkg — OpenWrt 自定义 opkg 软件源

用 **GitHub Actions + Cloudflare R2 + Cloudflare Worker** 搭建的 OpenWrt 插件源。
GitHub Actions 定时从各插件的 GitHub Release 拉取 ipk → 生成 `Packages` / `Packages.gz`
→ rclone 同步到 R2 的 `opkg/` 目录 → Worker 对外提供 opkg 源。

当前内置三个插件（后续可在 `config/feed.json` 里直接增加）：

| 插件 | 仓库 | 打包的 ipk |
| --- | --- | --- |
| zwrt 主题 | `KuWiNet/ZWRT-Theme` | `luci-theme-zwrt`、`luci-app-zwrt` |
| 5gmodem | `fildunsky/luci-app-5gmodem` | `luci-app-5gmodem` |
| afrp | `afrpnet/afrp-client` | `luci-app-afrp` |

> 三个插件目前都产出 `Architecture: all` 的 ipk。仓库结构做成多架构索引，
> 每个目标架构目录各有一份 `Packages`（含全部 all 包），将来加架构相关插件也能直接扩展。

## 架构与目录结构

```
GitHub Release (zwrt/5gmodem/afrp)
        │  定时(每天 03:17 UTC)
        ▼
GitHub Actions (ZWRT-opkg 仓库)
  build_feed.py 下载 ipk → 生成 Packages/Packages.gz
        │  rclone sync
        ▼
Cloudflare R2 桶 (opkg/ 目录)
        │  Worker 读取
        ▼
https://<域名>/opkg/<架构>/Packages   ← 路由器 opkg 从这里装
```

仓库布局：

```
ZWRT-opkg/
├── .github/workflows/update-feed.yml   # Actions 定时构建 + 同步
├── config/feed.json                    # 插件仓库 & 目标架构配置（改这里加插件）
├── scripts/build_feed.py               # 下载 ipk + 生成索引（纯 Python，零依赖）
├── worker.js                           # Cloudflare Worker 入口
├── wrangler.toml                       # Worker 配置（R2 桶绑定）——须在仓库根目录
├── package.json                        # wrangler 依赖声明
├── docs/rclone.conf.template           # 本地 rclone R2 配置模板
└── README.md
```

`build_feed.py` 兼容经典 ar 容器和部分新构建工具的 gzip-tar 容器两种 ipk 打包格式。

---

## 一、Cloudflare R2（从零）

1. 打开 [Cloudflare Dashboard](https://dash.cloudflare.com) → 左侧 **R2**。
2. 首次使用按提示绑定支付方式（R2 有免费额度：10GB 存储 / 每月 1000 万次读，本场景足够）。
3. **创建桶**：点 *Create bucket*，名称填 `zwrt-opkg`（与 `worker/wrangler.toml` 保持一致）。
4. 记下你的 **Account ID**：R2 页面右侧可见，形如 `6f6f...1a2b`。后面 GitHub secret 和 rclone 端点都要用。

### 创建 R2 API Token（rclone 上传用）

1. R2 页面 → **Manage R2 API Tokens** → *Create API token*。
2. 权限选择 **Object Read & Write**，桶限定为 `zwrt-opkg`。
3. 生成后复制 **Access Key ID** 和 **Secret Access Key**（只显示一次，先存好）。
4. rclone 的 R2 端点格式为：`https://<Account ID>.r2.cloudflarestorage.com`。

---

## 二、Cloudflare Worker（从零）

本地需要 Node.js（≥ 18）与 npm。在仓库根目录操作：

```bash
npm install              # 安装 wrangler（依赖在 package.json）
# 第一次需要登录（会打开浏览器授权你的 Cloudflare 账号）
npx wrangler login
# 确认 wrangler.toml 里 bucket_name = "zwrt-opkg"（与你建的桶一致）
npx wrangler deploy      # 部署，成功后会给一个 *.workers.dev 地址
```

> 也可以完全走网页：Cloudflare 左侧 **Workers & Pages → Workers → Create →
> 从 GitHub 部署**，选 ZWRT-opkg 仓库。表单里 构建命令填 `npm install`，
> 部署命令填 `npx wrangler deploy`，预览命令填 `npx wrangler preview`。
> 网页部署要求 `wrangler.toml` 在仓库根目录（本项目已放在根目录）。

部署后即可访问（示例）：`https://zwrt-opkg.<你的子域>.workers.dev/opkg/x86_64/Packages`

### 绑定自定义域名（推荐）

Worker 自带 `*.workers.dev` 域名是 HTTPS，opkg 可用，但对外更稳、更干净建议绑自己的域名：

1. 确保该域名已托管在 Cloudflare（在 **Websites** 里添加）。
2. Workers → 你的 worker → **Settings → Domains & Routes → Add Custom Domain**。
3. 输入 `opkg.example.com`（示例），Cloudflare 自动加 DNS 记录。
4. 绑定后源地址即 `https://opkg.example.com/opkg/<架构>/Packages`。

> 想立即验证 Worker 是否正常，浏览器打开：
> `https://<你的域名>/opkg/all/Packages` 应返回文本，`/opkg/all/Packages.gz` 应下载压缩包。

---

## 三、GitHub 仓库与 Actions 配置

### 1) 新建仓库并推送

在 GitHub 新建空仓库 **ZWRT-opkg**（先不勾 README/.gitignore），然后：

```bash
# 在你本地本目录执行
git init
git add .
git commit -m "init: ZWRT-opkg feed builder (Actions + R2 + Worker)"
git branch -M main
git remote add origin git@github.com:<你的用户名>/ZWRT-opkg.git
git push -u origin main
```

### 2) 添加 GitHub Secrets

仓库 **Settings → Secrets and variables → Actions → New repository secret**，逐个添加：

| Secret 名称 | 值 |
| --- | --- |
| `R2_ACCOUNT_ID` | Cloudflare Account ID |
| `R2_ACCESS_KEY_ID` | R2 API Token 的 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | R2 API Token 的 Secret Access Key |
| `R2_BUCKET` | 桶名（`zwrt-opkg`） |
| `GH_TOKEN`（可选） | GitHub PAT，提升拉取 release 的速率限制（匿名也能跑） |

### 3) 手动跑一次

仓库 **Actions** 页 → 左侧 *Build & Sync OpenWrt opkg Feed* → **Run workflow** → 等运行结束。
成功后在 R2 桶里应能看到 `opkg/all/...`、`opkg/x86_64/...` 等目录及各自的 `Packages` / `Packages.gz`。

---

## 四、路由器侧配置

在 OpenWrt 路由器的 LuCI → 系统 → 软件包 → 配置（或编辑 `/etc/opkg/customfeeds.conf`），添加：

```
src/gz zwrt_<架构> https://opkg.example.com/opkg/<架构>
```

其中 `<架构>` 用路由器的实际架构（`opkg print-architecture` 可查，例如 `x86_64`、`aarch64_cortex-a53`）。
然后：

```bash
opkg update
opkg install luci-theme-zwrt
```

---

## 五、加新插件 / 改架构

编辑 `config/feed.json`：

```jsonc
{
  "architectures": ["all", "x86_64", "aarch64_cortex-a53", "arm_cortex-a7"], // 每个都会生成一份 Packages 索引
  "plugins": [
    {
      "name": "新插件",
      "repo": "owner/repo",
      "asset_regex": "需要匹配的 release 资产名（正则，.*\\.ipk$）",
      "scan_releases": 1   // 扫描最近 N 个 release；若某 ipk 分散在不同 release，调大
    }
  ]
}
```

- `asset_regex` 用 `^(包A|包B)_[0-9].*\.ipk$` 可同时打包多个名字相近的 ipk。
- 多个同名的包会按版本去重保留最高版。
- 修改 `config/` 后 push 会自动触发一次构建。

---

## 六、验证清单

- [ ] Worker 打开 `.../opkg/all/Packages` 返回含 `Package:` 的文本
- [ ] `.../opkg/all/Packages.gz` 能下载且 `gzip -t` 通过
- [ ] 路由 `opkg update` 无 `not found` 报错，`opkg install luci-theme-zwrt` 成功
- [ ] Actions 定时任务已启用（Actions 页看 schedule 运行记录）

## 常见问题

- **rclone 报 `The authorization header is malformed`**：检查 `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` 是否填反、Account ID 是否正确。
- **`opkg update` 报 404**：架构目录或文件名写错。`Architecture: all` 的包 Filename 指向 `all/` 目录，务必保证 `all/` 里 ipk 存在。
- **想给源加签名（Packages.sig）**：可用 `usign -c -s <secret>` 生成，Worker 无需改动，README 后续可补充。
