"""多模态视觉诊断服务：图片预处理 + Qwen2.5-VL 结构化分析。

三条设计原则：

1. **图像先过预处理再进模型**。手机原图动辄 4~8MB、4000px 宽，直接 base64 塞给
   本地 7B 模型，光是视觉编码就会拖到几十秒，而且超出模型的有效感受野。
   统一压到 1280px 长边、JPEG q=88，体积降一个数量级，视觉信息几乎无损。

2. **结构化输出走「约束优先 + 三级降级」**。首选 Ollama 的 ``format`` 传 JSON Schema
   做语法级约束；模型不支持时降级为 ``format="json"``（只保证合法 JSON）；
   再不行才退到「纯提示词 + 宽容解析」。每次降级都记日志，便于定位是哪个模型不配合。

3. **视觉分析不走流式**。诊断结果是一个 JSON 对象，必须拿到完整内容才能校验；
   流式吐一半的 JSON 无法解析。真正需要流式体验的是后面的自然语言回答。
"""

from __future__ import annotations

import io
from functools import lru_cache

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

from app.config import Settings, get_settings
from app.core.errors import ImageValidationError, ModelOutputError, OllamaUnavailableError
from app.core.parsing import inline_json_schema, parse_model
from app.core.prompts import VISION_SYSTEM_PROMPT, build_vision_user_prompt
from app.schemas.diagnosis import ImageMeta, VisionDiagnosis
from app.services.ollama_client import OllamaClient

# 低于该边长视为分辨率不足，会在 warnings 中提示用户重拍
_MIN_USABLE_SIDE = 300
# 平均亮度低于/高于该阈值视为画面偏暗/过曝
_DARK_THRESHOLD = 45
_BRIGHT_THRESHOLD = 215


@lru_cache(maxsize=1)
def get_ollama_client() -> OllamaClient:
    """获取 Ollama 客户端单例（复用连接池）。"""
    settings = get_settings()
    return OllamaClient(settings.ollama_api, default_timeout=settings.llm_timeout)


class VisionService:
    """视觉诊断服务。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """初始化。

        Args:
            settings: 全局配置。
        """
        self.settings = settings or get_settings()
        self.client = get_ollama_client()

    # ------------------------------------------------------------------
    # 图像预处理
    # ------------------------------------------------------------------

    def prepare_image(self, raw: bytes, mime: str = "image/jpeg") -> tuple[bytes, str, ImageMeta]:
        """校验并标准化图片。

        处理内容：大小校验 → EXIF 方向校正 → 转 RGB → 长边缩放 → 重新编码 JPEG，
        同时基于亮度与尺寸给出「可能影响判断」的提示。

        Args:
            raw: 原始图片字节。
            mime: 声明的 MIME 类型。

        Returns:
            ``(处理后的字节, MIME, 元信息)``

        Raises:
            ImageValidationError: 体积超限、格式无法识别或图片损坏。
        """
        max_bytes = int(self.settings.max_image_mb * 1024 * 1024)
        if not raw:
            raise ImageValidationError("上传内容为空，请重新选择图片")
        if len(raw) > max_bytes:
            raise ImageValidationError(
                f"图片体积 {len(raw) / 1024 / 1024:.1f}MB 超过上限 {self.settings.max_image_mb}MB，"
                "请压缩后重试"
            )

        try:
            image = Image.open(io.BytesIO(raw))
            image.load()
        except UnidentifiedImageError as exc:
            raise ImageValidationError(
                "无法识别该图片格式，请上传 JPG / PNG / WEBP 格式的图片"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 损坏文件、解压炸弹等
            raise ImageValidationError(f"图片解析失败：{exc}") from exc

        warnings: list[str] = []
        original_width, original_height = image.size
        if min(original_width, original_height) < _MIN_USABLE_SIDE:
            warnings.append(
                f"图像分辨率偏低（{original_width}×{original_height}），细节可能不足以支撑判断，"
                "建议在光线充足处重拍"
            )

        # 手机拍摄的图片常带 EXIF 旋转标记，不校正会以错误方向送进模型
        image = ImageOps.exif_transpose(image)
        if image.mode != "RGB":
            image = image.convert("RGB")

        # 亮度评估：过暗/过曝是本地模型误判的高频原因，提前提示用户
        try:
            stat = ImageStat.Stat(image.convert("L"))
            mean_brightness = float(stat.mean[0])
            if mean_brightness < _DARK_THRESHOLD:
                warnings.append("图像整体偏暗，可能遗漏皮损细节，建议在自然光下重拍")
            elif mean_brightness > _BRIGHT_THRESHOLD:
                warnings.append("图像存在过曝，颜色信息可能失真，建议避免直射闪光灯")
        except Exception:  # noqa: BLE001
            pass

        max_side = self.settings.image_max_side
        resized = False
        if max(image.size) > max_side:
            ratio = max_side / max(image.size)
            new_size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
            image = image.resize(new_size, Image.LANCZOS)
            resized = True

        buffer = io.BytesIO()
        image.save(
            buffer, format="JPEG", quality=self.settings.image_jpeg_quality, optimize=True
        )
        processed = buffer.getvalue()

        meta = ImageMeta(
            mime="image/jpeg",
            width=image.width,
            height=image.height,
            size_bytes=len(processed),
            resized=resized,
            warnings=warnings,
        )
        logger.info(
            "图像预处理完成: {}x{} -> {}x{}，{}KB -> {}KB，{}",
            original_width,
            original_height,
            meta.width,
            meta.height,
            len(raw) // 1024,
            meta.size_bytes // 1024,
            "已缩放" if resized else "未缩放",
        )
        return processed, "image/jpeg", meta

    # ------------------------------------------------------------------
    # 结构化视觉分析
    # ------------------------------------------------------------------

    def _build_options(self) -> dict[str, object]:
        """构造采样参数。视觉任务用低温度，抑制「看图编故事」。"""
        return {
            "temperature": 0.1,
            "top_p": 0.9,
            "num_ctx": self.settings.num_ctx,
            "num_predict": self.settings.num_predict,
        }

    async def _analyze_via_rest(self, image: bytes, user_text: str) -> str:
        """REST 路径：三级 format 降级。"""
        messages = [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": build_vision_user_prompt(user_text)},
        ]
        schema = inline_json_schema(VisionDiagnosis.model_json_schema())

        # 依次尝试：Schema 约束 -> 仅 JSON -> 自由文本（提示词已含结构要求）
        strategies: list[tuple[str, dict | str | None]] = [
            ("json_schema", schema),
            ("json", "json"),
            ("freeform", None),
        ]

        last_error: Exception | None = None
        for index, (name, response_format) in enumerate(strategies):
            try:
                content = await self.client.chat(
                    self.settings.vision_model,
                    messages,
                    images=[image],
                    response_format=response_format,
                    options=self._build_options(),
                    timeout=self.settings.vision_timeout,
                )
            except ModelOutputError as exc:
                # 该模型不支持此 format，换更宽松的策略重试
                logger.warning("视觉分析 format={} 被拒绝：{}，降级重试", name, exc.message)
                last_error = exc
                continue

            if index > 0:
                logger.info("视觉分析使用降级策略：{}", name)
            return content

        raise ModelOutputError(
            "视觉模型拒绝了所有输出格式策略，请检查模型是否支持结构化输出",
            detail=str(last_error),
        )

    async def _analyze_via_langchain(self, image: bytes, user_text: str) -> str:
        """LangChain 路径：走 ChatOllama 的多模态消息。

        依赖 langchain-ollama 对 ``image_url`` content block 的支持，
        不同版本行为有差异，因此作为可选后端而非默认路径。
        """
        import base64

        from app.services.llm_factory import get_vision_llm

        data_uri = f"data:image/jpeg;base64,{base64.b64encode(image).decode('ascii')}"
        messages = [
            SystemMessage(content=VISION_SYSTEM_PROMPT),
            HumanMessage(
                content=[
                    {"type": "text", "text": build_vision_user_prompt(user_text)},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ]
            ),
        ]
        response = await get_vision_llm().ainvoke(messages)
        content = response.content
        if isinstance(content, list):  # content block 列表
            content = "".join(
                part if isinstance(part, str) else str(part.get("text", "")) for part in content
            )
        return str(content)

    async def analyze(
        self, image: bytes, user_text: str = "", mime: str = "image/jpeg"
    ) -> VisionDiagnosis:
        """对图片做结构化视觉分析。

        Args:
            image: 已预处理的图片字节。
            user_text: 用户附带的文字描述。
            mime: 图片 MIME 类型。

        Returns:
            结构化诊断结果。**解析失败时会抛出异常而不是返回空对象**，
            因为「静默的空诊断」比「明确的报错」危险得多。

        Raises:
            OllamaUnavailableError: Ollama 不可达。
            ModelOutputError: 模型输出无法解析为约定结构。
        """
        backend = self.settings.vision_backend
        logger.info("开始视觉分析：model={} backend={}", self.settings.vision_model, backend)

        if backend == "langchain":
            raw = await self._analyze_via_langchain(image, user_text)
        else:
            raw = await self._analyze_via_rest(image, user_text)

        try:
            diagnosis = parse_model(raw, VisionDiagnosis)
        except ValueError as exc:
            raise ModelOutputError(
                "视觉模型未按要求输出结构化结果，请重试或更换更大的视觉模型",
                detail=str(exc),
            ) from exc

        logger.info(
            "视觉分析完成：risk={}，候选 {} 个，危险信号 {} 条",
            diagnosis.risk_level.value,
            len(diagnosis.hypotheses),
            len(diagnosis.red_flags),
        )
        return diagnosis

    async def health(self) -> tuple[bool, bool]:
        """检查 Ollama 连通性与视觉模型是否就绪。

        Returns:
            ``(服务是否连通, 模型是否就绪)``
        """
        try:
            connected = bool(await self.client.list_models())
            if not connected:
                return False, False
            return True, await self.client.has_model(self.settings.vision_model)
        except OllamaUnavailableError:
            return False, False


@lru_cache(maxsize=1)
def get_vision_service() -> VisionService:
    """获取视觉服务单例。"""
    return VisionService()
