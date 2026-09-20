import hashlib
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typesafe_sdk import AsyncTypeSafeClient

PRICE_PER_MTOK = 0.042
MOCK = os.environ.get("MOCK") == "1"

DEFAULT_QUESTIONS = {
    "well_formed": {
        "type": "noul",
        "instructions": "Is this HTML well-formed: every opening tag matched by a correct closing tag, proper nesting, no stray tags?",
    },
    "self_closing": {
        "type": "noul",
        "instructions": "Does this HTML contain any self-contained self-closing tags (e.g. <br/>, <img ... />, <hr/>)?",
    },
    "is_html": {
        "type": "noul",
        "instructions": "Is this input HTML markup rather than plain text?",
    },
    "size": {
        "type": "choice",
        "instructions": "How large is the document?",
        "criteria": {
            "snippet": "a few tags, under ~20 elements",
            "section": "a page section, ~20-100 elements",
            "page": "a full HTML document with head and body, ~100+ elements",
        },
    },
    "nesting": {
        "type": "score",
        "instructions": "How deeply nested are the elements?",
        "criteria": ["flat, 1-2 levels", "shallow, 3-5 levels", "deep, 6-10 levels", "extreme, 10+ levels"],
    },
}

VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def mock_answers(html: str):
    tags = re.findall(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)", html)
    self_closing = bool(re.search(r"<[a-zA-Z][^>]*?/>", html))
    balanced, stack = True, []
    depth = max_depth = 0
    for close, name in tags:
        name = name.lower()
        if name in VOID_ELEMENTS:
            continue
        if close:
            if not stack or stack.pop() != name:
                balanced = False
            depth -= 1
        else:
            stack.append(name)
            depth += 1
            max_depth = max(max_depth, depth)
    balanced = balanced and not stack
    n = len(tags)
    size = "snippet" if n < 20 else "section" if n < 100 else "page"
    nesting = 0 if max_depth <= 2 else 1 if max_depth <= 5 else 2 if max_depth <= 10 else 3
    legend = {str(i): c for i, c in enumerate(DEFAULT_QUESTIONS["nesting"]["criteria"])}
    seed = int(hashlib.sha256(html.encode()).hexdigest()[:8], 16)
    jitter = (seed % 7) / 100
    conf = min(1.0, round(0.95 + jitter, 4))

    def probs(dist, chosen):
        rest = round((1 - dist[chosen]) / (len(dist) - 1), 4)
        return {k: (round(dist[chosen], 4) if k == chosen else rest) for k in dist}

    return {
        "well_formed": {"type": "noul", "noul": 0.98 if balanced else 0.06, "confidence": conf},
        "self_closing": {"type": "noul", "noul": 0.97 if self_closing else 0.05, "confidence": conf},
        "is_html": {"type": "noul", "noul": 0.99 if n > 3 else 0.04, "confidence": 1.0},
        "size": {
            "type": "choice",
            "choice": size,
            "confidence": conf,
            "probabilities": probs({"snippet": 0.55, "section": 0.7, "page": 0.85}, size),
        },
        "nesting": {
            "type": "score",
            "score": float(nesting),
            "confidence": conf,
            "legend": legend,
            "probabilities": {k: (1.0 if int(k) == nesting else 0.0) for k in legend},
        },
    }


app = FastAPI(title="html-jev")


class AskRequest(BaseModel):
    html: str = Field(min_length=1, max_length=200_000)
    questions: dict | None = None


def to_dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: to_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_dict(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: to_dict(v) for k, v in vars(value).items() if not k.startswith("_")}
    return value


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/ask")
async def ask(req: AskRequest):
    if MOCK:
        return {
            "model": "jev-mock",
            "answers": mock_answers(req.html),
            "usage": {"input_tokens": max(1, len(req.html) // 4), "output_tokens": 0},
            "latency_ms": 1.2,
            "cost_usd": 0.0,
        }
    try:
        async with AsyncTypeSafeClient() as client:
            t0 = time.perf_counter()
            resp = await client.system_one(
                state={"html": req.html},
                questions=req.questions or DEFAULT_QUESTIONS,
            )
            latency_ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        status = getattr(e, "status_code", None)
        raise HTTPException(
            status_code=status if status and 400 <= status < 600 else 502,
            detail=f"Jev call failed: {e}",
        ) from e

    usage = to_dict(resp.usage)
    input_tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
    return {
        "model": resp.model,
        "answers": to_dict(resp.answers),
        "usage": usage,
        "latency_ms": round(latency_ms, 1),
        "cost_usd": round(input_tokens * PRICE_PER_MTOK / 1_000_000, 8),
    }
