"""温故 · 复习教练 —— 后端服务。

设计要点：
1. **所有学习记录都存在 PowerContext 里**，本地不落库。换一台机器、换一个浏览器，
   只要 scope 还在，复习记录就在。每条知识点是一条 Memory，格式见 memory.py。
2. 出题与评分交给大模型（DeepSeek，OpenAI 兼容接口），但**掌握度只由评分决定**，
   模型不直接改分数，避免"模型说了算"。
3. 复习顺序按「遗忘曲线 + 上次得分」算，得分低的先复习、拖得久的先复习。
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

POWERCONTEXT_BASE_URL = os.environ.get("POWERCONTEXT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
POWERCONTEXT_SCOPE_ID = os.environ.get("POWERCONTEXT_SCOPE_ID", "")
POWERCONTEXT_TOKEN = os.environ.get("POWERCONTEXT_TOKEN", "")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

STATIC_DIR = Path(__file__).parent / "static"
# 分值 → 建议间隔（天）。得分越低，越早复习。
REVIEW_INTERVALS = {5: 7.0, 4: 4.0, 3: 2.0, 2: 1.0, 1: 0.5, 0: 0.25}

app = FastAPI(title="温故 · 复习教练", version="1.0.0")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
class KnowledgePoint(BaseModel):
    id: str
    subject: str
    topic: str
    score: int
    reviews: int
    last_reviewed: str
    due_in_days: float
    note: str
    created_at: str
    # 同一条知识点的先后顺序靠它判断（同一天多次复习也能区分）
    ts: int = 0


class CreateKnowledgePoint(BaseModel):
    subject: str = Field(min_length=1, max_length=40)
    topic: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=300)


class QuizRequest(BaseModel):
    id: str


class Quiz(BaseModel):
    id: str
    subject: str
    topic: str
    question: str
    reference: str
    hint: str


class GradeRequest(BaseModel):
    id: str
    question: str = Field(min_length=1, max_length=4000)
    reference: str = Field(default="", max_length=4000)
    answer: str = Field(min_length=1, max_length=8000)


class Grade(BaseModel):
    score: int
    feedback: str
    error_note: str
    next_review: str
    kp: KnowledgePoint


# --------------------------------------------------------------------------- #
# PowerContext：记忆即数据库
# --------------------------------------------------------------------------- #
@dataclass
class MemoryEntry:
    text: str
    citation: dict


def _pc_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if POWERCONTEXT_TOKEN:
        headers["Authorization"] = f"Bearer {POWERCONTEXT_TOKEN}"
    return headers


async def pc_post(path: str, payload: dict) -> dict:
    if not POWERCONTEXT_SCOPE_ID:
        raise HTTPException(status_code=503, detail="POWERCONTEXT_SCOPE_ID 未配置")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{POWERCONTEXT_BASE_URL}{path}", json=payload, headers=_pc_headers())
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"PowerContext 调用失败：{response.text[:300]}")
    return response.json()


def encode_kp(kp: KnowledgePoint) -> str:
    """把一条知识点压成一行 Memory 文本，读取时再解析回来。"""
    fields = {
        "id": kp.id,
        "subject": kp.subject,
        "topic": kp.topic,
        "score": str(kp.score),
        "reviews": str(kp.reviews),
        "last": kp.last_reviewed,
        "note": kp.note.replace("|", "／").replace("\n", " "),
        "created": kp.created_at,
        "ts": str(kp.ts or int(time.time() * 1000)),
    }
    return "KP|" + "|".join(f"{key}={value}" for key, value in fields.items())


def decode_kp(text: str) -> KnowledgePoint | None:
    if not text.startswith("KP|"):
        return None
    fields: dict[str, str] = {}
    for chunk in text.split("|")[1:]:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        fields[key.strip()] = value.strip()
    try:
        kp = KnowledgePoint(
            id=fields["id"],
            subject=fields.get("subject", ""),
            topic=fields.get("topic", ""),
            score=int(fields.get("score", "0")),
            reviews=int(fields.get("reviews", "0")),
            last_reviewed=fields.get("last", ""),
            due_in_days=0.0,
            note=fields.get("note", ""),
            created_at=fields.get("created", ""),
            ts=int(fields.get("ts", "0") or 0),
        )
    except (KeyError, ValueError):
        return None
    kp.due_in_days = due_in_days(kp)
    return kp


def due_in_days(kp: KnowledgePoint) -> float:
    """距离下次复习还有多久（天）。负数表示已过期，适合复习。"""
    interval = REVIEW_INTERVALS.get(max(0, min(5, kp.score)), 1.0)
    if not kp.last_reviewed:
        return -interval
    try:
        last = time.mktime(time.strptime(kp.last_reviewed, "%Y-%m-%d"))
    except ValueError:
        return -interval
    elapsed = (time.time() - last) / 86400.0
    return round(interval - elapsed, 2)


async def load_latest() -> dict[str, KnowledgePoint]:
    """读取整本账本，按知识点取最新一条记录（同一条会被反复更新）。"""
    payload = await pc_post("/v1/memory/entries/list", {"scope_id": POWERCONTEXT_SCOPE_ID})
    latest: dict[str, KnowledgePoint] = {}
    for entry in payload.get("entries", []):
        kp = decode_kp(entry.get("text", ""))
        if kp is None:
            continue
        current = latest.get(kp.id)
        # 按写入时间戳取最新一条：同一天复习多次也能正确覆盖
        if current is None or kp.ts >= current.ts:
            latest[kp.id] = kp
    return latest


async def save_kp(kp: KnowledgePoint) -> None:
    kp.ts = int(time.time() * 1000)
    await pc_post(
        "/v1/memory/remember",
        {
            "scope_id": POWERCONTEXT_SCOPE_ID,
            "kind": "review",
            "text": encode_kp(kp),
            "reason": f"{kp.subject} · {kp.topic} 的复习记录",
        },
    )


# --------------------------------------------------------------------------- #
# DeepSeek：出题与评分
# --------------------------------------------------------------------------- #
async def ask_model(system: str, user: str, max_tokens: int = 900) -> str:
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=503, detail="DEEPSEEK_API_KEY 未配置")
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.6,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"模型调用失败：{response.text[:300]}")
    data = response.json()
    return data["choices"][0]["message"].get("content") or ""


def _as_text(value: object) -> str:
    """模型有时把要点写成数组，统一成用「；」分隔的一段文字。"""
    if isinstance(value, (list, tuple)):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def format_due(days: float) -> str:
    if days <= 0:
        return "现在就该复习"
    return f"{days:.1f} 天后"


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text else text
        text = text.lstrip("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise HTTPException(status_code=502, detail=f"模型没有返回 JSON：{text[:200]}")
    import json

    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=502, detail=f"模型返回的 JSON 无法解析：{error}") from error


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health() -> dict:
    status = "ok"
    detail = ""
    try:
        await pc_post("/v1/memory/entries/list", {"scope_id": POWERCONTEXT_SCOPE_ID})
    except HTTPException as error:
        status, detail = "degraded", str(error.detail)
    return {"status": status, "detail": detail, "model": DEEPSEEK_MODEL, "scope": POWERCONTEXT_SCOPE_ID}


@app.post("/api/kp", response_model=KnowledgePoint)
async def create_kp(body: CreateKnowledgePoint) -> KnowledgePoint:
    kp = KnowledgePoint(
        id="kp_" + secrets.token_hex(4),
        subject=body.subject.strip(),
        topic=body.topic.strip(),
        score=0,
        reviews=0,
        last_reviewed="",
        due_in_days=0.0,
        note=body.note.strip(),
        created_at=time.strftime("%Y-%m-%d"),
    )
    kp.due_in_days = due_in_days(kp)
    await save_kp(kp)
    return kp


@app.get("/api/kp", response_model=list[KnowledgePoint])
async def list_kp() -> list[KnowledgePoint]:
    latest = await load_latest()
    return sorted(latest.values(), key=lambda item: (item.due_in_days, item.score, item.topic))


@app.get("/api/plan", response_model=list[KnowledgePoint])
async def plan(limit: int = 5) -> list[KnowledgePoint]:
    latest = await load_latest()
    ordered = sorted(latest.values(), key=lambda item: (item.due_in_days, item.score))
    return [kp for kp in ordered if kp.due_in_days <= 0][:limit] or ordered[:limit]


@app.post("/api/quiz", response_model=Quiz)
async def create_quiz(body: QuizRequest) -> Quiz:
    latest = await load_latest()
    kp = latest.get(body.id)
    if kp is None:
        raise HTTPException(status_code=404, detail="没有这个知识点")
    system = (
        "你是一位严格的复习教练，负责给大学生出简答题。"
        "只输出 JSON，字段为 question（题干，不超过 80 字）、reference（参考答案要点，3 条以内）、hint（一句提示）。"
        "题目要具体、可判分，不要出选择题。"
    )
    user = (
        f"学科：{kp.subject}\n知识点：{kp.topic}\n"
        f"历史薄弱点：{kp.note or '暂无'}\n已复习 {kp.reviews} 次，最近得分 {kp.score}/5。\n"
        "请出一道考查该知识点的简答题。"
    )
    data = parse_json(await ask_model(system, user))
    return Quiz(
        id=kp.id,
        subject=kp.subject,
        topic=kp.topic,
        question=_as_text(data.get("question")),
        reference=_as_text(data.get("reference")),
        hint=_as_text(data.get("hint")),
    )


@app.post("/api/grade", response_model=Grade)
async def grade(body: GradeRequest) -> Grade:
    latest = await load_latest()
    kp = latest.get(body.id)
    if kp is None:
        raise HTTPException(status_code=404, detail="没有这个知识点")
    system = (
        "你是严格的复习教练，按参考答案要点给学生的简答题打 0-5 分。"
        "只输出 JSON：score（整数 0-5）、feedback（两句话点评，指出对在哪里、缺什么）、"
        "error_note（一句话记下这次的薄弱点，便于下次复习，不超过 40 字）。"
        "完全没答到要点给 0 分，答出一半给 2-3 分，完整准确给 4-5 分。"
    )
    user = (
        f"学科：{kp.subject}\n知识点：{kp.topic}\n题目：{body.question}\n"
        f"参考答案要点：{body.reference or '（无）'}\n学生答案：{body.answer}"
    )
    data = parse_json(await ask_model(system, user, max_tokens=600))
    try:
        score = max(0, min(5, int(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    kp.score = score
    kp.reviews += 1
    kp.last_reviewed = time.strftime("%Y-%m-%d")
    kp.note = str(data.get("error_note", ""))[:120].replace("|", "／")
    kp.due_in_days = due_in_days(kp)
    await save_kp(kp)
    return Grade(
        score=score,
        feedback=str(data.get("feedback", "")),
        error_note=kp.note,
        next_review=format_due(kp.due_in_days),
        kp=kp,
    )


@app.get("/api/stats")
async def stats() -> dict:
    latest = await load_latest()
    items = list(latest.values())
    by_subject: dict[str, dict] = {}
    for kp in items:
        bucket = by_subject.setdefault(kp.subject, {"count": 0, "score_sum": 0, "weak": 0})
        bucket["count"] += 1
        bucket["score_sum"] += kp.score
        if kp.score <= 2:
            bucket["weak"] += 1
    for bucket in by_subject.values():
        bucket["avg_score"] = round(bucket["score_sum"] / bucket["count"], 2) if bucket["count"] else 0
    return {
        "total": len(items),
        "due_today": len([kp for kp in items if kp.due_in_days <= 0]),
        "avg_score": round(sum(kp.score for kp in items) / len(items), 2) if items else 0,
        "reviews": sum(kp.reviews for kp in items),
        "by_subject": by_subject,
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
