# ZWRT-Luci — OpenWrt 插件源

面向 OpenWrt 用户的在线插件源，提供 **opkg/ipk** 与 **apk** 两种格式，目录布局与 OpenWrt 官方一致。路由器配置后即可安装，无需手动下载。

提供两个入口，内容实时一致，任选其一：

- 主源（Cloudflare）：`https://luci.zwrt.cc/packages/<架构>`
- 备用源（VPS）：`https://mirror.zwrt.cc/packages/<架构>`

## 包含的插件

| 插件 | 说明 |
| --- | --- |
| `luci-theme-zwrt` | ZWRT 主题（kuwi.net） |
| `luci-app-5gmodem` | 5G/4G 上网卡管理 |
| `luci-i18n-5gmodem-zh-cn` | 5gmodem 简体中文语言包 |
| `luci-i18n-5gmodem-zh-tw` | 5gmodem 繁体中文语言包 |
| `luci-app-afrp` | AFRP NAT 穿透客户端管理 |

> 插件会持续增加。源内所有包均为 `all` 架构，兼容绝大多数 OpenWrt 设备。

## 支持的架构

以下架构目录均已生成索引（`all` 包对全部架构可用）：

- `x86_64`
- `aarch64_cortex-a53`
- `arm_cortex-a7`

> 如果你的路由器架构不在上述列表，可反馈补充，或自行确认架构后使用对应的索引目录。

## 使用方法（opkg/ipk，OpenWrt 23.05~24.10）

在路由器编辑 `/etc/opkg/customfeeds.conf`，添加一行：

```
src/gz zwrt https://mirror.zwrt.cc/packages/<架构>
```

把 `<架构>` 替换为你路由器的实际架构（执行 `opkg print-architecture` 可查看，常见为 `x86_64`、`aarch64_cortex-a53` 等）。注意：同一台路由器上 `src/gz` 的源名 `zwrt` 要唯一，一台设备只需添加对应自己架构的那一行。

然后更新并安装：

```bash
opkg update
opkg install luci-theme-zwrt
opkg install luci-app-5gmodem
opkg install luci-i18n-5gmodem-zh-cn
opkg install luci-app-afrp
```

也可以只更新索引、在 LuCI 界面（系统 → 软件包）里搜索并安装。

## 使用方法（apk，OpenWrt 25.12+ / apk-tools v3）

apk 的源配置写在 `/etc/apk/repositories.d/` 下（不是 `/etc/apk/repositories`），**每一行都必须直接指向 `packages.adb` 文件本身**。如果写成目录，apk 会按旧格式去找 `APKINDEX.tar.gz`，报 `unexpected end of file`。

**1）导入签名公钥**（一次性，长期有效）

```bash
mkdir -p /etc/apk/keys
wget -O /etc/apk/keys/zwrt-feed.pem https://mirror.zwrt.cc/packages/zwrt-feed.pem
```

**2）添加源并更新索引**

```bash
mkdir -p /etc/apk/repositories.d
echo "https://mirror.zwrt.cc/packages/all/packages.adb" >> /etc/apk/repositories.d/customfeeds.list
apk update
```

**3）安装**

```bash
apk add luci-theme-zwrt luci-app-5gmodem luci-i18n-5gmodem-zh-cn luci-i18n-5gmodem-zh-tw luci-app-afrp
```

> 系统升级（sysupgrade）会清掉 `/etc/apk/keys` 里的自定义公钥，建议把它加入备份清单：
> `echo /etc/apk/keys/zwrt-feed.pem >> /etc/sysupgrade.conf`

源内所有包都是 `all` 架构，因此任何架构都只需要上面这一行 `packages/all/packages.adb`，不用按架构区分。

## 更新机制

源内包每天定时自动从上游仓库同步（GitHub Actions 构建，北京时间每天 03:00 触发）。上游发布新版本后无需任何手动操作，更新索引即可拿到新版本。

## 常见问题

**更新索引报 404？**
大概率是架构目录名写错。先 `opkg print-architecture`（apk 用 `apk arch`）确认真实架构，再核对源地址里 `<架构>` 是否一致。

**apk update 报 `UNTRUSTED signature`？**
说明设备上没有导入本源的签名公钥，或系统升级后公钥丢失。重新执行 apk 章节的第 1 步导入 `zwrt-feed.pem`，并确认 `https://mirror.zwrt.cc/packages/zwrt-feed.pem` 能打开。

**源里的包装不上？**
个别插件可能依赖其他软件包（如 `luci-app-afrp` 依赖 `frpc`）。若更新成功但安装报缺依赖，先确认官方源正常、再重试安装。

**怎么确认源是通的？**
浏览器打开 `https://mirror.zwrt.cc/packages/all/Packages`（opkg 索引）或 `https://mirror.zwrt.cc/packages/all/packages.adb`（apk 索引），能看到内容即正常。
