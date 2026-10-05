"""Integración con Google Sheets y Google Forms para el feedback de postulaciones.

Pestañas que administra:
  - Ofertas:     ofertas enviadas en el digest + feedback del candidato (postuló, estado, comentario).
  - Analizadas:  todas las ofertas evaluadas por Claude, califiquen o no.
  - Metricas:    una fila por ejecución (volumen, puntajes, tokens y costo).

Flujo del feedback:
  1. Cada oferta del digest trae un botón que abre un Google Form con el ID ya completado.
  2. Las respuestas del Form se copian a la pestaña "Ofertas" al inicio de cada ejecución.
  3. Ese historial se usa como contexto al puntuar ofertas nuevas y en la reflexión semanal.

Requiere el secret GOOGLE_SERVICE_ACCOUNT_JSON (ver docs/CONFIGURACION.md).
"""
import json
import os
from datetime import date
from urllib.parse import urlencode

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

COLUMNAS = [
    "job_id", "fecha_digest", "empresa", "cargo", "puntaje", "link",
    "postule", "estado", "comentario", "actualizado",
]
COLS_FEEDBACK = ["postule", "estado", "comentario"]

# Pestaña "Analizadas": todas las ofertas que Claude evaluó, califiquen o no.
COLUMNAS_ANALIZADAS = [
    "job_id", "fecha_analisis", "empresa", "cargo", "puntaje", "califica", "recomendacion",
    "modalidad", "seniority", "ubicacion", "justificacion", "link",
]
# Pestaña "Metricas": una fila por ejecución del agente.
COLUMNAS_METRICAS = [
    "fecha", "hora", "tipo_ejecucion", "estado", "emails_alerta", "ofertas_nuevas", "descripciones_ok",
    "analizadas", "califican", "no_califican", "umbral", "puntaje_promedio", "puntaje_max",
    "tokens_entrada", "tokens_salida", "costo_usd", "feedback_total",
]

OPCIONES_POSTULE = ["Si", "No", "No me interesa"]
OPCIONES_ESTADO = [
    "Postulado", "En Revision", "Entrevista", "Prueba Tecnica",
    "Rechazado", "Oferta", "Me retire",
]

# Títulos y opciones deben coincidir con las preguntas del Google Form. Los títulos se comparan por inicio.
PREG_JOB_ID = "ID oferta"
PREG_POSTULE = "¿Postulaste"
PREG_ESTADO = "Estado"
PREG_COMENTARIO = "¿Qué faltó"


def _cliente():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _col(idx):
    """Índice 0-based -> letra de columna (A, B, ...). Suficiente para < 26 columnas."""
    return chr(ord("A") + idx)


def _buscar(registro, titulo):
    """Busca en una fila del Form la columna cuyo título empieza por `titulo` (sin distinguir mayúsculas)."""
    t = titulo.strip().lower()
    for clave, valor in registro.items():
        if str(clave).strip().lower().startswith(t):
            return str(valor).strip()
    return ""


class FeedbackSheet:
    def __init__(self, cfg):
        """cfg = config["feedback"] de config.json."""
        self.cfg = cfg
        self.sh = _cliente().open_by_key(cfg["sheet_id"])
        self.ws = self._tracker()

    # ---------- preparación ----------
    def _tracker(self):
        nombre = self.cfg.get("pestana_ofertas", "Ofertas")
        try:
            ws = self.sh.worksheet(nombre)
        except gspread.WorksheetNotFound:
            ws = self.sh.add_worksheet(title=nombre, rows=1000, cols=len(COLUMNAS))
        if ws.row_values(1) != COLUMNAS:
            ws.update(values=[COLUMNAS], range_name="A1")
            ws.freeze(rows=1)
            self._validaciones(ws)
        return ws

    def _validaciones(self, ws):
        """Listas desplegables en las columnas de feedback."""
        def regla(col, opciones):
            idx = COLUMNAS.index(col)
            return {"setDataValidation": {
                "range": {"sheetId": ws.id, "startRowIndex": 1,
                          "startColumnIndex": idx, "endColumnIndex": idx + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": o} for o in opciones]},
                         "showCustomUi": True, "strict": False},
            }}
        self.sh.batch_update({"requests": [
            regla("postule", OPCIONES_POSTULE),
            regla("estado", OPCIONES_ESTADO),
        ]})

    # ---------- escritura ----------
    def registrar_ofertas(self, jobs):
        """Agrega al Sheet las ofertas del digest (sin duplicar por job_id).

        Cada job debe ser un dict con: job_id, empresa, cargo, puntaje, link.
        """
        existentes = set(self.ws.col_values(1)[1:])
        hoy = date.today().isoformat()
        filas = []
        for j in jobs:
            jid = str(j["job_id"])
            if jid in existentes:
                continue
            existentes.add(jid)
            filas.append([jid, hoy, j.get("empresa", ""), j.get("cargo", ""),
                          j.get("puntaje", ""), j.get("link", ""), "", "", "", ""])
        if filas:
            # RAW para que el job_id quede como texto y no se transforme en número.
            self.ws.append_rows(filas, value_input_option="RAW")
        return len(filas)

    def sincronizar_formulario(self):
        """Copia la última respuesta del Form de cada oferta a la pestaña Ofertas.

        Si una respuesta deja un campo vacío, se mantiene lo que ya había en el Sheet.
        """
        nombre = self.cfg.get("pestana_respuestas")
        if not nombre:
            return 0
        try:
            resp = self.sh.worksheet(nombre)
        except gspread.WorksheetNotFound:
            return 0

        ultimo = {}
        for r in resp.get_all_records(numericise_ignore=["all"]):
            jid = _buscar(r, PREG_JOB_ID)
            if jid:
                ultimo[jid] = r  # las filas vienen en orden de llegada: gana la más reciente

        if not ultimo:
            return 0

        datos = self.ws.get_all_values()
        i_post = COLUMNAS.index("postule")
        i_act = COLUMNAS.index("actualizado")
        updates = []
        for n_fila, fila in enumerate(datos[1:], start=2):
            fila = fila + [""] * (len(COLUMNAS) - len(fila))
            r = ultimo.get(fila[0])
            if not r:
                continue
            marca = str(list(r.values())[0])  # primera columna del Form = marca temporal
            if fila[i_act] == marca:
                continue
            nuevos = [
                _buscar(r, PREG_POSTULE) or fila[i_post],
                _buscar(r, PREG_ESTADO) or fila[i_post + 1],
                _buscar(r, PREG_COMENTARIO) or fila[i_post + 2],
                marca,
            ]
            updates.append({
                "range": f"{_col(i_post)}{n_fila}:{_col(i_act)}{n_fila}",
                "values": [nuevos],
            })
        if updates:
            self.ws.batch_update(updates, value_input_option="RAW")
        return len(updates)

    # ---------- historial de análisis y métricas ----------
    def _hoja(self, nombre, columnas):
        """Devuelve la pestaña `nombre`, creándola con encabezados si no existe."""
        try:
            ws = self.sh.worksheet(nombre)
        except gspread.WorksheetNotFound:
            ws = self.sh.add_worksheet(title=nombre, rows=2000, cols=len(columnas))
        if not ws.row_values(1):
            ws.update(values=[columnas], range_name="A1")
            ws.freeze(rows=1)
        return ws

    def registrar_analizadas(self, filas):
        """Agrega todas las ofertas analizadas por Claude (califiquen o no), sin duplicar por job_id."""
        ws = self._hoja(self.cfg.get("pestana_analizadas", "Analizadas"), COLUMNAS_ANALIZADAS)
        existentes = set(ws.col_values(1)[1:])
        nuevas = []
        for f in filas:
            if str(f["job_id"]) in existentes:
                continue
            existentes.add(str(f["job_id"]))
            nuevas.append([f.get(c, "") for c in COLUMNAS_ANALIZADAS])
        if nuevas:
            ws.append_rows(nuevas, value_input_option="RAW")
        return len(nuevas)

    def registrar_metrica(self, datos):
        """Agrega una fila con las métricas de una ejecución."""
        ws = self._hoja(self.cfg.get("pestana_metricas", "Metricas"), COLUMNAS_METRICAS)
        # RAW: los números viajan como números (no texto), así el punto decimal no se confunde
        # con separador de miles en un Sheet configurado para Chile.
        ws.append_row([datos.get(c, "") for c in COLUMNAS_METRICAS], value_input_option="RAW")

    # ---------- lectura ----------
    def leer_feedback(self):
        """Filas donde diste algún feedback."""
        filas = self.ws.get_all_records(numericise_ignore=["all"])
        return [f for f in filas if any(str(f.get(c, "")).strip() for c in COLS_FEEDBACK)]

    def url(self):
        return f"https://docs.google.com/spreadsheets/d/{self.cfg['sheet_id']}"


# ---------- helpers para el digest ----------
def link_formulario(cfg, job):
    """Link al Form con el ID y el nombre de la oferta ya completados."""
    base = cfg.get("form_url")
    if not base:
        return None
    params = {
        cfg["form_entry_job_id"]: str(job["job_id"]),
        cfg["form_entry_oferta"]: f"{job.get('cargo', '')} — {job.get('empresa', '')}",
    }
    return f"{base}?usp=pp_url&{urlencode(params)}"


def boton_feedback_html(url):
    if not url:
        return ""
    return (f'<a href="{url}" style="display:inline-block;padding:6px 12px;'
            f'background:#0a66c2;color:#fff;border-radius:4px;text-decoration:none;'
            f'font-size:13px;">📝 Dar feedback</a>')


def encabezado_sheet_html(url_sheet):
    return (f'<p style="font-size:13px;">Registro de postulaciones: '
            f'<a href="{url_sheet}">abrir Google Sheet</a></p>')


# ---------- contexto para el prompt de evaluación ----------
def contexto_para_prompt(registros, ruta_aprendizajes="aprendizajes.md", max_items=25):
    """Texto para agregar al prompt de puntuación. Devuelve "" si aún no hay nada."""
    partes = []

    if os.path.exists(ruta_aprendizajes):
        with open(ruta_aprendizajes, encoding="utf-8") as f:
            texto = f.read().strip()
        if texto:
            partes.append("## Aprendizajes de semanas anteriores\n" + texto)

    if registros:
        recientes = sorted(registros, key=lambda r: r.get("fecha_digest", ""), reverse=True)[:max_items]
        lineas = []
        for r in recientes:
            linea = (f"- {r.get('cargo')} en {r.get('empresa')} (puntaje agente: {r.get('puntaje')}): "
                     f"postuló={r.get('postule') or '?'}, estado={r.get('estado') or '?'}")
            if r.get("comentario"):
                linea += f", comentario: {r['comentario']}"
            lineas.append(linea)
        partes.append("## Feedback reciente del candidato sobre ofertas anteriores\n" + "\n".join(lineas))

    if not partes:
        return ""

    return ("\n\n".join(partes) +
            "\n\nUsa este historial para calibrar el puntaje: sube la afinidad de ofertas parecidas a las "
            "que avanzaron o interesaron, bájala en las parecidas a las descartadas por el candidato. "
            "Si una oferta pide algo que antes causó rechazo, menciónalo como brecha. "
            "No descartes una oferta solo por un parecido superficial.")
