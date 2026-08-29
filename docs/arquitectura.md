# Cómo está hecha Iris

Mapa del sistema: qué guarda, dónde vive cada cosa y cómo se mueve entre ellas.
Para el despliegue, ver [deploy.md](deploy.md). Para el plan y su porqué,
[iris-desacoplada.html](iris-desacoplada.html).

---

# 1. Las tablas

Ocho tablas en un único Postgres (Supabase). No hay más bases de datos: el grafo
vivió en Neo4j y se consolidó aquí, porque ninguna consulta pasaba de profundidad
2 y eso en SQL es un JOIN.

Si Supabase no responde, [storage/factory.py](../storage/factory.py) cae solo a
SQLite + ChromaDB en `data/`. Mismo comportamiento, menos capacidades: sin grafo
por vectores y sin búsqueda semántica fina.

## `conversation_history` — lo que os habéis dicho

| columna | tipo | |
|---|---|---|
| `id` | serial | |
| `role` | text | `user` \| `iris` |
| `content` | text | el mensaje |
| `timestamp` | timestamptz | |

**Escribe:** `ConversationHistory.append_turn()`
([core/utils/history.py](../core/utils/history.py)), en cada turno y por cualquier
canal. Descarta los turnos a medias: un mensaje en blanco de Iris guardado aquí
aparece luego en la ventana de contexto de todos los mensajes siguientes y le
enseña al modelo que callarse es una respuesta válida.

**Lee:** al arrancar se cargan los últimos `MEMORY_STM_PERSIST_MESSAGES` (40).
De esos, los que van en el prompt de cada turno son `MEMORY_STM_WINDOW` (20).
Son dos números distintos a propósito: se recupera más historia de la que se
manda, para que el arranque no empiece en blanco sin engordar cada petición.

## `iris_memories` — lo que recuerda de ti

| columna | tipo | |
|---|---|---|
| `id` | text | uuid |
| `content` | text | el hecho, **en tercera persona** |
| `embedding` | vector(384) | `all-MiniLM-L6-v2` |
| `category` | text | `personal` \| `work` \| `preference` \| `routine` \| `achievement` \| `joke` \| `relationship` \| `task` |
| `importance` | int | 1–3 |
| `temporal_ref` | text | fecha del hecho, si la hay |
| `stored_at` | timestamptz | |
| `owner` | text | |
| `expires_at` | date | **null = para siempre** |

**Escribe:** `MemoryManager._extract_and_store()`
([core/memory.py](../core/memory.py)), una vez cada 20 mensajes y al cerrarse la
sesión por inactividad. Antes de insertar comprueba que no exista ya algo con
similitud > 0.97 — solo caza repeticiones casi literales, y a propósito:
este encoder puntúa dos hechos *contradictorios* («le gusta el azul» / «el
verde», 0.934) **más parecidos** que una paráfrasis real (0.782), así que no
existe umbral que separe los duplicados de los hechos distintos.

**Lee:** `get_relevant_memories()` en cada turno, por similitud, 5 resultados.

**Caduca:** los registros de tareas (`category='task'`) nacen con 7 días —
«te creé este archivo» importa esta semana, no en marzo. Las memorias de verdad
llegan con `expires_at` nulo. Se filtran al leer y se borran al arrancar
(`_purgar_caducadas`).

## `iris_entities` y `iris_relations` — el grafo

```
iris_entities                    iris_relations
  name       PK                    from_name  ─┐
  type       Person|Project|…      relation    ├─ PK compuesta
  properties jsonb                 to_name    ─┘
  updated_at                       context    por qué están conectadas
                                   rel_date
                                   history    jsonb: cómo ha ido cambiando
                                   created_at / updated_at
```

**Escribe:** la misma extracción cada 20 mensajes, con
`GRAPH_EXTRACTION_PROMPT`. Usa `get_extraction_llm()` — el modelo **grande**, y
no es un lujo: con el de 20B esta capa **nunca guardó una sola fila** en toda su
existencia. No conseguía producir JSON válido para un objeto anidado y Groq
rechazaba la generación entera con `json_validate_failed`, dentro de un
`try/except` en un hilo de fondo. Ahora, si el grafo está vacío habiendo
memorias, sale un `WARNING` al arrancar.

**Lee:** `_get_graph_context()` en cada turno. Las entidades se detectan por
**coincidencia de nombres**, no con una llamada a LLM: se comprueba cuáles de los
nombres que Iris ya conoce están escritos en el mensaje. La lista se cachea diez
minutos y el emparejamiento normaliza tildes con límites de palabra, así que
«mañana» no dispara «Ana» y «Halcon» sí encuentra «Halcón».

El recorrido es un CTE recursivo a profundidad 2, en ambos sentidos. Las aristas
se duplican invertidas para poder recorrerlas al revés, pero se marca cuáles lo
están: si no, imprimía «Halcón DESARROLLA Lucía» — la frase del revés.

## `iris_preferences` — sus gustos, no los tuyos

| columna | tipo | |
|---|---|---|
| `subject` | text PK | «hablar de música», «que me pidan cosas de madrugada» |
| `kind` | text | `tema` \| `actividad` \| `trato` \| `entidad` |
| `valence` | real | −1 (lo detesta) … +1 (le encanta) |
| `strength` | real | 0–1, evidencia acumulada |
| `formed_at` / `last_reinforced` | timestamptz | |
| `evidence` | jsonb | por qué |

**Escribe:** salen del campo `iris_reactions` del **mismo** JSON de extracción de
memoria — sin llamada extra. Refuerzo con rendimientos decrecientes: al principio
cambia de idea fácil, después ya no.

**Ojo con `strength`:** lo guardado es el valor *en el último refuerzo*. La fuerza
actual se **deriva** del tiempo transcurrido (`0.985 ^ días`, media vida ≈ 46
días). Consultar la columna a pelo da un valor caducado; usar
`Preference.current_strength`.

**Lee:** `build_system_prompt()` inyecta las que superan 0.25 más un bloque de
disposición que le permite negarse cuando no hay nada en juego. Se ven con
`/gustos`.

## `iris_journal` — lo que piensa cuando no estás

| columna | tipo | |
|---|---|---|
| `id` | bigserial | |
| `at` | timestamptz | |
| `kind` | text | `reflexion` \| `conexion` \| `curiosidad` \| `actividad` |
| `content` | text | en primera persona, como lo pensó |
| `shared` | bool | si ya te lo contó |
| `impulse` | real | 0–1, ganas de contarlo |

**Escribe:** `JournalKeeper.live_a_moment()`
([core/journal.py](../core/journal.py)), cada 20–40 minutos.

El `impulse` nace según el tipo: `conexion` 0.80, `actividad` 0.60,
`curiosidad` 0.50, `reflexion` 0.35. Esa diferencia es la tesis entera — «hace
rato que no hablamos» lo dice cualquier temporizador; «lo del martes es lo mismo
que te pasaba en noviembre» solo lo dice alguien a quien le ocurrió algo mientras
no mirabas.

**Lee:** dos sitios distintos. `ProactiveEngine` para escribirte por iniciativa
propia (umbral `JOURNAL_IMPULSE_THRESHOLD`, 0.75), y `algo_que_contar()` para
sacarlo en una conversación que ya existe — ahí el umbral baja a la mitad, porque
interrumpirte cuando no estás es caro y mencionarlo mientras habláis no cuesta
nada. Se ve con `/diario`.

## `iris_events` — qué ha hecho

| columna | tipo | |
|---|---|---|
| `id` | bigserial | |
| `at` | timestamptz | |
| `kind` | text | `proactivo` \| `diario` \| `delegacion` \| `error` |
| `summary` | text | una línea legible |
| `detail` | jsonb | coste, humor, energía, milisegundos… |
| `expires_at` | date | 30 días; los errores, 90 |

**No es el log del proceso**, y la distinción es deliberada: el log crudo ya lo
guarda Docker, y meterlo aquí añadiría una escritura por línea y un modo de fallo
tonto — si la base se cae, pierdes justo los registros que explicarían por qué.

Aquí van cosas con significado: que te escribió por su cuenta y en qué estado,
cada entrada de diario con su impulso, cada delegación con su coste y su
duración, y los errores que hoy se traga un `except`. Responde a «¿qué ha hecho
esta semana?», que antes no se podía contestar.

**Escribe:** `IrisAgent.registrar()`, que **nunca lanza** — un registro no puede
tumbar la conversación que está registrando.

**Lee:** `/eventos`, opcionalmente filtrando por tipo. Se purga al arrancar, en
el mismo sitio que las memorias.

## `iris_state` — su estado emocional

Una sola fila, `key = 'iris_emotional_state'`, con un JSON dentro: humor,
confianza, energía, `energy_updated_at`, chistes internos, apodo, contador de
mensajes proactivos del día.

**Que sea una sola fila importa:** dos procesos escribiendo aquí se pisan. Por eso
en modo cliente el portátil instancia `RemoteIris` y **no** un `IrisAgent` propio.

Al ser un blob JSON, añadir un campo no necesita migración.

---

# 2. Las carpetas

No hay un `brain/` y un `client/`, **y es deliberado**: la separación la imponen
los *imports perezosos*, no los directorios. `main.py` importa `IrisAgent` dentro
de su rama `else`, así que un portátil en modo cliente no carga LangChain,
LangGraph, psycopg2 ni sentence-transformers. Mover ficheros cambiaría de sitio
el código sin cambiar lo que se carga — y `core/` no se parte en dos:

| lado | módulos |
|---|---|
| **Solo cerebro** | `agent`, `memory`, `personality`, `preferences`, `proactive`, `journal`, `llm_factory`, `commands`, `storage/`, `interfaces/`, `config/prompts` |
| **Solo cliente** | `remote_iris`, `ui/`, `voice/`, `companion/` |
| **Los dos** | `executor`, `link/`, `config/settings`, y `claude_delegate` — cuyo `IntentAgent` corre en el cerebro y su `ClaudeDelegator` en el portátil |

**La regla que se deriva:** un módulo del cerebro no importa PyQt6 ni sounddevice
a nivel de módulo, y al revés. Donde uno necesita al otro (el `/salir` que cierra
la ventana), el import va protegido — el servidor no tiene Qt y un import a pelo
convierte un comando en un `ImportError`.

```
config/     settings.py      todo lo configurable, desde .env
            prompts.py       personalidad, reglas, ejemplos, extracción, diario
            capabilities.md  lo que Iris dice que sabe hacer, cuando le preguntan

core/       agent.py         IrisAgent: el grafo LangGraph de 4 nodos
            memory.py        las tres capas de memoria y la extracción
            personality.py   humor, confianza, energía, y el system prompt
            preferences.py   sus gustos: refuerzo y decaimiento
            journal.py       QUÉ piensa cuando no la miran
            proactive.py     CUÁNDO, y si además te lo cuenta
            claude_delegate.py  invocar la CLI de Claude, y el ClaudeResult
            executor.py      LA COSTURA: aquí o en el portátil
            link/            el WebSocket entre las dos máquinas
            remote_iris.py   la cara de IrisAgent, pero por HTTP
            commands.py      /status /memoria /gustos /diario /coste …
            llm_factory.py   los cuatro clientes de LLM
            startup.py       arranque de Telegram, voz, proactivo y enlace
            utils/           historial, montaje de mensajes, troceo en frases

storage/    base.py          las interfaces abstractas
            factory.py       Supabase, y si falla, SQLite
            supabase.py      el backend real
            sqlite_fallback.py

interfaces/ http_api.py      /chat /command /status, WS /stream y /agent
            telegram_bot.py  el webhook

ui/ voice/ companion/        la cara, los oídos y las manos. Solo portátil.
```

---

# 3. Los flujos

## Un turno de conversación

```
tu mensaje
   ├─ ¿es un comando?  → handle_command()          sin LLM
   ├─ prefiltro por regex: ¿huele a tarea?         sin LLM
   │     └─ sí → IntentAgent (20B) → ¿delegar?
   │                └─ sí → Claude Code (ver abajo)
   └─ no → el grafo LangGraph:
        1. analyze_input     20B   humor e intención
        2. retrieve_memory   —     vectores + grafo + capacidades + diario
        3. generate_response 120B  con la personalidad ya montada
        4. update_state      —     guarda el turno y el estado
```

El system prompt se monta en `build_system_prompt()` y va en este orden:
personalidad, hora, confianza, humor, energía, chistes, gustos, disposición,
reglas, **y los ejemplos al final**. Lo último pesa más, y una demostración vence
a una prohibición: con las reglas al final respondía con sentencias de ensayo
aunque los ejemplos mostraran lo contrario dos párrafos antes.

## Delegar en Claude Code

```
prompt técnico  +  personalidad de Iris (--append-system-prompt)
       ↓
claude -p --output-format json [--resume <sid>] [--json-schema …]
       ↓
ClaudeResult: text, ok (del is_error de la CLI), session_id, cost_usd
```

Claude responde **siendo** Iris, así que lo que devuelve ya es la respuesta: no
hay un segundo modelo reescribiéndola en primera persona. Los adjuntos y las
capturas van **por ruta**, nunca en base64 — Claude los abre con `Read`, y el
binario corre en la máquina que tiene el archivo.

`--resume` mantiene el hilo entre turnos, pero solo mientras el humor, la
confianza y la energía no cambien; si cambian, sesión nueva, porque si no
reanudaría siendo quien ya no es.

## El tick del diario, cada 20–40 min

```
1. el cuerpo   refrescar energía; parar si es horario de silencio o va justa
2. vivir       elegir actividad → hacerla → anotarla → gastar 4 de energía
3. ¿lo cuenta? solo si supera el umbral y le quedan mensajes del día
```

El intervalo lleva jitter a propósito: media hora exacta se nota y parece un
cron. Y pensar cuesta energía, porque sin ese coste rumiaría sin parar; con él el
ciclo se autorregula solo, y esa curva acaba notándose en su tono.

## Las dos máquinas

```
     SERVIDOR (VM)                          PORTÁTIL
  agente, memoria, diario      ←── WS ──→   avatar, micro, altavoz
  Telegram, motor autónomo                  claude -p  (tu suscripción)
  Postgres en Supabase                      escritorio: ratón, teclado, pantalla
```

El portátil abre **un solo WebSocket saliente** — sin puertos abiertos ni NAT que
configurar — y ofrece dos capacidades: `claude` y `desktop`.
[core/executor.py](../core/executor.py) es la costura: `run_claude()`,
`stream_claude()` y `desktop_request()` corren aquí o viajan por el cable, y
**quien llama no sabe cuál de las dos**. Por eso `IRIS_MODE=local` se comporta
exactamente como siempre.

Con el portátil apagado, Iris conversa, recuerda y te escribe igual. Lo que no
puede es ver imágenes, tocar archivos ni el escritorio — y lo dice ella, no da un
error.

---

# 4. Los cuatro modelos

Repartidos por **cada cuánto corren** y **si hace falta juicio**, no por
importancia:

| fábrica | modelo | cada | para qué |
|---|---|---|---|
| `get_llm()` | 120B prosa | mensaje | sus respuestas; las `conexion` del diario |
| `get_analysis_llm()` | 20B **JSON** | mensaje | humor, intención, relevancia |
| `get_fast_llm()` | 20B prosa | tick | reflexión, curiosidad |
| `get_extraction_llm()` | 120B **JSON** | 20 mensajes | memoria y grafo |

Los dos marcados **JSON** llevan `response_format: json_object`, que **exige la
palabra "json" en el prompt**. Pedirle prosa a esos clientes devuelve un 400 —
por eso existe `get_fast_llm()`, y por eso el diario no funcionaba al principio.

---

# 5. Comandos y configuración

## El editor visual

`/cerebro web` levanta un servidor en `127.0.0.1:8765` y abre el navegador con el
grafo dibujado: nodos coloreados por tipo, tamaño según cuántas conexiones tienen,
y arrastrables. Clic en un nodo para renombrarlo, borrarlo o ver sus relaciones;
clic en una flecha para borrarla. Dos pestañas más para las memorias (editables
en línea) y los gustos.

La página está en [ui/brain/index.html](../ui/brain/index.html) y la API en
[interfaces/brain_api.py](../interfaces/brain_api.py).

**Cómo se protege depende de dónde corra:**

- **En local** escucha en `127.0.0.1` y no pide nada. Quien está en la máquina ya
  está dentro; pedirle una llave para ver su propia memoria sería teatro.
- **En el servidor** se monta en `/cerebro` detrás de Caddy y exige `BRAIN_TOKEN`
  — en la URL (`?k=…`) o en la cabecera `X-Brain-Token`. Sin él devuelve 403 y
  **ni siquiera sirve la página**: enseñarla y proteger solo los datos dejaría ver
  la estructura y sabría cualquiera qué pedir.

`BRAIN_TOKEN` es un secreto **propio**, no el `IRIS_AGENT_TOKEN`, y eso importa:
aquel autoriza a ejecutar Claude en tu portátil con acceso a archivos, y este va a
viajar por un chat de Telegram cada vez que pidas el enlace. Que se filtre uno no
debe entregar el otro. Si no se define, se deriva del otro con un hash — funciona
sin configurar nada y sigue siendo un valor distinto.

Con `BRAIN_REMOTE_WRITE=false`, desde internet solo se puede mirar; para editar,
el portátil.

Escribe sobre el mismo `StorageFactory` que usa Iris, así que lo que corrijas ahí
lo lee ella en el turno siguiente, sin reiniciar. Renombrar a un nombre que ya
existe **fusiona** las dos entidades arrastrando sus relaciones — que es el caso
normal, porque la extracción guarda «Lucia» y «Lucía» como dos personas.

## Los comandos

| | |
|---|---|
| `/status` | humor, confianza, energía |
| `/cerebro web` | **el grafo interactivo** en el navegador: arrastrar, clic para editar |
| `/cerebro` | lo mismo por texto, para cuando no hay navegador — `/cerebro ayuda` |
| `/memoria` `/gustos` `/diario` | vistas rápidas de cada capa |
| `/eventos [tipo]` | qué ha hecho últimamente |
| `/coste` | lo que costaría delegar si se pagara por token |
| `/guardar` `/reset` `/trust ±n` `/energy ±n` `/salir` | |

Son los mismos por terminal, por la ventana y por Telegram. `/salir` no apaga
nada desde los canales remotos.

## Dónde se toca qué

| quiero cambiar… | está en |
|---|---|
| cómo habla, su carácter | `config/prompts.py` — `BASE_PERSONALITY`, `RULES`, `FEW_SHOT_EXAMPLES` |
| qué recuerda y qué no | `MEMORY_EXTRACTION_PROMPT` |
| qué hace cuando no estás | `ACTIVITY_PROMPTS` y `core/journal.py` |
| cada cuánto piensa | `JOURNAL_INTERVAL_MIN` / `MAX` |
| si puede interrumpirte | `JOURNAL_SHARE_ENABLED`, `JOURNAL_IMPULSE_THRESHOLD` |
| qué puede tocar Claude | `CLAUDE_ALLOWED_TOOLS` (Bash queda fuera a propósito) |
| dónde vive | `IRIS_MODE` |

Todo sale de `.env`; la plantilla comentada es
[.env.example](../.env.example).
