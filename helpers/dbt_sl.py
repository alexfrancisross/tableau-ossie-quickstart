"""dbt Cloud Admin API + Semantic Layer GraphQL for the quickstart.

Never prints Authorization or token values. Does not create the Cloud project,
git connection, or Semantic Layer credentials.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_NAME = "ossie-qs"
EXPECTED_METRICS = ("total_revenue", "revenue_per_customer")

_TOKEN_RE = re.compile(
    r"(dbtu_[A-Za-z0-9]+)|(Bearer\s+\S+)|(Token\s+\S+)|([A-Za-z0-9_-]{40,})",
    re.I,
)
_RUN_SUCCESS = 10
_RUN_ERROR = 20
_RUN_CANCELLED = 30
_RUN_DONE = {_RUN_SUCCESS, _RUN_ERROR, _RUN_CANCELLED}
_DBT_KEYS = (
    "DBT_CLOUD_URL",
    "DBT_CLOUD_TOKEN",
    "DBT_CLOUD_PROJECT",
    "DBT_CLOUD_ENVIRONMENT_ID",
    "DBT_CLOUD_JOB_ID",
    "DBT_CLOUD_SNOWFLAKE_USER",
    "DBT_CLOUD_SNOWFLAKE_ROLE",
)


class DbtSLError(RuntimeError):
    """Safe to print: tokens are stripped."""


def redact(text: str) -> str:
    return _TOKEN_RE.sub("***", text or "")


def sl_graphql_url(cloud_url: str) -> str:
    parsed = urlparse(cloud_url.rstrip("/"))
    host = parsed.netloc or parsed.path
    if host in {"cloud.getdbt.com", "cloud.dbt.com"}:
        return "https://semantic-layer.cloud.getdbt.com/api/graphql"
    prefix, _, rest = host.partition(".")
    if not rest:
        return f"https://semantic-layer.{host}/api/graphql"
    return f"https://{prefix}.semantic-layer.{rest}/api/graphql"


def _error_class(body: Any) -> str:
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            ext = first.get("extensions") or {}
            code = ext.get("code") or ext.get("errorClass") or ext.get("type")
            msg = redact(str(first.get("message") or first))
            return f"{code}: {msg}" if code else msg
        status = body.get("status")
        if isinstance(status, dict):
            user = status.get("user_message") or status.get("developer_message")
            if user:
                return redact(str(user))
        for key in ("message", "error", "detail"):
            if body.get(key):
                return redact(str(body[key]))
    return redact(str(body)[:240])


def load_dbt_env(qs: Path | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    roots = []
    if qs is not None:
        roots.append(qs / ".env")
        roots.append(qs.parent / ".env")
    roots.append(Path(".env"))
    for path in roots:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().upper()
            if k.startswith("DBT_CLOUD_"):
                env[k] = v.strip().strip("'\"")
    for k in _DBT_KEYS:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def cloud_configured(qs: Path | None = None) -> bool:
    env = load_dbt_env(qs)
    return bool(env.get("DBT_CLOUD_URL") and env.get("DBT_CLOUD_TOKEN"))


@dataclass
class Resource:
    id: int
    name: str
    extra: dict = field(default_factory=dict)


@dataclass
class Discover:
    account: Resource
    project: Resource
    environments: list[Resource]
    jobs: list[Resource]


class DbtSL:
    def __init__(self, base_url: str, token: str, project_name: str = PROJECT_NAME, timeout: int = 60) -> None:
        self.base = base_url.rstrip("/")
        self._token = token
        self.project_name = project_name
        self.timeout = timeout
        self.env_id_override: int | None = None
        self.job_id_override: int | None = None

    @classmethod
    def from_env(cls, qs: Path | None = None) -> DbtSL:
        env = load_dbt_env(qs)
        url = env.get("DBT_CLOUD_URL")
        token = env.get("DBT_CLOUD_TOKEN")
        if not url or not token:
            raise DbtSLError("DBT_CLOUD_URL and DBT_CLOUD_TOKEN are required")
        client = cls(url, token, env.get("DBT_CLOUD_PROJECT") or PROJECT_NAME)
        if env.get("DBT_CLOUD_ENVIRONMENT_ID"):
            client.env_id_override = int(env["DBT_CLOUD_ENVIRONMENT_ID"])
        if env.get("DBT_CLOUD_JOB_ID"):
            client.job_id_override = int(env["DBT_CLOUD_JOB_ID"])
        return client

    def sl_url(self) -> str:
        return sl_graphql_url(self.base)

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base}{path}"

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self._url(path), data=data, method=method)
        req.add_header("Authorization", f"Token {self._token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {"message": redact(raw.decode("utf-8", "replace")[:200])}
            raise DbtSLError(f"{method} {path} HTTP {status}: {_error_class(body)}") from None
        except urllib.error.URLError as exc:
            raise DbtSLError(redact(f"{method} {path}: {type(exc.reason).__name__}")) from None
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise DbtSLError(f"{method} {path} HTTP {status}: non-json") from None
        return body

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict | None = None) -> Any:
        return self._request("POST", path, payload or {})

    def _data(self, body: Any) -> Any:
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    def _rows(self, body: Any) -> list[dict]:
        data = self._data(body)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    def discover(self, project_name: str | None = None) -> Discover:
        wanted = (project_name or self.project_name).lower()
        accounts = self._rows(self.get("/api/v2/accounts/"))
        if not accounts:
            raise DbtSLError("token has no dbt Cloud accounts")
        acct = accounts[0]
        account = Resource(int(acct["id"]), str(acct.get("name") or acct["id"]), acct)
        projects = self._rows(self.get(f"/api/v3/accounts/{account.id}/projects/"))
        if not projects:
            projects = self._rows(self.get(f"/api/v2/accounts/{account.id}/projects/"))
        hit = next((p for p in projects if str(p.get("name") or "").lower() == wanted), None)
        if hit is None:
            names = sorted(str(p.get("name") or p.get("id")) for p in projects)
            raise DbtSLError(
                f"dbt Cloud project {wanted!r} not found; visible: {names}. "
                "Create that project (or set DBT_CLOUD_PROJECT) with subdirectory "
                "dbt_tpch. Some plans allow only one active project."
            )
        project = Resource(int(hit["id"]), str(hit.get("name") or hit["id"]), hit)
        environments = [
            Resource(int(e["id"]), str(e.get("name") or e["id"]), e)
            for e in self._list_environments(account.id, project.id)
        ]
        jobs = [
            Resource(int(j["id"]), str(j.get("name") or j["id"]), j)
            for j in self._list_jobs(account.id, project.id)
        ]
        return Discover(account, project, environments, jobs)

    def _list_environments(self, account_id: int, project_id: int) -> list[dict]:
        path = f"/api/v3/accounts/{account_id}/projects/{project_id}/environments/"
        try:
            return self._rows(self.get(path))
        except DbtSLError:
            return self._rows(self.get(f"/api/v2/accounts/{account_id}/environments/?project_id={project_id}"))

    def _list_jobs(self, account_id: int, project_id: int) -> list[dict]:
        try:
            return self._rows(self.get(f"/api/v2/accounts/{account_id}/jobs/?project_id={project_id}"))
        except DbtSLError:
            return []

    def deployment_env(self, disc: Discover) -> Resource:
        if self.env_id_override is not None:
            hit = next((e for e in disc.environments if e.id == self.env_id_override), None)
            if hit is None:
                raise DbtSLError(f"DBT_CLOUD_ENVIRONMENT_ID {self.env_id_override} not on project {disc.project.name}")
            return hit
        deploy = [
            e
            for e in disc.environments
            if str(e.extra.get("type") or "").lower() == "deployment"
            or str(e.extra.get("deployment_type") or "")
        ]
        if not deploy:
            names = [e.name for e in disc.environments]
            raise DbtSLError(
                f"project {disc.project.name} has no deployment environment (have {names}); "
                "Semantic Layer does not query Development"
            )
        return deploy[0]

    def parse_job(self, disc: Discover) -> Resource:
        if self.job_id_override is not None:
            hit = next((j for j in disc.jobs if j.id == self.job_id_override), None)
            if hit is None:
                raise DbtSLError(f"DBT_CLOUD_JOB_ID {self.job_id_override} not on project {disc.project.name}")
            return hit
        if not disc.jobs:
            raise DbtSLError(
                f"project {disc.project.name} has no jobs; create a deploy job that runs "
                "`dbt run --select stg_* metricflow_time_spine` then `dbt parse`"
            )
        return disc.jobs[0]

    def _failed_step_log(self, account_id: int, run_id: int, max_lines: int = 15) -> str:
        """Last log lines of the first failed run step; empty on any failure."""
        try:
            got = self._data(
                self.get(f'/api/v2/accounts/{account_id}/runs/{run_id}/?include_related=["run_steps"]')
            )
            for step in got.get("run_steps") or []:
                if int(step.get("status") or 0) == _RUN_ERROR:
                    text = str(step.get("logs") or step.get("debug_logs") or "")
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][-max_lines:]
                    if lines:
                        return f"\nstep {step.get('name')!r}:\n" + redact("\n".join(lines))
        except Exception:
            pass
        return ""

    def trigger_and_wait(self, account_id: int, job_id: int, timeout_s: int = 900) -> dict:
        body = self.post(
            f"/api/v2/accounts/{account_id}/jobs/{job_id}/run/",
            {"cause": "ossie quickstart Semantic Layer parse"},
        )
        run = self._data(body)
        if not isinstance(run, dict) or not run.get("id"):
            raise DbtSLError(f"trigger job {job_id} returned no run id")
        run_id = int(run["id"])
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            got = self._data(self.get(f"/api/v2/accounts/{account_id}/runs/{run_id}/"))
            status = int(got.get("status") or 0)
            if status in _RUN_DONE:
                if status != _RUN_SUCCESS:
                    human = got.get("status_humanized") or status
                    msg = redact(str(got.get("status_message") or ""))[:220]
                    detail = self._failed_step_log(account_id, run_id)
                    raise DbtSLError(f"job {job_id} run {run_id} {human}: {msg}{detail}")
                return got if isinstance(got, dict) else {"id": run_id, "status": status}
            time.sleep(8)
        raise DbtSLError(f"job {job_id} run {run_id} timed out after {timeout_s}s")

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(self.sl_url(), data=payload, method="POST")
        req.add_header("Authorization", f"Token {self._token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                raise DbtSLError(f"SL GraphQL HTTP {status}: non-json") from None
            raise DbtSLError(f"SL GraphQL HTTP {status}: {_error_class(body)}") from None
        except urllib.error.URLError as exc:
            raise DbtSLError(redact(f"SL GraphQL: {type(exc.reason).__name__}")) from None
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise DbtSLError(f"SL GraphQL HTTP {status}: non-json") from None
        if not isinstance(body, dict):
            raise DbtSLError("SL GraphQL returned a non-object")
        return body

    def sl_metrics(self, environment_id: int) -> tuple[list[str], str | None]:
        q_page = """
        query($env: BigInt!) {
          metricsPaginated(environmentId: $env, pageNum: 1, pageSize: 200) {
            items { name type }
          }
        }
        """
        body = self.graphql(q_page, {"env": environment_id})
        if body.get("errors"):
            q_old = """
            query($env: BigInt!) {
              metrics(environmentId: $env) { name }
            }
            """
            old = self.graphql(q_old, {"env": environment_id})
            if old.get("errors"):
                return [], _error_class(old)
            data = (old.get("data") or {}).get("metrics") or []
            return [str(m.get("name")) for m in data if isinstance(m, dict) and m.get("name")], None
        items = ((body.get("data") or {}).get("metricsPaginated") or {}).get("items") or []
        return [str(m.get("name")) for m in items if isinstance(m, dict) and m.get("name")], None

    def sl_query_metric(self, environment_id: int, name: str, timeout_s: int = 180) -> float:
        created = self.graphql(
            """
            mutation($env: BigInt!, $name: String!) {
              createQuery(environmentId: $env, metrics: [{name: $name}]) { queryId }
            }
            """,
            {"env": environment_id, "name": name},
        )
        if created.get("errors"):
            raise DbtSLError(f"createQuery {name}: {_error_class(created)}")
        query_id = ((created.get("data") or {}).get("createQuery") or {}).get("queryId")
        if not query_id:
            raise DbtSLError(f"createQuery {name} returned no queryId")
        deadline = time.time() + timeout_s
        last: dict = {}
        while time.time() < deadline:
            last = self.graphql(
                """
                query($env: BigInt!, $id: String!) {
                  query(environmentId: $env, queryId: $id) {
                    status
                    error
                    jsonResult(encoded: true)
                  }
                }
                """,
                {"env": environment_id, "id": query_id},
            )
            if last.get("errors"):
                raise DbtSLError(f"query {name}: {_error_class(last)}")
            row = (last.get("data") or {}).get("query") or {}
            status = str(row.get("status") or "").upper()
            if status in {"FAILED", "ERROR"}:
                raise DbtSLError(f"query {name} {status}: {redact(str(row.get('error') or ''))}")
            if status in {"SUCCESSFUL", "SUCCESS"}:
                return _metric_value(row.get("jsonResult"), name)
            time.sleep(3)
        raise DbtSLError(f"query {name} timed out ({(last.get('data') or {}).get('query', {}).get('status')})")


def _metric_value(raw: Any, name: str) -> float:
    if raw is None:
        raise DbtSLError(f"{name}: empty jsonResult")
    if isinstance(raw, str):
        try:
            decoded = base64.b64decode(raw)
            raw = json.loads(decoded)
        except (ValueError, json.JSONDecodeError):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DbtSLError(f"{name}: jsonResult not json") from exc
    if not isinstance(raw, dict):
        raise DbtSLError(f"{name}: jsonResult is not an object")
    rows = raw.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        row = rows[0]
    elif isinstance(raw.get("schema"), dict) and isinstance(rows, list) and rows:
        row = rows[0] if isinstance(rows[0], dict) else {}
    else:
        row = raw
    key = next((k for k in row if str(k).lower() == name.lower()), None)
    if key is None:
        raise DbtSLError(f"{name}: no numeric column in jsonResult keys {sorted(row)}")
    try:
        return float(row[key])
    except (TypeError, ValueError) as exc:
        raise DbtSLError(f"{name}: value {row[key]!r} is not numeric") from exc


def deploy_and_query(
    qs: Path | None = None,
    metrics: tuple[str, ...] = EXPECTED_METRICS,
    timeout_s: int = 900,
) -> dict[str, float]:
    client = DbtSL.from_env(qs)
    disc = client.discover()
    env = client.deployment_env(disc)
    job = client.parse_job(disc)
    print("dbt Cloud project, deployment environment and parse job found")
    run = client.trigger_and_wait(disc.account.id, job.id, timeout_s=timeout_s)
    print("dbt job run", run.get("status_humanized") or "success")
    names, err = client.sl_metrics(env.id)
    if err:
        raise DbtSLError(f"sl_metrics: {err}")
    print("sl metrics", names)
    missing = [n for n in metrics if n not in names]
    if missing:
        raise DbtSLError(f"Semantic Layer missing {missing}; have {names}")
    out: dict[str, float] = {}
    for name in metrics:
        out[name] = client.sl_query_metric(env.id, name)
        print(name, out[name])
    return out


def explorer_info(qs: Path | None = None) -> dict:
    """GraphQL explorer URL and deployment environment for the notebook. Never includes the token."""
    env = load_dbt_env(qs)
    info: dict = {"url": None, "environment_id": None, "environment_name": None}
    if not env.get("DBT_CLOUD_URL"):
        return info
    info["url"] = sl_graphql_url(env["DBT_CLOUD_URL"])
    if not env.get("DBT_CLOUD_TOKEN"):
        return info
    try:
        client = DbtSL.from_env(qs)
        deploy_env = client.deployment_env(client.discover())
    except DbtSLError:
        return info
    info["environment_id"] = deploy_env.id
    info["environment_name"] = deploy_env.name
    return info
