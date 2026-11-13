# OpenCode Go

OpenCode Go is a subscription gateway in front of many vendors' models: it speaks the OpenAI API, so TradingAgents reaches it through its OpenAI-compatible client.

## Selecting the provider

Choose **OpenCode Go (subscription)** in the CLI's provider menu, or set `llm_provider: "opencode-go"` in the configuration. The provider can also be chosen with `TRADINGAGENTS_LLM_PROVIDER=opencode-go`, which skips the provider question.

The API key comes from the OpenCode Console under Settings → Service accounts. Put it in `OPENCODE_GO_API_KEY`; the interactive CLI prompts for it when it is missing and saves it to `.env`, and a run without a terminal stops with an error naming the variable.

The model picker lists a few models per tier plus a **Custom model ID** entry. Any model ID the gateway serves is accepted, so a newly added model can be named directly in `quick_think_llm` or `deep_think_llm`.

## Client identity and session header

The gateway's docs ask every client to do two things a plain OpenAI client does not:

- identify itself by name rather than as a generic SDK, and
- send a stable `x-opencode-session` id per conversation, so requests route consistently and prompt caching can hit.

TradingAgents sends both. The identity is `User-Agent: TradingAgents/<version>`. The session id is generated once per process and reused for every request from that process rather than regenerated per call, because a fresh id per request would defeat both the routing affinity and the prompt cache the header exists for.

Set `OPENCODE_GO_SESSION_ID` to keep the id across processes. A scheduled job wants that: separate runs then share one conversation. Leave it unset and each process generates its own id.

## Endpoint

The official base URL is `https://opencode.ai/inference/go/openai/v1`, and that is the endpoint the provider resolves to when none is configured.

Set `backend_url` (or `TRADINGAGENTS_LLM_BACKEND_URL`) to pin any other endpoint the gateway offers. A tier that runs on its own provider can name its endpoint separately with `quick_think_backend_url` or `deep_think_backend_url`.

## Reasoning depth

Reasoning effort is settable per model tier, because the two tiers serve different jobs:

- `TRADINGAGENTS_QUICK_THINK_EFFORT` (`quick_think_reasoning_effort`) covers the analysts, the debate, the trader and the risk team.
- `TRADINGAGENTS_DEEP_THINK_EFFORT` (`deep_think_reasoning_effort`) covers the Research and Portfolio Managers.

A tier that names none keeps `openai_reasoning_effort` (`TRADINGAGENTS_OPENAI_REASONING_EFFORT`); when that is unset too, nothing is sent, so the model runs at its own default. A tier's own value wins over the provider-wide one: `openai_reasoning_effort: "high"` with `deep_think_reasoning_effort: "max"` runs the quick tier at high and the deep tier at max.

The tier knobs are configuration and environment only. The interactive CLI has one provider-wide effort question (Step 8: Reasoning Effort), skipped when `TRADINGAGENTS_OPENAI_REASONING_EFFORT` is set; it does not prompt per tier.

The gateway advertises the levels each model accepts in `/v1/models`, under `capabilities.reasoning.effort`; as advertised at the time of writing, DeepSeek V4.1 Flash and GLM-5.3 take low/high/max, while DeepSeek V4 Pro takes high/max. The value is passed through as given, so any level the model advertises can be named in a setting, though the interactive menu itself offers low, medium and high.

Effort travels to OpenCode Go and native OpenAI only. Through the configuration it is forwarded for `openai`, `chatgpt` (the ChatGPT-plan sign-in, which also calls OpenAI) and `opencode-go`; every other OpenAI-compatible provider never sees the field, because an endpoint that does not know it may reject the request. On native OpenAI the model-name gate still drops the parameter for models that do not accept it; that gate does not apply to OpenCode Go, whose models advertise their own levels.
