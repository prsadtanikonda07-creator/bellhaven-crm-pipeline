"""
Entry point: run the pipeline then start the review app.

Usage
-----
  python run.py              # run pipeline + start app on :5000
  python run.py --pipeline   # run pipeline only (no app)
  python run.py --app        # start app only (skip pipeline)
  python run.py --port 8080  # custom port
"""

import argparse
import logging
import sys

# Windows consoles default to cp1252, which can't print the emoji/dashes below.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

def main():
    parser = argparse.ArgumentParser(description="Bellhaven CRM Pipeline + Review App")
    parser.add_argument("--pipeline", action="store_true", help="Run pipeline only")
    parser.add_argument("--app",      action="store_true", help="Start review app only")
    parser.add_argument("--port",     type=int, default=5000, help="Flask port (default 5000)")
    args = parser.parse_args()

    run_pipeline = not args.app
    run_app      = not args.pipeline

    if run_pipeline:
        from pipeline import run as run_pipeline_fn
        summary = run_pipeline_fn()
        pending = (
            summary.get("needs_fix", 0)
            + summary.get("new_account", 0)
            + summary.get("orphaned", 0)
        )
        print(f"\n✅  Pipeline complete — {pending} proposals need review.\n")

    if run_app:
        from app import app
        print(f"🌐  Starting review app on http://localhost:{args.port}")
        app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
