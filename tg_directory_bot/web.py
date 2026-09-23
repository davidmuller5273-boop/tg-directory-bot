from __future__ import annotations

import csv
import hmac
import io
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .backup_utils import create_backup, list_backups
from .chain import ChainQueryError, ChainService
from .config import Config
from .lottery import LOTTERY_GAMES
from .storage import DirectoryStore
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
