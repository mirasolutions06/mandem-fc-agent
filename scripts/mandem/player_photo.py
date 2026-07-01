#!/usr/bin/env python3
# scripts/mandem/player_photo.py
# Look up a player's official headshot via API-Football, download it.
# When the agent has a named scorer in the caption, this returns the right person —
# no DDG search relevance roulette.
#
# API-Football endpoints used:
#   /players/squads?team=TEAM_ID        (1 call) — current squad with photos
#   /players/profiles?search=NAME       (1 call) — name search (no team needed)

from __future__ import annotations

import urllib.parse
import urllib.request
from pathlib import Path

from . import _env
from .footy_api import FootballClient, BASE_URL

DEFAULT_OUT_DIR = _env.data_dir() / "images"


def _strip_punct(s: str) -> str:
    """Loose name match — strips diacritics-by-ascii + punctuation."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()


def find_by_team_squad(player_name: str, team_id: int, out_dir: Path | None = None) -> dict:
    """When you know the team_id: hit /players/squads?team=ID, match name, download photo.
    API-Football abbreviates first names (e.g. Bukayo Saka → 'B. Saka'), so we match on
    surname + first-letter-of-first-name."""
    out_dir = out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    client = FootballClient()
    import httpx
    target = _strip_punct(player_name)
    target_parts = target.split()
    target_last = target_parts[-1] if target_parts else ""
    target_first_initial = target_parts[0][0] if target_parts and target_parts[0] else ""
    with httpx.Client(timeout=20.0) as cli:
        r = cli.get(
            f"{BASE_URL}/players/squads",
            headers=client._headers,
            params={"team": team_id},
        )
        r.raise_for_status()
        data = r.json().get("response") or []
    if not data:
        return {"ok": False, "error": f"no squad for team {team_id}"}
    players = data[0].get("players", [])
    # Multi-stage matching: prefer exact surname + first-initial, else surname alone
    surname_matches: list[dict] = []
    for p in players:
        full = _strip_punct(p.get("name", ""))
        if not full:
            continue
        full_parts = full.split()
        if not full_parts:
            continue
        full_last = full_parts[-1]
        if full_last != target_last:
            continue
        # Surname matches — score by first-initial agreement
        full_first_initial = full_parts[0][0] if full_parts[0] else ""
        score = 1 + (1 if full_first_initial == target_first_initial else 0)
        surname_matches.append({"player": p, "score": score})
    if not surname_matches:
        return {"ok": False, "error": f"surname '{target_last}' not in squad of team {team_id} ({len(players)} players)"}
    surname_matches.sort(key=lambda m: -m["score"])
    best = surname_matches[0]["player"]
    photo_url = best.get("photo")
    if not photo_url:
        return {"ok": False, "error": "no photo URL on player record"}
    # Download
    out = out_dir / f"player_{best['id']}.png"
    req = urllib.request.Request(photo_url, headers={"User-Agent": "mandem-fc-agent/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        out.write_bytes(r.read())
    return {
        "ok": True,
        "path": str(out),
        "player_id": best["id"],
        "player_name": best["name"],
        "photo_url": photo_url,
        "image_source": "api_football_squad",
    }


def find(player_name: str, team_id: int, out_dir: Path | None = None) -> dict:
    """Find a player's official headshot. team_id is REQUIRED — without it we can't
    reliably match (the /players/profiles?search=NAME endpoint returns hundreds of
    fuzzy matches, none guaranteed correct).

    For Mandem's flow: team_id always comes from the FT event's scorer record (which
    has team.id) or the fixture context (home_id / away_id from get_today_covered_fixtures).
    """
    if not team_id:
        return {
            "ok": False,
            "error": "team_id required — without it API-Football's name search is unreliable. "
                     "Pass the scorer's team.id from the ft_events row.",
        }
    return find_by_team_squad(player_name, team_id, out_dir)


# ---------- CLI ----------

def _cli(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="player_photo")
    p.add_argument("--name", required=True)
    p.add_argument("--team-id", type=int, default=0)
    args = p.parse_args(argv)
    import json
    print(json.dumps(find(args.name, args.team_id), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
