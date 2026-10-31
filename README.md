# reader：千问 AI 眼镜看 TXT 小说（glassreader）

> **这个仓库有两个分支**，是同一个阅读器的两种接入方式。现在看的是 `agent`（Agent 文字版）。
>
> | 分支 | 版本 | 接入方式 | 适合 | 限制 |
> |---|---|---|---|---|
> | `main` | 页图版 | MCP 插件 + Skill。服务端把每页渲染成图片，手机下载后传给眼镜 | 想要好看的排版：字体、字号、粗细、行距、两端对齐、缺字回退都由服务端控制 | 翻页约 1 秒一页，不能听书 |
> | `agent` | Agent 文字版 | Agent 自定义接入。服务走 WebSocket 下发文字，眼镜用原生字体显示 | 想要翻页立即响应，或者想听书：平台朗读，页面跟着翻 | 字号只有固定档位，行距不能调，没有两端对齐和缺字回退 |
>
> 切换：克隆后运行 `git switch agent` 或 `git switch main`。只要文字版可以直接 `git clone -b agent https://github.com/Edint386/QwenGlass-Reader.git reader`。两个版本可以各跑一个实例，共用书库和进度，换个 Skill 接着读。
>
> 这个分支也带着页图版的代码，下文以页图版为主。文字版的用法、听书和上线步骤见 [docs/agent.md](docs/agent.md)。

电脑端读 TXT、切章分页、把每页渲染成适合绿色光机的图片；眼镜上的 Waft-DSL 页面用镜腿滑动翻页，两侧栏和菜单是原生控件。翻页不经过大模型，页图请求本身就是阅读进度。打开书、换书、跳章、查进度走语音 → 大模型 → MCP 工具。

调研结论、方案比较、真机待验证清单见 [docs/research.md](docs/research.md)。

| 路径 | 内容 |
|---|---|
| `glassreader/book.py` | TXT 解码（UTF-8 / GB18030 / BOM）、切章、“第 N 章”查找 |
| `glassreader/pager.py` | 按章分页（禁则 / 悬挂标点 / 两端对齐）、页图渲染（478×290，4 级灰度 PNG） |
| `glassreader/agent.py` | Agent 自定义接入的文字传输版（`docs/agent.md`） |
| `glassreader/library.py` | 书库扫描、阅读位置（字符偏移 + 字号）、按计数差值翻页、跳转、进度文件 |
| `glassreader/mcp_tools.py` | MCP 工具 `list_books` / `open_book` / `jump` / `reading_status` |
| `glassreader/server.py` | HTTP：页图 `/r/<key>/<书>/<会话>/<计数>.png`、MCP `/mcp`（Bearer，无状态） |
| `glassreader/cli.py` | `glassreader serve` / `books` / `open` |
| `pages/reader/` | 眼镜阅读页 `template.xml` + Mock 数据 `data.json`（平台 `Pages/reader` 的本地源） |
| `skill/` | `SKILL.md`、`ui_schema.json`（平台技能的本地源） |
| `deploy/cloud/` | 云端运行：Docker 容器、OpenResty 站点、证书续期、发布和传书脚本，见 [deploy/cloud/README.md](deploy/cloud/README.md) |
| `deploy/` | 之前的 Mac + 隧道方案，留作回滚：OpenResty `/reader/` 分流、第二条隧道、局域网转发器按路径分流，见 [deploy/README.md](deploy/README.md) |
| `tests/` | 测试（不需要网络、服务器和真机） |
| `samples/` | 原创示例小说（仅用于排版测试） |

## 配置

复制 `.env.example` 为 `.env` 填好，`set -a; . ./.env; set +a` 后运行。优先级：命令行 > 环境变量 > 默认值。

| 变量 | 说明 |
|---|---|
| `READER_TOKEN` | 平台 MCP 插件的 Bearer Token。不填每次启动随机生成（平台插件会失效） |
| `READER_KEY` | 页图地址里的随机段：眼镜发不了请求头，页图只能靠路径鉴权 |
| `READER_PUBLIC_URL` | 眼镜和平台访问的公网根地址，可带路径前缀（反代剥掉前缀后转发），如 `https://cast.example.com/reader`。不填只能本机联调 |
| `READER_BOOKS_DIR` | TXT 书库目录，递归收集 `*.txt`（默认 `~/Books`） |
| `READER_STATE_FILE` | 进度文件（默认 `~/Library/Application Support/glassreader/progress.json`） |
| `READER_HOST` / `READER_PORT` | 监听地址（默认 `127.0.0.1:8780`） |
| `READER_SWAP_SWIPE` | `1` = 前滑 / 后滑方向对调（真机方向与预期相反时用，不用改页面） |
| `READER_ALLOWED_HOSTS` | MCP 额外放行的 Host 头，逗号分隔 |

## 运行

下文的命令都假定仓库克隆在名为 `reader` 的目录里（和 glasscast 并排放）：

```bash
git clone https://github.com/Edint386/QwenGlass-Reader.git reader
```

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。排版用 macOS 系统自带的冬青黑体 `Hiragino Sans GB`、宋体 `Songti SC`；Linux 上用 `fonts-noto-cjk`（见 `glassreader/pager.py`）。

```bash
cd reader && uv sync
```

```bash
cd reader && uv run pytest -q
```

```bash
cd reader && uv run glassreader serve --books samples --state .state/progress.json
```

不设 `READER_PUBLIC_URL` 时，启动信息里会打印一份能直接贴进模拟器的 dataSource。其他子命令：`glassreader books`（书库和进度）、`glassreader open [书名]`（打印一份新的 dataSource，key 要和 serve 一致）。

### 在本地模拟器里看

模拟器在 glasscast 项目里：把 glasscast 仓库克隆到本仓库旁边、目录名 `glasscast`，先运行它的 `tools/simulator/fetch_assets.sh` 下载内核。

```bash
cd glasscast && uv run python tools/simulator/serve.py --port 8772 --pages ../reader/pages
```

1. 打开 <http://127.0.0.1:8772/?preset=page:reader>，等状态变成"就绪"。
2. 把右侧 data 换成 serve 启动时打印的 dataSource（或 MCP `open_book` 返回的 `u`、`t`），点**渲染**。每次都要新会话号：内核按完整地址缓存页图，服务端按会话记计数基线。
3. 触控键：`后滑 next` 下一页、`前滑 prev` 上一页、`单击 confirm` 开菜单。服务端日志会打印每次请求的计数差值和页码。

## 上平台（2026-10-30 已做，真机可用）

1. 公网入口：现在是云端的 `reader.example.com`（[deploy/cloud/README.md](deploy/cloud/README.md)）。之前是 `cast.example.com` 按路径 `/reader/` 转给本服务。执行清单在 [deploy/README.md](deploy/README.md)（改服务器 OpenResty、第二条隧道、局域网转发器分流）。
2. 平台新建 MCP 插件（草稿）：基础 URL = `READER_PUBLIC_URL`，endpoint `/mcp`，认证 Bearer Token = `READER_TOKEN`（只填 token 本体），传输协议「无状态协议」。保存后应拉到 4 个工具。
3. 新建技能「眼镜看小说」：贴 `skill/SKILL.md`，把里面的 `READER` 换成平台给这个插件的编码（形如 `AbC123`，工具卡片存成 `^^编码.工具名^^`）；`ui_schema.json` 保持 `[]`；新建页面 `Pages/reader`，贴 `pages/reader/template.xml` 和 `data.json`。
4. 编辑器沙箱里说"看小说"，确认先调 `open_book` 再调 UI 页面渲染；然后真机测试 research §7 的 P1～P7。发布由你来点，范围选「个人私有」。
