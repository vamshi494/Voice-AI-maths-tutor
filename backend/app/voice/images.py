# app/voice/images.py
"""Byte-stream 'images' handler and vision transcription.

1. Client compresses the image and sends it with sendFile on topic 'images'.
2. Agent reads byte stream and re-encodes with livekit.agents.utils.images.encode():
   EncodeOptions(format="JPEG", quality=80,
                 resize_options=ResizeOptions(width=1024, height=1024,
                                              strategy="scale_aspect_fit"))
3. Calls gateway.complete_json(prompt_key="vision.question", schema=VisionQuestion, model=MODEL_VISION).
4. Sends result as QuestionDraft event.
5. If photo is illegible, sends notice: "I couldn't read that clearly. Could you type the question?"
"""
import asyncio
import base64
import io
from typing import Any
from PIL import Image

import livekit.rtc as rtc
import livekit.agents.utils.images as livekit_images
from app.config import settings
from app.contracts.messages import ErrorNotice, QuestionDraft, VisionQuestion
from app.gateway.groq_client import GroqGateway
from app.observability import log_event, logger
from app.transport import send_event


def reencode_image(image_bytes: bytes) -> bytes:
    """Re-encode image using livekit.agents.utils.images.encode."""
    pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    frame = rtc.VideoFrame(
        pil_img.width,
        pil_img.height,
        rtc.VideoBufferType.RGBA,
        pil_img.tobytes(),
    )
    opts = livekit_images.EncodeOptions(
        format="JPEG",
        quality=80,
        resize_options=livekit_images.ResizeOptions(
            width=1024,
            height=1024,
            strategy="scale_aspect_fit",
        ),
    )
    return livekit_images.encode(frame, opts)


async def process_image_bytes(
    image_bytes: bytes,
    gateway: GroqGateway | None = None,
    generation: int = 0,
) -> QuestionDraft | ErrorNotice:
    """Re-encode image and transcribe with vision.question prompt."""
    gw = gateway or GroqGateway()
    try:
        jpeg_bytes = reencode_image(image_bytes)
        b64_str = base64.b64encode(jpeg_bytes).decode("ascii")

        # Try configured vision model (e.g. qwen/qwen3.8-27b) with 45s timeout
        res: VisionQuestion | None = await gw.complete_json(
            prompt_key="vision.question",
            schema=VisionQuestion,
            model=settings.MODEL_VISION,
            user="Transcribe the maths question in this image exactly as printed including diagram details.",
            image_b64=b64_str,
            timeout_s=45.0,
        )

        # Fallback to fast vision model if primary model fails
        if (res is None or not res.legible or not res.question_text.strip()) and settings.MODEL_VISION != "qwen/qwen2.5-vl-72b-instruct":
            logger.info("Attempting OCR with fallback model qwen/qwen2.5-vl-72b-instruct")
            res = await gw.complete_json(
                prompt_key="vision.question",
                schema=VisionQuestion,
                model="qwen/qwen2.5-vl-72b-instruct",
                user="Transcribe the maths question in this image exactly as printed including diagram details.",
                image_b64=b64_str,
                timeout_s=30.0,
            )

        if res is not None and res.legible and res.question_text.strip():
            log_event("vision_question_transcribed", text_len=len(res.question_text))
            return QuestionDraft(generation=generation, text=res.question_text.strip())
        else:
            # The provider's error text (e.g. an OpenRouter 403 with a dashboard URL) is
            # for the log, never for the student.
            log_event("vision_question_illegible", error=(getattr(gw, "last_error", None) or "")[:300])
            return ErrorNotice(
                generation=generation,
                message="I couldn't read that clearly. Could you check the photo or type the question?",
            )
    except Exception as e:
        logger.error(f"Failed to process image question: {e}")
        return ErrorNotice(
            generation=generation,
            message="I couldn't read that clearly. Could you type the question?",
        )


def register_image_stream_handler(room: rtc.Room, manager: Any, gateway: GroqGateway | None = None) -> None:
    """Register byte-stream handler on topic 'images'."""
    gw = gateway or GroqGateway()

    async def _handle_stream(reader: Any, participant_identity: str) -> None:
        try:
            chunks: list[bytes] = []
            async for chunk in reader:
                chunks.append(chunk)
            raw_bytes = b"".join(chunks)
            if not raw_bytes:
                return

            gen = getattr(manager, "current_generation", 0)
            evt = await process_image_bytes(raw_bytes, gateway=gw, generation=gen)
            await send_event(evt, room=room)
        except Exception as e:
            logger.error(f"Error handling image byte-stream from {participant_identity}: {e}")

    def _sync_handler(reader: Any, participant_identity: str) -> None:
        asyncio.create_task(_handle_stream(reader, participant_identity))

    room.register_byte_stream_handler("images", _sync_handler)
    log_event("image_stream_handler_registered", topic="images")
