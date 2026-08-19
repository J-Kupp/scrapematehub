from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import re
import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from adapters import ADAPTER_REGISTRY
from config import load_supplier_configs, save_supplier_configs
from .auth import authenticate_user, change_password, ensure_bootstrap_users
from .config import WebAppConfig, load_webapp_config
from .db import connect, init_db
from .ecs_jobs import count_ecs_stream_matches, read_ecs_task_logs
from .jobs import JobRunner, build_job_command, get_job, list_jobs, queue_job, tail_file
from .service import (
    build_supplier_from_form,
    parse_bool_form,
    read_json_file,
    resolve_allowed_artifact,
    supplier_by_slug,
    supplier_health_summary,
    system_health,
)


def template_context(request: Request, **extra: Any) -> dict[str, Any]:
    context = {"request": request, "current_user": request.session.get("user")}
    context.update(extra)
    return context


def require_user(request: Request) -> dict[str, str]:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def load_job_logs(job: dict[str, Any], app_config: WebAppConfig) -> tuple[str, str, str]:
    if job.get("backend") == "ecs_fargate" and job.get("remote_job_id"):
        try:
            log_group, log_stream, logs = read_ecs_task_logs(
                app_config.ecs_backend,
                task_arn=job["remote_job_id"],
                log_group=job.get("cloudwatch_log_group", "") or "",
                log_stream=job.get("cloudwatch_log_stream", "") or "",
            )
        except Exception as exc:  # pragma: no cover - defensive path for AWS hiccups
            fallback = f"Unable to load CloudWatch logs right now: {exc}"
            return (
                job.get("cloudwatch_log_group", "") or "",
                job.get("cloudwatch_log_stream", "") or "",
                fallback,
            )
        return log_group, log_stream, logs
    if not job.get("log_path"):
        return job.get("cloudwatch_log_group", "") or "", job.get("cloudwatch_log_stream", "") or "", ""
    return (
        job.get("cloudwatch_log_group", "") or "",
        job.get("cloudwatch_log_stream", "") or "",
        tail_file(Path(job["log_path"])),
    )


def count_local_log_matches(log_path: str | None, pattern: re.Pattern[str]) -> int:
    if not log_path:
        return 0
    path = Path(log_path)
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if pattern.search(line):
                count += 1
    return count


PARSED_PRODUCT_RE = re.compile(r"\bParsed product\b")
ERROR_LINE_RE = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception|FAILED)\b", re.IGNORECASE)


def summarize_job_logs(log_text: str) -> str:
    if not log_text.strip():
        return "Scraped items: 0"

    lines = [line.rstrip() for line in log_text.splitlines() if line.strip()]
    scraped_items = sum(1 for line in lines if PARSED_PRODUCT_RE.search(line))
    error_lines: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not ERROR_LINE_RE.search(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        error_lines.append(line)

    summary_lines = [f"Scraped items: {scraped_items}"]
    if error_lines:
        summary_lines.append("")
        summary_lines.append("Potential errors:")
        summary_lines.extend(error_lines[-20:])
    return "\n".join(summary_lines)


def build_job_progress(
    job: dict[str, Any],
    app_config: WebAppConfig,
) -> dict[str, Any]:
    log_group, log_stream, log_tail = load_job_logs(job, app_config)
    scraped_count = 0
    if job.get("backend") == "ecs_fargate" and job.get("remote_job_id"):
        try:
            scraped_count = count_ecs_stream_matches(
                app_config.ecs_backend,
                task_arn=job["remote_job_id"],
                log_group=log_group,
                log_stream=log_stream,
            )
        except Exception:
            scraped_count = 0
    else:
        scraped_count = count_local_log_matches(job.get("log_path"), PARSED_PRODUCT_RE)

    error_lines = []
    seen: set[str] = set()
    for line in log_tail.splitlines():
        if not ERROR_LINE_RE.search(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        error_lines.append(line)

    return {
        "scraped_count": scraped_count,
        "errors": error_lines[-10:],
        "log_group": log_group,
        "log_stream": log_stream,
        "log_tail": log_tail,
    }


def create_app(config_path: Path | None = None) -> FastAPI:
    app_config = load_webapp_config(config_path)
    conn = connect(app_config.resolved_db_path())
    init_db(conn)
    ensure_bootstrap_users(conn, app_config)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    supplier_config_path = app_config.resolved_supplier_config_path()
    job_runner = JobRunner(
        conn,
        app_config.resolved_env_path(),
        supplier_config_path,
        job_backend=app_config.job_backend,
        ecs_backend=app_config.ecs_backend,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        job_runner.start()
        job_runner.start_scheduler(
            enabled=app_config.scheduler_enabled,
            mode=app_config.scheduler_mode,
        )
        try:
            yield
        finally:
            job_runner.stop()
            conn.close()

    middleware = [
        Middleware(
            ProxyHeadersMiddleware,
            trusted_hosts=app_config.forwarded_allow_ips,
        ),
        Middleware(
            SessionMiddleware,
            secret_key=app_config.session_secret(),
            same_site=app_config.session_same_site,
            https_only=app_config.session_https_only,
        ),
    ]
    app = FastAPI(title="Supplier Control Panel", lifespan=lifespan, middleware=middleware)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )
    app.state.db = conn
    app.state.webapp_config = app_config
    app.state.job_runner = job_runner

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        user = request.session.get("user")
        if not user:
            return RedirectResponse("/login", status_code=302)
        suppliers = supplier_health_summary(conn, app_config)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            template_context(
                request,
                suppliers=suppliers,
                available_adapters=sorted(ADAPTER_REGISTRY.keys()),
            ),
        )

    @app.get("/connections", response_class=HTMLResponse)
    def connections_page(
        request: Request,
        _: dict[str, str] = Depends(require_user),
    ) -> HTMLResponse:
        suppliers = supplier_health_summary(conn, app_config)
        return templates.TemplateResponse(
            request,
            "connections.html",
            template_context(request, suppliers=suppliers),
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        if request.session.get("user"):
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(
            request,
            "login.html",
            template_context(request, error=""),
        )

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ) -> HTMLResponse:
        user = authenticate_user(conn, username, password)
        if user is None:
            return templates.TemplateResponse(
                request,
                "login.html",
                template_context(request, error="Invalid username or password."),
                status_code=401,
            )
        request.session["user"] = user
        return RedirectResponse("/", status_code=302)

    @app.post("/logout")
    def logout(request: Request, _: dict[str, str] = Depends(require_user)) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(
        request: Request,
        _: dict[str, str] = Depends(require_user),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "settings.html",
            template_context(
                request,
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
            ),
        )

    @app.post("/settings/password")
    def update_password(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        confirm_password: str = Form(...),
    ) -> RedirectResponse:
        user = require_user(request)
        if new_password != confirm_password:
            return RedirectResponse(
                "/settings?error=New+password+confirmation+does+not+match.",
                status_code=302,
            )
        error = change_password(
            conn,
            username=user["username"],
            current_password=current_password,
            new_password=new_password,
        )
        if error:
            return RedirectResponse(f"/settings?error={error.replace(' ', '+')}", status_code=302)
        return RedirectResponse(
            "/settings?notice=Password+updated+successfully.",
            status_code=302,
        )

    @app.get("/suppliers/new", response_class=HTMLResponse)
    def supplier_new_page(
        request: Request,
        _: dict[str, str] = Depends(require_user),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "supplier_form.html",
            template_context(
                request,
                title="Add Supplier",
                supplier=None,
                form_mode="create",
                available_adapters=sorted(ADAPTER_REGISTRY.keys()),
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
            ),
        )

    @app.get("/suppliers/{supplier_slug}", response_class=HTMLResponse)
    def supplier_detail(
        supplier_slug: str,
        request: Request,
        _: dict[str, str] = Depends(require_user),
    ) -> HTMLResponse:
        supplier = supplier_by_slug(supplier_slug, app_config)
        summaries = supplier_health_summary(conn, app_config)
        selected = next(
            (summary for summary in summaries if summary["supplier_slug"] == supplier_slug),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=404, detail="Supplier not found.")
        jobs = list_jobs(conn, supplier_slug=supplier_slug, limit=10)
        return templates.TemplateResponse(
            request,
            "supplier_detail.html",
            template_context(
                request,
                supplier=supplier,
                summary=selected,
                jobs=jobs,
                notice=request.query_params.get("notice", ""),
            ),
        )

    @app.get("/suppliers/{supplier_slug}/edit", response_class=HTMLResponse)
    def supplier_edit_page(
        supplier_slug: str,
        request: Request,
        _: dict[str, str] = Depends(require_user),
    ) -> HTMLResponse:
        supplier = supplier_by_slug(supplier_slug, app_config)
        return templates.TemplateResponse(
            request,
            "supplier_form.html",
            template_context(
                request,
                title=f"Edit {supplier_slug}",
                supplier=supplier,
                form_mode="edit",
                available_adapters=sorted(ADAPTER_REGISTRY.keys()),
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
            ),
        )

    def _save_supplier_and_reload_scheduler(updated_supplier: dict[str, Any], *, create: bool) -> None:
        configs = load_supplier_configs(supplier_config_path)
        if create:
            if any(config.supplier_slug == updated_supplier["supplier_slug"] for config in configs):
                raise ValueError("Supplier slug already exists.")
            configs.append(build_supplier_from_form(**updated_supplier))
        else:
            replaced = False
            new_configs = []
            for config in configs:
                if config.supplier_slug == updated_supplier["supplier_slug"]:
                    new_configs.append(build_supplier_from_form(**updated_supplier))
                    replaced = True
                else:
                    new_configs.append(config)
            if not replaced:
                raise KeyError("Supplier not found.")
            configs = new_configs
        save_supplier_configs(configs, config_path=supplier_config_path)
        job_runner.reload_scheduler(
            enabled=app_config.scheduler_enabled,
            mode=app_config.scheduler_mode,
        )

    @app.post("/suppliers/new")
    def supplier_create(
        request: Request,
        supplier_slug: str = Form(...),
        enabled: str | None = Form(None),
        scraper_adapter: str = Form(...),
        base_url: str = Form(...),
        ybm_token_env_var: str = Form(...),
        output_dir: str = Form(...),
        catalog_update_policy: str = Form("delete_missing"),
        ybm_api_base: str = Form("https://connect.yourbarmate.com/api"),
        schedule_enabled: str | None = Form(None),
        schedule_frequency: str = Form("weekly"),
        schedule_weekday: str = Form("monday"),
        schedule_time: str = Form("03:30"),
        concurrency: str = Form(""),
        min_delay_seconds: str = Form(""),
        max_delay_seconds: str = Form(""),
        _: dict[str, str] = Depends(require_user),
    ) -> RedirectResponse:
        try:
            _save_supplier_and_reload_scheduler(
                {
                    "supplier_slug": supplier_slug,
                    "enabled": parse_bool_form(enabled),
                    "scraper_adapter": scraper_adapter,
                    "base_url": base_url,
                    "ybm_token_env_var": ybm_token_env_var,
                    "output_dir": output_dir,
                    "catalog_update_policy": catalog_update_policy,
                    "ybm_api_base": ybm_api_base,
                    "schedule_enabled": parse_bool_form(schedule_enabled),
                    "schedule_frequency": schedule_frequency,
                    "schedule_weekday": schedule_weekday,
                    "schedule_time": schedule_time,
                    "concurrency": concurrency,
                    "min_delay_seconds": min_delay_seconds,
                    "max_delay_seconds": max_delay_seconds,
                },
                create=True,
            )
        except Exception as exc:
            return RedirectResponse(
                f"/suppliers/new?error={str(exc).replace(' ', '+')}",
                status_code=302,
            )
        return RedirectResponse(
            f"/suppliers/{supplier_slug}?notice=Supplier+created.",
            status_code=302,
        )

    @app.post("/suppliers/{supplier_slug}/edit")
    def supplier_update(
        supplier_slug: str,
        enabled: str | None = Form(None),
        scraper_adapter: str = Form(...),
        base_url: str = Form(...),
        ybm_token_env_var: str = Form(...),
        output_dir: str = Form(...),
        catalog_update_policy: str = Form("delete_missing"),
        ybm_api_base: str = Form("https://connect.yourbarmate.com/api"),
        schedule_enabled: str | None = Form(None),
        schedule_frequency: str = Form("weekly"),
        schedule_weekday: str = Form("monday"),
        schedule_time: str = Form("03:30"),
        concurrency: str = Form(""),
        min_delay_seconds: str = Form(""),
        max_delay_seconds: str = Form(""),
        _: dict[str, str] = Depends(require_user),
    ) -> RedirectResponse:
        try:
            _save_supplier_and_reload_scheduler(
                {
                    "supplier_slug": supplier_slug,
                    "enabled": parse_bool_form(enabled),
                    "scraper_adapter": scraper_adapter,
                    "base_url": base_url,
                    "ybm_token_env_var": ybm_token_env_var,
                    "output_dir": output_dir,
                    "catalog_update_policy": catalog_update_policy,
                    "ybm_api_base": ybm_api_base,
                    "schedule_enabled": parse_bool_form(schedule_enabled),
                    "schedule_frequency": schedule_frequency,
                    "schedule_weekday": schedule_weekday,
                    "schedule_time": schedule_time,
                    "concurrency": concurrency,
                    "min_delay_seconds": min_delay_seconds,
                    "max_delay_seconds": max_delay_seconds,
                },
                create=False,
            )
        except Exception as exc:
            return RedirectResponse(
                f"/suppliers/{supplier_slug}/edit?error={str(exc).replace(' ', '+')}",
                status_code=302,
            )
        return RedirectResponse(
            f"/suppliers/{supplier_slug}?notice=Supplier+updated.",
            status_code=302,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(
        job_id: int,
        request: Request,
        _: dict[str, str] = Depends(require_user),
    ) -> HTMLResponse:
        job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        progress = build_job_progress(job, app_config)
        if progress["log_group"]:
            job["cloudwatch_log_group"] = progress["log_group"]
        if progress["log_stream"]:
            job["cloudwatch_log_stream"] = progress["log_stream"]
        run_summary = read_json_file(Path(job["run_summary_path"])) if job.get("run_summary_path") else {}
        sync_report = read_json_file(Path(job["sync_report_path"])) if job.get("sync_report_path") else {}
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            template_context(
                request,
                job=job,
                scraped_count=progress["scraped_count"],
                errors=progress["errors"],
                run_summary=run_summary,
                sync_report=sync_report,
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
            ),
        )

    @app.post("/jobs/{job_id}/stop")
    def stop_job(
        job_id: int,
        request: Request,
        _: dict[str, str] = Depends(require_user),
    ) -> RedirectResponse:
        try:
            job_runner.stop_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found.")
        except Exception as exc:
            return RedirectResponse(
                f"/jobs/{job_id}?error={str(exc).replace(' ', '+')}",
                status_code=302,
            )
        return RedirectResponse(
            f"/jobs/{job_id}?notice=Stop+requested.",
            status_code=302,
        )

    @app.get("/system", response_class=HTMLResponse)
    def system_page(
        request: Request,
        _: dict[str, str] = Depends(require_user),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "system.html",
            template_context(
                request,
                health=system_health(app_config),
                supplier_count=len(load_supplier_configs(supplier_config_path)),
            ),
        )

    @app.get("/api/health")
    def api_health(_: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
        return system_health(app_config)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/suppliers")
    def api_suppliers(_: dict[str, str] = Depends(require_user)) -> list[dict[str, Any]]:
        return supplier_health_summary(conn, app_config)

    @app.get("/api/suppliers/{supplier_slug}")
    def api_supplier(
        supplier_slug: str, _: dict[str, str] = Depends(require_user)
    ) -> dict[str, Any]:
        summaries = supplier_health_summary(conn, app_config)
        selected = next(
            (summary for summary in summaries if summary["supplier_slug"] == supplier_slug),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=404, detail="Supplier not found.")
        return selected

    def _queue_supplier_job(
        supplier_slug: str,
        job_type: str,
        request: Request,
    ) -> JSONResponse:
        user = require_user(request)
        try:
            supplier_by_slug(supplier_slug, app_config)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Supplier not found.") from exc
        try:
            command = build_job_command(
                supplier_slug,
                job_type,
                env_file=app_config.resolved_env_path(),
            )
            job_id = queue_job(
                conn,
                supplier_slug=supplier_slug,
                job_type=job_type,
                requested_by=user["username"],
                env_file_ref=str(app_config.resolved_env_path()),
                command=command,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"job_id": job_id, "job_type": job_type, "status": "queued"}, status_code=202)

    @app.post("/api/suppliers/{supplier_slug}/jobs/dry-run")
    def api_queue_dry_run(supplier_slug: str, request: Request) -> JSONResponse:
        return _queue_supplier_job(supplier_slug, "scrape_dry_run", request)

    @app.post("/api/suppliers/{supplier_slug}/jobs/scrape")
    def api_queue_scrape_only(supplier_slug: str, request: Request) -> JSONResponse:
        require_user(request)
        raise HTTPException(
            status_code=410,
            detail="Separate Scrape runs have been removed. Use Scrape + Sync instead.",
        )

    @app.post("/api/suppliers/{supplier_slug}/jobs/run-sync")
    def api_queue_run_sync(supplier_slug: str, request: Request) -> JSONResponse:
        return _queue_supplier_job(supplier_slug, "scrape_and_sync", request)

    @app.post("/api/suppliers/{supplier_slug}/jobs/sync-from-export")
    def api_queue_sync_from_export(supplier_slug: str, request: Request) -> JSONResponse:
        require_user(request)
        raise HTTPException(
            status_code=410,
            detail="Separate Sync runs have been removed. Use Scrape + Sync instead.",
        )

    @app.get("/api/jobs")
    def api_jobs(
        supplier_slug: str | None = None,
        _: dict[str, str] = Depends(require_user),
    ) -> list[dict[str, Any]]:
        return list_jobs(conn, supplier_slug=supplier_slug, limit=100)

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int, _: dict[str, str] = Depends(require_user)) -> dict[str, Any]:
        job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job

    @app.get("/api/jobs/{job_id}/logs")
    def api_job_logs(
        job_id: int, _: dict[str, str] = Depends(require_user)
    ) -> dict[str, Any]:
        job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        progress = build_job_progress(job, app_config)
        return {
            "job_id": job_id,
            "scraped_count": progress["scraped_count"],
            "errors": progress["errors"],
            "cloudwatch_log_group": progress["log_group"],
            "cloudwatch_log_stream": progress["log_stream"],
        }

    @app.get("/artifacts")
    def artifact_download(
        path: str,
        _: dict[str, str] = Depends(require_user),
    ) -> FileResponse:
        try:
            resolved = resolve_allowed_artifact(path, app_config)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not resolved.exists():
            raise HTTPException(status_code=404, detail="Artifact not found.")
        return FileResponse(resolved)

    return app
