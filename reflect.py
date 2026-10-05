"""Reflexión semanal: analiza el feedback de postulaciones y actualiza aprendizajes.md.

No modifica config.json: solo propone cambios. Tú decides cuáles aplicar.
Corre con .github/workflows/weekly.yml (viernes).
"""
import json
import os
import smtplib
import sys
from datetime import date
from email.mime.text import MIMEText

import anthropic

from sheets import FeedbackSheet

RUTA_APRENDIZAJES = "aprendizajes.md"

PROMPT = """Eres un coach de búsqueda de empleo analizando el historial de postulaciones de un candidato.

## Perfil y criterios actuales del agente (config.json)
{config}

## Aprendizajes de la semana anterior
{previos}

## Historial de ofertas con feedback del candidato
Columnas: cargo, empresa, puntaje que le dio el agente (0-100), si postuló, estado, comentario.
{historial}

Genera un documento en Markdown con estas secciones, en español, breve y concreto:

### Patrones de resultado
Dónde avanza y dónde lo rechazan (industria, seniority, tipo de cargo, skills pedidas). Indica cuántos casos respaldan cada patrón.

### Calibración del puntaje
Ofertas con puntaje alto que descartó o no le interesaron, y al revés. Qué criterio del agente está fallando.

### Brechas recurrentes
Skills o requisitos que aparecen repetidos en rechazos o comentarios, y una acción concreta para cada uno.

### Cambios sugeridos a config.json
Cambios puntuales (perfil, pesos de la rúbrica, umbral, condiciones de descarte), cada uno con su justificación. Son sugerencias: el candidato decide.

Reglas: no inventes patrones con menos de 2 casos; si la evidencia es poca, dilo. Máximo ~400 palabras."""


def cargar_config():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


def formatear(registros):
    return "\n".join(
        f"- {r.get('cargo')} | {r.get('empresa')} | {r.get('puntaje')} | "
        f"{r.get('postule') or '-'} | {r.get('estado') or '-'} | {r.get('comentario') or '-'}"
        for r in registros
    )


def enviar_mail(cfg, cuerpo):
    user = os.environ["GMAIL_USER"]
    email_cfg = cfg.get("email", {})
    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"] = f"Reflexión semanal de postulaciones — {date.today().isoformat()}"
    msg["From"] = user
    msg["To"] = email_cfg.get("destinatario_digest", user)
    with smtplib.SMTP(email_cfg.get("smtp_server", "smtp.gmail.com"), email_cfg.get("smtp_port", 587)) as s:
        s.starttls()
        s.login(user, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def main():
    cfg = cargar_config()
    fb_cfg = cfg["feedback"]
    fb = FeedbackSheet(fb_cfg)
    fb.sincronizar_formulario()
    registros = fb.leer_feedback()

    minimo = fb_cfg.get("minimo_registros_reflexion", 8)
    if len(registros) < minimo:
        print(f"Solo {len(registros)} ofertas con feedback (mínimo {minimo}). Se omite la reflexión.")
        return

    previos = "(ninguno)"
    if os.path.exists(RUTA_APRENDIZAJES):
        with open(RUTA_APRENDIZAJES, encoding="utf-8") as f:
            previos = f.read().strip() or "(ninguno)"

    config_sin_feedback = {k: v for k, v in cfg.items() if k not in ("feedback", "email")}
    prompt = PROMPT.format(
        config=json.dumps(config_sin_feedback, ensure_ascii=False, indent=2),
        previos=previos,
        historial=formatear(registros),
    )

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=cfg.get("modelo", "claude-sonnet-4-6"),  # mismo modelo que job_agent.py
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    texto = resp.content[0].text.strip()

    contenido = f"<!-- Generado automáticamente el {date.today().isoformat()} con {len(registros)} registros -->\n\n{texto}\n"
    with open(RUTA_APRENDIZAJES, "w", encoding="utf-8") as f:
        f.write(contenido)
    print(f"aprendizajes.md actualizado con {len(registros)} registros.")

    if fb_cfg.get("enviar_reflexion_por_mail", True):
        enviar_mail(cfg, texto)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error en la reflexión semanal: {e}", file=sys.stderr)
        raise
