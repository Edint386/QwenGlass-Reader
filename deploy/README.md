# glassreader 上线：`https://cast.example.com/reader/`

> 云端运行见 [cloud/README.md](cloud/README.md)。本文记录之前的 Mac + 隧道方案，留作回滚。

目标：不新建域名和证书，让 glassreader 和 glasscast 共用 `cast.example.com`，按路径分流：

```
平台（MCP）/ 在外的手机 ──https──► 服务器 OpenResty ──┬─ /reader/…  ─► 127.0.0.1:18780 ══SSH══► Mac 127.0.0.1:8780  glassreader
                                                      └─ 其余        ─► 127.0.0.1:18766 ══SSH══► Mac 127.0.0.1:8766  glasscast
在家里的手机（路由器把域名解析到 Mac）──https──► Mac :443 lan_https_proxy ──┬─ /reader/… ─► 127.0.0.1:8780
                                                                          └─ 其余       ─► 127.0.0.1:8766
```

两条路都**剥掉 `/reader` 前缀**再转发：glassreader 自己的路由仍是 `/healthz`、`/r/…`、`/mcp`，它用
`READER_PUBLIC_URL=https://cast.example.com/reader` 生成页图地址。

## 0. 文件

| 文件 | 去哪 | 改动 |
|---|---|---|
| `1panel/zz-glassreader-upstream.conf` | 服务器 `/opt/1panel/apps/openresty/openresty/conf/conf.d/` | **新文件**。`upstream glassreader_tunnel` → `127.0.0.1:18780`，和 glasscast 的 upstream 逐行同构（两个 peer、`max_fails=0`、keepalive，测试里比对过） |
| `1panel/reader.conf` | 服务器 `…/www/sites/cast.example.com/proxy/` | **新文件**，与 glasscast 的 `root.conf` 并列，`root.conf` 不改。只转发 `/reader/healthz`、`/reader/r/`（GET）、`/reader/mcp`（POST），其余 `/reader/…` 404 |
| `tunnel.sh` | Mac，直接在仓库里运行 | **新文件**（包装）：用 glasscast 的 `glasscast/deploy/tunnel.sh` 另开一条隧道 `18780 ⇐ 8780`，保温打 `/reader/healthz`。`glasscast/deploy/tunnel.sh` 本身不改 |
| `lan_https_proxy.py` | 审过后替换 `glasscast/tools/lan_https_proxy.py` | 新增 `--route /前缀/=主机:端口`：带它时逐个请求解析 HTTP/1.1、按路径分流并剥前缀；**不带它时走的代码和现在逐字相同**（测试比对 `pipe` / `make_handler` 源码） |
| `lan_https_proxy.diff` | 只供审阅 | 上面那个文件相对现行版本的 diff（+377 / −6，删掉的 6 行是文档字符串和启动日志） |

## 1. 设计取舍

- **路径前缀而不是新域名**：新域名要 DNS 记录和证书；前缀方案只动 OpenResty。glasscast 的路由（`/s/…`、`/mcp`、`/healthz`）不受影响：nginx 前缀匹配取最长，`^~ /reader/` 比 `root.conf` 的 `^~ /` 长。
- **单独的 `proxy/reader.conf` 而不是改 `root.conf`**：回滚就是删一个文件；`root.conf` 已经手改过（`/admin/` 404、rewrite），少碰它。前提是站点主配置 `include` 了 `proxy/*.conf`（§2 第二条检查）。
- **白名单式转发**：只放行 glassreader 实际有的三类路径，`/reader/` 下其他一律 404（同 glasscast 方案 P 的写法）。页图 location 关访问日志（地址里有 key）；`/reader/mcp`、`/reader/healthz` 照常记日志，排查平台超时要看。
- **另开一条隧道，而不是给 glasscast 的隧道多加一个 `-R`**：两个进程互不影响，重启一个不断另一个；保温请求打 `$PUBLIC_URL/healthz` = `/reader/healthz`，reader 的 upstream 池也是热的（冷连接约 0.7 s，平台工具超时 3 s）。多加 `-R` 的写法在 `tunnel.sh` 注释里，不推荐。
- **局域网转发器必须逐请求分流**：手机（CFNetwork）会在一条连接上先后请求 glasscast 和 glassreader，按连接分流会串台。所以 `--route` 模式里解析 HTTP/1.1（`Content-Length` / chunked / HEAD / 1xx / 204 / 304 / 读到关闭），每个上游各一条复用连接；旧连接被上游关掉时 GET / HEAD 换新连接重发一次；上游连不上回 502。请求体整个读进内存（上限 1 MiB）：走局域网的只有手机取图，MCP 永远走公网。
- **端口 18780**：和本机 8780 对应，照 glasscast 的 18766 ⇐ 8766。§2 先确认服务器上没人占。

## 2. 执行前的只读检查（不改任何东西）

在 Mac 上：

```bash
ssh ubuntu@server.example.com "sudo grep -rln 'cast.example.com' /opt/1panel/apps/openresty/openresty/conf/conf.d/"
```

对上一步找到的站点主配置（假设叫 `cast.example.com.conf`）：

```bash
ssh ubuntu@server.example.com "sudo grep -n 'include' /opt/1panel/apps/openresty/openresty/conf/conf.d/cast.example.com.conf"
```

期望有一行 `include /www/sites/cast.example.com/proxy/*.conf;`（或等价的通配）。没有就按 §1 改成粘进 `root.conf`。

```bash
ssh ubuntu@server.example.com "sudo ss -ltnp 'sport = :18780'; sudo ls -l /opt/1panel/apps/openresty/openresty/www/sites/cast.example.com/proxy/ /opt/1panel/apps/openresty/openresty/conf/conf.d/"
```

期望 18780 没有监听；记下现有文件的属主和权限，下面 `install` 时照着来（下面假设 root:root 0644）。

```bash
ssh ubuntu@server.example.com "sudo cat /opt/1panel/apps/openresty/openresty/www/sites/cast.example.com/proxy/root.conf" | diff - glasscast/deploy/1panel/root.conf
```

期望没有输出（线上 `root.conf` 和仓库副本一致）；有差异先弄清楚是谁改的，再继续。

## 3. 执行（审过、同意之后）

**Mac（先起服务和隧道，nginx 生效前它们只是空等）**

1. `reader/.env` 换成正式配置（`.env.example` 有说明）：固定的 `READER_TOKEN`、`READER_KEY`，`READER_PUBLIC_URL=https://cast.example.com/reader`，`READER_BOOKS_DIR` 指向真正的书库。
2. 新终端标签页起服务：

   ```bash
   cd reader && set -a && . ./.env && set +a && uv run glassreader serve
   ```

3. 再开一个标签页起隧道：

   ```bash
   reader/deploy/tunnel.sh --keep-awake
   ```

**服务器**

4. 拷文件、测试、生效（`-t` 不通过就**不要** reload，直接做 §4 的回滚）：

   ```bash
   scp reader/deploy/1panel/zz-glassreader-upstream.conf reader/deploy/1panel/reader.conf ubuntu@server.example.com:/tmp/
   ```

   ```bash
   ssh ubuntu@server.example.com "sudo install -m 644 -o root -g root /tmp/zz-glassreader-upstream.conf /opt/1panel/apps/openresty/openresty/conf/conf.d/ && sudo install -m 644 -o root -g root /tmp/reader.conf /opt/1panel/apps/openresty/openresty/www/sites/cast.example.com/proxy/ && sudo docker exec 1Panel-openresty-XXXX openresty -t && sudo docker exec 1Panel-openresty-XXXX openresty -s reload"
   ```

5. 从公网验证。家里路由器把域名解析到 Mac，所以要钉到公网地址（和 `tunnel.sh` 保温同一个办法）：

   ```bash
   IP=$(curl -fsS -H 'accept: application/dns-json' 'https://1.1.1.1/dns-query?name=cast.example.com&type=A' | grep -oE '[0-9]+(\.[0-9]+){3}' | tail -1); for p in /reader/healthz /healthz /reader/nope; do curl -s -o /dev/null -w "$p %{http_code}\n" --resolve "cast.example.com:443:$IP" "https://cast.example.com$p"; done
   ```

   期望 `/reader/healthz 200`、`/healthz 200`（glasscast 照常）、`/reader/nope 404`。再在同一个终端验 MCP（先读 `.env` 拿 token，期望列出 4 个工具）：

   ```bash
   set -a && . reader/.env && set +a && curl -s --resolve "cast.example.com:443:$IP" https://cast.example.com/reader/mcp -H "Authorization: Bearer $READER_TOKEN" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | grep -o '"name":"[a-z_]*"'
   ```

**Mac 局域网**

6. 替换转发器（留个备份），重启 lan-https 标签页里的转发器，加上 `--route`：

   ```bash
   cp glasscast/tools/lan_https_proxy.py glasscast/tools/lan_https_proxy.py.bak && cp reader/deploy/lan_https_proxy.py glasscast/tools/lan_https_proxy.py
   ```

   ```bash
   cd glasscast && uv run python tools/lan_https_proxy.py --route /reader/=127.0.0.1:8780
   ```

7. 在家里的网络上验证（这时域名解析到 Mac）：

   ```bash
   for p in /reader/healthz /healthz; do curl -s -o /dev/null -w "$p %{http_code}\n" "https://cast.example.com$p"; done
   ```

之后才是平台：MCP 插件草稿的基础 URL 填 `https://cast.example.com/reader`、endpoint `/mcp`（`reader/README.md`「上平台时」）。

## 4. 回滚

- 服务器：删掉两个新文件再测试、生效（`root.conf` 没动过，不用恢复）：

  ```bash
  ssh ubuntu@server.example.com "sudo rm -f /opt/1panel/apps/openresty/openresty/conf/conf.d/zz-glassreader-upstream.conf /opt/1panel/apps/openresty/openresty/www/sites/cast.example.com/proxy/reader.conf && sudo docker exec 1Panel-openresty-XXXX openresty -t && sudo docker exec 1Panel-openresty-XXXX openresty -s reload"
  ```

- Mac：停掉 reader 的隧道和服务；转发器去掉 `--route` 重启就回到原来的行为（同一份代码路径），或者把 `.bak` 拷回去。

## 5. 已知风险和注意

- **在家时 reader 离不开转发器的 `--route`**：路由器把整个域名都解析到 Mac，没有 `--route` 时 `/reader/…` 会被原样转给 glasscast，得到 404。
- **`jump(pages=N)` 可能多翻一次**：`/reader/mcp` 开了 `proxy_next_upstream non_idempotent`（隧道重连后池里旧连接失效时必须重发，否则平台先拿到 502、撞上 3 s 超时），SKILL.md 也让模型超时重试一次。如果第一次其实已经执行，按页数相对翻页会翻两次；按章节 / 百分比跳转和其余工具重发无害。
- **1Panel 面板**：在面板里改这个网站的反向代理，可能重写 `proxy/` 下的文件。改过面板之后检查 `reader.conf` 和 `root.conf` 还在、内容没变。
- **日志里的 key**：`reader.conf` 给页图关了访问日志，但隧道断开导致 502/504 时，nginx 的 `error_log`（warn 级）仍会记录请求行，里面有 key。1Panel 建的站点 `log/` 默认是 `0755`、日志文件 `0644`，服务器上的其他用户读得到。建议把 `log/` 改成 `0700`，同 glasscast `server-setup.sh` 的做法。怀疑泄露就换 `READER_KEY`，重启 glassreader，模型下次 `open_book` 就会拿到新地址。
- **转发器的分流模式是新代码**：本机测过（见 §6），还没和真手机跑过。手机在家取不到页图时，先看转发器 `-v` 日志。

## 6. 本机验证记录（2026-10-29，没碰服务器）

- `reader/tests/test_lan_proxy.py`（20 个）：自签证书 + 两个假上游。同一条 TLS 连接交替请求两个服务、剥前缀（含 `/reader`、带查询串）、`/readerx` 不误匹配、POST 定长 / chunked 请求体、`Expect: 100-continue`、chunked 响应、HEAD、304、读到关闭为止、上游悄悄关掉旧连接后重发、上游不在时 502 且不影响另一个上游、坏请求 400；不带 `--route` 时仍是原样转发。
- `reader/tests/test_deploy.py`（7 个）：nginx 片段的括号 / 分号、不含 `add_header` / HTTP/3、upstream 与 glasscast 同构、端口和公网前缀在 upstream / `tunnel.sh` / glassreader 之间一致、`reader.conf` 只转发 glassreader 实有的路径、不带 `--route` 的代码与现行版本逐字相同、`lan_https_proxy.diff` 打到现行版本上正好得到草稿。
- 真服务联调：草稿转发器开在 127.0.0.1:8443（不是 443），`/reader/` → 正在跑的 glassreader，其余 → glasscast。curl 在一条连接上依次取 `/reader/healthz`、两张页图、glasscast 的 `/healthz`、错 key 的页图：200 / 200 / 200 / 200 / 404，日志里 key 已打码。
- `reader/deploy/tunnel.sh --dry-run`：生成的 ssh 命令是 `-R 127.0.0.1:18780:127.0.0.1:8780 ubuntu@server.example.com`。
- **没做**：`openresty -t`（本机没有 nginx，只能在服务器上测）；任何服务器操作；真手机。
