# ZWRT-opkg — OpenWrt 插件源

一个面向 OpenWrt 用户的在线插件源，托管在 `https://opkg.zwrt.de`。定时从上游仓库同步更新，路由器配置后即可 `opkg update` 安装，无需手动下载 ipk。

## 包含的插件

| 插件 | 说明 |
| --- | --- |
| `luci-theme-zwrt` | ZWRT 主题（kuwi.net） |
| `luci-app-zwrt` | ZWRT 主题设置 |
| `luci-app-5gmodem` | 5G/4G 上网卡管理 |
| `luci-i18n-5gmodem-zh-cn` | 5gmodem 简体中文语言包 |
| `luci-i18n-5gmodem-zh-tw` | 5gmodem 繁体中文语言包 |
| `luci-app-afrp` | AFRP NAT 穿透客户端管理 |

> 插件会持续增加。源内所有包均为 `all` 架构，兼容绝大多数 OpenWrt 设备。

## 支持的架构

以下架构目录均已生成 `Packages` 索引（`all` 包对全部架构可用）：

- `x86_64`
- `aarch64_cortex-a53`
- `arm_cortex-a7`

> 如果你的路由器架构不在上述列表，可反馈补充，或自行确认架构后使用对应的索引目录。

## 使用方法

在路由器编辑 `/etc/opkg/customfeeds.conf`，添加一行：

```
src/gz zwrt_<架构> https://opkg.zwrt.de/opkg/<架构>
```

把 `<架构>` 替换为你路由器的实际架构（执行 `opkg print-architecture` 可查看，常见为 `x86_64`、`aarch64_cortex-a53` 等）。

然后更新并安装：

```bash
opkg update
opkg install luci-theme-zwrt
opkg install luci-app-5gmodem
opkg install luci-i18n-5gmodem-zh-cn
opkg install luci-app-afrp
```

也可以只更新索引、在 LuCI 界面（系统 → 软件包）里搜索并安装。

## 更新机制

源内包每天自动从上游仓库同步一次（GitHub Actions 定时构建），同步完成后无需任何手动操作，下次 `opkg update` 即可拿到新版本。

## 常见问题

**`opkg update` 报 404？**
大概率是架构目录名写错。先 `opkg print-architecture` 确认真实架构，再核对 `customfeeds.conf` 里 `<架构>` 是否一致。

**源里的包装不上？**
个别插件可能依赖其他软件包（如 `luci-app-afrp` 依赖 `frpc`）。若 `opkg update` 成功但安装报缺依赖，先确认官方源正常、再重试安装。

**怎么确认源是通的？**
浏览器打开 `https://opkg.zwrt.de/opkg/all/Packages`，能看到以 `Package:` 开头的文本列表即正常。
