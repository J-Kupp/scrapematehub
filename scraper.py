from __future__ import annotations

import argparse
from pathlib import Path

from cleaner import clean_csv_file
from orchestrator import run_all_suppliers, run_supplier
from webapp import create_app
from webapp.config import load_webapp_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run supplier scrapers, exports, and YourBarMate sync jobs.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional path to a local env file containing YourBarMate tokens.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_supplier_parser = subparsers.add_parser("run-supplier", help="Scrape, validate, export, and sync one supplier.")
    run_supplier_parser.add_argument("supplier_slug", help="Supplier slug from suppliers.json.")
    run_supplier_parser.add_argument("--force-refresh", action="store_true", help="Ignore local HTTP snapshot cache.")
    run_supplier_parser.add_argument("--limit-products", type=int, default=None, help="Only sync the first N products.")
    run_supplier_parser.add_argument(
        "--skip-inactivate",
        action="store_true",
        default=None,
        help="Do not mark missing remote products as INACTIVE during sync.",
    )

    scrape_only_parser = subparsers.add_parser(
        "scrape-supplier",
        help="Scrape and transform one supplier, then export the prepared artifacts without syncing.",
    )
    scrape_only_parser.add_argument("supplier_slug", help="Supplier slug from suppliers.json.")
    scrape_only_parser.add_argument("--force-refresh", action="store_true", help="Ignore local HTTP snapshot cache.")

    run_all_parser = subparsers.add_parser("run-all-suppliers", help="Run all enabled supplier jobs sequentially.")
    run_all_parser.add_argument("--force-refresh", action="store_true", help="Ignore local HTTP snapshot cache.")

    dry_run_parser = subparsers.add_parser(
        "dry-run-supplier",
        help="Scrape, validate, export, and compute a YourBarMate sync diff without writes.",
    )
    dry_run_parser.add_argument("supplier_slug", help="Supplier slug from suppliers.json.")
    dry_run_parser.add_argument("--force-refresh", action="store_true", help="Ignore local HTTP snapshot cache.")
    dry_run_parser.add_argument("--limit-products", type=int, default=None, help="Only diff the first N products.")
    dry_run_parser.add_argument(
        "--skip-inactivate",
        action="store_true",
        default=None,
        help="Do not include INACTIVE transitions in the sync diff.",
    )

    sync_from_export_parser = subparsers.add_parser(
        "sync-from-export",
        help="Sync from the latest normalized JSONL export without re-scraping.",
    )
    sync_from_export_parser.add_argument("supplier_slug", help="Supplier slug from suppliers.json.")
    sync_from_export_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch remote state and compute the sync diff without writes.",
    )
    sync_from_export_parser.add_argument("--limit-products", type=int, default=None, help="Only sync the first N products.")
    sync_from_export_parser.add_argument(
        "--skip-inactivate",
        action="store_true",
        default=None,
        help="Do not mark missing remote products as INACTIVE during sync.",
    )

    clean_csv_parser = subparsers.add_parser(
        "clean-csv",
        help="Clean an existing supplier CSV and write a correction report.",
    )
    clean_csv_parser.add_argument("input_csv", type=Path, help="Input CSV path.")
    clean_csv_parser.add_argument("output_csv", type=Path, help="Cleaned CSV output path.")
    clean_csv_parser.add_argument("report_csv", type=Path, help="Correction report CSV output path.")

    control_panel_parser = subparsers.add_parser(
        "run-control-panel",
        help="Start the internal control panel web app.",
    )
    control_panel_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional path to control_panel.json.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run-supplier":
        run_supplier(
            args.supplier_slug,
            dry_run=False,
            force_refresh=args.force_refresh,
            sync_from_export=False,
            scrape_only=False,
            env_path=args.env_file,
            limit_products=args.limit_products,
            skip_inactivate=args.skip_inactivate,
        )
        return 0
    if args.command == "run-all-suppliers":
        run_all_suppliers(
            dry_run=False,
            force_refresh=args.force_refresh,
            env_path=args.env_file,
        )
        return 0
    if args.command == "scrape-supplier":
        run_supplier(
            args.supplier_slug,
            dry_run=False,
            force_refresh=args.force_refresh,
            sync_from_export=False,
            scrape_only=True,
            env_path=args.env_file,
            limit_products=None,
            skip_inactivate=False,
        )
        return 0
    if args.command == "dry-run-supplier":
        run_supplier(
            args.supplier_slug,
            dry_run=True,
            force_refresh=args.force_refresh,
            sync_from_export=False,
            scrape_only=False,
            env_path=args.env_file,
            limit_products=args.limit_products,
            skip_inactivate=args.skip_inactivate,
        )
        return 0
    if args.command == "sync-from-export":
        run_supplier(
            args.supplier_slug,
            dry_run=args.dry_run,
            force_refresh=False,
            sync_from_export=True,
            scrape_only=False,
            env_path=args.env_file,
            limit_products=args.limit_products,
            skip_inactivate=args.skip_inactivate,
        )
        return 0
    if args.command == "clean-csv":
        clean_csv_file(args.input_csv, args.output_csv, args.report_csv)
        return 0
    if args.command == "run-control-panel":
        import uvicorn

        web_config = load_webapp_config(args.config)
        app = create_app(args.config)
        uvicorn.run(
            app,
            host=web_config.host,
            port=web_config.port,
            proxy_headers=True,
            forwarded_allow_ips=web_config.forwarded_allow_ips,
        )
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
