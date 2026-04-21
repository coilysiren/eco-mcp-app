# Task 8 — Fair-Price Advisor

**Prereq**: read `todo/README.md` first.

## Goal

Build an MCP tool `fair_price(item)` that returns a short narrative card using real-world commodity data.

## Data sources

- **SSM** `/eco-mcp-app/fred-api-key` (SecureString, **region `us-east-1`**) — already provisioned. Fetch once at app start.
- **FRED API** `GET https://api.stlouisfed.org/fred/series/observations?series_id=X&api_key=KEY&file_type=json`

### Internal mapping: eco item → FRED series

| Eco item | FRED series | Cadence |
|---|---|---|
| CopperIngot | PCOPPUSDM | **monthly** |
| Wheat | PWHEAMTUSDM | **monthly** |
| Board (lumber) | WPU0811 | monthly |
| IronIngot | PIORECRUSDM | monthly |
| Oil | DCOILWTICO | **daily** |

### Cadence matters — don't compute "7-day %" for monthly series

Most of the cheap FRED series for commodities publish **monthly**. Computing a "7-day percent change" against a monthly series is meaningless — you'll either get zero (same observation repeating) or garbage when the observation flips.

Branch the math on series frequency:

```python
def latest_pct_changes(observations, frequency):
    # observations: list of {date, value}, newest last
    if frequency == "daily":
        return {
            "7d":  pct(obs, lookback_days=7),
            "30d": pct(obs, lookback_days=30),
            "90d": pct(obs, lookback_days=90),
        }
    elif frequency == "monthly":
        return {
            "1m":  pct(obs, lookback=1),   # latest vs prior observation
            "3m":  pct(obs, lookback=3),
            "12m": pct(obs, lookback=12),
        }
```

Read the `frequency` from FRED's `/series?series_id=X` response (`frequency_short`: "D", "M", etc.).

## On each call

1. Fetch ~90 days (daily series) or ~13 observations (monthly) of the mapped series from FRED.
2. Compute percent changes using the branching rule above.
3. Return a narrative card:
   > Real copper: $4.12/lb (monthly series, latest Mar 2026). **Up 3.2% vs prior month, 12.1% YoY.** In-cycle fair price for CopperIngot trending up — if you were pricing against real markets, ask ~Y currency per unit.
4. "Y currency per unit" uses a **calibration**: at first-ever call for a given cycle, record the ratio of in-game median price (from your economy data, see task 2) vs the current FRED spot. Multiply by today's real price for subsequent calls. Store calibration in `~/.cache/eco-mcp-app/price-calib.json` keyed by cycle id.

## Caching

- FRED observations: SQLite at `~/.cache/eco-mcp-app/fred.sqlite`, 6-hour TTL.
- Calibration: flat JSON, per-cycle — set-once per cycle.

## Out of scope

- Any in-game enforcement. This is **advisory flavor only**; players opt in by calling the tool.

## Acceptance

- Tool returns a narrative for `["Copper", "Wheat", "Board", "Iron", "Oil"]` with non-null percentages.
- Daily vs monthly series render distinct cadence labels in the narrative (don't call it "7-day %" for a monthly series).
- Second call for any item hits the SQLite cache.
- `inv smoke` passes.
