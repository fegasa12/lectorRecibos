import io
import re
import time
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import fitz  # PyMuPDF
from PIL import Image
import pytesseract

app = FastAPI(title="CFE Extractor API Multi-Tarifa", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def parse_cfe_text(text: str) -> dict:
    data = {
        "numero_servicio": None,
        "rmu": None,
        "total_a_pagar": None,
        "fecha_limite_pago": None,
        "periodo_facturado": None,
        "consumo_kwh": None
    }

    # 1. No. de servicio (RPU - 12 dígitos)
    service_match = re.search(r'\b\d{12}\b', text)
    if service_match:
        data["numero_servicio"] = service_match.group(0)

    # 2. Total a Pagar ($5,184)
    total_match = re.search(r'TOTAL\s*A\s*PAGAR[^\d]*([\d,]+(?:\.\d{2})?)', text, re.IGNORECASE)
    if not total_match:
        total_match = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)', text)
    if total_match:
        data["total_a_pagar"] = float(total_match.group(1).replace(',', ''))

    # 3. RMU
    rmu_match = re.search(r'RMU[:\s]*([0-9A-Z\s\-]{15,35})', text, re.IGNORECASE)
    if rmu_match:
        data["rmu"] = re.sub(r'\s+', ' ', rmu_match.group(1)).strip()

    # 4. Fecha Límite de Pago
    limit_match = re.search(r'(?:LÍMITE|LIMITE)\s*DE\s*PAGO[:\s]*([\d]{2}\s+[A-Z]{3}\s+[\d]{2,4}|\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    if limit_match:
        data["fecha_limite_pago"] = limit_match.group(1).strip()

    # 5. Periodo Facturado
    period_match = re.search(r'PERIODO\s*FACTURADO[:\s]*([\d]{2}\s+[A-Z]{3}\s+[\d]{2,4}\s*-\s*[\d]{2}\s+[A-Z]{3}\s+[\d]{2,4})', text, re.IGNORECASE)
    if period_match:
        data["periodo_facturado"] = period_match.group(1).strip()

    # 6. Consumo kWh (Sin confundir importes en MXN)
    kwh_val = None

    # ESTRATEGIA 1: Tarifa Horaria GDMTH / GDMTO (kWh base + intermedia + punta)
    base_m = re.search(r'kWh\s*base[^\d]{1,30}([\d,]+)', text, re.IGNORECASE)
    inter_m = re.search(r'kWh\s*intermedia[^\d]{1,30}([\d,]+)', text, re.IGNORECASE)
    punta_m = re.search(r'kWh\s*punta[^\d]{1,30}([\d,]+)', text, re.IGNORECASE)

    if base_m or inter_m or punta_m:
        b = int(base_m.group(1).replace(',', '')) if base_m else 0
        i = int(inter_m.group(1).replace(',', '')) if inter_m else 0
        p = int(punta_m.group(1).replace(',', '')) if punta_m else 0
        total_gdmth = b + i + p
        if total_gdmth > 0:
            kwh_val = total_gdmth

    # ESTRATEGIA 2: Tabla de Histórico de la Página 2 ("Consumo total kWh")
    if kwh_val is None:
        hist_matches = re.findall(r'(?:ENE|FEB|MAR|ABR|MAY|JUN|JUL|AGO|SEP|OCT|NOV|DIC)\s*\d{2}[^\d]*?\d+[^\d]*?([\d,]+)', text, re.IGNORECASE)
        if hist_matches:
            kwh_val = int(hist_matches[-1].replace(',', ''))

    # ESTRATEGIA 3: Tarifa Residencial Estándar (Excluye 'Energía' suelta por ser importe monetario)
    if kwh_val is None:
        std_m = re.search(r'(?:Total\s*periodo|Consumo\s*total|Energ[ií]a\s*\(kWh\)|Consumo\s*kWh)[^\d]{1,30}([\d,]+)', text, re.IGNORECASE)
        if std_m:
            kwh_val = int(std_m.group(1).replace(',', ''))

    data["consumo_kwh"] = kwh_val
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
            
            # EXTRAER TEXTO DE TODAS LAS PÁGINAS DEL PDF (Página 1 + Página 2)
            pages_text = [page.get_text("text") for page in doc]
            raw_text = "\n".join(pages_text)

            # Fallback a OCR si el PDF no contiene texto nativo
            if len(raw_text.strip()) < 50 or "SERVICIO" not in raw_text.upper():
                source_type = "ocr_fallback"
                raw_text = ""
                for page in doc:
                    pix = page.get_pixmap(dpi=300)
                    img = Image.open(io.BytesIO(pix.tobytes("png")))
                    raw_text += "\n" + pytesseract.image_to_string(img, lang="spa")

        elif file_type in ["png", "jpg", "jpeg"]:
            source_type = "ocr_fallback"
            img = Image.open(io.BytesIO(contents))
            raw_text = pytesseract.image_to_string(img, lang="spa")
        else:
            raise HTTPException(status_code=400, detail="Formato no soportado. Envía PDF, PNG o JPG.")

        extracted_data = parse_cfe_text(raw_text)
        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        return {
            "status": "success",
            "processing_time_ms": elapsed_ms,
            "source": source_type,
            "data": extracted_data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al procesar archivo: {str(e)}")
