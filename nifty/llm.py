"""OpenRouter and Anthropic client with an on-disk response cache, a cost ledger and a budget stop."""
import base64
import hashlib
import json
import threading
import time
from pathlib import Path

import anthropic
import httpx

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
OUTPUT = ROOT / "output"
CACHE = WORK / "cache"
LEDGER = WORK / "ledger.jsonl"
API = "https://openrouter.ai/api/v1/chat/completions"

# Finish reasons meaning the text is absent or cut short and must not be used.
BAD_FINISH = {"length", "content_filter", "recitation", "safety", "error"}
# Dollars per million tokens: input, cache read, output.
DIRECT_PRICES = {
    "claude-fable-5-1": (10.0, 0.25, 50.0),
    "claude-opus-5-5": (4.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 0.20, 10.0),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
}
# Haiku's whole rate card is five times higher once a prompt passes 100K tokens.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, 5.0)}
SERVER_FALLBACK = {"claude-opus-5-5"}


class LLMError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


def env_value(*names: str) -> str | None:
    # The project's own .env wins, so a key exported in the shell cannot be billed by accident.
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() in names and value.strip():
            return value.strip().strip("\"'")
    return None


def image_part(path: Path) -> dict:
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


class Client:
    def __init__(self, max_cost_usd: float = 25.0, project: str = ""):
        self.max_cost = max_cost_usd
        self.project = project
        self.spent = 0.0
        self.lock = threading.Lock()
        self.anthropic = None
        self.http = None
        CACHE.mkdir(parents=True, exist_ok=True)

    def chat(self, model: str, system: str, content: list | str, *, label: str,
             max_tokens: int = 16000, json_mode: bool = False, reasoning: str | None = None,
             effort: str = "high") -> dict:
        """Returns {text, cost, model, cached}. Raises LLMError if no complete answer came back."""
        if "/" not in model:
            return self.chat_direct(model, system, content, label=label, max_tokens=max_tokens, effort=effort)
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "usage": {"include": True},
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if reasoning:
            body["reasoning"] = {"effort": reasoning}
        key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        cached = CACHE / f"{key}.json"
        if cached.exists():
            return {**json.loads(cached.read_text()), "cached": True}

        with self.lock:
            if self.spent >= self.max_cost:
                raise BudgetExceeded(f"spent ${self.spent:.2f} of ${self.max_cost:.2f}")
            if self.http is None:
                secret = env_value("OPENROUTER_KEY", "OPENROUTER_API_KEY")
                if not secret:
                    raise LLMError("OPENROUTER_API_KEY not found in .env")
                self.http = httpx.Client(
                    timeout=httpx.Timeout(600, connect=30),
                    headers={"Authorization": f"Bearer {secret}", "X-Title": "Nifty Translator"},
                )

        data = self._post(body, label)
        choice = data["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        finish = (choice.get("native_finish_reason") or choice.get("finish_reason") or "").lower()
        usage = data.get("usage") or {}
        cost = float(usage.get("cost") or 0)
        with self.lock:
            self.spent += cost
            with LEDGER.open("a") as f:
                f.write(json.dumps({
                    "t": time.strftime("%FT%T"), "project": self.project, "label": label, "model": model,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cost": cost, "finish": finish,
                }) + "\n")
        if not text or finish in BAD_FINISH:
            raise LLMError(f"{model} on {label}: finish={finish!r}, {len(text)} chars")

        result = {"text": text, "cost": cost, "model": model, "finish": finish}
        cached.write_text(json.dumps(result, ensure_ascii=False))
        return {**result, "cached": False}

    def chat_direct(self, model: str, system: str, content: str, *, label: str, max_tokens: int, effort: str) -> dict:
        """A model named without a vendor prefix goes straight to Anthropic, on CLAUDE_API_KEY.
        The same cache, ledger and budget stop apply. Thinking stays at each model's default."""
        if model not in DIRECT_PRICES:
            raise LLMError(f"no price known for {model}; add it to DIRECT_PRICES in nifty/llm.py")
        body = {"model": model, "system": system, "content": content, "max_tokens": max_tokens, "effort": effort}
        key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        cached = CACHE / f"{key}.json"
        if cached.exists():
            return {**json.loads(cached.read_text()), "cached": True}
        with self.lock:
            if self.spent >= self.max_cost:
                raise BudgetExceeded(f"spent ${self.spent:.2f} of ${self.max_cost:.2f}")
            if self.anthropic is None:
                secret = env_value("CLAUDE_API_KEY")
                if not secret:
                    raise LLMError("CLAUDE_API_KEY not found in .env")
                self.anthropic = anthropic.Anthropic(api_key=secret, max_retries=4)
        fallback = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in SERVER_FALLBACK else {}
        try:
            with self.anthropic.beta.messages.stream(
                model=model,
                max_tokens=max_tokens,
                output_config={"effort": effort},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                **fallback,
            ) as stream:
                message = stream.get_final_message()
        except anthropic.APIStatusError as e:
            # No credit is not a reason to try again, and the status says so.
            if e.status_code in (400, 402) and "credit" in e.message.lower():
                raise BudgetExceeded(f"Anthropic refused for lack of credit: {e.message}") from e
            raise LLMError(f"{model} on {label}: HTTP {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"{model} on {label}: {e}") from e
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        usage = message.usage
        read, write = usage.cache_read_input_tokens or 0, usage.cache_creation_input_tokens or 0
        price_in, price_read, price_out = DIRECT_PRICES[model]
        cost = (usage.input_tokens * price_in + read * price_read + write * price_in * 1.25
                + usage.output_tokens * price_out) / 1e6
        limit, multiple = LONG_PROMPT.get(model, (None, 1.0))
        if limit and usage.input_tokens + read + write > limit:
            cost *= multiple
        with self.lock:
            self.spent += cost
            with LEDGER.open("a") as f:
                f.write(json.dumps({
                    "t": time.strftime("%FT%T"), "project": self.project, "label": label, "model": model,
                    "prompt_tokens": usage.input_tokens + read + write, "cache_read": read,
                    "completion_tokens": usage.output_tokens, "cost": cost, "finish": message.stop_reason,
                }) + "\n")
        if not text or message.stop_reason != "end_turn":
            raise LLMError(f"{model} on {label}: stop={message.stop_reason!r}, {len(text)} chars")
        result = {"text": text, "cost": cost, "model": model, "finish": message.stop_reason}
        cached.write_text(json.dumps(result, ensure_ascii=False))
        return {**result, "cached": False}

    def _post(self, body: dict, label: str) -> dict:
        error = "no attempt made"
        for attempt in range(5):
            if attempt:
                time.sleep(2 ** attempt * 3)
            try:
                r = self.http.post(API, json=body)
            except httpx.HTTPError as e:
                error = repr(e)
                continue
            if r.status_code == 402:
                # Out of money: no other model can help, so stop rather than fall back.
                raise BudgetExceeded(f"OpenRouter refused for lack of credit: {r.text[:300]}")
            if r.status_code in (408, 429) or r.status_code >= 500:
                error = f"HTTP {r.status_code}: {r.text[:300]}"
                continue
            if r.status_code != 200:
                raise LLMError(f"{body['model']} on {label}: HTTP {r.status_code}: {r.text[:500]}")
            data = r.json()
            if "error" in data or not data.get("choices"):
                error = str(data.get("error") or data)[:300]
                continue
            return data
        raise LLMError(f"{body['model']} on {label}: gave up after retries: {error}")

    def chat_with_fallback(self, models: list[str], system: str, content, **kw) -> dict:
        """Tries each model in turn, so a block from one vendor does not lose the page."""
        last = None
        for model in models:
            try:
                return self.chat(model, system, content, **kw)
            except LLMError as e:
                print(f"  ! {e}")
                last = e
        raise last
