"""call_claude + the shared TOKENS accumulator (notebook cell 6; model name,
thinking setting and budget backstop read late-bound from markut.config).

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

TOKENS = {"input": 0, "output": 0, "calls": 0}


class ModelRefusal(RuntimeError):
    """The model declined the request (stop_reason == "refusal")."""


def _request_kwargs(system_prompt: str, user_content: str, max_tokens: int) -> dict:
    kwargs = {"model": config.MODEL_NAME, "max_tokens": max_tokens, "system": system_prompt,
              "messages": [{"role": "user", "content": user_content}]}
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


def call_claude(system_prompt: str, user_content: str, max_tokens: int = 1000) -> str:
    # HARD BACKSTOP (execution guardrail, belt-and-suspenders): even if graph
    # routing somehow kept looping past the soft budget check in
    # should_continue, no call may START once spend reaches 2x TOKEN_BUDGET.
    # Raising here is the fail-closed floor — a crashed run costs strictly
    # less than an unbounded one. TOKEN_BUDGET lives in the RUN CONFIGURATION
    # cell; the name resolves at call time, and every real call happens after
    # that cell has run.
    spent = TOKENS["input"] + TOKENS["output"]
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
            response = client.messages.create(**_request_kwargs(system_prompt, user_content, max_tokens))
            # WHY: only count usage on SUCCESS, so a failed+retried call isn't
            # double-billed in our totals. usage is on the response object.
            TOKENS["input"] += response.usage.input_tokens
            TOKENS["output"] += response.usage.output_tokens
            TOKENS["calls"] += 1
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

# Legacy demo evidence kept only for reference. The live graph now uses fetch_fundamentals().
EVIDENCE = """
[DEMO ONLY]
- This placeholder evidence is not used by research_node while live FMP data is enabled.
"""


def reset():
    # mirrors the notebook run cell: zero the accumulator before app.invoke
    # so the USAGE line reflects only the current run
    TOKENS["input"] = 0
    TOKENS["output"] = 0
    TOKENS["calls"] = 0
