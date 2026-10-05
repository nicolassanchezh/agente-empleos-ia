# Guía de configuración

Instalación completa del agente, desde cero. Toma entre 30 y 45 minutos, una sola vez.

| Parte | Obligatoria | Tiempo aprox. |
|---|---|---|
| 1. Repositorio y configuración | Sí | 5 min |
| 2. Alertas de LinkedIn | Sí | 5 min |
| 3. Gmail y Anthropic | Sí | 5 min |
| 4. Google Sheets y Form (feedback y métricas) | No, pero recomendada | 20 min |
| 5. Secrets y primera ejecución | Sí | 5 min |

Sin la parte 4 el agente funciona igual: envía el digest diario, pero sin botón de feedback, sin aprendizaje y sin métricas.

---

## 1. Repositorio y configuración

1. Crea un repositorio **privado** a partir de este: usa **Use this template** o descarga el ZIP (**Code → Download ZIP**) y sube los archivos a un repositorio privado nuevo. Debe ser privado porque `config.json` tendrá tu perfil y tus correos.
2. Copia `config.example.json` como `config.json` y complétalo:

| Campo | Qué poner |
|---|---|
| `perfil` | Tu perfil real: cargos buscados, años de experiencia, skills, industrias, modalidad. Mientras más específico, mejor el análisis. |
| `perfil.descartar_si` | Condiciones que limitan el puntaje a 30 como máximo (por ejemplo, `"seniority junior/trainee"`). Úsalas solo para descartes claros. |
| `rubrica_afinidad` | Pesos de cada criterio. Deben sumar 100. |
| `umbral_minimo_para_incluir` | Puntaje mínimo para que una oferta aparezca en el digest (por ejemplo, 50). |
| `modelo` | Modelo de Claude que se usará (por defecto `claude-sonnet-4-6`). |
| `email.remitentes_alertas` | Deja ambos remitentes de LinkedIn: las alertas llegan desde `jobalerts-noreply@linkedin.com`, y algunas desde `jobs-noreply@linkedin.com`. |
| `email.destinatario_digest` | El correo donde quieres recibir el digest. |
| `email.dias_busqueda` | Cuántos días hacia atrás buscar alertas (por defecto 2). |
| `feedback` | Se completa en la parte 4. Si no la usarás, elimina este bloque y desactiva el workflow **Reflexion semanal** (Actions → Reflexion semanal → ⋯ → Disable workflow). |

> En el editor web de GitHub, haz el commit **directo a `main`**. Por defecto GitHub sugiere crear una rama nueva, y los workflows siempre usan el código de `main`.

## 2. Alertas de LinkedIn

1. En LinkedIn, busca empleos con los filtros que quieras (cargo, ubicación, modalidad, seniority).
2. Activa **Alerta de empleo** con frecuencia **diaria** y notificación por **email**.
3. Puedes crear varias alertas: el agente procesa todas.

Qué ofertas llegan se ajusta en LinkedIn. Cómo se evalúan se ajusta en `config.json`.

## 3. Gmail y Anthropic

**Contraseña de aplicación de Gmail**

1. Activa la verificación en dos pasos en tu cuenta de Google.
2. Entra a myaccount.google.com → Seguridad → **Contraseñas de aplicaciones** y genera una.
3. Guárdala: es distinta a tu contraseña normal y es la única que usa el agente.

**API key de Anthropic**

Crea una en console.anthropic.com → API Keys.

## 4. Google Sheets y Form (feedback y métricas)

### 4.1 Cuenta de servicio de Google

1. En console.cloud.google.com, crea un proyecto.
2. **APIs y servicios → Biblioteca** → busca **Google Sheets API** → Habilitar.
3. **APIs y servicios → Credenciales → Crear credenciales → Cuenta de servicio**. No necesita roles.
4. Entra a la cuenta creada → **Claves → Agregar clave → JSON**. Se descarga un archivo.
5. Copia el email de la cuenta de servicio (termina en `iam.gserviceaccount.com`).

### 4.2 Google Sheet

1. Crea un Google Sheet vacío.
2. Copia su ID desde la URL: `docs.google.com/spreadsheets/d/`**`ESTE_ES_EL_ID`**`/edit`.
3. **Compartir** → pega el email de la cuenta de servicio → rol **Editor**.

No necesitas crear pestañas ni columnas. El agente crea `Ofertas`, `Analizadas` y `Metricas` en la primera ejecución.

### 4.3 Google Form

1. Crea un Form con estas preguntas, **en este orden, con estos títulos y estas opciones**. El código compara los títulos y las opciones, así que deben coincidir exactamente:

| Pregunta | Tipo | Opciones | Obligatoria |
|---|---|---|---|
| ID oferta | Respuesta corta | — | Sí |
| Oferta | Respuesta corta | — | No |
| ¿Postulaste | Opción múltiple | Si / No / No me interesa | No |
| Estado | Desplegable | Postulado / En Revision / Entrevista / Prueba Tecnica / Rechazado / Oferta / Me retire | No |
| ¿Qué faltó o qué comentario tienes? | Párrafo | — | No |

   Si prefieres otros títulos u opciones, cambia las constantes `PREG_*` y `OPCIONES_*` al inicio de `sheets.py`.

2. Pestaña **Respuestas → Vincular con Hojas de cálculo → Seleccionar hoja existente** → elige el Sheet anterior. Se crea una pestaña (normalmente "Respuestas de formulario 1"). Anota su nombre exacto.
3. Obtén los IDs de los campos para el enlace prellenado:
   - Menú ⋮ del Form → **Obtener vínculo prellenado**.
   - Escribe `TEST1` en "ID oferta" y `TEST2` en "Oferta" → **Obtener vínculo** → Copiar.
   - El enlace se ve así:
     `https://docs.google.com/forms/d/e/1FAIpQL.../viewform?usp=pp_url&entry.111111=TEST1&entry.222222=TEST2`
   - La parte hasta `viewform` es `form_url`, `entry.111111` es `form_entry_job_id` y `entry.222222` es `form_entry_oferta`.

### 4.4 Completar `config.json`

```json
"feedback": {
  "sheet_id": "ID_DEL_SHEET",
  "pestana_ofertas": "Ofertas",
  "pestana_respuestas": "Respuestas de formulario 1",
  "pestana_analizadas": "Analizadas",
  "pestana_metricas": "Metricas",
  "form_url": "https://docs.google.com/forms/d/e/1FAIpQL.../viewform",
  "form_entry_job_id": "entry.111111",
  "form_entry_oferta": "entry.222222",
  "minimo_registros_reflexion": 8,
  "enviar_reflexion_por_mail": true
}
```

`minimo_registros_reflexion` es la cantidad mínima de ofertas con feedback para que corra la reflexión semanal. Con menos casos, los patrones no son confiables.

## 5. Secrets y primera ejecución

### 5.1 Secrets

En el repositorio: **Settings → Secrets and variables → Actions → New repository secret**.

| Secret | Valor |
|---|---|
| `GMAIL_USER` | Tu dirección de Gmail |
| `GMAIL_APP_PASSWORD` | La contraseña de aplicación |
| `ANTHROPIC_API_KEY` | Tu API key de Anthropic |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | El contenido completo del archivo JSON de la cuenta de servicio (solo si usas la parte 4) |

Nunca subas el archivo JSON ni las claves al repositorio.

### 5.2 Primera ejecución

1. Pestaña **Actions → Digest diario de empleos → Run workflow**.
2. En el log del paso **Ejecutar agente** deberías ver algo como:

```
Leyendo alertas de LinkedIn...
12 empleos nuevos encontrados.
Descargando descripciones de los empleos...
  Descripciones obtenidas: 12/12
Feedback: 0 respuestas nuevas del Form, 0 ofertas con feedback.
Analizando con Claude...
  Analizando lote 1/2 (10 empleos)...
  Analizando lote 2/2 (2 empleos)...
Sheet: 7 ofertas registradas.
Analizadas: 12 ofertas registradas (7 califican, 5 no).
Digest enviado.
Métricas: digest enviado (tokens 38000+9000, US$0.2490).
```

3. Revisa el correo y el Sheet. Para probar la reflexión semanal antes de juntar 8 respuestas, baja temporalmente `minimo_registros_reflexion` a 1 y ejecuta **Reflexion semanal → Run workflow**.

Desde ahí corre solo de lunes a viernes.

---

## Uso diario

- **Dar feedback:** en el digest, botón **📝 Dar feedback** de cada oferta. El Form ya trae la oferta identificada.
- **Actualizar un proceso** (te llamaron, te rechazaron): vuelve a abrir el botón de esa oferta o edita la fila en la pestaña `Ofertas`. Gana la respuesta más reciente, y los campos vacíos no borran lo anterior.
- **Viernes:** llega por correo la reflexión semanal y se actualiza `aprendizajes.md`. Los cambios sugeridos a `config.json` los decides tú.

## Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| Hice cambios pero el workflow sigue con el código anterior | Se usó **Re-run jobs**, que repite la ejecución anterior | Usa **Run workflow**, que toma el código actual de `main` |
| Los cambios no tienen efecto | El commit quedó en otra rama | Haz el commit directo a `main` |
| `No hay emails de alertas en los últimos días` | No llegaron alertas o el remitente no coincide | Revisa `remitentes_alertas` y que las alertas lleguen a la casilla de `GMAIL_USER` |
| El digest llega horas más tarde de lo programado | GitHub Actions atrasa las ejecuciones programadas cuando tiene alta carga | Es normal en cuentas gratuitas. Los horarios de respaldo lo cubren; para mayor precisión, activa el workflow desde un cron externo |
| `Aviso: feedback no disponible (...)` | El Sheet no está compartido con la cuenta de servicio, la Sheets API no está habilitada o el secret está mal copiado | Revisa la parte 4.1, 4.2 y el secret `GOOGLE_SERVICE_ACCOUNT_JSON` |
| Las respuestas del Form no se reflejan en `Ofertas` | Los títulos u opciones del Form no coinciden con `sheets.py` | Ajusta el Form o las constantes `PREG_*` / `OPCIONES_*` |
| `Respuesta truncada en el lote N` | La respuesta de Claude superó el límite de tokens | Reduce `BATCH_SIZE` en `job_agent.py` |
| Fallo por `Internal server error` o `job was not acquired by Runner` | Incidente de GitHub Actions | Revisa githubstatus.com y vuelve a ejecutar cuando se resuelva |

## Ejecutar localmente (opcional)

```bash
pip install -r requirements.txt
export GMAIL_USER="tu-email@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export ANTHROPIC_API_KEY="sk-ant-..."
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat cuenta-de-servicio.json)"   # opcional
python job_agent.py
```

Ten en cuenta que una ejecución local envía el digest real y actualiza `seen_jobs.json`.
