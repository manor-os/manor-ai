#!/usr/bin/env python3
"""Write the read-only approval migration plan for explicit review.

Run with the deployment's DATABASE_URL_SYNC and keep the report private: it
contains configuration evidence. This command never applies a migration.
"""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, text

from packages.core.migrations.unified_approval import preflight


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--check', action='store_true', help='Exit 2 on blockers; print only aggregate counts')
    parser.add_argument('--repair', action='store_true', help='Inspect the forward repair of already-converted data')
    parser.add_argument('--resolutions', help='Reviewed per-owner unified replacement policies')
    parser.add_argument('--require-no-blockers', action='store_true', help='Stop before migration when the frozen plan has blockers')
    args = parser.parse_args()
    if not args.output and not args.check:
        parser.error('provide --output or --check')
    if args.repair and args.resolutions:
        parser.error('--repair restores only proven exact denials; resolutions apply to the original upgrade')
    resolutions = json.loads(Path(args.resolutions).read_text()) if args.resolutions else None
    engine = create_engine(os.environ['DATABASE_URL_SYNC'])
    try:
        with engine.connect() as connection:
            if connection.dialect.name == 'postgresql':
                connection.execute(text('SET TRANSACTION READ ONLY'))
            if args.repair:
                from packages.core.migrations.approval_conversion_repair import preflight as repair_preflight
                plan = repair_preflight(connection)
            else:
                plan = preflight(connection, resolutions=resolutions)
            connection.rollback()
    finally:
        engine.dispose()
    if args.output:
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(plan, output, indent=2, ensure_ascii=False, default=str)
            output.write('\n')
    summary = {'source_digest': plan['source_digest'], 'blockers': len(plan['blockers']),
        'operations': len(plan['operations'])}
    if args.output:
        summary['output'] = str(Path(args.output))
    if args.repair:
        summary.update(review_required=len(plan['review_required']), skipped=len(plan['skipped']),
                       already_applied=plan['already_applied'])
    print(json.dumps(summary))
    if args.check and (plan['blockers'] or plan.get('review_required') or plan.get('skipped')):
        raise SystemExit(2)
    if args.require_no_blockers and plan['blockers']:
        raise SystemExit('Approval migration remains blocked; inspect the private plan in the migration pod')


if __name__ == '__main__':
    main()
