// Cloudflare Worker —— 把 R2 桶对外提供为 OpenWrt opkg 软件源
//
// 部署后：
//   默认域名  https://<worker>.workers.dev/opkg/<arch>/Packages
//   或自定义域名（推荐） https://opkg.example.com/opkg/<arch>/Packages
//
// 路由器侧 opkg.conf 添加：
//   src/gz zwrt_<arch> https://<域名>/opkg/<arch>
//   （<arch> 替换为你的路由器架构，如 x86_64 / aarch64_cortex-a53）

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // R2 对象键 = 去掉前导 / 的完整路径，例如 opkg/x86_64/Packages
    const key = decodeURIComponent(url.pathname.slice(1));
    if (!key) {
      return new Response("ZWRT opkg feed: ok", { status: 200 });
    }

    const obj = await env.OPKG_BUCKET.get(key);
    if (!obj) {
      return new Response("Not Found", { status: 404 });
    }

    const headers = new Headers();
    obj.writeHttpMetadata(headers);
    headers.set("content-type", contentType(key));
    // Packages 短缓存，ipk 长缓存
    headers.set(
      "cache-control",
      key.includes("Packages")
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
  if (key.endsWith(".gz")) return "application/gzip";
  if (key.endsWith(".ipk")) return "application/octet-stream";
  if (key.endsWith("/Packages")) return "text/plain; charset=utf-8";
  return "application/octet-stream";
}
