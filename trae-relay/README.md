# Trae CN 反代（trae2api-cn + 9router）

把 Trae CN 账号的**积分**变成 OpenAI 兼容 API，供 9router 及各类客户端使用。
部署日期：2026-10-25

## 为什么需要这一层

签到脚本只解决「每日领积分」，积分本身没法用——Trae 的对话协议是私有的
（`chat_sessions` + SSE `plan_item`），不是 OpenAI 格式。

- **9router 自带的 `trae` provider 用不了**：上游作者已把它注释禁用
  （`registry/index.js` 里 `// p102, // trae — hidden, no tool calling`），
  且其实现只走**国际版** `core-normal.trae.ai` + OAuth refreshToken，
  与本次的 CN 账号 + `sessionid` 不是一条链路。
- 9router 支持**自定义 `openai-compatible` 节点**（`baseUrl` 可指任意地址），
  所以正确解法是：本地跑一个 CN 协议转换层，再把 9router 指过去。

```
客户端 → 9router(:20128) → trae-relay(:8000, 容器内) → trae-api-cn.mchost.guru → Trae CN 账号积分
```

## 已部署的容器

| 容器 | 镜像 | 作用 | 端口 |
|---|---|---|---|
| `trae-relay` | `ghcr.io/autumnsentiment/trae2api-cn:latest` | 协议转换，OpenAI 兼容 API | `127.0.0.1:18080` → 容器 8000 |
| `trae-mint` | 同上（复用镜像跑脚本） | JWT 续期 sidecar，每小时刷新 | 无 |

两者在 docker 网络 `trae-net` 内；9router 也已加入该网络，通过服务名
`http://trae-relay:8000` 访问，**公网不可见**（端口只绑 127.0.0.1）。

## 关键难点：relay 不会自动续期（已用 sidecar 解决）

| 事实 | 后果 |
|---|---|
| relay 只认 `refreshToken`（走 ExchangeToken）或手工粘 JWT | 它**没有** sessionid 入口 |
| `POST /cloudide/api/v3/trae/Login` 只回 `X-Cloudide-Session` + 昵称 | **不下发 refreshToken**，接不上它的自动续期 |
| 换来的 JWT 只有约 **8 小时** 寿命 | 不管就会隔夜失效 |

所以用 `mint_jwt.py` 顶替续期职责，链路与签到脚本一致：

```
sessionid(约60天) → Login → X-Cloudide-Session(约14天) → GetUserToken → JWT(8小时)
                                                        → POST /api/web-auth 热写入账号池
```

`trae-mint` 容器每小时跑一次，失败不退出（换上新 sessionid 后自动恢复）。
日志：`docker logs trae-mint`。

## 踩过的坑（实测）

- `https://api.trae.cn/api/remote/v1/*` 是 **404**；能用的是
  `https://trae-api-cn.mchost.guru/api/remote/v1`（镜像默认值正好正确）。
- `/api/ide/v2/llm_raw_chat` 对个人号固定返回
  `2001 failed to get app config: record not found`，需企业版 app config，别走 IDE 链路。
- 模型**要用账号真实开通的**。`glm-5.1` 会报
  `Trae remote model is not available for the bound account`，换成 `glm-5.3` 正常。
  正确清单以 `GET /v1/models` 为准（当前 43 条）。
- 上游 `chat_history` 字段服务端**不认**，多轮上下文要扁平成一段 query 才生效。
- `model_selection_strategy` 必须是 `manual` 才认 `model_name`。

## 9router 侧配置

节点（`/api/provider-nodes`）：

```
type=openai-compatible  prefix=trae-cn
baseUrl=http://trae-relay:8000/v1
```

连接（`/api/providers`）的 `apiKey` 填 `trae-relay/.apikey` 的内容。

调用时模型名带前缀：`trae-cn/glm-5.3`。

### 9router API 免密（省去猜登录密码）

9router 有失败锁定，别试密码。用 CLI token：

```bash
M=$(docker exec 9router cat /app/data/machine-id)
C=$(docker exec 9router cat /app/data/auth/cli-secret)
TOK=$(printf '%s' "${M}9r-cli-auth${C}" | sha256sum | cut -c1-16)
curl http://127.0.0.1:20128/api/provider-nodes -H "x-9r-cli-token: $TOK"
```

即 `sha256(machine-id + "9r-cli-auth" + cli-secret)[:16]`。

## 常用命令

```bash
# 手动刷新 JWT（正常由 sidecar 自动做）
/usr/bin/python3 /root/ZaiZaiCat-Checkin/trae-relay/mint_jwt.py

# 看可用模型
curl -s http://127.0.0.1:18080/v1/models -H "Authorization: Bearer $(cat .apikey)"

# 查今日用量/积分
curl -s http://127.0.0.1:18080/api/overview -H "Authorization: Bearer $(cat .apikey)"

# 健康检查
curl -s http://127.0.0.1:18080/healthz
```

## 生命周期与失效处理

| 凭证 | 寿命 | 失效表现 | 处理 |
|---|---|---|---|
| `sessionid`（config/token.json） | ~60 天 | mint 日志报 `Login 失败` | 回 trae.cn 重新复制 |
| `X-Cloudide-Session` | ~14 天 | 同上 | 自动，无需管 |
| `Cloud-IDE-JWT` | ~8 小时 | relay 返回 401/1001 | 自动，sidecar 每小时续 |

签到脚本与 relay **共用** `config/token.json` 里的同一个 `sessionid`，
所以这个值别忘了两个用途都在用。

## 模型/能力说明

- 支持 Chat Completions（流式 + 非流式）、Responses API、Anthropic Messages
  （`/v1/messages`，Claude Code 可直连）。
- 工具调用（tools/tool_choice）relay 侧有实现，但 Trae SOLO 的工具是它自己执行，
  桥接稳定性一般；重度工具场景建议实测后再依赖。
- 不要把 `trae-relay/.apikey`、`.env`、`data/` 提交——已在 `.gitignore` 中忽略。
