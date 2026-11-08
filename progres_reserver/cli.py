from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any, Iterable, Sequence

from .api import ApiError, ProgresMealApi, existing_reservation_keys, interpret_reservation_response
from .config import Config, build_config, load_dotenv_files
from .constants import MEAL_LABELS
from .logging_utils import LOG, setup_logging


def build_dates(cfg: Config) -> list[str]:
    if cfg.dates:
        dates = list(cfg.dates)
        if cfg.offsets:
            LOG.warning("Both dates and offsets are set; using the explicit dates.")
    else:
        offsets = cfg.offsets or [1, 2, 3]
        today = dt.date.today()
        dates = [(today + dt.timedelta(days=offset)).isoformat() for offset in offsets]
        if any(offset < 1 or offset > 3 for offset in offsets):
            LOG.warning(
                "The official app only offers tomorrow through +3 days; "
                "the server may reject other offsets."
            )
    today = dt.date.today().isoformat()
    for date in dates:
        if date < today:
            LOG.warning("Date %s is in the past.", date)
    return dates


def print_depots(depots: Iterable[dict[str, Any]]) -> None:
    rows = list(depots)
    print("\nAvailable restaurants/depots:")
    print(f"  {'ID':<8} {'nameFR':<42} nameAR")
    print(f"  {'-' * 8} {'-' * 42} {'-' * 30}")
    for depot in rows:
        print(
            f"  {str(depot.get('id', '')):<8} "
            f"{str(depot.get('nameFR') or ''):<42} "
            f"{depot.get('nameAR') or ''}"
        )
    print()


def choose_depot(depots: list[dict[str, Any]], wanted: str) -> dict[str, Any]:
    if not depots:
        raise ApiError("the meal API returned an empty depot list")
    if not wanted:
        print_depots(depots)
        raise ApiError("no restaurant selected; pass --depot ID or name (or set RESTO_DEPOT)")

    want = str(wanted).strip()
    by_id = [depot for depot in depots if str(depot.get("id", "")).strip() == want]
    if len(by_id) == 1:
        return by_id[0]

    def names(depot: dict[str, Any]) -> list[str]:
        return [
            (depot.get(key) or "").casefold() if isinstance(depot.get(key), str) else str(depot.get(key) or "").casefold()
            for key in ("nameFR", "nameAR")
            if depot.get(key)
        ]

    normalized = want.casefold()
    exact = [depot for depot in depots if normalized in names(depot)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        print_depots(exact)
        raise ApiError(f"restaurant name {wanted!r} is ambiguous; use its id")

    partial = [
        depot
        for depot in depots
        if any(normalized and normalized in name for name in names(depot))
    ]
    if len(partial) == 1:
        return partial[0]
    print_depots(partial if partial else depots)
    if len(partial) > 1:
        raise ApiError(f"restaurant name {wanted!r} matches several depots; use an id")
    raise ApiError(f"restaurant/depot {wanted!r} was not found")


def print_reservations(reservations: list[dict[str, Any]]) -> None:
    if not reservations:
        print("\nNo current meal reservations were returned.\n")
        return
    print(f"\nCurrent reservations ({len(reservations)}):")
    print(f"  {'Date':<12} {'Meal':<22} {'Depot':<38} cancellable")
    print(f"  {'-' * 12} {'-' * 22} {'-' * 38} {'-' * 11}")
    for reservation in sorted(
        reservations,
        key=lambda item: (
            item.get("date_reserve") or item.get("date") or "",
            str(item.get("mealtype_fr") or item.get("mealLabel") or ""),
        ),
    ):
        meal = reservation.get("mealtype_fr") or reservation.get("mealLabel") or "?"
        depot = reservation.get("depot_fr") or reservation.get("restoName") or "?"
        cancellable = "yes" if reservation.get("candelete") else "no"
        print(
            f"  {str(reservation.get('date_reserve') or reservation.get('date') or '?'):<12} "
            f"{str(meal):<22} {str(depot):<38} {cancellable}"
        )
    print()


def reserve_meals(
    api: ProgresMealApi,
    cfg: Config,
    depot: dict[str, Any],
    dates: list[str],
    reservations: list[dict[str, Any]],
) -> int:
    depot_id = depot.get("id")
    depot_name = depot.get("nameFR") or depot.get("nameAR") or str(depot_id)
    existing = existing_reservation_keys(reservations)
    plan: dict[int, list[str]] = {}

    for meal_type in cfg.meals or [1, 2, 3]:
        pending = [date for date in dates if cfg.force or (date, meal_type) not in existing]
        skipped = [date for date in dates if date not in pending]
        if skipped:
            LOG.info("%s: already reserved on %s; skipping.", MEAL_LABELS[meal_type], ", ".join(skipped))
        if pending:
            plan[meal_type] = pending

    if not plan:
        LOG.info("Nothing to reserve: all requested meals already exist.")
        return 0

    LOG.info("Restaurant/depot: %s (id=%s)", depot_name, depot_id)
    for meal_type, pending in plan.items():
        LOG.info("Plan: %s (menu_type=%s) on %s", MEAL_LABELS[meal_type], meal_type, ", ".join(pending))

    if cfg.dry_run:
        LOG.info("Dry run: no reservation request will be sent.")
        for meal_type, pending in plan.items():
            details = [
                json.dumps(
                    {"date_reserve": date, "menu_type": meal_type, "idDepot": depot_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for date in pending
            ]
            LOG.debug("details for %s: %s", MEAL_LABELS[meal_type], details)
        return 0

    failures: list[tuple[int, str]] = []
    total = len(plan)
    for index, (meal_type, pending) in enumerate(plan.items(), start=1):
        LOG.info("[%s/%s] Reserving %s at %s for %s...", index, total, MEAL_LABELS[meal_type], depot_name, ", ".join(pending))
        try:
            ok, message = api.reserve(meal_type, pending, depot_id)
        except ApiError as exc:
            LOG.error("Reservation failed for %s: %s", MEAL_LABELS[meal_type], exc)
            failures.append((meal_type, str(exc)))
            continue
        if ok:
            LOG.info("Reservation accepted for %s: %s", MEAL_LABELS[meal_type], message)
        else:
            LOG.error("Reservation rejected for %s: %s", MEAL_LABELS[meal_type], message)
            failures.append((meal_type, message))

    if failures:
        LOG.error("Finished with %s failed meal type(s): %s", len(failures), ", ".join(MEAL_LABELS[meal_type] for meal_type, _ in failures))
        return 2
    LOG.info("All requested reservations were accepted.")
    return 0


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Automatically reserve PROGRES+ WebEtu meals (breakfast, lunch, "
            "dinner) for the next days at a chosen restaurant."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python progres_meal_reserver.py --list-depots\n"
            "  python progres_meal_reserver.py --list-reservations\n"
            "  python progres_meal_reserver.py --depot 12 --dry-run\n"
            "  python progres_meal_reserver.py --depot \"Resto Central\"\n"
            "  python progres_meal_reserver.py --dates 2026-11-09,2026-11-10\n"
            "\n"
            "Credentials are read from WEBETU_USERNAME and WEBETU_PASSWORD, "
            "a .env file, or a JSON config file."
        ),
    )
    parser.add_argument("--config", help="path to a JSON config file")
    parser.add_argument("--username", help="WebEtu username (prefer WEBETU_USERNAME)")
    parser.add_argument("--password", help="WebEtu password (prefer WEBETU_PASSWORD)")
    parser.add_argument("--depot", help="restaurant/depot id or name (or set RESTO_DEPOT)")
    parser.add_argument("--residence", help="optional residence id override (or WEBETU_RESIDENCE)")
    parser.add_argument("--wilaya", help="optional wilaya id override (or WEBETU_WILAYA)")
    parser.add_argument("--dates", help="comma-separated dates, YYYY-MM-DD (instead of --offsets)")
    parser.add_argument("--offsets", help="comma-separated day offsets, default 1,2,3")
    parser.add_argument("--meals", help="comma-separated meal types, default 1,2,3 (1=breakfast, 2=lunch, 3=dinner)")
    parser.add_argument("--dry-run", action="store_true", default=None, help="show the plan without sending reservation requests")
    parser.add_argument("--force", action="store_true", default=None, help="reserve even when the same date/meal is already listed")
    parser.add_argument("--no-sign", action="store_true", default=None, help="do not send the HMAC signature headers (the live API rejects unsigned requests)")
    parser.add_argument("--list-depots", action="store_true", help="log in, print the restaurant list, then exit")
    parser.add_argument("--list-reservations", action="store_true", help="log in, print current reservations, then exit")
    parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=None, help="retries for GET/network errors")
    parser.add_argument("--log-file", default=None, help="log file path")
    parser.add_argument("--no-log-file", action="store_true", help="disable the default log file next to the script")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="debug logging")
    parser.add_argument("--version", action="version", version="%(prog)s 1.0")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv_files()
    args = parse_args(argv)
    configure_console()
    try:
        cfg = build_config(args)
    except Exception as exc:  # pragma: no cover
        from .config import ConfigError
        if isinstance(exc, ConfigError):
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        raise

    setup_logging(cfg)
    if not cfg.username or not cfg.password:
        LOG.error("Missing credentials. Set WEBETU_USERNAME and WEBETU_PASSWORD, create a .env file, or use --config.")
        return 2

    try:
        api = ProgresMealApi(cfg)
        api.authenticate()

        if cfg.list_depots:
            print_depots(api.depots)
            return 0

        reservations = api.get_reservations(required=cfg.list_reservations)
        if cfg.list_reservations:
            print_reservations(reservations)
            return 0

        depot = choose_depot(api.depots, cfg.depot)
        if reservations:
            LOG.info("Existing reservations returned by the API: %s", len(reservations))
        dates = build_dates(cfg)
        LOG.info("Target dates: %s", ", ".join(dates))
        return reserve_meals(api, cfg, depot, dates, reservations)
    except ApiError as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
