# What does a screenshot cost?

This page estimates what it costs to identify one screenshot in each mode, explains where the money goes, and describes the cost controls Keeper applies by default. It also shows how to check the estimate against what your server actually measures.

> **These are estimates.** They are built from the size of the request Keeper sends, Anthropic list prices, and assumptions about how often Claude searches the web. Keeper records the real usage of every analysis (see [Measuring real costs](#measuring-real-costs)). After a week of normal use, trust the **Usage & cost** panel over this page.

## Summary

With the defaults (hybrid mode, Claude Opus 5, batch processing, effort `medium`, 8k-token cap on fetched pages):

| Mode | Per screenshot | 300 screenshots / month (~10/day) |
|---|---|---|
| **`hybrid`** (default: local model first, Claude only when it isn't sure) | **~$0.05** average, if ~25% go to Claude | **~$15** |
| `claude` (every screenshot to Claude, batched) | ~$0.06 easy · **~$0.21 typical** · up to ~$0.70 | ~$20–65 |
| `claude` in real time (`KEEPER_CLAUDE_BATCH=false`) | ~$0.10 easy · ~$0.40 typical · up to ~$1.30 | ~$35–120 |
| `local` (your own model only) | ~$0.0002–0.001 in electricity | < $1 |
| `ocr` (no model) | ~$0 (about 1 s of CPU) | $0 |

Not included: hosting and GPU hardware. The metadata services Keeper uses (TMDB, GitHub, Open Library, and OMDb's free tier of 1,000 requests a day) cost nothing at this volume.

Before these cost controls, a typical Claude screenshot was estimated at **~$0.45** and the worst case at **~$1.50+**. See [Before and after](#before-and-after).

## Where the Claude cost comes from

### 1. The fixed part of every request: ~5,600 input tokens

| Part | Tokens | Notes |
|---|---|---|
| Screenshot | ~2,460 | Image tokens ≈ width × height / 750. A 1170×2532 iPhone screenshot is downscaled to 924×2000 before sending. A 1920×1080 desktop screenshot is ~2,770. |
| `save_analysis` result schema | ~1,780 | The structured form Claude fills in (category, title, links, details, confidence, alternatives…) |
| Instructions | ~420 | System prompt |
| Web search + fetch tool definitions | ~700 | Estimated |
| OCR hints | ~50–300 | The text Keeper's OCR read, plus the rule-based clues |
| **Total** | **~5,600** | ≈ $0.028 at $5/M (half of that in a batch) |

### 2. Web research, the multiplier

Claude doesn't just look at the image. It searches the web to confirm what it sees and to find the canonical page (the IMDb title, the GitHub repo, the original recipe).

- **Each search costs $0.01** ($10 per 1,000).
- **Each search adds its results to the conversation** (~2–4k tokens). Each fetched page adds up to `KEEPER_FETCH_MAX_TOKENS` (8k by default).
- **Claude re-reads the growing conversation after every step.** We assume every step is billed as input again, which is why input tokens grow faster than linearly.

| Scenario | Searches / fetches | Input tokens | Output tokens |
|---|---|---|---|
| **Easy**: a legible title, e.g. "Past Lives" on a poster | 1 / 0 | ~14k | ~1k |
| **Typical**: a caption mention, needs confirming | 3 / 1 | ~63k | ~2k |
| **Hard**: an obscure or ambiguous item; Keeper's limits are 6 searches and 3 fetches | 6 / 3 | ~220k | ~5k |

### 3. Output: reasoning plus the filled-in form

Output costs $25 per million tokens (Opus 5). It is Claude's (adaptive) thinking plus the ~500–1,000-token result. With effort `medium` we expect ~1–5k tokens, or $0.03–0.13 at real-time prices.

### Worked example: typical screenshot, defaults

```
input   63,000 tokens × $5  / 1M = $0.315
output   2,000 tokens × $25 / 1M = $0.050
                                   ------
tokens                             $0.365  × 0.5 (batch) = $0.183
web searches   3 × $0.01                                 = $0.030
                                                          ------
                                                          ≈ $0.21
```

## Cost controls (on by default)

| # | Control | Setting | Effect |
|---|---|---|---|
| 1 | **Cap on fetched pages** | `KEEPER_FETCH_MAX_TOKENS=8000` | An IMDb, recipe or news page can be 20–40k tokens, and it is re-read on every later step. The cap limits the hard cases: without it, the worst case could exceed $2. Little accuracy is lost, because the facts Keeper needs are near the top of those pages, and the enrichers look up ratings and ingredients separately. |
| 2 | **Lower effort** | `KEEPER_EFFORT=medium` | Less thinking, so fewer output tokens and fewer, more targeted tool calls. Identifying a screenshot is not deep reasoning; raise it to `high` if the review rate climbs. |
| 4 | **Batch processing** | `KEEPER_CLAUDE_BATCH=true` | New screenshots go through the Message Batches API: **50% off all tokens**. Results usually arrive within an hour (max 24 h). Web searches are billed at the normal rate. Re-analyses and corrections always run immediately, because you're waiting for them. If a batch request fails or expires, Keeper runs it in real time instead of leaving it stuck. |
| 5 | **Hybrid mode** | `KEEPER_ANALYZER=hybrid`, `KEEPER_ESCALATE_BELOW=70` | Your local vision model tries first. Only answers below 70% confidence, or failures, go to Claude, and those go in a batch too. If the local server is unreachable, Keeper uses Claude and retries the local server after 5 minutes. |

Also on by default: the OCR pre-pass (it lets small local models get more screenshots right, which means fewer escalations), and downscaling screenshots to a 2000 px long edge.

### Not applied (your call)

- **A cheaper Claude model.** `KEEPER_MODEL=claude-sonnet-5` is $2 / $10 per million tokens, **60% below Opus 5**, which would make a typical batched screenshot roughly **$0.09**. It may be less accurate on hard cases. Watch the *Needs review* rate in the app if you try it. See [MODELS.md](MODELS.md).
- **Lower confidence threshold.** `KEEPER_ESCALATE_BELOW=60` sends fewer screenshots to Claude and accepts more local answers as they are.

## Before and after

| | Real time, no controls | Defaults (batch + medium + fetch cap) |
|---|---|---|
| Easy | ~$0.12 | ~$0.06 |
| Typical | ~$0.45 | ~$0.21 |
| Hard | ~$1.50+ (uncapped pages) | ~$0.70 |
| + hybrid, ~25% escalated | – | **~$0.05 average** |

## Local and OCR costs

- **OCR (RapidOCR):** ~1 s of CPU per screenshot. Effectively free.
- **Local LLM:** only the power the machine uses. A GPU box drawing 350 W at $0.20/kWh costs **$0.07/hour**. At ~5–10 s per screenshot on a GPU, that is **~$0.0002** per screenshot. On CPU it takes 1–3 minutes, still well under $0.01. Set `KEEPER_LOCAL_COST_PER_HOUR=0.07` (your number) to include it in the usage report.
- **Hardware:** a used 12–24 GB GPU is a one-off cost. At ~$0.21 per Claude screenshot, a $600 GPU pays for itself after roughly 3,000 screenshots it keeps away from Claude.

## Measuring real costs

Keeper records every analyzer run: model, mode (realtime, batch or local), input, output and cache tokens, web searches and fetches, duration, and cost at list prices. This includes runs that failed and screenshots you later deleted.

- **Web app / PWA:** sidebar → **$ Usage & cost**. Shows total, per screenshot, the share sent to Claude, a 30-day projection, a per-analyzer breakdown and a daily chart. Each item's details show what it cost and how it was identified (`ocr → local:qwen3-vl:8b → claude`).
- **iOS app:** Settings → *Usage & cost*, plus the per-item cost in its details.
- **API:** `GET /api/usage?days=30`

```json
{
  "per_screenshot_usd": 0.052,
  "claude_share": 0.24,
  "projected_30d_usd": 15.6,
  "totals": { "screenshots": 300, "cost_usd": 15.6, "web_searches": 210, "...": "..." },
  "by_analyzer": [ { "analyzer": "claude", "mode": "batch", "avg_cost_usd": 0.2, "...": "..." }, "..." ]
}
```

Costs are computed from the `usage` the API returns on every response. Prices live in `keeper/usage.py`. If you have negotiated prices, or a model isn't listed, override them with `KEEPER_PRICING='{"claude-opus-5": [5, 25]}'` (USD per million input and output tokens).

### Checking the estimate

After some real use, compare `by_analyzer[claude].input_tokens / runs` with the scenarios above:

- **Much higher than ~60k:** Claude is searching and fetching more than assumed. Lower `KEEPER_FETCH_MAX_TOKENS`, or check whether one category (obscure recipes, say) drives it.
- **`claude_share` above ~30% in hybrid mode:** the local model is often unsure. Try a larger local model (see [MODELS.md](MODELS.md)), or lower `KEEPER_ESCALATE_BELOW` if its answers turn out right anyway (check how often you use **Fix it**).
