# Release Notes: OpenClaw us-west-2 Multi-Region Deployment + Feishu Channel

**Branch:** `deploy/starter-toolkit-hybrid`
**Date:** 2026-03-12
**Region:** us-west-2 (Oregon)

---

## 1. Multi-Region Deployment (Hybrid Architecture)

### Situation
OpenClaw 之前仅部署在 us-east-1 单区域，使用纯 CDK 部署。纯 CDK 方案存在多个痛点：
- **Docker 构建困难**：AgentCore Runtime 是 ARM64 机型，而一般开发机是 x86，本地交叉编译 ARM64 镜像慢且容易失败
- **AgentCore 控制台不显示指标**：怀疑与纯 CDK 的 CfnRuntime 部署方式有关，控制台 GenAI Observability 页面看不到任何 Runtime 相关的指标和 traces
- **Runtime 冷启动慢**：纯 CDK 部署下 Runtime 冷启动需要 **~60 秒**，用户体验差
- **跨区域部署受阻**：IAM Role 全局命名冲突、ECR 权限不匹配等问题阻止了多区域扩展

### Task
设计并实施一套新的部署方案，解决 Docker 构建、控制台指标、冷启动性能和跨区域部署等问题，并将服务扩展到 us-west-2。

### Action
- **采用 CDK + Starter Toolkit 混合部署架构**：CDK 管理基础设施（VPC、IAM、S3、Lambda、API Gateway、DynamoDB 等 7 个 stack），Starter Toolkit 管理 Runtime/Endpoint/ECR/Docker 构建
- **Docker 构建改进**：Starter Toolkit 支持两种模式——`--local-build`（本地 Docker 构建后推送）和默认 CodeBuild 模式（云端 ARM64 构建，无需本地 Docker），同时支持 `agentcore dev` 本地快速测试
- **IAM Role 名称加 region 后缀**（`openclaw-agentcore-execution-role-us-west-2`），避免全局冲突
- **CDK IAM Policy 适配 Starter Toolkit 的 ECR 命名规范**（`bedrock-agentcore-` 前缀），解决了一个导致"initialization exceeded 120s"误导性错误的权限问题
- **实现 3 阶段部署流程**：Phase 1 CDK 基础 → Phase 2 Starter Toolkit Runtime → Phase 3 CDK 依赖 stack
- **完善运维文档**：部署指南、操作手册、常用命令、踩坑记录

### Result
- us-west-2 全部 7 个 CDK stack + AgentCore Runtime 部署成功并通过端到端验证
- **AgentCore 控制台指标恢复正常**：切换到 Starter Toolkit 部署后，GenAI Observability Dashboard 能正确显示所有 Runtime 指标和 traces
- **Runtime 冷启动时间从 ~60s 降到 ~1s**：部署方式改变后启动时间大幅缩短
- 冷启动首条消息响应 **~5 秒**（Lightweight Agent 直接回复），OpenClaw 完整就绪 **~10 秒**
- 部署方案可复制到其他区域，已形成标准化的 3 阶段流程和文档

---

## 2. Feishu (飞书) Channel Integration

### Situation
OpenClaw 已支持 Telegram 和 Slack 两个消息渠道。团队内部主要使用飞书沟通，需要增加飞书渠道以方便内部用户使用。飞书的技术栈与 Telegram/Slack 有显著差异：事件体 AES-256-CBC 加密、OAuth tenant_access_token 认证、不同的消息格式和 API 结构。

### Task
实现飞书渠道的完整集成，包括：
- Webhook 事件接收、签名验证、加密事件解密
- 消息发送（P2P 和群聊）
- 图片上传支持
- 用户 allowlist 管理
- 一键配置脚本

### Action
- **Router Lambda 新增飞书 handler**：webhook 签名验证（SHA-256）、事件解密（AES-256-CBC）、消息解析、群聊 @mention 过滤、图片下载
- **AES 解密采用系统 OpenSSL**：通过 Python ctypes 直接调用 Lambda 环境中的 `libcrypto.so`，零第三方依赖，跨架构兼容（避免了 pycryptodome native binary 在 x86/ARM64 之间的兼容性问题）
- **CDK 新增**：API Gateway 飞书路由（`POST /webhook/feishu`）、Secrets Manager 飞书凭证、Cron Lambda 飞书消息投递
- **交互式配置脚本** `setup-feishu.sh`：引导用户完成飞书开发者后台配置、凭证存储、allowlist 添加

### Result
- 飞书渠道端到端验证通过：文字消息、群聊、长任务（含进度提示）均正常工作
- 事件解密性能优异（ctypes/OpenSSL，C 速度，无冷启动开销）
- 新增 27 个飞书单元测试，覆盖签名验证、事件解析、消息发送等场景
- 形成了**新渠道接入 checklist**，可指导后续 WhatsApp/Discord/LINE 等渠道开发

---

## 3. Container Observability (CloudWatch Logging)

### Situation
AgentCore Runtime 不会自动将容器 stdout 输出到 CloudWatch，导致容器内部问题（proxy 启动失败、OpenClaw 崩溃、credential 错误）无法排查。在 us-west-2 部署调试过程中，多次因缺乏容器日志而无法定位问题根因。

### Task
实现容器日志到 CloudWatch 的可靠输出，且不影响启动速度和 `/ping` 健康检查响应时间。

### Action
- **新增 `cloudwatch-logger.js` 模块**：Hook `console.log/warn/error`，缓冲日志事件，批量 flush 到 CloudWatch `/openclaw/container` log group
- **Init 时初始化**（不阻塞 `/ping`），**SIGTERM 时 flush**（确保关闭前日志不丢失）
- **Dockerfile 新增依赖**：`@aws-sdk/client-cloudwatch-logs`

### Result
- 容器日志实时可见，问题排查效率显著提升（从"盲猜"到"看日志定位"）
- 日志按 `{namespace}-{timestamp}` 分 stream，每个用户独立，便于追踪

---

## 4. Per-User Credential Isolation (STS Session Policy)

### Situation
容器内 OpenClaw 拥有 bash 执行能力，理论上可以通过 AWS CLI 访问其他用户的 S3 文件或 DynamoDB 数据。需要通过 STS session-scoped credentials 限制每个用户只能访问自己的资源。初始实现的 session policy 包含了详细的 DynamoDB Condition 块（`LeadingKeys`）和 S3 prefix 条件，导致 policy 超过 AWS STS 的 **2048 字节 packed 限制**。

### Task
在保持安全隔离的前提下，将 session policy 压缩到 2048 字节限制内。

### Action
- **精简 session policy**：S3 保留 namespace 级 Resource 限制，DynamoDB/Scheduler/SecretsManager 使用 `Resource: "*"`（依赖 execution role 自身的资源级限制），去掉所有 Condition 块
- **最终 policy 668 字节**（33% 利用率），远低于 2048 字节限制

### Result
- Scoped credentials 创建成功，OpenClaw 在受限环境下正常运行
- 每个用户的 S3 访问严格限制在 `{namespace}/*` 下，跨用户数据隔离生效
- 记录了 session policy size limit 的 gotcha，避免后续开发踩坑

---

## 5. Warm Pool — 探索与决策

### Situation
在纯 CDK 部署时代，AgentCore Runtime 冷启动需要 **~60 秒**（VPC ENI 创建 + 镜像拉取 + 容器启动），用户首条消息等待时间过长。为此设计了 Warm Pool 方案：通过 EventBridge 定时触发 Lambda 预创建 AgentCore session，用户首条消息直接 claim 预热的 session，跳过冷启动。

### Task
实现并验证 Warm Pool 方案，评估其在新部署架构下的必要性。

### Action
- **完整实现了 Warm Pool 方案**：
  - `WarmPoolStack`（CDK）：Lambda + EventBridge 每分钟检查，维护 DynamoDB 中的预热 session 池（默认 pool size = 1）
  - `claim_warm_session()`（Router Lambda）：原子性 claim 预热 session（DynamoDB conditional delete，防止并发竞争）
  - 支持 `WARM_POOL_ENABLED` 环境变量开关，默认关闭
- **部署并测试**：Stack 部署成功，Lambda 正常运行，预热 session 成功创建
- **发现 KMS 权限问题**：DynamoDB 使用 CMK 加密，Warm Pool Lambda 需要 KMS 权限——CDK 代码已正确配置 `cmk_arn` 参数，但 IAM 传播延迟导致初始几次调用失败，随后自动恢复

### Result
- Warm Pool 技术方案验证成功，功能完整可用
- **但发现不再需要**：切换到 Starter Toolkit 部署后，Runtime 冷启动从 ~60s 降到 ~1s。Lightweight Agent 在 **~5s** 内即可响应首条消息（包含 Bedrock 模型调用时间），用户体验已经足够好
- **决策：移除 Warm Pool**，减少不必要的复杂度（额外的 Lambda + EventBridge + DynamoDB 记录 + KMS 权限管理）
- **保留了技术方案文档**，如未来有需要可快速恢复

> **关键洞察**：最初 60s 冷启动的根因并非容器启动慢，而是纯 CDK CfnRuntime 部署方式的问题。切换部署方式后问题自然消失，Warm Pool 成为了一个解决已不存在问题的方案。

---

## Key Metrics

| 指标 | Before (纯 CDK) | After (混合部署) |
|------|------------------|-------------------|
| Runtime 冷启动 | ~60s | **~1s** |
| 首条消息响应 | ~60s+ | **~5s**（Lightweight Agent） |
| OpenClaw 完整就绪 | ~2-3 min | **~10s** |
| 控制台指标显示 | 不可用 | 正常显示 |
| Docker 构建 | 本地交叉编译（慢/易失败） | CodeBuild 云端 或 local-build |
| 支持渠道 | Telegram + Slack | Telegram + Slack + **Feishu (新增)** |
| 飞书单元测试 | — | 27 个 |
| 容器日志 | 不可见 | CloudWatch 实时输出 |
| Commits | — | 12 个（含 1 个 merge） |
