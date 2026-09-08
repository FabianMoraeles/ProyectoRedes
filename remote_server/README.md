# pet-care-mcp

A small MCP server served over **Streamable HTTP**. It answers routine questions
about caring for a healthy dog or cat: what a normal day looks like, and roughly
how much water an animal of a given weight drinks.

The business logic is deliberately simple. The point of this server is the
**transport**: it is the component of the project that puts MCP on a TCP socket,
and therefore the one the network analysis can actually observe.

The protocol is implemented **directly over JSON-RPC 2.0** in
[`minimcp.py`](src/pet_care_mcp/minimcp.py) — a verbatim copy of the file in the
public `adoptforme-mcp` repository. **No MCP SDK is used at runtime.**

## Tools

### `get_daily_care_checklist(species, life_stage, energy_level)`

| Parameter | Type | Allowed values |
| --- | --- | --- |
| `species` | string | `dog`, `cat` |
| `life_stage` | string | `puppy_kitten`, `adult`, `senior` |
| `energy_level` | string | `low`, `medium`, `high` |

Returns `{species, life_stage, energy_level, total_active_minutes, meals_per_day, items[], disclaimer}`,
where each item is `{time_of_day, activity, minutes, note}` and `time_of_day` is one
of `morning`, `midday`, `evening`, `weekly`.

How the numbers are produced: a baseline activity budget per (species, energy
level), scaled by a life-stage factor (`puppy_kitten` 0.7, `adult` 1.0,
`senior` 0.6) and floored at 5 minutes; meals per day come from the life stage
(3 for `puppy_kitten`, otherwise 2). Fully deterministic.

### `estimate_daily_water_ml(species, weight_kg)`

| Parameter | Type | Constraint |
| --- | --- | --- |
| `species` | string | `dog`, `cat` |
| `weight_kg` | number | greater than 0, at most 120 |

Returns `{species, weight_kg, estimated_ml_per_day, range_low_ml_per_day, range_high_ml_per_day, basis, disclaimer}`.
The range is 50–70 ml/kg/day for a dog and 45–60 ml/kg/day for a cat; the point
estimate is the midpoint. A weight outside the accepted range returns
`isError: true` with the reason.

**This is an estimate for a healthy animal at rest in a temperate climate.** Heat,
exercise, diet and illness all change it. Both tools return a disclaimer, and the
server's `instructions` tell the model to send symptoms, illness, injury or
medication to a veterinarian instead of calling these tools.

## Endpoints

| Path | Method | Purpose |
| --- | --- | --- |
| `/mcp` | `POST` / `GET` / `DELETE` | The MCP endpoint (Streamable HTTP) |
| `/healthz` | `GET` | Platform health check → `{"status":"ok","server":"pet-care","version":"1.0.0"}` |

`/healthz` is a plain Starlette route, **not** an MCP tool: a deployment probe is
not something a model should be able to call, and it must not appear in
`tools/list`. A test asserts that it does not.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- No MCP SDK: the runtime dependencies are `pydantic`, `starlette` and `uvicorn`.

## Running locally

```bash
uv sync
HOST=127.0.0.1 PORT=8080 uv run pet-care-mcp
curl http://127.0.0.1:8080/healthz
```

`HOST` defaults to `127.0.0.1` and `PORT` to `8080`. Cloud platforms inject `PORT`;
the server reads it at start-up, and the container image sets `HOST=0.0.0.0`.

## Connecting a host to it

```toml
[[servers]]
name = "pet_care_remote"
transport = "streamable-http"
enabled = true
url = "http://127.0.0.1:8080/mcp"
```

Or with MCP Inspector:

```bash
npx @modelcontextprotocol/inspector
# choose transport "Streamable HTTP", URL http://127.0.0.1:8080/mcp
```

## Container

```bash
docker build -t pet-care-mcp .
docker run --rm -p 8080:8080 -e PORT=8080 pet-care-mcp
```

The image installs from `uv.lock` with `--frozen`, so builds are reproducible, and
runs as a non-root user. It carries no secret, because the server needs none.

Deployment to Google Cloud Run — with the cost and risk stated up front — is in
[`../docs/deployment.md`](../docs/deployment.md).

## Tests

```bash
uv run pytest -q     # 18 tests
uv run ruff check .
```

Three levels:

1. **Raw JSON-RPC** — the handshake, `tools/list`, `tools/call`, input validation,
   the tools that must *not* exist, and `-32601` for an unknown method.
2. **Real Streamable HTTP** — uvicorn on an ephemeral port, exercised with an HTTP
   client: the session id issued at `initialize`, `202 Accepted` with no body for a
   notification, `204` for the `DELETE` that ends the session, a parse error rather
   than a 500 for a malformed body, and `405` for the optional `GET` stream this
   server declines to hold open.
3. **Interoperability** — the **official MCP SDK client** driving this
   hand-written server over TCP, in both of its negotiation modes. The SDK is a
   development dependency only.

## Limitations

- Generic guidance for a *healthy* animal only. No diagnosis, no treatment, no
  medication, no emergency advice.
- No authentication. If you deploy it publicly, anyone with the URL can call it.
  These two tools are stateless and harmless, which is why that is acceptable here
  and would not be for a server that touches data.
- No persistence: both tools are pure functions of their arguments.
