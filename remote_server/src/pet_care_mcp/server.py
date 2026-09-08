"""pet-care-mcp: a small MCP server served over **Streamable HTTP**.

The protocol is implemented directly over JSON-RPC 2.0 in
:mod:`pet_care_mcp.minimcp` (a verbatim copy of the file in the public
``adoptforme-mcp`` repository). **No MCP SDK is used.**

The business logic is deliberately simple. The point of this server is the
*transport*: it is the component that puts MCP on a TCP socket, which is what the
network-analysis part of the project captures and dissects.

Two tools, both pure functions of their arguments:

* ``get_daily_care_checklist`` -- a routine derived from species, life stage and
  energy level.
* ``estimate_daily_water_ml`` -- a rough maintenance water estimate from body mass.

Neither diagnoses, treats, nor replaces a veterinarian, and both say so in their
output.

Run it with ``uv run pet-care-mcp``; the MCP endpoint is ``/mcp`` and a plain
HTTP ``GET /healthz`` is available for platform health checks (that route is a
normal HTTP endpoint, not an MCP tool -- it must not appear in ``tools/list``).
"""

from __future__ import annotations

import os
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from pet_care_mcp.minimcp import MCPServer, ToolError, run_http

__version__ = "1.0.0"

Species = Literal["dog", "cat"]
LifeStage = Literal["puppy_kitten", "adult", "senior"]
EnergyLevel = Literal["low", "medium", "high"]

DISCLAIMER = (
    "General care guidance only. This is not veterinary advice and it does not "
    "diagnose or treat any condition. Consult a veterinarian for anything specific "
    "to an individual animal."
)

INSTRUCTIONS = """\
pet-care-mcp returns generic daily-care guidance for a healthy dog or cat.

Use it for routine questions (how often to feed, how much water is typical, what a
day of care looks like). Never present its output as medical advice, and always
relay the disclaimer it returns. If the user describes symptoms, illness, injury or
medication, say that this needs a veterinarian instead of calling these tools.
"""

server = MCPServer(
    name="pet-care",
    title="Pet Care Guidance (remote)",
    version=__version__,
    instructions=INSTRUCTIONS,
)


class ChecklistItem(BaseModel):
    """One line of the routine."""

    time_of_day: Literal["morning", "midday", "evening", "weekly"]
    activity: str
    minutes: int = Field(ge=0, le=240)
    note: str = ""


class CareChecklist(BaseModel):
    """Result of ``get_daily_care_checklist``."""

    species: Species
    life_stage: LifeStage
    energy_level: EnergyLevel
    total_active_minutes: int
    meals_per_day: int
    items: list[ChecklistItem]
    disclaimer: str = DISCLAIMER


class WaterEstimate(BaseModel):
    """Result of ``estimate_daily_water_ml``."""

    species: Species
    weight_kg: float
    estimated_ml_per_day: int
    range_low_ml_per_day: int
    range_high_ml_per_day: int
    basis: str
    disclaimer: str = DISCLAIMER


#: Baseline daily activity minutes by (species, energy level).
_ACTIVITY_MINUTES: dict[tuple[str, str], int] = {
    ("dog", "low"): 30,
    ("dog", "medium"): 60,
    ("dog", "high"): 100,
    ("cat", "low"): 10,
    ("cat", "medium"): 20,
    ("cat", "high"): 35,
}

#: Multiplier applied to the baseline for each life stage.
_LIFE_STAGE_FACTOR: dict[str, float] = {"puppy_kitten": 0.7, "adult": 1.0, "senior": 0.6}

#: Meals per day by life stage.
_MEALS: dict[str, int] = {"puppy_kitten": 3, "adult": 2, "senior": 2}

#: Typical maintenance water intake, millilitres per kilogram per day.
_WATER_ML_PER_KG: dict[str, tuple[int, int]] = {"dog": (50, 70), "cat": (45, 60)}

MAX_WEIGHT_KG = 120.0


@server.tool(
    title="Daily care checklist",
    description=(
        "Return a generic daily care routine for a healthy dog or cat, derived from "
        "species, life stage and energy level. Includes exercise, feeding, enrichment "
        "and grooming items with suggested durations. Generic guidance, not veterinary "
        "advice."
    ),
)
def get_daily_care_checklist(
    species: Annotated[Species, Field(description="dog or cat.")],
    life_stage: Annotated[LifeStage, Field(description="puppy_kitten, adult or senior.")],
    energy_level: Annotated[EnergyLevel, Field(description="low, medium or high.")],
) -> CareChecklist:
    baseline = _ACTIVITY_MINUTES[(species, energy_level)]
    active = max(5, round(baseline * _LIFE_STAGE_FACTOR[life_stage]))
    meals = _MEALS[life_stage]
    morning = active // 2
    evening = active - morning

    items = [
        ChecklistItem(
            time_of_day="morning",
            activity="Fresh water and first meal",
            minutes=10,
            note=f"{meals} meals per day at this life stage.",
        ),
        ChecklistItem(
            time_of_day="morning",
            activity="Walk" if species == "dog" else "Interactive play",
            minutes=morning,
        ),
        ChecklistItem(
            time_of_day="midday",
            activity="Enrichment: puzzle feeder, scent work or training",
            minutes=10 if life_stage == "senior" else 15,
        ),
        ChecklistItem(
            time_of_day="evening",
            activity="Second walk" if species == "dog" else "Second play session",
            minutes=evening,
        ),
        ChecklistItem(
            time_of_day="evening",
            activity="Last meal, water refill, litter or toilet break",
            minutes=10,
        ),
        ChecklistItem(
            time_of_day="weekly",
            activity="Brushing, nail check and a look at ears, teeth and skin",
            minutes=20,
            note="Report anything unusual to a veterinarian.",
        ),
    ]
    return CareChecklist(
        species=species,
        life_stage=life_stage,
        energy_level=energy_level,
        total_active_minutes=active,
        meals_per_day=meals,
        items=items,
    )


@server.tool(
    title="Estimate daily water intake",
    description=(
        "Estimate typical daily water intake in millilitres for a healthy dog or cat "
        "from its body mass, using a standard millilitres-per-kilogram range. This is "
        "an estimate for a healthy animal at rest in a temperate climate, not "
        "veterinary advice; heat, exercise, diet and illness all change it."
    ),
)
def estimate_daily_water_ml(
    species: Annotated[Species, Field(description="dog or cat.")],
    weight_kg: Annotated[float, Field(gt=0, le=MAX_WEIGHT_KG, description="Body mass in kilograms.")],
) -> WaterEstimate:
    if weight_kg <= 0 or weight_kg > MAX_WEIGHT_KG:
        raise ToolError(f"weight_kg must be greater than 0 and at most {MAX_WEIGHT_KG:.0f}.")
    low_per_kg, high_per_kg = _WATER_ML_PER_KG[species]
    low = round(weight_kg * low_per_kg)
    high = round(weight_kg * high_per_kg)
    return WaterEstimate(
        species=species,
        weight_kg=weight_kg,
        estimated_ml_per_day=round((low + high) / 2),
        range_low_ml_per_day=low,
        range_high_ml_per_day=high,
        basis=f"{low_per_kg}-{high_per_kg} ml per kg per day for a healthy {species}.",
    )


@server.route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    """Plain HTTP health check for the hosting platform.

    Intentionally *not* an MCP tool: a deployment probe is not something a model
    should be able to call, and it must not appear in ``tools/list``.
    """
    return JSONResponse({"status": "ok", "server": "pet-care", "version": __version__})


def main() -> None:
    """Console entry point. ``HOST`` and ``PORT`` follow the usual cloud contract."""
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    run_http(server, host=host, port=port, mcp_path="/mcp")


if __name__ == "__main__":
    main()
