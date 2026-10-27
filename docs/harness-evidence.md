# Fadianji evidence packet v2

The local audit candidate checks memory identity against the supplied scope on
every request, including requests without quoted text. For a valid
`group:G:user:U` history scope, group-level RAG and scene providers use
`group:G`; their returned evidence remains scope-checked. Character-book
evidence uses the actual persona name. The candidate has not been deployed.

## API and compatibility

`src/catty_qq_ai/fadianji_harness.py` exports `Evidence`, `QueryFlags`,
`detect_fadianji_query_flags`, `build_fadianji_evidence_packet`,
`build_fadianji_harness_context`, and the shared `EVIDENCE_USAGE` imported from
`harness_prompt.py`. It does not initialize the bot or call language-model APIs.

The packet builder retains these positional arguments, in order:
`text, persona="fadianji", scope_key="", is_private=False, user_id="",
group_id="", memory_store=None, rag_store=None, max_chars=4000`.
Existing keyword-only controls remain: `scene_k=3`, `book_k=3`,
`semantic=False`, `feed_store=None`, `feed_max_items=5`, `query_flags=None`,
`display_name=""`. New keyword-only controls are
`corpus_profile_enabled=True` and `corpus_profile_max_chars=320`; callers pass
their actual configuration. There is no implicit `.config.config` lookup.
The context wrapper accepts the same positional arguments and forwards keywords,
returning only the packet's `text`.

Existing packet keys remain: `text`, `truncated`, `rendered_counts`,
`omitted_count`, `rendered_chars`, `scope`, `is_private`, `flags`, `evidence`,
`scene_matches`, `counts`, `max_chars`. Added fields are:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `2`. |
| `source_status` | `memory/corpus/feed/book/rag/scene` mapped to state and retained count. |
| `diagnostics` | List of `{source,error_type}`; exception messages are never copied. |
| `rendered_ids` / `omitted_ids` | Ordered partition of retained evidence IDs. |
| `omitted_counts` | Omitted evidence count per semantic bucket. |
| `duplicate_count` | Eligible collected rows removed by semantic deduplication. |

## Evidence identity and provenance

`Evidence` retains all original positional fields through `rank`. Its appended
optional fields are `observed_at=None`, `event_date=""`, `confidence=""`, and
`flags=()`. `as_dict()` now includes these fields, `rank`, and `id` in addition
to the original keys. Flags are tuples in Python and JSON arrays after encoding.

`id` is a derived property: the first 20 hexadecimal characters of SHA-256 over
JSON containing source, scope, case/whitespace-normalized text, and privacy.
Changing rank or wall-clock freshness does not change identity. It is a content
reference, not an upstream database primary key or an authentication proof.
Deduplication compares normalized text within the same source-kind prefix and
scope; style and fact/memory rows are never collapsed together. The best ranked
copy is retained. Unknown source kinds render under `OTHER` rather than vanishing.

Actual source timestamps become numeric seconds in `observed_at`:

| Provider | Timestamp inputs |
| --- | --- |
| Memory profile | First usable `snapshot_at`, `snapshot_time`, `updated_at`, `time`, `ts`. |
| Notes | `time`, then `ts` or `snapshot_at`; `event_date` remains separate. |
| Recall summary | `summary_time`, snapshot fields, `time`, `ts`. |
| Recall match | `time`, `ts`, `snapshot_at`. |
| RAG | `ts`, then snapshot fields. |
| Qzone feed | `feed_time`; not the later local `recorded_at`. |
| Corpus profile | `last_ts` converted from milliseconds. |
| Scene | Optional `observed_at` and `event_date` attributes, if supplied. |

Numeric timestamps must be finite and positive. ISO timestamps are accepted;
naive ISO values are interpreted as UTC. Missing/invalid time remains `None`
and renders as `time=?`; known time renders as an ISO UTC date. The recency
score is an age heuristic derived from available time, otherwise zero, not a
measurement of factual freshness. Static relevance/importance/authority scores
are ranking heuristics. Confidence is copied only when provided; no confidence
label is invented for missing metadata.

Harness-generated flags are `historical` for corpus-derived rows, `style_only`
for scene/corpus-style rows, and `excerpt` when this adapter shortens source text.
Provider flags are preserved. Source adapters normalize whitespace and retain
their bounded extracts (for example, notes 300 characters, recall matches 320,
RAG 360, book content 520, feed text 120). A complete rendered row can therefore
be a marked excerpt, not an assertion that an entire original document is shown.
An upstream store may already return extracts; the harness cannot infer omitted
upstream text that it never received.

## Source states and isolation

Each source status has `state`, `count` (after deduplication), and
`collected_count` (eligible rows before deduplication). `rejected_count` and
`error_count` appear when relevant.

| State | Meaning in this adapter |
| --- | --- |
| `ok` | Obtained eligible rows, without an observed failure/rejection. |
| `empty` | A queried source returned no usable evidence. |
| `disabled` | Explicit limit/config/store switch prevents querying. |
| `unavailable` | Store/method absent, or corpus store reports unavailable. |
| `skipped` | This turn did not query (greeting scene gate, missing memory identity, or group corpus without a display name). |
| `scope_mismatch` | Returned candidates were rejected for scope, with none retained. |
| `error` | Observed provider/parsing exception, with no usable rows retained. |
| `partial` | Some rows survived alongside a failure, unavailable subquery, or scope rejection. |

Providers are queried independently. Memory profile, notes, and recall have
separate failure boundaries, so a failing notes call does not erase a successful
profile or prevent recall. A provider-body `TypeError` is never retried.
`inspect.signature` adapts only method-specific optional controls (`persona`,
`top_k`, `limit`, `max_chars_each`) and an empty legacy private-profile
`group_id`. Unsupported identity/scope selectors are not discarded: binding
fails rather than broadening a query. Opaque signatures are invoked once.

`book_k<=0` and `scene_k<=0` prevent their providers from being called at all.
Corpus disabled/zero-cap paths do not access its store. When enabled, corpus
lookup uses `lookup_user_profile(display_name, scope=scope)` after an optional
exact private-profile lookup. Returned private profiles must have exactly the
requested `private:<id>` scope. Group profiles allow `""`, `"group"`, or the
exact requested `group:<id>`; private and unrelated explicit group profiles are
rejected. RAG requires exact scope metadata. Existing public Qzone feed behavior
is retained; this adapter does not add an upstream Qzone privacy guarantee.

Corpus interaction count/time-span becomes `MEMORY.corpus_profile`; register,
length/address/topic tendencies and the selected old dialogue sample become
`STYLE_EXAMPLE.corpus_profile`. Both are historical. Their combined text length
is bounded by `corpus_profile_max_chars`: statistics take the first allocation,
style uses the remainder. Global prompt metadata is budgeted separately.

## Rendering and accounting

`max_chars` bounds only prompt `text`, measured with Python `len` (Unicode
characters, not UTF-8 bytes or model tokens). Full diagnostic evidence remains
available even when the prompt is empty. Rows are considered in rank order,
then descending authority/relevance/recency with deterministic source/scope/text
ties. Rank-10 memory is considered before rank-30 facts. If an entire row and
its metadata cannot fit, the renderer skips it and may accept a later shorter
row; this is a ranked greedy selection, not an optimal knapsack solver.

The renderer reserves a complete scope/intent header, source-state coverage,
budget/row/omission summary, and `【使用】` plus shared `EVIDENCE_USAGE`. It emits
headings only for selected rows and may repeat a heading when rank order moves
back to a bucket. Each row has scope, `src`, stable ID, time, relevance, and any
supplied event date/confidence/flags. Neither metadata nor rows are sliced to
fit. If complete mandatory metadata cannot fit, `text=""` and `truncated=True`.
Row text and IDs are prepared once; selection accounting is linear after sorting.

The packet guarantees:

- `rendered_chars == len(text) <= max_chars`.
- `rendered_ids` and `omitted_ids` are disjoint and cover retained evidence.
- Per bucket, `counts == rendered_counts + omitted_counts` (missing buckets mean zero).
- `omitted_count == len(omitted_ids) == sum(omitted_counts.values())`.
- `sum(source_status[*].count) == len(evidence) == sum(counts.values())`.
- Duplicates and scope-rejected candidates are not counted as budget omissions.

## Validation surface

`tests/test_fadianji_harness.py` retains the existing integration fixtures.
`tests/test_harness_v2.py` uses isolated imports and synthetic providers for
rank priority, CJK/emoji budgets, whole metadata, unknown prefixes, deterministic
IDs/counts, source states, single-call failures, partial memory survival, strict
corpus scopes, timestamp provenance, excerpt flags, and shared corpus text caps.
These tests do not establish live QQ delivery or model compliance with guidance.
The reviewed offline runner passed 622 tests with 9 existing skips and no
failures/errors on 2026-10-26. The synthetic 25-row renderer measured 0.1684 ms
median versus 0.0212 ms for the prior snapshot: provenance has measurable local
cost. This is not an end-to-end model or QQ latency measurement. That version
was deployed with `202971c`.

## 2026-10-27 deployed update

The candidate retains available group-recall speaker names and IDs, including
explicit unknowns; the prefix shares the row's character budget. Group corpus
profiles matched only by nickname become historical style examples rather than
assertions that the current speaker is the same person. Exact private-scope
profile handling remains unchanged. Bounded quoted text is used as a retrieval
query for memory, RAG and scene evidence, without becoming evidence or a book
instruction itself. The entry point rejects explicitly mismatched group metadata;
quotes without it depend on the event context. Normalized exact-trigger indexing
now returns all five checked group-scene matches, and implicit category matches
require lexical overlap. Long quotes and all-term memory matching can still miss.
The candidate passed 1,154 offline tests with nine skips, its selected files were
deployed and then merged into the local workspace. Live model and QQ behavior
remain untested.