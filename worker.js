// Cloudflare Worker —— 把 R2 桶对外提供为 OpenWrt 软件源（opkg/ipk + apk 双源）
//
// 目录布局（与 OpenWrt 官方一致，末级目录名 packages）：
//   packages/<arch>/Packages      <- opkg/ipk 索引
//   packages/<arch>/packages.adb  <- apk v3 索引
//   packages/<arch>/*.ipk / *.apk
//
// 部署后：
//   自定义域名（推荐） https://luci.zwrt.de/packages/<arch>/Packages
//   或默认 https://<worker>.workers.dev/packages/<arch>/Packages
//
// 路由器侧 opkg.conf 添加（opkg/ipk）：
//   src/gz zwrt https://luci.zwrt.de/packages/<arch>
//   （<arch> 替换为你的路由器架构，如 x86_64 / aarch64_cortex-a53）
// 路由器侧 apk 源（apk-tools v3，OpenWrt 25.12+）：
//   https://luci.zwrt.de/packages/<arch>

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // R2 对象键 = 去掉前导 / 的完整路径，例如 packages/x86_64/Packages
    const key = decodeURIComponent(url.pathname.slice(1));
    if (!key) {
      return new Response("ZWRT feed: ok", { status: 200 });
    }

    const obj = await env.OPKG_BUCKET.get(key);
    if (!obj) {
      return new Response("Not Found", { status: 404 });
    }

    const headers = new Headers();
    obj.writeHttpMetadata(headers);
    headers.set("content-type", contentType(key));
    // 索引文件（Packages / packages.adb / *.gz）短缓存，安装包（.ipk/.apk）长缓存
    headers.set(
      "cache-control",
      key.toLowerCase().includes("packages")
        ? "public, max-age=300"
        : "public, max-age=86400, immutable"
    );
    headers.set("etag", obj.httpEtag);
    // 允许跨域（可选）
    headers.set("access-control-allow-origin", "*");

    return new Response(obj.body, { headers });
  },
};

function contentType(key) {
  const k = key.toLowerCase();
  if (k.endsWith(".gz")) return "application/gzip";
  if (k.endsWith(".ipk")) return "application/octet-stream";
  if (k.endsWith(".adb")) return "application/octet-stream";
  if (k.endsWith("/packages")) return "text/plain; charset=utf-8";
  return "application/octet-stream";
}
