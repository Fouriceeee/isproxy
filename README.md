# ISProxy

一个仅 Python 标准库的本地反向代理：把 `127.0.0.1:<port>` 映射到指定站点。

## 安装

需要 Python >= 3.14

推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
```

## 使用

```bash
# 指定目标站点和端口
uv run cli https://www.example.com --port 8080

# 也可以用模块方式运行
uv run python -m isproxy https://www.example.com -p 8080
```

启动后浏览器打开 `http://127.0.0.1:8080` 即可访问目标站点，无需在浏览器里配置代理。

## 图形界面（Tkinter）

```bash
uv run gui

# 或者
uv run python -m isproxy.gui
```

在界面中可以：

- 完整页面链接（如 `https://www.example.com/example_dir/example.html`）后自动解析域名（`https://www.example.com`）和资源路径（`/example_dir/example.html`）；
- 配置监听地址和端口，启动 / 停止代理；
- “打开页面”会打开“本地代理地址 + 资源路径”（如 `http://127.0.0.1:8080/example_dir/example.html`）；
- “复制代理链接”可把当前本地代理链接复制到剪贴板；
- “JS 修改规则…”可用列表方式新增、编辑、删除和排序规则；
- 实时查看请求日志：时间、客户端、方法、路径、状态码、改写次数、耗时。

### 参数

| 参数 | 说明 |
| --- | --- |
| `target` | 上游站点 URL，可带路径前缀，如 `https://www.example.com/example_dir/example.html` |
| `--host` | 本地监听地址，默认 `127.0.0.1`；设为 `0.0.0.0` 可供局域网访问 |
| `-p, --port` | 本地监听端口，默认 `8080`；设为 `0` 时随机分配 |
| `--timeout` | 连接上游的超时秒数，默认 `30` |
| `-k, --insecure` | 不校验上游 HTTPS 证书 |

## 打包为独立 exe

Windows 下执行：

```bat
build_gui_exe.bat
```

构建完成后得到带版本号的单文件程序（版本号取自 `pyproject.toml` 的 `project.version`）：

```text
dist\isproxy-gui-<version>.exe
```

双击即可运行，不需要安装 Python 或项目依赖。

也可以直接执行构建命令（把 `<version>` 换成当前版本号）：

```bash
uv run --with "pyinstaller>=6.22" pyinstaller --noconfirm --clean --onefile --windowed \
  --name isproxy-gui-<version> --paths src \
  --specpath build --workpath build --distpath dist \
  launcher_gui.py
```

说明：

- `build_gui_exe.bat` 会自动从 `pyproject.toml` 读取版本号并拼到 exe 文件名末尾；
- 使用 `--windowed` 构建，不会额外弹出控制台窗口；
- `--onefile` 首次启动时会解压到临时目录，启动速度略慢；
- 打包后 JS 规则保存在 **exe 所在目录**的 `js_rules.json`；源码运行时仍保存到 `~/.isproxy/js_rules.json`；

## JS 修改（可选）

可以按规则修改代理返回的 HTML 内联 `<script>` 以及同源外部 JS 的内容。

规则文件示例 `js_rules.json`：

```json
{
  "enabled": true,
  "inline_scripts": true,
  "external_scripts": false,
  "rules": [
    {
      "find": "console.log('debug')",
      "replace": "/* removed */"
    },
    {
      "find": "window\\.open\\(",
      "replace": "void(",
      "regex": true,
      "flags": "i",
      "url": "/python3/*"
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否启用 JS 修改 |
| `inline_scripts` | 是否修改 HTML 内联 `<script>` |
| `external_scripts` | 是否修改同源外部 JS 响应 |
| `rules[].enabled` | 是否启用单条规则，默认 `true`；设为 `false` 时跳过该规则 |
| `find` / `replace` | 查找 / 替换内容 |
| `regex` | 是否按 Python 正则处理 |
| `flags` | 正则标志：`i` / `m` / `s` |
| `url` | 可选，按上游路径通配符限定，如 `/python3/*` |
| `count` | 可选，最多替换次数，`0` 表示全部 |

CLI 使用：

```bash
uv run isproxy https://www.runoob.com --js-rules js_rules.json
```

GUI 使用：点击“JS 修改规则…”打开规则列表，可新增/编辑/删除/上移/下移规则；选中规则后点“启用/禁用”可单独开关某条规则，编辑弹窗中也有“启用此规则”复选框；支持 JSON 导入导出。源码运行时规则保存在 `~/.isproxy/js_rules.json`，打包为 exe 后保存在 exe 同目录的 `js_rules.json`；代理运行中保存会立即生效。请求日志的“改写”列显示每条请求改写了几处。

注意：

- 只处理内联脚本和经过代理的同源 JS；第三方 CDN 脚本不经过代理，无法修改；
- 脚本内容按 `Content-Type` 中的 `charset` 解码，默认 UTF-8；
- JS 规则执行后还会进行链接重写，因此规则中写入的目标站点绝对链接也会被改成本地代理地址；
- 如果页面使用 SRI `integrity` 或 CSP 脚本哈希，修改后的脚本可能被浏览器拒绝。

## 功能

- 支持 `GET` / `POST` / `PUT` / `PATCH` / `DELETE` / `HEAD` / `OPTIONS` 请求；
- 请求体支持 `Content-Length` 和 `Transfer-Encoding: chunked`；
- 自动解压 `gzip` / `deflate` / `zstd` 响应（请求上游时会移除标准库无法解码的 `br`）；
- 重写 `Location`、`Refresh` 响应头，使重定向仍停留在本地代理；
- 清理 `Set-Cookie` 中不适合本地 HTTP 的 `Domain` / `Secure` 属性；
- 重写 HTML / CSS / JS 中指向目标站点的绝对链接（含协议相对链接 `//host/...`）；
- 向上游转发 `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Host`；
- 每个连接独立线程，支持并发访问。

## 已知问题

### 切换目标后仍访问旧站点

先确认端口没有被旧的代理进程占用：

```cmd
netstat -ano | findstr :8080
tasklist /FI "PID eq <PID>"
taskkill /PID <PID> /F
```

Windows 下旧版本代理可能与新实例同时监听同一个端口，导致新实例看似启动成功，但请求仍然被旧实例处理。当前版本已禁止重复绑定，端口被占用时会直接报错。也可以直接换一个端口（例如 `8090`），这样能同时避开旧进程和浏览器缓存。

### 浏览器仍显示旧页面或跳回真实站点

同一个本地地址（如 `http://127.0.0.1:8080`）先后代理不同站点时，浏览器会按 origin 缓存旧站点的 HTML、静态资源和跳转：

- 使用 `Ctrl+F5` 强制刷新；
- 或开发者工具 → Application → Storage 清除该地址的数据；
- 或换一个本地端口，让浏览器把它当成新的 origin。

## 测试

```bash
uv run python -m unittest discover -s tests -v
```

## 许可证

本项目基于 [MIT License](LICENSE) 发布。
