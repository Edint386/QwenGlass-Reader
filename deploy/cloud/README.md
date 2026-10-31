# glassreader 云端运行：`https://reader.example.com`

```
平台（MCP）/ 手机 ──https──► 服务器 OpenResty（reader.example.com）──► 127.0.0.1:19516 ──► 容器 glassreader :8780
```

书库和进度都在服务器上，手机下载页图直接到这里，不经过 Mac、隧道和局域网转发器。

## 服务器上的东西

| 位置 | 内容 |
|---|---|
| `/opt/glassreader/compose.yaml` | 容器定义：只监听回环、只读根文件系统、内存上限 256 MB、开机自启 |
| `/opt/glassreader/.env` | `READER_TOKEN`、`READER_KEY`、`READER_PUBLIC_URL`，0600 |
| `/opt/glassreader/books/` | 书库，容器里只读 |
| `/opt/glassreader/data/progress.json` | 阅读进度 |
| `/opt/glassreader/letsencrypt/` | certbot 的账号和证书 |
| `/opt/1panel/apps/openresty/openresty/conf/conf.d/reader.example.com.conf` | 站点配置，由 `site.http.conf` + `site.https.conf` 拼成 |
| `glassreader-cert-renew.timer` | 每周检查一次证书续期，证书路径同上面的站点目录 `ssl/` |

这个站点是手写配置，不在 1Panel 的网站列表里，证书也不归 1Panel 管。

## 日常操作

都在 Mac 上运行，需要能 SSH 到服务器。

```bash
deploy/cloud/sync_books.sh
```

把 `~/Books` 里的 TXT 传上去，只增改，不删云端的书。传完不用重启。

```bash
deploy/cloud/push.sh
```

发布代码并重建容器，不碰 `.env`、书库和进度。

```bash
ssh ubuntu@server.example.com sudo docker logs --tail 50 -f glassreader
```

看日志。页图地址里的 key 已经打码。

## 从零部署

1. DNS：`reader` A 记录指向服务器。
2. 建目录：`/opt/glassreader`（root，0750）、`books/`、`data/`（属主 10001，容器里的用户）、`letsencrypt/`（0700）、站点目录的 `log/`、`ssl/`。写好 `.env`。
3. 把 `site.http.conf` 装成站点配置，`openresty -t` 后 reload，用 certbot 容器 webroot 签证书：

   ```bash
   sudo docker run --rm -v /opt/glassreader/letsencrypt:/etc/letsencrypt -v /opt/1panel/apps/openresty/openresty/root:/var/www/acme certbot/certbot:latest certonly --webroot --webroot-path /var/www/acme -d reader.example.com --non-interactive --agree-tos --register-unsafely-without-email --key-type ecdsa
   ```

4. 把证书装到站点 `ssl/`，再把 `site.http.conf` + `site.https.conf` 拼成站点配置，`openresty -t` 后 reload。
5. 装 `renew-cert.sh`（0700）和续期 timer，`systemctl enable --now glassreader-cert-renew.timer`。
6. 运行 `push.sh`、`sync_books.sh`，把平台插件的基础 URL 改成这个域名。

## 回滚到 Mac

1. 把平台插件的基础 URL 改回 `https://cast.example.com/reader`。
2. 在 Mac 上按 [../README.md](../README.md) 启动 glassreader、隧道和局域网转发器。
3. 需要时把云端进度拷回 Mac：`/opt/glassreader/data/progress.json` → `~/Library/Application Support/glassreader/progress.json`。

## 注意

- **字体**：Linux 上用 Noto Sans CJK，比 Mac 上的冬青黑体略粗，字宽相同。分页结果可能和 Mac 不同，但进度存的是字符位置，不受影响。主字体没有的字（颜文字里的泰文、彝文、组合符号等）逐字换 `FALLBACK_FONTS` 里的字体画，用到的包都在 `Dockerfile` 里。
- **加书还要电脑**：目前只有 `sync_books.sh` 这一条路。
