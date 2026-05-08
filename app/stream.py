"""SSE-обвязка для UIMessageStream v1.

Только две вещи:
- SSE_HEADERS — заголовки ответа (`x-vercel-ai-ui-message-stream: v1`,
  ровно тот набор, что в node_modules/ai/dist/ui-message-stream-headers).
- sse(obj) -> bytes — сериализатор: `data: {json}\\n\\n`, или
  `data: [DONE]\\n\\n` для строки `"[DONE]"`.

Все типы UIMessageChunk (start/start-step/text-*/tool-*/finish-step/finish)
эмитит сам агент в app/agent.py.
"""
import json


SSE_HEADERS: dict[str, str] = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
    "x-vercel-ai-ui-message-stream": "v1",
}


def sse(obj) -> bytes:
    if obj == "[DONE]":
        return b"data: [DONE]\n\n"
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
