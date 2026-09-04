"""Model providers: Ollama, Anthropic, OpenAI (and compatible), Google Gemini.

The harness asks for tool calls as `<tool_call>` text rather than through each
vendor's function-calling API, so a provider only has to do one thing: turn a
list of messages into a stream of text. That is why each of these is a few
dozen lines and needs no SDK - the wire formats differ, the job does not.

Every provider yields the same events, whatever it is talking to:

    {"text": "..."}                        a piece of the reply
    {"thinking": "..."}                    reasoning, where the provider sends it
    {"done": True, "prompt_tokens": N, "completion_tokens": M,
     "total_seconds": f, "eval_seconds": f}

Credentials come from the environment first, then `~/.localchat/providers.json`,
which is written with owner-only permissions. Nothing is ever stored in the
project directory - that is a place people commit from.
"""

import asyncio
import contextlib
import json
import os
import threading
import time

from simple_harness import paths

from simple_harness import atomic
from simple_harness.sse import iter_sse


CONFIG_DIR = paths.home()
CONFIG_PATH = os.path.join(CONFIG_DIR, "providers.json")

HTTP_TIMEOUT = (10, 600)          # (connect, read): a long reply is not a hang


def _requests():
    import requests
    return requests


# ---------------------------------------------------------------------------
# message shaping
# ---------------------------------------------------------------------------

class _PartialCalls:
    """Collects tool calls that arrive as a stream of JSON fragments.

    Anthropic and OpenAI both send a tool call's arguments a few characters at
    a time, so nothing can be parsed until the last fragment has landed. Each
    call is kept under the index or id the provider tags it with, and turned
    into an event only when the provider says it is finished.
    """

    def __init__(self):
        self.calls = {}

    def start(self, key, name: str = "", call_id: str = "") -> None:
        entry = self.calls.setdefault(key, {"name": "", "id": "", "json": ""})
        if name:
            entry["name"] = name
        if call_id:
            entry["id"] = call_id

    def feed(self, key, fragment: str) -> None:
        if key in self.calls and fragment:
            self.calls[key]["json"] += fragment

    def take(self, key):
        entry = self.calls.pop(key, None)
        return _decode_call(entry) if entry else None

    def drain(self):
        for key in list(self.calls):
            event = self.take(key)
            if event:
                yield event


def _decode_call(entry: dict):
    """One collected call as a `tool_call` event, or None if it has no name."""
    name = (entry.get("name") or "").strip()
    if not name:
        return None
    blob = (entry.get("json") or "").strip()
    call = {"name": name, "id": entry.get("id", ""), "arguments": {}}
    if not blob:
        return {"tool_call": call}         # a tool that takes no arguments
    try:
        arguments = json.loads(blob)
    except json.JSONDecodeError as error:
        # The provider built this JSON, so this should not happen - but saying
        # so beats calling the tool with nothing and reporting a bad result.
        call["error"] = f"the arguments were not valid JSON ({error})"
        return {"tool_call": call}
    call["arguments"] = arguments if isinstance(arguments, dict) else {}
    return {"tool_call": call}


def token_budget(max_tokens: int | None) -> int:
    """The output cap to send, as an int.

    Every hosted API rejects a float here, and the setting is easy to write as
    one (`4096 * 1.5` is 6144.0), so it is coerced in the one place they all
    pass through rather than trusted at four call sites.
    """
    from simple_harness import config       # lazily, like everywhere else here - see the header
    return int(max_tokens or config.NUM_PREDICT)


def split_system(messages: list) -> tuple:
    """Separate the system prompt from the conversation.

    Ollama and OpenAI take it as a message; Anthropic and Gemini take it as its
    own field.
    """
    system = "\n\n".join(m.get("content", "") for m in messages
                         if m.get("role") == "system").strip()
    rest = [m for m in messages if m.get("role") != "system"]
    return system, rest


def merge_runs(messages: list) -> list:
    """Collapse consecutive same-role messages into one.

    The harness appends every tool result as its own user message, so a turn
    routinely ends up with several in a row. Some APIs reject that.
    """
    merged = []
    for message in messages:
        content = message.get("content", "")
        if merged and merged[-1]["role"] == message.get("role"):
            merged[-1]["content"] += "\n\n" + content
        else:
            merged.append({"role": message.get("role", "user"), "content": content})
    return merged


def _as_stream(make_chunks):
    """Turn a blocking generator into an async one, off the event loop.

    Without this the spinner would freeze and the tokens-per-second counter
    would stop while the reply streams in.
    """
    async def stream():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        done = object()

        def worker():
            try:
                for item in make_chunks():
                    loop.call_soon_threadsafe(queue.put_nowait, item)
            except BaseException as error:               # noqa: BLE001 - re-raised below
                loop.call_soon_threadsafe(queue.put_nowait, error)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = await queue.get()
            if item is done:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    return stream()


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------

class Provider:
    name = ""
    label = ""
    key_env: tuple = ()
    key_help = ""
    default_base_url = ""
    needs_key = True

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}

    # Whether this provider takes tool schemas over its own API. Ollama stays
    # False on purpose: the text <tool_call> protocol and its JSON repair are
    # what make small local models usable, and nothing here should disturb them.
    supports_native_tools = False

    def encode_tools(self, schemas: list) -> list:
        """Reshape `toolspec.native_schema()` into this provider's wire format."""
        raise NotImplementedError

    # -- configuration -----------------------------------------------------

    @property
    def api_key(self) -> str:
        for variable in self.key_env:
            value = os.environ.get(variable)
            if value:
                return value.strip()
        return str(self.settings.get("api_key") or "").strip()

    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url") or self.default_base_url).rstrip("/")

    @property
    def model(self) -> str:
        return str(self.settings.get("model") or "")

    @property
    def key_source(self) -> str:
        for variable in self.key_env:
            if os.environ.get(variable):
                return f"${variable}"
        return "saved" if self.settings.get("api_key") else ""

    def ready(self) -> str:
        """"" when this provider can be used, otherwise what is missing."""
        if self.needs_key and not self.api_key:
            return f"no API key ({' or '.join(self.key_env)})"
        if not self.model:
            return "no model chosen"
        return ""

    # -- what a subclass implements ---------------------------------------

    def list_models(self) -> list:
        """[{"name": ..., "detail": ...}], newest/most useful first."""
        raise NotImplementedError

    def stream(self, messages: list, max_tokens: int | None = None):
        raise NotImplementedError

    # -- shared helpers ----------------------------------------------------

    def _get_json(self, url: str, headers: dict) -> dict:
        response = _requests().get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def _post_sse(self, url: str, headers: dict, payload: dict):
        response = _requests().post(url, headers=headers,
                                    data=json.dumps(payload).encode("utf-8"),
                                    stream=True, timeout=HTTP_TIMEOUT)
        if response.status_code >= 400:
            detail = (response.text or "")[:400].strip()
            raise RuntimeError(f"{self.label} returned HTTP {response.status_code}: {detail}")
        return response


# What each local model says it can do, asked once per model. `ollama.show`
# is a local call, but the prompt is rebuilt often enough that asking every
# time would be wasteful, and a daemon that is down must not stall startup.
_ollama_capabilities: dict = {}


def ollama_supports_tools(model: str) -> bool:
    """Whether this local model was built with a tool-calling template.

    Ollama reports it outright, and it is worth asking: a model whose template
    cannot format a tool call will simply never make one, while a model that
    can is more accurate through that interface than through text. Models
    differ - of the twenty installed here, five have no tool support at all -
    so this is per model, not per provider.

    Anything that goes wrong means no: the text protocol works everywhere, so
    falling back to it is always safe.
    """
    if not model:
        return False
    if model in _ollama_capabilities:
        return _ollama_capabilities[model]
    supported = False
    try:
        import ollama
        shown = ollama.Client(timeout=2.0).show(model)
        capabilities = (shown.get("capabilities") if isinstance(shown, dict)
                        else getattr(shown, "capabilities", None)) or []
        supported = "tools" in capabilities
    except Exception:
        supported = False
    _ollama_capabilities[model] = supported
    return supported


def _ollama_calls(message):
    """Tool calls out of one Ollama chunk, however the client wrapped them.

    Ollama sends a call whole and has already parsed its arguments, so there
    is nothing to accumulate. The client returns pydantic objects rather than
    plain dicts, and older versions returned dicts, so both are read here.
    """
    raw = (message.get("tool_calls") if isinstance(message, dict)
           else getattr(message, "tool_calls", None)) or []
    events = []
    for call in raw:
        function = (call.get("function") if isinstance(call, dict)
                    else getattr(call, "function", None)) or {}
        name = (function.get("name") if isinstance(function, dict)
                else getattr(function, "name", "")) or ""
        arguments = (function.get("arguments") if isinstance(function, dict)
                     else getattr(function, "arguments", None))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not name:
            continue
        events.append({"tool_call": {
            "name": name, "id": "",
            "arguments": dict(arguments) if isinstance(arguments, dict) else {}}})
    return events


class OllamaProvider(Provider):
    name = "ollama"
    label = "Ollama"
    needs_key = False
    key_help = "Runs locally - no API key needed."

    @property
    def model(self) -> str:
        # Someone who never runs /connect keeps the model from config.py.
        from simple_harness import config
        return str(self.settings.get("model") or config.MODEL or "")

    @property
    def host(self) -> str:
        return str(self.settings.get("base_url") or "").rstrip("/")

    @property
    def supports_native_tools(self) -> bool:
        """Unlike the hosted providers, this depends on the model, not the API."""
        return ollama_supports_tools(self.model)

    def encode_tools(self, schemas: list) -> list:
        # Ollama takes OpenAI's shape.
        return [{"type": "function",
                 "function": {"name": s["name"], "description": s["description"],
                              "parameters": s["input_schema"]}} for s in schemas]

    def _client(self):
        import ollama
        return ollama.AsyncClient(host=self.host) if self.host else ollama.AsyncClient()

    def list_models(self) -> list:
        import ollama
        listing = ollama.list() if not self.host else ollama.Client(host=self.host).list()
        entries = listing.get("models", []) if isinstance(listing, dict) \
            else getattr(listing, "models", [])
        models = []
        for entry in entries:
            name = entry.get("model", entry.get("name")) if isinstance(entry, dict) \
                else getattr(entry, "model", None)
            size = entry.get("size", 0) if isinstance(entry, dict) else getattr(entry, "size", 0)
            if not name:
                continue
            detail = f"{size / 1024 ** 3:.1f}GB" if size >= 1024 ** 3 else \
                (f"{size / 1024 ** 2:.0f}MB" if size else "")
            models.append({"name": name, "detail": detail})
        return models

    async def stream(self, messages: list, max_tokens: int | None = None,
                     tools: list | None = None):
        from simple_harness import config
        options = {"num_ctx": config.NUM_CTX,
                   "num_predict": token_budget(max_tokens)}
        request = {"model": self.model, "messages": messages,
                   "stream": True, "options": options}
        if tools:
            request["tools"] = self.encode_tools(tools)
        response = await self._client().chat(**request)
        async for chunk in response:
            message = chunk.get("message") or {}
            text = message.get("content", "") or ""
            thinking = message.get("thinking", "") or ""
            if thinking:
                yield {"thinking": thinking}
            if text:
                yield {"text": text}
            for call in _ollama_calls(message):
                yield call
            if chunk.get("done"):
                yield {"done": True,
                       "prompt_tokens": chunk.get("prompt_eval_count", 0) or 0,
                       "completion_tokens": chunk.get("eval_count", 0) or 0,
                       "total_seconds": (chunk.get("total_duration", 0) or 0) / 1e9,
                       "eval_seconds": (chunk.get("eval_duration", 0) or 0) / 1e9}


class AnthropicProvider(Provider):
    name = "anthropic"
    label = "Anthropic"
    key_env = ("ANTHROPIC_API_KEY",)
    key_help = "console.anthropic.com -> API keys"
    default_base_url = "https://api.anthropic.com"
    api_version = "2023-06-01"
    supports_native_tools = True

    def encode_tools(self, schemas: list) -> list:
        # The canonical shape is already Anthropic's, so this is a copy rather
        # than a translation - kept explicit so the three stay symmetrical.
        return [{"name": s["name"], "description": s["description"],
                 "input_schema": s["input_schema"]} for s in schemas]

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key,
                "anthropic-version": self.api_version,
                "content-type": "application/json"}

    def list_models(self) -> list:
        data = self._get_json(f"{self.base_url}/v1/models?limit=100", self._headers())
        return [{"name": item["id"], "detail": item.get("display_name", "")}
                for item in data.get("data", []) if item.get("id")]

    def stream(self, messages: list, max_tokens: int | None = None,
               tools: list | None = None):
        system, conversation = split_system(messages)
        payload = {
            "model": self.model,
            # Anthropic requires max_tokens; it is a cap, not a target.
            "max_tokens": token_budget(max_tokens),
            "messages": merge_runs(conversation) or [{"role": "user", "content": "."}],
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self.encode_tools(tools)

        def chunks():
            response = self._post_sse(f"{self.base_url}/v1/messages",
                                      self._headers(), payload)
            prompt_tokens = completion_tokens = 0
            pending = _PartialCalls()
            started = time.time()
            for _event, data in iter_sse(response):
                if not data.strip():
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue          # a bare value: nothing to read from it
                kind = event.get("type")
                if kind == "message_start":
                    usage = (event.get("message") or {}).get("usage") or {}
                    prompt_tokens = usage.get("input_tokens", 0) or 0
                elif kind == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        pending.start(event.get("index"), block.get("name", ""),
                                      block.get("id", ""))
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "thinking_delta":
                        yield {"thinking": delta.get("thinking", "")}
                    elif delta.get("type") == "input_json_delta":
                        pending.feed(event.get("index"), delta.get("partial_json", ""))
                    elif delta.get("text"):
                        yield {"text": delta["text"]}
                elif kind == "content_block_stop":
                    finished = pending.take(event.get("index"))
                    if finished:
                        yield finished
                elif kind == "message_delta":
                    completion_tokens = (event.get("usage") or {}).get(
                        "output_tokens", completion_tokens) or completion_tokens
                elif kind == "error":
                    detail = (event.get("error") or {}).get("message", "unknown error")
                    raise RuntimeError(f"Anthropic: {detail}")
            yield from pending.drain()      # a stream cut short still reports
            elapsed = time.time() - started
            yield {"done": True, "prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens,
                   "total_seconds": elapsed, "eval_seconds": elapsed}

        return _as_stream(chunks)


class OpenAIProvider(Provider):
    name = "openai"
    label = "OpenAI"
    key_env = ("OPENAI_API_KEY",)
    key_help = "platform.openai.com -> API keys"
    default_base_url = "https://api.openai.com/v1"
    supports_native_tools = True

    def encode_tools(self, schemas: list) -> list:
        return [{"type": "function",
                 "function": {"name": s["name"], "description": s["description"],
                              "parameters": s["input_schema"]}} for s in schemas]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def list_models(self) -> list:
        data = self._get_json(f"{self.base_url}/models", self._headers())
        names = sorted(item["id"] for item in data.get("data", []) if item.get("id"))
        return [{"name": name, "detail": ""} for name in names]

    def stream(self, messages: list, max_tokens: int | None = None,
               tools: list | None = None):
        payload = {
            "model": self.model,
            "messages": merge_runs(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": token_budget(max_tokens),
        }
        if tools:
            payload["tools"] = self.encode_tools(tools)

        def chunks():
            try:
                response = self._post_sse(f"{self.base_url}/chat/completions",
                                          self._headers(), payload)
            except RuntimeError as error:
                # Older models and most OpenAI-compatible servers only know the
                # original parameter name.
                if "max_completion_tokens" not in str(error):
                    raise
                retry = dict(payload)
                retry["max_tokens"] = retry.pop("max_completion_tokens")
                response = self._post_sse(f"{self.base_url}/chat/completions",
                                          self._headers(), retry)

            prompt_tokens = completion_tokens = 0
            pending = _PartialCalls()
            started = time.time()
            for _event, data in iter_sse(response):
                data = data.strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue          # a bare value: nothing to read from it
                if event.get("error"):
                    raise RuntimeError(f"OpenAI: {event['error'].get('message', 'error')}")
                usage = event.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens) or prompt_tokens
                    completion_tokens = usage.get("completion_tokens",
                                                  completion_tokens) or completion_tokens
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        yield {"thinking": delta["reasoning_content"]}
                    if delta.get("content"):
                        yield {"text": delta["content"]}
                    for call in delta.get("tool_calls") or []:
                        # `index` is what ties the fragments of one call
                        # together; the id and name arrive only in the first.
                        key = call.get("index", 0)
                        function = call.get("function") or {}
                        pending.start(key, function.get("name", ""), call.get("id", ""))
                        pending.feed(key, function.get("arguments", ""))
            # OpenAI marks the end with finish_reason rather than a per-call
            # stop event, so the calls are read out once the stream is over.
            yield from pending.drain()
            elapsed = time.time() - started
            yield {"done": True, "prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens,
                   "total_seconds": elapsed, "eval_seconds": elapsed}

        return _as_stream(chunks)


class GeminiProvider(Provider):
    name = "gemini"
    label = "Google Gemini"
    key_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    key_help = "aistudio.google.com -> Get API key"
    default_base_url = "https://generativelanguage.googleapis.com"
    supports_native_tools = True

    def encode_tools(self, schemas: list) -> list:
        """One declaration per tool, inside the single `tools` entry Gemini takes.

        Gemini validates the schema against its own OpenAPI subset and rejects
        a parameter object with nothing in it, so a tool that takes no
        arguments is declared without a schema at all.
        """
        declarations = []
        for schema in schemas:
            entry = {"name": schema["name"], "description": schema["description"]}
            parameters = schema["input_schema"]
            if parameters.get("properties"):
                entry["parameters"] = {
                    "type": "object",
                    "properties": parameters["properties"],
                }
                if parameters.get("required"):
                    entry["parameters"]["required"] = parameters["required"]
            declarations.append(entry)
        return [{"functionDeclarations": declarations}]

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def list_models(self) -> list:
        data = self._get_json(f"{self.base_url}/v1beta/models?pageSize=200",
                              self._headers())
        models = []
        for item in data.get("models", []):
            name = str(item.get("name", "")).removeprefix("models/")
            methods = item.get("supportedGenerationMethods") or []
            if name and (not methods or "generateContent" in methods):
                models.append({"name": name, "detail": item.get("displayName", "")})
        return models

    def stream(self, messages: list, max_tokens: int | None = None,
               tools: list | None = None):
        system, conversation = split_system(messages)
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m.get("content", "")}]}
                    for m in merge_runs(conversation)]
        payload = {
            "contents": contents or [{"role": "user", "parts": [{"text": "."}]}],
            "generationConfig": {"maxOutputTokens": token_budget(max_tokens)},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = self.encode_tools(tools)

        url = (f"{self.base_url}/v1beta/models/{self.model}"
               ":streamGenerateContent?alt=sse")

        def chunks():
            response = self._post_sse(url, self._headers(), payload)
            prompt_tokens = completion_tokens = 0
            started = time.time()
            for _event, data in iter_sse(response):
                if not data.strip():
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue          # a bare value: nothing to read from it
                if event.get("error"):
                    raise RuntimeError(f"Gemini: {event['error'].get('message', 'error')}")
                usage = event.get("usageMetadata") or {}
                prompt_tokens = usage.get("promptTokenCount", prompt_tokens) or prompt_tokens
                completion_tokens = usage.get("candidatesTokenCount",
                                              completion_tokens) or completion_tokens
                for candidate in event.get("candidates") or []:
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        call = part.get("functionCall")
                        if call and call.get("name"):
                            # Gemini sends a call whole, so there is nothing to
                            # accumulate and nothing that can arrive half-parsed.
                            arguments = call.get("args")
                            yield {"tool_call": {
                                "name": call["name"], "id": "",
                                "arguments": arguments if isinstance(arguments, dict) else {}}}
                            continue
                        text = part.get("text")
                        if not text:
                            continue
                        if part.get("thought"):
                            yield {"thinking": text}
                        else:
                            yield {"text": text}
            elapsed = time.time() - started
            yield {"done": True, "prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens,
                   "total_seconds": elapsed, "eval_seconds": elapsed}

        return _as_stream(chunks)


PROVIDERS = {p.name: p for p in
             (OllamaProvider, AnthropicProvider, OpenAIProvider, GeminiProvider)}


# ---------------------------------------------------------------------------
# what is connected
# ---------------------------------------------------------------------------

_state: dict = {}
_active: Provider | None = None


def load_state(force: bool = False) -> dict:
    global _state
    if _state and not force:
        return _state
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("active", "ollama")
    data.setdefault("providers", {})
    _state = data
    return _state


def save_state() -> str:
    """Write the config with owner-only permissions - it holds API keys."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # `private` keeps the key owner-only for the whole write. Creating the file
    # and chmodding it afterwards leaves a moment where the key is readable.
    atomic.write_json(CONFIG_PATH, _state, private=True)
    return CONFIG_PATH


@contextlib.contextmanager
def using_model(model: str):
    """Run a block against a different model on the current provider.

    Both places a model can be named have to move together: the provider's own
    saved setting, which wins, and `config.MODEL`, which the display and the
    session file read. Restoring is in a `finally` because a sub-agent that
    raises must not leave the assistant talking to the wrong model.
    """
    from simple_harness import config
    provider = current()
    settings = settings_for(provider.name)
    had = "model" in settings
    previous_setting = settings.get("model")
    previous_config = config.MODEL
    try:
        if model:
            settings["model"] = model
            config.MODEL = model
        yield current()
    finally:
        if model:
            if had:
                settings["model"] = previous_setting
            else:
                settings.pop("model", None)
            config.MODEL = previous_config
            _active_cache_clear()


def _active_cache_clear() -> None:
    global _active
    _active = None


def settings_for(name: str) -> dict:
    return load_state()["providers"].setdefault(name, {})


def build(name: str) -> Provider | None:
    factory = PROVIDERS.get(name)
    return factory(settings_for(name)) if factory else None


def forget_key(name: str) -> tuple:
    """Delete a provider's saved API key. Returns (removed, detail).

    A key could be typed in and never taken back out: `_ensure_key` only asks
    when there is none, so a key pasted into the wrong provider, or one that
    has since been revoked, stayed in `providers.json` with nothing in the
    program able to remove it.

    Only the key goes. The model and base_url beside it are not secrets and
    are what makes reconnecting one step instead of three. An environment
    variable is not touched either - it is not ours to unset, and saying so is
    more use than quietly appearing to have done something.
    """
    if name not in PROVIDERS:
        return False, f"unknown provider '{name}' - try {', '.join(PROVIDERS)}"
    settings = settings_for(name)
    if not settings.pop("api_key", None):
        return False, ""
    save_state()
    # The live provider was built around the dict that just changed, but a
    # rebuild is what guarantees it rather than what happens to be true.
    _active_cache_clear()
    return True, CONFIG_PATH


def current() -> Provider:
    """The provider in use. Falls back to Ollama, which needs no account."""
    global _active
    if _active is None:
        _active = build(load_state().get("active", "ollama")) or OllamaProvider({})
    return _active


def connect(name: str, model: str = "", api_key: str = "",
            base_url: str = "") -> tuple:
    """Point the harness at a provider. Returns (provider, problem)."""
    global _active
    if name not in PROVIDERS:
        return None, f"unknown provider '{name}' - try {', '.join(PROVIDERS)}"

    settings = settings_for(name)
    if api_key:
        settings["api_key"] = api_key
    if base_url:
        settings["base_url"] = base_url
    if model:
        settings["model"] = model

    provider = build(name)
    load_state()["active"] = name
    _active = provider
    _sync_config(provider)
    save_state()
    return provider, provider.ready()


def _sync_config(provider: Provider) -> None:
    """Keep `config.MODEL` in step - the TUI and session files both read it."""
    try:
        from simple_harness import config
        config.MODEL = provider.model or config.MODEL
    except Exception:
        pass


async def complete(messages: list, max_tokens: int) -> str:
    """One short non-streamed answer - used for session titles and summaries."""
    pieces = []
    async for chunk in current().stream(messages, max_tokens=max_tokens):
        if chunk.get("text"):
            pieces.append(chunk["text"])
    return "".join(pieces).strip()


def status_line() -> str:
    provider = current()
    problem = provider.ready()
    if problem:
        return f"{provider.label} ({problem})"
    where = f" · key {provider.key_source}" if provider.key_source else ""
    return f"{provider.label} · {provider.model}{where}"


def apply_startup() -> None:
    """Restore the last connection when the app starts."""
    _sync_config(current())


if __name__ == "__main__":
    print("This file can not run directly.")
