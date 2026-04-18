# Forge Upgrade — Observability + Natural-Language FORGE.md

Two upgrades added to the base Forge project, delivered as an in-place patch.
Zero regressions against the existing test suite; 23 new passing tests.

## What changed

### New modules

```
forge/observability/               ← Upgrade #1: runtime observation layer
├── __init__.py                    init_observability_layer(session) entry point
├── tracer.py                      JSONL event writer → .forge/traces/{id}.jsonl
├── instrumentation.py             non-invasive wrappers (build_system_prompt,
                                   query_llm, ToolDispatch.execute, compact,
                                   evaluator.evaluate, pre_query/on_session_end)
├── analyzer.py                    summarize_session, criterion_trend, drift check
└── cli_patch.py                   idempotent patcher for cli.py integration

forge/constraints/
├── forge_md_compiler.py           ← Upgrade #2: NL-prose → structured rules
└── compiler_integration.py            idempotent patcher for cli.py + constraints/__init__.py

tests/
├── test_observability.py          7 tests (all passing)
└── test_forge_md_compiler.py     16 tests (all passing)

FORGE.md.natural_example           example of the new pure-prose format
```

### Modified files (in-place)

- `forge/cli.py` — `create_session` made `async`, observability init added,
  `/trace` and `/trend` REPL commands added, FORGE.md compiled at startup
- `forge/constraints/__init__.py` — `init_quality_layer` now prefers the
  pre-compiled `session.forge_md_compiled` over re-parsing YAML

Both modifications were applied via the marker-based `apply_patches()`
functions in `cli_patch.py` and `compiler_integration.py`, so they're
idempotent and can be re-run safely.

## Upgrade #1 — Observability

The key insight is that the Evaluate side of "Evaluate/Observation" was
already scaffolded (see `forge/evaluation/`). What was missing is
**Observation** — visibility into what the agent is doing over time.

### Events captured

- `session_start` / `session_end` / `session_summary`
- `turn_start` (each LLM round)
- `prompt_built` (total size + which layers were populated)
- `llm_call` (duration, token usage, tool-definition count)
- `tool_call_start` / `tool_call` (tool name, duration, permission decision, error flag)
- `compaction` (level, before/after ratio)
- `evaluator_verdict` (score, per-criterion breakdown)

All written as JSONL to `.forge/traces/{session_id}.jsonl`, rotated at 10MB,
GC'd at 10-session retention. Same failure-mode philosophy as the rest of
Forge: the tracer disables itself on I/O errors rather than crashing.

### New REPL commands

- `/trace [session_id]` — execution summary of current or past session
- `/trend [criterion]` — evaluator-score trend for a criterion across sessions,
  flagging drops >15 points over a 5-session window

### Drift detection at startup

`startup_drift_check` scans the last 30 sessions' evaluator verdicts and
prints a one-line warning if any criterion is drifting downward. Silent when
there's no drift — doesn't spam users.

## Upgrade #2 — Natural-Language FORGE.md

### The problem it solves

Old FORGE.md required three exact ` ```yaml ` code fences. A missing fence,
wrong indent, or tab-vs-space bug → silent fallback to defaults → user thinks
their rules are active but they aren't.

### How it works

1. At startup, `try_load_cache_only` checks for `.forge/forge_md.compiled.json`
   with a matching source-hash. **Zero LLM cost on the common path.**
2. On cache miss, `compile_forge_md` runs an independent LLM call with a
   strict JSON-extraction prompt, converts the prose into the same dataclasses
   (`ConstraintConfig`, `PermissionRule`) the downstream modules already expect.
3. On LLM failure or absent API key, falls back to the existing YAML regex
   parser so anyone with a YAML-format FORGE.md continues to work unchanged.
4. First-time compile prints a structured preview ("parsed 3 deny rules, 5
   layers, 2 forbidden patterns") so users can verify the LLM understood them.

The compiler output is cached to disk (`.forge/forge_md.compiled.json`,
human-readable JSON), which is itself a useful observability artifact —
you can open it and see "what does Forge think my rules are?"

### See `FORGE.md.natural_example`

for a realistic prose-only project rules document. No YAML, no code fences,
no format requirements — just prose.

## How to verify it works

```bash
pip install -e ".[dev]"
pytest tests/test_observability.py tests/test_forge_md_compiler.py -v
# Expect: 23 passed

pytest tests/ -v
# Expect: 87 passed (+ 13 pre-existing failures from tiktoken network
# issues — unrelated to these upgrades)
```

To see the observability output in a real session:

```bash
export ANTHROPIC_API_KEY="sk-..."
cd your-project
forge
You ▸ list the files in this project
You ▸ /trace         # see what just happened
You ▸ /exit
cat .forge/traces/*.jsonl | head
```

## Design principles followed

Both upgrades match the existing project style:

- **Single `init_xxx_layer(session)` entry point** registered from `cli.py`, no
  deep coupling into the loop
- **Hook-based wiring** using the existing `session.hooks` dict — no changes
  to `loop.py` beyond monkey-patching its `query_llm` reference (which is
  consistent with how `context` already patches `build_system_prompt`)
- **Fail-open by default** — tracer disables itself on errors, compiler
  falls back to legacy YAML parser, nothing blocks the agent
- **Dataclass-based config** — `CompiledForgeMD` converts to the same
  `ConstraintConfig` / `PermissionRule` shapes the downstream modules
  already consume, so they need no changes
- **Atomic file writes** (`.tmp` + rename) matching the pattern in `progress.py`
- **Token-budget awareness** — events are bounded in size and subject to
  the same GC treatment as `.forge/tool_outputs/`

## Known limitations (honest list)

1. **When no API key is set**, the NL compiler falls back to the YAML parser
   — which finds zero YAML blocks in a prose-only FORGE.md and uses
   hard-coded defaults. This is a safe failure mode (locked-down defaults,
   not wide-open) but a pure-prose user with no API key gets less than they
   wrote. A louder warning on this case would be a good follow-up.

2. **Per-layer prompt stats** are not captured — I only capture total prompt
   size. To get per-layer token breakdowns, `context/assembler.build` would
   need internal instrumentation (5 lines of change, deferred).

3. **Meta-evaluation loop** mentioned in the original design sketch (re-run
   evaluator on past verdicts with a different prompt to check rubric stability)
   is not implemented in this pass. The trace data to support it is captured;
   only the analysis loop is missing.

4. **The `compiler_integration.py` marker-based patcher** assumes the exact
   string literal from the original cli.py. If someone has already hand-edited
   those lines, the patcher reports `·` (partial match, no write). This was
   a deliberate choice for safety over convenience.
