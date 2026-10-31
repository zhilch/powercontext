# 忘川客栈（Dify × PowerContext）

> OceanBase 2026 训练营生态赛题 · Dify 项目投稿
> 一个"记忆是一种资源"的互动剧情小游戏：客栈掌柜阿箬靠一本《记忆账本》记住你，账本没写的事，她真的不记得。

## 文件说明与导入顺序

| 顺序 | 文件 | 用途 |
| --- | --- | --- |
| 1 | `运行说明.md` | 先读这个：环境、版本、部署与导入步骤、已知限制 |
| 2 | `powercontext/Dockerfile`、`powercontext/docker-compose.powercontext.yaml` | 起 PowerContext 记忆服务，并接入 Dify 的 `docker_default` 网络 |
| 3 | `scripts/init_powercontext.sh` | 建 scope + 写入开局记忆，输出 `WANGCHUAN_SCOPE_ID` |
| 4 | `忘川客栈.yml` | Dify 应用 DSL（advanced-chat / Chatflow），导入时选择它 |
| 5 | `验证样例.md` | 4 组可复现样例（写入记忆、跨会话复用、不自造记忆、降级处理） |

## 一句话玩法

- `记住：我叫沈砚，怕水。` → 阿箬提笔记账，并复述给你。
- 换个新会话说 `掌柜的，我又来了。` → 她翻账本，主动提起"沈砚"和"怕水"。
- 问 `我上次托你办的事呢？` → 账本上没写，她会明确表示不记得，**不会编造**。

## 技术分工

- **Dify**：全部流程编排（判断、HTTP 调用、数据整理、模型生成、回答）。
- **PowerContext**：记忆账本（`/v1/memory/remember` 写入、`/v1/memory/search` 检索），容器化部署，SQLite 命名卷持久化。

题面要求 seekdb 或 PowerContext 至少一种，本作品使用 **PowerContext**；部署里 Dify 的向量库后端用 seekdb，
仅为满足 Dify 自身依赖，不承担作品数据职责。

## 运行环境摘要

Dify 1.18.0（docker compose 部署）+ PowerContext 1.2.0（容器）+ 任一 OpenAI 兼容对话模型。
完整步骤见 `运行说明.md`。
