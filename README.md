# Airtable prospect stats (API only)

Single script **`main.py`**: reads `.env`, calls the Airtable REST API, prints **Prospect Situation** counts (Lost, Engaged, Last chance, Admitted, Serious, **Potential**, **Completed**), then either asks whether to write **KPIS** (`UPDATE_KPIS=prompt`, default), always writes (`true`), or never (`false`). Without **`KPI_RECORD_ID`**, each write **creates a new** KPIS row; with **`KPI_RECORD_ID`**, that row is **updated**. KPIS fields: **Serious**, **Admitted**, **Lost**, **Last_chance**, **Engaged**, **Potential**, **Completed**.

## Prerequisites

- Python **3.10+**
- A [Personal Access Token](https://airtable.com/create/tokens) with **`data.records:read`**. Add **`data.records:write`** if you will update KPIS (prompt `y` or `UPDATE_KPIS=true`).

## Install

```powershell
cd path\to\airtable-cron
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure `.env`

See `.env.example`. You need at least:

- **`AIRTABLE_BASE_URL`** (must contain `app…`) **or** **`AIRTABLE_BASE_ID`**
- **`AIRTABLE_PERSONAL_ACCESS_TOKEN`**
- **`PROSPECTS_TABLE_NAME`** (or `PROSPECTS_TABLE_ID`)
- **`KPIS_TABLE_NAME`** (or `KPIS_TABLE_ID`) if you use `UPDATE_KPIS=prompt` or `true`.
- **`UPDATE_KPIS`:** `prompt` (default locally if unset) asks after stats; `true` writes without asking; `false` never writes. **On GitHub Actions** there is no keyboard: `prompt` is treated as **never**, and the workflow defaults `UPDATE_KPIS` to **`false`** unless you set a repository **Variable** or **Secret** named `UPDATE_KPIS` to **`true`** (the workflow passes `secrets.UPDATE_KPIS` first, then `vars.UPDATE_KPIS`).
- **`KPI_RECORD_ID`** (optional): if set, that row is **updated**. If omitted, each write **creates a new** KPIS row (the log prints the new `rec…`; put it in `.env` if you want to update that same row next time).

## Run

```powershell
python main.py
```

## Security

Do not commit `.env` or tokens.
