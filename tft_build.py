#!/usr/bin/env python3
"""
tft_build.py — TFT Double Up Dashboard Builder
───────────────────────────────────────────────
Reads a dataset produced by tft_fetch.py and generates a self-contained
HTML dashboard. No API calls — runs in seconds.

Usage:
    python tft_build.py
    python tft_build.py --open
    python tft_build.py --region euw1 --min-samples 2 --open

Options:
    --data          Input JSON file (default: tft_data.json)
    --output        Output HTML file (default: tft_dashboard.html)
    --min-samples   Minimum games to show a comp pair (default: 3)
    --region        Filter to one region (e.g. euw1); default: all
    --open          Open in browser when done
"""

import argparse
import json
import re
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Comp analysis ────────────────────────────────────────────────────────────────
def clean_id(raw: str) -> str:
    """TFT14_SomeChampion → Some Champion"""
    if not raw:
        return "?"
    s = re.sub(r"^TFT\d*_", "", raw)
    s = s.replace("_", " ")
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return " ".join(s.split())


def match_patch(match: dict) -> str:
    """Extract patch like '14.24' from game_version field."""
    gv = match.get("info", {}).get("game_version", "")
    m = re.search(r"(\d+\.\d+)\.", gv)
    return m.group(1) if m else "?"


def patch_key(p: str) -> tuple:
    try:
        return tuple(int(x) for x in p.split("."))
    except ValueError:
        return (0, 0)


def comp_signature(participant: dict) -> tuple[str, list]:
    active = [
        t for t in participant.get("traits", [])
        if t.get("tier_current", 0) > 0 and t.get("num_units", 0) >= 2
    ]
    active.sort(key=lambda t: (t.get("style", 0), t.get("num_units", 0)), reverse=True)
    top    = active[:3]
    sig    = " + ".join(f"{clean_id(t['name'])} {t['num_units']}" for t in top) or "No active traits"
    traits = [{"name": clean_id(t["name"]), "count": t["num_units"], "style": t.get("style", 0)}
              for t in top]
    return sig, traits


def participant_units(p: dict) -> list[str]:
    units = sorted(p.get("units", []),
                   key=lambda u: (u.get("rarity", 0), u.get("tier", 0)), reverse=True)
    return [clean_id(u.get("character_id", "?")) for u in units]


def find_pairs(participants: list) -> list[tuple]:
    """
    Identify partner pairs.
    Use partner_group_id only when it covers every participant;
    otherwise fall back to shared-placement grouping.
    """
    by_pgid: dict = defaultdict(list)
    for p in participants:
        gid = p.get("partner_group_id")
        if gid is not None:
            by_pgid[gid].append(p)
    pairs = [tuple(v) for v in by_pgid.values() if len(v) == 2]
    if len(pairs) * 2 == len(participants):
        return pairs

    by_place: dict = defaultdict(list)
    for p in participants:
        pl = p.get("placement")
        if pl is not None:
            by_place[pl].append(p)
    return [tuple(v) for v in by_place.values() if len(v) == 2]


def process_matches(matches: list) -> list:
    """Return pair records for all Double Up matches, tagged with patch."""
    records = []
    skipped = 0
    for match in matches:
        if not match:
            continue
        info = match.get("info", {})
        if info.get("queue_id") != 1160:
            skipped += 1
            continue
        patch = match_patch(match)
        for p1, p2 in find_pairs(info.get("participants", [])):
            # Individual placements are 1-8; convert to team rank 1-4
            raw1 = p1.get("placement", 9)
            raw2 = p2.get("placement", 9)
            placement = (min(raw1, raw2) + 1) // 2
            sig1, tr1 = comp_signature(p1)
            sig2, tr2 = comp_signature(p2)
            if sig1 > sig2:
                sig1, sig2 = sig2, sig1
                p1,   p2   = p2,   p1
                tr1,  tr2  = tr2,  tr1
            records.append({
                "pair_sig": f"{sig1}  //  {sig2}",
                "sig1": sig1, "sig2": sig2,
                "traits1": tr1, "traits2": tr2,
                "placement": placement,
                "units1": participant_units(p1), "units2": participant_units(p2),
                "augs1":  [clean_id(a) for a in p1.get("augments", [])],
                "augs2":  [clean_id(a) for a in p2.get("augments", [])],
                "puuid1": p1.get("puuid", ""),   "puuid2": p2.get("puuid", ""),
                "patch":  patch,
            })
    if skipped:
        print(f"  (skipped {skipped} non-Double-Up matches)")
    return records


def aggregate_trait_intel(records: list, min_games: int = 3) -> dict:
    """
    From pair records, produce:
      solo  — traits grouped by base name (e.g. "4 Piltover" + "6 Piltover" → "Piltover"),
              each variant includes top units
      pairs — per (trait1, trait2) Double Up synergy stats
    """
    solo_pls:   dict = defaultdict(list)
    solo_units: dict = defaultdict(Counter)
    pair_pls:   dict = defaultdict(list)

    for r in records:
        lab1 = f"{r['traits1'][0]['count']} {r['traits1'][0]['name']}" if r["traits1"] else ""
        lab2 = f"{r['traits2'][0]['count']} {r['traits2'][0]['name']}" if r["traits2"] else ""
        pl   = r["placement"]
        if lab1:
            solo_pls[lab1].append(pl)
            for u in r["units1"]: solo_units[lab1][u] += 1
        if lab2:
            solo_pls[lab2].append(pl)
            for u in r["units2"]: solo_units[lab2][u] += 1
        if lab1 and lab2:
            pair_pls[tuple(sorted([lab1, lab2]))].append(pl)

    def make_stats(pls: list) -> dict:
        n    = len(pls)
        avg  = sum(pls) / n
        win  = sum(1 for p in pls if p == 1) / n
        top2 = sum(1 for p in pls if p <= 2) / n
        dist = [round(sum(1 for p in pls if p == i) / n * 100) for i in range(1, 5)]
        return {
            "games":    n,
            "avg":      round(avg,  2),
            "win_pct":  round(win  * 100, 1),
            "top2_pct": round(top2 * 100, 1),
            "dist":     dist,
        }

    # Build variant list, group by base trait name
    variants = [
        {"trait": lab, "top_units": [u for u, _ in solo_units[lab].most_common(8)],
         **make_stats(pls)}
        for lab, pls in solo_pls.items() if len(pls) >= min_games
    ]
    groups_dict: dict = defaultdict(list)
    for v in variants:
        base = " ".join(v["trait"].split()[1:])   # "6 Piltover" → "Piltover"
        groups_dict[base].append(v)

    grouped = []
    for base, vs in groups_dict.items():
        vs.sort(key=lambda x: int(x["trait"].split()[0]))   # 4 → 6 → 8
        total_games = sum(v["games"] for v in vs)
        g_avg  = sum(v["avg"]      * v["games"] for v in vs) / total_games
        g_win  = sum(v["win_pct"]  * v["games"] for v in vs) / total_games / 100
        g_top2 = sum(v["top2_pct"] * v["games"] for v in vs) / total_games / 100
        tier = (
            "S" if g_avg <= 1.8 and g_win  >= 0.25 else
            "A" if g_avg <= 2.3 and g_top2 >= 0.55 else
            "B" if g_avg <= 3.0 else
            "C"
        )
        grouped.append({
            "name": base, "variants": vs,
            "best_avg": min(v["avg"] for v in vs),
            "total_games": total_games, "tier": tier,
        })
    grouped.sort(key=lambda g: g["best_avg"])

    pairs = sorted(
        [{"trait1": k[0], "trait2": k[1], **make_stats(pls)} for k, pls in pair_pls.items() if len(pls) >= min_games],
        key=lambda x: (x["avg"], -x["win_pct"]),
    )
    return {"solo": grouped, "pairs": pairs}


def aggregate_comps(records: list, min_samples: int = 3) -> list:
    groups: dict = defaultdict(lambda: {
        "placements": [],
        "units1_bag": Counter(), "units2_bag": Counter(),
        "aug1_bag":   Counter(), "aug2_bag":   Counter(),
        "sig1": None, "sig2": None,
    })
    for r in records:
        g = groups[r["pair_sig"]]
        g["placements"].append(r["placement"])
        for u in r["units1"]: g["units1_bag"][u] += 1
        for u in r["units2"]: g["units2_bag"][u] += 1
        for a in r["augs1"]:  g["aug1_bag"][a]   += 1
        for a in r["augs2"]:  g["aug2_bag"][a]   += 1
        if g["sig1"] is None:
            g["sig1"] = r["sig1"]
            g["sig2"] = r["sig2"]

    results = []
    for pair_sig, g in groups.items():
        n = len(g["placements"])
        if n < min_samples:
            continue
        avg  = sum(g["placements"]) / n
        win  = sum(1 for p in g["placements"] if p == 1) / n
        top2 = sum(1 for p in g["placements"] if p <= 2) / n
        tier = (
            "S" if avg <= 1.8 and win  >= 0.25 else
            "A" if avg <= 2.3 and top2 >= 0.55 else
            "B" if avg <= 3.0 else
            "C"
        )
        results.append({
            "pair_sig": pair_sig,
            "sig1":     g["sig1"],
            "sig2":     g["sig2"],
            "tier":     tier,
            "avg":      round(avg, 2),
            "win_pct":  round(win  * 100, 1),
            "top2_pct": round(top2 * 100, 1),
            "samples":  n,
            "units1":   [u for u, _ in g["units1_bag"].most_common(8)],
            "units2":   [u for u, _ in g["units2_bag"].most_common(8)],
            "augs1":    [a for a, _ in g["aug1_bag"].most_common(3)],
            "augs2":    [a for a, _ in g["aug2_bag"].most_common(3)],
        })
    results.sort(key=lambda x: (x["avg"], -x["win_pct"]))
    return results


def _primary_trait(participant: dict) -> str:
    """Return e.g. '6 Piltover' for a participant, or '' if none active."""
    active = [t for t in participant.get("traits", [])
              if t.get("tier_current", 0) > 0 and t.get("num_units", 0) >= 2]
    if not active:
        return ""
    active.sort(key=lambda t: (t.get("style", 0), t.get("num_units", 0)), reverse=True)
    t = active[0]
    return f"{t['num_units']} {clean_id(t['name'])}"


def build_match_records(matches: list, players: dict) -> list:
    """Return one record per tracked-player team per match, tagged with patch."""
    records = []
    for match in matches:
        if not match:
            continue
        info = match.get("info", {})
        if info.get("queue_id") != 1160:
            continue

        patch    = match_patch(match)
        match_id = match.get("metadata", {}).get("match_id", "")
        ts = info.get("game_datetime", 0)
        if ts > 1e12:
            ts /= 1000
        game_date = (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d · %H:%M")
                     if ts else "")

        for p1, p2 in find_pairs(info.get("participants", [])):
            pu1, pu2 = p1.get("puuid", ""), p2.get("puuid", "")
            if pu1 not in players and pu2 not in players:
                continue
            # Individual placements are 1-8; convert to team rank 1-4
            raw1 = p1.get("placement", 9)
            raw2 = p2.get("placement", 9)
            placement = (min(raw1, raw2) + 1) // 2
            lp1 = players.get(pu1, {}).get("lp", 0)
            lp2 = players.get(pu2, {}).get("lp", 0)
            sig1, _ = comp_signature(p1)
            sig2, _ = comp_signature(p2)
            records.append({
                "match_id":  match_id,
                "game_date": game_date,
                "ts":        ts,
                "patch":     patch,
                "placement": placement,
                "max_lp":    max(lp1, lp2),
                "p1": {"puuid": pu1, "comp": sig1,
                       "units": participant_units(p1),
                       "augs":  [clean_id(a) for a in p1.get("augments", [])],
                       "level": p1.get("level", 0),
                       "ptrait": _primary_trait(p1)},
                "p2": {"puuid": pu2, "comp": sig2,
                       "units": participant_units(p2),
                       "augs":  [clean_id(a) for a in p2.get("augments", [])],
                       "level": p2.get("level", 0),
                       "ptrait": _primary_trait(p2)},
            })
    records.sort(key=lambda r: (-r["max_lp"], -r["ts"]))
    return records


# ── HTML template ────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TFT Double Up — {region_label}</title>
  <style>
    :root {{
      --bg:#0B0D14;--surface:#111520;--surface2:#1A1F30;--border:#252A3A;
      --text:#E8E8E8;--muted:#8892A4;--gold:#C89B3C;--gl:#F4D58D;
      --ts:#C89B3C;--ta:#9D4EDD;--tb:#4285F4;--tc:#6B7280;
      --p1:rgba(66,133,244,.07);--p2:rgba(157,78,221,.07);
      --pl1:#C89B3C;--pl2:#9AA4AF;--pl3:#CD7F32;--pl4:#4A5568;
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}}

    header{{background:linear-gradient(180deg,#0D1020 0%,var(--bg) 100%);border-bottom:1px solid var(--border);
      padding:20px 28px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}}
    h1{{font-size:19px;font-weight:700;color:var(--gl)}}
    .sub{{font-size:12px;color:var(--muted);margin-top:3px}}
    .pills{{display:flex;gap:8px;flex-wrap:wrap}}
    .pill{{background:var(--surface2);border:1px solid var(--border);border-radius:6px;
      padding:4px 12px;font-size:12px;color:var(--muted)}}
    .pill span{{color:var(--gl);font-weight:600}}

    .controls{{padding:10px 28px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;
      border-bottom:1px solid var(--border);background:var(--bg);position:sticky;top:0;z-index:10}}
    .tab{{background:transparent;border:none;color:var(--muted);padding:6px 16px;font-size:14px;
      font-weight:600;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;transition:.15s}}
    .tab.on{{color:var(--gl);border-bottom-color:var(--gold)}}
    .sep{{width:1px;height:20px;background:var(--border);margin:0 4px}}
    .flabel{{font-size:12px;color:var(--muted)}}
    .fbtn{{background:var(--surface);border:1px solid var(--border);color:var(--muted);
      padding:4px 13px;border-radius:20px;cursor:pointer;font-size:12px;transition:.15s}}
    .fbtn:hover{{border-color:var(--gold);color:var(--text)}}
    .fbtn.on{{color:#0B0D14;font-weight:700}}
    .fbtn[data-t="ALL"].on{{background:var(--gold);border-color:var(--gold)}}
    .fbtn[data-t="S"].on{{background:var(--ts);border-color:var(--ts)}}
    .fbtn[data-t="A"].on{{background:var(--ta);border-color:var(--ta);color:#fff}}
    .fbtn[data-t="B"].on{{background:var(--tb);border-color:var(--tb);color:#fff}}
    .fbtn[data-t="C"].on{{background:var(--tc);border-color:var(--tc);color:#fff}}
    select.ctrl{{background:var(--surface);border:1px solid var(--border);color:var(--text);
      padding:4px 10px;border-radius:6px;font-size:12px;cursor:pointer}}
    .badge{{background:var(--surface2);border:1px solid var(--border);border-radius:6px;
      padding:3px 9px;font-size:11px;color:var(--muted)}}
    .ssort{{background:var(--surface);border:1px solid var(--border);color:var(--text);
      padding:4px 10px;border-radius:6px;font-size:12px;cursor:pointer;margin-left:auto}}
    #tfilters{{display:none;align-items:center;gap:6px}}

    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:12px;padding:20px 28px}}

    .card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;
      overflow:hidden;transition:transform .12s,border-color .12s}}
    .card:hover{{transform:translateY(-2px);border-color:#3A4050}}

    /* match card */
    .mhdr{{display:flex;align-items:center;justify-content:space-between;
      padding:10px 14px;border-bottom:1px solid var(--border)}}
    .pbadge{{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:700}}
    .pdot{{width:10px;height:10px;border-radius:50%}}
    .p1 .pdot{{background:var(--pl1)}}.p1{{color:var(--pl1)}}
    .p2 .pdot{{background:var(--pl2)}}.p2{{color:var(--pl2)}}
    .p3 .pdot{{background:var(--pl3)}}.p3{{color:var(--pl3)}}
    .p4 .pdot{{background:var(--pl4)}}.p4{{color:var(--pl4)}}
    .minfo{{display:flex;align-items:center;gap:8px}}
    .gdate{{font-size:11px;color:var(--muted)}}
    .patchbadge{{font-size:10px;color:var(--muted);background:var(--surface2);
      border:1px solid var(--border);border-radius:3px;padding:1px 6px}}

    /* comp card */
    .cbar{{height:3px}}
    .cbar[data-t="S"]{{background:var(--ts)}}.cbar[data-t="A"]{{background:var(--ta)}}
    .cbar[data-t="B"]{{background:var(--tb)}}.cbar[data-t="C"]{{background:var(--tc)}}
    .chdr{{display:flex;align-items:center;gap:10px;padding:11px 14px 9px;border-bottom:1px solid var(--border)}}
    .tbadge{{width:24px;height:24px;border-radius:4px;flex-shrink:0;display:flex;
      align-items:center;justify-content:center;font-weight:800;font-size:12px}}
    .tbadge[data-t="S"]{{background:var(--ts);color:#0B0D14}}
    .tbadge[data-t="A"]{{background:var(--ta);color:#fff}}
    .tbadge[data-t="B"]{{background:var(--tb);color:#fff}}
    .tbadge[data-t="C"]{{background:var(--tc);color:#fff}}
    .pnames{{flex:1;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;min-width:0}}
    .cname{{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .arrow{{font-size:12px;color:var(--muted);flex-shrink:0}}

    /* shared pair body */
    .pbody{{display:grid;grid-template-columns:1fr 1fr}}
    .pcol{{padding:9px 12px}}
    .pcol:first-child{{background:var(--p1);border-right:1px solid var(--border)}}
    .pcol:last-child{{background:var(--p2)}}
    .prow{{display:flex;align-items:center;gap:6px;margin-bottom:5px;flex-wrap:wrap}}
    .plink{{font-size:12px;font-weight:600;color:var(--gl);text-decoration:none}}
    .plink:hover{{text-decoration:underline}}
    .punknown{{font-size:12px;font-weight:600;color:var(--muted)}}
    .lpchip{{font-size:10px;color:var(--muted);background:var(--surface2);
      border:1px solid var(--border);border-radius:3px;padding:1px 5px}}
    .rgchip{{font-size:10px;color:var(--muted);background:var(--surface2);
      border:1px solid var(--border);border-radius:3px;padding:1px 5px;text-transform:uppercase}}
    .clabel{{font-size:11px;color:var(--muted);margin-bottom:4px}}
    .urow{{display:flex;flex-wrap:wrap;gap:3px;margin-bottom:4px}}
    .uchip{{background:var(--surface2);border:1px solid var(--border);
      border-radius:3px;padding:2px 6px;font-size:10px;white-space:nowrap}}
    .arow{{display:flex;flex-wrap:wrap;gap:3px}}
    .achip{{background:rgba(155,89,182,.15);border:1px solid rgba(155,89,182,.3);
      border-radius:3px;padding:1px 5px;font-size:10px;color:#C09BE0}}
    .lvchip{{font-size:10px;font-weight:700;color:var(--gl);background:rgba(200,155,60,.12);
      border:1px solid rgba(200,155,60,.35);border-radius:3px;padding:1px 6px}}

    /* stats bar */
    .srow{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;
      background:var(--border);border-top:1px solid var(--border)}}
    .stat{{background:var(--surface2);padding:7px 4px;text-align:center}}
    .sv{{font-size:13px;font-weight:700}}
    .sl{{font-size:10px;color:var(--muted);margin-top:1px;text-transform:uppercase;letter-spacing:.3px}}
    .good{{color:#4ade80}}.ok{{color:var(--gl)}}.bad{{color:#f87171}}

    .empty{{grid-column:1/-1;text-align:center;color:var(--muted);padding:60px;font-size:15px}}
    footer{{text-align:center;padding:16px;font-size:11px;color:var(--muted);border-top:1px solid var(--border)}}

    /* intel tab */
    .isub{{background:transparent;border:none;color:var(--muted);padding:6px 16px;font-size:14px;
      font-weight:600;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;transition:.15s}}
    .isub.on{{color:var(--gl);border-bottom-color:var(--gold)}}
    .isubbar{{display:flex;gap:4px;padding:10px 28px 0;border-bottom:1px solid var(--border);background:var(--bg)}}
    .tprimary{{font-size:15px;font-weight:700;color:var(--text)}}
    .distbar{{display:flex;height:7px;border-radius:4px;overflow:hidden;gap:1px;margin-top:8px}}
    .db1{{background:var(--pl1)}}.db2{{background:var(--pl2)}}.db3{{background:var(--pl3)}}.db4{{background:var(--pl4)}}
    .distlabels{{display:flex;gap:12px;margin-top:5px}}
    .dlabel{{font-size:10px;color:var(--muted)}}
    .dlabel b{{color:var(--text);font-weight:600}}
  </style>
</head>
<body>
<header>
  <div>
    <h1>&#9876; TFT Double Up — {region_label}</h1>
    <div class="sub">{total_matches} matches · {total_players} players tracked · Built {built_at}</div>
  </div>
  <div class="pills">
    <div class="pill">Patches: <span>{patches_label}</span></div>
  </div>
</header>

<div class="controls">
  <button class="tab on" onclick="setTab('matches')">Recent Games</button>
  <button class="tab"    onclick="setTab('intel')">Trait Intel</button>
  <div class="sep"></div>

  <select class="ctrl" id="pfilter" onchange="setPatch(this.value)">
    {patch_options}
  </select>

  <select class="ctrl" id="rfilter" onchange="setRegion(this.value)">
    {region_options}
  </select>

  <span class="badge" id="badge"></span>
  <select class="ssort" id="ssort" onchange="setSort(this.value)">
    <option value="lp">Sort: Highest LP</option>
    <option value="place">Sort: Best Placement</option>
    <option value="recent">Sort: Most Recent</option>
  </select>
</div>

<div id="tab-m" class="grid"></div>
<div id="tab-i" style="display:none">
  <div class="isubbar">
    <button class="isub on" data-s="solo"  onclick="setISub('solo')">Solo Traits</button>
    <button class="isub"    data-s="pairs" onclick="setISub('pairs')">Trait Pairs</button>
  </div>
  <div id="intel-solo"  class="grid"></div>
  <div id="intel-detail" style="padding:0 28px 20px;display:none"></div>
  <div id="intel-pairs" class="grid" style="display:none"></div>
</div>

<footer>Riot Games API &middot; Double Up queue (1160) &middot; Not affiliated with Riot Games</footer>

<script>
const PATCHES_DATA = {patches_data_json};
const PATCHES      = {patches_json};
const PLAYERS      = {players_json};
const REGIONS      = {regions_json};

let tab='matches', sort='lp', rgn='ALL', curPatch=PATCHES[0]||'', isub='solo', intelSort='avg', selectedTrait=null;
const PL = ['','1st','2nd','3rd','4th'];
const TIER_ORDER = {{'S':0,'A':1,'B':2,'C':3}};

function pblock(p) {{
  const info = PLAYERS[p.puuid];
  const nm = info
    ? `<a class="plink" href="${{info.opgg}}" target="_blank">${{info.name}}#${{info.tag}}</a>
       <span class="lpchip">${{info.lp}} LP</span>
       <span class="rgchip">${{info.region}}</span>`
    : `<span class="punknown">Ally</span>`;
  const lv = p.level ? `<span class="lvchip">Lv ${{p.level}}</span>` : '';
  const au = p.augs.length
    ? `<div class="arow">${{p.augs.map(a=>`<span class="achip">${{a}}</span>`).join('')}}</div>` : '';
  return `<div class="pcol">
    <div class="prow">${{nm}}${{lv}}</div>
    <div class="clabel">${{p.comp}}</div>
    <div class="urow">${{p.units.map(u=>`<span class="uchip">${{u}}</span>`).join('')}}</div>
    ${{au}}</div>`;
}}

function renderMatches() {{
  const pd = PATCHES_DATA[curPatch] || {{}};
  let items = [...(pd.matches || [])];
  if (rgn !== 'ALL') items = items.filter(m => {{
    const pl = PLAYERS[m.p1.puuid] || PLAYERS[m.p2.puuid];
    return pl && pl.region === rgn;
  }});
  if (sort==='lp')    items.sort((a,b)=>b.max_lp-a.max_lp);
  if (sort==='place') items.sort((a,b)=>a.placement-b.placement);
  if (sort==='recent') items.sort((a,b)=>b.ts-a.ts);
  document.getElementById('badge').textContent = items.length+' games';
  document.getElementById('tab-m').innerHTML = items.length
    ? items.map(m=>`
      <div class="card">
        <div class="mhdr">
          <div class="pbadge p${{m.placement}}"><div class="pdot"></div>${{PL[m.placement]||m.placement+'th'}}</div>
          <div class="minfo">
            <span class="patchbadge">Patch ${{m.patch}}</span>
            <span class="gdate">${{m.game_date}}</span>
          </div>
        </div>
        <div class="pbody">${{pblock(m.p1)}}${{pblock(m.p2)}}</div>
      </div>`).join('')
    : '<div class="empty">No games for this filter.</div>';
}}

function renderComps() {{
  const ac=v=>v<=1.8?'good':v<=2.5?'ok':'bad';
  const wc=v=>v>=30?'good':v>=20?'ok':'bad';
  const tc=v=>v>=55?'good':v>=40?'ok':'bad';
  const pd = PATCHES_DATA[curPatch] || {{}};
  const all = pd.comps || [];
  let items = tier==='ALL' ? [...all] : all.filter(c=>c.tier===tier);
  items.sort((a,b)=>a.avg-b.avg);
  document.getElementById('badge').textContent = items.length+' pairs';
  document.getElementById('tab-c').innerHTML = items.length
    ? items.map(c=>`
      <div class="card">
        <div class="cbar" data-t="${{c.tier}}"></div>
        <div class="chdr">
          <div class="tbadge" data-t="${{c.tier}}">${{c.tier}}</div>
          <div class="pnames">
            <span class="cname">${{c.sig1}}</span>
            <span class="arrow">&#8646;</span>
            <span class="cname">${{c.sig2}}</span>
          </div>
        </div>
        <div class="pbody">
          <div class="pcol">
            <div class="urow">${{c.units1.map(u=>`<span class="uchip">${{u}}</span>`).join('')}}</div>
            ${{c.augs1.length?`<div class="arow">${{c.augs1.map(a=>`<span class="achip">${{a}}</span>`).join('')}}</div>`:''}}
          </div>
          <div class="pcol">
            <div class="urow">${{c.units2.map(u=>`<span class="uchip">${{u}}</span>`).join('')}}</div>
            ${{c.augs2.length?`<div class="arow">${{c.augs2.map(a=>`<span class="achip">${{a}}</span>`).join('')}}</div>`:''}}
          </div>
        </div>
        <div class="srow">
          <div class="stat"><div class="sv ${{ac(c.avg)}}">${{c.avg}}</div><div class="sl">Avg Place</div></div>
          <div class="stat"><div class="sv ${{wc(c.win_pct)}}">${{c.win_pct}}%</div><div class="sl">Win</div></div>
          <div class="stat"><div class="sv ${{tc(c.top2_pct)}}">${{c.top2_pct}}%</div><div class="sl">Top 2</div></div>
          <div class="stat"><div class="sv">${{c.samples}}</div><div class="sl">Games</div></div>
        </div>
      </div>`).join('')
    : '<div class="empty">No comp pairs for this tier.</div>';
}}

function renderIntel() {{
  const ac=v=>v<=1.8?'good':v<=2.5?'ok':'bad';
  const wc=v=>v>=30?'good':v>=20?'ok':'bad';
  const intel = (PATCHES_DATA[curPatch]||{{}}).intel || {{solo:[],pairs:[]}};

  if (isub === 'solo') {{
    let groups = [...intel.solo];
    if (intelSort==='games') groups.sort((a,b)=>b.total_games-a.total_games);
    else if (intelSort==='tier') groups.sort((a,b)=>TIER_ORDER[a.tier]-TIER_ORDER[b.tier]||a.best_avg-b.best_avg);
    // default 'avg': already sorted by best_avg

    document.getElementById('badge').textContent = groups.length + ' traits';
    document.getElementById('intel-solo').style.display = '';
    document.getElementById('intel-pairs').style.display = 'none';
    document.getElementById('intel-solo').innerHTML = groups.length
      ? groups.map(group => {{
          const sel = selectedTrait === group.name;
          const cols = group.variants.map(v => {{
            const [d1,d2,d3,d4] = v.dist;
            return `<div class="pcol" style="background:var(--surface2)">
              <div style="font-size:12px;font-weight:700;color:var(--gl);margin-bottom:7px">${{v.trait}}</div>
              <div class="distbar">
                <div class="db1" style="flex:${{d1||0.01}}" title="1st: ${{d1}}%"></div>
                <div class="db2" style="flex:${{d2||0.01}}" title="2nd: ${{d2}}%"></div>
                <div class="db3" style="flex:${{d3||0.01}}" title="3rd: ${{d3}}%"></div>
                <div class="db4" style="flex:${{d4||0.01}}" title="4th: ${{d4}}%"></div>
              </div>
              <div class="distlabels">
                <span class="dlabel">1st <b>${{d1}}%</b></span>
                <span class="dlabel">2nd <b>${{d2}}%</b></span>
                <span class="dlabel">3rd <b>${{d3}}%</b></span>
                <span class="dlabel">4th <b>${{d4}}%</b></span>
              </div>
              <div style="font-size:11px;color:var(--muted);margin-top:5px">
                Avg <b class="${{ac(v.avg)}}">${{v.avg}}</b> &middot;
                Win <b class="${{wc(v.win_pct)}}">${{v.win_pct}}%</b> &middot;
                <b>${{v.games}}</b> games
              </div>
              <div class="urow" style="margin-top:7px">${{v.top_units.map(u=>`<span class="uchip">${{u}}</span>`).join('')}}</div>
            </div>`;
          }}).join('');
          return `<div class="card" style="cursor:pointer;${{sel?'border-color:var(--gold);':''}}"
              onclick="selectTrait('${{group.name.replace(/'/g,"\\'")}}')">
            <div class="cbar" data-t="${{group.tier}}"></div>
            <div class="chdr">
              <div class="tbadge" data-t="${{group.tier}}">${{group.tier}}</div>
              <div class="tprimary" style="flex:1">${{group.name}}</div>
              <span class="badge">${{group.total_games}} games</span>
              <span style="font-size:11px;color:var(--muted);margin-left:4px">${{sel?'▲':'▼'}}</span>
            </div>
            <div class="pbody" style="grid-template-columns:repeat(${{group.variants.length}},1fr)">${{cols}}</div>
          </div>`;
        }}).join('')
      : '<div class="empty">No trait data for this patch.</div>';

    renderTraitDetail();

  }} else {{
    const pairs = [...intel.pairs];
    document.getElementById('badge').textContent = pairs.length + ' pairs';
    document.getElementById('intel-solo').style.display = 'none';
    document.getElementById('intel-pairs').style.display = '';
    document.getElementById('intel-detail').style.display = 'none';
    document.getElementById('intel-pairs').innerHTML = pairs.length
      ? pairs.map(t => {{
          const [d1,d2,d3,d4] = t.dist;
          return `<div class="card">
            <div class="chdr" style="flex-direction:column;align-items:flex-start;gap:6px">
              <div class="pnames">
                <span class="tprimary">${{t.trait1}}</span>
                <span class="arrow">&#8646;</span>
                <span class="tprimary">${{t.trait2}}</span>
              </div>
              <div class="distbar">
                <div class="db1" style="flex:${{d1||0.01}}"></div>
                <div class="db2" style="flex:${{d2||0.01}}"></div>
                <div class="db3" style="flex:${{d3||0.01}}"></div>
                <div class="db4" style="flex:${{d4||0.01}}"></div>
              </div>
              <div class="distlabels">
                <span class="dlabel">1st <b>${{d1}}%</b></span>
                <span class="dlabel">2nd <b>${{d2}}%</b></span>
                <span class="dlabel">3rd <b>${{d3}}%</b></span>
                <span class="dlabel">4th <b>${{d4}}%</b></span>
              </div>
            </div>
            <div class="srow">
              <div class="stat"><div class="sv">${{t.avg}}</div><div class="sl">Avg Place</div></div>
              <div class="stat"><div class="sv">${{t.win_pct}}%</div><div class="sl">Win</div></div>
              <div class="stat"><div class="sv">${{t.top2_pct}}%</div><div class="sl">Top 2</div></div>
              <div class="stat"><div class="sv">${{t.games}}</div><div class="sl">Games</div></div>
            </div>
          </div>`;
        }}).join('')
      : '<div class="empty">No trait pair data for this patch.</div>';
  }}
}}

function renderTraitDetail() {{
  const det = document.getElementById('intel-detail');
  if (!selectedTrait) {{ det.style.display='none'; return; }}
  const matches = (PATCHES_DATA[curPatch]||{{}}).matches || [];
  const base = selectedTrait;
  const filtered = matches.filter(m =>
    (m.p1.ptrait && m.p1.ptrait.slice(m.p1.ptrait.indexOf(' ')+1) === base) ||
    (m.p2.ptrait && m.p2.ptrait.slice(m.p2.ptrait.indexOf(' ')+1) === base)
  );
  filtered.sort((a,b)=>b.ts-a.ts);
  det.style.display = '';
  det.innerHTML = `
    <div style="font-size:13px;font-weight:600;color:var(--gl);padding:12px 0 10px;border-top:1px solid var(--border)">
      ${{filtered.length}} games featuring ${{base}}
    </div>
    <div class="grid" style="padding:0">${{
      filtered.map(m => matchCard(m)).join('') || '<div class="empty">No games found.</div>'
    }}</div>`;
}}

function matchCard(m) {{
  return `<div class="card">
    <div class="mhdr">
      <div class="pbadge p${{m.placement}}"><div class="pdot"></div>${{PL[m.placement]||m.placement+'th'}}</div>
      <div class="minfo">
        <span class="patchbadge">Patch ${{m.patch}}</span>
        <span class="gdate">${{m.game_date}}</span>
      </div>
    </div>
    <div class="pbody">${{pblock(m.p1)}}${{pblock(m.p2)}}</div>
  </div>`;
}}

function selectTrait(name) {{
  selectedTrait = selectedTrait === name ? null : name;
  renderIntel();
}}

function setISub(v) {{
  isub = v;
  selectedTrait = null;
  document.querySelectorAll('.isub').forEach(b => b.classList.toggle('on', b.dataset.s === v));
  renderIntel();
}}

function setTab(t) {{
  tab = t;
  document.querySelectorAll('.tab').forEach((b,i)=>b.classList.toggle('on',['matches','intel'][i]===t));
  document.getElementById('tab-m').style.display = t==='matches'?'':'none';
  document.getElementById('tab-i').style.display = t==='intel'?'':'none';
  const s = document.getElementById('ssort');
  if (t==='matches') {{
    s.innerHTML = `<option value="lp">Sort: Highest LP</option>
       <option value="place">Sort: Best Placement</option>
       <option value="recent">Sort: Most Recent</option>`;
    sort = 'lp';
  }} else {{
    s.innerHTML = `<option value="avg">Sort: Best Avg</option>
       <option value="tier">Sort: Tier (S→C)</option>
       <option value="games">Sort: Most Games</option>`;
    intelSort = 'avg';
  }}
  render();
}}
function setSort(v) {{ if(tab==='matches') sort=v; else intelSort=v; render(); }}
function setRegion(v){{rgn=v;render();}}
function setPatch(v){{curPatch=v;selectedTrait=null;render();}}
function render(){{tab==='matches'?renderMatches():renderIntel();}}
render();
</script>
</body>
</html>
"""


# ── Build ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Build TFT Double Up dashboard from local dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data",        default="tft_data.json")
    parser.add_argument("--output",      default="tft_dashboard.html")
    parser.add_argument("--min-samples", type=int, default=3)
    parser.add_argument("--region",      default="",
                        help="Filter to one region (e.g. euw1); default: all")
    parser.add_argument("--open",        action="store_true")
    args = parser.parse_args()

    data_file = Path(args.data)
    if not data_file.exists():
        print(f"ERROR: {data_file} not found. Run tft_fetch.py first.")
        raise SystemExit(1)

    print(f"Loading {data_file} …", end=" ", flush=True)
    data = json.loads(data_file.read_text(encoding="utf-8"))
    print(f"{len(data['matches'])} matches  |  {len(data['players'])} players")

    # Filter players by region if requested
    players: dict = data["players"]
    # Normalize op.gg URLs (remove www. if present in older cached data)
    for p in players.values():
        if p.get("opgg", "").startswith("https://www.op.gg/"):
            p["opgg"] = p["opgg"].replace("https://www.op.gg/", "https://op.gg/", 1)

    if args.region:
        players = {k: v for k, v in players.items() if v.get("region") == args.region}
        print(f"Region filter: {args.region.upper()} → {len(players)} players")

    # Filter matches to those involving selected players (if region filter active)
    player_puuids = set(players.keys())
    all_matches   = list(data["matches"].values())
    if args.region:
        all_matches = [
            m for m in all_matches
            if any(p.get("puuid") in player_puuids
                   for p in m.get("info", {}).get("participants", []))
        ]
        print(f"  → {len(all_matches)} matches involve {args.region.upper()} players")

    print("Analysing …")
    all_records       = process_matches(all_matches)
    all_match_records = build_match_records(all_matches, players)

    # Collect patches, sort newest first
    patches = sorted(
        {r["patch"] for r in all_records},
        key=patch_key, reverse=True,
    )
    if not patches:
        patches = ["?"]

    # Build per-patch data
    patches_data: dict = {}
    for p in patches:
        p_recs = [r for r in all_records       if r["patch"] == p]
        p_mats = [r for r in all_match_records if r["patch"] == p]
        intel  = aggregate_trait_intel(p_recs, min_games=args.min_samples)
        patches_data[p] = {"matches": p_mats, "intel": intel}

        n_groups = len(intel["solo"])
        print(f"  Patch {p}: {len(p_mats)} game records, {n_groups} trait groups")

    # Region dropdown options
    regions_in_data = sorted({v.get("region", "?") for v in data["players"].values()})
    region_options  = '<option value="ALL">All regions</option>' + "".join(
        f'<option value="{r}">{r.upper()}</option>' for r in regions_in_data
    )

    # Patch dropdown options (newest first = default selected)
    patch_options = "".join(
        f'<option value="{p}">Patch {p}</option>' for p in patches
    )

    region_label  = args.region.upper() if args.region else " + ".join(r.upper() for r in regions_in_data)
    patches_label = ", ".join(patches)

    html = HTML.format(
        region_label     = region_label,
        patches_label    = patches_label,
        total_players    = len(players),
        total_matches    = len(all_matches),
        built_at         = datetime.now().strftime("%Y-%m-%d %H:%M"),
        region_options   = region_options,
        patch_options    = patch_options,
        patches_data_json = json.dumps(patches_data,    ensure_ascii=False),
        patches_json     = json.dumps(patches,          ensure_ascii=False),
        players_json     = json.dumps(players,          ensure_ascii=False),
        regions_json     = json.dumps(regions_in_data,  ensure_ascii=False),
    )

    out = Path(args.output)
    out.write_text(html, encoding="utf-8")
    print(f"\nDashboard saved -> {out.resolve()}")

    if args.open:
        webbrowser.open(out.resolve().as_uri())
        print("Opening in browser …")
    else:
        print("Run with --open to launch in browser, or open the file manually.")


if __name__ == "__main__":
    main()
