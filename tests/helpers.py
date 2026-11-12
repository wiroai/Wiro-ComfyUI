"""Fakes shared by the tests."""


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


class FakeSession:
    """Records every POST and answers from a list of (path suffix, body) rules."""

    def __init__(self, routes=None):
        self.calls = []
        self.routes = routes or {}

    def post(self, url, json=None, data=None, files=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "data": data, "files": files, "headers": headers})
        for suffix, answer in self.routes.items():
            if url.endswith(suffix):
                if callable(answer):
                    answer = answer(self.calls[-1])
                if isinstance(answer, Exception):
                    raise answer
                if isinstance(answer, FakeResponse):
                    return answer
                return FakeResponse(answer)
        return FakeResponse({"result": False, "errors": [{"code": 1, "message": f"no route for {url}"}]})

    def paths(self):
        return [c["url"].split("/v1/", 1)[-1] for c in self.calls]


def blocked(value):
    """True for an output a node did not produce: None outside ComfyUI,
    an ExecutionBlocker inside it."""
    return value is None or type(value).__name__ == "ExecutionBlocker"
