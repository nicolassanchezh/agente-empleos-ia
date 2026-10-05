"""
Agente de empleos LinkedIn (vía alertas por email).

Flujo diario:
  1. Lee las alertas de empleo de LinkedIn de los últimos días desde Gmail (IMAP).
  2. Extrae las ofertas y descarga su descripción pública (sin login).
  3. Evalúa cada oferta con Claude contra el perfil de config.json, usando además
     el feedback del candidato (Google Sheets) y los aprendizajes semanales.
  4. Envía un digest por email ordenado por afinidad, con un botón de feedback por oferta.
  5. Registra ofertas, historial de análisis y métricas en Google Sheets.
  6. Guarda los IDs procesados en seen_jobs.json para no repetir ofertas.

Variables de entorno:
  GMAIL_USER                   -> dirección de Gmail que recibe las alertas
  GMAIL_APP_PASSWORD           -> contraseña de aplicación de Gmail (no la contraseña normal)
  ANTHROPIC_API_KEY            -> API key de Anthropic
  GOOGLE_SERVICE_ACCOUNT_JSON  -> (opcional) credenciales de la cuenta de servicio de Google
"""

import email
import imaplib
import json
import os
import re
import smtplib
import sys
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from sheets import (FeedbackSheet, link_formulario, boton_feedback_html,
                    encabezado_sheet_html, contexto_para_prompt)
import time

import anthropic
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
SEEN_FILE = BASE_DIR / "seen_jobs.json"

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


# ---------------------------------------------------------------- 1. LEER EMAILS

MESES_IMAP = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fetch_alert_emails():
    """Devuelve (html de cada email de alerta de los últimos días, ids de esos emails).

    Busca por fecha (leídos o no), así que abrir la alerta en el celular no hace
    que el agente la salte. seen_jobs.json evita repetir ofertas ya enviadas.
    """
    mail = imaplib.IMAP4_SSL(CONFIG["email"]["imap_server"])
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("inbox")

    dias = CONFIG["email"].get("dias_busqueda", 2)
    desde = date.today() - timedelta(days=dias)
    desde_imap = f"{desde.day:02d}-{MESES_IMAP[desde.month - 1]}-{desde.year}"  # formato IMAP, sin depender del idioma

    ids = []
    for remitente in CONFIG["email"]["remitentes_alertas"]:
        _, data = mail.search(None, f'(SINCE {desde_imap} FROM "{remitente}")')
        ids.extend(data[0].split())
    ids = list(dict.fromkeys(ids))  # quitar duplicados preservando orden
    html_bodies = []

    for msg_id in ids:
        # PEEK: leer sin marcar como leído; se marcan al final si todo salió bien
        _, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
        msg = email.message_from_bytes(msg_data[0][1])
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                html_bodies.append(part.get_payload(decode=True).decode(charset, errors="replace"))

    mail.logout()
    return html_bodies, ids


def mark_emails_processed(ids):
    """Marca como leídos los emails ya procesados (solo tras éxito completo)."""
    mail = imaplib.IMAP4_SSL(CONFIG["email"]["imap_server"])
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("inbox")
    for msg_id in ids:
        mail.store(msg_id, "+FLAGS", "\\Seen")
    mail.logout()


# ---------------------------------------------------------------- 2. PARSEAR EMPLEOS

def parse_jobs(html_bodies):
    """
    Extrae empleos de los emails de alerta.
    LinkedIn cambia su HTML de vez en cuando: esta función busca links a
    /jobs/view/ y toma el texto cercano, que es lo más estable.
    """
    jobs = {}
    for html in html_bodies:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            match = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", a["href"])
            if not match:
                continue
            job_id = match.group(1)
            # Texto del link y de su contenedor (suele incluir empresa · ubicación)
            texto_link = a.get_text(" ", strip=True)
            contenedor = a.find_parent("table") or a.parent
            texto_contexto = contenedor.get_text(" | ", strip=True)[:300] if contenedor else ""
            if job_id not in jobs and texto_link:
                jobs[job_id] = {
                    "id": job_id,
                    "texto": texto_link,
                    "contexto": texto_contexto,
                    "url": f"https://www.linkedin.com/jobs/view/{job_id}",
                }
    return list(jobs.values())


def filter_new(jobs):
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    return [j for j in jobs if j["id"] not in seen]


def save_seen(jobs):
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    seen.update(j["id"] for j in jobs)
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


# ------------------------------------------------- 2b. DESCARGAR DESCRIPCIONES

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
MAX_DESC_CHARS = 2500


def fetch_descriptions(jobs):
    """
    Descarga la descripción pública de cada empleo (sin login).
    Si LinkedIn bloquea o cambia la página, deja la descripción vacía
    y el análisis continúa solo con los datos del email.
    """
    ok = 0
    for job in jobs:
        try:
            r = requests.get(job["url"], headers=HEADERS, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                nodo = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
                if nodo:
                    job["descripcion"] = nodo.get_text(" ", strip=True)[:MAX_DESC_CHARS]
                    ok += 1
        except requests.RequestException:
            pass
        job.setdefault("descripcion", "")
        time.sleep(2)  # pausa entre peticiones: comportamiento respetuoso
    print(f"  Descripciones obtenidas: {ok}/{len(jobs)}")
    return jobs


# ---------------------------------------------------------------- 3. ANALIZAR CON CLAUDE

SYSTEM_PROMPT = """Eres un analista de reclutamiento experto. Tu tarea es evaluar ofertas de empleo
contra el perfil de un candidato y calcular un puntaje de afinidad de 0 a 100.

REGLAS ESTRICTAS:
1. Usa EXACTAMENTE la rúbrica de pesos provista. No inventes otros criterios.
2. Si un dato no aparece en la oferta (salario, seniority, modalidad), escribe
   "no especificado" y NO lo penalices ni lo inventes.
3. Si la oferta cumple alguna condición de la lista "descartar_si", el puntaje
   máximo es 30 y debes indicar cuál condición se cumplió.
4. Responde ÚNICAMENTE con JSON válido, sin markdown ni texto adicional.

FORMATO DE SALIDA (JSON array, un objeto por empleo):
[
  {
    "id": "...",
    "titulo": "...",
    "empresa": "... o no especificado",
    "ubicacion": "... o no especificado",
    "modalidad": "remoto/híbrido/presencial/no especificado",
    "seniority": "... o no especificado",
    "requisitos_clave": ["máximo 5"],
    "resumen": "3-4 frases describiendo el rol y responsabilidades, SOLO con información presente en la descripción; si el campo 'descripcion' está vacío escribe 'Descripción no disponible en la alerta.'",
    "afinidad": 0-100,
    "justificacion": "2-3 frases: qué suma, qué falta o descarta",
    "recomendacion": "postular / revisar / descartar"
  }
]"""


BATCH_SIZE = 10  # empleos por llamada a Claude (con descripciones son más largos)

# Métricas de la ejecución en curso (se escriben en la pestaña "Metricas" del Sheet).
# Precios aprox. de Claude Sonnet en US$ por millón de tokens; ajústalos si cambian.
PRECIO_ENTRADA_MTOK = 3.0
PRECIO_SALIDA_MTOK = 15.0
STATS = {"tokens_entrada": 0, "tokens_salida": 0}


CAMPOS_TEXTO = ["titulo", "empresa", "ubicacion", "modalidad", "seniority", "resumen", "justificacion", "recomendacion"]


def normalizar_resultado(r):
    """Completa campos faltantes o mal tipados para que un error de formato de Claude no bote el digest."""
    r["id"] = str(r["id"])
    for campo in CAMPOS_TEXTO:
        valor = r.get(campo)
        r[campo] = str(valor).strip() if valor not in (None, "") else "no especificado"
    req = r.get("requisitos_clave")
    if isinstance(req, str):
        req = [req]
    r["requisitos_clave"] = [str(x) for x in req] if isinstance(req, list) else []
    try:
        r["afinidad"] = int(float(r.get("afinidad", 0)))
    except (TypeError, ValueError):
        r["afinidad"] = 0
    return r


def analyze_jobs(jobs, contexto_feedback=""):
    client = anthropic.Anthropic()  # usa ANTHROPIC_API_KEY del entorno
    # el historial de postulaciones se suma a las instrucciones de evaluación
    system = SYSTEM_PROMPT + ("\n\n" + contexto_feedback if contexto_feedback else "")
    # El historial es largo; se repite el formato al final para que Claude no omita campos.
    system += ("\n\nRECORDATORIO FINAL: responde ÚNICAMENTE con el JSON array descrito en FORMATO DE SALIDA, "
               "un objeto por cada empleo recibido y con TODOS los campos (incluido \"recomendacion\").")
    perfil = json.dumps(CONFIG["perfil"], ensure_ascii=False, indent=2)
    rubrica = json.dumps(CONFIG["rubrica_afinidad"], ensure_ascii=False)

    resultados = []
    total_lotes = (len(jobs) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(jobs), BATCH_SIZE):
        lote = jobs[i:i + BATCH_SIZE]
        print(f"  Analizando lote {i // BATCH_SIZE + 1}/{total_lotes} ({len(lote)} empleos)...")
        listado = json.dumps(lote, ensure_ascii=False, indent=2)

        user_msg = f"""PERFIL DEL CANDIDATO:
{perfil}

RÚBRICA DE AFINIDAD (pesos en %):
{rubrica}

EMPLEOS A EVALUAR (extraídos de alertas de LinkedIn; el campo "contexto"
puede contener empresa y ubicación mezcladas con otro texto):
{listado}

Evalúa cada empleo y devuelve el JSON array."""

        response = client.messages.create(
            model=CONFIG.get("modelo", "claude-sonnet-4-6"),
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        uso = getattr(response, "usage", None)
        STATS["tokens_entrada"] += getattr(uso, "input_tokens", 0) or 0
        STATS["tokens_salida"] += getattr(uso, "output_tokens", 0) or 0
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                f"Respuesta truncada en el lote {i // BATCH_SIZE + 1}: "
                f"reduce BATCH_SIZE (actual: {BATCH_SIZE})."
            )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        resultados.extend(normalizar_resultado(r) for r in json.loads(raw) if isinstance(r, dict) and r.get("id"))

    return resultados


# ---------------------------------------------------------------- 4. ENVIAR DIGEST

def build_digest(resultados, jobs_by_id, url_sheet=None):
    """Devuelve (html del digest, lista de ofertas incluidas para registrar en el Sheet)."""
    fb_cfg = CONFIG.get("feedback", {})
    umbral = CONFIG["umbral_minimo_para_incluir"]
    incluidos = sorted(
        [r for r in resultados if r["afinidad"] >= umbral],
        key=lambda r: r["afinidad"],
        reverse=True,
    )
    descartados = len(resultados) - len(incluidos)

    filas = ""
    registros = []
    for r in incluidos:
        url = jobs_by_id.get(r["id"], {}).get("url", "#")
        # datos de la oferta para el Form y el Sheet
        oferta = {"job_id": str(r["id"]), "empresa": r.get("empresa", ""), "cargo": r.get("titulo", ""),
                  "puntaje": r.get("afinidad", ""), "link": url}
        registros.append(oferta)
        boton = boton_feedback_html(link_formulario(fb_cfg, oferta)) if fb_cfg else ""
        color = "#1a7f37" if r["afinidad"] >= 75 else ("#9a6700" if r["afinidad"] >= 60 else "#57606a")
        filas += f"""
        <div style="border:1px solid #d0d7de;border-radius:8px;padding:14px;margin-bottom:12px;font-family:Arial,sans-serif;">
          <div style="font-size:16px;font-weight:bold;">
            <a href="{url}" style="color:#0a66c2;text-decoration:none;">{r['titulo']}</a>
            <span style="float:right;color:{color};">{r['afinidad']}/100</span>
          </div>
          <div style="color:#57606a;font-size:13px;margin:4px 0;">
            {r['empresa']} · {r['ubicacion']} · {r['modalidad']} · {r['seniority']}
          </div>
          <div style="font-size:13px;margin:6px 0;">{r.get('resumen', '')}</div>
          <div style="font-size:13px;margin:6px 0;"><b>Requisitos:</b> {', '.join(r['requisitos_clave'])}</div>
          <div style="font-size:13px;">{r['justificacion']}</div>
          <div style="font-size:13px;margin-top:6px;"><b>Recomendación:</b> {r['recomendacion']}</div>
          <div style="margin-top:10px;">{boton}</div>
        </div>"""

    encabezado = encabezado_sheet_html(url_sheet) if url_sheet else ""  # link al Sheet

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:640px;margin:auto;">
      <h2>Resumen de empleos — {len(incluidos)} relevantes</h2>
      <p style="color:#57606a;font-size:13px;">{descartados} empleos quedaron bajo el umbral de {umbral} puntos.</p>
      {encabezado}
      {filas or '<p>No hubo empleos sobre el umbral hoy.</p>'}
    </body></html>"""
    return html, registros


def send_digest(html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Digest de empleos LinkedIn"
    msg["From"] = GMAIL_USER
    msg["To"] = CONFIG["email"]["destinatario_digest"]
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(CONFIG["email"]["smtp_server"], CONFIG["email"]["smtp_port"]) as s:
        s.starttls()
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)


# ---------------------------------------------------------------- MÉTRICAS

def ahora_chile():
    return datetime.now(ZoneInfo("America/Santiago"))


def registrar_metrica(fb, estado):
    """Escribe una fila en la pestaña Metricas. Nunca interrumpe el agente si falla."""
    if "feedback" not in CONFIG:
        return
    try:
        fb = fb or FeedbackSheet(CONFIG["feedback"])
        t = ahora_chile()
        costo = (STATS["tokens_entrada"] * PRECIO_ENTRADA_MTOK + STATS["tokens_salida"] * PRECIO_SALIDA_MTOK) / 1_000_000
        datos = dict(STATS)
        datos.update({
            "fecha": t.strftime("%Y-%m-%d"),
            "hora": t.strftime("%H:%M"),
            "tipo_ejecucion": {"schedule": "programada", "workflow_dispatch": "manual"}.get(
                os.environ.get("GITHUB_EVENT_NAME", ""), os.environ.get("GITHUB_EVENT_NAME", "local")),
            "estado": estado[:200],
            "umbral": CONFIG["umbral_minimo_para_incluir"],
            "costo_usd": round(costo, 4),
        })
        fb.registrar_metrica(datos)
        print(f"Métricas: {estado} (tokens {STATS['tokens_entrada']}+{STATS['tokens_salida']}, US${costo:.4f}).")
    except Exception as e:
        print(f"Aviso: no se pudieron registrar las métricas ({e})")


def filas_analizadas(resultados, jobs_by_id, umbral):
    hoy = ahora_chile().strftime("%Y-%m-%d")
    return [{
        "job_id": r["id"], "fecha_analisis": hoy, "empresa": r["empresa"], "cargo": r["titulo"],
        "puntaje": r["afinidad"], "califica": "Sí" if r["afinidad"] >= umbral else "No",
        "recomendacion": r["recomendacion"], "modalidad": r["modalidad"], "seniority": r["seniority"],
        "ubicacion": r["ubicacion"], "justificacion": r["justificacion"],
        "link": jobs_by_id.get(r["id"], {}).get("url", f"https://www.linkedin.com/jobs/view/{r['id']}"),
    } for r in resultados]


# ---------------------------------------------------------------- MAIN

def main():
    faltantes = [v for v in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY") if not os.environ.get(v)]
    if faltantes:
        raise SystemExit(f"Faltan variables de entorno: {', '.join(faltantes)}")

    print("Leyendo alertas de LinkedIn...")
    html_bodies, email_ids = fetch_alert_emails()
    STATS["emails_alerta"] = len(email_ids)
    if not html_bodies:
        print("No hay emails de alertas en los últimos días. Fin.")
        registrar_metrica(None, "sin alertas")
        return

    jobs = filter_new(parse_jobs(html_bodies))
    STATS["ofertas_nuevas"] = len(jobs)
    print(f"{len(jobs)} empleos nuevos encontrados.")
    if not jobs:
        mark_emails_processed(email_ids)
        registrar_metrica(None, "sin ofertas nuevas")
        return

    print("Descargando descripciones de los empleos...")
    jobs = fetch_descriptions(jobs)
    STATS["descripciones_ok"] = sum(1 for j in jobs if j.get("descripcion"))

    # sincroniza el Form y arma el contexto. Si Google falla, el digest sigue igual.
    fb, contexto_feedback = None, ""
    if "feedback" in CONFIG:
        try:
            fb = FeedbackSheet(CONFIG["feedback"])
            n = fb.sincronizar_formulario()
            registros_fb = fb.leer_feedback()
            STATS["feedback_total"] = len(registros_fb)
            contexto_feedback = contexto_para_prompt(registros_fb, ruta_aprendizajes=str(BASE_DIR / "aprendizajes.md"))
            print(f"Feedback: {n} respuestas nuevas del Form, {len(registros_fb)} ofertas con feedback.")
        except Exception as e:
            print(f"Aviso: feedback no disponible ({e})")

    print("Analizando con Claude...")
    resultados = analyze_jobs(jobs, contexto_feedback)

    umbral = CONFIG["umbral_minimo_para_incluir"]
    puntajes = [r["afinidad"] for r in resultados]
    STATS["analizadas"] = len(resultados)
    STATS["califican"] = sum(1 for p in puntajes if p >= umbral)
    STATS["no_califican"] = len(resultados) - STATS["califican"]
    STATS["puntaje_promedio"] = round(sum(puntajes) / len(puntajes), 1) if puntajes else ""
    STATS["puntaje_max"] = max(puntajes) if puntajes else ""

    jobs_by_id = {j["id"]: j for j in jobs}
    html, ofertas_del_digest = build_digest(resultados, jobs_by_id, fb.url() if fb else None)
    send_digest(html)
    save_seen(jobs)

    # registra en el Sheet las ofertas enviadas (solo tras envío exitoso)
    if fb:
        try:
            n = fb.registrar_ofertas(ofertas_del_digest)
            print(f"Sheet: {n} ofertas registradas.")
        except Exception as e:
            print(f"Aviso: no se pudo registrar en el Sheet ({e})")
        # todas las ofertas analizadas, califiquen o no
        try:
            n = fb.registrar_analizadas(filas_analizadas(resultados, jobs_by_id, umbral))
            print(f"Analizadas: {n} ofertas registradas ({STATS['califican']} califican, {STATS['no_califican']} no).")
        except Exception as e:
            print(f"Aviso: no se pudo registrar el historial de analizadas ({e})")

    mark_emails_processed(email_ids)
    print("Digest enviado.")
    registrar_metrica(fb, "digest enviado")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        registrar_metrica(None, f"error: {e}")
        sys.exit(1)
