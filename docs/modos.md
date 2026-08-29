# Los tres modos, y cómo pasar de uno a otro

Iris puede correr entera en una máquina o partida en dos. Lo decide **una
variable**, `IRIS_MODE`, y el resto del código no se entera: la costura está en
[core/executor.py](../core/executor.py) y quien llama nunca sabe dónde se ejecuta
lo que pide.

---

## Qué es cada uno

### `local` — todo en tu PC (por defecto)

Un solo proceso con todo dentro: el avatar, el micrófono, el agente, la memoria,
Telegram y el motor autónomo. Es como funcionaba antes de partirlo, y es lo que
tienes si no defines nada.

**Cuándo:** desarrollo, y siempre que no te importe que Iris solo exista mientras
tu PC esté encendido.

**Necesita:** `pip install -r requirements.txt`

### `server` — solo el cerebro

Agente, memoria, diario, preferencias, Telegram y el reloj autónomo. **Sin
interfaz, sin micrófono, sin escritorio.** Levanta una API
([interfaces/http_api.py](../interfaces/http_api.py)) con `POST /chat`,
`POST /command`, `WS /stream` y `WS /agent`.

Lo que necesita del portátil —ver imágenes, tocar archivos, controlar el
escritorio— se lo pide por WebSocket. Si el portátil está apagado, **te lo dice
ella**; no da un error.

**Cuándo:** en la VM. Es lo que hace que Iris siga viva y te escriba aunque tú no
estés delante de nada.

**Necesita:** `requirements.server.txt` — sin PyQt6, sin faster-whisper. La
transcripción la hace la API de Groq.

### `client` — la cara y las manos

El avatar, la ventana de terminal, el micrófono y el altavoz. **No instancia un
`IrisAgent`**: usa [core/remote_iris.py](../core/remote_iris.py), que expone la
misma superficie pero por HTTP.

Eso no es un detalle de estilo. `iris_state` tiene **una sola fila**: dos agentes
contra la misma base se sobrescriben la personalidad y entrelazan los historiales
hasta dejar la memoria sin sentido.

Abre **un WebSocket saliente** hacia el servidor —sin puertos abiertos ni NAT que
tocar— y ofrece dos capacidades: `claude` (tu suscripción de Claude Code, que
solo funciona donde está tu sesión) y `desktop`.

**Necesita:** `requirements.client.txt` — sin LangChain, sin psycopg2, sin
Whisper. Son unos 2 GB de dependencias que no se instalan.

---

## Cómo cambiar de modo

Es una línea del `.env` y reiniciar. No hay migración ni nada que mover: **la
memoria vive en Supabase**, no en el proceso.

```bash
# tu PC, todo dentro (por defecto)
IRIS_MODE=local

# tu PC como cara de un cerebro remoto
IRIS_MODE=client
IRIS_SERVER_URL=https://<lo-que-sea>.sslip.io
IRIS_AGENT_TOKEN=<el MISMO que en el servidor>
```

Y arrancas igual que siempre: `python main.py`.

**No corras `local` y `server` a la vez.** Serían dos Iris escribiendo la misma
fila de estado — exactamente el problema que `RemoteIris` existe para evitar.

**El token tiene que coincidir en las dos máquinas.**
[config/settings.py](../config/settings.py) lo valida al arrancar y falla claro
si falta. Se genera una vez:

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

---

## Desplegar desde cero

El runbook detallado está en [deploy.md](deploy.md). El resumen:

1. **Un nombre.** GCP te da una IP, no un dominio, y sin HTTPS Telegram no acepta
   el webhook. Lo más barato es `sslip.io`: `34.11.25.78.sslip.io` resuelve solo
   a esa IP, sin registrarte en nada, y Let's Encrypt le emite certificado.
2. **Proyecto propio** en GCP. Aísla el coste y borrarlo lo limpia todo de golpe.
3. **IP estática** en la región de tu Supabase — `us-east-1` de AWS ≈ `us-east4`
   de GCP. De ahí sale la bajada de latencia de ~208 ms a unos pocos.
4. **VM `e2-medium`**, Debian, 30 GB, con Docker. Firewall: solo 80 y 443.
5. **Subir el proyecto y el `.env`**, y `docker compose up -d --build`.
6. **Comprobar las cuatro líneas del arranque.** Si falta
   `[Telegram] Webhook registrado`, el bot no responderá.
7. **Alerta de presupuesto** antes de olvidarte.

Coste real medido: **~31 $/mes** (VM 28 + disco 3; la IP es gratis mientras la VM
esté encendida).

---

## Volver a desplegar

**Si borraste el proyecto hace menos de ~30 días, no lo rehagas.** GCP lo deja en
`DELETE_REQUESTED` y los recursos siguen ahí:

```bash
gcloud projects undelete <proyecto>
gcloud billing projects link <proyecto> --billing-account=<cuenta>   # se desvincula al borrar
gcloud compute instances start iris --zone=us-east4-a
```

Eso es todo. Los contenedores vuelven solos por el `restart: unless-stopped`, la
imagen sigue construida en el disco, y **la IP es la misma** — así que el dominio,
el certificado y la URL del webhook no cambian. Veinte minutos convertidos en
tres comandos.

Si el `.env` local perdió el token, está en el servidor:

```bash
gcloud compute ssh iris --zone=us-east4-a --command="grep IRIS_AGENT_TOKEN ~/iris/.env"
```

**Si ya pasaron los 30 días**, toca desde cero con [deploy.md](deploy.md). Lo
único que cambiará es la IP, y con ella el nombre de `sslip.io`: una línea de
`IRIS_DOMAIN` y reiniciar, porque el webhook se registra solo al arrancar.

---

## Apagar sin borrar

Parar la VM **no** deja el coste en cero:

| | encendida | parada |
|---|---|---|
| CPU y RAM | ~28 $ | 0 $ |
| disco 30 GB | 3 $ | 3 $ |
| IP estática | 0 $ | **~5,5 $** |

La IP es lo contraintuitivo: Google la cobra justo cuando **no** la usa una VM
encendida, para que no acapares direcciones. Total parada: ~8,5 $/mes.

Y con la VM parada Iris deja de tener vida interior —no escribe el diario, no te
escribe por su cuenta— y **Telegram deja de responder**, porque el webhook sigue
apuntando ahí. Si vas a estar un tiempo largo sin ella, desregistra el webhook
antes:

```bash
curl "https://api.telegram.org/bot<token>/deleteWebhook"
```

Si no, el bot queda mudo sin dar ningún error, que es el síntoma más difícil de
diagnosticar que existe.

---

## Si algo no va

**El bot no responde.** `getWebhookInfo`. Si la `url` está vacía o apunta a un
`trycloudflare.com`, el registro no corrió: revisa `IRIS_DOMAIN` y
`TELEGRAM_ENABLED`.

**403 a Telegram.** El secreto no cuadra. Pasa si cambiaste el token del bot sin
volver a registrar, porque el secreto se deriva de él. Reinicia el contenedor.

**El portátil no conecta.** Casi siempre los `IRIS_AGENT_TOKEN` no coinciden. El
servidor lo dice en su log.

**Caddy responde 503 a todo.** No encuentra el contenedor. La red de Docker tiene
que llamarse igual que `CADDY_INGRESS_NETWORKS` — por eso el compose la fija a
`iris_net` en vez de dejar que Compose la nombre según la carpeta.

**Caddy no saca certificado.** El DNS aún no resuelve, o el 80 está cerrado:
Let's Encrypt necesita ese puerto para validar.

**Funciona pero va lento.** Comprueba que la VM y Supabase están en la misma
región. Si no, sigues pagando el viaje de red que venías a quitar.
