# Changelog

## v3.2.6

修复：
- `Post.images` 字段类型从 `list[str]` 改为 `list[str | bytes]`，支持 aiimg 等插件直接传递 bytes 图片数据（之前 Pydantic 验证拒绝 bytes 输入导致发布失败）


## v3.2.5

修复：
- `_convert_to_supported_format` 增加色彩模式检查：已支持格式（JPEG/PNG/GIF/BMP）若色彩模式非 RGB/L（如 CMYK/RGBA/YCCK/P），也会重新编码为 baseline RGB JPEG 后再上传
- `_upload_image` multipart `picfile` 部分新增 `Content-Type` 头（如 `image/jpeg`），与浏览器上传行为一致


## v3.2.4

修复：
- `normalize_images` 下载/接收图片后自动检测格式，如为 QQ 空间不支持的格式（如 WebP、TIFF 等）则自动转换为 JPEG 后再上传


## v3.2.3

修复：
- 移除 `QzoneContext.headers()` 中硬编码的 `Host` 和 `Connection` 字段，改由 aiohttp 根据请求 URL 自动设置正确的 Host（根因：上传接口域名 `up.qzone.qq.com` 与硬编码的 `user.qzone.qq.com` 不匹配，导致服务器拒绝请求返回非 JSON 响应，触发"响应内容缺少 JSON 片段"和后续 Broken pipe 连锁失败）
- 图片上传循环增加网络异常捕获（`aiohttp.ClientError` / `OSError` / `ConnectionError`），网络中断时重试而非直接崩溃
- 上传接口增加调试日志（HTTP 状态码、响应长度、前200字符），便于排查


## v3.2.2

修复：
- 图片上传增加单张3次重试（间隔递增2s/4s），单张失败后跳过继续上传其余图片，仅全部失败才抛异常（原行为：任何一张失败直接导致整条说说发布失败）
- `parse_upload_result` 解析失败也纳入重试范围
- 修复 `client.py` 中 `timeout` 参数传入 `int` 导致 `aiohttp.ClientTimeout` 类型不匹配（`_upload_image` 的 `timeout=60` 实际无效）


## v3.2.1

修复：
- download_file 对 file:/// URL 路径未做 URL 解码，含中文/特殊字符的路径读取失败


## v3.2.0 (修复版)

重大修复 - 发布说说稳定性全面提升：

- 🔐 **Cookie 刷新机制重构** (`session.py`)：
  - 新增 `refresh_login()` — 带验证 + 智能重试的强制刷新（最多 3 轮，延迟 2s/5s/8s）
  - 获取 Cookie 时先尝试 CQHttp 动态获取，失败后兜底使用配置的 `cookies_str`
  - Cookie 就绪后立即发起轻量验证请求确认可用性
  - 完善日志：记录 Cookie 来源（CQHttp / 配置 / 手动传入）及关键字段是否存在

- 🔄 **请求层重试增强** (`client.py`)：
  - 登录失效时使用 `refresh_login()` 替代原来的 `login()`，确保 Cookie 真正刷新
  - 登录刷新后延迟 2 秒再重试，给 QQ 协议层缓冲时间
  - 空响应时记录 HTTP 状态码和 URL，便于诊断
  - 扩展登录失效检测范围（401 + code=-3000 + 403/5xx 中含 -3000）

- 📝 **发说说请求完善** (`api.py`)：
  - publish 请求新增显式 `Content-Type`、`Referer`、`Origin` 请求头
  - 保留 `format=json` 参数（已有）

- 🔍 **错误信息增强** (`service.py`)：
  - 新增 `_retry_with_refresh()` — 每次重试前自动 `invalidate()` 清空缓存 Cookie
  - 新增 `_build_publish_error()` — 发布失败时输出 code + HTTP 状态 + 具体消息
  - 不再只输出 `失败：{}`，改为细粒度诊断信息

- 🌐 **User-Agent 更新** (`model.py`)：
  - Chrome 138 → Chrome 142

- 🐛 **Bug 修复**：
  - 修复登录失效后 `client.py` 重试时仍使用相同过期 Cookie 的问题
  - 修复 Service 层三次重试间 Cookie 从不刷新的问题

---

## v3.1.1

改进：

- 🧠 LLM 评论说说时传入已有评论：`generate_comment` 现在会把帖子下已有评论列表传给 LLM，并提醒「不要重复，从不同角度补充」，避免 bot 复读同一句话

---

## v3.1.0

重大修复：

- 📝 优化默认 LLM 提示词：从教条式规则改为引导式风格，给人格留出表达空间
  - 写说说：不再强制「首句抛争议问题」，改为「内容自由发挥，保持真实感」
  - 评论：不再强制「犀利」，改为「用自然的说话方式评论」
  - 回复：不再干瘪，改为「根据性格简短自然回应」

- 🔥 错误处理全面加固：所有异常只记录日志，不再将原始错误信息暴露给用户，统一返回友好提示
- 🧠 LLM 注入人格：写说说、评论、回复时自动读取 AstrBot 全局人设（Persona），保证 AI 生成内容风格与 Bot 人格一致；三种场景的任务提示词仍保持用户可自定义
- 💪 稳定性大幅提升：所有写操作（发说说/评论/回复/点赞/删除）增加自动重试机制（间隔 10s/30s/60s），避免网络波动导致失败；发说说前增加登录态预检
- 🚫 定时评论任务移除自动点赞：点赞接口极不稳定容易导致 403，改为纯评论模式
- ⏰ 默认请求超时从 10 秒提升至 30 秒，最大可配置到 120 秒
- 📝 修正 README 和日志中偏移单位描述：统一使用「秒」而非「分钟」（代码实际单位一直是秒）

代码质量：

- 🔧 清理 Post 类重复定义（model.py 与 post.py 各一套），统一为 model.py 中的唯一版本
- 🧹 移除死代码与重复工具函数（download_file、extract_and_replace_nickname、remove_em_tags 各保留一份）
- 💡 改进异常日志输出格式，增加上下文信息便于排查


## v3.0.5

新功能：

- 新增 `silent_approve`（静默模式）配置项，开启后投稿/过稿/拒稿/评说说/定时评论/定时发说说等所有操作均不发送通知消息

## v3.1.0

Bug 修复：

- 在自动评论调度器中使用专用的评论偏移配置，而不是发布偏移配置。
- 确保自动评论过滤能够正确检测已有的自我评论，同时利用内存中的帖子数据和之前保存的帖子记录，防止在同一条 Feed 上重复评论。

改进：

- 在为自动评论查询 Feed 时，更早排除自己的帖子和已评论的帖子，以减少不必要的处理。
- 在自动评论任务中为每条帖子添加错误处理和结构化日志记录，这样单条评论失败不会中断剩余帖子的处理。

## v3.0.3

- Refactor: 重构自动发说说、自动评论的定时调度逻辑，改为以 `cron` 触发点作为基准时间，并通过 `publish_offset_minutes` / `comment_offset_minutes` 在基准时间前后随机浮动（`±N` 分钟）；偏移为 `0` 时严格按 `cron` 触发。

## v3.0.2

- Fix: 修复 QZone API 返回空字符串时 `json5.loads()` 抛出 `ValueError: Empty strings are not legal JSON5` 导致插件异常中断的问题。
- Improve: 新增接口响应兜底解析，针对空响应、无 JSON 片段、JSON 解析失败返回可处理的错误结果，避免直接抛异常。
- Improve: `QzoneParser.parse_response` 在解析结果非 `dict` 时，改为返回统一错误对象，不再抛出 `RuntimeError`，调用方错误处理路径更一致。
- Fix: 修复 `403` 被误判为“登录失效”并重复重登的问题；仅在 `401` 或接口明确登录失效（`code = -3000`）时触发重登。
- Improve: 查询说说失败时细化错误提示，区分“无权限查看”“登录状态失效”“接口响应异常”“暂无可见说说”等场景。
- Refactor: 新增 `core/qzone/constants.py`，集中维护错误码、错误消息、HTTP 状态及内部元数据键，降低多处散落导致的不一致风险。
- Refactor: 将 HTTP 状态注入到内部元数据 `__qzone_internal__.http_status`，并在 `ApiResponse.data` 中剥离内部字段，避免与业务字段冲突。
- Improve: 统一错误消息常量为中文（如“响应内容为空”“权限不足”），更符合插件用户使用场景。
- Behavior: 保持无参数调用默认行为为查询最新一条说说（`pos=0, num=1`）。
