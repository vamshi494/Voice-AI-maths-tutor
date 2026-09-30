# app/api/routes_ocr.py
from fastapi import APIRouter, File, HTTPException, UploadFile
from app.voice.images import process_image_bytes
from app.contracts.messages import QuestionDraft, ErrorNotice
from app.observability import log_event

router = APIRouter(prefix="", tags=["OCR"])


@router.post("/ocr")
@router.post("/images/ocr")
async def ocr_image(file: UploadFile = File(...)) -> dict:
    """Extract math question text and diagram details from uploaded image."""
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Empty file uploaded")

        res = await process_image_bytes(content)
        if isinstance(res, QuestionDraft):
            log_event("ocr_upload_success", text_len=len(res.text))
            return {
                "success": True,
                "text": res.text,
            }
        elif isinstance(res, ErrorNotice):
            log_event("ocr_upload_notice", message=res.message)
            return {
                "success": False,
                "message": res.message,
            }
        return {
            "success": False,
            "message": "Could not extract question from image.",
        }
    except HTTPException:
        raise                                   # a deliberate 4xx must not become a 500
    except Exception as e:
        # The client shows `detail` to the student; internal/provider text stays in the log.
        log_event("ocr_upload_error", error=str(e))
        raise HTTPException(status_code=500,
                            detail="I couldn't read that photo. Please try again or type the question.")
