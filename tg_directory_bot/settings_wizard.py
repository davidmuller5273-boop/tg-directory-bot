"""Draft-only settings conversations; existing handlers remain the commit boundary."""

import secrets
import time
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError


@dataclass(frozen=True)
class Question:
    title: str
    hint: str = "请发送本项内容。"
    kind: str = "text"
    minimum: str | None = None
    maximum: str | None = None
    choices: tuple[str, ...] = ()

    def validate(self, text, message):
        if self.kind in {"content", "media"}:
            media = any(getattr(message, key, None) for key in (
                "photo", "video", "animation", "document", "audio", "voice", "sticker", "video_note",
            ))
            if getattr(message, "media_group_id", None):
                raise ValueError("请一次发送一条消息；相册请拆成单张或单个视频后发送。")
            if self.kind == "media" and not media:
                raise ValueError("请发送或转发图片、视频、动画、音频、文件或贴纸。")
            if not media and not text.strip():
                raise ValueError("请发送或转发文字或媒体消息。")
            return
        if not text.strip():
            raise ValueError("本项还没有填写。")
        if self.choices and text not in self.choices:
            raise ValueError("请选择：" + "、".join(self.choices))
        if self.kind == "url" and not text.lower().startswith(("https://", "http://")):
            raise ValueError("请发送以 https:// 或 http:// 开头的完整链接。")
        if self.kind == "id" and not text.lstrip("#").isdigit():
            raise ValueError("请填写数字编号，可带 #。")
        if self.kind == "time" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
            raise ValueError("请填写24小时制时间，例如 09:00 或 23:30。")
        if self.kind in {"number", "integer", "threshold"}:
            if self.kind == "threshold" and text == "关闭":
                return
            try:
                value = Decimal(text)
                if not value.is_finite():
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ValueError("请填写有效数字。") from None
            if self.kind == "integer" and (not text.lstrip("-").isdigit()):
                raise ValueError("请填写整数。")
            if self.minimum is not None and value < Decimal(self.minimum):
                raise ValueError("不能小于 " + self.minimum)
            if self.maximum is not None and value > Decimal(self.maximum):
                raise ValueError("不能大于 " + self.maximum)


def number(title, minimum="0", maximum=None, integer=False, hint="请发送数字。"):
    return Question(title, hint, "integer" if integer else "number", minimum, maximum)


TARGET = Question("成员", "请发送 @用户名或数字ID；没有用户名时使用数字ID。")
CONTENT = Question("发布内容", "请直接发送或转发原消息，保留文字、原表情和媒体。", "content")
AD_CONTENT = Question(
    "发布内容",
    "请直接发送或转发原消息，保留文字、原表情和媒体。"
    "转发带按钮的频道原帖（机器人需在该频道内）可保留按钮。",
    "content",
)
PRIZE = Question("奖品", "请发送奖品名称或说明。")
WINNERS = number("中奖人数", "1", "1000", True)
MINUTES = number("多久后开奖（分钟）", "1", "10080", True)

# Only settings belong here. Searches and member ledgers must not be captured.
FLOWS = {
    "confirm_action": ("操作确认", [Question("是否执行此修改", choices=("确认", "取消"))]),
    "clone_token": ("申请机器人克隆", [Question("机器人Token", "请发送你自己机器人的Token；不会在确认页面显示。", "secret")]),
    "tron_monitor_address": ("添加地址监控", [Question("波场地址", "请输入以 T 开头的完整地址。")]),
    "points_checkin": ("签到积分", [number("最少奖励积分"), number("最多奖励积分"), number("连续3天额外积分")]),
    "points_activity": ("随机活跃积分", [number("最少发言条数", "1", integer=True), number("最多发言条数", "1", integer=True), number("最少奖励积分"), number("最多奖励积分")]),
    "points_giftadd": ("添加积分礼品", [number("兑换所需积分", "0.01"), Question("礼品名称"), number("库存", "-1", integer=True, hint="发送库存数量；-1 表示不限量。")]),
    "points_giftdel": ("删除积分礼品", [Question("礼品编号", "发送要删除的编号，例如 #1。", "id")]),
    "points_adjust": ("增减成员积分", [TARGET, number("增减积分", None, hint="增加发送正数，扣除发送负数，例如 -10。"), Question("调整原因")]),
    "points_clear": ("清零积分", [Question("清零对象", "发送 @用户名、数字ID，或发送“全部”。")]),
    "points_drawcost": ("本次抽奖消耗", [number("本次消耗积分", "0.01", "1000000")]),
    "points_drawmincost": ("积分抽奖最低消耗", [number("最低消耗积分", "0.01", "1000000")]),
    "points_drawconfig": ("积分抽奖", [number("最低消耗积分", "0.01", "1000000"), Question("抽奖开关", choices=("开启", "关闭"))]),
    "points_drawrate": ("积分抽奖中奖倍率", [number("中奖倍率", "0", "5")]),
    "points_diceodds": ("骰子赔率", [Question("赔率", "例如 2000 表示赔率 2.000。")]),
    "points_dicemin": ("骰子最低参与积分", [number("每次最低积分", "0.01", "1000000")]),
    "points_dicemsgmin": ("骰子最低当日活跃", [number("当日最少发言条数", "0", "100000", True, hint="发送整数；0 表示不限制。")]),
    "points_dicemsgfree": ("骰子免定时活跃", [number("当日发言满多少条不受定时限制", "0", "100000", True, hint="发送整数；0 表示关闭。")]),
    "points_dicemax": ("骰子单注上限", [number("单注最高积分", "0", "1000000", hint="发送数字；0 表示不限。")]),
    "points_diceschedule": ("骰子每日定时开关", [
        Question("定时功能", choices=("开启", "关闭")),
        Question("每日开放时间", "请填写24小时制时间，例如 09:00。", "time"),
        Question("每日关闭时间", "请填写24小时制时间，例如 23:30；跨午夜也支持。", "time"),
    ]),
    "group_ad_interval": ("定时广告", [number("发送间隔（分钟）", "1", "43200", True), AD_CONTENT]),
    "group_ad_prefix": ("消息前广告", [AD_CONTENT]),
    "group_ad_suffix": ("消息后广告", [AD_CONTENT]),
    "quickpost_text": ("快捷发布文字", [Question("帖子文字", "直接发送或转发文字，原格式和表情会保留。", "content")]),
    "quickpost_media": ("快捷发布媒体", [Question("帖子媒体", "发送或转发媒体；有说明时一起保存原说明与表情。", "media")]),
    "quickpost_add": ("添加快捷发布消息", [Question("消息名称", "例如：消息2"), CONTENT]),
    "quickpost_button": ("添加快捷发布按钮", [
        Question("按钮文字"),
        Question("按钮链接", "请发送完整的 https:// 链接。", "url"),
        Question("按钮颜色", choices=("默认", "蓝色", "绿色", "红色")),
        Question("按钮类型", "长按钮独占一行；短按钮每行并排两个。", choices=("长按钮", "短按钮")),
    ]),
    "quickpost_buttondel": ("删除快捷发布按钮", [Question("按钮编号", "例如 #3。", "id")]),
    "quickpost_buttonedit": ("修改快捷发布按钮", [
        Question("按钮编号", "例如 #3。", "id"),
        Question("输入内容", "给什么文字和表情，按钮就显示什么。"),
        Question("按钮链接", "请发送完整的 https:// 链接。", "url"),
        Question("按钮颜色", choices=("默认", "蓝色", "绿色", "红色")),
        Question("按钮类型", "长按钮独占一行；短按钮每行并排两个。", choices=("长按钮", "短按钮")),
    ]),
    "quickpost_schedule": ("定时发布", [
        Question("消息编号", "请填写快捷发布消息编号，例如 #2。", "id"),
        Question("发布时间", "请填写北京时间，例如 2026-09-10 20:30。"),
    ]),
    "quickpost_schedulecancel": ("取消定时发布", [
        Question("定时任务编号", "请填写待发布列表中的编号，例如 #3。", "id"),
    ]),
    "channel_add": ("添加频道", [Question("频道", "请发送频道 @用户名、数字ID，或转发一条频道消息。")]),
    "channel_delete": ("删除频道", [Question("频道编号", "例如 #2。", "id")]),
    "channel_groupadd": ("添加频道分组", [Question("分组名称")]),
    "channel_groupdel": ("删除频道分组", [Question("分组编号", "例如 #1。", "id")]),
    "channel_assign": ("频道加入分组", [
        Question("频道编号", "例如 #2。", "id"),
        Question("分组编号", "例如 #1；发送 0 表示移出分组。"),
    ]),
    "channel_messageadd": ("添加频道消息", [Question("消息名称"), CONTENT]),
    "channel_messageedit": ("修改频道消息", [
        Question("消息编号", "例如 #3。", "id"), Question("消息名称"), CONTENT,
    ]),
    "channel_messagedel": ("删除频道消息", [Question("消息编号", "例如 #3。", "id")]),
    "channel_schedule": ("频道定时发送", [
        Question("消息编号", "例如 #3。", "id"),
        Question("发送目标", "填写：全部频道、频道分组 #1 或 频道 #2。"),
        Question("发送时间", "请填写北京时间，例如 2026-09-10 20:30。"),
    ]),
    "group_join_welcome": ("进群欢迎语", [Question("欢迎语", "可以使用 {name} 和 {group} 占位符。")]),
    "invite_expirehours": ("邀请链接有效期", [number("有效小时数", integer=True, hint="发送小时数；0 表示无限制。")]),
    "invite_maxmembers": ("邀请链接人数限制", [number("最大邀请人数", integer=True, hint="发送人数；0 表示无限制。")]),
    "invite_points": ("邀请奖励", [number("每位新人奖励积分", hint="可带两位小数；0 表示不奖励。")]),
    "groupperm_set": ("分配群管理员权限", [TARGET, Question("权限", "用逗号分隔：统计、抽奖、开奖、群投票、广告、积分、欢迎、快捷发布、邀请链接、近期操作、管理处罚、骰子赔率；全部权限填 all。")]),
    "groupperm_reset": ("重置群管理员权限", [TARGET]),
    "admin_manage_add": ("添加管理员", [Question("管理员数字ID", "请发送 Telegram 数字ID。", "id")]),
    "admin_manage_delete": ("删除管理员", [Question("管理员数字ID", kind="id")]),
    "admin_manage_reset": ("重置管理员权限", [Question("管理员数字ID", kind="id")]),
    "admin_manage_permissions": ("分配管理员权限", [Question("管理员数字ID", kind="id"), Question("权限", "用逗号分隔：管理处罚、群组管理、双向客服、统计。")]),
    "custom_button_support": ("双向联系按钮", [Question("按钮文字", "例如：双向联系九爷"), Question("联系姓名", "例如：jiuye"), TARGET]),
    "custom_button_menu": ("自定义按钮", [Question("按钮文字"), Question("回复内容或链接", "发送回复内容，或完整的 https:// 链接。")]),
    "custom_button_delete": ("删除自定义按钮", [Question("按钮编号", "例如 #1。", "id")]),
    "badword_add": ("添加违规关键词", [Question("关键词")]),
    "badword_delete": ("删除违规关键词", [Question("关键词")]),
    "raffle_minutes": ("定时群抽奖", [MINUTES, WINNERS, PRIZE]),
    "raffle_at": ("指定时间群抽奖", [Question("开奖时间", "格式：2026-09-10 20:30（24小时制）。"), WINNERS, PRIZE]),
    "raffle_quick": ("十分钟群抽奖", [WINNERS, PRIZE]),
    "raffle_tiers": ("分档群抽奖", [MINUTES, Question("各档奖品", "每行一档，例如：\n1*一等奖\n2*二等奖")]),
    "raffle_delete": ("删除群抽奖", [Question("抽奖编号", "例如 #1；确认后删除。", "id")]),
    "raffle_active_start": ("活跃群抽奖", [Question("发言统计开始日期", "发送 0 从现在开始，或发送近31天内日期，例如 2026-09-01。"), Question("抽奖方式", choices=("随机", "排名")), number("多久后开奖（分钟）", "1", "44640", True), WINNERS, number("最低发言次数", "0", integer=True, hint="随机抽奖至少1条；排名抽奖填0。"), PRIZE]),
    "raffle_pro": ("样板通用抽奖", [
        Question("活动标题", "例如：春日福利 / @mychannel"),
        Question("规则", "每行一条规则；发送「无」表示不写规则。"),
        Question("开奖时间", "北京时间绝对时间，例如：2026-09-15 21:00"),
        Question("统计开始", "发送「立即」从现在开始；也可填过去的北京时间（用于统计已发言），例如 2026-09-15 12:00。"),
        Question("参与条件", "可组合，示例：频道 @mychannel\n发言 10\n关键词 抽奖\n助推 1\n发送「无」表示不设条件（仍可报名，开奖时标注未达标）。"),
        Question("如何参与", "展示在公告底部的说明文字。"),
        Question("奖品", "单档：中奖人数 | 奖品；或多档每行：1*一等奖\n2*二等奖"),
        Question("每日重复", choices=("否", "是")),
        number(
            "最少参与人数", "0", "100000", True,
            "开奖时参与人数达到该人数才开奖；不足则自动顺延一天（同一时间）直到人数足够。发送 0 表示不限制。",
        ),
    ]),
    "group_poll_question": ("创建群投票", [Question("投票问题", "请发送1-300字的问题。"), Question("投票选项", "每行一个选项，共2-10个。")]),
    "tron_monitor_low": ("监控低余额提醒", [Question("低余额阈值", "低于该余额才提醒；发送“关闭”可停用。", "threshold", "0")]),
    "tron_monitor_high": ("监控高余额提醒", [Question("高余额阈值", "高于该余额才提醒；发送“关闭”可停用。", "threshold", "0")]),
    "tron_monitor_transfer": ("监控交易提醒", [Question("最小交易金额", "小于该金额不提醒；至少0.1；发送“关闭”可停用。", "threshold", "0.1")]),
    "tron_monitor_delete": ("监控消息撤回时间", [number("撤回天数", "0", "30", True, "0 表示不自动撤回。")]),
}


@dataclass
class Draft:
    mode: str
    owner: int
    chat_id: int
    group_id: int | None
    panel_id: int
    nonce: str = field(default_factory=lambda: secrets.token_hex(6))
    step: int = 0
    revision: int = 0
    answers: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)
    busy: bool = False
    failures: int = 0
    action_data: str = ""
    action_title: str = ""
    edit_raffle_id: int | None = None

    def __post_init__(self):
        self.answers = [None] * len(FLOWS[self.mode][1])
        self.messages = [None] * len(self.answers)

    def view(self, error=""):
        title, questions = FLOWS[self.mode]
        lines = ["正在设置：" + (self.action_title or title)]
        if self.group_id:
            lines.append(f"群组ID：{self.group_id}")
        lines.append(f"第 {min(self.step + 1, len(questions))}/{len(questions)} 步" if self.step < len(questions) else "确认设置（尚未保存）")
        for question, answer in zip(questions, self.answers):
            if answer is not None:
                display = "已收到（隐藏）" if question.kind == "secret" else (answer[:120] or "已收到媒体")
                lines.append(f"{question.title}：{display}")
        if error:
            lines.extend(["", error])
        if self.step < len(questions):
            question = questions[self.step]
            lines.extend(["", "请填写：" + question.title, question.hint])
            current = self.answers[self.step]
            if current is not None:
                display = "已收到（隐藏）" if question.kind == "secret" else (str(current)[:200] or "已收到媒体")
                lines.extend([
                    f"当前值：{display}",
                    "点下一步可保留当前值；重新发送可修改",
                ])
        else:
            lines.extend(["", "确认无误后保存；需要修改请返回上一步。"])
        def button(label, action):
            return InlineKeyboardButton(label, callback_data=f"wizard:{self.nonce}:{self.revision}:{action}")
        rows = []
        if self.step < len(questions) and questions[self.step].choices:
            rows.append([button(value, "choice" + str(index)) for index, value in enumerate(questions[self.step].choices)])
        if self.step < len(questions):
            next_label = "保留并下一步" if self.answers[self.step] is not None else "下一步"
            next_action = "next"
        else:
            next_label, next_action = "确认保存", "save"
        rows.append([button("上一步", "back"), button(next_label, next_action)])
        rows.append([button("取消设置", "cancel")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)


class MessageInput:
    def __init__(self, original, text, answers):
        self.original_message = original
        self.text = text
        self.caption = None
        self.setting_answers = answers

    def __getattr__(self, name):
        return getattr(self.original_message, name)

    async def reply_text(self, *args, **kwargs):
        kwargs.setdefault("allow_sending_without_reply", True)
        return await self.original_message.reply_text(*args, **kwargs)


class UpdateInput:
    def __init__(self, original, message):
        self.original = original
        self.effective_message = message
        self.message = message
        self.callback_query = None

    def __getattr__(self, name):
        return getattr(self.original, name)


def input_parts(message, text, maxsplit=-1):
    answers = getattr(message, "setting_answers", None)
    if answers is None:
        return [part.strip() for part in text.split("|", maxsplit)]
    return list(answers)


async def render(context, draft, error=""):
    draft.revision += 1
    draft.touched = time.monotonic()
    text, markup = draft.view(error)
    try:
        await context.bot.edit_message_text(text, chat_id=draft.chat_id, message_id=draft.panel_id, reply_markup=markup, parse_mode=None)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            sent = await context.bot.send_message(draft.chat_id, text, reply_markup=markup, parse_mode=None)
            draft.panel_id = sent.message_id
    if hasattr(context.bot, "schedule_delete"):
        context.bot.schedule_delete(draft.chat_id, draft.panel_id)


async def begin(update, context, group_id):
    mode = context.user_data.get("menu_mode")
    if mode not in FLOWS or not update.callback_query.message:
        return
    draft = Draft(mode, update.effective_user.id, update.effective_chat.id, group_id, update.callback_query.message.message_id)
    prefills = context.user_data.pop("wizard_prefills", None)
    panel = update.callback_query.message
    if isinstance(prefills, (list, tuple)):
        for index, value in enumerate(prefills):
            if index < len(draft.answers) and value is not None:
                draft.answers[index] = value
                draft.messages[index] = panel
    edit_id = context.user_data.get("edit_raffle_id")
    if isinstance(edit_id, int):
        draft.edit_raffle_id = edit_id
    action_title = context.user_data.pop("wizard_action_title", None)
    if action_title:
        draft.action_title = str(action_title)
    context.user_data["settings_draft"] = draft
    await render(context, draft)


async def begin_action(update, context, group_id, title):
    context.user_data["menu_mode"] = "confirm_action"
    draft = Draft("confirm_action", update.effective_user.id, update.effective_chat.id, group_id, update.callback_query.message.message_id)
    draft.action_data = update.callback_query.data
    draft.action_title = title
    context.user_data["settings_draft"] = draft
    await update.callback_query.answer()
    await render(context, draft)


async def begin_from_input(update, context, group_id):
    mode = context.user_data.get("menu_mode")
    if mode not in FLOWS or mode == "confirm_action" or context.user_data.get("settings_draft"):
        return
    draft = Draft(mode, update.effective_user.id, update.effective_chat.id, group_id, 0)
    text, markup = draft.view()
    sent = await update.effective_message.reply_text(text, reply_markup=markup)
    draft.panel_id = sent.message_id
    context.user_data["settings_draft"] = draft


def discard(context):
    context.user_data.pop("settings_draft", None)
    context.user_data.pop("menu_mode", None)
    context.user_data.pop("edit_raffle_id", None)
    context.user_data.pop("wizard_prefills", None)
    context.user_data.pop("wizard_action_title", None)


async def receive(update, context):
    draft = context.user_data.get("settings_draft")
    if not draft or not update.effective_message or update.effective_chat.id != draft.chat_id:
        return False
    if update.effective_user.id != draft.owner:
        return False
    message = update.effective_message
    context.user_data["consumed_group_message"] = message.message_id
    context.user_data["consumed_private_message"] = message.message_id
    if context.user_data.get("menu_mode") != draft.mode or time.monotonic() - draft.touched > 180:
        discard(context)
        await message.reply_text("设置已超时，请重新打开设置。")
        return True
    if draft.busy:
        return True
    draft.busy = True
    try:
        questions = FLOWS[draft.mode][1]
        if draft.step >= len(questions):
            await render(context, draft, "请确认保存，或点上一步修改。")
            return True
        question = questions[draft.step]
        text = message.text if message.text is not None else (message.caption or "")
        value = text if question.kind in {"content", "media"} else text.strip()
        try:
            question.validate(value, message)
        except ValueError as exc:
            draft.failures += 1
            if draft.failures >= 3:
                discard(context)
                await context.bot.edit_message_text(
                    "连续3次输入不符合当前问题，已取消本次设置，未保存。",
                    chat_id=draft.chat_id, message_id=draft.panel_id, reply_markup=None,
                )
                return True
            await render(context, draft, str(exc))
            return True
        draft.failures = 0
        draft.answers[draft.step] = value
        draft.messages[draft.step] = message
        if question.kind == "secret":
            try:
                await message.delete()
            except TelegramError:
                pass
        draft.step += 1
        await render(context, draft)
        if hasattr(context.bot, "schedule_delete"):
            context.bot.schedule_delete(draft.chat_id, message.message_id)
        return True
    finally:
        draft.busy = False


async def callback(update, context, commit):
    query = update.callback_query
    if not str(query.data or "").startswith("wizard:"):
        return False
    draft = context.user_data.get("settings_draft")
    parts = query.data.split(":")
    if (not draft or len(parts) != 4 or parts[1] != draft.nonce
            or parts[2] != str(draft.revision) or update.effective_user.id != draft.owner
            or not query.message or query.message.chat_id != draft.chat_id
            or query.message.message_id != draft.panel_id
            or context.user_data.get("menu_mode") != draft.mode):
        await query.answer("此设置已失效或不属于你，请自行打开设置。", show_alert=True)
        return True
    if time.monotonic() - draft.touched > 180:
        discard(context)
        await query.answer("设置已超时，请重新打开。", show_alert=True)
        return True
    if draft.busy:
        await query.answer("正在处理，请稍候。")
        return True
    draft.touched = time.monotonic()
    draft.busy = True
    try:
        action = parts[3]
        questions = FLOWS[draft.mode][1]
        if action == "cancel":
            discard(context)
            await query.answer()
            await query.edit_message_text("已取消，未保存本次设置。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("返回菜单", callback_data="nav:group" if draft.group_id else "nav:admin")]]))
            return True
        if action == "back":
            if draft.step == 0:
                await query.answer("已经是第一步；退出请点取消设置。")
                return True
            draft.step -= 1
        elif action == "next":
            if draft.step >= len(questions) or draft.answers[draft.step] is None:
                await query.answer("请先回答当前问题。", show_alert=True)
                return True
            draft.step += 1
        elif action.startswith("choice"):
            try:
                choice = questions[draft.step].choices[int(action[6:])]
            except (IndexError, ValueError):
                await query.answer("选项无效。")
                return True
            draft.answers[draft.step] = choice
            draft.messages[draft.step] = query.message
            draft.step += 1
        elif action == "save":
            if draft.step != len(questions) or any(value is None for value in draft.answers):
                await query.answer("请完成所有步骤。", show_alert=True)
                return True
            await query.answer("正在保存。")
            # Remove the live draft before awaiting a write to reject duplicate taps.
            context.user_data.pop("settings_draft", None)
            try:
                await commit(update, context, draft)
            except TelegramError:
                discard(context)
                await context.bot.send_message(draft.chat_id, "消息发送失败，操作可能已保存。请打开原设置检查当前值，勿重复提交。")
                return True
            except ValueError as exc:
                context.user_data["menu_mode"] = draft.mode
                context.user_data["settings_draft"] = draft
                await render(context, draft, f"保存未完成：{exc}。请修改后重试。")
                return True
            if context.user_data.get("menu_mode"):
                context.user_data["menu_mode"] = draft.mode
                context.user_data["settings_draft"] = draft
                await render(context, draft, "未保存，请根据错误提示返回修改。")
            elif not draft.action_data:
                await query.edit_message_text("设置已完成。", reply_markup=None)
            return True
        else:
            await query.answer("操作无效。")
            return True
        await query.answer()
        await render(context, draft)
        return True
    finally:
        draft.busy = False
