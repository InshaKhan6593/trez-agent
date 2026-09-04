# Handoff — Trez WhatsApp Property Agent

State of the project as of **2026-09-04**. Read this before changing anything;
several decisions below look arbitrary until you know the bug that caused them.

See also **[ARCHITECTURE.md](ARCHITECTURE.md)** for the data pipeline and the
retrieval design rationale. This file covers the agent, WhatsApp and evals.

---

## 1. What works today

A WhatsApp agent that answers property questions from a Postgres database of
Trez Enterprises' live Zameen inventory, in English/Urdu/Roman Urdu.

```
customer WhatsApp message
   → Meta Cloud API webhook (FastAPI, app/webhook.py)
   → LangChain v1 create_agent  (agents/graph.py)
       tools: inventory_summary · find_location · find_properties · send_photos
   → reply rendered as WhatsApp text or an interactive list (agents/whatsapp.py)
   → photos uploaded from local disk to Meta on request
```

Verified end to end on a real phone: search, disambiguation, multi-turn memory,
interactive lists, photo delivery.

**Not built yet:** lead capture/scoring, voice-note transcription, seller flow
persistence, Postgres checkpointer (history is in-memory and dies on restart).

---

## 2. Run it

```bash
docker compose up -d                 # Postgres :5544 + ngrok tunnel :4041
uv run uvicorn app.webhook:app --host 0.0.0.0 --port 8000
```

Check the startup line says **4 tools**. If it says 3, the process is stale —
see §6.

```
INFO agent loaded with 4 tools: inventory_summary, find_location, find_properties, send_photos
```

Get the public URL for Meta's webhook config:

```bash
curl -s http://127.0.0.1:4041/api/tunnels
```

⚠️ The free ngrok URL **changes on every restart** and must be re-pasted into
the Meta console each time.

### Environment (`.env`, git-ignored)

```
OPENROUTER_API_KEY=          LANGSMITH_API_KEY=
DATABASE_URL=                LANGSMITH_TRACING=true
JUDGE_MODEL=openai/gpt-4o-mini
WHATSAPP_PHONE_NUMBER_ID=    WHATSAPP_ACCESS_TOKEN=
WHATSAPP_VERIFY_TOKEN=       WHATSAPP_TEST_RECIPIENT=
NGROK_AUTHTOKEN=
```

The WhatsApp token in use is a **temporary test token**. Replace it with a
System User token (never expires) before real use.

---

## 3. Layout

```
agents/
  db.py              pure DB functions — resolve_location, search_listings. NO LLM.
  tools.py           the 4 LangChain tools. Docstrings are prompts: the model reads them.
  graph.py           model choice + system prompt + build_agent() factory
  middleware.py      strips <think> reasoning from replies
  turn.py            isolates ONE conversational turn from thread history
  context.py         ContextVar holding the current WhatsApp recipient
  whatsapp.py        Pydantic models for Meta payloads + reply rendering
  whatsapp_inbound.py  webhook envelope parsing, voice-note fallback
  whatsapp_client.py   send / upload_media / mark_read / typing
  media.py           photo upload from local disk, media-id caching
app/webhook.py       FastAPI: verification handshake + inbound handling
etl/                 raw snapshot -> Postgres (see ARCHITECTURE.md)
evals/               goldens + two runners (see §5)
db/                  schema, auto-applied on first container start
```

---

## 4. Decisions that will look wrong until you read why

**The model never writes SQL.** It fills typed tool signatures; our code builds
parameterised queries. NL2SQL's dominant failure is inventing a filter value and
getting zero rows with no error — on WhatsApp a false "nothing available"
silently kills the lead.

**`purpose` ('sale'/'rent') is mandatory and never defaulted.** Rent and sale
share one price column. The cheapest "house" in the catalogue is a
₨1.35 lakh/month *rental* against a ₨7 crore sale floor — an ~800x error if
purpose leaks.

**Location resolution is trigram + rules, no LLM, no hand-written aliases.**
The gazetteer is built from Zameen's own breadcrumb location ids. An A/B test
showed LLM-generated aliases added nothing over rules + a 3-entry abbreviation
dictionary (both 12/13).

**Two guards in alias generation, both from real bugs:**
- never emit an all-generic alias — `"2 bed in bahria town"` matched *Gadap
  Town* at 1.00 on the shared word "town"
- never emit an alias equal to another node's name — `"Bahria Town Karachi"`
  would otherwise claim `karachi`

**ILIKE fallback after trigram.** Trigram compares whole strings, so a short
word inside a longer name scores badly: `similarity('town','gadap town')` is
below threshold. A customer asking *"town mai koi ghar hai?"* got "we don't
cover that area" while two real areas end in "Town". Now falls back to
`ILIKE '%town%'` and offers them.

**Photos are never sent without consent.** An earlier version auto-sent on row
tap. Tapping now only tells the agent which listing is meant; it offers, and
`send_photos` fires after the customer agrees.

**`send_photos(listing_id)` takes no phone number.** The recipient comes from a
ContextVar bound per turn, so the model cannot be talked into sending media
elsewhere. ContextVar (not a global) because webhook requests are concurrent.

**Local media files are sent without any hosting.** Meta's `/media` endpoint
accepts an upload and returns an id; we send `{"image":{"id":...}}` rather than
a URL. `{"image":{"link":...}}` would need a public host. Ids are cached in
`listing_media.meta_media_id` and refreshed after 25 days (Meta expires ~30).

**Only tool-returned ids are stripped from prose.** An earlier version removed
any bare 8-digit number and ate a real price: `"Rs 10750000 ka ghar"` became
`"Rs ka ghar"`.

---

## 5. Evaluation

```bash
uv run python -m evals.test_deterministic          # 19/19, free, milliseconds
uv run python -m evals.run_agent_evals --only A4   # one golden, local
uv run python -m evals.langsmith_eval --push       # refresh LangSmith dataset
uv run python -m evals.langsmith_eval              # full experiment
uv run python -m evals.langsmith_eval --no-judge   # deterministic only, free
```

Three layers, deliberately separate — when L3 fails you need to know whether
retrieval or the model was wrong:

| Layer | Tests | Cost |
|---|---|---|
| L1 location | `resolve_location` alone, exact id match | free |
| L2 search | `search_listings` alone, exact id-set match | free |
| L3 agent | tool choice, args, reply honesty | ~$0.006/pass |

**Most quality checks are deterministic, not judged**: invented listing ids
(regex the reply against tool output), WhatsApp formatting, reasoning leaks,
banned strings. LLM judging is reserved for groundedness.

**Judge = `openai/gpt-4o-mini`, agent = `qwen/qwen3.5-flash-02-23`.** The judge
must be stronger than what it judges, or it shares the agent's blind spots.

Two things not to repeat:
- **openevals' `create_llm_as_judge` does not work with ChatOpenRouter**
  (`KeyError: 'score'`). We use their prompt text — plain f-strings — with our
  own structured-output call.
- **openevals' `TOOL_SELECTION_PROMPT` is the wrong instrument here.** It
  assumes an agent should always call a tool, so it scored 0.00 every time the
  agent correctly asked a qualifying question. It read 0.30 while the
  deterministic check read 0.92.

DeepEval was tried and removed — hand-written GEval criteria drifted into
scoring "answer quality" and produced noise. Re-add it only with a concrete
reason.

---

## 6. Known issues and traps

**⚠️ `uvicorn --reload` silently misses edits on this project.** It once failed
to pick up a new tool and the agent kept saying *"main photos nahi bhej sakta"*
— which looked like a model refusal but was a stale process. Always restart
explicitly and check the "N tools" startup line.

**In-memory checkpointer.** `thread_id` is the phone number, so each customer
has their own history — but it dies on restart. Move to a Postgres checkpointer
before real traffic.

**24-hour window.** Meta only allows free-form replies within 24h of the
customer's last message. Lead follow-up outside that needs pre-approved
templates. Not built.

**Voice notes** get a canned Roman-Urdu reply asking the customer to type.
`media_id` is stored, so they can be replayed once transcription exists. This is
a real lead-loss risk in Karachi.

**No album view on WhatsApp** — each photo is a separate message. Capped at
`MAX_PHOTOS = 4`.

**3 plots have zero photos.** The agent must check the count before offering.

**Model quirks observed:** for *"budget 4 crore"* it once set both `min_price`
and `max_price` to 40000000, finding nothing when two 3.9 Cr flats matched. The
tool now drops a redundant `min_price` and says why. It also still emits
`**double asterisks**` occasionally; `to_whatsapp_text()` converts them.

---

## 7. Next

1. **Lead capture + rule-based qualification** — the actual product, and still
   entirely missing. Design agreed: harvest signals from tool args (they are
   already structured), hard gates (`sell`/`rent_out` always qualified; no
   `purpose` never qualified), then a weighted score. Deterministic, explainable
   to the client, re-scorable from stored signals.
2. Postgres checkpointer so history survives restarts.
3. Goldens for the two newest bugs: `min_price == max_price`, and the
   offer-then-send photo flow.
4. System User token; ngrok static domain.
