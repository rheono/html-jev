import os
import time
from html.parser import HTMLParser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typesafe_sdk import AsyncTypeSafeClient

PRICE_PER_MTOK = 0.042
MOCK = os.environ.get("MOCK") == "1"
MAX_HTML_CHARS = 60_000

VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Stats(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tag_names = []
        self.stack = []
        self.max_depth = 0
        self.self_closing = 0
        self.unclosed = []

    def handle_starttag(self, tag, attrs):
        self.tag_names.append(tag)
        if tag in VOID_ELEMENTS:
            return
        self.stack.append(tag)
        self.max_depth = max(self.max_depth, len(self.stack))

    def handle_startendtag(self, tag, attrs):
        self.tag_names.append(tag)
        self.self_closing += 1

    def handle_endtag(self, tag):
        self.tag_names.append(tag)
        if tag in VOID_ELEMENTS:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.unclosed.append(self.stack.pop())
            if self.stack:
                self.stack.pop()
        else:
            self.unclosed.append(tag)


def html_stats(html: str) -> dict:
    p = _Stats()
    p.feed(html)
    p.close()
    p.unclosed.extend(p.stack)
    return {
        "tags": len(p.tag_names),
        "elements": len(set(p.tag_names)),
        "max_depth": p.max_depth,
        "self_closing": p.self_closing,
        "unclosed": p.unclosed,
        "tag_names": sorted(set(p.tag_names)),
    }


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


app = FastAPI(title="html-jev")
app.mount("/fonts", StaticFiles(directory=Path(__file__).parent / "static" / "fonts"), name="fonts")
app.mount("/examples", StaticFiles(directory=Path(__file__).parent / "static" / "examples"), name="examples")


class MatchRequest(BaseModel):
    html: str = Field(min_length=1, max_length=MAX_HTML_CHARS)
    pattern: str = Field(min_length=3, max_length=2_000)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/match")
async def match(req: MatchRequest):
    stats = html_stats(req.html)

    if MOCK:
        import re

        lowered = req.pattern.lower()
        hay = req.html.lower()
        hits = sum(1 for word in re.findall(r"[a-z]+", lowered) if len(word) > 3 and word in hay)
        verdict = min(0.99, 0.15 + 0.3 * hits) if stats["tags"] else 0.05
        answers = {
            "match": {"type": "noul", "noul": round(verdict, 4), "confidence": 0.9},
            "well_formed": {"type": "noul", "noul": 0.98 if not stats["unclosed"] else 0.05, "confidence": 0.95},
        }
        usage = {"input_tokens": max(1, len(req.html) // 4), "output_tokens": 0}
        model, latency_ms = "jev-mock", 1.2
    else:
        questions = {
            "match": {
                "type": "noul",
                "instructions": (
                    "You are a pattern matcher for HTML. Decide whether this HTML matches the pattern below. "
                    "Interpret the pattern literally. "
                    f"Pattern: {req.pattern}"
                ),
            },
            "well_formed": {
                "type": "noul",
                "instructions": "Is this HTML well-formed: every opening tag matched by a correct closing tag, proper nesting, no stray tags?",
            },
        }
        try:
            async with AsyncTypeSafeClient() as client:
                t0 = time.perf_counter()
                resp = await client.system_one(state={"html": req.html}, questions=questions)
                latency_ms = (time.perf_counter() - t0) * 1000
        except Exception as e:
            status = getattr(e, "status_code", None)
            raise HTTPException(
                status_code=status if status and 400 <= status < 600 else 502,
                detail=f"Jev call failed: {e}",
            ) from e
        answers = to_dict(resp.answers)
        usage = to_dict(resp.usage)
        model = resp.model

    input_tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
    return {
        "model": model,
        "match": answers["match"]["noul"],
        "answers": answers,
        "stats": stats,
        "usage": usage,
        "latency_ms": round(latency_ms, 1),
        "cost_usd": round(input_tokens * PRICE_PER_MTOK / 1_000_000, 8),
    }
