# Task 2 — Economic Health Dashboard

**Prereq**: read `todo/README.md` first for cross-cutting conventions (SSM region, template paths, iframe CSP, Day-3 data thinness).

## Goal

Build an MCP tool `get_eco_economy` that renders an iframe card showing a live economic vitals board for the Eco server.

## Data sources

All from `/datasets/get?dataset={Name}&dayStart=0&dayEnd={days_elapsed}` on `http://eco.coilysiren.me:3001`. **Admin endpoint** — requires `X-API-Key` header from SSM `/eco-mcp-app/api-admin-token` (region `us-east-1`).

Verified-present stat names (all 14 confirmed in `/datasets/flatlist` on live server):
- `OfferedLoanOrBond`, `AcceptedLoanOrBond`, `RepaidLoanOrBond`, `DefaultedOnLoanOrBond`
- `PayWages`, `PayRentOrMoveInFee`
- `PostedContract`, `CompletedContract`, `FailedContract`
- `PropertyTransfer`, `ReputationTransfer`, `TransferMoney`
- `PayTax`, `ReceiveGovernmentFunds`

Also pull from `GET /info` (public):
- `EconomyDesc` (string like `"524 trades, 0 contracts"`)
- `TotalCulture` (number; **this is a /info field, NOT a /datasets stat** — `/datasets/get?dataset=TotalCulture` returns 500)

Compute `days_elapsed` from `/info`'s `TimeSinceStart` (seconds) → days.

## Card layout (iframe at `/mcp/ui/economy`)

- **Top KPI row**: trades/day, contract completion ratio, loan default rate, total wages paid, net tax flow
- **Middle**: 3–4 sparklines of the most volatile series (pick by stddev of the normalized time series)
- **Narrative strip**: `"Economy is {healthy|stressed|booming} — {N}% default rate, {M}% contracts completed"`

Classification thresholds (seed values, tune as you watch real data):
- `booming`: trades/day up 20% week-over-week AND default rate < 5%
- `stressed`: default rate > 15% OR contract failure rate > 30%
- `healthy`: otherwise

## Implementation notes

- Chart.js inlined (not CDN at runtime — CSP). Copy pattern from existing template.
- Follow existing iframe/partial pattern in `src/eco_mcp_app/templates/` (note: `templates/`, not `ui/`).
- Add MCP tool in `src/eco_mcp_app/server.py`.
- Cache the dataset fetches for 30–60 seconds in-process — the dashboard will be viewed in bursts.

## Out of scope

- Pollution metrics — nerfed in current cycle, don't wire them.

## Acceptance

- Card renders with live numbers at `inv harness`.
- Fresh/zero-data paths render without JS errors (Day-3 reality: most series will have only a handful of data points).
- `inv smoke` passes with the new tool registered.
- Unit tests with `respx` mocking the admin endpoints for at least the KPI computation.
