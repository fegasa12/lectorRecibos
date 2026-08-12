import io
import re
import time
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import fitz  # PyMuPDF
from PIL import Image
import pytesseract

app = FastAPI(title="Extractor Multiservicio (CFE + Agua y Drenaje)", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def classify_document(text: str) -> str:
    """Detecta el emisor del recibo según términos clave."""
    text_upper = text.upper()
    if any(k in text_upper for k in ["AGUA Y DRENAJE", "SADM", "N.I.S", "N.I.R"]):
        return "agua_y_drenaje"
    elif any(k in text_upper for k in ["CFE", "COMISION FEDERAL DE ELECTRICIDAD", "NO. DE SERVICIO"]):
        return "cfe"
    return "desconocido"


def parse_sadm_text(text: str) -> dict:
    """Extrae los campos de los recibos de Agua y Drenaje de Monterrey."""
    data = {
        "numero_servicio": None, # N.I.S
        "total_a_pagar": None,
        "fecha_limite_pago": None, # Vencimiento
        "periodo_facturado": None,
        "consumo": None,
        "unidad_consumo": "m3",
        "medidor": None,
        "rmu": None
    }

    # 1. Número de Servicio / N.I.S (ej. 2247852-01)[cite: 2]
    nis_match = re.search(r'N\.?I\.?S[:\s]*([\d\-]+)', text, re.IGNORECASE)
    if nis_match:
        data["numero_servicio"] = nis_match.group(1).strip()

    # 2. Total a Pagar ($972.00)[cite: 2]
    total_match = re.search(r'TOTAL\s*A\s*PAGAR[^\d]*\$?\s*([\d,]+(?:\.\d{2})?)', text, re.IGNORECASE)
    if not total_match:
        total_match = re.search(r'TOTAL\s*DEL\s*MES[^\d]*([\d,]+(?:\.\d{2})?)', text, re.IGNORECASE)[cite: 2]
    if total_match:
        data["total_a_pagar"] = float(total_match.group(1).replace(',', ''))

    # 3. Fecha Límite de Pago / Vencimiento (28/SEP/2020)[cite: 2]
    venc_match = re.search(r'VENCIMIENTO[\s|]+(\d{2}/[A-Z]{3}/\d{4}|\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)[cite: 2]
    if venc_match:
        data["fecha_limite_pago"] = venc_match.group(1).strip()

    # 4. Periodo de Consumo (10/AGO/2020 - 08/SEP/2020)[cite: 2]
    period_match = re.search(r'PERIODO\s*DE\s*CONSUMO[\s|]+(\d{2}/[A-Z]{3}/\d{4}\s*\d{2}/[A-Z]{3}/\d{4}|\d{2}/\d{2}/\d{4}\s*-\s*\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)[cite: 2]
    if not period_match:
        period_match = re.search(r'MES\s*FACTURADO[\s|]+([A-Z]{3}/\d{4})', text, re.IGNORECASE)[cite: 2]
    if period_match:
        data["periodo_facturado"] = re.sub(r'\s+', ' - ', period_match.group(1).strip())

    # 5. Consumo en m3 (CONSUMO 42)[cite: 2]
    consumo_match = re.search(r'CONSUMO[\s|]+(\d+)', text, re.IGNORECASE)[cite: 2]
    if consumo_match:
        data["consumo"] = int(consumo_match.group(1))

    # 6. Número de Medidor (02412438)[cite: 2]
    medidor_match = re.search(r'MEDIDOR[\s|]+(\d+)', text, re.IGNORECASE)[cite: 2]
    if medidor_match:
        data["medidor"] = medidor_match.group(1).strip()

    return data


def parse_cfe_text(text: str) -> dict:
    """Extrae los campos de los recibos de CFE."""
    data = {
        "numero_servicio": None,
        "rmu": None,
        "total_a_pagar": None,
        "fecha_limite_pago": None,
        "periodo_facturado": None,
        "consumo": None,
        "unidad_consumo": "kWh",
        "medidor": None
    }

    # No. de Servicio (RPU)[cite: 1]
    service_match = re.search(r'\b\d{12}\b', text)[cite: 1]
    if service_match:
        data["numero_servicio"] = service_match.group(0)[cite: 1]

    # Total a Pagar[cite: 1]
    total_match = re.search(r'TOTAL\s*A\s*PAGAR[^\d]*([\d,]+(?:\.\d{2})?)', text, re.IGNORECASE)[cite: 1]
    if not total_match:
        total_match = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)', text)[cite: 1]
    if total_match:
        data["total_a_pagar"] = float(total_match.group(1).replace(',', ''))

    # RMU[cite: 1]
    rmu_match = re.search(r'RMU[:\s]*([0-9A-Z\s\-]{15,35})', text, re.IGNORECASE)[cite: 1]
    if rmu_match:
        data["rmu"] = re.sub(r'\s+', ' ', rmu_match.group(1)).strip()

    # Fecha Límite de Pago[cite: 1]
    limit_match = re.search(r'(?:LÍMITE|LIMITE)\s*DE\s*PAGO[:\s]*([\d]{2}\s+[A-Z]{3}\s+[\d]{2,4}|\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)[cite: 1]
    if limit_match:
        data["fecha_limite_pago"] = limit_match.group(1).strip()

    # Periodo Facturado[cite: 1]
    period_match = re.search(r'PERIODO\s*FACTURADO[:\s]*([\d]{2}\s+[A-Z]{3}\s+[\d]{2,4}\s*-\s*[\d]{2}\s+[A-Z]{3}\s+[\d]{2,4})', text, re.IGNORECASE)[cite: 1]
    if period_match:
        data["periodo_facturado"] = period_match.group(1).strip()

    # Consumo kWh (Histórico P2 o Suma GDMTH)[cite: 1]
    history_matches = re.findall(r'(?:ENE|FEB|MAR|ABR|MAY|JUN|JUL|AGO|SEP|OCT|NOV|DIC)\s*\d{2}[\s|]+(?:\d+[\s|]+)?([\d,]+)', text, re.IGNORECASE)[cite: 1]
    if history_matches:
        data["consumo"] = int(history_matches[-1].replace(',', ''))
    else:
        base_m = re.search(r'kWh\s*base[\s|]+([\d,]+)', text, re.IGNORECASE)
        inter_m = re.search(r'kWh\s*intermedia[\s|]+([\d,]+)', text, re.IGNORECASE)
        punta_m = re.search(r'kWh\s*punta[\s|]+([\d,]+)', text, re.IGNORECASE)
        if base_m or inter_m or punta_m:
            b = int(base_m.group(1).replace(',', '')) if base_m else 0
            i = int(inter_m.group(1).replace(',', '')) if inter_m else 0
            p = int(punta_m.group(1).replace(',', '')) if punta_m else 0
            data["consumo"] = b + i + p

    return data


@app.post("/extract")
async def extract_receipt(file: UploadFile = File(...)):
    start_time = time.time()
    contents = await file.read()
    file_type = file.filename.split(".")[-1].lower()

    raw_text = ""
    source_type = "digital_stream"

    try:
        if file_type == "pdf":
            doc = fitz.open(stream=contents, filetype="pdf")
            raw_text = "\n".join([page.get_text("text") for page in doc])

            if len(raw_text.strip()) < 50:
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
            raise HTTPException(status_code=400, detail="Formato no soportado.")

        # Clasificación y selección de parser
        issuer = classify_document(raw_text)

        if issuer == "agua_y_drenaje":
            extracted_data = parse_sadm_text(raw_text)
        elif issuer == "cfe":
            extracted_data = parse_cfe_text(raw_text)
        else:
            extracted_data = parse_sadm_text(raw_text)  # Fallback genérico

        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        return {
            "status": "success",
            "processing_time_ms": elapsed_ms,
            "source": source_type,
            "emisor": issuer,
            "data": extracted_data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al procesar archivo: {str(e)}")
