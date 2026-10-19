# Optional campaign telemetry and equipment proposals

These extensions are opt-in. The original vanilla configuration and native assets,
and existing regular MD/CWIC/TFR defaults, retain their behavior. OEM is a separate
`millennium_dawn_oem` profile with `config-unified-md-oem.json`, its own memory,
realism setting and pilot setting. Its descriptor must explicitly identify OEM;
regular MD's Workshop id is never enough to select OEM or to relabel it regular MD.
The OEM adapter supports the unified runtime only.

## Enable after a coordinated restart

Set `"telemetry": {"enabled": true}` in a private unified configuration. Compile
the matching native adapter with `unified_mod.py --telemetry` while HOI4 is closed,
then start the matching helper and game. The bridge passes the configuration flag
to the compiler. New scripted effects require a game restart. Do not replace files
under a running game/helper. The example keeps dry run on, live approval off,
events off and the strategic pilot disabled unless separately configured.

Set `"proposals": {"enabled": true}` to enable durable equipment drafts. This is
enabled in the new OEM example; other profiles can explicitly opt in. It does not
grant live execution permission. The radio-only compatibility change is a separate
patch and is also required for playsets containing supported radio packs.

## Data contract and units

`AIDIPLO_TELEMETRY|v=1|profile=...|tag=...|gid=...|day=...|date=...` is emitted after
the player state export. It is consumed locally and appended to unified assistant
context only when enabled, matching profile/player/campaign and at most one game
day old. Old, future, malformed and cross-profile snapshots are unavailable.
Log restarts invalidate cached data. Missing data is never silently zero.

| Metric | Native source | Meaning |
| --- | --- | --- |
| Counts | `num_divisions`, `num_deployed_planes`, `num_ships` | Country totals, not an assessment of operational readiness |
| Personnel | `deployed_*_manpower_k` | Thousands of personnel, separately army/air force/navy |
| Fuel | `fuel_k`, `max_fuel_k` | Thousands of fuel units |
| Strength/organization | `every_country_division`, `unit_strength < 0.8`, `unit_organization < 0.5` | Counts below thresholds; denominator independently counted in the same iteration; no weighted average |
| Equipment reserve | `num_equipment@TYPE` | Units in stockpile |
| Fielded equipment | `num_equipment_in_armies@TYPE` | Units in armies |
| Fielded requirement | `num_target_equipment_in_armies@TYPE` | Units required by those armies |

Equipment is limited to each profile's existing verified catalog. Fielded deficit
is `max(required - fielded, 0)`; net deficit after reserves also subtracts stockpile.
These numbers exclude future production, recruitment and garrison requirements.
They do not imply that aircraft or naval inventories are design-specific.

OEM financial data is exported only for OEM, with an independent `has_variable`
flag per field. An uninitialized variable is unavailable even though the native
temporary value defaults to zero. `gdp_total` is annual GDP in billion USD;
`treasury` and `debt` are billion USD; `treasury_rate`, `display_income` and
`display_expense` are billion USD per week; `interest_rate` is percent.
`last_quarter_gdp_growth_rate` and `last_quarter_gdpc_growth_rate` are ratios for
the previous completed quarter, multiplied by 100 for display as percentages.
Per-capita growth is labeled separately. Decimal exports use three decimal places;
counts use zero decimals. No financial variable is changed.

Division training, detailed supply, positions/front assignment, air/naval
organization, individual designs and production queues remain explicitly unknown.
The source audit uses installed HOI4 1.19.3.0.c01a (5632), Steam build 25205862,
OEM 2.0.2 at `149b042d65a3d7d21e674b5088f63a64b003f62d`.
`tests/fixtures/oem-telemetry-source.json` records source hashes without game assets,
credentials, personal paths or save data. Authoritative installed documentation:
`documentation/dynamic_variables_documentation.md` (country variables),
`effects_documentation.md` (`every_country_division`) and
`triggers_documentation.md` (`unit_strength`, `unit_organization`). OEM's
`common/scripted_effects/00_money_system.txt` establishes the economy and stockpile
semantics. Source verification and offline tests do not establish native acceptance.

## Multi-turn transfers and execution evidence

In a foreign leader chat, ask for a supported transfer, e.g. “Please send us rifles.”
The model can propose `propose_equipment` with a null quantity. A reply “25” fills
the durable draft without another provider request. The bridge shows the exact
sender, receiver, type, quantity and proposal id. `confirm ID` authorizes that
revision. `change to 40` or `make that 40` replaces its id; an old confirmation
cannot apply. `cancel` cancels the current draft. `status ID` reads the journal.
Unsupported or ambiguous equipment and model-invented quantities are rejected.

Drafts are isolated by profile, campaign, player, channel and current representative.
They expire after two game days and on rollback or identity change. Drafts persist
across helper restarts. Confirmation is claimed atomically once; neither restarting
nor repeating the message resubmits it. New drafts supersede the channel's old one.
The original equipment request and the human quantity update remain available for
authority revalidation; a bare number never creates independent authority.

States are `proposed`, `confirmed`, `superseded`, `cancelled`, `stale`, or
`unsupported`; a confirmed draft linked to a transaction shows that transaction's
actual state: `preview`, `pending`, `uncertain`, `not_delivered`, `rejected`,
`unverified`, `executed`, `applied`, or `duplicate`. A crash after confirmation but
before a linked transaction stays confirmed; it is never automatically retried.
Dry-run confirmation produces only a preview. With live execution enabled, a
matching engine receipt is still required. Existing equipment effects report the
guarded effect branch as `executed`; this is not independent verification of
recipient inventory/delivery totals. The runtime does not relabel it delivered.
Model prose cannot establish queued/applied/delivered status; unsupported aircraft
orders are handled locally without a provider request or command.

## Aircraft procurement: precise remaining blocker

The installed engine documents `add_equipment_production` with equipment type,
creator, version name and requested factories. This is a possible balanced route:
let the engine charge industrial output/resources and advance production with time.
No compiler for this route is exposed here, and no free aircraft or abstract money
deduction substitutes for it. The documented `amount` example is a ship line;
finite aircraft batch behavior has not been verified.

The installed OEM history at `history/units/USA_2000_bba.txt:342` and
`common/scripted_effects/99_USA_main_scripted_effects.txt:3892` names `F-22A Raptor`
using `medium_plane_fighter_airframe_3` and
`USA_lockheed_martin_aircraft_manufacturer`. This is a generated role-specific
type, not a missing equipment definition: `duplicate_archetypes` in
`common/units/equipment/MD_x_plane_airframes.txt:112` derives the fighter archetype
from `medium_plane_airframe`. Its `_3` derives from `medium_plane_airframe_3`,
unlocked by `gen_5_medium` in `common/technologies/BBA_aircraft.txt:396`.
The USA BBA starting history grants that technology and calls
`USA_create_bba_air_variants`. The base frame has IC cost 33 and aluminium
2/composites 3/rubber 1 before modules, upgrades, ideas and MIO modifiers.
Production must retain the generated fighter type and exact named design;
substituting the base airframe would change the requested equipment.
Current-save availability and final costs still require native evidence.

`has_design_based_on` verifies only a buildable non-obsolete design of an archetype,
not the exact named variant. The installed script documentation yielded no verified
production-line enumeration/removal or per-order named-design delivery counter.
Stockpile changes and role-wide produced totals cannot distinguish a specific order
from parallel production, losses, transfers and deployment. Country purchase
contracts are foreign trade, not a domestic manufacturer production order.

Therefore aircraft orders are durably `unsupported`; the UI states that no
manufacturer was contacted and no effect submitted. Required next work is a
coordinated disposable native test to verify: actual current-save design/technology,
line identity and costs, finite-quantity stopping/cancellation, and attributable
production/delivery receipts. Only then may lifecycle states `queued` and
`delivered` be exposed. No current game/save is an acceptance-test target.

## Validation boundary

Offline tests cover opt-in rendering for vanilla/regular MD/OEM/CWIC/TFR,
cross-profile/stale packet rejection, units/deficits/missing economy data, reversible
owned compilation, persistent quantities/cancel/change/restart/duplicate races,
dry-run gating and receipt identity. Existing vanilla asset/config regression tests
remain in the suite. Native telemetry acceptance, current-save aircraft availability
and all production execution remain pending a coordinated test run.
