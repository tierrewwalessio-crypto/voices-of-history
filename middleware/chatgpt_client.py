"""ChatGPT plan provider using the documented OAuth Responses API route."""

import json

import requests

from chatgpt_auth import ChatGPTAuth, ChatGPTAuthError, RESOURCE
from profiles import model_instructions, request_instructions, TFR
from safe_actions import response_schema


class ChatGPTPlanClient:
    def __init__(self, cfg):
        settings = cfg.get("chatgpt_plan", {})
        self.auth = ChatGPTAuth()
        self.model = settings.get("model", "").strip()
        self.timeout = settings.get("timeout_sec", 120)
        self.base_url = RESOURCE
        self.profile_instructions = model_instructions(cfg)
        self.cfg = cfg
        self.response_schema = response_schema(cfg.get("game_profile", "vanilla"),
                                               proposals=cfg.get('proposals', {}).get('enabled', False)) if cfg.get("unified") else None

    def heartbeat(self):
        try:
            self.auth.get_access_token()
            return True
        except (ChatGPTAuthError, requests.RequestException):
            return False

    def _model(self, token):
        if self.model:
            return self.model
        response = requests.get(RESOURCE + "/models",
                                headers={"Authorization": "Bearer " + token}, timeout=30)
        if not response.ok:
            raise ChatGPTAuthError(f"ChatGPT model list failed (HTTP {response.status_code}).")
        models = [m.get("slug") for m in response.json().get("models", [])
                  if m.get("visibility") == "list" and m.get("slug")]
        if not models:
            raise ChatGPTAuthError("No ChatGPT plan models are available for this account.")
        self.model = models[0]
        return self.model

    def chat(self, messages):
        """Return assistant text only after a response.completed stream event."""
        token = self.auth.get_access_token()
        instructions = []
        inputs = []
        for message in messages:
            role = message.get("role")
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role in ("system", "developer"):
                instructions.append(content)
            elif role in ("user", "assistant"):
                inputs.append({"role": role, "content": content})
        self.last_usage = None
        profile_instructions = request_instructions(self.cfg)
        if profile_instructions:
            instructions.append(profile_instructions)
        if not inputs:
            raise ValueError("A ChatGPT request needs at least one user or assistant message.")
        body = {"model": self._model(token), "input": inputs,
                "store": False, "stream": True}
        if getattr(self, "output_limit", None):
            body["max_output_tokens"] = self.output_limit
        if self.cfg.get("unified") and body["model"] == "gpt-6.1-sol":
            body["reasoning"] = {"effort": "low"}
        if self.response_schema is not None:
            body["text"] = {"format": {"type": "json_schema", "name": "voices_turn",
                                       "strict": True, "schema": self.response_schema}}
        if instructions:
            body["instructions"] = "\n\n".join(instructions)
        headers = {"Authorization": "Bearer " + token,
                   "Content-Type": "application/json"}
        with requests.post(RESOURCE + "/responses", json=body, headers=headers,
                           stream=True, timeout=self.timeout) as response:
            if not response.ok:
                raise ChatGPTAuthError(f"ChatGPT response request failed (HTTP {response.status_code}).")
            parts = []
            completed = False
            final_text = ""
            for line in response.iter_lines(decode_unicode=True):
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].lstrip()
                if payload == "[DONE]":
                    break
                event = json.loads(payload)
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    parts.append(event.get("delta", ""))
                elif kind in ("response.failed", "response.incomplete"):
                    self.last_usage = event.get("response", {}).get("usage")
                    detail = event.get("response", {}).get("error") or {}
                    code = detail.get("code", kind) if isinstance(detail, dict) else kind
                    raise ChatGPTAuthError(f"ChatGPT response did not complete ({code}).")
                elif kind == "response.completed":
                    completed = True
                    self.last_usage = event.get("response", {}).get("usage")
                    for item in event.get("response", {}).get("output", []):
                        for content in item.get("content", []):
                            if content.get("type") == "output_text":
                                final_text += content.get("text", "")
            if not completed:
                raise ChatGPTAuthError("ChatGPT stream ended before response.completed.")
            return "".join(parts) or final_text
