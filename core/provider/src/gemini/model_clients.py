"""
Google Gemini model clients using native google.genai SDK.
Supports LLM (with tool calling & thinking), Embedding, and Image generation.
"""

import json
import time
import uuid
from typing import Optional

from google import genai
from google.genai import types

from core.provider import ModelInfo
from core.provider import LLMModelClient, EmbeddingModelClient, ImageModelClient
from core.provider.llm_model import LLMRequest, LLMResponse
from core.provider.image_result import ImageResult
from core.logging_manager import get_logger

logger = get_logger("provider.gemini", "purple")


def _build_client(model: ModelInfo) -> genai.Client:
    """Create a google.genai Client from provider config.

    Supports custom base_url for proxy/mirror endpoints (useful in China).
    """
    api_key = model.provider_config.get("api_key", "")
    base_url = model.provider_config.get("base_url", "")

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["http_options"] = types.HttpOptions(base_url=base_url)

    return genai.Client(**kwargs)


# ─── OpenAI format → Gemini format converters ────────────────────────────────

def _convert_messages(messages: list[dict]) -> tuple[Optional[str], list[types.Content]]:
    """
    Convert OpenAI-format messages to Gemini Contents.
    Returns (system_instruction, contents).
    """
    system_instruction = None
    contents: list[types.Content] = []

    for msg in messages:
        role = msg.get("role", "user")
        raw_content = msg.get("content", "")

        # System message → system_instruction (Gemini uses separate param)
        if role == "system":
            if isinstance(raw_content, str):
                system_instruction = raw_content
            elif isinstance(raw_content, list):
                # Multi-part system prompt: extract text parts
                texts = [p.get("text", "") for p in raw_content if p.get("type") == "text"]
                system_instruction = "\n".join(texts)
            continue

        # Map roles: OpenAI → Gemini
        gemini_role = "model" if role == "assistant" else "user"

        # Tool results → user role with function_response parts
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            func_name = msg.get("name", tool_call_id)
            result_str = msg.get("content", "{}")
            try:
                result_dict = json.loads(result_str)
            except (json.JSONDecodeError, TypeError):
                result_dict = {"result": result_str}

            part = types.Part.from_function_response(
                name=func_name,
                response=result_dict
            )
            contents.append(types.Content(role="user", parts=[part]))
            continue

        # Build parts
        parts = []

        if isinstance(raw_content, str):
            if raw_content:
                parts.append(types.Part.from_text(text=raw_content))
        elif isinstance(raw_content, list):
            # Multi-modal content (text + images)
            for item in raw_content:
                item_type = item.get("type", "text")
                if item_type == "text":
                    text = item.get("text", "")
                    if text:
                        parts.append(types.Part.from_text(text=text))
                elif item_type == "image_url":
                    image_url = item.get("image_url", {}).get("url", "")
                    if image_url.startswith("data:"):
                        # Base64 inline image
                        # Format: data:image/TYPE;base64,DATA
                        try:
                            header, b64_data = image_url.split(",", 1)
                            mime_type = header.split(":")[1].split(";")[0]
                            import base64
                            image_bytes = base64.b64decode(b64_data)
                            parts.append(types.Part.from_bytes(
                                data=image_bytes,
                                mime_type=mime_type
                            ))
                        except Exception as e:
                            logger.warning(f"Failed to parse inline image: {e}")
                    elif image_url:
                        parts.append(types.Part.from_uri(
                            file_uri=image_url,
                            mime_type="image/jpeg"
                        ))

        # Handle assistant messages that contain tool_calls
        if role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    func_name = func.get("name", "")
                    raw_args = func.get("arguments", "{}")
                    try:
                        args_dict = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        args_dict = {}
                    parts.append(types.Part.from_function_call(
                        name=func_name,
                        args=args_dict
                    ))

        if parts:
            contents.append(types.Content(role=gemini_role, parts=parts))

    return system_instruction, contents


def _convert_tools(tools: Optional[list[dict]]) -> Optional[list[types.Tool]]:
    """Convert OpenAI-format tool definitions to Gemini Tool objects."""
    if not tools:
        return None

    declarations = []
    for tool_def in tools:
        if tool_def.get("type") != "function":
            continue
        func = tool_def.get("function", {})
        name = func.get("name", "")
        description = func.get("description", "")
        parameters = func.get("parameters", {})

        decl = types.FunctionDeclaration(
            name=name,
            description=description,
            parameters=parameters if parameters else None
        )
        declarations.append(decl)

    if not declarations:
        return None

    return [types.Tool(function_declarations=declarations)]


def _convert_tool_choice(tool_choice: Optional[str]) -> Optional[types.ToolConfig]:
    """Convert OpenAI tool_choice to Gemini ToolConfig."""
    if not tool_choice or tool_choice == "none":
        return None

    mode_map = {
        "auto": "AUTO",
        "required": "ANY",
    }
    mode_str = mode_map.get(tool_choice, "AUTO")
    return types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(mode=mode_str)
    )


class GeminiLLMClient(LLMModelClient):
    """LLM client using google.genai native SDK."""

    def __init__(self, model: ModelInfo):
        super().__init__(model)

    async def chat(self, request: LLMRequest, **kwargs) -> LLMResponse:
        client = _build_client(self.model)

        try:
            start_time = time.perf_counter()

            # Convert messages
            system_instruction, contents = _convert_messages(request.messages)

            # Build config
            temperature = (self.model.model_config.get("temperature")
                           if self.model.model_config else None)
            thinking_enabled = (self.model.model_config.get("thinking", False)
                                if self.model.model_config else False)
            thinking_budget = (self.model.model_config.get("thinking_budget", 8192)
                               if self.model.model_config else 8192)

            config_kwargs = {}
            if temperature is not None:
                config_kwargs["temperature"] = temperature
            if system_instruction:
                config_kwargs["system_instruction"] = system_instruction

            # Tools
            gemini_tools = _convert_tools(request.tools)
            if gemini_tools:
                config_kwargs["tools"] = gemini_tools
                # Disable auto function calling — we handle it ourselves
                config_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                    disable=True
                )
            tool_config = _convert_tool_choice(request.tool_choice)
            if tool_config:
                config_kwargs["tool_config"] = tool_config

            # Thinking mode
            if thinking_enabled:
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_budget=thinking_budget
                )

            config = types.GenerateContentConfig(**config_kwargs)

            # Call Gemini API
            response = await client.aio.models.generate_content(
                model=self.model.model_id,
                contents=contents,
                config=config
            )

            end_time = time.perf_counter()

            # Parse response
            llm_resp = LLMResponse("")
            llm_resp.time_consumed = round(end_time - start_time, 2)

            if response.usage_metadata:
                llm_resp.input_tokens = response.usage_metadata.prompt_token_count
                llm_resp.output_tokens = response.usage_metadata.candidates_token_count

            if not response.candidates:
                # Gemini returned no candidates (content filter, empty response, etc.)
                finish_reason = None
                if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                    finish_reason = getattr(response.prompt_feedback, 'block_reason', None)
                logger.warning(f"Gemini returned no candidates. Block reason: {finish_reason}")
                llm_resp.text_response = "[Gemini 未返回内容，可能被安全过滤拦截]"
                return llm_resp

            candidate = response.candidates[0]

            # Check for blocked/incomplete responses
            finish_reason = getattr(candidate, 'finish_reason', None)
            if finish_reason and str(finish_reason) not in ('STOP', 'FinishReason.STOP',
                                                             'MAX_TOKENS', 'FinishReason.MAX_TOKENS'):
                logger.warning(f"Gemini finish_reason: {finish_reason}")

            if not candidate.content or not candidate.content.parts:
                logger.warning(f"Gemini candidate has no content/parts, finish_reason={finish_reason}")
                llm_resp.text_response = f"[Gemini 响应异常: finish_reason={finish_reason}]"
                return llm_resp

            text_parts = []
            thinking_parts = []

            for part in candidate.content.parts:
                # Thinking content
                if hasattr(part, 'thought') and part.thought:
                    if part.text:
                        thinking_parts.append(part.text)
                    continue

                # Function calls → convert to OpenAI format
                if part.function_call:
                    fc = part.function_call
                    tool_call_id = f"call_{uuid.uuid4().hex[:24]}"
                    llm_resp.tool_calls.append({
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": fc.name,
                            "arguments": json.dumps(
                                dict(fc.args) if fc.args else {},
                                ensure_ascii=False
                            )
                        }
                    })
                    continue

                # Regular text
                if part.text:
                    text_parts.append(part.text)

            llm_resp.text_response = "".join(text_parts)
            if thinking_parts:
                llm_resp.reasoning_content = "\n".join(thinking_parts)

            return llm_resp

        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            return LLMResponse(text_response=f"[Error] {e}")


class GeminiEmbeddingClient(EmbeddingModelClient):
    """Embedding client using google.genai native SDK."""

    def __init__(self, model: ModelInfo):
        super().__init__(model)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        client = _build_client(self.model)

        try:
            start_time = time.perf_counter()
            result = await client.aio.models.embed_content(
                model=self.model.model_id,
                contents=texts
            )
            elapsed = round(time.perf_counter() - start_time, 2)

            slow_threshold = (self.model.model_config.get("slow_request_threshold", 5.0)
                              if self.model.model_config else 5.0)
            if elapsed > slow_threshold:
                logger.warning(f"Slow embedding request: {elapsed}s")

            return [e.values for e in result.embeddings]

        except Exception as e:
            logger.error(f"Gemini embedding error: {e}")
            return []


class GeminiImageClient(ImageModelClient):
    """Image generation client using Gemini's native image capabilities."""

    def __init__(self, model: ModelInfo):
        super().__init__(model)

    async def text_to_image(self, prompt) -> ImageResult:
        client = _build_client(self.model)

        try:
            aspect_ratio = (self.model.model_config.get("aspect_ratio")
                            if self.model.model_config else None)

            config_kwargs = {
                "response_modalities": ["IMAGE"],
            }
            if aspect_ratio:
                config_kwargs["image_config"] = types.ImageConfig(
                    aspect_ratio=aspect_ratio
                )

            config = types.GenerateContentConfig(**config_kwargs)

            response = await client.aio.models.generate_content(
                model=self.model.model_id,
                contents=prompt,
                config=config
            )

            # Extract image from response
            if response.candidates:
                for part in response.candidates[0].content.parts:
                    if part.inline_data:
                        import base64
                        b64_data = base64.b64encode(part.inline_data.data).decode("utf-8")
                        mime = part.inline_data.mime_type or "image/png"
                        data_url = f"data:{mime};base64,{b64_data}"
                        return ImageResult(data_url)

            logger.error("No image generated in Gemini response")
            return ImageResult("")

        except Exception as e:
            logger.error(f"Gemini image generation error: {e}")
            return ImageResult("")

    async def image_to_image(self, prompt: str, url: Optional[str] = None,
                             base64: Optional[str] = None) -> ImageResult:
        # Gemini's image editing via multimodal input
        client = _build_client(self.model)

        try:
            parts = []

            if base64:
                import base64 as b64_module
                image_bytes = b64_module.b64decode(base64)
                parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))

            parts.append(types.Part.from_text(text=prompt))

            config = types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            )

            response = await client.aio.models.generate_content(
                model=self.model.model_id,
                contents=types.Content(role="user", parts=parts),
                config=config
            )

            if response.candidates:
                for part in response.candidates[0].content.parts:
                    if part.inline_data:
                        import base64 as b64_module
                        b64_data = b64_module.b64encode(part.inline_data.data).decode("utf-8")
                        mime = part.inline_data.mime_type or "image/png"
                        data_url = f"data:{mime};base64,{b64_data}"
                        return ImageResult(data_url)

            return ImageResult("")

        except Exception as e:
            logger.error(f"Gemini image-to-image error: {e}")
            return ImageResult("")
