# QwenGlass-Reader：在千问 AI 眼镜上看自己的 TXT 小说

![眼镜上的阅读页](docs/reading.png)

> 这个仓库有两个分支，是同一个阅读器的两种接入方式。现在看的是 `main`（页图版）。
>
> | 分支 | 版本 | 接入方式 | 好处 | 限制 |
> |---|---|---|---|---|
> | `main` | 页图版 | MCP 插件 + Skill，服务把每页画成图片 | 排版好看：字体、字号、粗细、行距、两端对齐都由服务控制，不缺字 | 翻页约 1 秒一页，不能听书 |
> | [`agent`](https://github.com/Edint386/QwenGlass-Reader/tree/agent) | 文字版 | Agent 自定义接入，服务用 WebSocket 发文字 | 翻页立即响应，能听书、智能翻页，语音指令不经过大模型 | 字号只有固定几档，行距不能调，原生字体缺的字显示成空白 |
>
> 两个版本可以各跑一个实例，挂同一个书库和进度目录，换个 Skill 接着读。

## 怎么工作

服务读 TXT、分章、分页，把每页画成适合眼镜绿色屏幕的图片（478×290，4 级灰度）。眼镜上的页面用镜腿滑动翻页，翻页不经过大模型：页面直接下载下一张页图，这个请求本身就记下了阅读进度。打开书、换书、跳章、问读到哪了，靠语音让大模型调 MCP 工具。

| 路径 | 内容 |
|---|---|
| `glassreader/` | 服务的 Python 源码：读 TXT、切章（`book.py`）、分页和画页图（`pager.py`）、书库和进度（`library.py`）、MCP 工具（`mcp_tools.py`）、HTTP 服务（`server.py`） |
| `skill/` | 千问平台上 Skill 的文件，结构和平台编辑器一样：`SKILL.md`、`ui_schema.json`、`Pages/reader/` |
| `deploy/` | `Dockerfile`、`compose.yaml` 和反向代理配置示例 `site.conf` |

代码注释里提到的 `docs/research.md` 是开发笔记，没有放进这个仓库。

## 配置

复制 `.env.example` 为 `.env` 填好。优先级：命令行 > 环境变量 > 默认值。

| 变量 | 说明 |
|---|---|
| `READER_TOKEN` | 平台 MCP 插件的 Bearer Token。不填每次启动随机生成，平台插件会失效 |
| `READER_KEY` | 页图地址里的随机段：眼镜发不了请求头，页图只能靠路径鉴权。只用字母、数字、`-` 和 `_` |
| `READER_PUBLIC_URL` | 眼镜和平台访问的公网根地址，如 `https://reader.example.com`，可以带路径前缀。不填只能本机调试 |
| `READER_BOOKS_DIR` | TXT 书库目录，递归收集 `*.txt`，扩展名不分大小写（默认 `~/Books`） |
| `READER_STATE_FILE` | 进度文件（默认 `~/Library/Application Support/glassreader/progress.json`） |
| `READER_HOST` / `READER_PORT` | 监听地址（默认 `127.0.0.1:8780`） |
| `READER_SWAP_SWIPE` | `1` = 前滑、后滑方向对调，真机方向和预期相反时用 |
| `READER_ALLOWED_HOSTS` | MCP 额外放行的 Host 头，逗号分隔 |

TXT 支持 UTF-8 和 GBK/GB18030，章节标题自动识别（“第 N 章”“序章”“Chapter 12”这类）。

## 在电脑上运行

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。排版在 macOS 上用系统自带的冬青黑体和宋体，在 Linux 上要装 `fonts-noto-cjk`。

```bash
uv sync
```

```bash
uv run glassreader serve --books ~/Books --state .state/progress.json
```

不想装 uv，`pip install .` 之后直接运行 `glassreader serve` 也行。其他子命令：`glassreader books` 列出书库和进度，`glassreader open [书名]` 打印一份可以贴进模拟器的 dataSource。

## 部署到服务器

眼镜和平台要从公网访问服务，所以要一台服务器和一个 HTTPS 域名。

1. 在仓库根目录复制 `.env.example` 为 `.env`，填 `READER_TOKEN`、`READER_KEY`（各一串随机字符），`READER_PUBLIC_URL` 填你的域名。
2. 准备书库和进度目录。容器里的用户是 10001，进度目录要它能写：

   ```bash
   cd deploy && mkdir -p books data && sudo chown 10001:10001 data
   ```

3. 把 TXT 放进 `deploy/books/`，启动：

   ```bash
   docker compose up -d --build
   ```

   服务只监听本机的 `127.0.0.1:19516`，根文件系统只读，内存上限 256 MB。
4. 反向代理照 `deploy/site.conf` 配：80 端口只放行证书校验，443 只转发 `/healthz`、`/r/`（页图）、`/mcp`（平台），其余 404。示例里证书和日志的路径是 1Panel OpenResty 的写法，按你的服务器改。
5. 打开 `https://你的域名/healthz`，返回 `ok` 就通了。

## 在千问平台上配置

1. 新建 MCP 插件：基础 URL 填 `READER_PUBLIC_URL`，endpoint 填 `/mcp`，认证选 Bearer Token，填 `READER_TOKEN`（只填 token 本身），传输协议选「无状态协议」。保存后应该拉到 4 个工具：`list_books`、`open_book`、`jump`、`reading_status`。
2. 新建技能「眼镜看小说」：
   - 贴 `skill/SKILL.md`，把里面的 `READER` 换成平台给这个插件的编码（形如 `AbC123`）。
   - `ui_schema.json` 保持 `[]`。
   - 新建页面 `Pages/reader`，贴 `skill/Pages/reader/template.xml` 和 `data.json`。
3. 在编辑器里说“看小说”，确认先调 `open_book`、再调 UI 页面渲染。然后点真机测试，绑定你的眼镜。发布范围选「个人私有」。

## 眼镜上的操作

| 状态 | 前滑 / 后滑 | 单击 | 双击 |
|---|---|---|---|
| 阅读 | 下一页 / 上一页 | 打开菜单 | 退出（进度已经存在服务上） |
| 菜单 | 移动选中项 | 返回阅读、自动翻页、下一章、上一章、显示设置 | 同上 |
| 自动翻页 | 每页停留时间加减 1 秒（1～30 秒） | 确定 | 同上 |
| 显示设置 | 选字号、粗细、亮度、行距，进去后前滑增大、后滑减小 | 保存 | 同上 |

语音（经过大模型，说法比较随意）：看小说、继续看、打开《书名》、换一本、书架上有什么、跳到第 N 章、下一章、跳到一半、跳到百分之 N、回到开头、往后翻 N 页、我看到哪了。
