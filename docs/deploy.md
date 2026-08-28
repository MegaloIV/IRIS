# Desplegar Iris (fase 05)

El objetivo no es solo que Iris viva sin el portátil. Es también la mayor
optimización de latencia que queda: desde casa, un `SELECT 1` contra Supabase
tarda ~208 ms, y eso es la velocidad de la luz más unos saltos de red, no código
mejorable. Una VM en la misma región que la base de datos baja las seis lecturas
de un turno de 1,2 s a algo imperceptible.

Todo va en Docker Compose sobre una VM normal — nada específico de Google. El
día que se acabe el crédito puedes quedarte, mudarte a otro VPS o volver a local
sin migrar nada.

## Antes de empezar

- Un dominio apuntando (registro A) a la IP de la VM. Caddy saca el certificado
  solo, pero necesita que el DNS ya resuelva.
- El proyecto ya commiteado y accesible desde la VM.
- Haber probado en local. Depurar la VM y un refactor a la vez es el doble de
  trabajo, y cuesta distinguir cuál de los dos falla.

## 1. La VM

`e2-small` (2 GB) llega; `e2-medium` (4 GB) quita toda duda sobre torch, que se
usa para los embeddings. **Créala en la misma región que tu proyecto de
Supabase** — es de donde sale la mejora de latencia, y cambiarlo después
significa recrear la instancia.

Abre solo 80 y 443. El puerto 8000 no se expone: en el compose el servicio `iris`
usa `expose`, no `ports`, así que solo lo alcanza Caddy por la red interna de
Docker.

Instala Docker y el plugin de compose, y clona el repo.

## 2. El `.env` del servidor

```bash
cp .env.example .env
```

Lo que **no** puede faltar en modo servidor:

| Variable | Por qué |
|---|---|
| `IRIS_MODE=server` | Sin esto arranca la UI y falla: no hay pantalla |
| `IRIS_DOMAIN` | Caddy lo usa para el TLS, y de ahí sale la URL del webhook |
| `IRIS_AGENT_TOKEN` | **El mismo** que en el portátil, o el enlace no se abre |
| `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_KEY` | La memoria vive fuera de la VM |
| `GROQ_API_KEYS` | Conversación, análisis y transcripción |

Genera el token una vez y cópialo a las dos máquinas:

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

Si `IRIS_DOMAIN` está vacío, Caddy cae a `iris.localhost` y no emite certificado
real. Iris arranca igual y avisa por log de que el webhook no se registra — pero
el bot no recibirá nada.

## 3. Levantar

```bash
docker compose up -d --build
docker compose logs -f iris
```

La primera construcción tarda: hornea el modelo de embeddings en la imagen para
que el contenedor arranque sin salir a HuggingFace.

En el log deben aparecer, por este orden:

```
[Iris] Iniciada — Mood: ... | Trust: ... | Energy: ...
[Telegram] Webhook montado en /tg/webhook
[Telegram] Webhook registrado (con secreto): https://<dominio>/tg/webhook
[Servidor] Iris escuchando en 0.0.0.0:8000
```

**Si falta la tercera línea, el bot no responderá.** Es la que registra el
webhook contra el dominio fijo, y es exactamente lo que no ocurría antes.

Comprueba desde fuera:

```bash
curl https://<dominio>/health
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

En `getWebhookInfo`, `url` debe ser tu dominio con `/tg/webhook`, y
`has_custom_certificate` debe ser `false`. Si `last_error_message` dice algo,
ahí está el problema.

## 4. El portátil

En el `.env` del portátil, sin tocar nada más:

```
IRIS_MODE=client
IRIS_SERVER_URL=https://<dominio>
IRIS_AGENT_TOKEN=<el mismo del servidor>
```

Instala solo lo que necesita ese lado:

```bash
pip install -r requirements.client.txt
```

`python main.py` abre el avatar y el enlace. En el log del servidor debe salir
que el agente se conectó y qué capacidades ofrece (`claude`, `desktop`).

A partir de ahí: el cerebro está en la VM, y el portátil pone los ojos y las
manos. Con el portátil apagado Iris sigue conversando, recordando y escribiéndote
por Telegram; lo que no puede es ver imágenes, tocar archivos ni el escritorio —
y lo dice ella, no da un error.

## 5. El presupuesto

Los 300 $ son crédito de prueba a 90 días. El trial **no cobra solo**: al
agotarse el crédito o cumplirse el plazo, los recursos se detienen hasta que
actives una cuenta de pago. Aun así, pon una alerta de presupuesto en
*Billing → Budgets & alerts* — enterarte por correo es mejor que enterarte
porque Iris dejó de contestar.

Coste esperado: `e2-small` ~13 $/mes, `e2-medium` ~27 $/mes, ambos contra el
crédito. Supabase, Groq y la suscripción de Claude no cambian.

## Cuando algo no va

**El bot no responde.** Mira `getWebhookInfo`. Si la `url` está vacía o apunta a
un `trycloudflare.com`, es que el registro no corrió: revisa que `IRIS_DOMAIN`
esté puesto y que `TELEGRAM_ENABLED=true`.

**Responde 403 a Telegram.** El secreto no cuadra. Pasa si cambiaste
`TELEGRAM_BOT_TOKEN` sin volver a registrar el webhook, porque por defecto el
secreto se deriva del token. Reinicia el contenedor y se registra de nuevo.

**El portátil no se conecta.** Casi siempre es que los `IRIS_AGENT_TOKEN` no
coinciden. El servidor rechaza el handshake y lo dice en su log.

**Caddy no saca certificado.** El DNS todavía no resuelve a la IP de la VM, o el
80 está cerrado — Let's Encrypt necesita ese puerto para validar.

**Todo funciona pero va lento.** Comprueba que la VM y Supabase están en la misma
región. Si no lo están, sigues pagando el viaje de red que venías a quitar.
