"""
Airtable prospect situation stats via REST API; optional KPIS update (prompt, auto, or off).

Run:  python main.py
Configure: .env (see .env.example).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import load_dotenv

# --- logging -----------------------------------------------------------------
logger = logging.getLogger("airtable")


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


# --- config ------------------------------------------------------------------
_BASE_ID_RE = re.compile(r"app[a-zA-Z0-9]{14}")


def _parse_base_id_from_url(url: str) -> str | None:
    if not url:
        return None
    m = _BASE_ID_RE.search(url.strip())
    return m.group(0) if m else None


@dataclass(frozen=True)
class Settings:
    base_id: str
    prospects_table: str
    kpis_table: str
    token: str
    kpi_record_id: str | None
    kpi_update_mode: str  # "always" | "never" | "prompt"
    prospect_situation_field: str


def load_settings() -> Settings:
    load_dotenv()

    base_url = os.getenv("AIRTABLE_BASE_URL", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    if not base_id:
        base_id = _parse_base_id_from_url(base_url) or ""

    prospects = (
        os.getenv("PROSPECTS_TABLE_ID", "").strip()
        or os.getenv("PROSPECTS_TABLE_NAME", "").strip()
    )
    kpis = (
        os.getenv("KPIS_TABLE_ID", "").strip()
        or os.getenv("KPIS_TABLE_NAME", "").strip()
    )

    token = (
        os.getenv("AIRTABLE_PERSONAL_ACCESS_TOKEN", "").strip()
        or os.getenv("AIRTABLE_API_KEY", "").strip()
    )

    kpi_record_id = os.getenv("KPI_RECORD_ID", "").strip() or None

    # UPDATE_KPIS: true = write after stats (no question). false = stats only.
    # prompt = ask in an interactive terminal (default locally only).
    is_ci = os.getenv("GITHUB_ACTIONS") == "true"
    _uk = os.getenv("UPDATE_KPIS")
    if _uk is None or not str(_uk).strip():
        # In GitHub Actions the workflow should pass UPDATE_KPIS; if missing, do not assume prompt.
        kpi_update_mode = "never" if is_ci else "prompt"
    else:
        raw = str(_uk).strip().lower()
        if raw in ("1", "true", "yes", "always"):
            kpi_update_mode = "always"
        elif raw in ("0", "false", "no", "never"):
            kpi_update_mode = "never"
        elif raw in ("prompt", "ask"):
            kpi_update_mode = "prompt"
        else:
            kpi_update_mode = "prompt"

    if kpi_update_mode == "prompt" and (is_ci or not sys.stdin.isatty()):
        kpi_update_mode = "never"
        if is_ci:
            logger.warning(
                "UPDATE_KPIS=prompt is invalid in GitHub Actions (no keyboard). "
                "Set repository Secret or Variable UPDATE_KPIS=true (literal true) to write KPIS. "
                "Using never for this run."
            )
        else:
            logger.warning(
                "UPDATE_KPIS=prompt needs an interactive terminal; using never for this run."
            )

    prospect_situation_field = (
        os.getenv("PROSPECT_SITUATION_FIELD", "").strip() or "Prospect Situation"
    )

    missing = []
    if not base_id:
        missing.append("AIRTABLE_BASE_ID or AIRTABLE_BASE_URL (with app… id)")
    if not prospects:
        missing.append("PROSPECTS_TABLE_ID or PROSPECTS_TABLE_NAME")
    if kpi_update_mode in ("always", "prompt") and not kpis:
        missing.append(
            "KPIS_TABLE_ID or KPIS_TABLE_NAME (required when UPDATE_KPIS is true or prompt)"
        )
    if not token:
        missing.append("AIRTABLE_PERSONAL_ACCESS_TOKEN or AIRTABLE_API_KEY")

    if missing:
        raise ValueError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    return Settings(
        base_id=base_id,
        prospects_table=prospects,
        kpis_table=kpis,
        token=token,
        kpi_record_id=kpi_record_id,
        kpi_update_mode=kpi_update_mode,
        prospect_situation_field=prospect_situation_field,
    )


# --- Airtable API ------------------------------------------------------------
API_ROOT = "https://api.airtable.com/v0"


class AirtableAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AirtableClient:
    def __init__(self, base_id: str, token: str, timeout: int = 60):
        self.base_id = base_id
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )
        self._timeout = timeout

    def list_records(
        self,
        table: str,
        fields: list[str] | None = None,
        page_size: int = 100,
        max_records: int | None = None,
    ) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        offset: str | None = None

        while True:
            q: list[tuple[str, str]] = [("pageSize", str(page_size))]
            if offset:
                q.append(("offset", offset))
            for name in fields or []:
                q.append(("fields[]", name))

            url = f"{API_ROOT}/{self.base_id}/{requests.utils.quote(table, safe='')}"
            try:
                r = self._session.get(url, params=q, timeout=self._timeout)
            except requests.Timeout as e:
                raise AirtableAPIError("Airtable request timed out") from e
            except requests.RequestException as e:
                raise AirtableAPIError(f"Airtable network error: {e}") from e

            if r.status_code >= 400:
                raise AirtableAPIError(
                    f"Airtable API error listing records in {table!r}",
                    status_code=r.status_code,
                    body=r.text[:2000],
                )

            data = r.json()
            records = data.get("records", [])
            all_rows.extend(records)
            if max_records is not None and len(all_rows) >= max_records:
                return all_rows[:max_records]

            offset = data.get("offset")
            if not offset:
                break
            time.sleep(0.2)

        return all_rows

    def patch_record(self, table: str, record_id: str, fields: dict[str, Any]) -> None:
        url = f"{API_ROOT}/{self.base_id}/{requests.utils.quote(table, safe='')}/{record_id}"
        try:
            r = self._session.patch(
                url,
                json={"fields": fields},
                timeout=self._timeout,
            )
        except requests.Timeout as e:
            raise AirtableAPIError("Airtable patch timed out") from e
        except requests.RequestException as e:
            raise AirtableAPIError(f"Airtable network error on patch: {e}") from e

        if r.status_code >= 400:
            raise AirtableAPIError(
                f"Airtable API error updating record {record_id} in {table!r}",
                status_code=r.status_code,
                body=r.text[:2000],
            )

    def create_record(self, table: str, fields: dict[str, Any]) -> str:
        """Create a row; returns the new record id (rec…)."""
        url = f"{API_ROOT}/{self.base_id}/{requests.utils.quote(table, safe='')}"
        try:
            r = self._session.post(
                url,
                json={"fields": fields},
                timeout=self._timeout,
            )
        except requests.Timeout as e:
            raise AirtableAPIError("Airtable create timed out") from e
        except requests.RequestException as e:
            raise AirtableAPIError(f"Airtable network error on create: {e}") from e

        if r.status_code >= 400:
            raise AirtableAPIError(
                f"Airtable API error creating record in {table!r}",
                status_code=r.status_code,
                body=r.text[:2000],
            )

        data = r.json()
        rid = data.get("id")
        if not rid:
            raise AirtableAPIError("Airtable create response missing id", body=r.text[:2000])
        return str(rid)


# --- stats -------------------------------------------------------------------
DEFAULT_SITUATION_FIELD = "Prospect Situation"
# Situation values we count (Prospect Situation) and mirror to KPIS number fields.
ORDERED_LABELS = (
    "Lost",
    "Engaged",
    "Last chance",
    "Admitted",
    "Serious",
    "Potential",
    "Completed",
)

_ALIAS_TO_CANONICAL: dict[str, str] = {
    "lost": "Lost",
    "engaged": "Engaged",
    "last chance": "Last chance",
    "last_chance": "Last chance",
    "lastchance": "Last chance",
    "admitted": "Admitted",
    "serious": "Serious",
    "potential": "Potential",
    "completed": "Completed",
}

_WS_RE = re.compile(r"\s+")


def _collapse_ws(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def _normalize_one_label(raw: str) -> str | None:
    s = _collapse_ws(raw)
    if not s:
        return None
    key = s.casefold()
    if key in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[key]
    for canonical in ORDERED_LABELS:
        if s.casefold() == canonical.casefold():
            return canonical
    return s


def _extract_string_tokens(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, bool):
        return [str(raw)]
    if isinstance(raw, (int, float)):
        return [str(raw)]
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, dict):
        for k in ("name", "value", "text", "label"):
            if k in raw and raw[k] is not None:
                return _extract_string_tokens(raw[k])
        return []
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            out.extend(_extract_string_tokens(item))
        return out
    return [str(raw)] if str(raw).strip() else []


def _canonical_labels_for_record(raw: Any) -> set[str]:
    out: set[str] = set()
    for t in _extract_string_tokens(raw):
        n = _normalize_one_label(t)
        if n in ORDERED_LABELS:
            out.add(n)
    return out


def log_situation_field_diagnostics(
    records: list[dict[str, Any]], field_name: str, *, max_samples: int = 15
) -> None:
    n = len(records)
    with_key = 0
    non_null = 0
    type_counter: Counter[str] = Counter()
    value_samples: list[str] = []

    for rec in records:
        fields = rec.get("fields") or {}
        if field_name not in fields:
            continue
        with_key += 1
        val = fields[field_name]
        if val is None or val == "" or val == []:
            continue
        non_null += 1
        type_counter[type(val).__name__] += 1
        if len(value_samples) < max_samples:
            sample = repr(val)
            if len(sample) > 120:
                sample = sample[:117] + "..."
            value_samples.append(sample)

    logger.info(
        "Situation diagnostics — field %r: %d records, %d with this field key in API, "
        "%d with a non-empty value. Value types: %s",
        field_name,
        n,
        with_key,
        non_null,
        dict(type_counter) if type_counter else {},
    )
    if value_samples:
        logger.info("Example raw values from API: %s", " | ".join(value_samples))


def count_prospect_situations(
    records: list[dict[str, Any]], field_name: str = DEFAULT_SITUATION_FIELD
) -> dict[str, int]:
    counts = {label: 0 for label in ORDERED_LABELS}
    missing_key = 0
    empty_cell = 0
    unknown: dict[str, int] = {}

    for rec in records:
        fields = rec.get("fields") or {}
        if field_name not in fields:
            missing_key += 1
            continue

        raw = fields[field_name]
        if raw is None or raw == "" or raw == []:
            empty_cell += 1
            continue

        labels = _canonical_labels_for_record(raw)
        if labels:
            for lab in labels:
                counts[lab] += 1
            continue

        tokens = _extract_string_tokens(raw)
        if not tokens:
            empty_cell += 1
            continue

        for t in tokens:
            n = _normalize_one_label(t)
            if n not in ORDERED_LABELS and n:
                unknown[n] = unknown.get(n, 0) + 1

    if unknown:
        logger.warning(
            "Values in %r outside the tracked situation labels %s: %s",
            field_name,
            ", ".join(repr(x) for x in ORDERED_LABELS),
            ", ".join(f"{k!r} ({v})" for k, v in sorted(unknown.items())[:25]),
        )

    if missing_key:
        logger.info(
            "%d record(s) omit %r in the API payload (often empty cells).",
            missing_key,
            field_name,
        )
    if empty_cell:
        logger.info("%d record(s) had an empty %r value.", empty_cell, field_name)

    return counts


def kpi_payload_from_counts(counts: dict[str, int]) -> dict[str, int]:
    """KPIS column names must exist on the KPI row (numbers)."""
    return {
        "Serious": int(counts["Serious"]),
        "Admitted": int(counts["Admitted"]),
        "Lost": int(counts["Lost"]),
        "Last_chance": int(counts["Last chance"]),
        "Engaged": int(counts["Engaged"]),
        "Potential": int(counts["Potential"]),
        "Completed": int(counts["Completed"]),
    }


# Airtable field names written on KPIS (for logs / errors)
KPIS_FIELD_NAMES_LOG = (
    "Serious, Admitted, Lost, Last_chance, Engaged, Potential, Completed"
)


# --- orchestration -----------------------------------------------------------
def _parse_airtable_error_type(body: str | None) -> str | None:
    if not body:
        return None
    try:
        data = json.loads(body)
        return (data.get("error") or {}).get("type")
    except json.JSONDecodeError:
        return None


def _raise_api_context(
    err: AirtableAPIError,
    *,
    prospects: bool = False,
    kpis: bool = False,
    situation_field: str = "Prospect Situation",
) -> None:
    t = _parse_airtable_error_type(err.body)
    if err.status_code == 404 or t == "TABLE_NOT_FOUND":
        table = "Prospects" if prospects else "KPIS" if kpis else "requested"
        raise RuntimeError(
            f"{table} table was not found (HTTP {err.status_code}). "
            f"Check PROSPECTS_TABLE_* / KPIS_TABLE_* in your .env (name or tbl… id)."
        ) from err
    if prospects and t == "UNKNOWN_FIELD_NAME":
        raise RuntimeError(
            f"Field {situation_field!r} was not found in the Prospects table, "
            f"or the token cannot read it. Set PROSPECT_SITUATION_FIELD in .env to the exact "
            f"column name, and verify API scopes."
        ) from err
    if kpis and t == "UNKNOWN_FIELD_NAME":
        raise RuntimeError(
            "One or more KPI fields were rejected by Airtable (UNKNOWN_FIELD_NAME). "
            "Verify the KPIS table has these fields (exact names): "
            f"{KPIS_FIELD_NAMES_LOG}."
        ) from err
    raise err


def _fetch_prospects(
    client: AirtableClient, table: str, situation_field: str
) -> list[dict]:
    try:
        return client.list_records(table, fields=[situation_field])
    except AirtableAPIError as e:
        _raise_api_context(e, prospects=True, situation_field=situation_field)


def _create_kpis_row(client: AirtableClient, settings: Settings, fields: dict[str, Any]) -> str:
    try:
        return client.create_record(settings.kpis_table, fields)
    except AirtableAPIError as e:
        _raise_api_context(e, kpis=True)


def _patch_kpis(client: AirtableClient, settings: Settings, record_id: str, fields: dict) -> None:
    try:
        client.patch_record(settings.kpis_table, record_id, fields)
    except AirtableAPIError as e:
        _raise_api_context(e, kpis=True)


def _apply_kpi_update(
    client: AirtableClient, settings: Settings, counts: dict[str, int]
) -> None:
    """Write KPI number fields — PATCH if KPI_RECORD_ID set, else POST new row."""
    payload = kpi_payload_from_counts(counts)
    if settings.kpi_record_id:
        rid = settings.kpi_record_id
        logger.info("Updating KPIS row %s — fields: %s", rid, KPIS_FIELD_NAMES_LOG)
        _patch_kpis(client, settings, rid, payload)
        logger.info("KPIS row updated.")
    else:
        logger.info("Creating new KPIS row — fields: %s", KPIS_FIELD_NAMES_LOG)
        new_id = _create_kpis_row(client, settings, payload)
        logger.info(
            "Created KPIS record %s. To update this same row next time, add to .env: KPI_RECORD_ID=%s",
            new_id,
            new_id,
        )


def _want_kpi_write_interactive() -> bool:
    print("")
    print("Write these counts to the KPIS table?")
    print("  (Creates a new row unless KPI_RECORD_ID is set in .env, then updates that row.)")
    print("  Fields: " + KPIS_FIELD_NAMES_LOG)
    try:
        ans = input("  Type y or yes to update, anything else to skip [N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def main() -> int:
    _configure_logging()

    try:
        settings = load_settings()
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        return 2

    logger.info("=== Airtable prospect stats (API only) ===")
    logger.info("Base id: %s", settings.base_id)
    if settings.kpi_update_mode == "always":
        logger.info("UPDATE_KPIS=true — KPI row will be updated after counts (no prompt).")
    elif settings.kpi_update_mode == "never":
        logger.info("KPIS writes disabled (effective mode: never).")
    else:
        logger.info(
            "UPDATE_KPIS=prompt (default) — after counts you will be asked whether to update KPIS."
        )

    client = AirtableClient(settings.base_id, settings.token)

    try:
        logger.info(
            "Fetching %r from table %r …",
            settings.prospect_situation_field,
            settings.prospects_table,
        )
        prospects = _fetch_prospects(
            client, settings.prospects_table, settings.prospect_situation_field
        )
        logger.info("Loaded %d prospect record(s).", len(prospects))

        log_situation_field_diagnostics(prospects, settings.prospect_situation_field)
        logger.info("Counting stats…")
        counts = count_prospect_situations(prospects, settings.prospect_situation_field)

        print("")
        print("--- Prospect Situation counts ---")
        for label in ORDERED_LABELS:
            print(f"{label}: {counts[label]}")
        print("---------------------------------")
        print("")

        do_kpis = False
        if settings.kpi_update_mode == "always":
            do_kpis = True
        elif settings.kpi_update_mode == "prompt":
            do_kpis = _want_kpi_write_interactive()

        if do_kpis:
            _apply_kpi_update(client, settings, counts)
        elif settings.kpi_update_mode == "prompt":
            logger.info("KPIS update skipped (no confirmation).")
        elif os.getenv("GITHUB_ACTIONS") == "true" and not do_kpis:
            logger.info(
                "GitHub Actions: KPIS not written. To enable, set repository Secret or Variable "
                "UPDATE_KPIS=true, add Secret KPIS_TABLE_NAME, and grant the PAT data.records:write."
            )

        logger.info("Done.")
    except RuntimeError as e:
        logger.error("%s", e)
        return 4
    except AirtableAPIError as e:
        logger.error("Airtable API error (HTTP %s): %s", e.status_code, e)
        if e.body:
            logger.error("Response body (truncated): %s", e.body[:1200])
        if e.status_code == 401:
            logger.error(
                "HTTP 401 means Airtable did not accept your API token. Fix it by:\n"
                "  1. Open https://airtable.com/create/tokens and create a new Personal Access Token.\n"
                "  2. Enable scope: data.records:read (and data.records:write when updating KPIS).\n"
                "  3. Under Access, add your base.\n"
                "  4. Copy the full token into AIRTABLE_PERSONAL_ACCESS_TOKEN in .env.\n"
                "  5. Revoke old tokens you no longer use."
            )
        return 5
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
