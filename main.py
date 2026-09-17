"""Parlay picker command line.

    python main.py dashboard                    launch the local website
    python main.py sync    [--sport nfl] [--days 3]
    python main.py board   [--sport nfl] [--date YYYY-MM-DD]
    python main.py matchup <game_id> [--fetch-prices]
    python main.py parlay  [--sport nfl] [--legs 3] [--rank probability|value]
    python main.py grade   [--date YYYY-MM-DD]
    python main.py credits
    python main.py backtest [--season YYYY]

Content generation lives in archive/ and is no longer wired up.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from src.data import sports as sport_registry
from src.db import database as db

# The Windows console defaults to cp1252, which cannot encode the sport icons.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _day(value: str | None) -> dt.date:
    return dt.date.fromisoformat(value) if value else dt.date.today()


def cmd_dashboard(_args) -> None:
    import threading
    import webbrowser
    from src.web.app import create_app
    from waitress import serve

    url = "http://127.0.0.1:8502"
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Opening Edgeboard locally at {url}")
    serve(create_app(), host="127.0.0.1", port=8502, threads=6)


def cmd_sync(args) -> None:
    from src.picker.engine import Picker
    for code in _sports(args):
        picker = Picker(code)
        start = _day(args.date)
        games = sum(picker.sync_games(start + dt.timedelta(days=offset))
                    for offset in range(args.days))
        odds = picker.sync_odds(force=args.force, day=start)
        print(f"{code.upper():4s} {games:3d} games over {args.days} day(s); "
              f"odds via {odds['source']} ({odds['games']} games)")


def cmd_board(args) -> None:
    from src.picker import board
    day = _day(args.date)
    for code in _sports(args):
        cards = board.day_board(day, code, min_edge=args.min_edge)
        config = sport_registry.get(code)
        print(f"\n{config.emoji} {config.name} - {day.isoformat()} "
              f"({len(cards)} games)")
        if not cards:
            print("  No games. Run `sync` first if you expected some.")
            continue
        for card in cards:
            print(f"\n  {card.matchup:12s} {card.kickoff_local()}")
            if card.h2h:
                print(f"    H2H: {card.h2h.summary_line()}")
            if not card.bettable:
                print(f"    Already under way ({card.status}) - no pregame prices.")
            elif not card.candidates:
                print("    No line cleared the edge threshold.")
            for candidate in sorted(card.candidates, key=lambda c: c.edge, reverse=True):
                print(f"    {candidate.headline:16s} {candidate.odds:+5d} "
                      f"{candidate.bookmaker:12s} edge {candidate.edge:+6.1%}  "
                      f"conf {candidate.confidence['score']}/10  {candidate.units}u")
                if candidate.h2h_note:
                    print(f"        {candidate.h2h_note}")


def cmd_matchup(args) -> None:
    from src.picker import board
    card = _find_card(args.game_id, args.date)
    if card is None:
        raise SystemExit(f"No game {args.game_id} on {args.date or 'today'}. "
                         "Run `board` to list game ids.")
    print(f"{card.matchup} - {card.kickoff_local()}")
    if card.h2h:
        print(f"H2H: {card.h2h.summary_line()}")
        for meeting in card.h2h.meetings:
            print(f"  {meeting.date}  {meeting.away} {meeting.away_score:g} @ "
                  f"{meeting.home} {meeting.home_score:g}")

    def progress(_fraction, message):
        print(f"  ... {message}")

    deep = board.deep_matchup(card, fetch_prices=args.fetch_prices, progress=progress)
    print(f"\nPlayer props ({'market priced' if deep.priced else 'model only'}):")
    if deep.props_note:
        print(f"  note: {deep.props_note}")
    for prop in deep.props[:20]:
        price = f"{prop.odds:+d} {prop.bookmaker}" if prop.priced else "no price"
        edge = f"edge {prop.edge:+.1%}" if prop.edge is not None else ""
        print(f"  {prop.headline:42s} {price:18s} p {prop.probability:5.1%} {edge}")
        print(f"      {prop.evidence()}")


def cmd_parlay(args) -> None:
    from src.picker import board
    from src.picker.parlay_builder import optimize
    day = _day(args.date)
    cards = []
    for code in _sports(args):
        cards += board.day_board(day, code)
    pool = board.parlay_pool(cards, min_probability=args.min_leg_prob)
    if len(pool) < 2:
        raise SystemExit("Not enough qualifying legs. Try a lower --min-leg-prob.")

    parlays = optimize(pool, min_legs=args.legs, max_legs=args.legs,
                       rank_by=args.rank, limit=args.limit)
    if not parlays:
        raise SystemExit("No parlay cleared the filters.")
    label = "most likely" if args.rank == "probability" else "best value"
    print(f"Top {len(parlays)} {args.legs}-leg parlays ({label}):\n")
    for i, parlay in enumerate(parlays, 1):
        print(f"{i}. {parlay.american_display()}  "
              f"hit {parlay.combined_model_prob:.1%}  "
              f"EV {parlay.expected_value:+.3f}u per unit")
        for leg in parlay.legs:
            print(f"     {leg.display()}  {int(leg.odds_american):+d}"
                  + ("" if leg.priced else "  (assumed price)"))
            if leg.reason:
                print(f"       {leg.reason}")
        for note in parlay.notes:
            print(f"     ! {note}")
        print()


def cmd_grade(args) -> None:
    from src.picker.backtest import record_summary
    from src.picker.grader import grade_all
    graded = grade_all(game_date=args.date)
    wins = sum(1 for g in graded if g["result"] == "W")
    losses = sum(1 for g in graded if g["result"] == "L")
    pushes = sum(1 for g in graded if g["result"] == "P")
    print(f"Graded {len(graded)} picks. {wins} W, {losses} L, {pushes} P")
    print(f"Season record: {record_summary()['overall']}")


def cmd_credits(_args) -> None:
    from src.data.odds_client import OddsClient
    info = OddsClient.credits()
    if not info:
        print("No credit info cached yet. Run `sync` once with ODDS_API_KEY set.")
        return
    print(f"Odds API credits remaining: {info['remaining']} "
          f"(used {info['used']}), checked {info['checked_at']}")
    print("Prop pulls cost one credit per market per game.")


def cmd_backtest(args) -> None:
    from src.picker.backtest import simulate_season
    report = simulate_season(season=args.season)
    if "error" in report:
        raise SystemExit(f"Could not backtest: {report['error']}")
    print(f"Season {args.season} replay ({report['n']} bets):")
    print(f"  record  {report['wins']}-{report['losses']} ({report['hit_rate']:.1%})")
    print(f"  units   {report['units']:+0.2f}  ROI {report['roi']:+.1%}")
    for score, data in report["by_conf"].items():
        print(f"     conf {score}/10 : n={data['n']}  win {data['hit_rate']:.1%}")


# ------------------------------------------------------------------ helpers
def _sports(args) -> list[str]:
    if getattr(args, "sport", None):
        return [args.sport]
    return sport_registry.enabled_codes()


def _find_card(game_id: int, date: str | None):
    from src.picker import board
    day = _day(date)
    for code in sport_registry.enabled_codes():
        for card in board.day_board(day, code):
            if card.game_id == game_id:
                return card
    return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="main.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    codes = sorted(sport_registry.REGISTRY)

    sub.add_parser("dashboard", help="launch the local website")

    p = sub.add_parser("sync", help="sync games and game-line odds")
    p.add_argument("--sport", choices=codes)
    p.add_argument("--date", help="first date (YYYY-MM-DD; default: today)")
    p.add_argument("--days", type=int, default=3, choices=range(1, 15))
    p.add_argument("--force", action="store_true", help="bypass the odds cache")

    p = sub.add_parser("board", help="show the modelled games")
    p.add_argument("--sport", choices=codes)
    p.add_argument("--date")
    p.add_argument("--min-edge", type=float, default=None,
                   help="override the edge threshold (e.g. 0.02)")

    p = sub.add_parser("matchup", help="deep head-to-head for one game")
    p.add_argument("game_id", type=int)
    p.add_argument("--date")
    p.add_argument("--fetch-prices", action="store_true",
                   help="spend Odds API credits to price the props")

    p = sub.add_parser("parlay", help="search for the best parlays")
    p.add_argument("--sport", choices=codes)
    p.add_argument("--date")
    p.add_argument("--legs", type=int, default=3, choices=range(2, 7))
    p.add_argument("--rank", choices=["probability", "value"], default="probability")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--min-leg-prob", type=float, default=0.55)

    p = sub.add_parser("grade", help="grade picks whose games are final")
    p.add_argument("--date", help="only grade picks on this game date")

    sub.add_parser("credits", help="show Odds API credits remaining")

    p = sub.add_parser("backtest", help="replay the picker on a past season")
    p.add_argument("--season", type=int, default=dt.date.today().year - 1)

    args = parser.parse_args()
    db.init_db()
    {
        "dashboard": cmd_dashboard, "sync": cmd_sync, "board": cmd_board,
        "matchup": cmd_matchup, "parlay": cmd_parlay, "grade": cmd_grade,
        "credits": cmd_credits, "backtest": cmd_backtest,
    }[args.command](args)


if __name__ == "__main__":
    main()
