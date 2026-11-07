# 通道路由与错误处理

## 配置优先级

1. 会话证据与用户明确选择优先。`--mode native` 不读 API 凭据、不执行 API 引擎；不凭内置工具存在就认定账号登录。
2. 显式 `--config` 或 `IMAGEGEN_RELAY_CONFIG` 指向的独立配置失败即停止，不静默换服务。
3. 自动模式识别默认 OpenAI provider 且 ChatGPT 登录时使用 native；不把 OAuth token 当 API Key。
4. API 模式下，已授权独立 DPAPI 配置 → 旧版私有 JSON（只读兼容）→ 成对 `IMAGEGEN_API_KEY` / `IMAGEGEN_BASE_URL`，可选 `IMAGEGEN_MODEL`。
5. 只读当前 Codex provider/profile 对应 Base URL；凭据来自该 provider 的 bearer/env_key 或明确允许的 OpenAI API Key，不取其它供应商的第一个 Key。
6. cc-switch 只有地址完全匹配时可补充当前网关凭据；不同服务不混用 Key。无明确 Codex 网关时才可采用唯一当前 cc-switch provider。非当前供应商仅作备选，用户明确选定才用。
7. 不改 Codex 登录文件、全局环境或 cc-switch 当前供应商，不读取浏览器 cookies、历史或剪贴板寻找凭据。

模型列表支持 `{data:[{id:...}]}` 与 `{models:[{slug:...}]}`。当前执行适配器支持 GPT Image 兼容协议；其它图片模型可识别但需要专用适配器。列表中出现某模型不代表图片端点、编辑权限、额度或全部参数可用。

## 状态处理

| 状态 | 处理 |
| --- | --- |
| native | 调用宿主内置 image_gen，不问 Key |
| ready | 用当前网关、匹配 Key 和发现模型执行 |
| need_credentials | 已知地址时只隐藏输入缺失 Key |
| need_image_model | 当前服务未发现兼容图片模型，补充图片服务或由用户选已有服务 |
| model_not_listed | 不擅自替换用户指定模型，说明情况 |
| unsupported_image_api | 说明需要对应接口适配器，不反复要求换 Key |
| authentication_failed / permission_denied | 分别诊断 401 / 403 |
| rate_limited | 诊断限流/额度并停止，不自动付费重试 |
| model_list_unavailable | 仅在用户确认明确模型与一次试调用后可 allow-unverified |
| network_error / invalid_model_list / gateway_error | 诊断网络、格式或网关，不归为“缺生图 Key” |
| unknown | 无法确定方式时只问必要信息，不猜服务 |

## 凭据与收费边界

Key 使用交互 TTY 隐藏输入，默认不保存。只有用户明确希望记住时用 Windows DPAPI 加密保存；非 Windows 只支持临时输入或安全环境变量。不可把 Key 放进命令行、prompt、README、日志或元数据；禁止管道退化为可回显输入。

远程网关必须 HTTPS，回环允许 HTTP。不跨站跟随凭据请求重定向。网关与 Key 成对使用，不静默切服务。付费请求结果不确定或失败时不自动换后端重试。

`--backend auto` 在任何收费请求前选后端；具体支持范围见 `components.md`。上述技术命令由智能体执行，不作为用户日常使用步骤。

## 模型发现与 Cloudflare 403 / 1010

模型发现请求使用真实应用标识 `User-Agent: imagegen-relay/1.0`，不依赖 Python urllib 的默认标识，不伪装浏览器或轮换身份。某些网关边缘策略会拒绝默认客户端标识；这类拒绝不应直接解释为 API Key 错误或图片模型不可用。

当 `/models` 返回 HTTP 403 时，程序最多检查 8192 字节的响应正文。只有响应头具有 Cloudflare 标识，且正文明确包含 Error 1010，才在保留 `permission_denied` 和原始 HTTP 状态的同时输出：

- `diagnostic: cloudflare_1010`
- `next_action: diagnose_gateway_client_identity_or_edge_policy`

此时应检查客户端标识与网关边缘策略，而不是要求用户反复更换 Key。普通 403、401、429 和其他错误仍按原有状态处理，不把所有 Cloudflare 响应都归因于 1010。诊断仅输出固定标识，不输出响应正文、响应头值或凭据；不自动重试、切换服务或修改服务器安全配置。

模型列表查询成功只说明发现阶段恢复，不能证明付费生图、编辑端点、额度或全部参数可用。离线回归测试覆盖请求标识、诊断边界、错误状态兼容性、资源关闭和敏感内容不泄露。

### 有 CF 与无 CF 的兼容行为

- 两类网关使用相同的标准模型发现请求，无需配置 CF 开关。`User-Agent` 是应用标识，不是 CF 专用协议；成功响应不要求具有 CF 响应头。
- 正常 JSON 模型列表无论经过 Cloudflare、Nginx 或没有服务器标识，均按原有格式解析和选择模型。
- 普通认证失败、权限拒绝、限流及上游故障保留原有错误分类；仅有 CF 响应头不足以判定为 1010。
- CF 或其他网关返回的 HTML 页面不能当作模型列表成功结果。此修复不绕过验证码、浏览器挑战或网关访问控制；仍然被拦截时需要服务端提供适合 API 客户端的访问策略。
- 不额外增加探测请求，不轮换客户端身份，不自动重试收费请求。
