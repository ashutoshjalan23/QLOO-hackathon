# RainCheck × Qloo

> **Hackathon submission** — A taste-powered group coordination bot for Hong Kong.
> Built on the [Qloo Hackathon Kit](https://github.com/qloo/qloo-hackathon-kit).

RainCheck turns a Telegram group chat into a finished restaurant booking — constraints
extracted from the conversation, options ranked by real cultural-taste intelligence, and
a phone call placed to the winning venue. Qloo is the layer that makes the
recommendations worth trusting: it knows that the people who like a certain band also
like a certain kind of food, without ever seeing anyone's personal data.

---

## What Qloo adds to this project

Without Qloo, RainCheck searches OpenStreetMap for restaurants near the stated
districts and re-ranks them by geography and cuisine keyword. That works, but it is
essentially a smarter Google Maps query.

With Qloo, the same shortlisting step becomes **taste-aware**:

| What the chat gives us | What Qloo turns it into |
|---|---|
| `prefer_cuisines: ["hotpot"]` | A ranked shortlist of entities whose audiences overlap the hotpot-fan profile in Hong Kong |
| Soft preferences ("somewhere fun", "a bit upscale") | Qloo tags matched to those phrases via `qloo_find_tags` |
| The group has liked Thai before | `qloo_recommend` with the Thai-restaurant entity as a signal → cross-category affinities |
| Three candidate restaurants from OSM | `qloo_rank` re-orders them by how well they fit the group's aggregate taste profile |
| "Where is this type of place most popular?" | `qloo_where_popular` with an entity and a bounding box → Hong Kong district-level heat map |

Qloo results describe **aggregate affinities**, not individual profiles. We never send
anyone's name, message text, or personal data to Qloo — only the structured facts that
`pipeline.py` already extracts (cuisine types, vibe words, district names).

---

## Architecture with Qloo

```
Telegram group chat
  messages, commands, votes
          │
          ▼
     bot.py ─────────────────────────┐
          │                          │
          ▼                          ▼
   pipeline.py                 private_inputs.py
   Gemini extraction           SQLite private requirements
          │
          ▼
   qloo_search.py   ◄── NEW ──────────────────────────────┐
   Qloo harness via CLI subprocess (qloo exec / qloo api)  │
          │                                                │
          ├── qloo_find_tags(vibe words, cuisine prefs)    │
          ├── qloo_recommend(cuisine signals, target=restaurant) ◄── replaces/augments Exa
          ├── qloo_rank(OSM candidate list, group taste profile) ◄── replaces relevance_rank
          └── qloo_where_popular(entity, within=HK bounding box)
          │
          ▼
   places.py (OSM / Overpass)
   Still the source of dialable phone numbers —
   Qloo names places; OSM makes them callable.
          │
          ▼
   Selected plan + human approval
          │
          ▼
   bridge/server.py :8080
          │
          ├── ElevenLabs operator call desk
          └── Vapi + Twilio outbound call
          │
          ▼
   Transcript + booking result + calendar link → Telegram
```

---

## Qloo integration points in detail

### 1. `qloo_find_tags` — soft preference → structured signal

**Where:** called from `pipeline.py` after `extract_constraints()` returns.

The chat might produce `soft: [{constraint: "somewhere cosy and a bit upscale"}]`.
Qloo's `find_tags` workflow resolves that free text into validated Qloo tag IDs
(`ambience:cosy`, `price_band:mid-to-high`, etc.) that the downstream
`recommend` and `rank` calls can consume.

```
pipeline.py extracts: soft preference "somewhere fun and lively"
                       ↓
qloo_find_tags("fun and lively")
                       ↓
Qloo tags: ["ambience:lively", "crowd:social"]
                       ↓
passed as signals to qloo_recommend
```

---

### 2. `qloo_recommend` — taste-powered candidate generation

**Where:** replaces / augments `exa_search.py` in the `/decide` flow.

Currently Exa is called with a natural-language query built from the constraints.
`qloo_recommend` does the same job with cultural-taste intelligence:
given a cuisine or restaurant entity as a signal, it returns similar entities
(other restaurants with overlapping fan audiences), optionally filtered to a
category and a geographic scope.

```python
# Pseudo-call (actual implementation via qloo exec subprocess)
qloo_recommend(
    signals=["restaurant:hotpot-hk"],   # from prefer_cuisines
    target_type="restaurant",
    filters={"location": "Hong Kong"},
    tags=["ambience:lively"],            # from qloo_find_tags step
    limit=20
)
```

This returns restaurant names. We then cross-reference with the OSM/Overpass
cache to find dialable phone numbers — Qloo names the places, OSM makes them
callable. This separation is already enforced in `exa_search.py` (rule 2:
"it never claims a phone number") and carries directly into the Qloo layer.

---

### 3. `qloo_rank` — re-ranking the OSM pool by group taste

**Where:** replaces `places.relevance_rank()` as the final ranking step.

`relevance_rank()` today ranks by cuisine keyword match and geographic proximity.
`qloo_rank` takes the same candidate list and re-orders it by how well each
option fits the group's aggregate taste profile:

```python
# Pipeline today:
candidates = places.relevance_rank(osm_rows + exa_rows, constraints)[:60]

# With Qloo:
candidates = qloo_rank(
    options=[r["name"] for r in osm_rows],   # names from OSM
    option_type="restaurant",
    signals=["cuisine:hotpot"],               # from constraints
    tags=["ambience:lively"],                 # from qloo_find_tags
    limit=10
)
# Then re-join with OSM rows to recover phone numbers
```

This means the three options presented in the Telegram poll are ranked by
**cultural resonance**, not just by which district they happen to be in.

---

### 4. `qloo_where_popular` — fair-meeting-point refinement

**Where:** augments `places.meeting_districts()` when travel origins are known.

When the group has stated multiple origins (Kai from Sha Tin, Ethan from Kennedy Town,
Jenny from Sheung Shui), `meeting_districts()` currently returns a geometric midpoint.
`qloo_where_popular` can tell us which HK district the relevant restaurant type is most
popular in, intersected with the geographic fairness constraint:

```python
qloo_where_popular(
    entity="restaurant:hotpot",
    within="Hong Kong",
    filters={"districts": fair_districts}   # from meeting_districts()
)
```

This upgrades "geographically fair" to "geographically fair AND where this kind
of outing actually thrives."

---

## Integration method: `qloo exec` subprocess

The harness exposes Qloo as a local CLI command (`qloo exec find_tags --input-file
request.json`). The Python bot calls this as a subprocess, which means:

- **No pip dependency** — the harness is an npm global, not a Python package.
  The project's zero-pip-install principle is preserved.
- **Standard library only on the Python side** — `subprocess.run`, `json.loads`.
- **Credential isolation** — the API key lives in the harness's private config or in
  `QLOO_API_KEY` env var; it never appears in Python source or in `.env`.
- **Same fallback design** — if `qloo exec` returns a non-zero exit or empty JSON,
  the new `qloo_search.py` module returns `[]`, and the existing OSM/Exa path
  takes over. Qloo is an enhancement, never a dependency.

```python
# Sketch of qloo_search.py (not yet written)
import json, subprocess

def _exec(workflow: str, payload: dict) -> list:
    """Call qloo exec and return parsed results, or [] on any failure."""
    try:
        inp = json.dumps(payload)
        result = subprocess.run(
            ["qloo", "exec", workflow],
            input=inp, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout) or []
    except Exception:
        return []
```

---

## Setup

### Prerequisites

1. Python 3.10+
2. Node.js 22.19.0+ (for the Qloo harness)
3. A Telegram bot token
4. A Gemini API key
5. A Qloo event credential (issued at the hackathon)

### Install the Qloo harness

```bash
npm install --global @qloo/qloo-harness
qloo --version   # must be 0.1.26 or newer
qloo setup --qloo   # enter the event-provided credential
qloo setup --status  # verify
```

### Clone and configure

```bash
git clone https://github.com/AikagraGupta/convene-gl-hacks.git
cd convene-gl-hacks
cp .env.example .env
```

Fill in `.env`. Required at minimum:

```
TELEGRAM_TOKEN=...
GEMINI_API_KEY=...
# QLOO_API_KEY is managed by the harness, not this file
```

### Run

```bash
python bridge/server.py   # terminal 1
python bot.py             # terminal 2
```

The operator page is at http://localhost:8080/.

Run the preflight check before a live session:

```bash
python preflight.py
```

---

## Commands

```text
/decide       Extract the current plan, find Qloo-ranked options, and open a poll
/close        Close the poll and prepare the selected plan
/private      Create a private requirements link in direct messages
/negotiate    Set the time and deposit boundaries for the call
/status       Show the current outing and approval state
/who          Show remembered preferences and their source messages
/forget NAME  Remove one person's remembered preferences
/forget       Clear remembered preferences and start a fresh chat context
```

---

## Stack

| Area | Technology |
|---|---|
| Group coordination | Telegram Bot API and native polls |
| Conversation understanding | Gemini with OpenRouter fallback |
| **Taste-powered ranking** | **Qloo (via `@qloo/qloo-harness` CLI)** |
| Restaurant and place discovery | Overpass, OpenStreetMap |
| Private requirements | SQLite |
| Voice calls | ElevenLabs Agents or Vapi with Twilio |
| Local services | Python standard library + bridge on port 8080 |
| Calendar | Google Calendar event links |

---

## What we do NOT send to Qloo

Per the [Qloo safe use policy](https://github.com/qloo/qloo-hackathon-kit/blob/main/docs/SAFE_USE.md):

- No names, user IDs, or Telegram identifiers
- No raw chat messages
- No location histories or individual origins ("Priya is coming from Sha Tin")
- Cuisine types (`hotpot`, `Italian`) — aggregate cultural signals — YES
- Vibe phrases (`cosy`, `lively`) resolved to Qloo tags — YES
- District names as geographic filters, not tied to individuals — YES
- Restaurant names from OSM (public entity references) — YES

Qloo results are used only to rank and select from the candidate pool. The
group sees restaurant recommendations, not "Qloo scores" or any inference about
individual members.

---

## Tests

```bash
python tests/run.py
```

The suite covers conversation extraction, place ranking, cuisine matching,
private requirements, negotiations, Telegram commands, voice outcomes,
transcripts, calendar links, and bridge contracts.

---

## License

MIT
