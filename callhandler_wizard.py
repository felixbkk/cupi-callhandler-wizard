#!/usr/bin/env python3
"""
cupi-callhandler-wizard
Fetches call handler routing data from Cisco Unity Connection CUPI REST API
and generates an interactive D3.js force graph visualization.
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv

requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)

load_dotenv()

HOST = os.getenv("CUC_HOST", "").rstrip("/")
USER = os.getenv("CUC_USER", "")
PASS = os.getenv("CUC_PASS", "")

HEADERS = {"Accept": "application/json"}
ROWS_PER_PAGE = 512


def api_get(session, path, params=None):
    url = f"{HOST}{path}"
    resp = session.get(url, params=params, headers=HEADERS, verify=False)
    resp.raise_for_status()
    return resp.json()


def paginated_fetch(session, path, collection_key):
    """Fetch all records from a paginated CUPI endpoint."""
    all_records = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, path, params)
        total = int(data.get("@total", 0))
        if total == 0:
            break
        container = data.get(collection_key, {})
        # CUPI returns a single object instead of a list when there's only one record
        if isinstance(container, dict):
            records = container.get(collection_key[:-1] if collection_key.endswith("s") else collection_key, [])
        elif isinstance(container, list):
            records = container
        else:
            records = []
        if isinstance(records, dict):
            records = [records]
        all_records.extend(records)
        if len(all_records) >= total:
            break
        page += 1
    return all_records


def fetch_call_handlers(session):
    print("Fetching call handlers...")
    path = "/vmrest/handlers/callhandlers"
    all_handlers = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, path, params)
        total = int(data.get("@total", 0))
        if total == 0:
            break
        handlers = data.get("Callhandler", [])
        if isinstance(handlers, dict):
            handlers = [handlers]
        all_handlers.extend(handlers)
        print(f"  Fetched {len(all_handlers)}/{total} call handlers")
        if len(all_handlers) >= total:
            break
        page += 1
    return all_handlers


def fetch_interview_handlers(session):
    print("Fetching interview handlers...")
    path = "/vmrest/handlers/interviewhandlers"
    all_handlers = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, path, params)
        total = int(data.get("@total", 0))
        if total == 0:
            break
        handlers = data.get("InterviewHandler", [])
        if isinstance(handlers, dict):
            handlers = [handlers]
        all_handlers.extend(handlers)
        print(f"  Fetched {len(all_handlers)}/{total} interview handlers")
        if len(all_handlers) >= total:
            break
        page += 1
    return all_handlers


def fetch_routing_rules(session):
    print("Fetching routing rules...")
    path = "/vmrest/routingrules"
    all_rules = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, path, params)
        total = int(data.get("@total", 0))
        if total == 0:
            break
        rules = data.get("RoutingRule", [])
        if isinstance(rules, dict):
            rules = [rules]
        all_rules.extend(rules)
        print(f"  Fetched {len(all_rules)}/{total} routing rules")
        if len(all_rules) >= total:
            break
        page += 1
    return all_rules


def fetch_menu_entries(session, handler_id, handler_name):
    try:
        data = api_get(session, f"/vmrest/handlers/callhandlers/{handler_id}/menuentries")
        entries = data.get("MenuEntry", [])
        if isinstance(entries, dict):
            entries = [entries]
        return entries
    except requests.exceptions.HTTPError as e:
        print(f"  Warning: Failed to fetch menu entries for '{handler_name}' ({handler_id}): {e}")
        return []


def fetch_transfer_rules(session, handler_id, handler_name):
    try:
        data = api_get(session, f"/vmrest/handlers/callhandlers/{handler_id}/transferrules")
        rules = data.get("TransferRule", [])
        if isinstance(rules, dict):
            rules = [rules]
        return rules
    except requests.exceptions.HTTPError as e:
        print(f"  Warning: Failed to fetch transfer rules for '{handler_name}' ({handler_id}): {e}")
        return []


def fetch_greetings(session, handler_id, handler_name):
    try:
        data = api_get(session, f"/vmrest/handlers/callhandlers/{handler_id}/greetings")
        greetings = data.get("Greeting", [])
        if isinstance(greetings, dict):
            greetings = [greetings]
        return greetings
    except requests.exceptions.HTTPError as e:
        print(f"  Warning: Failed to fetch greetings for '{handler_name}' ({handler_id}): {e}")
        return []


# -- Action type constants from CUPI --
# 0 = Ignore, 1 = Hangup, 2 = Goto (transfer to handler), 3 = Error,
# 4 = Take Message, 5 = Skip Greeting, 6 = Transfer to alternative contact number
# For after-greeting: action 2 with a TargetHandlerObjectId means route to another handler
ACTION_GOTO = "2"


def build_graph(call_handlers, interview_handlers, routing_rules, session):
    nodes = {}
    edges = []
    handler_map = {}  # ObjectId -> handler info

    # Add call handler nodes
    for ch in call_handlers:
        oid = ch.get("ObjectId", "")
        name = ch.get("DisplayName", "Unknown")
        ext = ch.get("DtmfAccessId", "")
        handler_map[oid] = ch
        nodes[oid] = {
            "id": oid,
            "name": name,
            "extension": ext,
            "type": "callhandler",
            "classification": "normal",
        }

    # Add interview handler nodes
    for ih in interview_handlers:
        oid = ih.get("ObjectId", "")
        name = ih.get("DisplayName", "Unknown")
        nodes[oid] = {
            "id": oid,
            "name": name,
            "extension": "",
            "type": "interview",
            "classification": "normal",
        }

    # Track which handler OIDs are targeted by routing rules
    routing_targets = set()

    # Add routing rule nodes and edges
    for rule in routing_rules:
        rule_oid = rule.get("ObjectId", "")
        rule_name = rule.get("DisplayName", rule.get("RuleName", "Routing Rule"))
        target_oid = rule.get("RouteTargetHandlerObjectId", "")

        nodes[rule_oid] = {
            "id": rule_oid,
            "name": rule_name,
            "extension": "",
            "type": "routingrule",
            "classification": "root",
        }

        if target_oid and target_oid in nodes:
            routing_targets.add(target_oid)
            edges.append({
                "source": rule_oid,
                "target": target_oid,
                "label": rule_name,
            })

    # Track transfer target extensions for dead-end detection
    has_transfer_target = set()

    # Fetch menu entries, transfer rules, and greetings for each call handler
    total = len(call_handlers)
    for i, ch in enumerate(call_handlers):
        oid = ch.get("ObjectId", "")
        name = ch.get("DisplayName", "Unknown")
        if (i + 1) % 10 == 0 or i == 0 or i == total - 1:
            print(f"Fetching details for handler {i + 1}/{total}: {name}")

        # Menu entries
        menu_entries = fetch_menu_entries(session, oid, name)
        for entry in menu_entries:
            target = entry.get("TargetHandlerObjectId", "")
            key = entry.get("TouchtoneKey", "?")
            action = str(entry.get("Action", "0"))
            if target and action == ACTION_GOTO:
                # Ensure target node exists (might be a handler we haven't seen)
                if target not in nodes:
                    nodes[target] = {
                        "id": target,
                        "name": f"Unknown ({target[:8]})",
                        "extension": "",
                        "type": "callhandler",
                        "classification": "normal",
                    }
                edges.append({
                    "source": oid,
                    "target": target,
                    "label": f"Key {key}",
                })

        # Transfer rules
        transfer_rules = fetch_transfer_rules(session, oid, name)
        for tr in transfer_rules:
            rule_name_t = tr.get("RuleIndex", tr.get("TransferRuleDisplayName", "Transfer"))
            extension = tr.get("Extension", "")
            tr_enabled = tr.get("TransferEnabled", "false")
            target_handler = tr.get("TargetHandlerObjectId", "")

            if target_handler and target_handler in nodes:
                edges.append({
                    "source": oid,
                    "target": target_handler,
                    "label": f"Xfer:{rule_name_t}",
                })
                has_transfer_target.add(oid)
            elif extension and str(tr_enabled).lower() == "true":
                # Create a terminal phone node for the extension
                phone_id = f"phone_{extension}"
                if phone_id not in nodes:
                    nodes[phone_id] = {
                        "id": phone_id,
                        "name": f"Ext {extension}",
                        "extension": extension,
                        "type": "phone",
                        "classification": "normal",
                    }
                edges.append({
                    "source": oid,
                    "target": phone_id,
                    "label": f"Xfer:{rule_name_t}",
                })
                has_transfer_target.add(oid)

        # Greetings (after-greeting actions)
        greetings = fetch_greetings(session, oid, name)
        for gr in greetings:
            action = str(gr.get("AfterGreetingAction", "0"))
            target = gr.get("AfterGreetingTargetHandlerObjectId", "")
            greeting_name = gr.get("GreetingType", "Greeting")
            if action == ACTION_GOTO and target:
                if target not in nodes:
                    nodes[target] = {
                        "id": target,
                        "name": f"Unknown ({target[:8]})",
                        "extension": "",
                        "type": "callhandler",
                        "classification": "normal",
                    }
                edges.append({
                    "source": oid,
                    "target": target,
                    "label": f"After:{greeting_name}",
                })

    # Classify nodes
    incoming = {nid: set() for nid in nodes}
    outgoing = {nid: set() for nid in nodes}
    for edge in edges:
        src = edge["source"]
        tgt = edge["target"]
        if tgt in incoming:
            incoming[tgt].add(src)
        if src in outgoing:
            outgoing[src].add(tgt)

    for nid, node in nodes.items():
        if node["type"] == "routingrule":
            node["classification"] = "root"
            continue
        if node["type"] == "phone":
            continue

        has_in = len(incoming[nid]) > 0
        has_out = len(outgoing[nid]) > 0
        is_routing_target = nid in routing_targets

        if is_routing_target or (not has_in and node["type"] == "routingrule"):
            node["classification"] = "root"
        elif not has_in and not has_out and not is_routing_target:
            node["classification"] = "orphan"
        elif has_out and not has_in and not is_routing_target:
            node["classification"] = "unreachable"
        elif has_in and not has_out and nid not in has_transfer_target:
            node["classification"] = "deadend"
        else:
            node["classification"] = "normal"

        # Handlers targeted by routing rules with no other incoming are roots
        if is_routing_target and not has_in:
            node["classification"] = "root"
        elif is_routing_target:
            node["classification"] = "root"

    return list(nodes.values()), edges


def generate_html(nodes, edges):
    graph_data = json.dumps({"nodes": nodes, "links": edges})
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CUC Call Handler Routing Map</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; height: 100vh; background: #1a1a2e; color: #e0e0e0; }}
#graph-container {{ flex: 1; position: relative; overflow: hidden; }}
svg {{ width: 100%; height: 100%; }}
#sidebar {{ width: 320px; background: #16213e; border-left: 1px solid #0f3460; padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; }}
#sidebar h2 {{ color: #e94560; font-size: 18px; border-bottom: 1px solid #0f3460; padding-bottom: 8px; }}
#sidebar h3 {{ color: #e94560; font-size: 14px; margin-top: 8px; }}
.detail-row {{ display: flex; flex-direction: column; gap: 2px; padding: 4px 0; }}
.detail-label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
.detail-value {{ font-size: 14px; word-break: break-all; }}
.controls {{ display: flex; flex-direction: column; gap: 8px; }}
.toggle-btn {{ padding: 8px 12px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 12px; text-align: left; transition: background 0.2s; }}
.toggle-btn:hover {{ background: #0f3460; }}
.toggle-btn.active {{ background: #0f3460; border-color: #e94560; }}
.legend {{ display: flex; flex-direction: column; gap: 6px; }}
.legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 12px; }}
.legend-dot {{ width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }}
.node-label {{ font-size: 10px; fill: #ccc; pointer-events: none; }}
.link-label {{ font-size: 9px; fill: #888; pointer-events: none; }}
.link {{ stroke-opacity: 0.5; fill: none; }}
marker {{ fill: #666; }}
#node-details {{ min-height: 120px; }}
</style>
</head>
<body>
<div id="graph-container">
<svg></svg>
</div>
<div id="sidebar">
<h2>Call Handler Map</h2>
<div class="controls">
<h3>Toggle Visibility</h3>
<button class="toggle-btn active" data-class="orphan" onclick="toggleClass(this, \'orphan\')">Show True Orphans</button>
<button class="toggle-btn active" data-class="unreachable" onclick="toggleClass(this, \'unreachable\')">Show Unreachable Subtrees</button>
<button class="toggle-btn active" data-class="deadend" onclick="toggleClass(this, \'deadend\')">Show Dead Ends</button>
</div>
<div class="legend">
<h3>Legend</h3>
<div class="legend-item"><span class="legend-dot" style="background:#2ecc71"></span> Root (entry point)</div>
<div class="legend-item"><span class="legend-dot" style="background:#3498db"></span> Normal</div>
<div class="legend-item"><span class="legend-dot" style="background:#95a5a6"></span> True Orphan (isolated)</div>
<div class="legend-item"><span class="legend-dot" style="background:#e67e22"></span> Unreachable Subtree</div>
<div class="legend-item"><span class="legend-dot" style="background:#e74c3c"></span> Dead End</div>
<div class="legend-item"><span class="legend-dot" style="background:#9b59b6"></span> Interview Handler</div>
<div class="legend-item"><span class="legend-dot" style="background:#1abc9c"></span> Phone Extension</div>
</div>
<div id="node-details">
<h3>Node Details</h3>
<p style="font-size:12px; color:#666;">Click a node to see details</p>
</div>
</div>
<script>
const graphData = {graph_data};

const colorMap = {{
    root: "#2ecc71",
    normal: "#3498db",
    orphan: "#95a5a6",
    unreachable: "#e67e22",
    deadend: "#e74c3c"
}};

const typeColorOverride = {{
    interview: "#9b59b6",
    phone: "#1abc9c",
    routingrule: "#2ecc71"
}};

function nodeColor(d) {{
    if (typeColorOverride[d.type]) return typeColorOverride[d.type];
    return colorMap[d.classification] || colorMap.normal;
}}

function nodeRadius(d) {{
    if (d.type === "routingrule") return 10;
    if (d.type === "phone") return 6;
    return 8;
}}

const hiddenClasses = new Set();

function toggleClass(btn, cls) {{
    btn.classList.toggle("active");
    if (hiddenClasses.has(cls)) {{
        hiddenClasses.delete(cls);
    }} else {{
        hiddenClasses.add(cls);
    }}
    updateVisibility();
}}

function updateVisibility() {{
    node.style("display", d => hiddenClasses.has(d.classification) ? "none" : null);
    label.style("display", d => hiddenClasses.has(d.classification) ? "none" : null);
    link.style("display", d => {{
        const srcNode = typeof d.source === "object" ? d.source : graphData.nodes.find(n => n.id === d.source);
        const tgtNode = typeof d.target === "object" ? d.target : graphData.nodes.find(n => n.id === d.target);
        if (!srcNode || !tgtNode) return null;
        return (hiddenClasses.has(srcNode.classification) || hiddenClasses.has(tgtNode.classification)) ? "none" : null;
    }});
    linkLabel.style("display", d => {{
        const srcNode = typeof d.source === "object" ? d.source : graphData.nodes.find(n => n.id === d.source);
        const tgtNode = typeof d.target === "object" ? d.target : graphData.nodes.find(n => n.id === d.target);
        if (!srcNode || !tgtNode) return null;
        return (hiddenClasses.has(srcNode.classification) || hiddenClasses.has(tgtNode.classification)) ? "none" : null;
    }});
}}

const container = document.getElementById("graph-container");
const width = container.clientWidth;
const height = container.clientHeight;

const svg = d3.select("svg")
    .attr("viewBox", [0, 0, width, height]);

const g = svg.append("g");

svg.call(d3.zoom()
    .scaleExtent([0.1, 8])
    .on("zoom", (event) => g.attr("transform", event.transform)));

svg.append("defs").append("marker")
    .attr("id", "arrowhead")
    .attr("viewBox", "0 -5 10 10")
    .attr("refX", 20)
    .attr("refY", 0)
    .attr("markerWidth", 6)
    .attr("markerHeight", 6)
    .attr("orient", "auto")
    .append("path")
    .attr("d", "M0,-5L10,0L0,5")
    .attr("fill", "#666");

const simulation = d3.forceSimulation(graphData.nodes)
    .force("link", d3.forceLink(graphData.links).id(d => d.id).distance(120))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collision", d3.forceCollide().radius(20));

const link = g.append("g")
    .selectAll("line")
    .data(graphData.links)
    .join("line")
    .attr("class", "link")
    .attr("stroke", "#666")
    .attr("stroke-width", 1.5)
    .attr("marker-end", "url(#arrowhead)");

const linkLabel = g.append("g")
    .selectAll("text")
    .data(graphData.links)
    .join("text")
    .attr("class", "link-label")
    .text(d => d.label);

const node = g.append("g")
    .selectAll("circle")
    .data(graphData.nodes)
    .join("circle")
    .attr("r", d => nodeRadius(d))
    .attr("fill", d => nodeColor(d))
    .attr("stroke", "#fff")
    .attr("stroke-width", 1.5)
    .style("cursor", "pointer")
    .call(d3.drag()
        .on("start", dragstarted)
        .on("drag", dragged)
        .on("end", dragended))
    .on("click", (event, d) => showDetails(d));

const label = g.append("g")
    .selectAll("text")
    .data(graphData.nodes)
    .join("text")
    .attr("class", "node-label")
    .attr("dy", -12)
    .attr("text-anchor", "middle")
    .text(d => d.name.length > 25 ? d.name.substring(0, 22) + "..." : d.name);

simulation.on("tick", () => {{
    link
        .attr("x1", d => d.source.x)
        .attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x)
        .attr("y2", d => d.target.y);
    linkLabel
        .attr("x", d => (d.source.x + d.target.x) / 2)
        .attr("y", d => (d.source.y + d.target.y) / 2);
    node
        .attr("cx", d => d.x)
        .attr("cy", d => d.y);
    label
        .attr("x", d => d.x)
        .attr("y", d => d.y);
}});

function dragstarted(event) {{
    if (!event.active) simulation.alphaTarget(0.3).restart();
    event.subject.fx = event.subject.x;
    event.subject.fy = event.subject.y;
}}

function dragged(event) {{
    event.subject.fx = event.x;
    event.subject.fy = event.y;
}}

function dragended(event) {{
    if (!event.active) simulation.alphaTarget(0);
    event.subject.fx = null;
    event.subject.fy = null;
}}

const classLabels = {{
    root: "Root (Entry Point)",
    normal: "Normal",
    orphan: "True Orphan",
    unreachable: "Unreachable Subtree",
    deadend: "Dead End"
}};

function showDetails(d) {{
    const details = document.getElementById("node-details");
    details.innerHTML = `
        <h3>Node Details</h3>
        <div class="detail-row">
            <span class="detail-label">Display Name</span>
            <span class="detail-value">${{d.name}}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Extension / DTMF Access ID</span>
            <span class="detail-value">${{d.extension || "N/A"}}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Object ID</span>
            <span class="detail-value">${{d.id}}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Type</span>
            <span class="detail-value">${{d.type}}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Classification</span>
            <span class="detail-value" style="color:${{nodeColor(d)}}">${{classLabels[d.classification] || d.classification}}</span>
        </div>
    `;
}}
</script>
</body>
</html>'''


def main():
    if not HOST or not USER or not PASS:
        print("Error: CUC_HOST, CUC_USER, and CUC_PASS must be set in .env file")
        sys.exit(1)

    print(f"Connecting to {HOST}...")
    session = requests.Session()
    session.auth = (USER, PASS)

    try:
        call_handlers = fetch_call_handlers(session)
        interview_handlers = fetch_interview_handlers(session)
        routing_rules = fetch_routing_rules(session)
    except requests.exceptions.ConnectionError as e:
        print(f"Error: Could not connect to {HOST}: {e}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"Error: API request failed: {e}")
        sys.exit(1)

    print(f"\nFound {len(call_handlers)} call handlers, "
          f"{len(interview_handlers)} interview handlers, "
          f"{len(routing_rules)} routing rules")

    print("\nBuilding graph (fetching menu entries, transfer rules, greetings)...")
    nodes, edges = build_graph(call_handlers, interview_handlers, routing_rules, session)

    # Summary
    classifications = {}
    for n in nodes:
        c = n["classification"]
        classifications[c] = classifications.get(c, 0) + 1

    print(f"\nGraph: {len(nodes)} nodes, {len(edges)} edges")
    for cls, count in sorted(classifications.items()):
        print(f"  {cls}: {count}")

    print("\nGenerating callhandler_map.html...")
    html = generate_html(nodes, edges)
    with open("callhandler_map.html", "w") as f:
        f.write(html)
    print("Done! Open callhandler_map.html in a browser to view the graph.")


if __name__ == "__main__":
    main()
