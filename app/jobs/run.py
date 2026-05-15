import argparse

from scheduler.jobs import (
    analytics_job,
    normalize_job,
    run_module_collectors_job,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Data Core jobs manually.")
    parser.add_argument("--module", required=True, choices=["ecommerce", "real_estate", "sports_odds", "crypto", "trading"])
    parser.add_argument("--source", required=False)
    parser.add_argument("--skip-normalize", action="store_true")
    parser.add_argument("--skip-analytics", action="store_true")
    args = parser.parse_args()

    run_module_collectors_job(args.module, source=args.source)
    if not args.skip_normalize:
        normalize_job(args.module)
    if not args.skip_analytics:
        analytics_job(args.module)
        if args.module == "crypto":
            analytics_job("trading")


if __name__ == "__main__":
    main()
