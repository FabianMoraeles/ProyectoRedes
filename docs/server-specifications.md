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

**Not yet integrated.** Two placeholder entries ship in
`config/servers.example.toml` with `enabled = false`, and a test asserts that they
do. The integration checklist is in [`classmate-servers.md`](classmate-servers.md).

Fill in this table when the real servers are chosen:

| | Server 1 | Server 2 |
| --- | --- | --- |
| Author | | |
| Repository | | |
| Purpose | | |
| Transport | | |
| Launch command / URL | | |
| Dependencies | | |
| Tools exposed | | |
| Name collisions with ours | | |
| Risks noticed while reading the code | | |
| Scenario demonstrated | | |
| Date integrated | | |

---

## Tool inventory across all servers

Run this to regenerate the current list:

```bash
uv run adoptamatch-chatbot --offline --check
```

At the time of writing, with the four available servers connected: **33 tools**
across `adoptamatch` (5), `filesystem` (14), `git` (12) and `pet_care_remote` (2),
with no name collisions — so no tool needed qualifying.
