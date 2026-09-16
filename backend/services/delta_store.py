"""Delta table persistence for workbench state (scan results, stars, seen spaces).

Used as the fallback when Lakebase (PostgreSQL) is unavailable — i.e., local dev
or any deployment without LAKEBASE_HOST set.  All tables live in the same
catalog/schema as the GSO optimizer tables (GSO_CATALOG / GSO_SCHEMA).

All warehouse calls are synchronous (SDK); callers wrap them with
``asyncio.get_event_loop().run_in_executor`` where needed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

def _catalog() -> str:
    from backend.services.request_config import get_request_config
    rc = get_request_config()
    return rc.gso_catalog or os.environ.get("GSO_CATALOG", "")

def _schema() -> str:
    return os.environ.get("GSO_SCHEMA", "genie_space_optimizer")

def _warehouse_id() -> str:
    from backend.services.request_config import get_request_config
    rc = get_request_config()
    return rc.warehouse_id or os.environ.get("GSO_WAREHOUSE_ID") or os.environ.get("SQL_WAREHOUSE_ID", "")

def _q(s: str) -> str:
    """Backtick-quote a UC identifier (handles hyphens)."""
    return f"`{s.replace('`', '``')}`"

def _sql_escape(s: str) -> str:
    """Escape single quotes for an inline SQL string literal.

    Defined as a plain function (not inlined into an f-string) because
    backslashes are not permitted inside f-string expression parts on
    Python 3.11.
    """
    return s.replace("'", "\\'")

def _tbl(name: str) -> str:
    """Return fully-qualified backtick-quoted table reference."""
    return f"{_q(_catalog())}.{_q(_schema())}.{_q(name)}"

def _is_configured() -> bool:
    return bool(_catalog() and _warehouse_id())


def _log_config_state(op: str) -> bool:
    """Log the resolved catalog/schema/warehouse for an op. Returns _is_configured()."""
    cat, sch, wh = _catalog(), _schema(), _warehouse_id()
    if cat and wh:
        logger.info(
            "delta_store.%s → catalog=%r schema=%r warehouse=%r", op, cat, sch, wh
        )
        return True
    logger.warning(
        "delta_store.%s SKIPPED — not configured (catalog=%r warehouse=%r). "
        "Set GSO_CATALOG + GSO_WAREHOUSE_ID/SQL_WAREHOUSE_ID env, or ensure the "
        "setup-wizard headers reach this call (see _run_delta_in_ctx).",
        op, cat, wh,
    )
    return False


# ── Shared warehouse accessor ─────────────────────────────────────────────────

def _ws():
    from backend.services.auth import get_service_principal_client
    return get_service_principal_client()

def _execute(sql: str) -> None:
    from genie_space_optimizer.common.warehouse import sql_warehouse_execute
    sql_warehouse_execute(_ws(), _warehouse_id(), sql)

def _query(sql: str) -> list[dict]:
    from genie_space_optimizer.common.warehouse import sql_warehouse_query
    df = sql_warehouse_query(_ws(), _warehouse_id(), sql)
    return df.to_dict(orient="records") if not df.empty else []


# ── DDL — create tables on first use ─────────────────────────────────────────

_ddl_done: set[str] = set()

def _ensure_table(name: str, ddl: str) -> None:
    if name in _ddl_done:
        return
    if not _is_configured():
        return
    try:
        _execute(ddl)
        _ddl_done.add(name)
        logger.info("delta_store: ensured Delta table %s exists at %s", name, _tbl(name))
    except Exception as exc:
        logger.warning("Could not create Delta table %s (%s): %s", name, _tbl(name), exc)


def _ensure_scan_results() -> None:
    _ensure_table("genie_scan_results", f"""
        CREATE TABLE IF NOT EXISTS {_tbl('genie_scan_results')} (
            space_id   STRING NOT NULL,
            score      INT,
            maturity   STRING,
            breakdown  STRING,
            findings   STRING,
            next_steps STRING,
            scanned_at TIMESTAMP
        )
        USING DELTA
        TBLPROPERTIES ('delta.enableChangeDataFeed' = 'false')
    """)


def _ensure_starred() -> None:
    _ensure_table("genie_starred_spaces", f"""
        CREATE TABLE IF NOT EXISTS {_tbl('genie_starred_spaces')} (
            space_id   STRING NOT NULL,
            starred_at TIMESTAMP
        )
        USING DELTA
    """)


def _ensure_seen() -> None:
    _ensure_table("genie_seen_spaces", f"""
        CREATE TABLE IF NOT EXISTS {_tbl('genie_seen_spaces')} (
            space_id STRING NOT NULL,
            seen_at  TIMESTAMP
        )
        USING DELTA
    """)


# ── Scan results ──────────────────────────────────────────────────────────────

def save_scan_result(space_id: str, scan_result: dict) -> None:
    if not _log_config_state("save_scan_result"):
        return
    try:
        _ensure_scan_results()
        scanned_at = scan_result.get("scanned_at") or datetime.utcnow().isoformat()
        breakdown = json.dumps({
            "optimization_accuracy": scan_result.get("optimization_accuracy"),
            "checks": scan_result.get("checks", []),
            "warnings": scan_result.get("warnings", []),
            "warning_next_steps": scan_result.get("warning_next_steps", []),
        })
        findings = json.dumps(scan_result.get("findings", []))
        next_steps = json.dumps(scan_result.get("next_steps", []))
        # Precompute all SQL-escaped values as locals. Backslashes are NOT allowed
        # inside f-string expression parts on Python 3.11, so the escaping cannot
        # be inlined into the f-string below (this was the import-time SyntaxError).
        sid = _sql_escape(space_id)
        maturity = _sql_escape(str(scan_result.get("maturity", "")))
        breakdown_e = _sql_escape(breakdown)
        findings_e = _sql_escape(findings)
        next_steps_e = _sql_escape(next_steps)
        score = int(scan_result.get("score", 0))
        ts = scanned_at[:19]
        _execute(f"""
            INSERT INTO {_tbl('genie_scan_results')}
              (space_id, score, maturity, breakdown, findings, next_steps, scanned_at)
            VALUES (
              '{sid}',
              {score},
              '{maturity}',
              '{breakdown_e}',
              '{findings_e}',
              '{next_steps_e}',
              CAST('{ts}' AS TIMESTAMP)
            )
        """)
        logger.info(
            "delta_store: wrote scan result for %s (score=%s) to %s",
            space_id, scan_result.get("score"), _tbl("genie_scan_results"),
        )
    except Exception as exc:
        logger.warning("delta_store.save_scan_result failed for %s: %s", space_id, exc)


def get_latest_score(space_id: str) -> dict | None:
    if not _is_configured():
        return None
    try:
        _ensure_scan_results()
        sid = space_id.replace("'", "\\'")
        rows = _query(f"""
            SELECT score, maturity, breakdown, findings, next_steps, scanned_at
            FROM {_tbl('genie_scan_results')}
            WHERE space_id = '{sid}'
            ORDER BY scanned_at DESC
            LIMIT 1
        """)
        if not rows:
            return None
        return _row_to_scan(rows[0])
    except Exception as exc:
        logger.warning("delta_store.get_latest_score failed for %s: %s", space_id, exc)
        return None


def get_space_history(space_id: str) -> list[dict]:
    if not _is_configured():
        return []
    try:
        _ensure_scan_results()
        sid = space_id.replace("'", "\\'")
        rows = _query(f"""
            SELECT score, maturity, breakdown, findings, next_steps, scanned_at
            FROM {_tbl('genie_scan_results')}
            WHERE space_id = '{sid}'
            ORDER BY scanned_at DESC
            LIMIT 30
        """)
        return [_row_to_scan(r) for r in rows]
    except Exception as exc:
        logger.warning("delta_store.get_space_history failed for %s: %s", space_id, exc)
        return []


def get_latest_scores_batch(space_ids: list[str]) -> dict[str, dict]:
    if not _is_configured() or not space_ids:
        return {}
    try:
        _ensure_scan_results()
        id_list = ", ".join(f"'{sid.replace(chr(39), chr(92)+chr(39))}'" for sid in space_ids)
        rows = _query(f"""
            SELECT space_id, score, maturity, breakdown, findings, next_steps, scanned_at
            FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY space_id ORDER BY scanned_at DESC) AS rn
                FROM {_tbl('genie_scan_results')}
                WHERE space_id IN ({id_list})
            ) t WHERE rn = 1
        """)
        return {r["space_id"]: _row_to_scan(r) for r in rows if r.get("space_id")}
    except Exception as exc:
        logger.warning("delta_store.get_latest_scores_batch failed: %s", exc)
        return {}


def _row_to_scan(row: dict) -> dict:
    extra: dict[str, Any] = {}
    try:
        extra = json.loads(row.get("breakdown") or "{}")
    except Exception:
        pass
    findings: list = []
    try:
        findings = json.loads(row.get("findings") or "[]")
    except Exception:
        pass
    next_steps: list = []
    try:
        next_steps = json.loads(row.get("next_steps") or "[]")
    except Exception:
        pass
    scanned_at = row.get("scanned_at")
    if scanned_at and not isinstance(scanned_at, str):
        scanned_at = str(scanned_at)
    return {
        "score": row.get("score", 0),
        "maturity": row.get("maturity", ""),
        "checks": extra.get("checks", []),
        "warnings": extra.get("warnings", []),
        "warning_next_steps": extra.get("warning_next_steps", []),
        "optimization_accuracy": extra.get("optimization_accuracy"),
        "findings": findings,
        "next_steps": next_steps,
        "scanned_at": scanned_at,
        "space_id": row.get("space_id"),
    }


def get_all_scanned() -> list[dict]:
    """Return the latest scan result for every known space."""
    if not _is_configured():
        return []
    try:
        _ensure_scan_results()
        rows = _query(f"""
            SELECT space_id, score, maturity, breakdown, findings, next_steps, scanned_at
            FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY space_id ORDER BY scanned_at DESC) AS rn
                FROM {_tbl('genie_scan_results')}
            ) t WHERE rn = 1
        """)
        return [_row_to_scan(r) for r in rows]
    except Exception as exc:
        logger.warning("delta_store.get_all_scanned failed: %s", exc)
        return []


# ── Stars ─────────────────────────────────────────────────────────────────────

def star_space(space_id: str, starred: bool) -> None:
    if not _is_configured():
        return
    try:
        _ensure_starred()
        sid = space_id.replace("'", "\\'")
        if starred:
            _execute(f"""
                INSERT INTO {_tbl('genie_starred_spaces')} (space_id, starred_at)
                SELECT '{sid}', current_timestamp()
                WHERE NOT EXISTS (
                    SELECT 1 FROM {_tbl('genie_starred_spaces')} WHERE space_id = '{sid}'
                )
            """)
        else:
            _execute(f"DELETE FROM {_tbl('genie_starred_spaces')} WHERE space_id = '{sid}'")
    except Exception as exc:
        logger.warning("delta_store.star_space failed for %s: %s", space_id, exc)


def get_starred_spaces() -> list[str]:
    if not _is_configured():
        return []
    try:
        _ensure_starred()
        rows = _query(f"SELECT space_id FROM {_tbl('genie_starred_spaces')}")
        return [r["space_id"] for r in rows if r.get("space_id")]
    except Exception as exc:
        logger.warning("delta_store.get_starred_spaces failed: %s", exc)
        return []


def is_space_starred(space_id: str) -> bool:
    if not _is_configured():
        return False
    try:
        _ensure_starred()
        sid = space_id.replace("'", "\\'")
        rows = _query(f"SELECT 1 FROM {_tbl('genie_starred_spaces')} WHERE space_id = '{sid}' LIMIT 1")
        return bool(rows)
    except Exception as exc:
        logger.warning("delta_store.is_space_starred failed for %s: %s", space_id, exc)
        return False


# ── Seen spaces ───────────────────────────────────────────────────────────────

def record_seen(space_id: str) -> None:
    if not _is_configured():
        return
    try:
        _ensure_seen()
        sid = space_id.replace("'", "\\'")
        _execute(f"""
            INSERT INTO {_tbl('genie_seen_spaces')} (space_id, seen_at)
            SELECT '{sid}', current_timestamp()
            WHERE NOT EXISTS (
                SELECT 1 FROM {_tbl('genie_seen_spaces')} WHERE space_id = '{sid}'
            )
        """)
    except Exception as exc:
        logger.warning("delta_store.record_seen failed for %s: %s", space_id, exc)


def is_space_seen(space_id: str) -> bool:
    if not _is_configured():
        return False
    try:
        _ensure_seen()
        sid = space_id.replace("'", "\\'")
        rows = _query(f"SELECT 1 FROM {_tbl('genie_seen_spaces')} WHERE space_id = '{sid}' LIMIT 1")
        return bool(rows)
    except Exception as exc:
        logger.warning("delta_store.is_space_seen failed for %s: %s", space_id, exc)
        return False


# ── Cross-run champion pointer ────────────────────────────────────────────────
# One row per space recording which run/iteration's config is CURRENTLY LIVE in
# the Genie agent (the cross-run champion), plus the best config ever produced
# across all runs. Kept in sync by the scanner, which fingerprints the live
# serialized_space and matches it to a stored iteration snapshot.

def _ensure_champion() -> None:
    _ensure_table("genie_space_champion", f"""
        CREATE TABLE IF NOT EXISTS {_tbl('genie_space_champion')} (
            space_id            STRING NOT NULL,
            run_id              STRING,
            iteration           INT,
            accuracy            DOUBLE,
            config_fingerprint  STRING,
            source              STRING,
            best_run_id         STRING,
            best_iteration      INT,
            best_accuracy       DOUBLE,
            updated_at          TIMESTAMP
        )
        USING DELTA
    """)


def set_space_champion(
    space_id: str,
    *,
    run_id: str | None,
    iteration: int | None,
    accuracy: float | None,
    config_fingerprint: str | None,
    source: str,
    best_run_id: str | None = None,
    best_iteration: int | None = None,
    best_accuracy: float | None = None,
) -> None:
    """Upsert the live-champion pointer for a space (one row per space)."""
    if not _is_configured():
        return
    try:
        _ensure_champion()
        sid = _sql_escape(space_id)
        rid = "NULL" if not run_id else "'" + _sql_escape(run_id) + "'"
        fpv = "NULL" if not config_fingerprint else "'" + _sql_escape(config_fingerprint) + "'"
        src = "'" + _sql_escape(source or "") + "'"
        itr = "NULL" if iteration is None else int(iteration)
        acc = "NULL" if accuracy is None else float(accuracy)
        b_rid = "NULL" if not best_run_id else "'" + _sql_escape(best_run_id) + "'"
        b_itr = "NULL" if best_iteration is None else int(best_iteration)
        b_acc = "NULL" if best_accuracy is None else float(best_accuracy)
        _execute(f"DELETE FROM {_tbl('genie_space_champion')} WHERE space_id = '{sid}'")
        _execute(f"""
            INSERT INTO {_tbl('genie_space_champion')}
              (space_id, run_id, iteration, accuracy, config_fingerprint, source,
               best_run_id, best_iteration, best_accuracy, updated_at)
            VALUES ('{sid}', {rid}, {itr}, {acc}, {fpv}, {src},
                    {b_rid}, {b_itr}, {b_acc}, current_timestamp())
        """)
    except Exception as exc:
        logger.warning("delta_store.set_space_champion failed for %s: %s", space_id, exc)


def get_space_champion(space_id: str) -> dict | None:
    if not _is_configured():
        return None
    try:
        _ensure_champion()
        sid = _sql_escape(space_id)
        rows = _query(f"""
            SELECT space_id, run_id, iteration, accuracy, config_fingerprint, source,
                   best_run_id, best_iteration, best_accuracy, updated_at
            FROM {_tbl('genie_space_champion')}
            WHERE space_id = '{sid}'
            ORDER BY updated_at DESC LIMIT 1
        """)
        return rows[0] if rows else None
    except Exception as exc:
        logger.warning("delta_store.get_space_champion failed for %s: %s", space_id, exc)
        return None


# ── Load tests ──────────────────────────────────────────────────────────────

def save_load_test(
    *,
    run_id: str,
    space_id: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    question: str | None,
    steps: list[dict],
    catalog: str,
    schema: str,
    warehouse: str,
) -> None:
    """Persist per-wave load-test results to Delta.

    Config is passed EXPLICITLY (catalog/schema/warehouse) rather than read from
    the request ContextVar, because this is called from an SSE streaming
    generator / executor thread where that context is not available. One row per
    concurrency wave, grouped by ``run_id``.
    """
    if not (catalog and warehouse):
        logger.warning(
            "delta_store.save_load_test SKIPPED — not configured (catalog=%r warehouse=%r)",
            catalog, warehouse,
        )
        return
    if not steps:
        return
    try:
        from backend.services.auth import get_service_principal_client
        from genie_space_optimizer.common.warehouse import sql_warehouse_execute

        ws = get_service_principal_client()
        tbl = f"{_q(catalog)}.{_q(schema)}.{_q('genie_load_tests')}"
        sql_warehouse_execute(ws, warehouse, f"""
            CREATE TABLE IF NOT EXISTS {tbl} (
                run_id           STRING,
                space_id         STRING,
                started_at       TIMESTAMP,
                finished_at      TIMESTAMP,
                duration_ms      BIGINT,
                step_index       INT,
                concurrent_users INT,
                total            INT,
                success          INT,
                error            INT,
                timeout          INT,
                rate_limited     INT,
                avg_ms           INT,
                p50_ms           INT,
                p95_ms           INT,
                p99_ms           INT,
                min_ms           INT,
                max_ms           INT,
                question         STRING,
                created_at       TIMESTAMP
            )
            USING DELTA
        """)
        # Pre-existing tables (created before rate_limited was tracked) need the
        # new column added. Best-effort — ignored if it already exists.
        try:
            sql_warehouse_execute(
                ws, warehouse,
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS rate_limited INT",
            )
        except Exception:
            pass

        rid = _sql_escape(run_id)
        sid = _sql_escape(space_id)
        q_literal = "NULL" if not question else "'" + _sql_escape(question) + "'"
        st = started_at[:19]
        ft = finished_at[:19]
        dur = int(duration_ms)

        cols = (
            "run_id, space_id, started_at, finished_at, duration_ms, step_index, "
            "concurrent_users, total, success, error, timeout, rate_limited, "
            "avg_ms, p50_ms, p95_ms, p99_ms, min_ms, max_ms, question, created_at"
        )
        rows = []
        for s in steps:
            rows.append(
                f"('{rid}','{sid}',"
                f"CAST('{st}' AS TIMESTAMP),CAST('{ft}' AS TIMESTAMP),{dur},"
                f"{int(s.get('step_index', 0))},{int(s.get('concurrent_users', 0))},"
                f"{int(s.get('total', 0))},{int(s.get('success', 0))},"
                f"{int(s.get('error', 0))},{int(s.get('timeout', 0))},"
                f"{int(s.get('rate_limited', 0))},"
                f"{int(s.get('avg_ms', 0))},{int(s.get('p50_ms', 0))},"
                f"{int(s.get('p95_ms', 0))},{int(s.get('p99_ms', 0))},"
                f"{int(s.get('min_ms', 0))},{int(s.get('max_ms', 0))},"
                f"{q_literal},current_timestamp())"
            )
        sql_warehouse_execute(ws, warehouse, f"INSERT INTO {tbl} ({cols}) VALUES " + ",".join(rows))
        logger.info(
            "delta_store: wrote %d load-test wave rows for run %s to %s",
            len(rows), run_id, tbl,
        )
    except Exception as exc:
        logger.warning("delta_store.save_load_test failed for %s: %s", space_id, exc)
