# RainCheck × Qloo

RainCheck turns a Telegram conversation into restaurant options, a group vote,
and an approved booking call. Qloo adds aggregate taste recommendations to
the existing OpenStreetMap and Exa search. The adapter is implemented in
[`qloo_search.py`](qloo_search.py), using `@qloo/qloo-harness` **0.1.26**.

## Setup

Use Python 3.10+ and Node.js 22.19.0+. Python needs no third-party packages.

```bash
git clone --branch QLOO https://github.com/ashutoshjalan23/QLOO-hackathon.git
cd QLOO-hackathon
cp .env.example .env
npm install --global @qloo/qloo-harness@0.1.26
qloo setup --qloo
qloo setup --status --json
```

On PowerShell, `Copy-Item .env.example .env` also works. Configure Telegram,
Gemini, and the chosen voice provider as described in the [main README](README.md).
Save the Qloo credential through setup, or supply `QLOO_API_KEY` in the server
environment. The adapter lets the harness read its own saved configuration.
Never commit credentials. A model-provider login is not needed for `qloo exec`.

```bash
python bridge/server.py   # terminal 1
python bot.py             # terminal 2
python preflight.py       # readiness checks
python tests/run.py       # mocked tests; no outbound calls
```

Preflight checks the harness version command and credential readiness without
making a Qloo API request. Missing Qloo setup produces a warning; OSM/Exa remain
the discovery fallback. It does not establish live restaurant coverage.

## Implemented flow

1. `pipeline.extract_constraints()` extracts the chat's requirements.
2. `bot.find_candidates()` gets the full OSM area/cuisine pool and optional Exa
   results, then calls `qloo_search.recommend()` with aggregate cuisine and vibe
   concepts. No speaker names, message quotes, or individual origins are sent.
3. Qloo names are joined to existing candidates by exact normalized name,
   preserving Unicode letters. Unmatched results have no phone number. Fuzzy
   name fragments never transfer a number between branches.
4. Geographic/cuisine relevance ranks the full pool before taking 60 rows.
5. Qloo ranks up to 10 leading unique names using Hong Kong audience affinity
   with cuisine include/exclude filters. Unmentioned candidates retain their
   original order, and no original candidate is discarded.
6. `pipeline.propose()` chooses the poll options. Approval, voice calling,
   transcripts, and calendar generation use the existing flow.

The deterministic `/demo` scene uses preselected options and bypasses Qloo.
Use normal chat messages with `DEMO_MODE=0` to exercise live discovery. Qloo
results describe aggregate affinities, not individual preferences.

## Request contracts

The adapter sends one JSON object to `qloo exec <workflow>` on stdin. The CLI
also accepts `--input` or `--input-file`.

Recommendation uses this shape:

```json
{
  "target_type": "place",
  "signal_tags": ["hotpot", "cosy"],
  "filter_location": "Hong Kong",
  "include_tags": ["hotpot"],
  "limit": 15
}
```

The harness resolves concepts to Qloo tags. Ambiguous concepts can produce
`needs_input`; the adapter falls back instead of guessing. `avoid_cuisines`
becomes `exclude_tags`. No `cuisine:...` entity IDs are invented.

Ranking uses this shape:

```json
{
  "options": ["Samsen", "Chom Chom"],
  "option_type": "place",
  "signal_location": "Hong Kong",
  "include_tags": ["Thai"]
}
```

Rank accepts at most 10 options and does not accept `tags`, `signal_tags`, or
`limit`. Cuisine concepts act as filters; this is Hong Kong audience affinity,
not a personalized ranking inferred from individuals.

`find_tags("cosy")` is an exploratory helper returning tag candidates for
inspection, using `{"query":"cosy","limit":5}`. The bot does not auto-select
these results: recommendation passes concepts to the harness resolver.

`where_popular("Samsen", "Hong Kong")` sends
`{"entity":"Samsen","within":"Hong Kong","limit":10}` and returns heatmap
cells intact. This exploratory helper is **not connected to the bot** and does
not invent district names. Meeting-point selection uses geographic fairness.

## Limits and fallback

- `QLOO_RECOMMEND_LIMIT`: default 15, bounded to 1–20.
- `QLOO_RANK_LIMIT`: default 10, bounded to 1–10. Invalid values use defaults.
- Each workflow has a 12-second subprocess timeout. Sequential recommendation
  and ranking can add roughly 24 seconds if both time out.
- Missing executables, rejected credentials, malformed JSON, errors, empty
  results, and unresolved inputs leave the existing discovery path available.
- Qloo's `place` type is broad. Cuisine filters narrow recommendations where
  supplied. Results do not verify category, opening hours, availability, or a
  booking; the remaining pipeline must evaluate the venue's suitability.
- Coverage and concept resolution depend on live data. Mocked tests verify
  integration behavior, not the availability of any particular venue.

See the official [CLI starter](https://github.com/qloo/qloo-hackathon-kit/tree/main/starter/cli-workflow),
[API access guide](https://github.com/qloo/qloo-hackathon-kit/blob/main/docs/API_ACCESS.md),
and [safe-use guide](https://github.com/qloo/qloo-hackathon-kit/blob/main/docs/SAFE_USE.md).
