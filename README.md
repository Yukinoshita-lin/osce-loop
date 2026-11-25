# OSCE·Loop

面向客观结构化临床考试（OSCE）全流程的 AI 辅助系统。设计目标不是让 AI 替代考官，而是把 AI 放在**可验证、可回退、可审计**的位置：AI 负责证据提取与初评建议，人保留签字、裁决与终止权。

完整设计说明见 [design/AI-OSCE-全流程设计方案.html](design/AI-OSCE-全流程设计方案.html)。

## 核心能力

- **评分表即协议**：rubric 行为锚、证据形态、对话阶段与 AI 权限四元组驱动 SP 信息槽、检查点、证据探针与反馈模板的自动派生。
- **证据锚定评分**：每条 AI 建议必须携带时间戳证据；无证据不写入，证据缺口自动升级人工全量复核。
- **风险路由**：L0–L3 分级授权，关键项、低置信、边缘案例、申诉等场景按具名原因强制送审。
- **SP 人格一致性闭环**：确定性状态机控制"说什么"，LLM 只负责"怎么说"，独立验证器按人格保真 / 信息边界 / 临床合理三项判据放行，失败回退安全模板。
- **闸门式放行**：G0 离线副驾 → G1 练习沙箱 → G2 模拟考 → G3 有限高利害，指标未达标不放行；版本变更自动降权。
- **治理与追溯**：哈希链审计、外部锚点、签字链、数据流向年度复核、内容审计、阈值本地校准、模型卡与数据表公开。
- **公平性与体验**：亚组分数差、编辑率差异、反馈可及性监测，SUS、实用性、真实感与申诉/改判率纳入验收。
- **S9 度量看板**：21 项验收指标自动判定，未采集不冒充达标，支持 CSV 导出。

## 快速开始

要求 Python 3.11+（开发与验证使用 Python 3.14）。

```bash
pip install -r requirements.txt
python run.py
```

打开 http://127.0.0.1:8000 。首次启动会自动初始化数据库、演示站包与演示账号。

### 演示账号

| 用户名 | 密码 | 角色 |
| --- | --- | --- |
| `admin` | `admin123` | 系统管理员 |
| `exam` | `exam123` | 考务管理者 |
| `reviewer` | `review123` | 考官 / 评分员 |
| `auditor` | `audit123` | 审计员 |
| `student` | `student123` | 学生 |

> 演示账号仅用于本地体验，部署前必须修改或移除。

## 配置

通过环境变量配置，常用项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OSCELOOP_HOST` / `OSCELOOP_PORT` | `127.0.0.1` / `8000` | 监听地址与端口 |
| `OSCELOOP_DB` | `data/osceloop.db` | SQLite 数据库路径 |
| `OSCELOOP_RELEASE_STAGE` | `G0_offline` | 当前闸门阶段 |
| `OSCELOOP_AI_PROVIDER` | `mock` | 评分模型提供方：`mock` / `http` |
| `OSCELOOP_AI_BASE_URL` | 空 | OpenAI 兼容接口地址 |
| `OSCELOOP_AI_API_KEY` | 空 | 模型接口密钥 |
| `OSCELOOP_ASR_PROVIDER` | `off` | 转写提供方：`off` / `http` |
| `OSCELOOP_ASR_BASE_URL` | 空 | OpenAI 兼容 `/audio/transcriptions` 地址 |
| `OSCELOOP_AI_ALLOW_EXTERNAL` | `0` | 是否允许评分端点出内网 |
| `OSCELOOP_ASR_ALLOW_EXTERNAL` | `0` | 是否允许 ASR 端点出内网 |
| `OSCELOOP_TRUSTED_AI_HOSTS` | 空 | 额外信任的端点域名，逗号分隔 |
| `OSCELOOP_COOKIE_SECURE` | `0` | HTTPS 部署时设为 `1` |

涉及真实考生音视频的 L2–L3 场景，应将模型与 ASR 部署在院内/校内网络，并完成数据流向复核。

## 项目结构

```text
app/
  api.py            FastAPI 路由与鉴权中间件
  db.py             SQLite 表结构与审计链
  config.py         环境变量与阈值配置
  domain/           业务域：评分、闸门、SP、编排、治理、审计等
  ai/               能力网关、评分/ASR 提供方与调用流水
web/                原生 HTML/CSS/JS 单页工作台
design/             全流程设计方案
tests/              pytest 测试
run.py              本地启动入口
```

## 测试

```bash
python -m pytest tests -q
```

当前全量测试 **418 passed**，覆盖评分路由、证据完整性、SP 红队与认证、闸门、审计链、公平性、成本、S9 指标与治理闭环。

## 打包与发布

发布包为源码压缩包，不包含 `data/` 运行时数据与本地缓存。构建示例：

```powershell
Compress-Archive -Path app,design,tests,web,run.py,requirements.txt,LICENSE,README.md,.gitignore -DestinationPath dist/osce-loop-v0.1.0.zip -Force
```

## 安全说明

- 所有治理写操作均写入审计事件；关键操作需要角色权限。
- 默认拒绝外部 AI / ASR 端点，必须显式放行并记录数据流向复核。
- 内容审计发现的高风险表述会进入 P0 处置、红队回归用例与动态禁用词。
- 发现安全问题时请勿公开提交 Issue，先联系维护者。

## 许可证

[Apache License 2.0](LICENSE)
