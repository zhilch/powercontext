# 温故 · 复习教练

> 用 Qoder 开发、以 **PowerContext** 作为记忆账本的复习工具。
> 你把自己学过的知识点录进去，它会出题考你、按答案打分，并**记住你哪里薄弱、什么时候该再复习一次**。

## 解决的问题

复习最大的问题不是"没时间"，而是**不知道自己哪里真的不会**，以及**上次错在哪已经忘了**。
市面上的题库 App 都是"刷题 + 正确率"，但它们不记得你的具体错因，也不会告诉你"今天该复习哪三条"。

温故做的是：

1. 你录入知识点（学科 + 一句话描述 + 你最容易错的地方）；
2. 它按遗忘曲线挑出今天该复习的，逐条出一道简答题；
3. 你写答案，它按要点打分并记下这次暴露的薄弱点；
4. 掌握度低的更早复习、掌握度高的往后推 —— 而且**这些记录存在 PowerContext 里，换个浏览器、换台机器都还在**。

## 核心功能

| 功能 | 说明 |
| --- | --- |
| 知识点录入 | 学科 / 知识点 / 你的易错点，一次录入即进入复习队列 |
| 智能出题 | 按知识点和历史薄弱点让模型出简答题，不是固定题池 |
| 要点打分 | 0–5 分，给出"对在哪、缺什么"，并提炼一句话错因 |
| 遗忘曲线排程 | 5 分 → 7 天后；4 分 → 4 天；3 分 → 2 天；2 分 → 1 天；0–1 分 → 当天/半天后再来 |
| 跨会话记忆 | 所有学习记录存在 PowerContext，重启应用、换设备都不丢 |
| 学习统计 | 总量、今日到期、平均掌握度、按学科汇总薄弱项 |

## 技术分工

| 组成 | 在项目中的作用 |
| --- | --- |
| **Qoder** | 项目主要开发工作与网站发布（Qoder Sites），开发记录见 `Qoder开发记录.md` |
| **PowerContext** | 记忆账本：知识点、掌握度、复习次数、错因、下次复习时间全部以 Memory 形式存在这里；应用本身**不落库** |
| 大模型（DeepSeek，OpenAI 兼容） | 只负责出题与评分，掌握度由评分结果决定，模型不直接改分数 |

为什么用 PowerContext 而不是自建数据库：这个项目的核心资产就是"关于你的学习记录"，
它天然属于"跨会话、跨工具都要能带走的上下文"。PowerContext 正好提供 scope + Memory + 精确 revision，
应用侧只需要两个 HTTP 调用（写一条记忆、读整本账本）。

## 目录结构

```
qoder/
├── app/
│   ├── main.py              # FastAPI：知识点 CRUD、出题、评分、复习计划、统计
│   └── static/index.html    # 单页前端（无构建步骤）
├── powercontext/Dockerfile  # 记忆服务镜像（PowerContext 1.2.0）
├── scripts/init_scope.sh    # 建 scope + 写 3 条示例知识点
├── Dockerfile               # 应用镜像
├── docker-compose.yaml      # 一键起 powercontext + app
├── requirements.txt
├── .env.example
├── 运行说明.md
├── 验证样例.md
└── Qoder开发记录.md
```

## 快速开始

```bash
cp .env.example .env          # 填 DEEPSEEK_API_KEY
docker compose up -d --build  # 先起 powercontext
bash scripts/init_scope.sh    # 建账本，拿到 scope_id
# 把 scope_id 填进 .env 的 WENGGU_SCOPE_ID，再
docker compose up -d app
# 打开 http://localhost:8080
```

完整步骤（含本地不用 Docker 的跑法）见 `运行说明.md`。

## 验证样例

见 `验证样例.md`：录入 → 出题 → 弱答案 1 分 → 强答案 4 分 → 复习时间从"现在"推到 3.5 天后。

## 提交材料索引

| 材料 | 位置 |
| --- | --- |
| 项目网站 | Qoder Sites 发布的公网地址（见提交平台） |
| 代码仓库 | 本仓库 `qoder/` 目录 |
| 运行说明 | `运行说明.md` |
| 验证样例 | `验证样例.md` |
| Qoder 开发记录 | `Qoder开发记录.md`（含 Qoder 账号 UID） |
