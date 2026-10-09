"""call_claude + the shared TOKENS accumulator (notebook cell 6; model name,
thinking setting and budget hard stop read late-bound from markut.config).

Sonnet 5.5 notes (why this differs from the notebook's one-liner):
- thinking is ON by default on this model; we send the explicit setting from
  config so cost and max_tokens semantics are deliberate, not accidental
- the reply is read by block TYPE: with thinking enabled a response can start
  with a thinking block, so content[0].text is no longer the answer
- a safety refusal is a normal 200 with stop_reason "refusal"; it is raised
  as ModelRefusal and NOT retried (retrying a refusal just refuses again)
"""
import time

from anthropic import Anthropic

from markut import config

client = Anthropic()

# input = uncached input tokens; cache_write/cache_read = the evidence prefix
# written once per run (1.25x price) and read on every later call (0.1x).
TOKENS = {"input": 0, "output": 0, "calls": 0, "cache_write": 0, "cache_read": 0}
# stop_reason of the most recent successful call ("end_turn" | "max_tokens" |
# ...). WHY a module global: the notebook-era nodes read call_claude's string
# return; exposing the stop reason beside it lets bull/bear detect a reply cut
# off at the output cap without changing the call signature every test stubs.
LAST_STOP_REASON = "end_turn"


class ModelRefusal(RuntimeError):
    """The model declined the request (stop_reason == "refusal")."""


def _request_kwargs(system_prompt: str, user_content: str, max_tokens: int, schema: dict = None,
                    cached_prefix: str = None) -> dict:
    # PROMPT CACHING (cost fix after run #6): the evidence packet is identical
    # for every call of a debate, so it goes FIRST in the system blocks with a
    # cache marker; the agent's role prompt follows. Render order is tools ->
    # system -> messages, so bull, bear, judge and governor all share one
    # cached prefix — one write per run, reads at a tenth of the price after.
    if cached_prefix:
        system = [{"type": "text", "text": cached_prefix, "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": system_prompt}]
    else:
        system = system_prompt
    kwargs = {"model": config.MODEL_NAME, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user_content}]}
    if schema is not None:
        # structured output: the API guarantees the text block is JSON valid
        # against this schema (shape); callers still validate substance
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    if config.THINKING_EFFORT:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": config.THINKING_EFFORT}
    else:
        kwargs["thinking"] = {"type": "between_tools"}   # lowest setting = no extended thinking
    return kwargs


def _text_of(response) -> str:
    # join the TEXT blocks only — thinking blocks (empty under the default
    # display) must never leak into a bull case or a judge's JSON
    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text")


def _complete(kwargs: dict) -> str:
    # HARD STOP (execution guardrail, belt-and-suspenders): even if graph
    # routing somehow kept looping past the soft budget check in
    # should_continue, no call may START once spend reaches 2x TOKEN_BUDGET.
    # Raising here is the fail-closed floor — a crashed run costs strictly
    # less than an unbounded one.
    spent = TOKENS["input"] + TOKENS["output"] + TOKENS["cache_write"] + TOKENS["cache_read"]
    if spent >= 2 * config.TOKEN_BUDGET:
        raise RuntimeError(
            f"call_claude hard-stop: {spent:,} tokens spent >= 2x TOKEN_BUDGET ({config.TOKEN_BUDGET:,})")
    # WHY (retry loop): a single transient network / rate-limit blip should not kill
    # a whole multi-round debate. We try twice with a short pause between, then give
    # up loudly. Two attempts is a deliberate floor: enough to survive a blip,
    # cheap enough that a truly-down API fails fast instead of hanging.
    last_error = None
    for attempt in range(2):
        try:
            response = client.messages.create(**kwargs)
            # WHY: only count usage on SUCCESS, so a failed+retried call isn't
            # double-billed in our totals. usage is on the response object.
            TOKENS["input"] += response.usage.input_tokens
            TOKENS["output"] += response.usage.output_tokens
            TOKENS["cache_write"] += getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            TOKENS["cache_read"] += getattr(response.usage, "cache_read_input_tokens", 0) or 0
            TOKENS["calls"] += 1
            global LAST_STOP_REASON
            LAST_STOP_REASON = getattr(response, "stop_reason", "end_turn") or "end_turn"
            # one line per call: where the tokens went (uncached / cache write / cache read) and how it stopped
            print(f"CALL: in={response.usage.input_tokens} cache_w={getattr(response.usage, 'cache_creation_input_tokens', 0) or 0} "
                  f"cache_r={getattr(response.usage, 'cache_read_input_tokens', 0) or 0} out={response.usage.output_tokens} "
                  f"stop={LAST_STOP_REASON}")
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                raise ModelRefusal(f"model refused the request (category: {category})")
            return _text_of(response)
        except ModelRefusal:
            raise  # deliberate: a refusal is not a transient error
        except Exception as e:
            last_error = e
            if attempt == 0:
                print(f"call_claude: attempt 1 failed ({e}); retrying in 2s...")
                time.sleep(2)  # brief backoff so we don't immediately re-hit a rate limit
    # WHY (raise after 2nd failure): surface a clear error instead of returning
    # empty text, which would silently poison a bull/bear/judge argument downstream.
    raise RuntimeError(f"call_claude failed after 2 attempts: {last_error}")


def call_claude(system_prompt: str, user_content: str, max_tokens: int = 1000, schema: dict = None,
                cached_prefix: str = None) -> str:
    return _complete(_request_kwargs(system_prompt, user_content, max_tokens, schema, cached_prefix))


def call_claude_images(system_prompt: str, user_content: str, images: list, max_tokens: int = 3000) -> str:
    """One call with image blocks ahead of the text (used to transcribe
    image-only filing exhibits). images = [(bytes, media_type), ...]."""
    import base64
    content = [{"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.b64encode(blob).decode("ascii")}}
               for blob, media_type in images]
    content.append({"type": "text", "text": user_content})
    kwargs = _request_kwargs(system_prompt, user_content, max_tokens)
    kwargs["messages"] = [{"role": "user", "content": content}]
    return _complete(kwargs)

# Legacy demo evidence kept only for reference. The live graph now uses fetch_fundamentals().
EVIDENCE = """
[DEMO ONLY]
- This placeholder evidence is not used by research_node while live FMP data is enabled.
"""


def reset():
    # mirrors the notebook run cell: zero the accumulator before app.invoke
    # so the USAGE line reflects only the current run
    for k in TOKENS:
        TOKENS[k] = 0
