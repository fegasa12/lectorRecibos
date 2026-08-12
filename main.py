import io
import re
import time
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import fitz  # PyMuPDF
from PIL import Image
import pytesseract

app = FastAPI(title="CFE Extractor API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def parse_cfe_text(text: str) -> dict:
    """Extrae mediante expresiones regulares la información clave del texto."""
    data = {
        "numero_servicio": None,
        "rmu": None,
        "total_a_pagar": None,
        "fecha_limite_pago": None,
        "periodo_facturado": None,
        "consumo_kwh": None,
    }

    # 1. Número de servicio (RPU - 12 dígitos)
    service_match = re.search(r"\b\d{12}\b", text)
    if service_match:
        data["numero_servicio"] = service_match.group(0)

    # 2. Total a Pagar
    total_match = re.search(
        r"TOTAL\s*A\s*PAGAR[^\d]*([\d,]+\.\d{2})", text, re.IGNORECASE
    )
    if not total_match:
        total_match = re.search(r"\$\s*([\d,]+\.\d{2})", text)
    if total_match:
        data["total_a_pagar"] = float(total_match.group(1).replace(",", ""))

    # 3. RMU
    rmu_match = re.search(
        r"RMU[:\s]*([0-9A-Z\s\-]{15,35})", text, re.IGNORECASE
    )
    if rmu_match:
        data["rmu"] = rmu_match.group(1).strip()

    # 4. Fecha Límite de Pago
    limit_match = re.search(
        r"(?:LÍMITE|LIMITE)\s*DE\s*PAGO[:\s]*([\d]{2}\s+[A-Z]{3}\s+[\d]{2,4}|\d{2}/\d{2}/\d{4})",
        text,
        re.IGNORECASE,
    )
    if limit_match:
        data["fecha_limite_pago"] = limit_match.group(1).strip()

    # 5. Periodo Facturado
    period_match = re.search(
        r"PERIODO\s*FACTURADO[:\s]*([\d]{2}\s+[A-Z]{3}\s+[\d]{2,4}\s*-\s*[\d]{2}\s+[A-Z]{3}\s+[\d]{2,4})",
        text,
        re.IGNORECASE,
    )
    if period_match:
        data["periodo_facturado"] = period_match.group(1).strip()

    # 6. Consumo kWh
    kwh_match = re.search(
        r"(?:Total\s*periodo|Consumo)[:\s]*(\d+)", text, re.IGNORECASE
    )
    if kwh_match:
        data["consumo_kwh"] = int(kwh_match.group(1))

    return data


@app.post("/extract")
async def extract_cfe(file: UploadFile = File(...)):
    start_time = time.time()
    contents = await file.read()
    file_type = file.filename.split(".")[-1].lower()

    raw_text = ""
    source_type = "digital_stream"

    try:
        if file_type == "pdf":
            doc = fitz.open(stream=contents, filetype="pdf")
            page = doc[0]

            # Intentar extracción directa de texto nativo
            raw_text = page.get_text("text")

            # Fallback a OCR si el texto es escaso o es una imagen escaneada
            if len(raw_text.strip()) < 50 or "SERVICIO" not in raw_text.upper():
                source_type = "ocr_fallback"
                pix = page.get_pixmap(dpi=300)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                raw_text = pytesseract.image_to_string(img, lang="spa")

        elif file_type in ["png", "jpg", "jpeg"]:
            source_type = "ocr_fallback"
            img = Image.open(io.BytesIO(contents))
            raw_text = pytesseract.image_to_string(img, lang="spa")
        else:
            raise HTTPException(
                status_code=400,
                detail="Formato no soportado. Envía PDF, PNG o JPG.",
            )

        extracted_data = parse_cfe_text(raw_text)
        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        return {
            "status": "success",
            "processing_time_ms": elapsed_ms,
            "source": source_type,
            "data": extracted_data,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al procesar archivo: {str(e)}"
        )