"""Headless Duck.ai chat client (real Chrome + UI-driven, no IP rotation).

WHY THIS DESIGN (root-cause analysis, not guesswork):
  The same machine/IP that the user browses Duck.ai with manually works fine,
  but our Playwright relay got 418 ERR_BN_LIMIT. Two findings from js-reverse:

  1. Playwright's *bundled Chromium* is fingerprinted (TLS/HTTP2/JA3 + UA carries
     "HeadlessChrome") and is hard-banned with ERR_BN_LIMIT on the FIRST request.
     Real Chrome (`channel="chrome"`) passes that layer and instead gets
     ERR_CHALLENGE - i.e. the server is willing to talk, just wants a valid
     client-generated challenge answer.

  2. Calling fetch('/duckchat/v1/chat') by hand (bypassing the app) yields
     ERR_CHALLENGE because x-fe-signals / x-vqd-hash-1 are only produced by the
     app's OWN send logic (real interaction telemetry + challenge JS). Driving
     the real UI - type into the textarea, trigger send - lets the app generate
     the full, valid request. That path returns the real answer ("PONG" in test).

  => The fix is NOT a new IP. It is: real Chrome + drive the app's own send,
     then read the assistant reply from the intercepted chat response.

  We tee the /duckchat/v1/chat RESPONSE (SSE) with a clone().body.getReader() so
  we receive it token-by-token as it streams (the app still reads the original).
  Each SSE line looks like `data: {"action":"success","message":"<token>",...}`
  and ends with `data: [DONE]`. Parsing on the wire is far more robust than
  scraping the DOM, and lets us expose true incremental streaming upstream.

SESSION REUSE (2026-09): the app natively sends structured multi-turn
  `messages[]` and keeps conversation context in the page. When a follow-up
  request's flattened prompt exactly EXTENDS the previous one, we type only the
  new turn into the same live page (no re-warm, no re-sending history). Anything
  else falls back to a fresh page with the full flattened prompt. Either way the
  model sees the whole conversation.

BODY REWRITE (experimental): the hook may patch the app's own outgoing chat
  body (reasoningEffort / toolChoice / canUseTools) before send. If the server
  binds its challenge to the body (ERR_CHALLENGE), we auto-disable rewriting for
  the process lifetime and retry unmodified.

Model catalog (live /duckchat/v1/models snapshot 2026-09-19) + alias map follow.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import urllib.request
from typing import AsyncIterator, List, Optional

from playwright.async_api import async_playwright

logger = logging.getLogger("duckai")

BASE = os.getenv("DUCKAI_BASE", "https://duck.ai")


def _default_chrome_path() -> str:
    """Chrome binary per platform. Playwright's bundled Chromium is fingerprinted
    and hard-banned by Duck.ai, so we always drive a REAL Chrome install."""
    if os.name == "nt":
        return r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    # Linux (containers incl. Railway/Docker): the google-chrome-stable package.
    for cand in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if os.path.exists(cand):
            return cand
    return "/usr/bin/google-chrome"


# Use the system Chrome, not Playwright's bundled Chromium (fingerprint reasons above).
CHROME_PATH = os.getenv("DUCKAI_CHROME_PATH", "") or _default_chrome_path()
# A real desktop Chrome UA (no "HeadlessChrome" marker). Version pinned to a common
# stable release; must NOT contain "HeadlessChrome".
REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

# Static fallback catalog (snapshot 2026-08-27). The live list comes from
# fetch_model_catalog(); /v1/models and access checks prefer the live data.
MODEL_LABELS = {
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.4-mini": "GPT-5.4 mini",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "mistral-small-2603": "Mistral Small 4",
    "tinfoil/gpt-oss-120b": "gpt-oss 120B",
    "tinfoil/gemma4-31b": "Gemma 4 31B",
}
DEFAULT_MODEL = "gpt-5.6-luna"

MODEL_ALIASES = {
    "gpt-5.6": "gpt-5.6-luna",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.6-sol": "gpt-5.6-sol",
    # `gpt-5.4` left the catalog on 2026-09-18; the mini is its surviving sibling.
    "gpt-5.4": "gpt-5.4-mini",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.5": "gpt-5.4-mini",
    "gpt-5": "gpt-5.4-mini",
    "gpt-4o": "gpt-5.4-mini",
    "gpt-4o-mini": "gpt-5.4-mini",
    "o3-mini": "gpt-5.4-mini",
    "claude-3-5-sonnet": "claude-sonnet-4-6",
    "claude-3.7-sonnet": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-3-haiku": "claude-haiku-4-5",
    "claude-haiku": "claude-haiku-4-5",
    "claude-haiku-4-5": "claude-haiku-4-5",
    "claude-3-opus": "claude-opus-4-8",
    "claude-opus": "claude-opus-4-8",
    "claude-opus-4-8": "claude-opus-4-8",
    "mistral-small": "mistral-small-2603",
    "mistral-small-2603": "mistral-small-2603",
    "gpt-oss-120b": "tinfoil/gpt-oss-120b",
    "tinfoil/gpt-oss-120b": "tinfoil/gpt-oss-120b",
    "gemma4-31b": "tinfoil/gemma4-31b",
    "tinfoil/gemma4-31b": "tinfoil/gemma4-31b",
    # Image generation runs through GPT-5.6 Luna's native GenerateImage tool
    # (backed by "GPT Image 2"); OpenAI image-model names alias onto it.
    "gpt-image-1": "gpt-5.6-luna",
    "gpt-image-2": "gpt-5.6-luna",
    "dall-e-3": "gpt-5.6-luna",
}

MODEL_FAMILY = {
    "gpt-5.6": "gpt-5.6-luna",
    "gpt-5.4": "gpt-5.4-mini",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-haiku": "claude-haiku-4-5",
    "claude-opus": "claude-opus-4-8",
    "claude": "claude-sonnet-4-6",
    "mistral": "mistral-small-2603",
    "gpt-oss": "tinfoil/gpt-oss-120b",
    "gemma": "tinfoil/gemma4-31b",
}


def resolve_model(name: str | None) -> str:
    """Map any user-facing model name to a real Duck.ai backend id."""
    if not name:
        return DEFAULT_MODEL
    n = name.strip()
    if n in MODEL_LABELS:
        return n
    low = n.lower()
    if low in MODEL_ALIASES:
        return MODEL_ALIASES[low]
    # A live catalog id (fetched at startup) is passed through untouched.
    if low in _LIVE_MODELS:
        return _LIVE_MODELS[low]
    for k, v in MODEL_ALIASES.items():
        if k.lower() == low:
            return v
    for fam in sorted(MODEL_FAMILY, key=len, reverse=True):
        if low.startswith(fam):
            return MODEL_FAMILY[fam]
    return n


# id(lower) -> canonical id, refreshed by fetch_model_catalog() via note_live_models().
_LIVE_MODELS: dict = {}


def note_live_models(models: List[dict]) -> None:
    """Remember live catalog ids so resolve_model() stops guessing for them."""
    global _LIVE_MODELS
    out = {}
    for m in models:
        mid = (m.get("id") or "").strip()
        if mid:
            out[mid.lower()] = mid
    if out:
        _LIVE_MODELS = out


def fetch_model_catalog(timeout: float = 10.0) -> List[dict]:
    """Public GET /duckchat/v1/models (no tokens needed). Raises on network error."""
    url = f"{BASE}/duckchat/v1/models"
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": REAL_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    models = data.get("models") or []
    note_live_models(models)
    return models


class DuckAIError(Exception):
    pass


class DuckAIRateLimit(DuckAIError):
    pass


class _Null:
    banned = False


_NULL = _Null()


def _normalize_proxy(proxy: str | None) -> dict:
    """Convert a proxy URL (http/https/socks5, optional user:pass) into Playwright's proxy dict.

    Duck.ai issues a persistent per-IP ban (ERR_BN_LIMIT); a clean proxy is the fix.
    """
    if not proxy:
        return {}
    p = proxy.strip()
    m = re.match(
        r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://(?:(?P<user>[^:@]+):(?P<pwd>[^@]*)@)?(?P<host>.+)$",
        p,
    )
    if not m:
        # bare host:port without scheme -> assume http
        return {"server": f"http://{p}"}
    server = f"{m.group('scheme')}://{m.group('host')}"
    out: dict = {"server": server}
    if m.group("user"):
        out["username"] = m.group("user")
        out["password"] = m.group("pwd") or ""
    return out


# Hook contract:
#   window.__duckai = {chunks, done, err}  - the CURRENT request's capture object.
# Python installs a fresh capture object (via _RESET_JS) before each send; the hook
# attaches to whatever object is live at response time, so a stale reader from an
# abandoned stream keeps writing to its OWN (garbage) object, never the new one.
#   window.__duckai.rewrite = {...}        - optional body patch, consumed once.
_INIT_SCRIPT = r"""
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.__duckai = {chunks:[], done:false, err:null, rewrite:null, rewriteApplied:false};
(function(){
  const O = window.fetch;
  window.fetch = async function(u,o){
    o = o || {};
    if(typeof u === 'string' && u.includes('/duckchat/v1/chat')){
      const D = window.__duckai;
      D.chunks = []; D.done = false; D.err = null;
      const rw = D.rewrite; D.rewrite = null;
      if(rw && typeof o.body === 'string'){
        try{
          const j = JSON.parse(o.body);
          if(rw.model) j.model = rw.model;
          if(rw.reasoningEffort) j.reasoningEffort = rw.reasoningEffort;
          if(rw.canUseTools !== undefined) j.canUseTools = rw.canUseTools;
          if(rw.toolChoice){
            j.metadata = Object.assign({}, j.metadata);
            j.metadata.toolChoice = Object.assign({}, j.metadata.toolChoice, rw.toolChoice);
          }
          o = Object.assign({}, o, {body: JSON.stringify(j)});
          D.rewriteApplied = true;
        }catch(e){}
      }
      const r = await O.call(this, u, o);
      try{
        const reader = r.clone().body.getReader();
        const dec = new TextDecoder();
        (async function pump(){
          try{
            while(true){
              const {value,done} = await reader.read();
              if(done){ D.chunks.push(dec.decode()); D.done=true; break; }
              const s = dec.decode(value,{stream:true});
              D.chunks.push(s);
              if(s.indexOf('"action":"error"') !== -1) D.err = s;
            }
          }catch(e){ D.err = String(e); D.done = true; }
        })();
      }catch(e){ D.err = String(e); D.done = true; }
      return r;
    }
    return O.apply(this, arguments);
  };
})();
"""

# Fresh capture object for the next send (drops any stale abandoned-stream state).
_RESET_JS = r"""
(rw) => { window.__duckai = {chunks:[], done:false, err:null, rewrite: rw, rewriteApplied:false}; }
"""

# Pull only the chunks appended since index `n`. Returns {n, text, done, err}.
_POLL_JS = r"""
(n) => {
  const D = window.__duckai || {chunks:[],done:false,err:null};
  const text = D.chunks.slice(n).join('');
  return {n: D.chunks.length, text, done: D.done, err: D.err, rewriteApplied: !!D.rewriteApplied};
}
"""


def _parse_event(line: str) -> Optional[dict]:
    """Parse one SSE payload line into an event dict, or None if not a JSON object.

    Text turns use `{"message": ...}`; image turns use role=ui-component events
    carrying `data.b64Image`. Both flow through here so the driver stays generic.
    """
    line = line.strip()
    if not line.startswith("{"):
        if line.startswith("data:"):
            line = line[5:].strip()
        else:
            return None
    if line == "[DONE]" or not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# Flipped off process-wide if a rewritten body ever draws ERR_CHALLENGE
# (i.e. Duck.ai binds its anti-bot answer to the request body).
_REWRITE_OK = True


# Used when the composer textarea exposes no maxlength attribute. 12K chars is
# verified to get an answer from duck.ai; 24K never replies.
_PROMPT_FALLBACK_LIMIT = 12000


# Page warmup. A fixed 7s sleep paid on EVERY turn dominated end-to-end latency
# (measured: ~10s to first token, of which 7s was this sleep). We now poll for
# readiness with WARM_MIN as a hard floor and WARM_MAX as the deadline, which
# keeps the anti-ban guarantee but lets a fast page skip straight to ready.
#
# WARM_MIN=1.0 (2026-11-13): live probes sent successfully at 1.5s page age
# with valid replies and no ERR_BN_LIMIT/ERR_CHALLENGE - the textarea only
# appears once the challenge JS is up (~2.0s load), so the effective gate is
# textarea presence, not the floor. Set env DUCKAI_WARM_MIN back to 2.0 if a
# deploy ever sees challenge errors.
WARM_MIN = float(os.getenv("DUCKAI_WARM_MIN", "1.0"))
WARM_MAX = float(os.getenv("DUCKAI_WARM_MAX", "7.0"))

# Launch Chrome and warm a page at server startup instead of on first request.
# Set to 0 to boot lazy (no browser until someone actually asks).
PREWARM = os.getenv("DUCKAI_PREWARM", "1") not in ("0", "false", "False")

# Keep a fully-warmed spare tab ready at all times. A fresh-prompt request
# (anything that is not a continuation) must otherwise open a page and pay the
# 2-7s challenge warm BEFORE the first token can move; taking an already-warm
# standby cuts that to ~0, and a replacement warms in the background while the
# current request streams. Costs one extra Chrome tab (~100-150MB) - set
# DUCKAI_STANDBY=0 on memory-tight boxes.
STANDBY = os.getenv("DUCKAI_STANDBY", "1") not in ("0", "false", "False")
# Number of hot spares. Two keeps back-to-back fresh prompts (client retries,
# parallel IDE calls) off the cold path entirely - BUT every model with a
# session runs its own browser, so RAM scales as models x (tabs + spares).
# Railway's small plans OOM-kill Chrome at 3 browsers x 2 spares ("Target
# crashed"), so default to ONE spare; raise to 2 only on fat single-model
# deployments.
STANDBY_COUNT = int(os.getenv("DUCKAI_STANDBY_COUNT", "1"))


def _clamp_prompt(prompt: str, limit: int) -> str:
    """Trim an over-long prompt to `limit`, keeping the head (system preamble)
    and the tail (the live question) and dropping the middle."""
    marker = "\n…[context truncated]…\n"
    if limit <= len(marker) + 40:
        return prompt[:limit]
    head_n = max(1, limit // 10)
    tail_n = limit - head_n - len(marker)
    return prompt[:head_n] + marker + prompt[-tail_n:]


class _BrowserSession:
    """One persistent real-Chrome tab; UI-driven send; SSE response tee-streamed."""

    def __init__(self, model: str, proxy: Optional[str], timeout: float, new_chat: bool = False) -> None:
        self.model = model
        self.proxy = proxy
        self.timeout = timeout
        self.new_chat = new_chat
        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self._last_prompt: Optional[str] = None
        self._ready = False
        self.banned = False
        # When the current page was opened - drives the warmup poll in _wait_warm.
        self._page_opened_at = 0.0
        # The reused page holds live conversation state, so only one send may drive
        # it at a time; concurrent clients queue instead of clobbering each other.
        self._lock = asyncio.Lock()
        # Hot spares: fully-warmed tabs taken instantly by fresh-prompt
        # requests, refilled in the background while that request streams.
        # Two spares back-to-back bursts (client retries, parallel IDE calls)
        # from ever falling back to the cold open+warm path.
        self._standby_pages: List[Any] = []
        self._standby_tasks: List[asyncio.Task] = []
        # Set by a failed standby refill when the context itself is dead; the
        # next _ensure_ready honours it and rebuilds the whole transport.
        self._reset_needed = False

    async def _ensure_ready(self) -> None:
        if self._ready:
            # A failed standby refill can flag the transport dead while
            # _ready is still True; honour the flag instead of serving from
            # a zombie browser forever (the overnight-EAGAIN failure mode).
            if self._reset_needed:
                self._ready = False
                await self._reset()
            else:
                return
        self._pw = await async_playwright().start()
        launch: dict = {"headless": True, "channel": "chrome"}
        if CHROME_PATH and os.path.exists(CHROME_PATH):
            launch["executable_path"] = CHROME_PATH
        args = ["--disable-blink-features=AutomationControlled"]
        if os.name == "posix":
            # Container survival: small /dev/shm (the Docker default, 64M) makes
            # Chrome tab-render crash mid-stream; this flag routes around it.
            args.append("--disable-dev-shm-usage")
            # Chrome's setuid sandbox does not exist in most containers. Force
            # --no-sandbox when the deploy opted in (Dockerfile sets it) or when
            # we actually run as root - otherwise the browser never launches.
            if (os.getenv("DUCKAI_CHROME_NO_SANDBOX", "").strip() == "1"
                    or (hasattr(os, "geteuid") and os.geteuid() == 0)):
                args.append("--no-sandbox")
        launch["args"] = args
        if self.proxy:
            launch["proxy"] = _normalize_proxy(self.proxy)
        self.browser = await self._pw.chromium.launch(**launch)
        self.ctx = await self.browser.new_context(user_agent=REAL_UA)
        await self.ctx.add_init_script(_INIT_SCRIPT)
        self._ready = True

    async def _drop_page(self) -> None:
        if self.page is not None:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        self._last_prompt = None
        # Page dropped -> nothing warm for the next fresh send; refill now so
        # even a cold path only waits for one warm cycle, not the next two.
        self._refill_standby()

    async def _reset(self) -> None:
        """Tear the whole browser down after a transport-level failure so the
        next request rebuilds from scratch (dead browser/ctx would otherwise
        make every later call throw)."""
        await self._drop_page()
        # Drop any warm spares too: they live in the dying context, and their
        # Chrome renderers must be reaped or fork-EAGAIN compounds overnight.
        for p in self._standby_pages:
            try:
                await p.close()
            except Exception:
                pass
        self._standby_pages = []
        for t in self._standby_tasks:
            t.cancel()
        self._standby_tasks = []
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.browser = None
        self.ctx = None
        self._pw = None
        self._ready = False
        self._reset_needed = False

    async def _open_warmed_page(self):
        """Open a fresh page and warm it so the app mints its challenge/token JS.

        Duck.ai needs the first seconds of a fresh page to build its challenge
        answer; sending too early hits ERR_BN_LIMIT. 7s is the safe floor.

        We poll for readiness instead of sleeping the full 7s: the challenge
        token is ready well before 7s on a warm connection, so a ready-check
        cuts the common case to ~2s while the deadline still guards slow ones.

        Takes the hot standby tab when available (zero-wait) and schedules a
        replacement warm in the background.
        """
        await self._ensure_ready()
        if self._standby_pages:
            page = self._standby_pages.pop(0)
            self._page_opened_at = asyncio.get_event_loop().time()
        else:
            page = await self.ctx.new_page()
            await page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
            self._page_opened_at = asyncio.get_event_loop().time()
            await self._wait_warm(page)
        self.page = page
        self._last_prompt = None
        self._refill_standby()
        return page

    async def _warm_standby(self) -> None:
        """Warm one spare tab onto _standby_pages (best-effort, never raises)."""
        if self._pw is None:
            return
        page = None
        try:
            page = await self.ctx.new_page()
            await page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
            opened_at = asyncio.get_event_loop().time()
            # Warm against the page's own open time, not the session's: with
            # multiple spares each tab must independently satisfy WARM_MIN.
            await self._wait_warm(page, page_opened_at=opened_at)
            self._standby_pages.append(page)
        except Exception as e:  # noqa: BLE001 - standby is pure optimization
            logger.warning("standby warm skipped: %s", e)
            # A half-opened tab would leak a Chrome renderer on every failed
            # refill; overnight that compounds into fork-EAGAIN (EADDR across
            # hundreds of zombie children). Close it, and if the CONTEXT died,
            # tear the whole transport down so the next request rebuilds clean.
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            if self.ctx is not None:
                try:
                    probe = await self.ctx.new_page()
                    await probe.close()
                except Exception:
                    logger.warning("standby: context dead after warm failure; scheduling reset")
                    self._reset_needed = True

    def _refill_standby(self) -> None:
        if not STANDBY or self._pw is None:
            return
        self._standby_tasks = [t for t in self._standby_tasks if not t.done()]
        while len(self._standby_pages) + len(self._standby_tasks) < STANDBY_COUNT:
            self._standby_tasks.append(
                asyncio.get_event_loop().create_task(self._warm_standby()))

    async def _wait_warm(self, page, budget: float = 7.0, page_opened_at: Optional[float] = None) -> None:
        """Block until the page looks ready to send, capped at `budget` seconds.

        Readiness signal: the composer textarea exists AND the page has been up
        for at least WARM_MIN seconds - Duck.ai refuses an instant send while
        its challenge JS is still booting. Falls back to the full budget if the
        probe never goes green, so behaviour can never get WORSE than before.

        `page_opened_at` overrides the session's open timestamp: standby tabs
        each track their own load clock so several can warm concurrently.
        """
        loop = asyncio.get_event_loop()
        opened = self._page_opened_at if page_opened_at is None else page_opened_at
        deadline = loop.time() + budget
        while loop.time() < deadline:
            if loop.time() - opened >= WARM_MIN:
                try:
                    if await page.query_selector("textarea") is not None:
                        return
                except Exception:
                    return
            await page.wait_for_timeout(100)

    def _continuation(self, prompt: str) -> Optional[str]:
        """Text to type when `prompt` only ADDS one turn to the live conversation.

        Stateless clients (Claude Code et al.) re-send the whole messages[] each
        turn; our flatten() is deterministic, so when the new prompt starts with
        exactly what we typed last time, the delta is one `Human:` turn - the page
        already holds the rest as native context. Anything messier -> None (fresh
        full-prompt send), which is always correct, just slower.
        """
        if self._last_prompt is None or self.page is None:
            return None
        if not prompt.startswith(self._last_prompt):
            return None
        tail = prompt[len(self._last_prompt):].strip()
        marks = list(re.finditer(r"(?m)^Human: ", tail))
        if len(marks) != 1:
            return None
        new_text = tail[marks[0].end():].strip()
        return new_text or None

    async def _trigger_send(self, page, prompt: str) -> None:
        ta = await page.query_selector("textarea")
        if ta is None:
            raise DuckAIError("duck.ai textarea not found (page layout changed)")
        # Duck.ai's composer hard-caps its length (observed: 12K ok, 24K never
        # answers). IDE clients inject 30-100K contexts; over-long fills leave
        # Send disabled and the turn hangs until timeout, poisoning the session.
        raw = await ta.get_attribute("maxlength")
        limit = int(raw) if raw and raw.isdigit() and int(raw) > 0 else _PROMPT_FALLBACK_LIMIT
        if len(prompt) > limit:
            logger.warning("prompt %d chars exceeds duck.ai limit %d; truncating (head+tail kept)",
                           len(prompt), limit)
            prompt = _clamp_prompt(prompt, limit)
        await ta.click()
        await ta.fill(prompt)
        await page.wait_for_timeout(150)
        sent = False
        for btn in await page.query_selector_all("button"):
            t = (await btn.inner_text() or "").strip()
            a = await btn.get_attribute("aria-label") or ""
            if t in ("Ask", "Send", "问", "发送") or a in ("Ask", "Send", "问", "发送"):
                await btn.click(force=True)
                sent = True
                break
        if not sent:
            await ta.press("Enter")

    # duck.ai's hidden file input accepts exactly these (probed live 2026-11).
    _ATTACH_MIMES = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
    }

    async def _attach_files(self, page, files: List[dict]) -> None:
        """Attach image/PDF bytes to the composer via duck.ai's hidden file input.

        `files` is a list of {data: bytes, mime: str}. The input is hidden but a
        real <input type=file>, so Playwright's set_input_files drives the app's
        own upload path (thumbnail preview, request framing) exactly like a
        manual pick - no fetch crafting, no challenge break.
        """
        if not files:
            return
        uploads: List[dict] = []
        for f in files:
            ext = self._ATTACH_MIMES.get(f["mime"])
            if ext is None:
                raise DuckAIError(f"unsupported attachment type: {f['mime']}")
            fh = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            try:
                fh.write(f["data"])
                fh.close()
                uploads.append({"name": fh.name, "mime": f["mime"]})
            except Exception:
                fh.close()
                raise
        try:
            # Last input[type=file] wins: duck.ai renders exactly one upload
            # input per composer; querying all keeps us safe against duplicates.
            inputs = await page.query_selector_all("input[type=file]")
            target = inputs[-1] if inputs else None
            if target is None:
                raise DuckAIError("duck.ai file input not found (page layout changed)")
            await target.set_input_files([u["name"] for u in uploads])
            # Give the app a beat to build thumbnails / flip Send enabled: a
            # thumbnail chip appearing next to the composer is the readiness
            # signal; cap at 2s (thumbnails are local, this is near-instant).
            for _ in range(20):
                await page.wait_for_timeout(100)
                chips = await page.query_selector_all("[class*='thumbnail'], [class*='attachment'], img[src^='blob:']")
                if chips:
                    break
            await page.wait_for_timeout(150)
            logger.info("attached %d file(s) to duck.ai composer", len(uploads))
        finally:
            for u in uploads:
                try:
                    os.unlink(u["name"])
                except OSError:
                    pass

    async def _iter_events(self, prompt: str, timeout: float, rewrite: Optional[dict],
                           attachments: Optional[List[dict]] = None) -> AsyncIterator[dict]:
        global _REWRITE_OK
        page_alive = self.page is not None and not self.page.is_closed()
        text = None
        # Attachments always force a fresh page: the continuation fast-path only
        # types a delta into the existing composer, which would leave the files
        # behind - vision turns must go through the full upload+send path.
        if page_alive and not self.new_chat and not attachments:
            text = self._continuation(prompt)
        if text is None:
            if page_alive:
                await self._drop_page()
            page = await self._open_warmed_page()
            text = prompt
        else:
            page = self.page

        await page.evaluate(_RESET_JS, rewrite if (rewrite and _REWRITE_OK) else None)
        try:
            if attachments:
                await self._attach_files(page, attachments)
            await self._trigger_send(page, text)
        except Exception:
            await self._drop_page()
            raise

        buf = ""
        idx = 0
        saw_data = False
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            st = await page.evaluate(_POLL_JS, idx)
            idx = st["n"]
            if st.get("err"):
                if st["err"].startswith("Error:") or st["err"].startswith("TypeError"):
                    # pump() read-failure, not a server payload - surface as generic error
                    await self._drop_page()
                    raise DuckAIError(f"duck.ai stream read failed: {st['err']}")
                if rewrite and st.get("rewriteApplied") and "ERR_CHALLENGE" in st["err"]:
                    _REWRITE_OK = False
                    logger.warning("rewritten chat body drew ERR_CHALLENGE; body rewrite disabled")
                    await self._drop_page()
                    raise _RewriteRejected()
                self._raise_err(st["err"])
            new = st.get("text") or ""
            if new:
                saw_data = True
                buf += new
                # Emit every complete SSE line; keep the trailing partial for next round.
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    ev = _parse_event(line)
                    if ev is not None:
                        yield ev
            if st.get("done"):
                if buf.strip():
                    ev = _parse_event(buf)
                    if ev is not None:
                        yield ev
                if not saw_data:
                    await self._drop_page()
                    raise DuckAIError("Duck.ai closed the stream without a reply")
                self._last_prompt = prompt
                return
            # 150ms floor: stream chunks land between polls; 500ms pre-first-byte
            # was pure added latency once the standby tab made sends near-instant.
            await page.wait_for_timeout(150 if saw_data else 300)
        await self._drop_page()
        raise DuckAIError("timed out waiting for Duck.ai reply")

    async def send_stream_ui(self, prompt: str, timeout: float = 120.0, rewrite: Optional[dict] = None,
                             attachments: Optional[List[dict]] = None) -> AsyncIterator[str]:
        """Drive the real UI to send, then stream the reply token-by-token.

        Reuses the live page when the request continues the previous conversation;
        otherwise opens a fresh warmed page and re-sends the full flattened prompt
        (Duck.ai is a one-textarea chat; history must arrive in the body).
        `attachments` ({data, mime} list) rides the app's own file-upload path.

        A dead-page error (browser OOM-killed the standby tab, context closed)
        retries ONCE via a full _reset - the tab is gone either way, so the
        second attempt rebuilds the browser from scratch and serves the turn.
        Retry only happens pre-first-token: after that the client already has
        partial output and a re-send would duplicate it.
        """
        yielded_any = False
        for attempt in (0, 1, 2):
            try:
                async with self._lock:
                    async for ev in self._iter_events(prompt, timeout, rewrite, attachments):
                        msg = ev.get("message")
                        if msg:
                            yielded_any = True
                            yield msg
                return
            except _RewriteRejected:
                if attempt < 2 and rewrite:
                    continue
                raise
            except Exception as e:
                dead_transport = ("Target page, context or browser has been closed" in str(e)
                                  or "browser transport" in str(e))
                if attempt < 2 and dead_transport and not yielded_any:
                    logger.warning("dead page/transport (%s); full reset and retry", e)
                    await self._reset()
                    continue
                raise

    async def send_ui(self, prompt: str, timeout: float = 120.0, rewrite: Optional[dict] = None,
                      attachments: Optional[List[dict]] = None) -> str:
        parts: List[str] = []
        async for tok in self.send_stream_ui(prompt, timeout, rewrite, attachments):
            parts.append(tok)
        return "".join(parts)

    _IMAGE_INSTRUCTION = (
        "Call the image generation tool directly and generate one image from the "
        "description below. Do not ask questions, do not explain the process. "
        "Image description: {prompt}{size_hint}"
    )

    async def send_image_ui(
        self,
        prompt: str,
        timeout: float = 180.0,
        size: Optional[str] = None,
        rewrite_model: Optional[str] = None,
    ) -> dict:
        """Delegate to Duck.ai's native GenerateImage (GPT Image 2) and return the image.

        The chat turn makes the model invoke the tool; the SSE carries a
        tool-invocation "call" event (refined imageGenPrompt), then ui-component
        data events whose status=="success" frame holds the FULL base64 JPEG
        (earlier status=="partial" frames are lower-res previews - ignored).
        """
        hint = ""
        if size:
            dims = re.split(r"[x×]", str(size))
            try:
                w, h = int(dims[0]), int(dims[1])
                hint = " (composition: wide landscape)" if w > h else " (composition: portrait)" if h > w else " (composition: square)"
            except (ValueError, IndexError):
                pass
        instruction = self._IMAGE_INSTRUCTION.format(prompt=prompt, size_hint=hint)
        rewrite = {"model": rewrite_model} if rewrite_model else None

        result = {"b64": None, "title": None, "gen_prompt": None, "text": ""}
        for attempt in (0, 1):
            try:
                async with self._lock:
                    async for ev in self._iter_events(instruction, timeout, rewrite):
                        if ev.get("message"):
                            result["text"] += ev["message"]
                        if ev.get("role") == "tool-invocation" and ev.get("state") == "call":
                            try:
                                result["gen_prompt"] = json.loads(ev.get("toolArguments") or "{}").get("imageGenPrompt")
                            except json.JSONDecodeError:
                                pass
                        data = ev.get("data")
                        if isinstance(data, dict):
                            if data.get("type") == "image-title":
                                result["title"] = data.get("title") or result["title"]
                            img = data.get("b64Image")
                            if img and data.get("status") == "success":
                                result["b64"] = img
                break
            except _RewriteRejected:
                if attempt == 0 and rewrite:
                    continue
                raise
        if not result["b64"]:
            raise DuckAIError("Duck.ai returned no image (the model may have answered in text)")
        return result

    @staticmethod
    def _raise_err(text: str) -> None:
        m = re.search(r'"type"\s*:\s*"([^"]+)"', text)
        typ = m.group(1) if m else "error"
        if "ERR_BN_LIMIT" in text:
            raise DuckAIRateLimit("Duck.ai ERR_BN_LIMIT (IP/fingerprint banned)")
        if "ERR_CHALLENGE" in text:
            raise DuckAIError("Duck.ai ERR_CHALLENGE (challenge required)")
        raise DuckAIError(f"Duck.ai error: {typ}")

    async def close(self) -> None:
        for t in self._standby_tasks:
            t.cancel()
        for p in self._standby_pages:
            try:
                await p.close()
            except Exception:
                pass
        self._standby_tasks = []
        self._standby_pages = []
        try:
            if self.browser:
                await self.browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass


class _RewriteRejected(DuckAIError):
    """Internal: rewritten body got ERR_CHALLENGE; retry once unmodified."""


class DuckAISession:
    """Thin pool over _BrowserSession; rotates on ban.

    The fundamental fix for the ERR_BN_LIMIT the user hit is real Chrome + UI-driven
    send (see module docstring) - NOT IP rotation. Proxy is optional, kept only for
    users who genuinely need it.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        proxies: Optional[List[str]] = None,
        timeout: float = 120.0,
        new_chat: bool = False,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.new_chat = new_chat
        # LRU bookkeeping (main.get_session RAM guard); 0.0 = never used.
        self.last_used = 0.0
        if not proxies:
            self.proxies: List[Optional[str]] = [None]
        elif isinstance(proxies, str):
            self.proxies = [p.strip() or None for p in proxies.split(",")]
        else:
            self.proxies = [p.strip() or None for p in proxies]
        self._sessions: dict = {}
        self._order = list(range(len(self.proxies)))
        self._idx = 0

    def _next_proxy_index(self) -> Optional[int]:
        healthy = [i for i in self._order if not self._sessions.get(i, _NULL).banned]
        if not healthy:
            return None
        for i in range(len(self._order)):
            cand = (self._idx + i) % len(self._order)
            if not self._sessions.get(cand, _NULL).banned:
                self._idx = cand
                return cand
        return healthy[0]

    async def _session_for(self, idx: int) -> _BrowserSession:
        if idx not in self._sessions:
            self._sessions[idx] = _BrowserSession(
                self.model, self.proxies[idx], self.timeout, new_chat=self.new_chat
            )
        return self._sessions[idx]

    async def prewarm(self) -> None:
        """Pay the browser+page startup cost ahead of the first request.

        Launching Chrome and warming a page costs ~10s, and _iter_events drops
        and re-warms a page on most turns, so an unwarmed server makes the very
        first caller wait for the full cost. Called once from app startup: the
        first request then finds a live, already-warmed page - and with STANDBY
        on, a second hot tab behind it for the next fresh-prompt request.

        Best-effort - a failure here must not stop the server booting, because
        the lazy path in _session_for still works.
        """
        if not PREWARM:
            return
        t0 = asyncio.get_event_loop().time()
        try:
            idx = self._next_proxy_index()
            if idx is None:
                return
            s = await self._session_for(idx)
            await s._ensure_ready()
            await s._open_warmed_page()
            logger.info("prewarmed browser+page in %.1fs", asyncio.get_event_loop().time() - t0)
        except Exception as e:
            logger.warning("prewarm failed (%s); serving lazily instead", e)

    async def send(self, prompt: str, rewrite: Optional[dict] = None, max_rotations: int = 3,
                   attachments: Optional[List[dict]] = None) -> str:
        last_err: Optional[Exception] = None
        for _ in range(max(1, max_rotations)):
            idx = self._next_proxy_index()
            if idx is None:
                raise DuckAIRateLimit("all sessions failed (Duck.ai ban)")
            try:
                return await (await self._session_for(idx)).send_ui(prompt, self.timeout, rewrite, attachments)
            except DuckAIRateLimit as e:
                last_err = e
                self._sessions.get(idx).banned = True
                continue
            except DuckAIError:
                raise
            except Exception as e:
                # Playwright transport died (browser OOM-killed, context closed).
                # Rebuild in-loop and RETRY the same request once: the caller
                # paid for the answer, and a rebuilt browser will serve it -
                # surfacing a 502 here just moved the crash to the client.
                logger.warning("browser transport died (%s); rebuilding and retrying", e)
                await self._sessions[idx]._reset()
                try:
                    return await (await self._session_for(idx)).send_ui(prompt, self.timeout, rewrite, attachments)
                except Exception as e2:
                    raise DuckAIError(f"browser transport failed after rebuild: {e2}")
        raise last_err or DuckAIRateLimit("all sessions failed")

    async def send_stream(self, prompt: str, rewrite: Optional[dict] = None, max_rotations: int = 2,
                          attachments: Optional[List[dict]] = None) -> AsyncIterator[str]:
        """Stream the assistant reply token-by-token (real incremental SSE)."""
        last_err: Optional[Exception] = None
        for _ in range(max(1, max_rotations)):
            idx = self._next_proxy_index()
            if idx is None:
                raise DuckAIRateLimit("all sessions failed (Duck.ai ban)")
            saw_token = False
            try:
                async for tok in (await self._session_for(idx)).send_stream_ui(prompt, self.timeout, rewrite, attachments):
                    saw_token = True
                    yield tok
                return
            except DuckAIRateLimit as e:
                last_err = e
                self._sessions.get(idx).banned = True
                continue
            except DuckAIError:
                raise
            except Exception as e:
                # Same rebuild-and-retry contract as send(): a dead transport
                # must cost the caller one clean retry, not a hard 502. Only
                # safe pre-first-token: once tokens flowed the client already
                # got a partial stream, so re-sending would duplicate output.
                if saw_token:
                    raise DuckAIError(f"browser transport failed mid-stream: {e}")
                logger.warning("browser transport died (%s); rebuilding and retrying", e)
                await self._sessions[idx]._reset()
                try:
                    async for tok in (await self._session_for(idx)).send_stream_ui(prompt, self.timeout, rewrite, attachments):
                        saw_token = True
                        yield tok
                    return
                except Exception as e2:
                    if saw_token:
                        raise DuckAIError(f"browser transport failed mid-stream: {e2}")
                    raise DuckAIError(f"browser transport failed after rebuild: {e2}")
        raise last_err or DuckAIRateLimit("all sessions failed")

    async def send_image(self, prompt: str, size: Optional[str] = None, max_rotations: int = 2) -> dict:
        """Generate an image via Duck.ai's native GenerateImage tool.

        Returns {b64, title, gen_prompt, text}. Rotates proxy sessions on ban,
        mirroring send(). rewrite_model forces the browser page onto this
        session's model so the tool-invocation reaches the right backend.
        """
        last_err: Optional[Exception] = None
        for _ in range(max(1, max_rotations)):
            idx = self._next_proxy_index()
            if idx is None:
                raise DuckAIRateLimit("all sessions failed (Duck.ai ban)")
            try:
                return await (await self._session_for(idx)).send_image_ui(
                    prompt, timeout=180.0, size=size, rewrite_model=self.model
                )
            except DuckAIRateLimit as e:
                last_err = e
                self._sessions.get(idx).banned = True
                continue
            except DuckAIError:
                raise
            except Exception as e:
                await self._sessions[idx]._reset()
                raise DuckAIError(f"browser transport failed: {e}")
        raise last_err or DuckAIRateLimit("all sessions failed")

    async def close(self) -> None:
        for s in self._sessions.values():
            await s.close()
