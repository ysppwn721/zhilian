# 阿里云同域部署（备选方案）

> ⚠️ **当前线上不是这套。** 2026-09-26 实际上线的是
> 「Cloudflare Worker 出静态页 + Cloudflare Tunnel 接后端」：静态站为
> `zhilian.space`，真实工作台为 `demo.zhilian.space`，服务器只监听 `127.0.0.1:8765`。
> 详见 `docs/部署说明.md`。本文这套 compose + Caddy 方案适用于「服务器直出整站并
> 自行申请 HTTPS」，与线上方案二选一即可（两者都会占 80/443）。

这套部署是历史上的同域备选方案。当前线上已采用静态站与演示后端分域：

- `https://zhilian.space/`：产品介绍、下载和使用说明
- `https://demo.zhilian.space/`：真实知链工作台和 FastAPI 后端
- `https://zhilian.space/demo/`：兼容旧链接的跳转页

当前线上由 Cloudflare Tunnel 将 `demo.zhilian.space` 转发到服务器本机 FastAPI，服务器不开放公网入站端口。
本文的 Docker + Caddy 方案仅供需要服务器直出整站时参考。

## 首次部署

在本地项目根目录执行：

```powershell
scp -r deploy site zhilian Dockerfile requirements.txt run.py <SSH用户>@<服务器IP>:/opt/zhilian/
```

然后登录服务器：

```bash
cd /opt/zhilian/deploy
cp .env.example .env
vi .env
docker compose up -d --build
docker compose ps
```

若采用本文备选方案，将域名 DNS 指向服务器公网 IP。当前线上方案的验证命令为：

```bash
curl -I https://zhilian.space/
curl -i https://demo.zhilian.space/api/health
curl -u zhilian:<部署密码> https://demo.zhilian.space/api/health
```

未带认证的后端健康检查预期返回 `401`，带测试账号预期返回 `200`。浏览器打开
`https://demo.zhilian.space/`，用户名固定为 `zhilian`，密码是 `.env` 中的
`ZHILIAN_ACCESS_PASSWORD`。两个公网域名均由 Cloudflare 提供边缘 HTTPS。

## 更新版本

```bash
cd /opt/zhilian
git pull
cd deploy
docker compose up -d --build
```

用户数据保存在 Docker volume `deploy_zhilian_data`，更新镜像不会删除项目数据。不要把 `.env` 提交到 Git，也不要把 API Key 写入站点文件。
