#!/usr/bin/env python3
"""
tft_fetch.py — TFT Double Up Dataset Fetcher
─────────────────────────────────────────────
Fetches top Double Up players from multiple regions and their recent matches,
then saves everything to a single JSON file for tft_build.py to consume.

Usage:
    python tft_fetch.py --api-key RGAPI-xxx
    python tft_fetch.py --api-key RGAPI-xxx --regions euw1,na1,kr --players 25
    python tft_fetch.py --api-key RGAPI-xxx --update   # only fetch new matches

Options:
    --api-key    Riot API key  (developer.riotgames.com)
    --regions    Comma-separated list  (default: euw1,na1,kr)
    --players    Top players per region to analyse  (default: 25)
    --matches    Match history depth per player  (default: 20)
    --output     Output JSON file  (default: tft_data.json)
    --update     Load existing file and only add new matches / players
"""

import argparse
import json
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ── Routing ─────────────────────────────────────────────────────────────────────
REGION_TO_PLATFORM = {
    "na1":  "americas", "br1":  "americas", "la1": "americas", "la2": "americas",
    "euw1": "europe",   "eun1": "europe",   "tr1": "europe",   "ru":  "europe",
    "kr":   "asia",     "jp1":  "asia",
    "oc1":  "sea",      "ph2":  "sea",      "sg2": "sea",
    "th2":  "sea",      "tw2":  "sea",      "vn2": "sea",
}
OPGG_REGION = {
    "na1": "na",  "br1": "br",  "la1": "lan", "la2": "las",
    "euw1": "euw","eun1": "eune","tr1": "tr",  "ru":  "ru",
    "kr":  "kr",  "jp1": "jp",  "oc1": "oce",
}
DOUBLE_UP_QUEUE    = "RANKED_TFT_DOUBLE_UP"
DOUBLE_UP_QUEUE_ID = 1160

BROWSER_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Charset":  "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin":          "https://developer.riotgames.com",
}

# ── Rate limiter — one instance per API host ─────────────────────────────────────
# Riot enforces limits per routing value (euw1, europe, americas …).
# By keying limiters on the request hostname we automatically respect this.
_limiters: dict = {}

class _RateLimiter:
    """Enforces 20 req/1 s and 100 req/2 min (with safety margins)."""
    WINDOWS = [(1.0, 18), (120.0, 90)]

    def __init__(self):
        self._times: list[float] = []

    def wait(self):
        while True:
            now = time.time()
            self._times = [t for t in self._times if now - t < 121.0]
            wait_needed = 0.0
            for window, cap in self.WINDOWS:
                in_w = [t for t in self._times if now - t < window]
                if len(in_w) >= cap:
                    wait_needed = max(wait_needed, window - (now - min(in_w)) + 0.05)
            if wait_needed <= 0:
                break
            time.sleep(wait_needed)
        self._times.append(time.time())


def _limiter_for(url: str) -> _RateLimiter:
    host = urllib.parse.urlparse(url).netloc
    if host not in _limiters:
        _limiters[host] = _RateLimiter()
    return _limiters[host]


# ── HTTP helper ──────────────────────────────────────────────────────────────────
def api_get(url: str, api_key: str, retries: int = 3):
    for attempt in range(retries):
        _limiter_for(url).wait()
        headers = {**BROWSER_HEADERS, "X-Riot-Token": api_key}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("\nERROR: Invalid or expired API key.", file=sys.stderr)
                sys.exit(1)
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "10"))
                print(f"\n  Rate limited — waiting {wait}s …", flush=True)
                time.sleep(wait + 1)
            elif e.code == 404:
                return None
            elif e.code in (500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"\n  HTTP {e.code} — {url}", flush=True)
                return None
        except Exception as ex:
            if attempt == retries - 1:
                print(f"\n  Request error: {ex}", flush=True)
                return None
            time.sleep(1)
    return None


# ── Patch detection ──────────────────────────────────────────────────────────────
def get_current_patch() -> str:
    """Returns e.g. '14.24' from Data Dragon (no API key needed)."""
    try:
        req = urllib.request.Request(
            "https://ddragon.leagueoflegends.com/api/versions.json",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            parts = json.loads(r.read().decode())[0].split(".")
            return f"{parts[0]}.{parts[1]}"
    except Exception as e:
        print(f"  (patch detection failed: {e})")
        return ""


# ── Patch helper ─────────────────────────────────────────────────────────────────
def _match_patch(match: dict) -> str:
    """Extract '15.5' from game_version field."""
    gv = match.get("info", {}).get("game_version", "")
    m  = re.search(r"(\d+\.\d+)\.", gv)
    return m.group(1) if m else ""


# ── Per-region fetch ─────────────────────────────────────────────────────────────
def fetch_region(
    region: str,
    api_key: str,
    n_players: int,
    n_matches: int,
    existing_match_ids: set,
    existing_players: dict,
) -> tuple[dict, list[str]]:
    """
    Fetch top players + new match IDs for one region.
    Returns (players_dict, list_of_new_match_ids).
    """
    platform  = REGION_TO_PLATFORM.get(region, "europe")
    opgg_rgn  = OPGG_REGION.get(region, region.rstrip("1"))

    # ── Leaderboard ──
    print(f"\n  [{region.upper()}] Leaderboard …", end=" ", flush=True)
    entries = []
    source  = "Double Up"
    for tier in ("challenger", "grandmaster", "master"):
        url  = f"https://{region}.api.riotgames.com/tft/league/v1/{tier}?queue={DOUBLE_UP_QUEUE}"
        data = api_get(url, api_key)
        if data and data.get("entries"):
            entries = data["entries"]
            break
    if not entries:
        source = "Standard TFT (fallback)"
        print("no Double Up ladder, using standard TFT … ", end="", flush=True)
        for tier in ("challenger", "grandmaster"):
            url  = f"https://{region}.api.riotgames.com/tft/league/v1/{tier}?queue=RANKED_TFT"
            data = api_get(url, api_key)
            if data and data.get("entries"):
                entries = data["entries"]
                break
    if not entries:
        print("SKIP (no data)")
        return {}, []

    entries.sort(key=lambda e: e.get("leaguePoints", 0), reverse=True)
    entries = entries[:n_players]
    print(f"{len(entries)} players ({source})")

    # ── PUUIDs ──
    lp_map: dict[str, int] = {}
    puuids: list[str] = []
    for e in entries:
        if e.get("puuid"):
            puuids.append(e["puuid"])
            lp_map[e["puuid"]] = e.get("leaguePoints", 0)
        elif e.get("summonerId"):
            url = f"https://{region}.api.riotgames.com/tft/summoner/v1/summoners/{e['summonerId']}"
            s   = api_get(url, api_key)
            if s and s.get("puuid"):
                puuids.append(s["puuid"])
                lp_map[s["puuid"]] = e.get("leaguePoints", 0)

    # ── Account names ──
    print(f"  [{region.upper()}] Fetching {len(puuids)} account names …")
    players: dict = {}
    for i, puuid in enumerate(puuids, 1):
        if puuid in existing_players:
            # Update LP but keep the rest
            p = dict(existing_players[puuid])
            p["lp"] = lp_map.get(puuid, p.get("lp", 0))
            players[puuid] = p
            print(f"    [{i:>2}/{len(puuids)}] {p['name']}#{p['tag']} ({p['lp']} LP)  (cached)")
            continue
        url     = f"https://{platform}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}"
        account = api_get(url, api_key)
        lp      = lp_map.get(puuid, 0)
        if account:
            gn  = account.get("gameName", "?")
            tl  = account.get("tagLine", "")
            opgg = (f"https://op.gg/tft/summoners/{opgg_rgn}/"
                    f"{urllib.parse.quote(gn)}-{urllib.parse.quote(tl)}")
        else:
            gn, tl, opgg = "Unknown", "", ""
        players[puuid] = {"name": gn, "tag": tl, "lp": lp, "region": region, "opgg": opgg}
        print(f"    [{i:>2}/{len(puuids)}] {gn}#{tl} ({lp} LP)")

    # ── Match IDs ──
    print(f"  [{region.upper()}] Collecting match IDs ({n_matches}/player) …")
    all_ids: set[str] = set()
    for i, puuid in enumerate(puuids, 1):
        url = (f"https://{platform}.api.riotgames.com/tft/match/v1/matches/"
               f"by-puuid/{puuid}/ids?queue={DOUBLE_UP_QUEUE_ID}&count={n_matches}")
        ids = api_get(url, api_key) or []
        all_ids.update(ids)
        print(f"    [{i:>2}/{len(puuids)}] +{len(ids):>3} → {len(all_ids)} unique", flush=True)

    new_ids = [mid for mid in all_ids if mid not in existing_match_ids]
    cached  = len(all_ids) - len(new_ids)
    print(f"  [{region.upper()}] {len(new_ids)} new matches  (+{cached} already cached)")
    return players, new_ids


def fetch_matches(match_ids: list[str], region: str, api_key: str, patch: str = "") -> dict:
    platform = REGION_TO_PLATFORM.get(region, "europe")
    matches: dict = {}
    skipped = 0
    total = len(match_ids)
    if not total:
        return matches
    label = f" (patch {patch} only)" if patch else ""
    print(f"  Fetching {total} matches{label} …")
    for i, mid in enumerate(match_ids, 1):
        url = f"https://{platform}.api.riotgames.com/tft/match/v1/matches/{mid}"
        m   = api_get(url, api_key)
        if m:
            if patch and _match_patch(m) != patch:
                skipped += 1
            else:
                matches[mid] = m
        if i % 25 == 0 or i == total:
            print(f"    [{i:>4}/{total}] kept {len(matches)}  skipped {skipped}", flush=True)
    if skipped:
        print(f"  ({skipped} matches discarded — wrong patch)")
    return matches


# ── Main ─────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Fetch TFT Double Up dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--api-key",  required=True)
    parser.add_argument("--regions",  default="euw1,na1,kr",
                        help="Comma-separated regions (default: euw1,na1,kr)")
    parser.add_argument("--players",  type=int, default=25,
                        help="Top players per region (default: 25)")
    parser.add_argument("--matches",  type=int, default=20,
                        help="Matches per player (default: 20)")
    parser.add_argument("--output",   default="tft_data.json")
    parser.add_argument("--update",   action="store_true",
                        help="Add only new data to an existing file")
    args = parser.parse_args()

    regions = [r.strip().lower() for r in args.regions.split(",")]
    output  = Path(args.output)

    # Load existing data (for --update or to skip already-cached matches)
    existing: dict = {"meta": {}, "players": {}, "matches": {}}
    if args.update and output.exists():
        print(f"Loading existing data from {output} …")
        existing = json.loads(output.read_text(encoding="utf-8"))
        print(f"  {len(existing['matches'])} matches  |  {len(existing['players'])} players already cached")
    elif output.exists() and not args.update:
        # Always skip already-fetched matches even without --update flag
        existing = json.loads(output.read_text(encoding="utf-8"))
        print(f"Found existing {output} — will skip {len(existing['matches'])} cached matches.")

    print(f"\nTFT Double Up Dataset Fetcher")
    print(f"Regions  : {', '.join(r.upper() for r in regions)}")
    print(f"Players  : {args.players}/region  |  Matches: {args.matches}/player")

    print("\nDetecting current patch …", end=" ", flush=True)
    patch = get_current_patch()
    print(f"patch {patch}" if patch else "could not detect")

    all_players  = dict(existing["players"])
    all_matches  = dict(existing["matches"])
    known_ids    = set(all_matches.keys())

    for region in regions:
        players, new_ids = fetch_region(
            region, args.api_key, args.players, args.matches,
            known_ids, all_players,
        )
        all_players.update(players)

        new_matches = fetch_matches(new_ids, region, args.api_key, patch)
        all_matches.update(new_matches)
        known_ids.update(new_matches.keys())
        print(f"  [{region.upper()}] Region complete.  Running total: {len(all_matches)} matches")

    # Save ── use compact JSON to keep file size reasonable
    data = {
        "meta": {
            "fetched_at":         datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "patch":              patch,
            "regions":            regions,
            "players_per_region": args.players,
            "total_players":      len(all_players),
            "total_matches":      len(all_matches),
        },
        "players": all_players,
        "matches": all_matches,
    }
    output.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    size_mb = output.stat().st_size / 1_048_576
    print(f"\nDataset saved → {output.resolve()}")
    print(f"  {len(all_players)} players  |  {len(all_matches)} matches  |  {size_mb:.1f} MB")
    print(f"\nNext: python tft_build.py  (or  python tft_build.py --open)")


if __name__ == "__main__":
    main()
