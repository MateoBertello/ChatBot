# Chatwoot Agent Bot con Claude (WhatsApp, multi-cliente)

Servicio en Python (FastAPI) que funciona como **Agent Bot de Chatwoot**: recibe los
mensajes entrantes de WhatsApp, responde con Claude usando la base de conocimiento de
cada negocio y deriva a una persona cuando hace falta. Un único deploy atiende a
varios clientes (uno por cuenta de Chatwoot).

## Cómo funciona

```
WhatsApp ─▶ Chatwoot ──(webhook message_created)──▶ POST /webhook
                                                     │ valida el token, filtra y responde 200 al instante
                                                     ▼ (en segundo plano)
                         carga clients/<account_id>.yaml
                         verifica que la conversación no esté "open" (tomada por un humano)
                         trae los últimos 20 mensajes de la conversación
                         llama a Claude con tool use:
                           • responder(texto)
                           • derivar_a_humano(motivo)
                           • registrar_interes_de_compra(producto, datos)
                         ejecuta las herramientas contra la API de Chatwoot
```

- Solo se procesan eventos `message_created` con `message_type: incoming` (y no privados).
  Todo lo demás responde `{"status": "ignored"}`.
- **No responde si la conversación está en estado `open`**: se chequea al recibir el
  webhook, antes de llamar a Claude y otra vez justo antes de enviar la respuesta (por si
  un agente la tomó mientras el modelo pensaba).
- **Herramientas:**
  - `responder(texto)`: envía el mensaje al cliente.
  - `derivar_a_humano(motivo)`: deja una nota privada con el motivo, avisa al cliente que
    lo atiende una persona (con un mensaje distinto si está fuera del horario de atención
    humana) y cambia el estado a `open`.
  - `registrar_interes_de_compra(producto, datos)`: agrega la etiqueta `venta`
    (conservando las existentes) y una nota privada con los datos del potencial cliente.
- El prompt limita al agente a los temas del negocio (redirige con amabilidad los ajenos),
  le prohíbe inventar precios, stock o políticas, y le indica derivar cuando no sabe.
- **Agrupa mensajes seguidos**: en WhatsApp la gente escribe en varios mensajes cortos.
  El bot espera `DEBOUNCE_SECONDS` (2 s por defecto) y responde una sola vez al último.
- **Deduplica** reintentos de Chatwoot por id de mensaje.
- **Ante errores** (API de Claude caída, respuesta inválida, rechazo del modelo) el bot
  deriva la conversación a una persona en lugar de dejar al cliente sin respuesta, y
  registra el error en los logs.

### Notas de voz, imágenes y PDFs

- **Notas de voz:** se transcriben con una API compatible con OpenAI (Groq, OpenAI o un
  servidor Whisper propio, ver `TRANSCRIPTION_*`) y Claude recibe el texto. Si Chatwoot ya
  trae su propia transcripción (`transcribed_text`), se usa esa y no se paga otra.
  Las transcripciones quedan en memoria, así que cada audio se transcribe una sola vez.
- **Imágenes** (JPEG, PNG, GIF, WebP, hasta 5 MB) **y PDFs** (hasta 10 MB): se descargan
  de Chatwoot y Claude los ve directamente.
- **Solo se procesan los adjuntos de los mensajes que el bot está por responder.** Los
  anteriores aparecen como texto ("[El cliente envió una imagen]"), así no se pagan
  tokens de imagen en cada turno. Como máximo `MEDIA_MAX_PER_RUN` imágenes/PDFs por
  respuesta (4 por defecto).
- Ubicaciones y contactos compartidos se le pasan a Claude como texto. Los videos no se
  procesan.
- Por seguridad, el bot descarga adjuntos solo desde el host de `CHATWOOT_URL` (y los de
  `MEDIA_ALLOWED_HOSTS`); sigue las redirecciones a S3/almacenamiento.
- El prompt le indica **no confirmar pagos a partir de un comprobante**: agradece y deriva
  para que una persona lo verifique.

### Consumo por cliente y límites

- Cada llamada a Claude, cada transcripción y cada mensaje atendido se registran en
  `USAGE_FILE` (JSON por línea) con el `account_id`, los tokens y un **costo estimado** en
  USD con precios de lista (tabla `PRICES_PER_MTOK` en `app/usage.py`).
- `GET /usage` devuelve los totales por cuenta (requiere `ADMIN_TOKEN`):

  ```bash
  curl -H "Authorization: Bearer $ADMIN_TOKEN" "https://bot.tudominio.com/usage?mes=2026-10"
  curl -H "Authorization: Bearer $ADMIN_TOKEN" "https://bot.tudominio.com/usage?mes=2026-10&account_id=1"
  ```

  Devuelve, por cuenta: mensajes atendidos, derivaciones, ventas registradas, mensajes
  limitados, llamadas y tokens de Claude, costo estimado y transcripciones.
- **Límites:** como máximo `RATE_LIMIT_CONVERSATION_PER_HOUR` respuestas automáticas por
  conversación por hora (20) y `RATE_LIMIT_ACCOUNT_PER_DAY` por cuenta cada 24 h (1000).
  Cada cliente puede pisarlos en su YAML (`limites`). Al superarlos, la conversación se
  deriva a una persona con una nota que explica el motivo. `0` = sin límite.

### Detalles del uso de Claude

- Modelo por defecto `claude-opus-5` (configurable con `CLAUDE_MODEL`), con *adaptive
  thinking* y `effort` `medium` (buen equilibrio latencia/calidad para chat; se puede
  cambiar con `CLAUDE_EFFORT`).
- **Fallback del lado del servidor activado** (`fallbacks: "default"`, beta
  `server-side-fallback-2026-07-01`): si el clasificador de seguridad rechaza un pedido,
  la API lo reintenta automáticamente en el modelo de respaldo recomendado. Si aun así se
  rechaza, el bot deriva a un humano. Se desactiva con `CLAUDE_FALLBACKS=false`
  (necesario si usás un modelo que no lo soporta).
- **Prompt caching**: las reglas y la base de conocimiento de cada cliente van en un bloque
  de sistema cacheado; la fecha/hora y la disponibilidad de humanos van en un bloque aparte
  para no invalidar la caché.
- Loop de herramientas manual: termina apenas el bot le envió algo al cliente, para no
  gastar una llamada extra.

## Estructura

```
app/
  main.py           FastAPI: /webhook, /usage, /health y validación del secreto
  service.py        filtrado de eventos, deduplicado, agrupación, límites y orquestación
  agent.py          llamada a Claude, herramientas y conversión del historial
  media.py          notas de voz, imágenes, PDFs y otros adjuntos
  transcription.py  cliente de transcripción (API compatible con OpenAI)
  usage.py          registro de consumo y costo estimado por cuenta
  limits.py         límites de respuestas (ventana deslizante)
  prompts.py        prompt de sistema a partir del YAML del cliente
  clients.py        carga/validación de clients/<account_id>.yaml y horario humano
  chatwoot.py       cliente de la API de Chatwoot
  config.py         variables de entorno
  simulador/        simulador local (python -m app.simulador)
clients/1.yaml  cliente de ejemplo (Panadería La Espiga, account_id 1)
tests/          tests con pytest
```

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | sí, o `OPENROUTER_API_KEY` | API key de Anthropic. |
| `OPENROUTER_API_KEY` | alternativa | Usa Claude a través de OpenRouter (ver abajo). |
| `CHATWOOT_URL` | sí | URL base de Chatwoot, ej. `https://chatwoot.tudominio.com`. |
| `CHATWOOT_BOT_TOKEN` | sí | Access token del Agent Bot. |
| `WEBHOOK_SECRET` | sí | Secreto compartido para validar los webhooks. |
| `CHATWOOT_API_TOKEN` | recomendada | Token de un usuario administrador, **solo para leer el historial** (ver abajo). |
| `CLIENTS_DIR` | no | Carpeta de configuraciones. Por defecto `clients` (en Docker, `/clients`). |
| `CLAUDE_MODEL` | no | Por defecto `claude-opus-5`. |
| `CLAUDE_EFFORT` | no | `low`, `medium` (por defecto), `high`, `xhigh`, `max`. Vacío = no se envía. |
| `CLAUDE_THINKING` | no | `adaptive` (por defecto) o vacío para no enviarlo. |
| `CLAUDE_MAX_TOKENS` | no | Por defecto `16000`. |
| `CLAUDE_FALLBACKS` | no | `true` (por defecto) / `false`. |
| `HISTORY_LIMIT` | no | Mensajes de contexto. Por defecto `20`. |
| `DEBOUNCE_SECONDS` | no | Espera para agrupar mensajes seguidos. Por defecto `2`. |
| `MAX_AGENT_STEPS` | no | Máximo de llamadas a Claude por mensaje. Por defecto `6`. |
| `LOG_LEVEL` | no | Por defecto `INFO`. |
| `TRANSCRIPTION_PROVIDER` | no | `none` (por defecto), `groq`, `openai` o `custom`. |
| `TRANSCRIPTION_API_KEY` | si hay proveedor | API key del proveedor de transcripción. |
| `TRANSCRIPTION_MODEL` | no | Por defecto `whisper-large-v3-turbo` (Groq) o `gpt-4o-mini-transcribe` (OpenAI). |
| `TRANSCRIPTION_BASE_URL` | con `custom` | URL base de una API compatible con OpenAI, ej. `http://whisper:8000/v1`. |
| `TRANSCRIPTION_LANGUAGE` | no | Idioma de los audios. Por defecto `es`. |
| `MEDIA_MAX_PER_RUN` | no | Imágenes/PDFs enviados a Claude por respuesta. Por defecto `4`. |
| `MEDIA_ALLOWED_HOSTS` | no | Hosts extra (separados por coma) desde donde descargar adjuntos. Necesario si el `FRONTEND_URL` de Chatwoot es distinto de `CHATWOOT_URL`. |
| `USAGE_FILE` | no | Archivo de consumo. Por defecto `data/usage.jsonl` (en Docker, `/data/usage.jsonl`). Vacío = no registrar. |
| `ADMIN_TOKEN` | no | Habilita `GET /usage`. Vacío = endpoint deshabilitado. |
| `RATE_LIMIT_CONVERSATION_PER_HOUR` | no | Respuestas automáticas por conversación por hora. Por defecto `20`. |
| `RATE_LIMIT_ACCOUNT_PER_DAY` | no | Respuestas automáticas por cuenta cada 24 h. Por defecto `1000`. |

### Usar Claude a través de OpenRouter

OpenRouter expone la misma API de mensajes de Anthropic, así que el bot funciona sin
cambios de código:

```
OPENROUTER_API_KEY=sk-or-...
CLAUDE_MODEL=anthropic/claude-haiku-5.5
```

- Los IDs de modelo usan el formato de OpenRouter (`anthropic/claude-haiku-5.5`,
  `anthropic/claude-opus-5`, `anthropic/claude-sonnet-5`), no el de Anthropic.
- Con OpenRouter, por defecto no se envían el `effort`, el `thinking` ni el fallback de
  modelo (es una beta de Anthropic): el modelo usa sus valores por defecto. Haiku 5.5, por
  ejemplo, ya piensa de forma adaptativa con esfuerzo medio. Si OpenRouter los acepta para
  tu modelo, podés activarlos con `CLAUDE_EFFORT`, `CLAUDE_THINKING=adaptive` y
  `CLAUDE_FALLBACKS=true`.
- `LLM_BASE_URL` permite apuntar a otro gateway compatible con la API de Anthropic.
- El costo estimado de `/usage` reconoce los IDs de OpenRouter, pero lo que te cobra
  OpenRouter puede diferir (comisiones, otros proveedores); el valor real está en su panel.

> **Importante sobre el historial:** Chatwoot no permite que un token de Agent Bot liste
> los mensajes de una conversación (sí puede enviar mensajes, cambiar el estado y manejar
> etiquetas). Para que el bot tenga los últimos 20 mensajes como contexto, definí
> `CHATWOOT_API_TOKEN` con el *access token* de un usuario administrador (Chatwoot →
> Perfil → Configuración del perfil → Token de acceso). Ese usuario tiene que pertenecer a
> todas las cuentas que atiende el bot; si no, usá tokens por cliente (`api_token_env`).
> Sin ese token el bot funciona igual, pero responde viendo solo el último mensaje (y lo
> advierte en los logs).

## Configurar un cliente

Cada cuenta de Chatwoot tiene su archivo `clients/<account_id>.yaml` (el `account_id` es
el número que aparece en la URL: `/app/accounts/<account_id>/...`). Tomá como base
[`clients/1.yaml`](clients/1.yaml):

```yaml
negocio:
  nombre: "Mi Negocio"
  descripcion: "Qué hace el negocio"
  tono: "cálido y cercano, tuteando con vos"
  idioma: español

prompt_sistema: |          # instrucciones específicas del negocio (opcional)
  - ...

base_conocimiento:
  faqs:
    - pregunta: ...
      respuesta: ...
  productos:               # admite campos extra (variantes, medidas, stock, etc.)
    - nombre: ...
      precio: 1000
      moneda: ARS
  horarios: |
    Lunes a viernes de 9 a 18.
  politicas:
    - ...

atencion_humana:
  zona_horaria: America/Argentina/Buenos_Aires
  horario:                 # días sin rangos = sin atención humana
    lunes: ["09:00-13:00", "15:00-19:00"]
    sabado: ["09:00-13:00"]
  mensaje_derivacion: "..."
  mensaje_derivacion_fuera_de_horario: "..."

etiqueta_venta: venta      # opcional

limites:                   # opcional; pisa RATE_LIMIT_* (0 = sin límite)
  respuestas_por_conversacion_por_hora: 20
  respuestas_por_dia: 500

chatwoot:                  # opcional: tokens propios de este cliente
  bot_token_env: MI_NEGOCIO_BOT_TOKEN
  api_token_env: MI_NEGOCIO_API_TOKEN
```

- El bot **solo informa stock si la base de conocimiento lo dice explícitamente**; si no
  está, deriva.
- Los archivos se recargan solos cuando cambian: no hace falta reiniciar.
- Si llega un mensaje de una cuenta sin archivo, se ignora y se registra un warning.

## Deploy

### Con Docker

```bash
docker build -t chatwoot-claude-bot .

docker run -d --name chatwoot-bot --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/clients:/clients:ro \
  -v chatwoot-bot-data:/data \
  chatwoot-claude-bot
```

Copiá `.env.example` a `.env` y completalo. Montar `clients/` como volumen permite agregar
o editar clientes sin reconstruir la imagen.

Ejemplo con docker compose (por ejemplo, junto a Chatwoot en el mismo servidor):

```yaml
services:
  chatwoot-bot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./clients:/clients:ro
      - bot-data:/data
    ports:
      - "8000:8000"

volumes:
  bot-data:
```

Recomendaciones:

- Exponé el servicio por **HTTPS** detrás de un reverse proxy (Nginx, Caddy, Traefik) o
  en una plataforma como Railway, Render, Fly.io o Cloud Run (el contenedor escucha en
  `$PORT`, por defecto 8000).
- Usá **una sola instancia/worker**: el deduplicado, la agrupación de mensajes, los
  límites y la caché de transcripciones se guardan en memoria (se reinician con el
  servicio). El registro de consumo sí persiste en `/data`.
- `GET /health` sirve como health check.
- Si Chatwoot corre en el mismo host, el bot debe poder alcanzar `CHATWOOT_URL` y Chatwoot
  debe poder alcanzar la URL del bot.

### Sin Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a && source .env && set +a
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Registrar el Agent Bot en Chatwoot

La URL del webhook lleva el secreto como parámetro:

```
https://bot.tudominio.com/webhook?token=<WEBHOOK_SECRET>
```

### Opción A — un bot por cuenta (desde la interfaz)

1. En Chatwoot, entrá a la cuenta del cliente como administrador.
2. **Configuración → Bots → Agregar bot**.
3. Completá nombre (ej. "Asistente IA"), descripción y **Webhook URL** con la URL de arriba.
4. Guardá y copiá el **access token** del bot.
   - Si es el único cliente, usalo como `CHATWOOT_BOT_TOKEN`.
   - Si tenés varios clientes, cada bot tiene su propio token: guardalo en una variable
     (ej. `ESPIGA_BOT_TOKEN`) y referenciala en el YAML con `chatwoot.bot_token_env`.
5. Conectá el bot al inbox de WhatsApp: **Configuración → Inboxes → (tu inbox de
   WhatsApp) → Configuración del bot**, elegí el bot y guardá.

También se puede hacer por API con el token de un administrador:

```bash
# Crear el bot (la respuesta incluye id y access_token)
curl -X POST "$CHATWOOT_URL/api/v1/accounts/<account_id>/agent_bots" \
  -H "api_access_token: <token_admin>" -H "Content-Type: application/json" \
  -d '{"name": "Asistente IA", "outgoing_url": "https://bot.tudominio.com/webhook?token=<WEBHOOK_SECRET>"}'

# Conectarlo al inbox
curl -X POST "$CHATWOOT_URL/api/v1/accounts/<account_id>/inboxes/<inbox_id>/set_agent_bot" \
  -H "api_access_token: <token_admin>" -H "Content-Type: application/json" \
  -d '{"agent_bot": <agent_bot_id>}'
```

### Opción B — un bot global para todos los clientes (Chatwoot self-hosted)

1. Entrá al panel de super admin: `https://chatwoot.tudominio.com/super_admin`.
2. **Agent Bots → New agent bot**, con la Webhook URL de arriba y sin cuenta asociada.
3. Copiá su access token en `CHATWOOT_BOT_TOKEN`: sirve para todas las cuentas.
4. En cada cuenta, conectá el bot al inbox de WhatsApp como en el paso 5 de la opción A.

### Qué pasa después

- Con un bot conectado, las conversaciones nuevas del inbox quedan en estado **pendiente**
  (`pending`) y las atiende el bot.
- Cuando el bot deriva, la conversación pasa a **abierta** (`open`) y aparece en la
  bandeja de los agentes con la nota privada del motivo. Desde ese momento el bot no
  vuelve a responder en esa conversación.
- Para devolverle una conversación al bot, un agente puede ponerla otra vez en pendiente.
- Las conversaciones con interés de compra quedan con la etiqueta `venta` (creala en
  **Configuración → Etiquetas** si querés asignarle un color o usarla en filtros).

### Firma de Chatwoot (opcional)

Las versiones recientes de Chatwoot firman los webhooks de bots con
`X-Chatwoot-Signature` (HMAC-SHA256 con el *secret* del bot). El servicio acepta esa firma
si `WEBHOOK_SECRET` coincide con el secret del bot. Con varios bots (un secret distinto por
cuenta) es más simple usar el `?token=` en la URL, que funciona siempre.

## Probar localmente

### Simulador (sin Chatwoot, WhatsApp ni servidor)

Para probar cómo responde el bot con la configuración de cada cliente, solo hace falta
Python 3.11+ y una API key de Anthropic:

```bash
python -m venv .venv && source .venv/bin/activate     # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...                    # o ponela en un archivo .env
# con OpenRouter, en lugar de la línea anterior:
#   export OPENROUTER_API_KEY=sk-or-...
#   export CLAUDE_MODEL=anthropic/claude-haiku-5.5
python -m app.simulador
```

Se abre el navegador en http://127.0.0.1:8080 con dos paneles:

- **El celular del cliente:** escribís como si fueras un cliente de WhatsApp, con
  sugerencias de mensajes para probar. Podés adjuntar fotos, PDFs y audios (los audios
  se transcriben si configurás `TRANSCRIPTION_PROVIDER`).
- **Lo que ve tu equipo en Chatwoot:** el estado de la conversación, las etiquetas, las
  notas privadas del bot, los botones para tomarla, resolverla o devolvérsela al bot,
  una caja para responder como agente humano y el consumo de tokens con el costo estimado.

Corre el mismo código que en producción (servicio, agente, adjuntos, límites); solo
Chatwoot está simulado en memoria. Lee `clients/*.yaml` en cada mensaje, así que podés
editar el YAML y probar de nuevo sin reiniciar. Opciones: `--puerto 9000` y `--no-abrir`.
Usa las mismas variables de entorno que el bot (`CLAUDE_MODEL`, `CLAUDE_EFFORT`,
`DEBOUNCE_SECONDS`, etc.) y lee un `.env` en la carpeta actual si existe.

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Simular un mensaje entrante (con el servicio corriendo y `clients/1.yaml`):

```bash
curl -X POST "http://localhost:8000/webhook?token=$WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
        "event": "message_created",
        "id": 123,
        "message_type": "incoming",
        "private": false,
        "content": "Hola! ¿Cuánto sale la torta rogel?",
        "account": {"id": 1},
        "conversation": {"id": 45, "status": "pending"},
        "sender": {"name": "Ana", "type": "contact"}
      }'
# → {"status":"accepted"}; la respuesta se envía a la conversación 45 de Chatwoot.
```

Para exponer tu máquina a Chatwoot mientras desarrollás podés usar `ngrok http 8000` o
`cloudflared tunnel` y registrar esa URL en el bot.
