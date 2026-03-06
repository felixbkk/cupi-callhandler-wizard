# cupi-callhandler-wizard

Connects to a Cisco Unity Connection 12.x server via the CUPI REST API and generates interactive visualizations of the call handler routing tree. All operations are **read-only** — the tool never modifies any data on the CUC server.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

### Generate Reports (default)

```bash
python callhandler_wizard.py --host https://10.212.111.17 --user admin generate
```

You will be securely prompted for the password. Outputs two self-contained HTML files:

- **`callhandler_map.html`** — Interactive D3.js force-directed graph visualization
- **`callhandler_report.html`** — Static table/list report with schedule view, debug tools, and audio links

The `generate` subcommand is the default — you can omit it:

```bash
python callhandler_wizard.py --host https://10.212.111.17 --user admin
```

### CLI Debug Tools

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

**Query any raw CUPI API path** and dump the JSON response:

```bash
python callhandler_wizard.py --host https://... --user admin query /vmrest/handlers/callhandlers
python callhandler_wizard.py --host https://... --user admin query /vmrest/schedules
```

## Report Features

### Graph View (`callhandler_map.html`)

- **Drag** nodes to rearrange the layout
- **Click** a node to view its details in the sidebar
- **Zoom** and pan the graph
- **Toggle** visibility of orphans, unreachable subtrees, and dead ends

### Table View (`callhandler_report.html`)

- **Schedule mode selector** — switch between Standard, Off Hours, Holiday, and All views to see how call routing changes by time of day
- **Search and filter** by name, extension, type, or classification
- **Audio links** for handlers with custom greeting recordings
- **Schedules table** showing business hour time blocks per schedule
- **Holiday schedules table** with start/end dates
- **Debug panel** (bottom-right button) with:
  - Node lookup by name, extension, or Object ID
  - Problem finder (dead ends, orphans, unreachable nodes)
  - Full JSON data dump for export

## Node Classifications

| Color  | Classification       | Description |
|--------|----------------------|-------------|
| Green  | **Root**             | Targeted by a routing rule — valid entry point |
| Blue   | **Normal**           | Standard reachable call handler |
| Grey   | **True Orphan**      | No connections at all — completely isolated |
| Orange | **Unreachable Subtree** | Has outgoing edges but nothing routes to it |
| Red    | **Dead End**         | Has incoming edges but callers have nowhere to go |
| Purple | **Interview Handler** | Interview handler node |
| Teal   | **Phone Extension**  | Transfer target phone extension |

## Security Notes

- All API calls are **read-only** (HTTP GET only). The tool never creates, updates, or deletes any data on the CUC server.
- Password is entered via secure prompt (hidden input) — never stored to disk or passed as a CLI argument.
- SSL verification is disabled (`verify=False`) to support self-signed certificates common on CUC servers.
