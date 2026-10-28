"""Power BI Direct Lake step for the Tableau Ossie quickstart notebook.

Rewrites Convert's SQL shapes for ossie_microsoft, exports TPC-H from
SNOWFLAKE_SAMPLE_DATA.TPCH_SF1 into a Fabric lakehouse (typed parquet, pipe-delimited
fallback), converts to a Direct Lake model.bim and deploys it. Lakehouse names
cannot contain hyphens; TMSL needs defaultPowerBIDataSourceVersion=powerBI_V3.
Tokens are never printed.

ossie_microsoft only translates SUM over a single column, so LINEITEM is exported
with DISCOUNTED_PRICE = L_EXTENDEDPRICE * (1 - L_DISCOUNT) and ORDER_CUSTKEY from
ORDERS. revenue_per_customer counts that key on LINEITEM, the customers on the
line-item join, not every row of CUSTOMER.

Set OSSIE_QS_TPCH_FILTER=YYYY-MM to export one L_SHIPDATE month instead of full SF1.
"""
from __future__ import annotations

import base64
import copy
import csv
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
import warnings
from decimal import Decimal
from pathlib import Path
from time import sleep

import yaml
from ossie_microsoft import convert_ossie_to_semantic_model
from ossie_microsoft.engine import (
    FABRIC_API,
    deploy,
    evaluate,
    refresh,
    _request,
    _request_failure,
    _wait_for_operation,
)

LH_NAME = "ossie_qs_lh"  # Fabric displayName cannot contain hyphens
MODEL_NAME = "ossie-qs-tpch"
LH_SCHEMA = "TPCH_SF1"
# Fabric caches Parquet footers by path, so a reused path keeps an old schema; use a fresh folder per run.
UPLOAD_DIR = "Files/tpch_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
SF_DB = "SNOWFLAKE_SAMPLE_DATA"
SF_SCHEMA = "TPCH_SF1"
TABLES = ("LINEITEM", "ORDERS", "CUSTOMER", "NATION", "PART", "SUPPLIER")
SF1_ROWS = {
    "LINEITEM": 6_001_215,
    "ORDERS": 1_500_000,
    "PART": 200_000,
    "CUSTOMER": 150_000,
    "SUPPLIER": 10_000,
    "NATION": 25,
}
SQL_DIALECTS = frozenset({"ANSI_SQL", "SNOWFLAKE"})
RPC_DAX = (
    "DIVIDE(SUM('LINEITEM'[DISCOUNTED_PRICE]), "
    "DISTINCTCOUNTNOBLANK('LINEITEM'[ORDER_CUSTKEY]))"
)
_PBI_KEYS = ("PBI_TENANT_ID", "PBI_CLIENT_ID", "PBI_CLIENT_SECRET", "PBI_WORKSPACE_ID")
_PLACEHOLDER_ID = "00000000-0000-0000-0000-000000000000"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
QUOTED = re.compile(r'"([^"]+)"|`([^`]+)`|\[([^\]]+)\]')
DOUBLE_PAREN = re.compile(r"\(\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\)")
DISTINCT_PAREN = re.compile(r"\bDISTINCT\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", re.I)
QUALIFIED = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")


def _unquote(expr: str) -> str:
    e = expr.strip()
    if len(e) >= 2 and (
        (e[0] == e[-1] == '"')
        or (e[0] == e[-1] == "`")
        or (e[0] == "[" and e[-1] == "]")
    ):
        return e[1:-1]
    return e


def _sql_dialects(obj: dict) -> list[dict]:
    out = []
    for de in ((obj.get("expression") or {}).get("dialects") or []):
        if (
            isinstance(de, dict)
            and de.get("dialect") in SQL_DIALECTS
            and isinstance(de.get("expression"), str)
        ):
            out.append(de)
    return out


def _physical_to_field(doc: dict) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for ds in doc.get("datasets") or []:
        for f in ds.get("fields") or []:
            name = f.get("name")
            if not name:
                continue
            for de in _sql_dialects(f):
                raw = de["expression"].strip()
                phys = _unquote(raw)
                if phys and phys != name:
                    mapping[phys] = name
                    mapping[raw] = name
    return mapping


def _rewrite_sql(expr: str, mapping: dict[str, str]) -> str:
    def quoted(mo: re.Match) -> str:
        phys = mo.group(1) or mo.group(2) or mo.group(3)
        return mapping.get(phys, mapping.get(mo.group(0), mo.group(0)))

    def qualified(mo: re.Match) -> str:
        table, col = mo.group(1), mo.group(2)
        return mapping.get(f"{table}.{col}", mapping.get(col, mo.group(0)))

    text = QUOTED.sub(quoted, expr)
    text = QUALIFIED.sub(qualified, text)
    text = DOUBLE_PAREN.sub(r"(\1)", text)
    text = DISTINCT_PAREN.sub(r"DISTINCT \1", text)
    return text


def _ensure_lineitem_field(doc: dict, name: str, datatype: str) -> None:
    for ds in doc.get("datasets") or []:
        if ds.get("name") != "LINEITEM":
            continue
        fields = ds.setdefault("fields", [])
        if any(f.get("name") == name for f in fields):
            return
        fields.append(
            {
                "name": name,
                "expression": {
                    "dialects": [
                        {"dialect": "SNOWFLAKE", "expression": name},
                        {"dialect": "ANSI_SQL", "expression": name},
                    ]
                },
                "datatype": datatype,
            }
        )
        return


def _ensure_discounted_price(doc: dict) -> None:
    _ensure_lineitem_field(doc, "DISCOUNTED_PRICE", "Float")
    _ensure_lineitem_field(doc, "ORDER_CUSTKEY", "Integer")


def _is_physical_column(field: dict) -> bool:
    """Keep columns that exist in the exported Delta tables.

    Tableau calcs (customer_label = C_NAME || ' - ' || C_MKTSEGMENT) are not
    written to parquet. ossie_microsoft would map them as Direct Lake
    sourceColumns and refresh would fail.
    """
    dialects = _sql_dialects(field)
    if not dialects:
        return bool(IDENTIFIER_RE.match(field.get("name") or ""))
    return all(IDENTIFIER_RE.match(_unquote(de["expression"])) for de in dialects)


def reformat_document(doc: dict) -> dict:
    d = copy.deepcopy(doc)
    _ensure_discounted_price(d)
    mapping = _physical_to_field(d)
    for ds in d.get("datasets") or []:
        ds["fields"] = [f for f in (ds.get("fields") or []) if _is_physical_column(f)]
        for f in ds["fields"]:
            name = f.get("name")
            if not name:
                continue
            for de in _sql_dialects(f):
                if not IDENTIFIER_RE.match(_unquote(de["expression"])):
                    de["expression"] = name
    for m in d.get("metrics") or []:
        name = m.get("name")
        for de in _sql_dialects(m):
            if name == "total_revenue":
                de["expression"] = "SUM(DISCOUNTED_PRICE)"
            elif name == "revenue_per_customer":
                de["expression"] = "SUM(DISCOUNTED_PRICE) / COUNT(DISTINCT ORDER_CUSTKEY)"
            else:
                de["expression"] = _rewrite_sql(de["expression"], mapping)
    return d


def month_filter() -> tuple[str | None, str]:
    raw = (os.environ.get("OSSIE_QS_TPCH_FILTER") or "").strip()
    if not raw:
        return None, ""
    start = datetime.strptime(raw, "%Y-%m").date()
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    clause = (
        f"WHERE L_SHIPDATE >= '{start.isoformat()}' "
        f"AND L_SHIPDATE < '{end.isoformat()}'"
    )
    return raw, clause


def chunk_rows() -> int:
    return int(os.environ.get("OSSIE_QS_CHUNK_ROWS") or "1000000")


def _read_pbi_env(qs: Path, ossie_root: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for path in (qs / ".env", ossie_root.parent / ".env", Path(".env")):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().upper()
            if k.startswith("PBI_"):
                env[k] = v.strip().strip("'\"")
    for k in _PBI_KEYS:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def pbi_configured(qs: Path, ossie_root: Path) -> bool:
    env = _read_pbi_env(qs, ossie_root)
    return all(env.get(k) for k in _PBI_KEYS)


def load_pbi_env(qs: Path, ossie_root: Path) -> dict[str, str]:
    env = _read_pbi_env(qs, ossie_root)
    missing = [k for k in _PBI_KEYS if not env.get(k)]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} in .env (values are never printed)")
    return env


def aad_token(env: dict[str, str], scope: str) -> str:
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": env["PBI_CLIENT_ID"],
            "client_secret": env["PBI_CLIENT_SECRET"],
            "scope": scope,
        }
    ).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{env['PBI_TENANT_ID']}/oauth2/v2.0/token",
        data=data,
        method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        token = json.loads(resp.read()).get("access_token")
    if not token:
        raise RuntimeError(f"AAD token response for {scope} had no access_token")
    return token


def wait_lro(status, body, headers, token, what: str):
    if status in (200, 201):
        return body if isinstance(body, dict) else {}
    if status != 202:
        raise RuntimeError(f"{what}: {_request_failure(status, body)}")
    loc = headers.get("Location") or headers.get("location")
    if not loc:
        raise RuntimeError(f"{what}: HTTP 202 with no Location")
    result = _wait_for_operation(loc, token, tries=60, delay=3)
    if result.get("status") != "Succeeded":
        raise RuntimeError(f"{what}: {json.dumps(result)[:1500]}")
    st, created, _ = _request("GET", f"{loc}/result", token)
    if st == 200 and isinstance(created, dict):
        return created
    return result


def dfs_call(method, url, token, data=None, extra=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("x-ms-version", "2021-06-08")
    if extra:
        for k, v in extra.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:400]


def describe_workspace_capacity(wid: str, fabric: str) -> str:
    """Return sku / assignment state. Lakehouses need Fabric F or Trial, not PPU."""
    st, ws, _ = _request("GET", f"{FABRIC_API}/workspaces/{wid}", fabric)
    if st != 200 or not isinstance(ws, dict):
        return f"workspace lookup failed: {_request_failure(st, ws)}"
    cid = ws.get("capacityId")
    progress = ws.get("capacityAssignmentProgress") or "unknown"
    sku = state = name = "?"
    st, caps, _ = _request("GET", f"{FABRIC_API}/capacities", fabric)
    if st == 200 and isinstance(caps, dict) and cid:
        hit = next((c for c in (caps.get("value") or []) if c.get("id") == cid), None)
        if hit:
            sku = str(hit.get("sku") or "?")
            state = str(hit.get("state") or "?")
            name = str(hit.get("displayName") or "?")
    note = f"capacity {name} sku={sku} state={state} assignment={progress}"
    if progress and progress != "Completed":
        note += (
            "; lakehouse create stays FeatureNotAvailable until assignment completes"
        )
    elif sku.startswith("PP"):
        note += "; Premium Per User cannot host lakehouses (need Fabric F or Trial)"
    elif sku in {"", "?"}:
        note += "; workspace has no Fabric capacity"
    return note


def find_or_create_lakehouse(wid: str, fabric: str) -> str | None:
    st, body, _ = _request(
        "GET", f"{FABRIC_API}/workspaces/{wid}/items?type=Lakehouse", fabric
    )
    if st != 200 or not isinstance(body, dict):
        print("list lakehouses", _request_failure(st, body))
        items = []
    else:
        items = body.get("value") or []
    hit = next((i for i in items if i.get("displayName") == LH_NAME and i.get("id")), None)
    if hit:
        print("lakehouse", LH_NAME, _mask(hit["id"]))
        return hit["id"]
    st, body, headers = _request(
        "POST",
        f"{FABRIC_API}/workspaces/{wid}/lakehouses",
        fabric,
        {
            "displayName": LH_NAME,
            "description": "Ossie quickstart TPC-H from Snowflake",
            "creationPayload": {"enableSchemas": True},
        },
    )
    if st in (200, 201, 202):
        created = wait_lro(st, body, headers, fabric, f"create lakehouse {LH_NAME}")
        lh_id = created.get("id") if isinstance(created, dict) else None
        if lh_id:
            print("created lakehouse", LH_NAME, _mask(lh_id))
            return lh_id
    print("create lakehouse", LH_NAME, _request_failure(st, body))
    print(describe_workspace_capacity(wid, fabric))
    fallback = next((i for i in items if i.get("id")), None)
    if fallback:
        print("using existing lakehouse", fallback.get("displayName"), _mask(fallback["id"]))
        return fallback["id"]
    return None


def ensure_schema(wid: str, lh_id: str, fabric: str) -> None:
    st, body, headers = _request(
        "POST",
        f"{FABRIC_API}/workspaces/{wid}/lakehouses/{lh_id}/schemas",
        fabric,
        {"name": LH_SCHEMA},
    )
    if st in (200, 201, 202):
        wait_lro(st, body, headers, fabric, f"create schema {LH_SCHEMA}")
        print("schema", LH_SCHEMA)
        return
    detail = _request_failure(st, body)
    if st in (400, 409) and "already" in detail.lower():
        print("schema", LH_SCHEMA, "exists")
        return
    # Some tenants expose schemas only via the load path.
    print("schema", LH_SCHEMA, "create skipped", st)


def _upload(local: Path, rel: str, wid: str, lh_id: str, storage: str) -> None:
    blob = local.read_bytes()
    base = f"https://onelake.dfs.fabric.microsoft.com/{wid}/{lh_id}/{rel}"
    st, body = dfs_call("PUT", base + "?resource=file", storage)
    if st not in (201, 200):
        raise RuntimeError(f"OneLake create {rel}: HTTP {st} {body!r}"[:500])
    st, body = dfs_call(
        "PATCH",
        base + "?action=append&position=0",
        storage,
        blob,
        {"Content-Type": "application/octet-stream"},
    )
    if st not in (200, 201, 202):
        raise RuntimeError(f"OneLake append {rel}: HTTP {st} {body!r}"[:500])
    st, body = dfs_call(
        "PATCH", base + f"?action=flush&position={len(blob)}", storage
    )
    if st not in (200, 201):
        raise RuntimeError(f"OneLake flush {rel}: HTTP {st} {body!r}"[:500])


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)



_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _mask(text: str) -> str:
    """Hide Fabric GUIDs in printed output."""
    return _GUID.sub("<id>", str(text))


def _arrow_schema(description) -> "object | None":
    """Fixed Arrow schema from Snowflake cursor metadata so every chunk has identical types."""
    try:
        import pyarrow as pa
        from snowflake.connector.constants import FIELD_ID_TO_NAME
    except ImportError:
        return None
    fields = []
    for d in description:
        kind = FIELD_ID_TO_NAME.get(d.type_code, "TEXT")
        if kind == "FIXED":
            typ = pa.decimal128(d.precision or 38, d.scale) if d.scale else pa.int64()
        elif kind == "REAL":
            typ = pa.float64()
        elif kind == "DATE":
            typ = pa.date32()
        elif kind.startswith("TIMESTAMP"):
            typ = pa.timestamp("us")
        elif kind == "BOOLEAN":
            typ = pa.bool_()
        else:
            typ = pa.string()
        fields.append(pa.field(d.name, typ))
    return pa.schema(fields)


def _write_rows(
    columns: list[str],
    rows: list[tuple],
    local_parquet: Path,
    local_psv: Path,
    schema=None,
) -> tuple[Path, str, dict]:
    local_parquet.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.table(
            {col: [row[i] for row in rows] for i, col in enumerate(columns)}, schema=schema
        )
        pq.write_table(table, local_parquet, store_decimal_as_integer=True)
        return local_parquet, f"{UPLOAD_DIR}/{local_parquet.name}", {"format": "Parquet"}
    except ImportError:
        global _WARNED_NO_PYARROW
        if not _WARNED_NO_PYARROW:
            print("pyarrow not installed (pip install pyarrow, then restart the kernel); uploading pipe-delimited CSV")
            _WARNED_NO_PYARROW = True
    # Pipe-delimited fallback, written only when pyarrow is missing.
    with local_psv.open("w", newline="") as fh:
        writer = csv.writer(fh, delimiter="|", lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow(_text(v) for v in row)
    return local_psv, f"{UPLOAD_DIR}/{local_psv.name}", {"format": "Csv", "header": True, "delimiter": "|"}


_WARNED_NO_PYARROW = False


def _require_delete_ok(what: str) -> None:
    if os.environ.get("PBI_ALLOW_DELETE") != "1":
        raise RuntimeError(
            f"{what} needs to delete existing Fabric data; set PBI_ALLOW_DELETE=1 to allow it"
        )


def _drop_table_dir(wid: str, lh_id: str, storage: str, table: str) -> None:
    """Remove a stale lakehouse table folder so Overwrite cannot keep its old schema."""
    url = (
        f"https://onelake.dfs.fabric.microsoft.com/{wid}/{lh_id}/"
        f"Tables/{LH_SCHEMA}/{table}"
    )
    st, _ = dfs_call("HEAD", url, storage)
    if st == 404:
        return
    if st not in (200, 202):
        raise RuntimeError(f"could not inspect {LH_SCHEMA}.{table} (HTTP {st})")
    st, body = dfs_call("DELETE", f"{url}?recursive=true", storage)
    if st not in (200, 202, 404):
        raise RuntimeError(
            f"delete of {LH_SCHEMA}.{table} failed (HTTP {st}): {_mask(body.decode(errors='replace'))}"
        )
    st, _ = dfs_call("HEAD", url, storage)
    if st != 404:
        raise RuntimeError(f"{LH_SCHEMA}.{table} still exists after delete (HTTP {st})")
    print("replaced stale", f"{LH_SCHEMA}.{table}")


def _load_table(
    wid: str,
    lh_id: str,
    fabric: str,
    storage: str,
    table: str,
    rel: str,
    fmt: dict,
    mode: str,
) -> None:
    def load():
        return _request(
            "POST",
            f"{FABRIC_API}/workspaces/{wid}/lakehouses/{lh_id}/schemas/{LH_SCHEMA}/tables/{table}/load?beta=true",
            fabric,
            {
                "relativePath": rel,
                "pathType": "File",
                "mode": mode,
                "formatOptions": fmt,
            },
        )

    try:
        st, body, headers = load()
        wait_lro(st, body, headers, fabric, f"load {LH_SCHEMA}.{table} {mode}")
    except RuntimeError as exc:
        if "timestampNtz" not in str(exc) and "PARQUET_COLUMN_DATA_TYPE_MISMATCH" not in str(exc):
            raise
        _drop_table_dir(wid, lh_id, storage, table)
        st, body, headers = load()
        wait_lro(st, body, headers, fabric, f"load {LH_SCHEMA}.{table} after drop")


def export_tpch_from_snowflake(
    qs: Path, out: Path, wid: str, lh_id: str, fabric: str, storage: str
) -> dict[str, int]:
    from connect import DEFAULT_ROLE, connect

    month, where = month_filter()
    chunk = chunk_rows()
    conn = connect(DEFAULT_ROLE)
    cur = conn.cursor()
    counts: dict[str, int] = {}
    try:
        for table in TABLES:
            fq = f"{SF_DB}.{SF_SCHEMA}.{table}"
            if table == "LINEITEM":
                # ORDER_CUSTKEY is the customer on each line, so a distinct
                # count matches the SQL pin (customers on the join, not all of CUSTOMER).
                sql = (
                    "SELECT l.*, o.O_CUSTKEY AS ORDER_CUSTKEY, "
                    "l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT) AS DISCOUNTED_PRICE "
                    f"FROM {fq} l "
                    f"JOIN {SF_DB}.{SF_SCHEMA}.ORDERS o ON l.L_ORDERKEY = o.O_ORDERKEY"
                )
                if where:
                    sql = f"{sql} {where.replace('L_SHIPDATE', 'l.L_SHIPDATE')}"
            else:
                sql = f"SELECT * FROM {fq}"
            print("export", fq, month or "full SF1")
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            schema = _arrow_schema(cur.description)
            _drop_table_dir(wid, lh_id, storage, table)
            n = 0
            part = 0
            while True:
                rows = cur.fetchmany(chunk)
                if not rows:
                    break
                stem = f"{table.lower()}_{part:03d}" if table == "LINEITEM" else table.lower()
                local, rel, fmt = _write_rows(
                    cols, rows, out / f"{stem}.parquet", out / f"{stem}.psv", schema
                )
                print("wrote", local.relative_to(qs), "rows", len(rows))
                _upload(local, rel, wid, lh_id, storage)
                mode = "Overwrite" if part == 0 else "Append"
                _load_table(wid, lh_id, fabric, storage, table, rel, fmt, mode)
                n += len(rows)
                part += 1
            counts[table] = n
            print("loaded", f"{LH_SCHEMA}.{table}", "rows", n)
    finally:
        conn.close()
    return counts


def _patch_revenue_per_customer(bim: dict) -> None:
    found = False
    for table in (bim.get("model") or {}).get("tables") or []:
        for measure in table.get("measures") or []:
            if measure.get("name") == "revenue_per_customer":
                measure["expression"] = RPC_DAX
                found = True
    if not found:
        raise AssertionError("revenue_per_customer measure missing")


def _build_bim(pbi_yaml: Path, wid: str, lh_id: str | None) -> dict:
    source = {"workspaceId": wid, "itemId": lh_id or _PLACEHOLDER_ID}
    log = logging.getLogger("ossie_microsoft")
    prev_level = log.level
    log.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            bim = convert_ossie_to_semantic_model(pbi_yaml.read_text(), source=source)
    finally:
        log.setLevel(prev_level)
    kinds: dict[str, int] = {}
    for w in caught:
        key = re.sub(r"\[[^\]]*\]\s*", "", str(w.message)).split(";")[0]
        kinds[key] = kinds.get(key, 0) + 1
    for msg, n in kinds.items():
        print(f"ossie_microsoft warning x{n}: {msg}")
    bim["model"]["defaultPowerBIDataSourceVersion"] = "powerBI_V3"
    _patch_revenue_per_customer(bim)
    tables = (bim.get("model") or {}).get("tables") or []
    by_name = {t.get("name"): t for t in tables if t.get("name")}
    assert set(TABLES) <= set(by_name), sorted(by_name)
    measures = [
        (m["name"], m.get("expression"))
        for t in tables
        for m in (t.get("measures") or [])
        if m.get("name")
    ]
    names = [n for n, _ in measures]
    assert "total_revenue" in names and "revenue_per_customer" in names, measures
    assert all(expr and expr != "BLANK()" for _, expr in measures), measures
    rpc = [expr for name, expr in measures if name == "revenue_per_customer"]
    assert rpc == [RPC_DAX], rpc
    lineitem = by_name["LINEITEM"]
    part = lineitem["partitions"][0]
    assert part["mode"] == "directLake", part
    assert part["source"].get("schemaName") == LH_SCHEMA, part
    assert part["source"].get("entityName") == "LINEITEM", part
    expr = ((bim.get("model") or {}).get("expressions") or [{}])[0].get("expression")
    joined = "\n".join(expr) if isinstance(expr, list) else str(expr)
    assert "AzureStorage.DataLake" in joined
    assert wid in joined
    if lh_id:
        assert lh_id in joined
        assert _PLACEHOLDER_ID not in joined
    cols = {c["name"]: c.get("sourceColumn") for c in lineitem.get("columns") or []}
    assert cols.get("DISCOUNTED_PRICE") == "DISCOUNTED_PRICE", cols
    assert cols.get("ORDER_CUSTKEY") == "ORDER_CUSTKEY", cols
    return bim


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def deploy_or_update(bim: dict, wid: str, fabric: str) -> str:
    st, items, _ = _request(
        "GET", f"{FABRIC_API}/workspaces/{wid}/items?type=SemanticModel", fabric
    )
    if st != 200 or not isinstance(items, dict):
        raise RuntimeError(f"list semantic models: {_request_failure(st, items)}")
    existing = next(
        (i for i in (items.get("value") or []) if i.get("displayName") == MODEL_NAME),
        None,
    )
    definition = {
        "format": "TMSL",
        "parts": [
            {"path": "model.bim", "payload": b64(bim), "payloadType": "InlineBase64"},
            {
                "path": "definition.pbism",
                "payload": b64({"version": "1.0", "settings": {}}),
                "payloadType": "InlineBase64",
            },
        ],
    }
    if existing and existing.get("id"):
        item_id = existing["id"]
        st, body, headers = _request(
            "POST",
            f"{FABRIC_API}/workspaces/{wid}/semanticModels/{item_id}/updateDefinition",
            fabric,
            {"definition": definition},
        )
        if st in (200, 201, 202):
            wait_lro(st, body, headers, fabric, f"updateDefinition {MODEL_NAME}")
            print("updated", MODEL_NAME, item_id)
            return item_id
        _require_delete_ok(f"redeploying semantic model {MODEL_NAME}")
        print("updateDefinition failed; deleting and deploying")
        _request("DELETE", f"{FABRIC_API}/workspaces/{wid}/items/{item_id}", fabric)
        sleep(3)
    item_id, err = deploy(bim, wid, MODEL_NAME, fabric)
    if err:
        raise RuntimeError(f"deploy {MODEL_NAME}: {err}")
    print("deployed", MODEL_NAME, item_id)
    return item_id


def _delete_item_by_name(qs: Path, ossie_root: Path, item_type: str, name: str) -> None:
    """Delete one Fabric item by type and display name when PBI_* is set and the item exists."""
    try:
        env = load_pbi_env(qs, ossie_root)
    except SystemExit as exc:
        print("skipped Fabric", item_type, exc)
        return
    fabric = aad_token(env, "https://api.fabric.microsoft.com/.default")
    wid = env["PBI_WORKSPACE_ID"]
    st, items, _ = _request(
        "GET", f"{FABRIC_API}/workspaces/{wid}/items?type={item_type}", fabric
    )
    if st != 200 or not isinstance(items, dict):
        print("skipped Fabric", item_type, _request_failure(st, items))
        return
    existing = next(
        (i for i in (items.get("value") or []) if i.get("displayName") == name),
        None,
    )
    if not existing or not existing.get("id"):
        print("skipped Fabric", item_type, name, "not found")
        return
    st, body, _ = _request(
        "DELETE", f"{FABRIC_API}/workspaces/{wid}/items/{existing['id']}", fabric
    )
    if st in (200, 202, 204):
        print("deleted", item_type, name)
        return
    print("skipped Fabric", item_type, _request_failure(st, body))


def delete_dataset_if_present(qs: Path, ossie_root: Path) -> None:
    """Delete semantic model ossie-qs-tpch when PBI_* is set and the item exists."""
    _delete_item_by_name(qs, ossie_root, "SemanticModel", MODEL_NAME)


def delete_lakehouse_if_present(qs: Path, ossie_root: Path) -> None:
    """Delete lakehouse ossie_qs_lh (with its tables and uploaded files) when it exists."""
    _delete_item_by_name(qs, ossie_root, "Lakehouse", LH_NAME)


def delete_fabric_objects(qs: Path, ossie_root: Path) -> None:
    """Delete the semantic model first, then the lakehouse it reads from."""
    delete_dataset_if_present(qs, ossie_root)
    delete_lakehouse_if_present(qs, ossie_root)


def run(
    yaml01: Path,
    qs: Path,
    out: Path,
    ossie_root: Path,
    expected_revenue: float,
    expected_rpc: float,
) -> None:
    doc = yaml.safe_load(yaml01.read_text())
    if not isinstance(doc, dict):
        raise ValueError("Ossie YAML must be a mapping")
    pbi_yaml = yaml01.with_name(yaml01.name.replace(".ossie.yaml", ".pbi.ossie.yaml"))
    pbi_yaml.write_text(
        yaml.safe_dump(reformat_document(doc), sort_keys=False, allow_unicode=True)
    )
    print("rewrote", pbi_yaml.relative_to(qs))

    if not pbi_configured(qs, ossie_root):
        bim = _build_bim(pbi_yaml, _PLACEHOLDER_ID, None)
        bim_path = out / "model.bim"
        bim_path.write_text(json.dumps(bim, indent=2) + "\n")
        print("wrote", bim_path.relative_to(qs))
        print(
            "skipped Power BI (set "
            + ", ".join(_PBI_KEYS)
            + ")"
        )
        return

    env = load_pbi_env(qs, ossie_root)
    wid = env["PBI_WORKSPACE_ID"]
    fabric = aad_token(env, "https://api.fabric.microsoft.com/.default")
    storage = aad_token(env, "https://storage.azure.com/.default")
    powerbi = aad_token(env, "https://analysis.windows.net/powerbi/api/.default")
    lh_id = find_or_create_lakehouse(wid, fabric)
    loaded = False
    if lh_id:
        try:
            ensure_schema(wid, lh_id, fabric)
            counts = export_tpch_from_snowflake(qs, out, wid, lh_id, fabric, storage)
            month, _ = month_filter()
            if not month:
                assert counts["LINEITEM"] == SF1_ROWS["LINEITEM"], counts
                assert counts["NATION"] == SF1_ROWS["NATION"], counts
            loaded = True
        except (RuntimeError, urllib.error.URLError, OSError, AssertionError) as exc:
            print("lakehouse load skipped (", type(exc).__name__, ": ", _mask(str(exc)), ")", sep="")
    else:
        print("no Fabric lakehouse available; writing model.bim only")

    bim = _build_bim(pbi_yaml, wid, lh_id)
    measures = [
        (m["name"], m.get("expression"))
        for t in (bim.get("model") or {}).get("tables") or []
        for m in (t.get("measures") or [])
        if m.get("name")
    ]
    print("measures", measures)
    bim_path = out / "model.bim"
    bim_path.write_text(json.dumps(bim, indent=2) + "\n")
    print("wrote", bim_path.relative_to(qs))

    if not loaded:
        if not lh_id:
            print("total_revenue", expected_revenue, "(Snowflake; Fabric lakehouse not available)")
            print("revenue_per_customer", expected_rpc, "(Snowflake; Fabric lakehouse not available)")
        else:
            print("model was not updated; LINEITEM was not reloaded with ORDER_CUSTKEY")
        return

    item_id = deploy_or_update(bim, wid, fabric)
    ref_err = refresh(wid, item_id, powerbi, tries=40, delay=3)
    if ref_err:
        print("refresh", ref_err)
    else:
        print("refresh DirectLakeFraming completed")
    rows, qerr = evaluate(
        wid,
        item_id,
        powerbi,
        'EVALUATE ROW("total_revenue", [total_revenue], "revenue_per_customer", [revenue_per_customer])',
    )
    if qerr:
        denied = not isinstance(qerr, str) or "NotAuthorized" in qerr
        print(
            "executeQueries denied (PowerBINotAuthorizedException): not a failure. "
            "Deploy, refresh and the DAX check passed; the live numbers were not compared. "
            "To enable, have a Fabric admin allow service principals to use Power BI APIs and the "
            "Dataset Execute Queries REST API, and grant the service principal Build on the model."
            if denied
            else f"executeQueries {qerr}; model was not queried"
        )
    else:
        row = rows[0] if rows else {}
        total = row.get("[total_revenue]", row.get("total_revenue"))
        rpc = row.get("[revenue_per_customer]", row.get("revenue_per_customer"))
        print("total_revenue", total)
        print("revenue_per_customer", rpc)
        assert abs(float(total) - expected_revenue) < 1.0, total
        assert abs(float(rpc) - expected_rpc) < 0.01, rpc
    print(
        "open",
        f"https://app.fabric.microsoft.com/groups/{wid}/datasets/{item_id}?experience=power-bi",
    )
