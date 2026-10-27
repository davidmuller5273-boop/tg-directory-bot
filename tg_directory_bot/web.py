from __future__ import annotations

import asyncio
import csv
import hmac
import io
import json
import logging
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .backup_utils import create_backup, list_backups
from .chain import ChainQueryError, ChainService
from .config import Config
from .lottery import LOTTERY_GAMES
from .storage import DirectoryStore
from .tron_draw import (
    BEST_MAX_PERIODS, CANONICAL_KEY, DRAW_INTERVAL, best_params, MAX_ROWS, SINGLE, bead_road, config_key, longest_runs,
    ai_trend, lookback_needed, my_trend, parse_limit,
    parse_reference_params, record_next_auto_prediction, reference_panel,
)
from .time_utils import beijing_now_text, format_beijing_time, format_beijing_timestamp_ms
from .validation import parse_submission_payload


def create_app(config: Config) -> FastAPI:
    base_dir = Path(__file__).resolve().parent
    store = DirectoryStore(config.db_path)
    store.init()
    store.ensure_config_admins(
        config.admin_ids, config.super_admin_ids, config.developer_ids
    )
    app = FastAPI(title="TG Directory Admin", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.session_secret or "development-only-secret", same_site="lax")
    app.mount("/static", StaticFiles(directory=base_dir / "static"), name="static")
    templates = Jinja2Templates(directory=base_dir / "templates")
    templates.env.filters["beijing_time"] = format_beijing_time
    templates.env.filters["beijing_timestamp_ms"] = format_beijing_timestamp_ms
    app.state.config = config
    app.state.store = store
    chain = ChainService(config)
    app.state.chain = chain

    @app.middleware("http")
    async def runtime_heartbeat(request: Request, call_next):
        store.heartbeat("web", request.url.path)
        return await call_next(request)

    def csrf(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(24)
            request.session["csrf"] = token
        return token

    def check_csrf(request: Request, token: str) -> None:
        if not hmac.compare_digest(str(request.session.get("csrf", "")), token):
            raise HTTPException(status_code=403, detail="CSRF validation failed")

    def logged_in(request: Request) -> bool:
        return bool(request.session.get("admin"))

    def require_login(request: Request):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        return None

    def flash(request: Request, message: str, kind: str = "success") -> None:
        request.session["flash"] = {"message": message, "kind": kind}

    def render(request: Request, template: str, **context) -> HTMLResponse:
        common = {
            "request": request,
            "csrf": csrf(request),
            "flash": request.session.pop("flash", None),
            "site": store.get_settings(),
            "active": context.pop("active", ""),
        }
        common.update(context)
        return templates.TemplateResponse(request=request, name=template, context=common)

    @app.get("/health")
    async def health():
        return {"status": "ok", "database": str(config.db_path)}

    # --- public TRON 单/双 draw statistics (no login; no admin data) ---
    def tron_draw_payload(
        limit: int, streak_n: int = 3, recent_m: int = 100, mode: str = "follow",
        minority_m: int = 20, mine_limit: int = 0, ai_limit: int = 0, auto_limit: int = 0,
    ) -> dict:
        extra = lookback_needed(streak_n, recent_m, minority_m)
        ai_on = bool(config.tron_ai_enabled)
        ai_limit = ai_limit if ai_on else 0
        all_rows = store.tron_draw_recent(limit + extra + max(mine_limit, ai_limit, auto_limit))
        rows = all_rows[-limit:]
        params = {"streak_n": streak_n, "recent_m": recent_m, "mode": mode, "minority_m": minority_m}
        reference = reference_panel([str(row["result"]) for row in all_rows], limit, **params)
        key = config_key(limit, streak_n, mode, recent_m, minority_m)
        next_block = int(all_rows[-1]["block_number"]) + DRAW_INTERVAL if all_rows else None
        reference["final"]["for_block"] = next_block
        reference["final"]["recorded"] = False
        if next_block and reference["final"]["next"]:
            if key == CANONICAL_KEY:  # default settings: keep the pre-draw history
                store.record_tron_draw_prediction(next_block, reference["final"]["next"], key)
            reference["final"]["recorded"] = next_block in store.tron_draw_predictions(
                next_block, next_block, key,
            )
        previous = None
        if all_rows:
            last_block = int(all_rows[-1]["block_number"])
            stored_last = store.tron_draw_predictions(last_block, last_block, key).get(last_block)
            if stored_last:
                previous = {
                    "block": last_block, "predicted": stored_last,
                    "result": str(all_rows[-1]["result"]),
                    "ok": stored_last == str(all_rows[-1]["result"]),
                }
        mine = None
        if mine_limit > 0 and all_rows:
            blocks = [int(row["block_number"]) for row in all_rows]
            stored = store.tron_draw_predictions(blocks[0], blocks[-1], key)
            mine = my_trend(
                blocks, [str(row["result"]) for row in all_rows], mine_limit, limit,
                params, stored,
            )
        def pre_draw_source(stored: dict, trend_limit: int) -> dict:
            """next pick / hit window / trend / previous of a source whose picks were stored before each draw."""
            out = {"next": None, "window": None, "trend": None, "prev": None}
            if not all_rows:
                return out
            blocks = [int(row["block_number"]) for row in all_rows]
            texts = [str(row["result"]) for row in all_rows]
            if next_block in stored:
                out["next"] = {"block": next_block, "pick": stored[next_block]}
            win_blocks, win_texts = blocks[-limit:], texts[-limit:]
            judged = [stored[b] == r for b, r in zip(win_blocks, win_texts) if b in stored]
            out["window"] = {
                "limit": limit, "hits": sum(judged), "total": len(judged),
                "rate": round(sum(judged) * 100 / len(judged), 1) if judged else None,
            }
            if blocks[-1] in stored:
                out["prev"] = {
                    "block": blocks[-1], "pick": stored[blocks[-1]], "ok": stored[blocks[-1]] == texts[-1],
                }
            if trend_limit > 0:
                out["trend"] = ai_trend(blocks, texts, trend_limit, stored)
            return out

        ai = None
        if ai_on:  # AI reference: the key never appears here, only pick/model/stats
            ai = {"enabled": True, "model": config.ai_model, "next": None, "window": None, "trend": None, "prev": None}
            if all_rows:
                stored_ai = store.tron_ai_predictions(
                    int(all_rows[0]["block_number"]), next_block, config.ai_model,
                )
                ai.update(pre_draw_source(stored_ai, ai_limit))
        ensure_auto_prediction()
        auto = {"enabled": True, "next": None, "window": None, "trend": None, "prev": None, "params": None}
        if all_rows:
            stored_auto = store.tron_auto_predictions(int(all_rows[0]["block_number"]), next_block)
            auto.update(pre_draw_source(stored_auto, auto_limit))
            if auto["next"]:
                try:
                    auto["params"] = json.loads(store.tron_auto_params(next_block) or "null")
                except ValueError:
                    auto["params"] = None
        cells, columns = bead_road([str(row["result"]) for row in rows])
        singles = sum(1 for row in rows if row["result"] == SINGLE)
        latest = rows[-1] if rows else None
        return {
            "limit": limit,
            "count": len(rows),
            "total_recorded": store.tron_draw_total(),
            "counts": {"single": singles, "double": len(rows) - singles},
            "columns": columns,
            "max_rows": MAX_ROWS,
            "cells": cells,
            "blocks": [int(row["block_number"]) for row in rows],
            "ai": ai,
            "auto": auto,
            "reference": reference,
            "runs": longest_runs([str(row["result"]) for row in rows]),
            "previous": previous,
            "my_trend": mine,
            "latest": None if latest is None else int(latest["block_number"]),
            "next_draw_block": None if latest is None else int(latest["block_number"]) + DRAW_INTERVAL,
            "recent": [
                {
                    "block": int(row["block_number"]), "hash": str(row["block_hash"]),
                    "digit": int(row["last_digit"]), "result": str(row["result"]),
                    "ts": int(row["block_timestamp_ms"]),
                    "verified": bool(row["verified"]),
                    "time": format_beijing_timestamp_ms(row["block_timestamp_ms"])
                    if row["block_timestamp_ms"] else "",
                }
                for row in reversed(rows[-20:])
            ],
        }

    def tron_draw_block_detail(block: int, limit: int, params: tuple) -> dict | None:
        """One recorded draw for the page's click-to-inspect panel (+ AI / algorithm picks)."""
        row = store.tron_draw_get(block)
        if row is None:
            return None
        streak_n, recent_m, mode, minority_m = params
        result = str(row["result"])
        algo = store.tron_draw_predictions(
            block, block, config_key(limit, streak_n, mode, recent_m, minority_m),
        ).get(int(block))
        ai_pick = (
            store.tron_ai_predictions(block, block, config.ai_model).get(int(block))
            if config.tron_ai_enabled else None
        )
        auto_pick = store.tron_auto_predictions(block, block).get(int(block))
        return {
            "block": int(row["block_number"]), "hash": str(row["block_hash"]),
            "digit": int(row["last_digit"]), "result": result,
            "ts": int(row["block_timestamp_ms"]),
            "time": format_beijing_timestamp_ms(row["block_timestamp_ms"]) if row["block_timestamp_ms"] else "",
            "verified": bool(row["verified"]),
            "algo": None if algo is None else {"pick": algo, "ok": algo == result},
            "ai": None if ai_pick is None else {"pick": ai_pick, "ok": ai_pick == result, "model": config.ai_model},
            "auto": None if auto_pick is None else {"pick": auto_pick, "ok": auto_pick == result},
        }

    @app.get("/api/tron-draw")
    async def tron_draw_api(
        limit: str = "100", streak_n: str = "", recent_m: str = "", mode: str = "",
        minority_m: str = "", mine_limit: str = "0", ai_limit: str = "0", auto_limit: str = "0", block: str = "",
    ):
        if not config.tron_draw_stats:
            raise HTTPException(404)
        if str(block).strip():  # detail of one draw: /api/tron-draw?block=12345
            detail = tron_draw_block_detail(
                parse_limit(block, 0, 10**12), parse_limit(limit),
                parse_reference_params(streak_n, recent_m, mode, minority_m),
            )
            if detail is None:
                raise HTTPException(404)
            return JSONResponse(detail, headers={"Cache-Control": "no-store"})
        mine = parse_limit(mine_limit, 0, 5000) if str(mine_limit).strip() not in ("", "0") else 0
        ai_n = parse_limit(ai_limit, 0, 5000) if str(ai_limit).strip() not in ("", "0") else 0
        auto_n = parse_limit(auto_limit, 0, 5000) if str(auto_limit).strip() not in ("", "0") else 0
        return JSONResponse(
            tron_draw_payload(
                parse_limit(limit), *parse_reference_params(streak_n, recent_m, mode, minority_m),
                mine_limit=mine, ai_limit=ai_n, auto_limit=auto_n,
            ),
            headers={"Cache-Control": "no-store"},
        )

    auto_lock = threading.Lock()

    def ensure_auto_prediction() -> None:
        """The recorder stores it right after each draw; this covers the gap until it has (never raises)."""
        try:
            newest = store.tron_draw_recent(1)
            if not newest:
                return
            nxt = int(newest[0]["block_number"]) + DRAW_INTERVAL
            if nxt in store.tron_auto_predictions(nxt, nxt):
                return
            with auto_lock:
                record_next_auto_prediction(store)
        except Exception:
            logging.getLogger(__name__).warning("TRON auto-best prediction failed", exc_info=False)

    best_cache: dict = {"key": None, "value": None}
    best_lock = threading.Lock()

    def tron_draw_best_payload() -> dict:
        """Best setting per algorithm; recomputed only when a new draw arrives."""
        latest = store.tron_draw_recent(1)
        key = (int(latest[0]["block_number"]), store.tron_draw_total()) if latest else (0, 0)
        with best_lock:
            if best_cache["key"] != key:
                rows = store.tron_draw_recent(BEST_MAX_PERIODS)
                best_cache["value"] = best_params([str(row["result"]) for row in rows])
                best_cache["value"]["latest"] = key[0]
                best_cache["key"] = key
            return best_cache["value"]

    AI_BROADCAST_WINDOW = 180

    def tron_ai_broadcast_payload() -> dict:
        """Compact feed for the Go bot's AI broadcast (localhost use; pre-draw stored predictions only)."""
        if not config.ai_api_key:
            return {"enabled": False, "reason": "AI_API_KEY 未配置"}
        if not config.tron_ai_predict:
            return {"enabled": False, "reason": "TRON_AI_PREDICT=0 已关闭"}
        model = config.ai_model
        rows = store.tron_draw_recent(AI_BROADCAST_WINDOW)
        latest = int(rows[-1]["block_number"]) if rows else 0
        next_block = latest + DRAW_INTERVAL if latest else 0
        next_pick = store.tron_ai_predictions(next_block, next_block, model).get(next_block) if next_block else None
        window = store.tron_ai_scored(model, low=int(rows[0]["block_number"])) if rows else []
        hits = sum(1 for _, pick, result in window if pick == result)
        kind, count = "none", 0
        for _, pick, result in reversed(window):  # newest first; periods without a prediction are absent
            win = pick == result
            if count and (kind == "win") != win:
                break
            kind, count = ("win" if win else "loss"), count + 1
        overall_hits, overall_total = store.tron_ai_overall(model)
        return {
            "enabled": True,
            "reason": "",
            "latest_block": latest,
            "latest_ts": int(rows[-1]["block_timestamp_ms"] or 0) if rows else 0,
            "next_block": next_block,
            "next_pick": next_pick,
            "recent": [
                {"block": block, "result": result, "pick": pick}
                for block, pick, result in store.tron_ai_scored(model, limit=10)
            ],
            "window": {"periods": AI_BROADCAST_WINDOW, "hits": hits, "total": len(window)},
            "overall": {"hits": overall_hits, "total": overall_total},
            "streak": {"kind": kind, "count": count},
        }

    @app.get("/api/tron-draw/ai-broadcast")
    async def tron_draw_ai_broadcast_api():
        if not config.tron_draw_stats:
            raise HTTPException(404)
        return JSONResponse(
            await asyncio.to_thread(tron_ai_broadcast_payload), headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/tron-draw/best")
    async def tron_draw_best_api():
        if not config.tron_draw_stats:
            raise HTTPException(404)
        return JSONResponse(
            await asyncio.to_thread(tron_draw_best_payload), headers={"Cache-Control": "no-store"},
        )

    @app.get("/tron-draw", response_class=HTMLResponse)
    async def tron_draw_page(request: Request):
        if not config.tron_draw_stats:
            raise HTTPException(404)
        return templates.TemplateResponse(
            request=request, name="tron_draw.html", context={"request": request},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if logged_in(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html")

    @app.post("/login")
    async def login(request: Request, username: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
        check_csrf(request, csrf_token)
        if hmac.compare_digest(username, config.admin_username) and hmac.compare_digest(password, config.admin_password):
            request.session.clear()
            request.session["admin"] = username
            request.session["csrf"] = secrets.token_urlsafe(24)
            store.audit(f"web:{username}", "auth.login")
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", error="用户名或密码错误")

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form(...)):
        check_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(
            request, "dashboard.html", active="dashboard", stats=store.dashboard_stats(),
            pending=store.list_entries(status="pending", limit=8),
            conversations=store.support_conversations(limit=6),
        )

    @app.get("/entries", response_class=HTMLResponse)
    async def entries(request: Request, status: str = "", q: str = "", page: int = 1):
        if redirect := require_login(request):
            return redirect
        page = max(1, page)
        selected = status if status in {"pending", "approved", "rejected", "removed"} else None
        return render(
            request, "entries.html", active="entries",
            entries=store.list_entries(status=selected, query=q, limit=30, offset=(page - 1) * 30),
            total=store.count_entries(selected, q), selected_status=status, q=q, page=page,
        )

    @app.get("/entries/{entry_id}", response_class=HTMLResponse)
    async def entry_edit(request: Request, entry_id: int):
        if redirect := require_login(request):
            return redirect
        entry = store.get(entry_id)
        if not entry:
            raise HTTPException(404)
        return render(request, "entry_edit.html", active="entries", entry=entry, categories=config.categories)

    @app.post("/entries/{entry_id}/update")
    async def entry_update(
        request: Request, entry_id: int, title: str = Form(...), url: str = Form(...),
        category: str = Form(...), description: str = Form(""),
        content_text: str = Form(""), csrf_token: str = Form(...),
    ):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        try:
            entry = store.get(entry_id)
            if not entry:
                raise ValueError("没有找到这条收录")
            if entry.url.startswith("tgcontent://") or entry.media_file_id:
                if category not in config.categories:
                    raise ValueError("无效分类")
                store.update_rich_entry(entry_id, title, content_text, category, description)
            else:
                submission = parse_submission_payload(
                    f"{url} | {title} | {category} | {description}", config.categories
                )
                store.update_entry(
                    entry_id, submission.title, submission.url,
                    submission.category, submission.description,
                )
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(f"/entries/{entry_id}", status_code=303)
        store.audit(f"web:{request.session['admin']}", "entry.update", str(entry_id))
        flash(request, "收录内容已保存")
        return RedirectResponse(f"/entries/{entry_id}", status_code=303)

    @app.post("/entries/{entry_id}/status")
    async def entry_status(
        request: Request, entry_id: int, status: str = Form(...), reason: str = Form(""), csrf_token: str = Form(...)
    ):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        if status not in {"pending", "approved", "rejected", "removed"}:
            raise HTTPException(400, "invalid status")
        entry = store.get(entry_id)
        if not entry:
            raise HTTPException(404)
        store.update_status(entry_id, status, reason)
        label = {"approved": "已通过", "rejected": "已拒绝", "removed": "已下架", "pending": "待审核"}[status]
        store.queue_message(entry.user_id, f"你的提交 #{entry_id} {label}。" + (f"\n原因：{reason}" if reason else ""))
        store.audit(f"web:{request.session['admin']}", f"entry.{status}", str(entry_id), reason)
        flash(request, f"#{entry_id} 状态已更新")
        return RedirectResponse(request.headers.get("referer", "/entries"), status_code=303)

    @app.get("/entries-export.csv")
    async def entries_export(request: Request):
        if redirect := require_login(request):
            return redirect
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "id", "title", "url", "content_text", "media_type", "media_name",
            "category", "status", "user_id", "username", "created_at",
        ])
        for entry in store.list_entries(status=None, limit=100000):
            writer.writerow([
                entry.id, entry.title, entry.url, entry.content_text,
                entry.media_type, entry.media_name, entry.category, entry.status,
                entry.user_id, entry.username, format_beijing_time(entry.created_at),
            ])
        return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=entries.csv"})

    @app.get("/users", response_class=HTMLResponse)
    async def users(request: Request, q: str = ""):
        if redirect := require_login(request):
            return redirect
        return render(request, "users.html", active="users", users=store.list_users(q), q=q)

    @app.post("/users/{user_id}/block")
    async def block_user(request: Request, user_id: int, blocked: int = Form(...), csrf_token: str = Form(...)):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        store.set_user_blocked(user_id, bool(blocked))
        store.audit(f"web:{request.session['admin']}", "user.block" if blocked else "user.unblock", str(user_id))
        flash(request, "用户状态已更新")
        return RedirectResponse("/users", status_code=303)

    @app.get("/admins", response_class=HTMLResponse)
    async def admins(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(
            request,
            "admins.html",
            active="admins",
            admins=store.list_bot_admins(),
            config_admin_ids=config.admin_ids,
            super_admin_ids=config.super_admin_ids,
        )

    @app.post("/admins")
    async def admin_create(request: Request, user_id: int = Form(...), csrf_token: str = Form(...)):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        store.add_bot_admin(user_id, "admin", 0)
        store.audit(f"web:{request.session['admin']}", "admin.add", str(user_id))
        flash(request, f"管理员 {user_id} 已添加")
        return RedirectResponse("/admins", status_code=303)

    @app.post("/admins/{user_id}/delete")
    async def admin_delete(request: Request, user_id: int, csrf_token: str = Form(...)):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        if user_id in config.super_admin_ids:
            flash(request, "不能删除主超级管理员", "error")
            return RedirectResponse("/admins", status_code=303)
        if user_id in config.admin_ids:
            flash(request, "该管理员写在 .env 的 ADMIN_IDS 中，需要先改 .env 再重启", "error")
            return RedirectResponse("/admins", status_code=303)
        if not store.remove_bot_admin(user_id):
            raise HTTPException(404)
        store.audit(f"web:{request.session['admin']}", "admin.delete", str(user_id))
        flash(request, f"管理员 {user_id} 已删除")
        return RedirectResponse("/admins", status_code=303)

    @app.get("/support", response_class=HTMLResponse)
    async def support(request: Request, user_id: int | None = None):
        if redirect := require_login(request):
            return redirect
        thread = store.support_thread(user_id) if user_id else []
        return render(
            request, "support.html", active="support", conversations=store.support_conversations(),
            selected_user=user_id, thread=thread,
        )

    @app.post("/support/{user_id}/reply")
    async def support_reply(request: Request, user_id: int, body: str = Form(...), csrf_token: str = Form(...)):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        if not body.strip():
            raise HTTPException(400, "empty reply")
        store.queue_support_reply(user_id, body.strip(), request.session["admin"])
        store.audit(f"web:{request.session['admin']}", "support.reply", str(user_id))
        flash(request, "回复已进入发送队列")
        return RedirectResponse(f"/support?user_id={user_id}", status_code=303)

    @app.get("/auto-replies", response_class=HTMLResponse)
    async def auto_replies(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(
            request, "auto_replies.html", active="auto_replies", rows=store.list_auto_replies()
        )

    @app.post("/auto-replies")
    async def auto_reply_create(
        request: Request, keyword: str = Form(...), reply_text: str = Form(...),
        match_mode: str = Form("contains"), scope: str = Form("private"), csrf_token: str = Form(...),
    ):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        try:
            reply_id = store.create_auto_reply(keyword, reply_text, match_mode, scope)
        except ValueError as exc:
            flash(request, str(exc), "error")
        else:
            store.audit(f"web:{request.session['admin']}", "auto_reply.create", str(reply_id), keyword)
            flash(request, "自动回复规则已创建")
        return RedirectResponse("/auto-replies", status_code=303)

    @app.post("/auto-replies/{reply_id}/toggle")
    async def auto_reply_toggle(
        request: Request, reply_id: int, enabled: int = Form(...), csrf_token: str = Form(...)
    ):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        if enabled not in {0, 1} or not store.set_auto_reply_enabled(reply_id, bool(enabled)):
            raise HTTPException(404)
        store.audit(f"web:{request.session['admin']}", "auto_reply.toggle", str(reply_id), str(enabled))
        flash(request, "自动回复状态已更新")
        return RedirectResponse("/auto-replies", status_code=303)

    @app.post("/auto-replies/{reply_id}/delete")
    async def auto_reply_delete(request: Request, reply_id: int, csrf_token: str = Form(...)):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        if not store.delete_auto_reply(reply_id):
            raise HTTPException(404)
        store.audit(f"web:{request.session['admin']}", "auto_reply.delete", str(reply_id))
        flash(request, "自动回复规则已删除")
        return RedirectResponse("/auto-replies", status_code=303)

    @app.get("/broadcasts", response_class=HTMLResponse)
    async def broadcasts(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(request, "broadcasts.html", active="broadcasts", broadcasts=store.list_broadcasts())

    @app.post("/broadcasts")
    async def broadcast_create(
        request: Request, title: str = Form(...), body: str = Form(...), confirm: str = Form(""),
        csrf_token: str = Form(...),
    ):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        if confirm != "yes" or not title.strip() or not body.strip():
            flash(request, "请填写内容并确认发送", "error")
            return RedirectResponse("/broadcasts", status_code=303)
        broadcast_id = store.create_broadcast(title.strip(), body.strip())
        store.audit(f"web:{request.session['admin']}", "broadcast.create", str(broadcast_id), title)
        flash(request, f"群发 #{broadcast_id} 已进入队列")
        return RedirectResponse("/broadcasts", status_code=303)

    @app.get("/reports", response_class=HTMLResponse)
    async def reports(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(request, "reports.html", active="reports", reports=store.list_reports())

    @app.get("/groups", response_class=HTMLResponse)
    async def groups(
        request: Request, chat_id: int | None = None, speaker_page: int = 1
    ):
        if redirect := require_login(request):
            return redirect
        selected = store.group_stats(chat_id) if chat_id is not None else None
        speaker_total = store.count_group_speakers(chat_id) if chat_id is not None else 0
        speaker_pages = max(1, (speaker_total + 9) // 10)
        speaker_page = min(max(1, speaker_page), speaker_pages)
        return render(
            request,
            "groups.html",
            active="groups",
            groups=store.list_groups(),
            selected=selected,
            daily=store.group_daily(chat_id) if chat_id is not None else [],
            speakers=store.group_speaker_stats(
                chat_id, limit=10, offset=(speaker_page - 1) * 10
            ) if chat_id is not None else [],
            speaker_total=speaker_total,
            speaker_page=speaker_page,
            speaker_pages=speaker_pages,
            raffles=store.list_raffles(chat_id, limit=50) if chat_id is not None else store.list_raffles(limit=50),
        )

    @app.post("/raffles/{raffle_id}/cancel")
    async def raffle_cancel(request: Request, raffle_id: int, csrf_token: str = Form(...)):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        raffle = store.get_raffle(raffle_id)
        if not raffle:
            raise HTTPException(404)
        if store.cancel_raffle(raffle_id):
            store.queue_message(
                int(raffle["chat_id"]), f"群抽奖 #{raffle_id} 已由管理员从后台取消。", kind="group"
            )
            store.audit(f"web:{request.session['admin']}", "raffle.cancel", str(raffle_id))
            flash(request, f"抽奖 #{raffle_id} 已取消")
        else:
            flash(request, "抽奖已结束或已取消", "error")
        return RedirectResponse(request.headers.get("referer", "/groups"), status_code=303)

    @app.post("/raffles/{raffle_id}/draw")
    async def raffle_draw(request: Request, raffle_id: int, csrf_token: str = Form(...)):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        if store.expire_raffle(raffle_id):
            store.audit(f"web:{request.session['admin']}", "raffle.expire", str(raffle_id))
            flash(request, f"抽奖 #{raffle_id} 将在 15 秒内开奖")
        else:
            flash(request, "抽奖已结束或已取消", "error")
        return RedirectResponse(request.headers.get("referer", "/groups"), status_code=303)

    @app.get("/monitor", response_class=HTMLResponse)
    async def monitor(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(
            request,
            "monitor.html",
            active="monitor",
            stats=store.monitoring_stats(),
            services=store.runtime_status(),
            groups=store.list_groups(limit=10),
        )

    @app.get("/lottery", response_class=HTMLResponse)
    async def lottery_page(request: Request):
        if redirect := require_login(request):
            return redirect
        selector_labels = {
            "all": "全部彩种", "cwl": "全部福彩", "sport": "全部体彩",
            "marksix": "全部六合彩",
            **{code: game.name for code, game in LOTTERY_GAMES.items()},
        }
        return render(
            request,
            "lottery.html",
            active="lottery",
            results=store.latest_lottery_results(),
            subscriptions=store.lottery_subscriptions(),
            sources=store.lottery_source_status(),
            source_labels={
                "cwl": "中国福彩网", "sport": "中国体彩网",
                "hkjc": "香港赛马会官方",
                "marksix6": "第三方 Marksix6（非澳门官方）",
            },
            selector_labels=selector_labels,
            poll_seconds=config.lottery_poll_seconds,
        )

    @app.post("/lottery/subscriptions/delete")
    async def lottery_subscription_delete(
        request: Request, chat_id: int = Form(...), selector: str = Form(...),
        csrf_token: str = Form(...),
    ):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        if not store.remove_lottery_subscription(chat_id, selector):
            raise HTTPException(404)
        store.audit(
            f"web:{request.session['admin']}", "lottery.unsubscribe", str(chat_id), selector
        )
        flash(request, "开奖订阅已删除")
        return RedirectResponse("/lottery", status_code=303)

    async def chain_page_context() -> dict:
        try:
            buy_quotes = await chain.okx_p2p_quotes("buy")
            sell_quotes = await chain.okx_p2p_quotes("sell")
            quote_error = ""
        except ChainQueryError as exc:
            buy_quotes, sell_quotes, quote_error = [], [], str(exc)
        return {
            "buy_quotes": buy_quotes,
            "sell_quotes": sell_quotes,
            "quote_error": quote_error,
            "quote_time": beijing_now_text(),
            "queries": store.list_chain_queries(),
        }

    @app.get("/chain", response_class=HTMLResponse)
    async def chain_page(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(request, "chain.html", active="chain", balance=None, balance_error="", **await chain_page_context())

    @app.post("/chain/balance", response_class=HTMLResponse)
    async def chain_balance(request: Request, address: str = Form(...), csrf_token: str = Form(...)):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        try:
            result = await chain.tron_balance(address)
            error = ""
            store.add_chain_query(0, result.address, "admin_balance", f"TRX={result.trx:f}, USDT={result.usdt:f}")
        except (ValueError, ChainQueryError) as exc:
            result, error = None, str(exc)
            store.add_chain_query(0, address, "admin_balance", error, False)
        return render(
            request, "chain.html", active="chain", balance=result, balance_error=error,
            **await chain_page_context(),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(
            request, "settings.html", active="settings", settings=store.get_settings(),
            moderation_keywords=store.list_moderation_keywords(),
        )

    @app.get("/search-stats", response_class=HTMLResponse)
    async def search_stats(
        request: Request, keyword_page: int = 1, event_page: int = 1
    ):
        if redirect := require_login(request):
            return redirect
        keyword_total = store.count_search_keywords()
        event_total = store.count_search_events()
        keyword_pages = max(1, (keyword_total + 9) // 10)
        event_pages = max(1, (event_total + 9) // 10)
        keyword_page = min(max(1, keyword_page), keyword_pages)
        event_page = min(max(1, event_page), event_pages)
        return render(
            request,
            "search_stats.html",
            active="search_stats",
            keyword_rows=store.search_keyword_rankings(10, (keyword_page - 1) * 10),
            event_rows=store.list_search_events(10, (event_page - 1) * 10),
            keyword_total=keyword_total,
            event_total=event_total,
            keyword_page=keyword_page,
            event_page=event_page,
            keyword_pages=keyword_pages,
            event_pages=event_pages,
        )

    @app.post("/settings")
    async def settings_update(
        request: Request, site_name: str = Form(...), welcome_text: str = Form(...),
        not_found_text: str = Form(...), support_ack_text: str = Form(...),
        maintenance_mode: str = Form("0"), support_enabled: str = Form("0"), csrf_token: str = Form(...),
        chain_enabled: str = Form("0"), rate_enabled: str = Form("0"),
        auto_reply_enabled: str = Form("0"),
        lottery_enabled: str = Form("0"), lottery_broadcast_enabled: str = Form("0"),
        group_keyword_enabled: str = Form("0"), group_directory_trigger: str = Form("地址"),
        group_rate_trigger: str = Form("z0"),
        group_monitoring_enabled: str = Form("0"), group_moderation_enabled: str = Form("0"),
    ):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        for key, value in {
            "site_name": site_name.strip(), "welcome_text": welcome_text.strip(),
            "not_found_text": not_found_text.strip(), "support_ack_text": support_ack_text.strip(),
            "maintenance_mode": "1" if maintenance_mode == "1" else "0",
            "support_enabled": "1" if support_enabled == "1" else "0",
            "chain_enabled": "1" if chain_enabled == "1" else "0",
            "rate_enabled": "1" if rate_enabled == "1" else "0",
            "auto_reply_enabled": "1" if auto_reply_enabled == "1" else "0",
            "lottery_enabled": "1" if lottery_enabled == "1" else "0",
            "lottery_broadcast_enabled": "1" if lottery_broadcast_enabled == "1" else "0",
            "group_keyword_enabled": "1" if group_keyword_enabled == "1" else "0",
            "group_directory_trigger": group_directory_trigger.strip(),
            "group_rate_trigger": group_rate_trigger.strip(),
            "group_monitoring_enabled": "1" if group_monitoring_enabled == "1" else "0",
            "group_moderation_enabled": "1" if group_moderation_enabled == "1" else "0",
        }.items():
            store.set_setting(key, value)
        store.audit(f"web:{request.session['admin']}", "settings.update")
        flash(request, "设置已保存")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/moderation-keywords")
    async def moderation_keyword_create(
        request: Request, keyword: str = Form(...), csrf_token: str = Form(...)
    ):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        try:
            keyword_id = store.add_moderation_keyword(keyword, 0)
        except ValueError as exc:
            flash(request, str(exc), "error")
        else:
            store.audit(
                f"web:{request.session['admin']}", "moderation_keyword.add",
                str(keyword_id), keyword,
            )
            flash(request, "违规关键词已添加")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/moderation-keywords/{keyword_id}/delete")
    async def moderation_keyword_delete(
        request: Request, keyword_id: int, csrf_token: str = Form(...)
    ):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        removed = store.remove_moderation_keyword(keyword_id)
        if removed:
            store.audit(
                f"web:{request.session['admin']}", "moderation_keyword.delete",
                str(keyword_id),
            )
            flash(request, "违规关键词已删除")
        else:
            flash(request, "没有找到该违规关键词", "error")
        return RedirectResponse("/settings", status_code=303)

    @app.get("/backups", response_class=HTMLResponse)
    async def backups(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(request, "backups.html", active="backups", backups=list_backups(config.backups_dir))

    @app.post("/backups")
    async def backup_create(request: Request, csrf_token: str = Form(...)):
        if redirect := require_login(request):
            return redirect
        check_csrf(request, csrf_token)
        backup, checksum = create_backup(config.db_path, config.backups_dir)
        store.audit(f"web:{request.session['admin']}", "backup.create", backup.name, checksum)
        flash(request, f"备份 {backup.name} 已创建")
        return RedirectResponse("/backups", status_code=303)

    @app.get("/backups/{name}")
    async def backup_download(request: Request, name: str):
        if redirect := require_login(request):
            return redirect
        if Path(name).name != name or not name.endswith(".sqlite3"):
            raise HTTPException(400)
        path = config.backups_dir / name
        if not path.is_file():
            raise HTTPException(404)
        store.audit(f"web:{request.session['admin']}", "backup.download", name)
        return FileResponse(path, filename=name, media_type="application/vnd.sqlite3")

    @app.get("/audit", response_class=HTMLResponse)
    async def audit(request: Request):
        if redirect := require_login(request):
            return redirect
        return render(request, "audit.html", active="audit", rows=store.list_audit())

    return app
