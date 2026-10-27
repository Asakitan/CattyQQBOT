# Harness corpus configuration and scopes

The local audit fixes were merged and deployed on 2026-10-27. They read the
active package configuration for corpus limits and style settings. Retrieval
and match cache keys include the effective corpus limit, so a changed enable
switch or limit is reflected on the next lookup. Corpus echo also checks the
corpus enable switch.

## Configuration aliases

`catty_config_loader._apply_config` keeps the existing `chat.fadianji_*` JSON keys
and `CATTY_FADIANJI_*` exports. It additionally exports `CATTY_FDJ_*`, matching the
actual `Config.catty_fdj_*` fields consumed at startup and runtime reload.

For every suffix below, the mapping is:
`chat.fadianji_<suffix>` → `CATTY_FADIANJI_<SUFFIX>` **and**
`CATTY_FDJ_<SUFFIX>` → `Config.catty_fdj_<suffix>`.

| Suffix | Config type |
| --- | --- |
| `corpus_enabled` | `bool` |
| `corpus_max_pairs` | `int` |
| `corpus_semantic_enabled` | `bool` |
| `corpus_lore_enabled` | `bool` |
| `corpus_lore_max_chars` | `int` |
| `stat_critic_enabled` | `bool` |
| `stat_critic_pass_score` | `float` |
| `echo_enabled` | `bool` |
| `echo_probability` | `float` |
| `echo_min_score` | `float` |
| `echo_cooldown_seconds` | `float` |

Explicit `false` and `0` are exported, not replaced with defaults. Missing/null
values retain `_set_env`'s no-write behavior; runtime reload clears model-managed
environment names before applying JSON. Existing flags remain the controls: no
new harness kill switch or real `config.json` change is introduced.

## Corpus profile scopes

`lookup_user_profile(display_name, *, scope=None)` is additive. `scope=None`
preserves legacy lookup, including the last-record-wins normalized-name index and
first-indexed prefix tie handling. New harness callers pass their current scope.

Scoped lookup filters **all profile records before selecting names**, so an
ineligible duplicate cannot overwrite an eligible nickname:

- `private:<id>` accepts only the same `private:<id>` profile; no generic/group or
  other-private fallback.
- `group:<id>` accepts empty/missing profile scope, `group`, and the matching
  `group:<id>`. Private scopes and other explicit group IDs are excluded.
- `group` accepts only empty/missing profile scope and `group`.
- Scope comparison strips surrounding whitespace and lowercases it; unsupported
  or empty requested scopes return no match. Callers still validate live scopes.

Normalized exact names precede bidirectional prefix matching. Both names must
have at least four characters for a prefix match. Within scoped exact or prefix
candidates, highest `triggers` wins; equal scores retain corpus file order.
Scoped results are deep copies and neither selection nor caller edits mutate the
cached profiles. The separate `private_profile(scope)` exact-scope path is
unchanged and still returns the first matching record.

Offline coverage: `tests.test_fdj_corpus_store` and
`tests.test_harness_config_aliases`; the alias tests use synthetic settings and
the actual `Config` model without reading local credentials or starting the bot.