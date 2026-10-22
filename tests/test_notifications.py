# Dead FCM tokens are deleted on the first failed send, valid ones are kept.

import core.notifications as notif


class FakeResp:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


DEAD = {"error": {"code": 404, "message": "NotRegistered", "status": "NOT_FOUND",
                  "details": [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                               "errorCode": "UNREGISTERED"}]}}
BAD_PAYLOAD = {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                         "message": "Invalid value at 'message.data[0].value'"}}
BAD_TOKEN = {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                       "message": "The registration token is not a valid FCM registration token"}}


def _run(responses):
    deleted = []
    notif._get_access_token = lambda: "x"
    notif.db.delete_fcm_token = deleted.append
    it = iter(responses)
    notif.requests.post = lambda *a, **k: next(it)
    notif._send_to([f"tok{i}" for i in range(len(responses))], "t", "b")
    return deleted


def test_dead_tokens_deleted_valid_kept():
    deleted = _run([FakeResp(404, DEAD), FakeResp(200, {"name": "ok"}), FakeResp(404, DEAD)])
    assert deleted == ["tok0", "tok2"]


def test_payload_error_does_not_delete_token():
    assert _run([FakeResp(400, BAD_PAYLOAD)]) == []


def test_invalid_token_is_deleted():
    assert _run([FakeResp(400, BAD_TOKEN)]) == ["tok0"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed.")
