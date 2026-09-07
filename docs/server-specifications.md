# MCP server specifications

Every server this host connects to, what it exposes and how it is reached. The two
written for this project are specified in full; the reference servers are
summarised with a pointer to their upstream documentation.

---

## 1. `adoptamatch` — own local server

| | |
| --- | --- |
| Repository | <https://github.com/FabianMoraeles/adoptamatch-mcp> (public) |
| Transport | stdio |
| Launched as | `uv run adoptamatch-mcp`, cwd `../../adoptamatch-mcp` |
| Endpoint | none — stdin/stdout of the subprocess |
| Storage | SQLite, `data/adoptamatch.sqlite3` |
| Protocol | JSON-RPC 2.0, hand-written in `minimcp.py`; **no MCP SDK** |
| Full specification | [that repository's README](https://github.com/FabianMoraeles/adoptamatch-mcp#tool-specification) |

Five tools:

| Tool | Parameters | Returns | Errors |
| --- | --- | --- | --- |
| `search_animals` | `species?`, `size?`, `energy_level?`, `good_with_children?`, `good_with_dogs?`, `good_with_cats?`, `include_unavailable=false`, `limit=20` (1–50) | `{total_matches, returned, filters_applied, animals[]}` | `limit` out of range |
| `get_animal_details` | `animal_id` | one full `Animal` record | unknown id |
| `recommend_animals` | `housing`, `children_at_home`, `resident_dogs`, `resident_cats`, `daily_exercise_minutes`, `experience_level`, `preferred_species?`, `preferred_energy?`, `limit=5` | `{household_summary, evaluated, excluded, exclusions[], recommendations[]}` where each recommendation has `score` 0–100, a six-part `breakdown`, `reasons[]` and `concerns[]` | negative counts, `limit` out of range |
| `compare_animals` | `animal_ids[]` (2–5 distinct) | `{fields_compared, differences, common_ground, animals[]}` | wrong count, duplicates, unknown id |
| `register_adoption` | `animal_id`, `adopter_name`, `adopter_email` | `{adoption_id, animal_id, animal_name, adopter_name, adopter_email, adopted_at, animal_status, message}` | unknown id, not available, invalid e-mail, short name |

Why it is not trivial: hard household rules are applied before scoring; the score
is a deterministic weighted sum over six dimensions whose written explanations are
generated from the same components as the number; compatibility flags are
three-state (`true` / `false` / unknown), and unknown never excludes but always
costs points and raises a concern; the adoption is transactional and guarded by a
`UNIQUE` constraint so an animal cannot be adopted twice.

---

## 2. `pet-care` — own remote server (Streamable HTTP)

| | |
| --- | --- |
| Source | `remote_server/` in this repository |
| Transport | Streamable HTTP |
| MCP endpoint | `POST`/`GET`/`DELETE` on `/mcp` |
| Health endpoint | `GET /healthz` → `{"status":"ok","server":"pet-care","version":"1.0.0"}` — a plain HTTP route, deliberately **not** an MCP tool |
| Configuration | `HOST` (default `127.0.0.1`), `PORT` (default `8080`) |
| Protocol | JSON-RPC 2.0, hand-written in `minimcp.py` (a verbatim copy of the file in the public repository); **no MCP SDK** |
| Container | `remote_server/Dockerfile`, non-root, reads `PORT` |
| Deployment guide | [`deployment.md`](deployment.md) |

### `get_daily_care_checklist`

| Parameter | Type | Constraint |
| --- | --- | --- |
| `species` | enum | `dog` \| `cat` |
| `life_stage` | enum | `puppy_kitten` \| `adult` \| `senior` |
| `energy_level` | enum | `low` \| `medium` \| `high` |

Returns `{species, life_stage, energy_level, total_active_minutes, meals_per_day, items[], disclaimer}`.
Each item is `{time_of_day, activity, minutes, note}` with `time_of_day` one of
`morning`, `midday`, `evening`, `weekly`.

Rules: a baseline activity budget per (species, energy level), multiplied by a
life-stage factor (`puppy_kitten` 0.7, `adult` 1.0, `senior` 0.6) and floored at
5 minutes; meals per day from the life stage (3 for `puppy_kitten`, otherwise 2).

Example call and response:

```json
{"species": "cat", "life_stage": "adult", "energy_level": "medium"}
```
```json
{
  "species": "cat", "life_stage": "adult", "energy_level": "medium",
  "total_active_minutes": 20, "meals_per_day": 2,
  "items": [
    {"time_of_day": "morning", "activity": "Fresh water and first meal", "minutes": 10,
     "note": "2 meals per day at this life stage."},
    {"time_of_day": "morning", "activity": "Interactive play", "minutes": 10, "note": ""},
    {"time_of_day": "midday", "activity": "Enrichment: puzzle feeder, scent work or training",
     "minutes": 15, "note": ""},
    {"time_of_day": "evening", "activity": "Second play session", "minutes": 10, "note": ""},
    {"time_of_day": "evening", "activity": "Last meal, water refill, litter or toilet break",
     "minutes": 10, "note": ""},
    {"time_of_day": "weekly", "activity": "Brushing, nail check and a look at ears, teeth and skin",
     "minutes": 20, "note": "Report anything unusual to a veterinarian."}
  ],
  "disclaimer": "General care guidance only. This is not veterinary advice ..."
}
```

### `estimate_daily_water_ml`

| Parameter | Type | Constraint |
| --- | --- | --- |
| `species` | enum | `dog` \| `cat` |
| `weight_kg` | number | `0 < weight_kg <= 120` |

Returns `{species, weight_kg, estimated_ml_per_day, range_low_ml_per_day, range_high_ml_per_day, basis, disclaimer}`.
The range is 50–70 ml/kg/day for a dog and 45–60 for a cat; the point estimate is
the midpoint. Out-of-range weights return `isError: true`.

**Safety.** Neither tool diagnoses or treats anything. Both return a disclaimer,
and the server's `instructions` tell the model to route symptoms, illness, injury
or medication to a veterinarian instead of calling these tools.

---

## 3. `filesystem` — official reference server

| | |
| --- | --- |
| Package | `@modelcontextprotocol/server-filesystem` (npm) |
| Upstream | <https://github.com/modelcontextprotocol/servers> |
| Transport | stdio, launched with `npx -y … demo_workspace` |
| Scope | **`./demo_workspace` only** |

Fourteen tools discovered, including `list_allowed_directories`,
`create_directory`, `write_file`, `read_text_file`, `list_directory`,
`directory_tree`, `edit_file`, `move_file`, `get_file_info`, `search_files`.

Scope is enforced by the server, not by the host: `list_allowed_directories`
returns the single directory it was launched with, and any path outside it is
rejected. Never launch it against a home directory or a drive root.

## 4. `git` — official reference server

| | |
| --- | --- |
| Package | `mcp-server-git` (PyPI) |
| Upstream | <https://github.com/modelcontextprotocol/servers> |
| Transport | stdio, launched with `uvx mcp-server-git --repository demo_workspace/demo-repo` |
| Scope | **`./demo_workspace/demo-repo` only** |

Twelve tools discovered: `git_status`, `git_add`, `git_commit`, `git_log`,
`git_show`, `git_diff`, `git_diff_staged`, `git_diff_unstaged`, `git_branch`,
`git_create_branch`, `git_checkout`, `git_reset`.

**Gotcha worth recording:** every tool takes a `repo_path`, and it must be the path
the server was launched with, relative to *its* working directory —
`demo_workspace/demo-repo`, not `.`. Passing `.` returns
`Repository path '.' is outside the allowed repository`.

---

## 5. `classmate_server_1` and `classmate_server_2`

**One of two integrated.** `config/servers.example.toml` still ships the
`classmate_server_1` / `classmate_server_2` placeholders with `enabled = false` (a
test asserts that), because the *example* file must stay portable across
machines. The real, local `config/servers.toml` (git-ignored) has
`classmate_server_1` filled in and enabled, pointed at Camila Ramirez's
`academic-planner-mcp`. The checklist used is in
[`classmate-servers.md`](classmate-servers.md).

| | Server 1 | Server 2 |
| --- | --- | --- |
| Author | Camila Ramirez (`CamiR24`) | Diego Lopez (`jcdiegolopez`) |
| Repository | <https://github.com/CamiR24/academic-planner-mcp> (public) | <https://github.com/jcdiegolopez/spring-architecture-analyzer-mcp> (public) |
| Purpose | Academic task management: priority scoring, workload analysis, slack-time-aware study scheduling, study-technique recommendation, project decomposition | Static analysis of Java Spring Boot (Maven) repositories: Tree-sitter AST parsing into a SQLite dependency graph, layer-violation checks, cycle detection, blast-radius impact, refactoring-risk ranking, dependency-graph PNGs |
| Transport | stdio | stdio |
| Launch command | `<venv>/Scripts/python.exe -m src.server`, cwd = the repository root. Their README also documents a `pip`/`venv` install (not `uv`); this project's `uv venv` + `uv pip install -r requirements.txt` was used instead to keep the classmate's virtual environment isolated from this host's own dependencies. | `<venv>/Scripts/python.exe server.py`, cwd = the repository root. Same `uv venv` + `uv pip install` treatment. |
| Dependencies | `mcp==1.29.1` only (their `requirements.txt`); official MCP SDK, low-level `Server` API | `mcp`, `tree-sitter`, `tree-sitter-java`, `networkx`, `matplotlib`; official MCP SDK via the high-level **FastMCP** decorator API — a third code path exercised, alongside our hand-written implementation and Camila's low-level `Server` |
| Tools exposed | `add_academic_task`, `get_upcoming_tasks`, `update_task_status`, `calculate_task_priority`, `analyze_workload`, `generate_study_schedule`, `recommend_study_technique`, `decompose_project` (8) | `index_repository`, `get_architecture_overview`, `get_change_impact`, `find_dependency_cycles`, `validate_architecture_rules`, `rank_refactoring_targets`, `generate_dependency_graph` (7) |
| Name collisions with ours | None, against the other 33 tools | None, against the other 39 tools |
| Risks noticed while reading the code | Self-contained: local SQLite (`academic.db`, created in its own cwd) is its only I/O, no network calls, no shell execution, no credentials required. Safe to run with default privileges. | Reads only the Java source tree it is pointed at (no writes there) plus its own `cache/` (SQLite) and `outputs/` (PNGs); no network calls, no shell execution, no credentials. `generate_dependency_graph` writes a file to disk — worth knowing before pointing it at a shared directory. |
| Scenario demonstrated | One turn, two chained tool calls: `add_academic_task` (register "Examen de Redes", CC3067, due 2026-09-15) then `get_upcoming_tasks`, both through Gemini (`LLM_PROVIDER=gemini`) end to end | One turn, four chained tool calls — `index_repository`, `get_architecture_overview`, `find_dependency_cycles`, `validate_architecture_rules` — against a small synthetic Maven project built for this test (`demo_workspace/spring-demo`, 5 classes) with a deliberate `Controller → Repository` layer violation and an `OrderService ↔ PricingService` cycle. Both were correctly detected and reported, through Gemini end to end. |
| Date integrated | 2026-09-06 | 2026-09-06 |

**Confirms a provider fix, not a server-specific one.** Gemini's thinking models
attach an opaque `thought_signature` to a `function_call` part that must be
replayed unchanged on the next request; that bug was first found and fixed
against `adoptamatch`'s `recommend_animals` (see `llm/gemini_provider.py`). Both
classmate integrations are additional, independently-written servers the fix was
verified against — evidence it lives in `GeminiProvider` where it belongs, not
patched around one server's quirks.

**A model-lifecycle gotcha found while testing server 2.** `gemini-2.5-flash`,
pinned explicitly, came back `404 ... no longer available to new users` two days
after it was last used successfully in this project. `gemini-flash-latest` (this
project's default, see `.env.example`) is Google's own rolling alias for the
current free-tier Flash model and was unaffected — a concrete reason to prefer
the alias over pinning a dated model id for a long-running demo project.

---

## Tool inventory across all servers

Run this to regenerate the current list:

```bash
uv run adoptamatch-chatbot --offline --check
```

At the time of writing, with all six available servers connected: **48 tools**
across `adoptamatch` (5), `filesystem` (14), `git` (12), `pet_care_remote` (2),
`academic_planner` (8) and `spring_architecture` (7), with no name collisions —
so no tool needed qualifying.
