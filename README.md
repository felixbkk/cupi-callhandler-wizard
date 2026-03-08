# cupi-callhandler-wizard

Connects to a Cisco Unity Connection server via the CUPI REST API and generates interactive visualizations of the call handler routing tree. All operations are **read-only** -- the tool never modifies any data on the CUC server.

## Setup

Install dependencies (requires internet):

```bash
pip install -r requirements.txt
```

**Offline install** -- all dependencies are bundled in `resources/`:

```bash
pip install --no-index --find-links=resources requests
```

## Usage

### Generate Reports (default)

```bash
python callhandler_wizard.py --host https://10.212.111.17 --user admin generate
```

You will be securely prompted for the password. The `generate` subcommand is the default and can be omitted:

```bash
python callhandler_wizard.py --host https://10.212.111.17 --user admin
```

Output is written to `reports/<SiteName>_<timestamp>/` containing:

| File | Description |
|------|-------------|
| `index.html` | Landing page with city flag, UTC timestamp, report links, and CUC admin login |
| `callhandler_map.html` | Interactive D3.js force-directed graph |
| `callhandler_report.html` | Searchable handler table with routing rules, debug tools, audio playback |
| `callflow.html` | Interactive card-based call flow with deep linking |
| `callflow_trees.html` | Text-based BFS call flow trees with schedule filtering |
| `schedules.html` | Business hour and holiday schedule details |
| `test_times.html` | Recommended test times for each day of the week |
| `audit.html` | Categorized audit findings: warnings, holidays, classification, audio, extension dialing |
| `help.html` | User guide explaining report features, node colors, warnings, and audio formats |
| `audio/` | Downloaded greeting audio WAV files for inline playback |
| `favicon.svg` | Site icon shared across all report pages |
| `d3.v7.min.js` | Local D3 copy for offline use |
| `audit.log` | Text summary of all audit findings |
| `run.log` | Console output log for the generation run |

### CLI Debug Commands

**Look up a specific handler** by name, extension, or Object ID:

```bash
python callhandler_wizard.py --host https://... --user admin handler "Opening Greeting"
python callhandler_wizard.py --host https://... --user admin handler 2000
python callhandler_wizard.py --host https://... --user admin handler abc123 --raw
```

**List all schedules** (business hours and holidays):

```bash
python callhandler_wizard.py --host https://... --user admin schedules
```

**Find orphaned and unreachable handlers** with per-schedule reachability analysis:

```bash
python callhandler_wizard.py --host https://... --user admin orphans
```

**Probe CUPI endpoints** to see what's available on a given server:

```bash
python callhandler_wizard.py --host https://... --user admin probe
```

**Probe all handlers for uploaded greeting audio** (HEAD-checks every greeting URL):

```bash
python callhandler_wizard.py --host https://... --user admin audio
```

**Query any raw CUPI API path** and dump the JSON response:

```bash
python callhandler_wizard.py --host https://... --user admin query /vmrest/handlers/callhandlers
```

## Report Features

All report pages share a **floating navigation pill** and a **dark/light mode toggle** (persisted across pages via localStorage).

### Graph View (`callhandler_map.html`)

- **Layouts**: Force-directed, hierarchical, and radial
- **Drag** nodes to rearrange; **pin** nodes in place, **unpin all** to reset
- **Click** a node to view details in the sidebar (transfer rules, greetings, menu entries, warnings)
- **Zoom** and pan the graph
- **Toggle** visibility of orphans, unreachable subtrees, and dead ends
- **Color-coded edges** by schedule (standard, off hours, holiday, alternate)
- **Transfer details** on edge labels -- transfer type (release/supervised) and rings to wait
- **Extension dialing info** -- unlocked keys and digit timeout
- **Misconfiguration warnings** highlighted per node
- **CUC admin deep links** to open handlers directly in the CUC admin interface

### Flow Trees (`callflow_trees.html`)

- **Text-based BFS call flow trees** from each routing rule entry point
- **Schedule mode selector** -- switch between Standard, Off Hours, Holiday, and All
- **Inline audio playback** with schedule badges
- **Copy as text** for pasting into documentation or tickets

### Table Report (`callhandler_report.html`)

- **Schedule mode selector** -- switch between Standard, Off Hours, Holiday, and All views to see how call routing changes by time of day
- **Search and filter** by name, extension, type, or classification
- **Inline audio playback** for handlers with uploaded greeting recordings, with schedule badges and enabled/disabled state
- **CUC admin deep links** for each handler and greeting
- **Debug panel** (bottom-right button) with node lookup, problem finder, and full JSON data dump

### Call Flow View (`callflow.html`)

- **Card-based call flow trees** from each routing rule entry point
- **Deep linking** -- link directly to a specific handler in the call flow
- **Inline audio playback** with schedule badges and enabled/disabled indicators
- **Misconfiguration warnings** displayed per handler card
- **Extension dialing info** -- which keys are unlocked and digit timeout
- **Transfer details** -- release vs supervised, rings to wait
- **CUC admin deep links** for handlers and greetings

### Schedules View (`schedules.html`)

- Business hour schedules with day-by-day time blocks
- Holiday schedule listing with dates

### Test Times (`test_times.html`)

- Analyzes schedule data to compute recommended test times for each day of the week
- Groups days with identical schedules into ranges (e.g., "Monday -- Thursday")
- Identifies standard hours, off-hours, and transition points
- **Dial path cheat sheet** -- exact key sequences to reach every handler from each entry point
- Copy as Markdown button for each day group and the cheat sheet
- Includes a note about creating a temporary holiday for testing holiday routing

## Misconfiguration Detection

The tool automatically detects and warns about common auto-attendant misconfigurations:

| Warning | Description |
|---------|-------------|
| Alternate transfer rule enabled | Overrides Standard and Off Hours transfer rules |
| Alternate greeting enabled | Overrides Standard greeting |
| Supervised transfer | Should be Release for auto-attendant handlers |
| No timeout key (*) | Callers who press nothing have no path |
| After-greeting = Hangup | Caller gets disconnected after greeting |
| After-greeting = Take Message | Voicemail behavior on an auto-attendant |
| After-message = Hangup | Caller disconnected after recording |
| Menu key = Take Message | Voicemail behavior on a menu key |
| Key routes to itself | Self-referencing menu entry loop |
| Circular routing | Two handlers route to each other with no exit |
| Record your message prompt | "Record at the tone" prompt enabled on AA handler |
| Caller input disabled | DTMF keys ignored during greeting playback |
| Schedule gap | Reachable during Standard but not Off Hours, or vice versa |

Warnings are shown in the console during generation, on call flow cards, and in the graph sidebar.

## Extension Resolution

Transfer targets that route to phone extensions can be resolved to user/contact names via best-effort lookup against `/vmrest/users` and `/vmrest/contacts`. This is **opt-in** via the `--resolve-extensions` flag (adds ~12s for the user/contact fetch):

```bash
python callhandler_wizard.py --host https://... --user admin --resolve-extensions
```

Resolved names display as "John Smith (x1234)" instead of "Ext 1234". This applies to transfer rule targets and alternate contact numbers.

## Node Classifications

| Color  | Classification       | Description |
|--------|----------------------|-------------|
| Green  | **Root**             | Targeted by a routing rule -- valid entry point |
| Blue   | **Normal**           | Standard reachable call handler |
| Grey   | **True Orphan**      | No connections at all -- completely isolated |
| Orange | **Unreachable Subtree** | Has outgoing edges but nothing routes to it |
| Red    | **Dead End**         | Has incoming edges but callers have nowhere to go |
| Purple | **Interview Handler** | Interview handler node |
| Teal   | **Phone Extension**  | Transfer target phone extension |

## Offline / Air-Gapped Use

This tool is designed to work on machines with no internet access. The only network requirement is connectivity to the CUC server.

- **Python dependencies** are bundled as wheel files in `resources/` -- install with `pip install --no-index --find-links=resources requests`
- **D3.js** is bundled in `resources/` and copied into each report directory automatically
- **Generated reports** are fully self-contained -- all CSS, JavaScript, fonts, and audio are embedded or local. No CDN, no web fonts, no external requests. Reports can be viewed on any machine with a browser.

## TLS Compatibility

The tool probes the server with standard TLS first. If the handshake fails (common on older CUC servers with self-signed certs), it automatically falls back to a legacy SSL adapter with relaxed settings. SSL certificate verification is disabled to support self-signed certificates.

## Security Notes

- All API calls are **read-only** (HTTP GET only). The tool never creates, updates, or deletes any data on the CUC server.
- Password is entered via secure prompt (hidden input) -- never stored to disk or passed as a CLI argument.
- Voicemail handlers (numeric-only display names) are automatically filtered out during data collection.
