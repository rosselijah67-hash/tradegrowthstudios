"""Build a static local HTML review dashboard."""

from __future__ import annotations

from html import escape

from . import db
from .cli_utils import build_parser, finish_command, setup_command
from .config import project_path


COMMAND = "build_review_dashboard"


def _render_dashboard(prospects: list[dict]) -> str:
    rows = []
    for prospect in prospects:
        rows.append(
            "<tr>"
            f"<td>{escape(str(prospect['id']))}</td>"
            f"<td>{escape(prospect.get('business_name') or '')}</td>"
            f"<td>{escape(prospect.get('market') or '')}</td>"
            f"<td>{escape(prospect.get('niche') or '')}</td>"
            f"<td>{escape(prospect.get('website_url') or '')}</td>"
            f"<td>{escape(prospect.get('status') or '')}</td>"
            "</tr>"
        )

    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lead Review Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2rem; color: #1f2933; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #d9e2ec; padding: 0.55rem; text-align: left; }
    th { background: #f0f4f8; }
  </style>
</head>
<body>
  <h1>Lead Review Dashboard</h1>
  <table>
    <thead>
      <tr>
        <th>ID</th>
        <th>Business</th>
        <th>Market</th>
        <th>Niche</th>
        <th>Website</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      {{ROWS}}
    </tbody>
  </table>
</body>
</html>
"""
    return template.replace("{{ROWS}}", "\n      ".join(rows))


def main() -> int:
    parser = build_parser("Build a static local HTML review dashboard.")
    args = parser.parse_args()
    context = setup_command(args, COMMAND)

    connection = db.init_db(args.db_path)
    prospects = db.fetch_prospects(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
    )
    dashboard_html = _render_dashboard(prospects)
    dashboard_path = project_path("artifacts/dashboard/review.html")
    content_hash = db.stable_hash(dashboard_html)

    if args.dry_run:
        context.logger.info(
            "dashboard_would_build",
            extra={
                "event": "dashboard_would_build",
                "path": str(dashboard_path),
                "prospects": len(prospects),
            },
        )
    else:
        dashboard_path.parent.mkdir(parents=True, exist_ok=True)
        if not dashboard_path.exists() or dashboard_path.read_text(encoding="utf-8") != dashboard_html:
            dashboard_path.write_text(dashboard_html, encoding="utf-8")
        db.upsert_artifact(
            connection,
            artifact_key="global:dashboard:review",
            artifact_type="review_dashboard",
            path=str(dashboard_path),
            content_hash=content_hash,
            status="ready",
            metadata={"prospect_count": len(prospects)},
        )
        connection.commit()

    connection.close()
    finish_command(context, prospects=len(prospects))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
