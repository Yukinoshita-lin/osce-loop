# OSCE-Loop 考试系统

OSCE-Loop 是一个用于客观结构化临床考试（OSCE）的全流程管理系统，支持考试编排、实时执行、AI 评分、审核仲裁和数据可视化等核心功能。

## 功能特性

### 考试管理
- **考试编排**：智能生成考生轮转schedule，支持特殊需求审批、设备冲突检测
- **实时执行**：考试现场管理，实时监控考站状态，支持异常标记和hint控制
- **数据采集**：支持音视频录制转录、笔录导入、自动语音识别（ASR）

### AI 评分
- **自动评分**：基于 LLM 的评分引擎，支持多维度证据提取和置信度评估
- **批量重评**：支持对历史考试进行批量重评分
- **评分缓存**：幂等性设计，避免重复调用节省成本

### 审核与质量
- **双盲审核**：支持双盲审核机制，仲裁分歧
- **临界失误追踪**：自动识别 AI 漏判的关键项目
- **指标监控**：多维度质量指标（G1-G3），异常自动告警

### 透明与合规
- **审计链**：区块链式审计链，支持篡改检测
- **AI 披露**：向考生公开 AI 参与程度和人工确认状态
- **数据留存**：按风险等级自动管理数据生命周期

## 技术架构

- **后端**：Python 3.11+ / FastAPI
- **数据库**：SQLite（含审计链）
- **AI**：支持多种评分 provider（Http / Mock）
- **前端**：原生 JavaScript + CSS

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 初始化数据库

```bash
python -c "from app.db import connect, init_db; conn = connect(); init_db(conn)"
```

### 启动服务

```bash
python run.py
```

服务默认在 `http://localhost:8000` 启动。

### 初始化演示数据

系统启动后会自动检查并初始化演示数据（用户、考站、评分标准等）。

## 项目结构

```
.
├── app/
│   ├── api.py          # FastAPI 路由定义
│   ├── config.py       # 配置管理
│   ├── db.py           # 数据库操作
│   ├── seed.py         # 演示数据生成
│   ├── ai/             # AI 评分相关
│   │   ├── asr.py      # 语音识别
│   │   ├── base.py     # 评分基类
│   │   ├── factory.py  # provider 工厂
│   │   ├── http_provider.py  # HTTP 评分客户端
│   │   ├── mock.py     # Mock 评分实现
│   │   └── pipeline.py # 评分管道
│   └── domain/         # 领域逻辑
│       ├── scheduling.py    # 排程算法
│       ├── execution.py     # 考试执行
│       ├── scoring.py       # 评分逻辑
│       ├── review.py        # 审核流程
│       └── ...
├── web/
│   ├── app.js     # 前端交互逻辑
│   ├── index.html # 页面模板
│   └── styles.css # 样式
├── tests/         # 测试用例
└── run.py         # 入口脚本
```

## 配置说明

主要通过环境变量配置：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URL` | 数据库路径 | `data/osce.db` |
| `SCORING_PROVIDER_URL` | 评分服务地址 | - |
| `SCORING_API_KEY` | 评分服务密钥 | - |
| `ASR_PROVIDER_URL` | ASR 服务地址 | - |
| `MEDIA_DIR` | 媒体文件目录 | `data/media` |

未配置 AI 服务时，系统使用内置 Mock Provider，便于开发和测试。

## 核心 API

- `POST /api/auth/login` - 用户登录
- `GET /api/bundles` - 获取考站列表
- `POST /api/bundles/draft` - 创建考试方案
- `POST /api/attempts/ingest` - 录入考试成绩
- `GET /api/review/queue` - 获取待审核队列
- `POST /api/execution/start` - 启动考试场次
- `GET /api/quality` - 质量报告
- `GET /api/governance/status` - 治理状态

## 许可

Apache-2.0 license