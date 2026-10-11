"""签到消息（机器人回复和用户的「签到」）按标准延时自动撤回；奖励消息仍不撤回。"""
import time
import unittest

from telegram import Update

from tests import test_group_wizard_save as gw


class CheckinCleanupTest(gw.GroupWizardSaveTest):
    def scheduled(self):
        return set(self.app.bot._delete_by_message)

    async def test_group_checkin_text_and_reply_auto_deleted(self):
        self.assertEqual(self.app.bot.auto_delete_seconds, 180)
        message_id = next(self.message_ids)
        data = {"update_id": next(self.update_ids), "message": {
            "message_id": message_id, "date": int(time.time()),
            "chat": self.chat(), "from": self.user(), "text": "签到",
        }}
        self.request.calls.clear()
        await self.app.process_update(Update.de_json(data, self.app.bot))
        replies = [p for api, p in self.request.calls if api == "sendMessage"]
        self.assertTrue(any("签到成功" in str(p.get("text")) for p in replies), replies)
        scheduled = self.scheduled()
        self.assertIn((gw.GROUP, message_id), scheduled)  # 用户的「签到」
        reply_ids = {(gw.GROUP, int(m)) for m in self._sent_ids()}
        self.assertTrue(reply_ids & scheduled, (reply_ids, scheduled))  # 机器人的签到回复

    async def test_button_checkin_reply_auto_deleted(self):
        self.request.calls.clear()
        await self.tap("points:checkin")
        self.assertTrue(any("签到成功" in t for t in self.texts()), self.texts())
        reply_ids = {(gw.GROUP, int(m)) for m in self._sent_ids()}
        self.assertTrue(reply_ids & self.scheduled())

    def _sent_ids(self):
        # FakeRequest 用递增 id 生成 sendMessage 的 message_id；找回签到回复的 id
        ids = []
        for api, params in self.request.calls:
            if api == "sendMessage" and "签到成功" in str(params.get("text")):
                ids.append(self._reply_id(params))
        return [i for i in ids if i]

    def _reply_id(self, params):
        return self.request.sent_ids.get(id(params))


# FakeRequest 记录每次 sendMessage 返回的 message_id
_orig = gw.FakeRequest.do_request


async def _do_request(self, url, method, request_data=None, **kwargs):
    status, body = await _orig(self, url, method, request_data, **kwargs)
    if url.endswith("/sendMessage"):
        import json
        result = json.loads(body)["result"]
        if not hasattr(self, "sent_ids"):
            self.sent_ids = {}
        self.sent_ids[id(self.calls[-1][1])] = result["message_id"]
    return status, body


class _Recording(gw.FakeRequest):
    do_request = _do_request


for _name in [n for n in dir(gw.GroupWizardSaveTest) if n.startswith("test_")]:
    if _name not in CheckinCleanupTest.__dict__:
        setattr(CheckinCleanupTest, _name, None)

_setup = CheckinCleanupTest.asyncSetUp


async def _asyncSetUp(self):
    await _setup(self)
    self.request.__class__ = _Recording
    self.request.sent_ids = {}


CheckinCleanupTest.asyncSetUp = _asyncSetUp
