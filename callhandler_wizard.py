#!/usr/bin/env python3
"""
cupi-callhandler-wizard
Fetches call handler routing data from Cisco Unity Connection CUPI REST API
and generates an interactive D3.js force graph visualization.
"""

import argparse
import getpass
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlparse

import requests

requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)

HEADERS = {"Accept": "application/json"}
ROWS_PER_PAGE = 512


API_TIMEOUT = 30  # seconds per request


def api_get(session, host, path, params=None):
    url = f"{host}{path}"
    resp = session.get(url, params=params, headers=HEADERS, verify=False, timeout=API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def paginated_fetch(session, host, path, collection_key):
    """Fetch all records from a paginated CUPI endpoint."""
    all_records = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, host, path, params)
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


def fetch_site_id(session, host):
    """Fetch a unique site identifier from the CUC cluster info."""
    try:
        data = api_get(session, host, "/vmrest/cluster")
        servers = data.get("ClusterMember", [])
        if isinstance(servers, dict):
            servers = [servers]
        if servers:
            # Use the first server's name as the site ID
            name = servers[0].get("ServerName", "") or servers[0].get("Hostname", "")
            if name:
                return name
    except requests.exceptions.HTTPError:
        pass
    # Fallback: try vmsservers
    try:
        data = api_get(session, host, "/vmrest/vmsservers")
        servers = data.get("VmsServer", data.get("VMSServer", []))
        if isinstance(servers, dict):
            servers = [servers]
        if servers:
            name = servers[0].get("ServerName", "")
            if name:
                return name
    except requests.exceptions.HTTPError:
        pass
    # Last resort: derive from host URL
    parsed = urlparse(host)
    return parsed.hostname or "unknown-site"


def sanitize_dirname(name):
    """Make a string safe for use as a directory name."""
    return re.sub(r'[^\w\-.]', '_', name).strip('_')


def prepare_site_dir(site_id):
    """Create reports/<ServerName>_YYYY-MM-DD/ directory."""
    safe_name = sanitize_dirname(site_id)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = os.path.join("reports", f"{safe_name}_{timestamp}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def fetch_call_handlers(session, host):
    print("Fetching call handlers...")
    path = "/vmrest/handlers/callhandlers"
    all_handlers = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, host, path, params)
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

    # Filter out user voicemail handlers (numeric-only names like 88712142)
    before = len(all_handlers)
    all_handlers = [h for h in all_handlers
                    if not h.get("DisplayName", "").strip().isdigit()]
    skipped = before - len(all_handlers)
    if skipped:
        print(f"  Filtered out {skipped} voicemail handlers ({before} → {len(all_handlers)})")

    return all_handlers


def fetch_interview_handlers(session, host):
    print("Fetching interview handlers...")
    path = "/vmrest/handlers/interviewhandlers"
    all_handlers = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, host, path, params)
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


def fetch_routing_rules(session, host):
    print("Fetching routing rules...")
    path = "/vmrest/routingrules"
    all_rules = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, host, path, params)
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


def fetch_routing_rule_conditions(session, host, rule_id, rule_name):
    """Fetch conditions for a routing rule (called/calling number patterns, etc.)."""
    path = f"/vmrest/routingrules/{rule_id}/routingruleconditions"
    try:
        data = api_get(session, host, path)
        conditions = data.get("RoutingRuleCondition", [])
        if isinstance(conditions, dict):
            conditions = [conditions]
        return conditions
    except requests.exceptions.HTTPError:
        return []


# Condition parameter types from CUPI docs
_CONDITION_PARAMS = {
    "1": "Calling Number",
    "2": "Called Number",
    "3": "Forwarded From",
    "5": "Port",
    "7": "Schedule Set",
    "9": "Phone System",
}

_CONDITION_OPS = {
    "1": "In",
    "2": "Equals",
    "3": "Greater Than",
    "4": "Less Than",
}


def fetch_schedule_sets(session, host):
    """Fetch schedule sets and their member schedules."""
    print("Fetching schedule sets...")
    path = "/vmrest/schedulesets"
    all_sets = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, host, path, params)
        total = int(data.get("@total", 0))
        if total == 0:
            break
        sets = data.get("ScheduleSet", [])
        if isinstance(sets, dict):
            sets = [sets]
        all_sets.extend(sets)
        print(f"  Fetched {len(all_sets)}/{total} schedule sets")
        if len(all_sets) >= total:
            break
        page += 1

    # Filter out subscriber-owned sets (same as schedules)
    before = len(all_sets)
    all_sets = [s for s in all_sets if not s.get("OwnerSubscriberObjectId")]
    skipped = before - len(all_sets)
    if skipped:
        print(f"  Filtered out {skipped} user schedule sets ({before} -> {len(all_sets)})")

    return all_sets


# Track endpoints that have 404'd so we don't spam warnings for every handler
_disabled_endpoints = set()


def _fetch_handler_sub(session, host, handler_id, handler_name, subpath, key, alt_paths=None):
    """Fetch a call handler sub-resource, trying alternative paths if the primary 404s.
    Once an endpoint 404s, it's disabled for the rest of the run."""
    paths_to_try = [subpath] + (alt_paths or [])
    for path in paths_to_try:
        full = f"/vmrest/handlers/callhandlers/{handler_id}/{path}"
        if path in _disabled_endpoints:
            continue
        try:
            data = api_get(session, host, full)
            records = data.get(key, [])
            if isinstance(records, dict):
                records = [records]
            return records
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                if path not in _disabled_endpoints:
                    _disabled_endpoints.add(path)
                    print(f"  Endpoint '{path}' not available (404) — skipping for all handlers")
            else:
                print(f"  Warning: {path} failed for '{handler_name}': {e}")
            continue
    return []


def fetch_menu_entries(session, host, handler_id, handler_name):
    return _fetch_handler_sub(session, host, handler_id, handler_name,
                              "menuentries", "MenuEntry")


def fetch_transfer_rules(session, host, handler_id, handler_name):
    return _fetch_handler_sub(session, host, handler_id, handler_name,
                              "transferrules", "TransferRule",
                              alt_paths=["transferoptions"])


def fetch_greetings(session, host, handler_id, handler_name):
    return _fetch_handler_sub(session, host, handler_id, handler_name,
                              "greetings", "Greeting")


def fetch_holiday_schedules(session, host):
    print("Fetching holiday schedules...")
    path = "/vmrest/holidayschedules"
    all_schedules = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, host, path, params)
        total = int(data.get("@total", 0))
        if total == 0:
            break
        schedules = data.get("HolidaySchedule", [])
        if isinstance(schedules, dict):
            schedules = [schedules]
        all_schedules.extend(schedules)
        print(f"  Fetched {len(all_schedules)}/{total} holiday schedules")
        if len(all_schedules) >= total:
            break
        page += 1

    # Fetch individual holidays for each schedule
    for sched in all_schedules:
        sched_id = sched.get("ObjectId", "")
        sched_name = sched.get("DisplayName", "Unknown")
        try:
            data = api_get(session, host, f"/vmrest/holidayschedules/{sched_id}/holidays")
            holidays = data.get("Holiday", [])
            if isinstance(holidays, dict):
                holidays = [holidays]
            sched["_holidays"] = holidays
        except requests.exceptions.HTTPError:
            print(f"  Warning: Failed to fetch holidays for schedule '{sched_name}'")
            sched["_holidays"] = []

    return all_schedules


def fetch_schedules(session, host):
    print("Fetching schedules...")
    path = "/vmrest/schedules"
    all_schedules = []
    page = 0
    while True:
        params = {"rowsPerPage": ROWS_PER_PAGE, "pageNumber": page}
        data = api_get(session, host, path, params)
        total = int(data.get("@total", 0))
        if total == 0:
            break
        schedules = data.get("Schedule", [])
        if isinstance(schedules, dict):
            schedules = [schedules]
        all_schedules.extend(schedules)
        print(f"  Fetched {len(all_schedules)}/{total} schedules")
        if len(all_schedules) >= total:
            break
        page += 1

    # Filter out per-user schedules (Sync Schedule, voice recognition, etc.)
    # Primary: OwnerSubscriberObjectId set means subscriber-owned
    # Fallback: known system-generated schedule names
    _SKIP_NAMES = {"Sync Schedule", "Voice Recognition Update Schedule"}
    before = len(all_schedules)
    all_schedules = [s for s in all_schedules
                     if not s.get("OwnerSubscriberObjectId")
                     and s.get("DisplayName", "") not in _SKIP_NAMES]
    skipped = before - len(all_schedules)
    if skipped:
        print(f"  Filtered out {skipped} user/system schedules ({before} -> {len(all_schedules)})")

    # Fetch time blocks for each remaining schedule
    total_sched = len(all_schedules)
    for i, sched in enumerate(all_schedules):
        sched_id = sched.get("ObjectId", "")
        sched_name = sched.get("DisplayName", "Unknown")
        if (i + 1) % 5 == 0 or i == 0 or i == total_sched - 1:
            print(f"  Fetching schedule details {i + 1}/{total_sched}: {sched_name}")
        try:
            data = api_get(session, host, f"/vmrest/schedules/{sched_id}/scheduledetails")
            details = data.get("ScheduleDetail", [])
            if isinstance(details, dict):
                details = [details]
            sched["_details"] = details
        except (requests.exceptions.HTTPError, requests.exceptions.Timeout):
            sched["_details"] = []

    return all_schedules


# -- Action type constants from CUPI --
# 0 = Ignore, 1 = Hangup, 2 = Goto (transfer to handler), 3 = Error,
# 4 = Take Message, 5 = Skip Greeting, 6 = Transfer to alternative contact number
# For after-greeting: action 2 with a TargetHandlerObjectId means route to another handler
ACTION_GOTO = "2"

# Schedule context for transfer rules (by RuleIndex) and greetings (by GreetingType)
TRANSFER_SCHEDULE = {
    "0": "standard", "1": "offhours", "2": "alternate",
    "Standard": "standard", "Off Hours": "offhours", "Alternate": "alternate",
}
GREETING_SCHEDULE = {
    "Standard": "standard", "Off Hours": "offhours", "Holiday": "holiday",
    "Alternate": "alternate", "Busy": "always", "Internal": "always", "Error": "always",
}


def greeting_audio_url(host, handler_id, greeting_type, language_code="1033"):
    """Build the CUPI URL for a greeting's audio stream (WAV).
    This is the direct API path — requires authentication to access.
    """
    return (
        f"{host}/vmrest/handlers/callhandlers/{handler_id}"
        f"/greetings/{greeting_type}/greetingstreamfiles/{language_code}/audio"
    )


def build_graph(call_handlers, interview_handlers, routing_rules, session, host,
                schedule_set_map=None):
    nodes = {}
    edges = []
    handler_map = {}  # ObjectId → handler info

    # Add call handler nodes
    for ch in call_handlers:
        oid = ch.get("ObjectId", "")
        name = ch.get("DisplayName", "Unknown")
        ext = ch.get("DtmfAccessId", "")
        handler_map[oid] = ch
        sched_set_id = ch.get("ScheduleSetObjectId", "")
        sched_name = ""
        if schedule_set_map and sched_set_id in schedule_set_map:
            sched_name = schedule_set_map[sched_set_id]
        post_greeting = str(ch.get("PlayPostGreetingRecording", "0"))
        nodes[oid] = {
            "id": oid,
            "name": name,
            "extension": ext,
            "type": "callhandler",
            "classification": "normal",
            "audio": [],
            "scheduleName": sched_name,
            "system": str(ch.get("Undeletable", "false")).lower() == "true",
            "postGreeting": post_greeting != "0",
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
    total_rules = len(routing_rules)
    for i, rule in enumerate(routing_rules):
        rule_oid = rule.get("ObjectId", "")
        rule_name = rule.get("DisplayName", rule.get("RuleName", "Routing Rule"))
        target_oid = rule.get("RouteTargetHandlerObjectId", "")
        rule_state = str(rule.get("State", "0"))

        # Fetch conditions for this rule
        conditions = fetch_routing_rule_conditions(session, host, rule_oid, rule_name)
        cond_list = []
        for c in conditions:
            param = _CONDITION_PARAMS.get(str(c.get("Parameter", "")), "Unknown")
            op = _CONDITION_OPS.get(str(c.get("Operator", "")), "?")
            value = c.get("OperandValue", "")
            cond_list.append({"param": param, "op": op, "value": value})
        if (i + 1) % 5 == 0 or i == 0 or i == total_rules - 1:
            print(f"  Fetching rule conditions {i + 1}/{total_rules}: {rule_name}")

        nodes[rule_oid] = {
            "id": rule_oid,
            "name": rule_name,
            "extension": "",
            "type": "routingrule",
            "classification": "root",
            "conditions": cond_list,
            "ruleState": "Active" if rule_state == "0" else "Inactive" if rule_state == "1" else "Invalid",
        }

        if target_oid and target_oid in nodes:
            routing_targets.add(target_oid)
            edges.append({
                "source": rule_oid,
                "target": target_oid,
                "label": rule_name,
                "schedule": "always",
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
        menu_entries = fetch_menu_entries(session, host, oid, name)
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
                    "schedule": "always",
                })

        # Transfer rules
        transfer_rules = fetch_transfer_rules(session, host, oid, name)
        for tr in transfer_rules:
            rule_name_t = tr.get("RuleIndex", tr.get("TransferRuleDisplayName", "Transfer"))
            extension = tr.get("Extension", "")
            tr_enabled = tr.get("TransferEnabled", "false")
            target_handler = tr.get("TargetHandlerObjectId", "")
            tr_schedule = TRANSFER_SCHEDULE.get(str(rule_name_t), "standard")

            if target_handler and target_handler in nodes:
                edges.append({
                    "source": oid,
                    "target": target_handler,
                    "label": f"Xfer:{rule_name_t}",
                    "schedule": tr_schedule,
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
                    "schedule": tr_schedule,
                })
                has_transfer_target.add(oid)

        # Greetings (after-greeting actions + audio URLs)
        greetings = fetch_greetings(session, host, oid, name)
        for gr in greetings:
            action = str(gr.get("AfterGreetingAction", "0"))
            target = gr.get("AfterGreetingTargetHandlerObjectId", "")
            greeting_name = gr.get("GreetingType", "Greeting")
            language_code = str(gr.get("LanguageCode", "1033"))
            enabled = str(gr.get("PlayWhat", ""))  # 1 = system default, 2 = custom recording
            gr_schedule = GREETING_SCHEDULE.get(greeting_name, "always")
            if enabled == "2":
                nodes[oid]["audio"].append({
                    "greeting": greeting_name,
                    "url": greeting_audio_url(host, oid, greeting_name, language_code),
                    "schedule": gr_schedule,
                })
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
                    "schedule": gr_schedule,
                })

    # Build adjacency maps
    incoming = {nid: set() for nid in nodes}
    outgoing = {nid: set() for nid in nodes}
    outgoing_by_schedule = {}  # nid → {schedule → set of target nids}
    for edge in edges:
        src = edge["source"]
        tgt = edge["target"]
        sched = edge.get("schedule", "always")
        if tgt in incoming:
            incoming[tgt].add(src)
        if src in outgoing:
            outgoing[src].add(tgt)
        outgoing_by_schedule.setdefault(src, {}).setdefault(sched, set()).add(tgt)

    # BFS reachability from all routing rule nodes
    def bfs_reachable(start_nodes, edge_filter=None):
        """Return set of all node IDs reachable from start_nodes."""
        visited = set()
        queue = list(start_nodes)
        while queue:
            nid = queue.pop(0)
            if nid in visited:
                continue
            visited.add(nid)
            if edge_filter is None:
                for tgt in outgoing.get(nid, set()):
                    if tgt not in visited:
                        queue.append(tgt)
            else:
                for sched, targets in outgoing_by_schedule.get(nid, {}).items():
                    if edge_filter(sched):
                        for tgt in targets:
                            if tgt not in visited:
                                queue.append(tgt)
        return visited

    # All routing rule node IDs are entry points
    root_ids = {nid for nid, n in nodes.items() if n["type"] == "routingrule"}

    # Global reachability (any schedule)
    reachable_all = bfs_reachable(root_ids)

    # Per-schedule reachability
    def schedule_filter(active_sched):
        return lambda s: s == "always" or s == active_sched

    reachable_standard = bfs_reachable(root_ids, schedule_filter("standard"))
    reachable_offhours = bfs_reachable(root_ids, schedule_filter("offhours"))
    reachable_holiday = bfs_reachable(root_ids, schedule_filter("holiday"))

    # Classify nodes using true reachability
    for nid, node in nodes.items():
        if node["type"] == "routingrule":
            node["classification"] = "root"
            node["reachable"] = {"standard": True, "offhours": True, "holiday": True}
            continue
        if node["type"] == "phone":
            node["reachable"] = {"standard": True, "offhours": True, "holiday": True}
            continue

        has_in = len(incoming[nid]) > 0
        has_out = len(outgoing[nid]) > 0
        is_routing_target = nid in routing_targets
        is_reachable = nid in reachable_all

        node["reachable"] = {
            "standard": nid in reachable_standard,
            "offhours": nid in reachable_offhours,
            "holiday": nid in reachable_holiday,
        }

        if is_routing_target:
            node["classification"] = "root"
        elif not has_in and not has_out:
            node["classification"] = "orphan"
        elif not is_reachable and has_out:
            node["classification"] = "unreachable"
        elif not is_reachable and has_in:
            # Part of a disconnected cluster — has edges but no path from any root
            node["classification"] = "unreachable"
        elif is_reachable and has_in and not has_out and nid not in has_transfer_target:
            node["classification"] = "deadend"
        else:
            node["classification"] = "normal"

    return list(nodes.values()), edges


D3_CDN_URL = "https://d3js.org/d3.v7.min.js"
D3_FILENAME = "d3.v7.min.js"


def download_d3(site_dir):
    """Download D3.js into the report directory for offline use."""
    dest = os.path.join(site_dir, D3_FILENAME)
    try:
        print("Downloading D3.js for offline use...")
        resp = requests.get(D3_CDN_URL, timeout=15)
        resp.raise_for_status()
        with open(dest, "w", encoding="utf-8") as f:
            f.write(resp.text)
        print(f"  Saved {D3_FILENAME}")
        return True
    except Exception as e:
        print(f"  Warning: Could not download D3.js: {e}")
        print("  Graph will require internet access to load D3 from CDN")
        return False


def generate_html(nodes, edges, d3_local=False):
    graph_data = json.dumps({"nodes": nodes, "links": edges})
    d3_tag = f'<script src="{D3_FILENAME}"></script>' if d3_local else f'<script src="{D3_CDN_URL}"></script>'
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CUC Call Handler Routing Map</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='%231a1a2e'/><path d='M16 20a4 4 0 014-4h8a4 4 0 014 4v24a4 4 0 01-4 4h-8a4 4 0 01-4-4z' fill='%23e94560'/><circle cx='24' cy='42' r='2' fill='%231a1a2e'/><path d='M36 28h10m0 0l-4-4m4 4l-4 4' stroke='%232ecc71' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/><path d='M36 38h10m0 0l-4-4m4 4l-4 4' stroke='%233498db' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>">
{d3_tag}
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
<a href="callhandler_report.html" style="color:#1abc9c; font-size:13px;">Switch to Table Report &rarr;</a>
<div class="controls">
<h3>Layout</h3>
<div style="display:flex; gap:6px; flex-wrap:wrap;">
<button class="toggle-btn active" id="layout-force" onclick="setLayout(\'force\')">Force</button>
<button class="toggle-btn" id="layout-hierarchical" onclick="setLayout(\'hierarchical\')">Hierarchical</button>
<button class="toggle-btn" id="layout-radial" onclick="setLayout(\'radial\')">Radial</button>
</div>
</div>
<div class="controls">
<h3>Navigation</h3>
<div style="display:flex; gap:6px; flex-wrap:wrap;">
<button class="toggle-btn" onclick="zoomIn()">Zoom In</button>
<button class="toggle-btn" onclick="zoomOut()">Zoom Out</button>
<button class="toggle-btn" onclick="fitAll()">Fit All</button>
<button class="toggle-btn" onclick="unpinAll()">Unpin All</button>
</div>
</div>
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

const zoom = d3.zoom()
    .scaleExtent([0.1, 8])
    .on("zoom", (event) => g.attr("transform", event.transform));
svg.call(zoom);

function zoomIn() {{
    svg.transition().duration(300).call(zoom.scaleBy, 1.5);
}}
function zoomOut() {{
    svg.transition().duration(300).call(zoom.scaleBy, 0.67);
}}
function fitAll() {{
    const bounds = g.node().getBBox();
    if (bounds.width === 0 || bounds.height === 0) return;
    const pad = 40;
    const scale = Math.min(
        width / (bounds.width + pad * 2),
        height / (bounds.height + pad * 2)
    );
    const tx = width / 2 - (bounds.x + bounds.width / 2) * scale;
    const ty = height / 2 - (bounds.y + bounds.height / 2) * scale;
    svg.transition().duration(500)
        .call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
}}
function unpinAll() {{
    graphData.nodes.forEach(d => {{ d.fx = null; d.fy = null; }});
    simulation.alphaTarget(0.3).restart();
    setTimeout(() => simulation.alphaTarget(0), 500);
    updatePinIndicators();
}}

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

// BFS depth from root nodes for hierarchical/radial layouts
const adj = {{}};
graphData.nodes.forEach(n => adj[n.id] = []);
graphData.links.forEach(l => {{
    const sid = typeof l.source === "object" ? l.source.id : l.source;
    const tid = typeof l.target === "object" ? l.target.id : l.target;
    adj[sid].push(tid);
}});
const roots = graphData.nodes.filter(n => n.type === "routingrule" || n.classification === "root");
const depthMap = {{}};
const bfsQueue = roots.map(r => {{ depthMap[r.id] = 0; return r.id; }});
while (bfsQueue.length) {{
    const nid = bfsQueue.shift();
    (adj[nid] || []).forEach(tid => {{
        if (depthMap[tid] === undefined) {{
            depthMap[tid] = depthMap[nid] + 1;
            bfsQueue.push(tid);
        }}
    }});
}}
const maxDepth = Math.max(1, ...Object.values(depthMap));
// Assign depth to unreachable nodes
graphData.nodes.forEach(n => {{
    if (depthMap[n.id] === undefined) depthMap[n.id] = maxDepth + 1;
}});

let currentLayout = "force";

const simulation = d3.forceSimulation(graphData.nodes)
    .force("link", d3.forceLink(graphData.links).id(d => d.id).distance(120))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collision", d3.forceCollide().radius(20));

function setLayout(mode) {{
    currentLayout = mode;
    document.querySelectorAll("[id^=layout-]").forEach(b => b.classList.remove("active"));
    document.getElementById("layout-" + mode).classList.add("active");

    // Clear all pins
    graphData.nodes.forEach(d => {{ d.fx = null; d.fy = null; }});

    if (mode === "force") {{
        simulation
            .force("center", d3.forceCenter(width / 2, height / 2))
            .force("charge", d3.forceManyBody().strength(-300))
            .force("x", null)
            .force("y", null);
    }} else if (mode === "hierarchical") {{
        const layerH = height / (maxDepth + 3);
        // Count nodes per depth for horizontal spread
        const perDepth = {{}};
        graphData.nodes.forEach(n => {{
            const d = depthMap[n.id];
            perDepth[d] = (perDepth[d] || 0) + 1;
        }});
        const depthIdx = {{}};
        graphData.nodes.forEach(n => {{
            const d = depthMap[n.id];
            depthIdx[d] = (depthIdx[d] || 0) + 1;
            const count = perDepth[d];
            const spacing = width / (count + 1);
            n.fx = spacing * depthIdx[d];
            n.fy = layerH * (d + 1);
        }});
        simulation
            .force("center", null)
            .force("charge", null)
            .force("x", null)
            .force("y", null);
    }} else if (mode === "radial") {{
        const maxR = Math.min(width, height) / 2 - 60;
        const perDepth = {{}};
        graphData.nodes.forEach(n => {{
            const d = depthMap[n.id];
            perDepth[d] = (perDepth[d] || 0) + 1;
        }});
        const depthIdx = {{}};
        graphData.nodes.forEach(n => {{
            const d = depthMap[n.id];
            depthIdx[d] = (depthIdx[d] || 0) + 1;
            const count = perDepth[d];
            const r = d === 0 ? 0 : (d / (maxDepth + 1)) * maxR;
            const angle = (2 * Math.PI * depthIdx[d]) / count - Math.PI / 2;
            if (d === 0 && count === 1) {{
                n.fx = width / 2;
                n.fy = height / 2;
            }} else {{
                n.fx = width / 2 + r * Math.cos(angle);
                n.fy = height / 2 + r * Math.sin(angle);
            }}
        }});
        simulation
            .force("center", null)
            .force("charge", null)
            .force("x", null)
            .force("y", null);
    }}

    updatePinIndicators();
    simulation.alpha(1).restart();
    setTimeout(() => fitAll(), 600);
}}

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
    .on("click", (event, d) => showDetails(d))
    .on("dblclick", (event, d) => {{
        d.fx = null;
        d.fy = null;
        simulation.alphaTarget(0.3).restart();
        setTimeout(() => simulation.alphaTarget(0), 300);
        updatePinIndicators();
    }});

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
    // Pin node where it was dropped
    event.subject.fx = event.x;
    event.subject.fy = event.y;
    updatePinIndicators();
}}

function updatePinIndicators() {{
    node.attr("stroke", d => d.fx != null ? "#e94560" : "#fff")
        .attr("stroke-width", d => d.fx != null ? 2.5 : 1.5);
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


def _format_minutes(mins):
    """Convert minutes-from-midnight to HH:MM AM/PM."""
    try:
        m = int(mins)
    except (ValueError, TypeError):
        return str(mins)
    h, mm = divmod(m, 60)
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d} {ampm}"


DAYS_OF_WEEK = {
    "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
    "4": "Thu", "5": "Fri", "6": "Sat",
}


def generate_table_html(nodes, edges, holiday_schedules, schedules):
    report_data = json.dumps({
        "nodes": nodes,
        "edges": edges,
        "holidays": [{
            "name": s.get("DisplayName", ""),
            "entries": [{
                "name": h.get("DisplayName", ""),
                "start": h.get("StartDate", ""),
                "end": h.get("EndDate", ""),
            } for h in s.get("_holidays", [])]
        } for s in holiday_schedules],
        "schedules": [{
            "name": s.get("DisplayName", ""),
            "id": s.get("ObjectId", ""),
            "details": [{
                "startDay": DAYS_OF_WEEK.get(str(d.get("StartDayOfWeek", "")), str(d.get("StartDayOfWeek", ""))),
                "endDay": DAYS_OF_WEEK.get(str(d.get("EndDayOfWeek", "")), str(d.get("EndDayOfWeek", ""))),
                "startTime": _format_minutes(d.get("StartTime", "")),
                "endTime": _format_minutes(d.get("EndTime", "")),
                "active": str(d.get("IsActive", "true")).lower() == "true",
            } for d in s.get("_details", [])]
        } for s in schedules],
    })

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CUC Call Handler Report</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='%231a1a2e'/><path d='M16 20a4 4 0 014-4h8a4 4 0 014 4v24a4 4 0 01-4 4h-8a4 4 0 01-4-4z' fill='%23e94560'/><circle cx='24' cy='42' r='2' fill='%231a1a2e'/><path d='M36 28h10m0 0l-4-4m4 4l-4 4' stroke='%232ecc71' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/><path d='M36 38h10m0 0l-4-4m4 4l-4 4' stroke='%233498db' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a2e; color: #e0e0e0; padding: 24px; }}
h1 {{ color: #e94560; margin-bottom: 8px; }}
h2 {{ color: #e94560; margin: 32px 0 12px 0; font-size: 20px; border-bottom: 1px solid #0f3460; padding-bottom: 8px; }}
.summary {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; }}
.summary-badge {{ padding: 6px 14px; border-radius: 4px; font-size: 13px; font-weight: 600; color: #fff; }}
.stats {{ color: #888; font-size: 14px; margin-bottom: 16px; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; font-size: 13px; }}
th {{ background: #16213e; color: #e94560; text-align: left; padding: 10px 12px; position: sticky; top: 0; border-bottom: 2px solid #0f3460; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #0f3460; vertical-align: top; }}
tr:hover {{ background: #16213e; }}
.muted {{ color: #555; }}
.oid {{ font-family: monospace; font-size: 11px; color: #666; }}
.filter-bar {{ margin: 12px 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
.filter-bar input {{ padding: 8px 12px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; border-radius: 4px; font-size: 13px; width: 300px; }}
.filter-bar select {{ padding: 8px 12px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; border-radius: 4px; font-size: 13px; }}
.audio-link {{ color: #1abc9c; text-decoration: none; font-size: 12px; }}
.audio-link:hover {{ text-decoration: underline; }}
.debug-toggle {{ position: fixed; bottom: 16px; right: 16px; padding: 8px 14px; background: #0f3460; border: 1px solid #e94560; color: #e94560; cursor: pointer; border-radius: 4px; font-size: 12px; z-index: 100; }}
.debug-toggle:hover {{ background: #1a1a4e; }}
#debugPanel {{ display: none; background: #0d1b2a; border: 1px solid #0f3460; border-radius: 8px; padding: 20px; margin-top: 32px; }}
#debugPanel h2 {{ color: #e94560; margin-bottom: 12px; }}
.debug-bar {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }}
.debug-bar input {{ flex: 1; min-width: 200px; padding: 8px 12px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; border-radius: 4px; font-size: 13px; }}
.debug-btn {{ padding: 8px 14px; border: 1px solid #0f3460; background: #16213e; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 12px; white-space: nowrap; }}
.debug-btn:hover {{ background: #0f3460; }}
#debugOutput {{ background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px; padding: 12px; font-family: monospace; font-size: 12px; white-space: pre-wrap; max-height: 500px; overflow-y: auto; color: #aaa; }}
.schedule-bar {{ display: flex; gap: 4px; margin: 16px 0; }}
.schedule-btn {{ padding: 8px 16px; border: 2px solid #0f3460; background: #16213e; color: #e0e0e0; cursor: pointer; border-radius: 4px; font-size: 13px; font-weight: 600; transition: all 0.2s; }}
.schedule-btn:hover {{ border-color: #e94560; }}
.schedule-btn.active {{ background: #0f3460; border-color: #e94560; color: #fff; }}
.schedule-label {{ font-size: 13px; color: #888; align-self: center; margin-right: 8px; }}
.diff-highlight {{ background: #2a1a3e; }}
.flow-tree {{ background: #16213e; border: 1px solid #0f3460; border-radius: 6px; padding: 16px; margin-bottom: 16px; font-family: monospace; font-size: 13px; line-height: 1.6; white-space: pre; overflow-x: auto; }}
.flow-tree .flow-root {{ color: #2ecc71; font-weight: 700; }}
.flow-tree .flow-handler {{ color: #3498db; }}
.flow-tree .flow-label {{ color: #e94560; }}
.flow-tree .flow-muted {{ color: #555; }}
.flow-tree .flow-visited {{ color: #555; font-style: italic; }}
</style>
</head>
<body>
<h1>CUC Call Handler Report</h1>
<a href="callhandler_map.html" style="color:#1abc9c; font-size:13px;">&larr; Switch to Graph View</a>
<div id="stats" class="stats"></div>
<div id="summary" class="summary"></div>

<h2>Call Flow Schedule View</h2>
<div class="schedule-bar">
<span class="schedule-label">Active schedule:</span>
<button class="schedule-btn active" onclick="setSchedule('standard')">Standard</button>
<button class="schedule-btn" onclick="setSchedule('offhours')">Off Hours</button>
<button class="schedule-btn" onclick="setSchedule('holiday')">Holiday</button>
<button class="schedule-btn" onclick="setSchedule('all')">All (raw)</button>
</div>

<h2>Call Flow Trees</h2>
<div id="callFlowTrees"></div>

<h2>Call Handlers &amp; Routing</h2>
<div class="filter-bar">
<input type="text" id="search" placeholder="Filter by name, extension, or type..." oninput="renderTable()">
<select id="classFilter" onchange="renderTable()">
<option value="">All Classifications</option>
<option value="root">Root (Entry Point)</option>
<option value="normal">Normal</option>
<option value="deadend">Dead End</option>
<option value="unreachable">Unreachable Subtree</option>
<option value="orphan">True Orphan</option>
</select>
</div>
<table id="handlerTable">
<thead>
<tr><th>Name</th><th>Extension</th><th>Type</th><th>Classification</th><th>Schedule / Conditions</th><th>Incoming</th><th>Outgoing</th><th>Audio</th><th>Object ID</th></tr>
</thead>
<tbody></tbody>
</table>

<h2>Schedules (Business Hours)</h2>
<table id="scheduleTable">
<thead>
<tr><th>Schedule</th><th>Day(s)</th><th>Start Time</th><th>End Time</th><th>Active</th></tr>
</thead>
<tbody></tbody>
</table>

<h2>Holiday Schedules</h2>
<table id="holidayTable">
<thead>
<tr><th>Schedule</th><th>Holiday</th><th>Start Date</th><th>End Date</th></tr>
</thead>
<tbody></tbody>
</table>

<script>
const data = {report_data};
let activeSchedule = "standard";

const classColors = {{
    root: "#2ecc71", normal: "#3498db", orphan: "#95a5a6",
    unreachable: "#e67e22", deadend: "#e74c3c"
}};
const typeColors = {{ interview: "#9b59b6", phone: "#1abc9c", routingrule: "#2ecc71" }};
const classLabels = {{
    root: "Root (Entry Point)", normal: "Normal", orphan: "True Orphan",
    unreachable: "Unreachable Subtree", deadend: "Dead End"
}};

const nodeMap = {{}};
data.nodes.forEach(n => nodeMap[n.id] = n);

function nodeColor(n) {{
    return typeColors[n.type] || classColors[n.classification] || "#3498db";
}}

function esc(s) {{
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
}}

function edgeMatchesSchedule(e) {{
    if (activeSchedule === "all") return true;
    return e.schedule === "always" || e.schedule === activeSchedule;
}}

function audioMatchesSchedule(a) {{
    if (activeSchedule === "all") return true;
    return a.schedule === "always" || a.schedule === activeSchedule;
}}

function setSchedule(mode) {{
    activeSchedule = mode;
    document.querySelectorAll(".schedule-btn").forEach(btn => {{
        btn.classList.toggle("active", btn.textContent.toLowerCase().replace(/[^a-z]/g, "") === mode ||
            (mode === "offhours" && btn.textContent === "Off Hours") ||
            (mode === "all" && btn.textContent === "All (raw)"));
    }});
    renderTable();
}}

function renderTable() {{
    const search = (document.getElementById("search").value || "").toLowerCase();
    const clsFilter = document.getElementById("classFilter").value;

    // Filter edges by schedule
    const activeEdges = data.edges.filter(edgeMatchesSchedule);
    const outgoing = {{}};
    const incoming = {{}};
    activeEdges.forEach(e => {{
        (outgoing[e.source] = outgoing[e.source] || []).push(e);
        (incoming[e.target] = incoming[e.target] || []).push(e);
    }});

    // Sort nodes
    const typeOrder = {{ routingrule: 0, callhandler: 1, interview: 2, phone: 3 }};
    const classOrder = {{ root: 0, normal: 1, deadend: 2, unreachable: 3, orphan: 4 }};
    const sorted = [...data.nodes].sort((a, b) =>
        (typeOrder[a.type] ?? 9) - (typeOrder[b.type] ?? 9) ||
        (classOrder[a.classification] ?? 9) - (classOrder[b.classification] ?? 9) ||
        a.name.toLowerCase().localeCompare(b.name.toLowerCase())
    );

    const tbody = document.querySelector("#handlerTable tbody");
    tbody.innerHTML = "";

    sorted.forEach(n => {{
        const color = nodeColor(n);
        const clsLabel = classLabels[n.classification] || n.classification;

        // Text filter
        const text = (n.name + " " + n.extension + " " + n.type + " " + clsLabel).toLowerCase();
        if (search && !text.includes(search)) return;
        if (clsFilter && n.classification !== clsFilter) return;

        const outLinks = outgoing[n.id] || [];
        const inLinks = incoming[n.id] || [];

        const outHtml = outLinks.length
            ? outLinks.map(e => esc(e.label) + " &rarr; " + esc((nodeMap[e.target] || {{}}).name || "?")).join("<br>")
            : '<span class="muted">None</span>';
        const inHtml = inLinks.length
            ? inLinks.map(e => esc((nodeMap[e.source] || {{}}).name || "?") + " &rarr; " + esc(e.label)).join("<br>")
            : '<span class="muted">None</span>';

        const audioList = (n.audio || []).filter(audioMatchesSchedule);
        const audioHtml = audioList.length
            ? audioList.map(a => '<a href="' + esc(a.url) + '" target="_blank" class="audio-link">' + esc(a.greeting) + '</a>').join("<br>")
            : '<span class="muted">&mdash;</span>';

        // Schedule name for handlers, conditions for routing rules
        let schedCondHtml = '<span class="muted">&mdash;</span>';
        if (n.type === "routingrule") {{
            const conds = n.conditions || [];
            const stateTag = n.ruleState !== "Active" ? ' <span class="muted">(' + esc(n.ruleState) + ')</span>' : "";
            if (conds.length) {{
                schedCondHtml = conds.map(c => esc(c.param) + " " + esc(c.op) + " " + esc(c.value)).join("<br>") + stateTag;
            }} else {{
                schedCondHtml = '<span class="muted">No conditions (matches all)</span>' + stateTag;
            }}
        }} else if (n.scheduleName) {{
            schedCondHtml = esc(n.scheduleName);
        }}

        const tr = document.createElement("tr");
        tr.innerHTML =
            '<td style="color:' + color + '; font-weight:600">' + esc(n.name) + (n.system ? ' <span class="muted">(system)</span>' : "") + (n.postGreeting ? ' <span style="color:#e67e22">&#9654; post-greeting</span>' : "") + '</td>' +
            '<td>' + esc(n.extension) + '</td>' +
            '<td>' + esc(n.type) + '</td>' +
            '<td style="color:' + color + '">' + esc(clsLabel) + '</td>' +
            '<td>' + schedCondHtml + '</td>' +
            '<td>' + inHtml + '</td>' +
            '<td>' + outHtml + '</td>' +
            '<td>' + audioHtml + '</td>' +
            '<td class="oid">' + esc(n.id) + '</td>';
        tbody.appendChild(tr);
    }});

    // Update stats
    const counts = {{}};
    data.nodes.forEach(n => counts[n.classification] = (counts[n.classification] || 0) + 1);
    document.getElementById("stats").innerHTML =
        data.nodes.length + " nodes &middot; " + activeEdges.length + " active connections &middot; " +
        data.edges.length + " total connections &middot; " + data.holidays.length + " holiday schedules";
    document.getElementById("summary").innerHTML =
        ["root","normal","deadend","unreachable","orphan"]
            .filter(c => counts[c])
            .map(c => '<span class="summary-badge" style="background:' + classColors[c] + '">' + classLabels[c] + ': ' + counts[c] + '</span>')
            .join("");

    renderCallFlowTrees(activeEdges);
}}

function renderCallFlowTrees(activeEdges) {{
    const container = document.getElementById("callFlowTrees");
    // Build adjacency: source -> [{label, target}]
    const adj = {{}};
    activeEdges.forEach(e => {{
        (adj[e.source] = adj[e.source] || []).push({{ label: e.label, target: e.target }});
    }});
    // Sort edges: Key entries first (by key), then After:, then Xfer:
    function edgeSortKey(e) {{
        if (e.label.startsWith("Key ")) return "0_" + e.label;
        if (e.label.startsWith("After:")) return "1_" + e.label;
        return "2_" + e.label;
    }}
    Object.values(adj).forEach(edges => edges.sort((a, b) => edgeSortKey(a).localeCompare(edgeSortKey(b))));

    // Find routing rules that connect to call handlers
    const roots = data.nodes.filter(n => n.type === "routingrule" && (adj[n.id] || []).length > 0);
    if (!roots.length) {{
        container.innerHTML = '<p class="muted">No routing rules with connections found.</p>';
        return;
    }}

    let html = "";
    roots.forEach(root => {{
        const target = (adj[root.id] || [])[0];
        if (!target) return;
        const targetNode = nodeMap[target.target];
        if (!targetNode) return;

        function audioLinks(node, indent) {{
            if (!node || !node.audio) return [];
            const audios = node.audio.filter(audioMatchesSchedule);
            if (!audios.length) return [];
            const prefix = "  ".repeat(indent);
            return audios.map(a => prefix + '<a href="' + esc(a.url) + '" target="_blank" class="audio-link">&#9835; ' + esc(a.greeting) + ' greeting</a>');
        }}

        let lines = [];
        // Show conditions on the root line
        const conds = root.conditions || [];
        const condStr = conds.length
            ? ' <span class="flow-muted">[' + conds.map(c => esc(c.param) + " " + esc(c.op) + " " + esc(c.value)).join(", ") + ']</span>'
            : "";
        lines.push('<span class="flow-root">' + esc(root.name) + '</span>' + condStr + ' -> <span class="flow-handler">' + esc(targetNode.name) + (targetNode.extension ? " (" + esc(targetNode.extension) + ")" : "") + '</span>' + (targetNode.scheduleName ? ' <span class="flow-muted">[' + esc(targetNode.scheduleName) + ']</span>' : ""));
        lines.push(...audioLinks(targetNode, 1));

        // BFS tree with depth tracking
        const visited = new Set([root.id]);
        function walk(nodeId, indent) {{
            if (!adj[nodeId]) return;
            adj[nodeId].forEach(edge => {{
                const tgt = nodeMap[edge.target];
                const name = tgt ? tgt.name : "?";
                const ext = tgt && tgt.extension ? " (" + esc(tgt.extension) + ")" : "";
                const prefix = "  ".repeat(indent);
                if (visited.has(edge.target)) {{
                    lines.push(prefix + '<span class="flow-label">[' + esc(edge.label) + ']</span> -> <span class="flow-visited">' + esc(name) + ext + ' (see above)</span>');
                    return;
                }}
                visited.add(edge.target);
                lines.push(prefix + '<span class="flow-label">[' + esc(edge.label) + ']</span> -> <span class="flow-handler">' + esc(name) + ext + '</span>');
                lines.push(...audioLinks(tgt, indent + 1));
                walk(edge.target, indent + 1);
            }});
        }}
        visited.add(target.target);
        walk(target.target, 1);

        html += '<div class="flow-tree">' + lines.join("\n") + '</div>';
    }});

    container.innerHTML = html;
}}

// Render schedules table (static)
(function() {{
    const tbody = document.querySelector("#scheduleTable tbody");
    if (!data.schedules.length) {{
        tbody.innerHTML = '<tr><td colspan="5" class="muted">No schedules found</td></tr>';
        return;
    }}
    data.schedules.forEach(s => {{
        if (!s.details.length) {{
            const tr = document.createElement("tr");
            tr.innerHTML = '<td>' + esc(s.name) + '</td><td colspan="4" class="muted">No time blocks configured</td>';
            tbody.appendChild(tr);
            return;
        }}
        s.details.forEach(d => {{
            const days = d.startDay === d.endDay ? d.startDay : d.startDay + " &ndash; " + d.endDay;
            const tr = document.createElement("tr");
            tr.innerHTML = '<td>' + esc(s.name) + '</td><td>' + days + '</td><td>' + esc(d.startTime) + '</td><td>' + esc(d.endTime) + '</td><td>' + (d.active ? "Yes" : '<span class="muted">No</span>') + '</td>';
            tbody.appendChild(tr);
        }});
    }});
}})();

// Render holiday table (static)
(function() {{
    const tbody = document.querySelector("#holidayTable tbody");
    if (!data.holidays.length) {{
        tbody.innerHTML = '<tr><td colspan="4" class="muted">No holiday schedules found</td></tr>';
        return;
    }}
    data.holidays.forEach(s => {{
        if (!s.entries.length) {{
            const tr = document.createElement("tr");
            tr.innerHTML = '<td>' + esc(s.name) + '</td><td colspan="3" class="muted">No holidays configured</td>';
            tbody.appendChild(tr);
            return;
        }}
        s.entries.forEach(h => {{
            const tr = document.createElement("tr");
            tr.innerHTML = '<td>' + esc(s.name) + '</td><td>' + esc(h.name) + '</td><td>' + esc(h.start) + '</td><td>' + esc(h.end) + '</td>';
            tbody.appendChild(tr);
        }});
    }});
}})();

// Initial render
renderTable();

// --- Debug Tools ---
function toggleDebug() {{
    const panel = document.getElementById("debugPanel");
    panel.style.display = panel.style.display === "none" ? "block" : "none";
}}

function debugLookup() {{
    const q = document.getElementById("debugQuery").value.trim().toLowerCase();
    const out = document.getElementById("debugOutput");
    if (!q) {{ out.textContent = "Enter a name, extension, or Object ID to search."; return; }}

    const matchedNodes = data.nodes.filter(n =>
        n.name.toLowerCase().includes(q) ||
        (n.extension && n.extension.toLowerCase().includes(q)) ||
        n.id.toLowerCase().includes(q)
    );

    if (!matchedNodes.length) {{ out.textContent = "No matching nodes found."; return; }}

    const results = matchedNodes.map(n => {{
        const outEdges = data.edges.filter(e => e.source === n.id);
        const inEdges = data.edges.filter(e => e.target === n.id);
        return {{
            node: n,
            outgoing: outEdges.map(e => ({{
                label: e.label,
                schedule: e.schedule,
                target_id: e.target,
                target_name: (nodeMap[e.target] || {{}}).name || "?"
            }})),
            incoming: inEdges.map(e => ({{
                label: e.label,
                schedule: e.schedule,
                source_id: e.source,
                source_name: (nodeMap[e.source] || {{}}).name || "?"
            }}))
        }};
    }});
    out.textContent = JSON.stringify(results, null, 2);
}}

function debugDumpAll() {{
    const out = document.getElementById("debugOutput");
    out.textContent = JSON.stringify(data, null, 2);
}}

function debugOrphans() {{
    const out = document.getElementById("debugOutput");
    const report = {{}};

    // True orphans: zero connections
    report.trueOrphans = data.nodes
        .filter(n => n.classification === "orphan")
        .map(n => ({{ name: n.name, extension: n.extension, id: n.id }}));

    // Unreachable: have edges but no path from any routing rule
    report.unreachable = data.nodes
        .filter(n => n.classification === "unreachable")
        .map(n => {{
            const outEdges = data.edges.filter(e => e.source === n.id);
            const inEdges = data.edges.filter(e => e.target === n.id);
            return {{
                name: n.name, extension: n.extension, id: n.id,
                connectsTo: outEdges.map(e => (nodeMap[e.target] || {{}}).name || e.target),
                connectedFrom: inEdges.map(e => (nodeMap[e.source] || {{}}).name || e.source)
            }};
        }});

    // Dead ends: reachable but callers get stuck
    report.deadEnds = data.nodes
        .filter(n => n.classification === "deadend")
        .map(n => {{
            const inEdges = data.edges.filter(e => e.target === n.id);
            return {{
                name: n.name, extension: n.extension, id: n.id,
                reachedVia: inEdges.map(e => (nodeMap[e.source] || {{}}).name + " (" + e.label + ")")
            }};
        }});

    // Schedule gaps: reachable in some schedules but not others
    report.scheduleGaps = data.nodes
        .filter(n => n.reachable && n.type === "callhandler" && n.classification !== "orphan" &&
            !(n.reachable.standard && n.reachable.offhours && n.reachable.holiday))
        .map(n => ({{
            name: n.name, extension: n.extension, id: n.id,
            reachableDuring: {{
                standard: n.reachable.standard,
                offhours: n.reachable.offhours,
                holiday: n.reachable.holiday
            }}
        }}));

    // Per-schedule edge counts
    const scheduleCounts = {{}};
    data.edges.forEach(e => scheduleCounts[e.schedule] = (scheduleCounts[e.schedule] || 0) + 1);
    report.edgesBySchedule = scheduleCounts;

    report.summary = {{
        totalHandlers: data.nodes.filter(n => n.type === "callhandler").length,
        trueOrphans: report.trueOrphans.length,
        unreachable: report.unreachable.length,
        deadEnds: report.deadEnds.length,
        scheduleGaps: report.scheduleGaps.length
    }};

    out.textContent = JSON.stringify(report, null, 2);
}}
function copyDebugOutput() {{
    const text = document.getElementById("debugOutput").textContent;
    navigator.clipboard.writeText(text).then(() => {{
        const btn = document.getElementById("copyBtn");
        btn.textContent = "Copied!";
        setTimeout(() => btn.textContent = "Copy Output", 1500);
    }});
}}
</script>

<button class="debug-toggle" onclick="toggleDebug()">Debug Tools</button>
<div id="debugPanel">
<h2>Debug Tools</h2>
<div class="debug-bar">
<input type="text" id="debugQuery" placeholder="Search by name, extension, or Object ID..." onkeydown="if(event.key==='Enter')debugLookup()">
<button class="debug-btn" onclick="debugLookup()">Lookup Node</button>
<button class="debug-btn" onclick="debugOrphans()">Find Problems</button>
<button class="debug-btn" onclick="debugDumpAll()">Dump All Data</button>
<button class="debug-btn" onclick="copyDebugOutput()" id="copyBtn">Copy Output</button>
</div>
<pre id="debugOutput">Use the tools above to inspect raw data.

&bull; Lookup Node &mdash; search for a handler by name, extension, or ID to see its full data, all connections, and schedule tags
&bull; Find Problems &mdash; list dead ends, orphans, unreachable nodes, and edge counts per schedule
&bull; Dump All Data &mdash; export the complete JSON dataset (nodes, edges, schedules, holidays)</pre>
</div>
</body>
</html>'''


def ping_check(host):
    """Ping the host to verify network connectivity before attempting API calls."""
    hostname = urlparse(host).hostname or host
    print(f"Checking connectivity to {hostname}...")
    param = "-n" if platform.system().lower() == "windows" else "-c"
    try:
        result = subprocess.run(
            ["ping", param, "2", hostname],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"\nError: Cannot reach {hostname}.")
            print("Are you connected to the VPN?")
            sys.exit(1)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print(f"\nError: Cannot reach {hostname}.")
        print("Are you connected to the VPN?")
        sys.exit(1)
    print(f"  {hostname} is reachable.")


def connect(args):
    """Create an authenticated session from CLI args."""
    host = args.host.rstrip("/")
    ping_check(host)
    password = getpass.getpass(f"Password for {args.user}@{host}: ")
    session = requests.Session()
    session.auth = (args.user, password)
    return session, host


class TeeLogger:
    """Write to both stdout and a log file."""
    def __init__(self, log_path):
        self.terminal = sys.stdout
        self.log = open(log_path, "w", encoding="utf-8")

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()
        sys.stdout = self.terminal


def cmd_generate(args):
    """Full report generation (default command)."""
    session, host = connect(args)

    print("Identifying site...")
    site_id = fetch_site_id(session, host)
    print(f"  Site: {site_id}")

    site_dir = prepare_site_dir(site_id)

    # Start logging to file
    log_path = os.path.join(site_dir, "run.log")
    tee = TeeLogger(log_path)
    sys.stdout = tee
    print(f"Log: {log_path}")
    print(f"Site: {site_id}")
    print(f"Host: {host}")
    print(f"User: {args.user}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    try:
        call_handlers = fetch_call_handlers(session, host)
        interview_handlers = fetch_interview_handlers(session, host)
        routing_rules = fetch_routing_rules(session, host)
    except requests.exceptions.ConnectionError as e:
        print(f"Error: Could not connect to {host}: {e}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"Error: API request failed: {e}")
        sys.exit(1)

    # Non-critical data — continue if endpoints are unavailable
    try:
        holiday_schedules = fetch_holiday_schedules(session, host)
    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
        print(f"  Warning: Could not fetch holiday schedules: {e}")
        holiday_schedules = []

    try:
        schedules = fetch_schedules(session, host)
    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
        print(f"  Warning: Could not fetch schedules: {e}")
        schedules = []

    try:
        schedule_sets = fetch_schedule_sets(session, host)
    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
        print(f"  Warning: Could not fetch schedule sets: {e}")
        schedule_sets = []

    # Build schedule set OID -> display name lookup
    schedule_set_map = {s["ObjectId"]: s.get("DisplayName", "") for s in schedule_sets}

    print(f"\nFound {len(call_handlers)} call handlers, "
          f"{len(interview_handlers)} interview handlers, "
          f"{len(routing_rules)} routing rules, "
          f"{len(holiday_schedules)} holiday schedules, "
          f"{len(schedules)} schedules, "
          f"{len(schedule_sets)} schedule sets")

    print("\nBuilding graph (fetching menu entries, transfer rules, greetings, rule conditions)...")
    nodes, edges = build_graph(call_handlers, interview_handlers, routing_rules, session, host,
                               schedule_set_map=schedule_set_map)

    # Summary
    classifications = {}
    for n in nodes:
        c = n["classification"]
        classifications[c] = classifications.get(c, 0) + 1

    print(f"\nGraph: {len(nodes)} nodes, {len(edges)} edges")
    for cls, count in sorted(classifications.items()):
        print(f"  {cls}: {count}")

    d3_local = download_d3(site_dir)

    map_path = os.path.join(site_dir, "callhandler_map.html")
    report_path = os.path.join(site_dir, "callhandler_report.html")

    print(f"\nGenerating {map_path}...")
    html = generate_html(nodes, edges, d3_local=d3_local)
    with open(map_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Generating {report_path}...")
    table_html = generate_table_html(nodes, edges, holiday_schedules, schedules)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(table_html)

    print(f"\nDone! Reports written to {site_dir}/")
    print(f"  Open {map_path} (graph) or {report_path} (table) in a browser.")
    tee.close()


def cmd_query(args):
    """Query a specific CUPI API path and dump raw JSON."""
    session, host = connect(args)
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    try:
        data = api_get(session, host, path)
        print(json.dumps(data, indent=2))
    except requests.exceptions.HTTPError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_handler(args):
    """Look up a call handler by name or extension and dump its details."""
    session, host = connect(args)
    call_handlers = fetch_call_handlers(session, host)

    q = args.search.lower()
    matches = [ch for ch in call_handlers if
               q in ch.get("DisplayName", "").lower() or
               q == ch.get("DtmfAccessId", "").lower() or
               q in ch.get("ObjectId", "").lower()]

    if not matches:
        print(f"No handlers matching '{args.search}'")
        sys.exit(1)

    for ch in matches:
        oid = ch.get("ObjectId", "")
        name = ch.get("DisplayName", "")
        print(f"\n{'='*60}")
        print(f"Handler: {name}")
        print(f"Extension: {ch.get('DtmfAccessId', 'N/A')}")
        print(f"ObjectId: {oid}")
        print(f"{'='*60}")

        print("\n--- Transfer Rules ---")
        for tr in fetch_transfer_rules(session, host, oid, name):
            rule_idx = tr.get("RuleIndex", "?")
            print(f"  [{rule_idx}] Enabled={tr.get('TransferEnabled')} "
                  f"Extension={tr.get('Extension', '')} "
                  f"Target={tr.get('TargetHandlerObjectId', 'N/A')}")

        print("\n--- Greetings ---")
        for gr in fetch_greetings(session, host, oid, name):
            gt = gr.get("GreetingType", "?")
            play = gr.get("PlayWhat", "?")
            action = gr.get("AfterGreetingAction", "?")
            target = gr.get("AfterGreetingTargetHandlerObjectId", "")
            print(f"  [{gt}] PlayWhat={play} AfterAction={action} "
                  f"Target={target or 'N/A'}")

        print("\n--- Menu Entries ---")
        for me in fetch_menu_entries(session, host, oid, name):
            key = me.get("TouchtoneKey", "?")
            action = me.get("Action", "?")
            target = me.get("TargetHandlerObjectId", "")
            print(f"  Key {key}: Action={action} Target={target or 'N/A'}")

        if args.raw:
            print("\n--- Raw Handler JSON ---")
            print(json.dumps(ch, indent=2))


PROBE_ENDPOINTS = [
    # Core
    ("/vmrest/handlers/callhandlers", "Call Handlers"),
    ("/vmrest/handlers/interviewhandlers", "Interview Handlers"),
    ("/vmrest/routingrules", "Routing Rules"),
    ("/vmrest/routingruleconditions", "Routing Rule Conditions"),
    # Schedules
    ("/vmrest/schedules", "Schedules"),
    ("/vmrest/schedulesets", "Schedule Sets"),
    ("/vmrest/holidayschedules", "Holiday Schedules"),
    ("/vmrest/holidayschedulesets", "Holiday Schedule Sets"),
    ("/vmrest/schedules/{sched_id}/scheduledetails", "Schedule Details (sample)"),
    ("/vmrest/schedulesets/{schedset_id}/schedulessetmembers", "Schedule Set Members (sample)"),
    ("/vmrest/schedulesets/{schedset_id}/schedulesetmembers", "Schedule Set Members alt (sample)"),
    # Users & contacts
    ("/vmrest/users", "Users"),
    ("/vmrest/contacts", "Contacts"),
    ("/vmrest/distributionlists", "Distribution Lists"),
    # System
    ("/vmrest/cluster", "Cluster"),
    ("/vmrest/vmsservers", "VMS Servers"),
    ("/vmrest/systemconfig", "System Config"),
    ("/vmrest/portgroups", "Port Groups"),
    ("/vmrest/ports", "Ports"),
    ("/vmrest/phonerecordings", "Phone Recordings"),
    # Call handler sub-resources (tested against first handler found)
    ("/vmrest/handlers/callhandlers/{id}/menuentries", "Menu Entries (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/transferrules", "Transfer Rules (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/transferoptions", "Transfer Options (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/greetings", "Greetings (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/callerInput", "Caller Input (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/callerinput", "Caller Input lowercase (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/callhandlerowner", "Call Handler Owner (sample)"),
    ("/vmrest/handlers/callhandlers/{id}/transferrule", "Transfer Rule singular (sample)"),
    # Other
    ("/vmrest/callhandlertemplates", "Call Handler Templates"),
    ("/vmrest/notificationdevices", "Notification Devices"),
    ("/vmrest/smpproviders", "SMPP Providers"),
    ("/vmrest/tenants", "Tenants"),
    ("/vmrest/timezones", "Time Zones"),
    ("/vmrest/partitions", "Partitions"),
    ("/vmrest/searchspaces", "Search Spaces"),
    ("/vmrest/cosses", "Classes of Service"),
    ("/vmrest/restrictionpatterns", "Restriction Patterns"),
    ("/vmrest/restrictiontables", "Restriction Tables"),
    ("/vmrest/ldapdirectories", "LDAP Directories"),
    ("/vmrest/externalservices", "External Services"),
    ("/vmrest/policies", "Policies"),
]


def cmd_probe(args):
    """Probe known CUPI endpoints to see what's available on this server."""
    session, host = connect(args)

    # Get sample IDs for sub-resource probes
    sample_id = None
    sample_sched_id = None
    sample_schedset_id = None
    try:
        data = api_get(session, host, "/vmrest/handlers/callhandlers", {"rowsPerPage": 1})
        handlers = data.get("Callhandler", [])
        if isinstance(handlers, dict):
            handlers = [handlers]
        if handlers:
            sample_id = handlers[0].get("ObjectId", "")
    except Exception:
        pass
    try:
        data = api_get(session, host, "/vmrest/schedules", {"rowsPerPage": 1})
        scheds = data.get("Schedule", [])
        if isinstance(scheds, dict):
            scheds = [scheds]
        if scheds:
            sample_sched_id = scheds[0].get("ObjectId", "")
    except Exception:
        pass
    try:
        data = api_get(session, host, "/vmrest/schedulesets", {"rowsPerPage": 1})
        ssets = data.get("ScheduleSet", [])
        if isinstance(ssets, dict):
            ssets = [ssets]
        if ssets:
            sample_schedset_id = ssets[0].get("ObjectId", "")
    except Exception:
        pass

    print(f"\n{'='*70}")
    print(f"CUPI ENDPOINT PROBE — {host}")
    print(f"{'='*70}\n")

    available = []
    unavailable = []
    errors = []

    id_subs = {
        "{id}": sample_id,
        "{sched_id}": sample_sched_id,
        "{schedset_id}": sample_schedset_id,
    }

    for path, label in PROBE_ENDPOINTS:
        resolved = path
        skip = False
        for placeholder, val in id_subs.items():
            if placeholder in resolved:
                if not val:
                    unavailable.append((path, label, "skipped — no sample ID"))
                    skip = True
                    break
                resolved = resolved.replace(placeholder, val)
        if skip:
            continue

        try:
            url = f"{host}{resolved}"
            resp = session.get(url, params={"rowsPerPage": 1}, headers=HEADERS, verify=False)
            code = resp.status_code
            total = ""
            if code == 200:
                try:
                    data = resp.json()
                    t = data.get("@total", "")
                    if t:
                        total = f" ({t} records)"
                except Exception:
                    pass
                available.append((path, label, f"{code}{total}"))
            elif code == 404:
                unavailable.append((path, label, str(code)))
            else:
                errors.append((path, label, f"{code} {resp.reason}"))
        except requests.exceptions.ConnectionError:
            errors.append((path, label, "connection error"))
        except Exception as e:
            errors.append((path, label, str(e)))

    print(f"  AVAILABLE ({len(available)}):")
    for path, label, info in available:
        print(f"    ✓ {label:<35} {path:<60} {info}")

    if unavailable:
        print(f"\n  NOT FOUND ({len(unavailable)}):")
        for path, label, info in unavailable:
            print(f"    ✗ {label:<35} {path:<60} {info}")

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for path, label, info in errors:
            print(f"    ! {label:<35} {path:<60} {info}")

    print(f"\n{'='*70}")
    print(f"  {len(available)} available / {len(unavailable)} not found / {len(errors)} errors")
    print(f"{'='*70}")


def cmd_schedules(args):
    """List all schedules and their time blocks."""
    session, host = connect(args)

    try:
        schedules = fetch_schedules(session, host)
    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
        print(f"  Warning: Could not fetch schedules: {e}")
        schedules = []

    try:
        holiday_schedules = fetch_holiday_schedules(session, host)
    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
        print(f"  Warning: Could not fetch holiday schedules: {e}")
        holiday_schedules = []

    print(f"\n{'='*60}")
    print("BUSINESS HOUR SCHEDULES")
    print(f"{'='*60}")
    for s in schedules:
        print(f"\n  {s.get('DisplayName', 'Unknown')} ({s.get('ObjectId', '')})")
        for d in s.get("_details", []):
            start_day = DAYS_OF_WEEK.get(str(d.get("StartDayOfWeek", "")), "?")
            end_day = DAYS_OF_WEEK.get(str(d.get("EndDayOfWeek", "")), "?")
            start_time = _format_minutes(d.get("StartTime", ""))
            end_time = _format_minutes(d.get("EndTime", ""))
            active = d.get("IsActive", True)
            days = start_day if start_day == end_day else f"{start_day}-{end_day}"
            print(f"    {days}: {start_time} - {end_time} {'(active)' if active else '(inactive)'}")

    print(f"\n{'='*60}")
    print("HOLIDAY SCHEDULES")
    print(f"{'='*60}")
    for s in holiday_schedules:
        print(f"\n  {s.get('DisplayName', 'Unknown')}")
        for h in s.get("_holidays", []):
            print(f"    {h.get('DisplayName', '?')}: {h.get('StartDate', '?')} - {h.get('EndDate', '?')}")


def cmd_orphans(args):
    """Find orphaned, unreachable, and dead-end call handlers."""
    session, host = connect(args)

    try:
        call_handlers = fetch_call_handlers(session, host)
        interview_handlers = fetch_interview_handlers(session, host)
        routing_rules = fetch_routing_rules(session, host)
    except requests.exceptions.ConnectionError as e:
        print(f"Error: Could not connect to {host}: {e}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"Error: API request failed: {e}")
        sys.exit(1)

    print("\nBuilding graph...")
    nodes, edges = build_graph(call_handlers, interview_handlers, routing_rules, session, host)

    # Group by classification
    by_class = {}
    for n in nodes:
        if n["type"] in ("routingrule", "phone"):
            continue
        by_class.setdefault(n["classification"], []).append(n)

    total_handlers = sum(len(v) for v in by_class.values())

    print(f"\n{'='*70}")
    print(f"ORPHAN ANALYSIS — {total_handlers} call/interview handlers")
    print(f"{'='*70}")

    # True orphans
    orphans = by_class.get("orphan", [])
    print(f"\n  TRUE ORPHANS ({len(orphans)}) — no connections at all:")
    if orphans:
        for n in sorted(orphans, key=lambda x: x["name"].lower()):
            ext = f" (ext {n['extension']})" if n["extension"] else ""
            print(f"    - {n['name']}{ext}")
    else:
        print("    (none)")

    # Unreachable
    unreachable = by_class.get("unreachable", [])
    print(f"\n  UNREACHABLE ({len(unreachable)}) — have edges but no path from any routing rule:")
    if unreachable:
        node_map = {n["id"]: n for n in nodes}
        for n in sorted(unreachable, key=lambda x: x["name"].lower()):
            ext = f" (ext {n['extension']})" if n["extension"] else ""
            out_edges = [e for e in edges if e["source"] == n["id"]]
            in_edges = [e for e in edges if e["target"] == n["id"]]
            connections = []
            for e in out_edges:
                tgt = node_map.get(e["target"], {})
                connections.append(f"→ {tgt.get('name', '?')} [{e['label']}]")
            for e in in_edges:
                src = node_map.get(e["source"], {})
                connections.append(f"← {src.get('name', '?')} [{e['label']}]")
            print(f"    - {n['name']}{ext}")
            for c in connections:
                print(f"        {c}")
    else:
        print("    (none)")

    # Dead ends
    deadends = by_class.get("deadend", [])
    print(f"\n  DEAD ENDS ({len(deadends)}) — reachable but callers get stuck:")
    if deadends:
        node_map = {n["id"]: n for n in nodes}
        for n in sorted(deadends, key=lambda x: x["name"].lower()):
            ext = f" (ext {n['extension']})" if n["extension"] else ""
            in_edges = [e for e in edges if e["target"] == n["id"]]
            via = ", ".join(f"{node_map.get(e['source'], {}).get('name', '?')} [{e['label']}]" for e in in_edges)
            print(f"    - {n['name']}{ext}")
            if via:
                print(f"        reached via: {via}")
    else:
        print("    (none)")

    # Schedule gaps
    schedule_gaps = [n for n in nodes
                     if n.get("reachable") and n["type"] == "callhandler"
                     and n["classification"] not in ("orphan", "unreachable")
                     and not (n["reachable"].get("standard") and n["reachable"].get("offhours") and n["reachable"].get("holiday"))]
    print(f"\n  SCHEDULE GAPS ({len(schedule_gaps)}) — reachable in some schedules but not all:")
    if schedule_gaps:
        for n in sorted(schedule_gaps, key=lambda x: x["name"].lower()):
            ext = f" (ext {n['extension']})" if n["extension"] else ""
            r = n["reachable"]
            missing = []
            if not r.get("standard"):
                missing.append("standard")
            if not r.get("offhours"):
                missing.append("off-hours")
            if not r.get("holiday"):
                missing.append("holiday")
            print(f"    - {n['name']}{ext}")
            print(f"        NOT reachable during: {', '.join(missing)}")
    else:
        print("    (none)")

    # Summary
    normal = len(by_class.get("normal", []))
    roots = len(by_class.get("root", []))
    print(f"\n{'='*70}")
    print(f"  Roots: {roots}  |  Normal: {normal}  |  Orphans: {len(orphans)}  |  "
          f"Unreachable: {len(unreachable)}  |  Dead Ends: {len(deadends)}  |  Schedule Gaps: {len(schedule_gaps)}")
    print(f"{'='*70}")


def main():
    parser = argparse.ArgumentParser(
        description="CUC Call Handler Wizard — CUPI routing visualizer and debug tool."
    )
    parser.add_argument("--host", required=True, help="CUC server URL (e.g. https://10.212.111.17)")
    parser.add_argument("--user", required=True, help="CUC admin username")

    subparsers = parser.add_subparsers(dest="command")

    # generate (default)
    sub_gen = subparsers.add_parser("generate", help="Generate HTML report and graph (default)")

    # query — raw API path
    sub_query = subparsers.add_parser("query", help="Query a raw CUPI API path and dump JSON")
    sub_query.add_argument("path", help="API path (e.g. /vmrest/handlers/callhandlers)")

    # handler — lookup a specific handler
    sub_handler = subparsers.add_parser("handler", help="Look up a call handler by name, extension, or ID")
    sub_handler.add_argument("search", help="Handler name, extension, or Object ID to search for")
    sub_handler.add_argument("--raw", action="store_true", help="Also dump raw JSON for the handler")

    # schedules — list all schedules
    sub_sched = subparsers.add_parser("schedules", help="List all schedules and holiday schedules")

    # orphans — find unreachable handlers
    sub_orphans = subparsers.add_parser("orphans", help="Find orphaned, unreachable, and dead-end handlers")

    # probe — test what endpoints exist
    sub_probe = subparsers.add_parser("probe", help="Probe CUPI endpoints to see what's available on this server")

    args = parser.parse_args()

    if args.command is None or args.command == "generate":
        cmd_generate(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "handler":
        cmd_handler(args)
    elif args.command == "schedules":
        cmd_schedules(args)
    elif args.command == "orphans":
        cmd_orphans(args)
    elif args.command == "probe":
        cmd_probe(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted. Exiting.")
        sys.exit(0)
