# Optional profiles and strategic reviews

The original `launcher.py`, `middleware/config.json` entry and
`mod/HOI4_AI_Diplomacy` remain the base-game path. The shipping configuration,
Player2 endpoint, ordinary vanilla personality prompts, realism modes and native
mod files retain their original defaults. The GUI launcher adds saved-language
controls and source/frozen middleware startup support. Memory/reload fixes and
dry-run guards also apply to the baseline path.

## Choose the runtime

| Entry | Native installation | Purpose |
| --- | --- | --- |
| `python launcher.py` | Original `HOI4_AI_Diplomacy` | Existing base-game behavior and full original catalog |
| `config-unified-*.json` | Separate `HOI4_AI_Voices_Universal` | Compact dialogue, typed/journaled effects, optional strategic reviews |
| `config-md.json`, `config-cwic-dev.json`, `config-tfr.json` | Separate corresponding `HOI4_AI_Voices_*` | Broader legacy plans and limited overhaul-compatible generic effects |

The MD profile targets [regular upstream Millennium Dawn](MILLENNIUM_DAWN_COMPATIBILITY.md),
Workshop item `2777392649`; it does not select the OEM distribution. Use
`config-unified-md.json` or `config-md.json`, with profile `millennium_dawn`.

All added configuration examples use Player2, dry run and explicit expanded
personality settings. Copy an example to an ignored `config.local.json` before
customizing it. They contain no account credentials or installation-specific paths.

Create an environment from the repository root:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r middleware/requirements.txt
```

Install a separate unified adapter while the game is closed:

```powershell
.\.venv\Scripts\python.exe unified_mod.py --userdir "C:/Users/YOU/Documents/Paradox Interactive/Hearts of Iron IV" --profile vanilla --initial
```

Select `vanilla`, `millennium_dawn`, `cwic_dev` or `the_fire_rises`.
`--legacy` installs the corresponding broader overhaul adapter instead of the
unified adapter; vanilla uses the original entry for its full catalog.
The installer refuses an existing destination on initial installation. Subsequent
compilation requires its ownership manifest and unchanged generated files; it
backs up its owned installation. It does not edit a base mod, playset or save.

Enable only the matching adapter, after its overhaul, in a dedicated Paradox
playset. Start HOI4 with `-debug` (Ironman is unsupported). Start middleware with:

```powershell
.\.venv\Scripts\python.exe middleware/main.py --config middleware/config-unified-vanilla.json
```

Use the matching config for the selected profile. Install/compile again without
`--initial` after changing a unified playset's overhaul. Missing/unrecognized
overhauls must be explicitly mapped, not treated as vanilla.

## Optional Paradox launcher bridge

`Launcher-Voices.py` can start the selected middleware and a debug game, forwarding
Paradox arguments without recording their values. It refuses a second running
game and stops only its own middleware. It requires an ignored
`launcher-bridge.json` next to the script:

```json
{"game_directory":"D:/SteamLibrary/steamapps/common/Hearts of Iron IV","exePath":"hoi4.exe","exeArgs":[]}
```

Set `VOICES_USERDIR` for redirected Documents or an isolated user directory. The
bridge pins the game to that directory with `-userdir=...`. It derives references
from the enabled base descriptor, checks profile agreement and compiles only an
existing owned unified installation. Run `python Launcher-Voices.py --check`
to inspect selection without starting either process. `VoicesLauncher.cs` is an
optional source adapter for Paradox's executable entry; build it locally with the
Windows C# compiler and keep the resulting binary private. No executable is shipped.

The bridge permits unpacked radio-only extras alongside the matching adapter and
overhaul. It checks both descriptors and the actual files, rather than trusting a
mod name or Sound/Music tag alone. The supported layout is music plus MMCT station
faceplates, radio-cover sprites and song localisation. Mods with gameplay files,
replace paths, dependencies, archives or unrelated UI/localisation remain subject
to the existing strict profile selection. Duplicate entries, mixed overhauls,
older Voices adapters and an adapter loaded before its overhaul still fail.
The radio entries stay enabled in the actual playset; `--check` lists them as
`ignored_radio_mods` only for profile detection. Other sound-mod layouts are not
automatically exempted.

### An in-game button does nothing

The Office and diplomacy buttons emit log messages; the external middleware opens
the chat panel. Enabling the native mod and using ordinary Paradox **Play** does
not start that helper. Use the named Voices launcher option for the next full
session. A current `AIDIPLO_DECREE_OPEN`/`AIDIPLO_OPEN` log entry with no middleware
process or current helper log indicates a disconnected helper rather than a
missing button or a GUI-placement failure.

An already-running campaign does not need to restart just to recover dry-run
chat. If no helper is running, start the source middleware separately with the
matching configuration, as above, keeping its dry-run/live-approval settings.
Wait for campaign synchronization, then click Office/Talk again. Startup reads
existing state but intentionally does not replay old button clicks or actions.
Do not start another game or a second helper. Executable game commands still
require a suitable non-Ironman/debug session; attaching the helper does not add
debug mode to an already-running game.

Regular MD is recognized by verified Workshop metadata (`ugc_2777392649.mod`) or
`MD_LOCAL_STANDARD.mod`, including renamed selected descriptors with matching regular
metadata. Other existing aliases recognize `Cold_War_Iron_Curtain.mod` and TFR's `ugc_3350890356.mod`. For another local filename,
put an explicit mapping in `voices-profiles.json` in the HOI4 user directory:

```json
{"mod/my-cwic.mod":"cwic_dev"}
```

Regular-MD mappings must also pass the Workshop ID and regular release/developer
distribution-name checks; an explicit alias cannot turn an OEM source into regular MD.
The bridge/installer reads that descriptor's actual display name for load-order
dependencies and its source path for characterization. Duplicate entries, mixed
overhauls, wrong load order, unknown extras and mismatched profiles are rejected.
This intentionally conservative selector does not claim compatibility with an
arbitrary combination of additional mods. Direct source startup is available for
an explicitly configured profile without changing the launcher.

## Providers and privacy

Player2 remains the default (`localhost:4315`, chat completions, Player2 game-key
header and heartbeat). The optional `openai_compatible` provider uses the same
`player2` configuration block for `base_url`, `chat_path`, `health_path`, `model`
and `timeout_sec`. An `api_key_env` value names an environment variable containing
an existing credential; secrets do not belong in JSON. This mode omits Player2's
header. Local compatible servers can omit `api_key_env`. Set the correct health
route for that server, for example `/v1/models`.

For the local ChatGPT plan adapter, install the optional requirements and choose
`"provider": "chatgpt_plan"`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r middleware/requirements-chatgpt.txt
.\.venv\Scripts\python.exe middleware/chatgpt_auth.py login
.\.venv\Scripts\python.exe middleware/chatgpt_auth.py models
```

Choose an available model in `chatgpt_plan.model`, or leave it empty for model
discovery. This is an explicit browser authorization using the documented
[open-source sign-in flow](https://developers.openai.com/siwc/token-sharing-open-source/sign-in).
Issued registrations and rotating tokens are stored with Windows DPAPI for the
current user. It never imports another application's credentials. The adapter
sends Responses requests and requires a completed stream; incomplete responses
cannot execute actions. The contract is described in
[models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).
Availability remains subject to the provider and account. Tests use synthetic
credentials and mocked inference, not live account access.

Cloud providers receive the chosen game briefing and conversation. They do not
receive unrelated ChatGPT history, account memories, complete save files, or an
entire local lore index. `store:false` is a response-storage request, not a
promise about all provider retention. There is **no native Claude Messages API
adapter** in the supplied local functionality. A compatible gateway must implement
the documented chat-completions contract; direct Claude support is not claimed.

## Personality and context

Set `behavior.enhanced_personalities:true` to opt in to natural, identity-matched
leader dialogue. Omit it or use false to keep the baseline vanilla leader prompt.
Overhaul-specific political mappings and action restrictions follow
`game_profile`; they do not replace vanilla ideology/equipment mappings globally.
The country selector always includes the selected foreign country's snapshot and
uses its current representative, with separate conversation channels.

Optional `characterization.source` (or `--characterization-source`) reads your
installed game/overhaul text locally. MD/CWIC/vanilla references require exact
country/current-name matches: at most one unambiguous biography (1,800 characters)
and three shared trait descriptions (400 characters each). TFR additionally
supplies bounded opening-setting and broad political-group references. Missing,
ambiguous and dynamic descriptions stay unknown. Source text is characterization,
not proof of active ideas, private motives or completed/future story branches.
No game or overhaul assets or biographies are distributed with these readers.

Unified turns use up to twelve bounded previous messages, ten bounded campaign
facts and eight selected country snapshots. Older exchanges are dropped to fit
`usage.max_input_chars`; an oversized current request is rejected. This is a local
character ceiling and token estimate, **not** discovery of a model's context window.
Configure smaller limits for small local models. The complete history stays in
SQLite. There is no automatic export of arbitrary overhaul variables, every idea,
focus tree, hidden information or tactical front state. Unknown data remains unknown.

## Strategic pilot and effects

Unified mode exposes **Usage / AI pilot**. The pilot is disabled on first use.
Its saved setting explicitly selects three synchronized AI major countries other
than the player. Native daily exports drive weekly or relevant-crisis reviews;
pausing, startup/catch-up and loading old saves do not generate catch-up bursts.
Each country sees only its own previous reviews, bounded current context and the
trigger. It can choose hold, infantry or mobile production emphasis for 14 days.
The ordinary game AI continues operations. This is not independent war planning,
AI-to-AI treaty negotiation or a replacement for the tactical AI. Countries can
consider the player and other visible countries; the implementation does not
conduct private multi-agent conversations between them.

Unified actions are typed: bounded goodwill/distrust, pacts, guarantees, access,
stockpile-funded equipment transfers, explicit small Office PP orders, and AI
priority. The channel determines authority; a representative cannot act for a third
country. Automatic reviews cannot grant resources or treaties. The legacy adapters
also support generic stats, supported equipment and timed plans while rejecting
overhaul elections, budgets, laws, leader replacement and raw-command substitutes.

Every added profile starts with `injection.dry_run:true`; unified live delivery
additionally requires `actions.live_approved:true`. Leave both defaults intact for
dialogue and previews. The pilot setting and live-action setting are independent.
Use a disposable campaign and review proposed effects before explicitly enabling
live delivery. Native transaction flags and a local journal prevent automatic
replay; uncertain delivery is not retried. An `executed` result means the guarded
branch ran, while `applied` includes the available script postcondition. Opinion
values, transfer totals and expiry still require independent native/save checks.
English text-only/no-action intent is checked locally; it is not a comprehensive
multilingual authorization parser. Dry run remains the reliable preview boundary.

Unified profiles always meter provider requests. Other modes opt in with
`usage.enabled:true`. Defaults allow 30 requests/100,000 estimated or reported tokens
per rolling day, with automatic sublimits of six/20,000 per day and three per ten
minutes. Failures consume reservations; quota errors pause requests without retry.
These are local allowances, not the account's remaining quota. Disable events with
`events.enabled:false`; unified examples already do so. Legacy action extraction
retains the original baseline default, while added examples explicitly disable it.

## Saves, rollback and validation

Campaign IDs and days isolate memory. Earlier saves exclude future conversation
and action history; resuming an earlier branch removes abandoned future memory.
Old unidentified rows are not assigned to a new campaign. Existing positive-ID
records and plan metadata remain readable; a plan must match both campaign and
player country. Do not share profile databases across unrelated installations.
The original game variables and event namespace are retained; the unified profile
does not execute legacy pending-plan rewards. Finish legacy plans before switching
runtime, or keep that campaign on its legacy entry. Back up saves and local memory
before switching a campaign's mod/runtime combination. Disable/remove the separate
adapter and return to the original playset/config to use the baseline path again.

Offline validation:

```powershell
.\.venv\Scripts\python.exe -m pip install -r middleware/requirements-chatgpt.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover original prompt/native/config fingerprints, all political mappings,
provider requests/completion failures, missing/ambiguous lore, old databases and
reloads, dry-run/no-action behavior, budgets and concurrency, action authority,
transaction replay and generated native block structure. Windows CI runs the same
suite. They do not prove HOI4 parses every effect or displays every UI correctly.

Native acceptance remains required for vanilla, CWIC, MD and TFR: load a disposable
campaign; verify profile/date/leader and minor-country selection; check the UI
placement; pause/reload and compare exports; test legacy concurrent plan choices;
then separately approve a bounded live effect, confirm game.log, verify the resulting
saved state and expiry, and confirm pilot priorities leave normal AI behavior intact.
No native game was launched for this contribution. Earlier OEM observations do not establish regular-MD acceptance.
