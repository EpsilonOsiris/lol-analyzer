# lol-analyzer

> **¿No programas? → [Guía de instalación](INSTALAR.md).** Descarga el programa [aquí (zip)](https://github.com/EpsilonOsiris/lol-analyzer/archive/refs/heads/main.zip), haz doble clic en `instalar.bat` y sigue la guía: no necesitas terminal ni editar archivos. Lo que sigue es la documentación técnica.

Descarga **todas** tus partidas de League of Legends desde la API de Riot, las guarda en una base de datos local (SQLite) y analiza qué haces bien y qué haces mal, partida por partida y en general. También puedes agregar a los miembros de tu clan: cada uno tiene su propio análisis, hay una comparación entre todos y las partidas que jugaron juntos (ver [Tu clan](#tu-clan-varios-jugadores)).

Qué produce:

- Un reporte por partida ("boleta de calificaciones") que compara tus métricas con las de tu rival de línea y con tu propio historial (primero con ese campeón; si hay menos de 5 partidas, con tu rol; y si no, con todas tus partidas).
- Por campeón (con 5 partidas o más): qué métricas separan más tus victorias de tus derrotas (tamaño del efecto = diferencia de promedios / desviación estándar combinada).
- Un resumen general: winrate por cola, rol y campeón, y tendencias por mes, por día de la semana y por número de partida dentro de una sesión (una sesión es una racha de partidas con menos de 45 minutos de separación).
- Un CSV con una fila por partida y todas las métricas, para que lo explores con tu herramienta favorita.
- Con varios jugadores: una tabla comparativa del clan (winrate, KDA, muertes, CS/min, visión, rol y campeón más jugados) y cuánto ganan cuando juegan en el mismo equipo.

Solo se analizan las colas de la Grieta del Invocador (400, 420, 430, 440 y 490) y se excluyen los remakes (menos de 5 minutos o rendición temprana). Las demás partidas se guardan en la base de datos, pero se omiten del análisis (puedes incluirlas con `--todas-las-colas`). Para analizar una sola cola, pasa `--cola` a `analyze`, `game` o `campeon` (por ejemplo, `analyze --cola solo`).

**Tú** eres el jugador del `RIOT_ID` de `.env`: si no indicas otro, todos los comandos y páginas trabajan con tus partidas.

## Instalación

Requiere Python 3.10 o superior (probado con 3.13). En Windows, `instalar.bat` hace todo esto solo (ver [INSTALAR.md](INSTALAR.md)); si programas, a mano:

```bash
git clone https://github.com/EpsilonOsiris/lol-analyzer.git
cd lol-analyzer
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Linux / macOS
```

Configura tu cuenta. Lo más cómodo es abrir el panel (`python -m lolan web`): la primera vez te lleva a la página **Configuración**, donde escribes tu Riot ID, eliges el servidor y pegas tu key (con un botón para probarla). También puedes copiar y llenar el archivo a mano:

```bash
cp .env.example .env
```

```
RIOT_API_KEY=RGAPI-...        # tu API key
RIOT_ID=Nombre#TAG            # tu Riot ID completo
PLATFORM=la1                  # tu servidor (ver tabla abajo)
REGION=americas               # región de enrutamiento (ver tabla abajo)
GEMINI_API_KEY=               # opcional: para el análisis de video (próximamente)
```

La página Configuración escribe este mismo `.env` (de forma atómica y sin tocar las líneas que no conoce) y además guarda `RIOT_API_KEY_SAVED_AT` (cuándo se guardó la key de Riot, para avisarte cuando una key de desarrollo probablemente ya expiró) y `RIOT_API_KEY_PERMANENT=1` si marcas que tu key no expira (una Personal API Key). Los cambios se aplican sin reiniciar el panel.

### Cómo obtener la API key

1. Entra a <https://developer.riotgames.com> con tu cuenta de Riot.
2. Copia la **Development API Key** que aparece en el panel.
3. Pégala en la página Configuración del panel o en `.env`. **Las keys de desarrollo expiran cada 24 horas**: si recibes el error "la API key es inválida o expiró", genera una nueva (botón "Regenerate API Key"), actualízala y vuelve a ejecutar el comando. Nada de lo ya descargado se pierde. Para no renovarla a diario puedes pedir en el mismo sitio una *Personal API Key* (gratis, no expira, Riot tarda unos días en aprobarla).

La key nunca se imprime en pantalla y `.env` está en `.gitignore`.

### Cambiar de key de desarrollo a Personal (u otra app)

Una Development API Key y una Personal API Key pertenecen a **apps distintas** de Riot, y Riot cifra los PUUID por app. Al cambiar de una a otra hay que pasar tus datos una sola vez: ejecuta `python -m lolan rekey` o, en el panel, pulsa **«Actualizar mis datos»** (el aviso «Cambiaste de API key» aparece en **Configuración** al guardar la key nueva y en **Sincronizar** si un sync lo necesita). Regenerar la Development key cada 24 h **no** cambia de app. Más detalles en el comando `rekey` de la sección Uso. En el panel, la actualización es un trabajo en segundo plano con barra de progreso, excluyente con la sincronización.

### Plataforma y región

`REGION` es el valor de enrutamiento que usa la API para cuentas y partidas. Si la omites, se deduce de `PLATFORM`.

| PLATFORM | REGION |
|---|---|
| la1, la2, na1, br1 | americas |
| euw1, eun1, tr1, ru, me1 | europe |
| kr, jp1 | asia |
| oc1, ph2, sg2, th2, tw2, vn2 | sea |

## Uso

```bash
.venv/Scripts/python -m lolan sync [--jugador "Nombre#TAG" | --clan] [--max N] [--since AAAA-MM-DD] [--cola solo] [--no-timeline]
.venv/Scripts/python -m lolan analyze [--jugador "Nombre#TAG" | --clan] [--cola solo]
.venv/Scripts/python -m lolan game <matchId | ultima> [--jugador "Nombre#TAG"]
.venv/Scripts/python -m lolan campeon <Nombre> [--jugador "Nombre#TAG"]
.venv/Scripts/python -m lolan jugadores [listar | agregar "Nombre#TAG" | quitar "Nombre#TAG"]
.venv/Scripts/python -m lolan composiciones [--cola solo] [--linea soporte] [--todos-los-adc]
.venv/Scripts/python -m lolan rivales [--jugador "Nombre#TAG"]
.venv/Scripts/python -m lolan web [--puerto N] [--no-abrir]
```

- `sync` descarga las partidas nuevas (la partida y su timeline; cada partida son unas 2 peticiones). Respeta los límites de una key de desarrollo (20 peticiones por segundo y 100 cada 2 minutos), así que rinde unas 50 partidas cada 2 minutos. **Una sincronización completa de ~1000 partidas tarda alrededor de 40 minutos.** Guarda cada partida apenas la descarga: si se interrumpe (Ctrl+C, falla la red, expira la key), vuelve a ejecutar el mismo comando y continúa donde se quedó, sin repetir lo ya guardado.
  - `--max N`: descarga como máximo N partidas nuevas (útil para probar).
  - `--since AAAA-MM-DD`: solo partidas desde esa fecha.
  - `--cola COLA`: solo partidas de esa cola: `solo` (clasificatoria solo/dúo, 420), `flex` (440), `normal` (400), `blind` (430), `quickplay` (490) o el número de cola.
  - `--no-timeline`: no descarga el timeline. Es el doble de rápido, pero se pierden las métricas por minuto (diferencias a los minutos 7/14/21/28, muertes por fase, objetivos, etc.).
  - Sin más opciones descarga solo **tus** partidas. `--jugador "Nombre#TAG"` descarga las de ese jugador registrado y `--clan` las de todos los registrados, uno tras otro. `--max`, `--since`, `--cola` y `--no-timeline` se aplican a cada jugador.
- `rekey` actualiza tus datos después de cambiar de API key **a una de otra app de Riot** (por ejemplo, de la Development API Key a una Personal API Key). Riot cifra los identificadores de jugador (PUUID) por app: con la key nueva el mismo jugador tiene otro PUUID y los guardados ya no sirven (Riot responde 400 «Exception decrypting»). `rekey` hace una copia de `data/lol.db` (`data/lol.db.antes-rekey-AAAAMMDD-HHMM`; si no hay espacio en disco no hace nada), vuelve a buscar a cada jugador registrado por su Riot ID, vuelve a descargar cada partida y su timeline (los IDs de partida son los mismos; unas 2 peticiones por partida, ~13 min para 330 partidas), reemplaza los identificadores viejos en el registro y en toda tabla con una columna `puuid`, y al final verifica que cada jugador conserva las mismas partidas y el mismo análisis por cola. Se puede interrumpir (Ctrl+C, key vencida) y volver a ejecutar: continúa donde se quedó. Una partida que Riot ya no devuelve (404) se conserva como estaba y se avisa; un Riot ID que ya no existe se omite y sus datos quedan intactos. Mientras no lo ejecutes, `sync` se detiene con un mensaje que lo pide. Si cambias a otra key de la misma app (o regeneras la de desarrollo) no hace falta.
- `analyze` reconstruye `data/games.csv` y todos los reportes a partir de lo que ya está en la base de datos (no usa la red, así que puedes correrlo las veces que quieras). `--jugador` analiza a otro jugador registrado y `--clan` genera los reportes de todos.
- `game <matchId>` imprime la boleta de una partida; `game ultima` usa la más reciente.
- `campeon <Nombre>` imprime los promedios y lo que decide tus partidas con ese campeón (el nombre no distingue mayúsculas ni apóstrofes: `kaisa` funciona).
- `composiciones` analiza tus partidas junto a tu clan (no usa la red): pools de cada amigo, dúo bot por soporte, matriz de arquetipos de ADC x soporte, alineaciones, itemización (penetración de armadura) y Purificar; imprime un resumen con las recomendaciones de ADC para la alineación más común y escribe `reports/composiciones.md`. Todo se calcula desde tu línea (`--linea top|jungla|mid|adc|soporte`; por defecto, la que más juegas en esa cola; en el panel, el selector «Línea» junto al de la cola): de ADC, dúo bot por soporte, matriz ADC x soporte y ADC x engage; de soporte, el dúo bot por ADC y la matriz soporte x ADC; de jungla, top o mid, tu tipo de campeón contra el de tu aliado en la línea vecina; en todos, los picks recomendados de tu línea van primero. Un jugador cuenta como del clan si su nombre empieza con `CLAN_PREFIX` (por defecto `Epsilon`) o es un jugador registrado; `CLAN_ALIASES` (`Cuenta=Persona;Otra=Persona`) junta varias cuentas de una misma persona. Ambos se editan en la página Configuración. La página **Composiciones** del panel muestra lo mismo y un formulario «Armar draft». También calcula, desde tu asiento, el winrate de tu equipo por tipo de aliado en cada línea (y por persona del clan), las duplas de tipos (jungla × mid, jungla × top, soporte × ADC) y el «plan de pelea» de cada equipo (engage, proteger al carry, poke, pick, split o mixto) con su cruce contra el plan rival; el plan de ambos equipos aparece en la página de cada partida y en `data/games.csv` (`team_plan`, `enemy_plan`). El formulario recomienda los 3 mejores picks para cada línea sin campeón (los campeones de la persona elegida, o solo tipos si dejas «Aleatorio») y el plan resultante. Las clasificaciones de ADC y soportes salen de un video de coaching, salvo las marcadas como «propia»; las de top, jungla y mid y el plan de pelea son nuestras (guías como EarlyGame, LoLTheory, WeCoach y Mobalytics), no datos oficiales de Riot.
- `rivales` muestra quién está en tu partida **ahora** (la de tu `RIOT_ID`, o la de otro jugador registrado con `--jugador`) con la API Spectator de Riot: campeones, hechizos, runa clave, rango, maestría, partidas recientes e historial contigo. Ver [Rivales](#rivales-quién-está-en-tu-partida). Usa tu key (hasta 45 peticiones; lo ya consultado en las últimas 6 horas sale de la caché).
- `jugadores` muestra los jugadores registrados (con cuántas partidas hay guardadas de cada uno); `jugadores agregar "Nombre#TAG"` busca la cuenta con tu API key y la registra; `jugadores quitar "Nombre#TAG"` la saca de la lista (sus partidas se conservan). Los Riot ID no distinguen mayúsculas.

## Tu clan (varios jugadores)

Puedes registrar a los miembros de tu clan y analizarlos igual que a ti:

```bash
.venv/Scripts/python -m lolan jugadores agregar "Compañero#LAN"   # una vez por jugador
.venv/Scripts/python -m lolan sync --clan                          # descarga a todos (tú primero)
.venv/Scripts/python -m lolan analyze --clan                       # un reporte por jugador
```

- **Una sola API key para todos: la tuya.** Una key de desarrollo puede consultar a cualquier Riot ID, así que no hace falta pedirle su key a nadie ni su contraseña. Compartir keys o usar varias para saltarse los límites va contra las políticas de Riot: no lo hagas.
- **Cuánto tarda.** Cada partida nueva son unas 2 peticiones (la partida y su timeline), y la key permite unas 50 partidas cada 2 minutos (~100 peticiones / 2 min) **en total**, no por jugador: ~1000 partidas nuevas son unos 40 minutos, vengan de un jugador o de varios. Las partidas que jugaron juntos se descargan **una sola vez** y cuentan para todos los que estaban en ellas (aunque todavía no los hayas agregado: al registrarlos ya aparecen sus partidas guardadas).
- Con `sync --clan`, si la cuenta de un jugador no se encuentra (por ejemplo, cambió de nombre) se omite y se sigue con los demás; al final se imprime un resumen por jugador. Si la key expira, se detiene todo (lo ya descargado queda guardado). Cada vez que sincronizas se vuelve a buscar el Riot ID de cada jugador, así que un cambio de nombre se actualiza si usas el nombre nuevo (`jugadores agregar "NombreNuevo#TAG"` si hace falta).
- **Dónde quedan los reportes.** Los tuyos siguen en `reports/` y `data/games*.csv`. Los de los demás van a `reports/jugadores/<nombre>/` (con `<cola>/` dentro si usas `--cola`) y `data/games-<nombre>[-<cola>].csv`, donde `<nombre>` es el nombre de juego en minúsculas y sin acentos (y con el TAG si hay dos jugadores con el mismo nombre).
- **Panel.** Hay un selector de jugador junto al de cola, la página **Clan** compara a todos y muestra qué tan bien juegan juntos, y la página **Jugadores** permite agregar y quitar jugadores desde el navegador.

```bash
.venv/Scripts/python -m lolan web
```

Levanta un panel local en <http://127.0.0.1:8765> y abre el navegador (`--puerto N` cambia el puerto, `--no-abrir` no abre el navegador). Solo escucha en `127.0.0.1`: no es accesible desde otros equipos ni desde tu red. Para detenerlo, presiona Ctrl+C en la terminal. Lee la misma base de datos `data/lol.db` y funciona sin conexión: lo único que baja de internet son el mapa y los íconos de campeones de Riot (Data Dragon, ver [Muertes y peleas](#muertes-y-peleas-con-el-timeline)), una vez, y si no hay conexión usa un mapa esquemático e iniciales.

Todas las páginas tienen un selector de cola (Todas / Solo/Dúo / Flex; por defecto Solo/Dúo) y, si hay más de un jugador registrado, un selector de jugador (por defecto, tú). Ambos se conservan al navegar (`?cola=` y `?jugador=` en la dirección).

| Página | Qué muestra |
|---|---|
| Resumen (`/`) | Partidas, winrate, KDA, CS/min, muertes por 10 min y período; winrate por mes, por rol y por campeón (las tablas se ordenan haciendo clic en el encabezado); por día de la semana y por número de partida en la sesión; winrate después de una victoria y de una derrota; lo que decide tus partidas con cada campeón de 5 partidas o más; y **Patrones de tus muertes**, mapa de calor, tablas contra qué junglas y dúos de bot, y **Plan de mejora** |
| Campeón (`/campeon/<Nombre>`) | Promedios, qué es más alto en tus victorias y en tus derrotas, y la lista de tus partidas con ese campeón |
| Partidas (`/partidas`) | Todas tus partidas, de la más reciente a la más antigua, de 50 en 50 |
| Partida (`/partida/<matchId>`) | La boleta de esa partida: fortalezas, debilidades, comparación con tu rival de línea y con tu historial; y, si la partida tiene timeline, **Tus muertes**, **Peleas** y **Objetivos perdidos** |
| Clan (`/clan`) | Tabla comparativa de todos los jugadores registrados en la cola elegida (partidas, winrate, KDA, muertes por 10 min, muertes en solitario y antes del minuto 14 por partida, CS/min, visión/min, rol y campeón más jugados; se ordena haciendo clic en el encabezado) y **Jugando juntos**: winrate de cada pareja cuando está en el mismo equipo, junto al winrate de cada uno (solo parejas con 3 partidas juntos o más) |
| Composiciones (`/composiciones`) | Desde tu asiento (selector «Línea»: automática = la que más juegas en la cola) y con el clan: pools por persona, dúo bot (de ADC: por soporte; de soporte: por ADC; por campeón y arquetipo, con dif. de oro @14 y muertes), matriz ADC x soporte (o soporte x ADC), ADC x engage del equipo, tu tipo × el de tu aliado si juegas jungla, top o mid, alineaciones y parejas persona+campeón, penetración de armadura contra tanques y Purificar por terciles de CC enemigo; tipos por línea, duplas de tipos, plan de pelea (tuyo y del rival) y el formulario «Armar draft» que recomienda picks de tu pool en tu línea (el ranking de ADC si juegas de ADC) y para las demás líneas |
| Jugadores (`/jugadores`) | Jugadores registrados con las partidas guardadas de cada uno; formulario para agregar un Riot ID (se busca con tu API key) y botón para quitar a cualquiera menos a ti, con confirmación en la misma fila |
| Sincronizar (`/sincronizar`) | Descarga partidas nuevas desde el navegador (jugador: tú, uno en particular o todo el clan; cola, máximo de partidas por jugador y timeline) con el progreso en vivo |
| Configuración (`/configuracion`) | Riot ID, servidor (con nombres como LAN o EUW), key de Riot (con botón «Probar key de Riot») y key de Gemini opcional; instrucciones para conseguir cada key. Si falta la key o el Riot ID, todas las demás páginas redirigen aquí. Las keys guardadas solo se muestran como «termina en ••••abcd» y dejar un campo vacío la conserva |

Sincronizar usa la `RIOT_API_KEY` y el `RIOT_ID` de `.env` y lo vuelve a leer en cada sincronización: si tu key expira, regenera una en developer.riotgames.com, pégala en **Configuración** (o actualiza `.env`) y pulsa **Sincronizar** de nuevo, sin reiniciar el panel. Todas las páginas muestran un aviso si la key se guardó hace más de 23 horas o si Riot la rechazó en el último intento de este servidor (no se hace ninguna petición solo para mostrar la página). Solo se puede ejecutar una sincronización a la vez. Al terminar, los análisis se actualizan solos. Si no hay partidas todavía, el panel te lleva a esa página.

## Muertes y peleas (con el timeline)

Para las partidas con timeline (las que no se descargaron con `--no-timeline`) el panel analiza cada una de tus muertes y de las peleas. Todo se calcula en el momento a partir de `data/lol.db`; no usa la API de Riot.

- **Tus muertes** (página de la partida): una línea de tiempo y, por cada muerte, quién te mató (campeón y rol) y quién ayudó, si participó el jungla enemigo, cuántos rivales intervinieron, tu nivel y tu oro sin gastar frente al de quien te mató, si habías matado a alguien en los 60 s anteriores, el objetivo (dragón, barón, torre...) que cayó 45 s antes o después, cuántos wards puso tu equipo en los 90 s previos y si compraste un ward de control en los últimos 3 minutos, un gráfico del daño que recibiste (por campeón y habilidad, con nombres en español, más minions y torres), el daño que hiciste antes de morir y un minimapa con el punto exacto de tu muerte.
- **Peleas**: una pelea es una racha de 3 o más muertes de campeones (de cualquier equipo) con 15 s o menos entre una y otra y a menos de 3500 unidades del centro de la pelea, o de 2 muertes en las que participaste. Cada pelea se reconstruye paso a paso en el minimapa (botones ◀ ▶ y reproducción automática), con el resultado, quién cayó primero y qué objetivo se tomó en el minuto siguiente.
- **Objetivos perdidos**: dragones, barones y heraldos del equipo enemigo, con si estabas muerto, lejos (a más de 4000 unidades del foso) o cerca cuando cayeron.
- **Resumen**: «Patrones de tus muertes» (porcentajes en todas tus partidas, en victorias y en derrotas), mapa de calor (filtros por fase y resultado), «Contra qué junglas» y «Contra qué dúos de bot» (solo con 3 partidas o más) y el **Plan de mejora**: tus 3 prioridades. Para cada patrón candidato se calcula cuánto separa tus derrotas de tus victorias (tamaño del efecto, debe ser mayor a 0.1) y se multiplica por el porcentaje de partidas en las que no cumples su meta (al menos 20 %); se muestran los 3 mayores, con una fila ✓/✗ de tus últimas 10 partidas. Hacen falta 8 partidas con timeline, con 2 victorias y 2 derrotas.

**Qué es aproximado.** Riot solo guarda la posición de todos los jugadores una vez por minuto y no da la posición de los wards. Por eso: las posiciones de rivales y aliados entre kills salen del registro por minuto más cercano (contorno discontinuo en los mapas; el punto donde muere alguien sí es exacto), el nivel y el oro sin gastar son los del último registro antes de morir, «estabas muerto» usa un tiempo de reaparición calculado a partir de tu nivel y los minutos de partida, y los wards solo se cuentan por equipo. Las zonas (línea de top/mid/bot, río, jungla, base) son franjas del mapa, no una medición exacta.

**Mapa e íconos.** La primera vez que abres una partida con timeline, el panel descarga una sola vez, de Data Dragon (el CDN público de Riot, sin enviar nada tuyo), el mapa, los íconos de los campeones de esa partida y sus nombres de habilidades en español (es_MX), y los guarda en `data/ddragon/`. Usa la versión del parche de la partida cuando existe. La descarga corre en segundo plano y una página nunca espera más de unos segundos: sin conexión (o mientras baja) se ve un mapa esquemático y las iniciales de cada campeón, y se reintenta más tarde.

## Líneas (cómo va cada cruce)

Para cada línea con un solo jugador por equipo (top, jungla, mid, ADC, soporte) se compara a cada campeón con su rival directo y se mide **la línea, no la partida**, con el timeline. Se calcula desde `data/lol.db`, sin la API de Riot, y se guarda en archivos JSON en `data/lineas/` (sin base de datos; se puede borrar y se regenera).

- **Gana la línea**: oro + experiencia propios menos los del rival en el minuto 14. Más de +300 es «gana», menos de −300 «pierde» y entre medias «pareja» (constante `LANE_EVEN`). El porcentaje de línea cuenta las parejas como media victoria.
- **En qué niveles ganas o pierdes**: lo que ganas por minuto, según tu nivel, en los tramos 1-2, 3-5, 6-8 y 9+; y por cada nivel de 1 a 11 (picos de poder).
- **Quién llega antes** al nivel 2 y al 6; **picos con los items** (cuánto ganas por minuto 3 minutos antes y después de completar tu 1.º, 2.º y 3.er item legendario; salen de los datos de Data Dragon de ese parche, sin ellos queda vacío) y el **winrate de la partida según su duración** (<25, 25-30, 30-35, 35+).
- Con pocas partidas el porcentaje se acerca al promedio del campeón («ajustado») y el texto avisa «pocas partidas».

```bash
python -m lolan lineas --reconstruir                          # calcula las líneas de todas tus partidas guardadas
python -m lolan lineas Diana --vs Akali --linea mid           # una línea concreta
python -m lolan lineas Diana --linea mid                      # el campeón contra todos y sus mejores / peores cruces
python -m lolan lineas Diana --persona Osiris --parches todos # solo las partidas de un miembro del clan
python -m lolan lineas --personas                             # miembros del clan con líneas calculadas
```

`--reconstruir` es idempotente. Después de cada `sync` se calculan solas las partidas nuevas. La muestra incluye ambos lados de cada línea (tus partidas y las de todos los demás jugadores que salieron en ellas); por defecto se usan los 3 parches más recientes (`--parches N` o `--parches todos`). Cada fila solo nombra a los miembros del clan (el prefijo y los alias de la configuración, igual que Composiciones); el resto queda anónimo y no se guarda ningún puuid.

Archivos: `filas/<parche>/<fuente>.jsonl` (una fila por lado, solo se agrega), `procesadas.json`, `duelos/<LÍNEA>/<id del campeón>.json` (los agregados que se pueden leer a mano, con las cifras contra todos y contra cada rival), `campeones.json` e `indice.json`. La descarga Diamante+ agrega `descarga.json` (su estado, para reanudar).

### Muestra Diamante+ (qué pasa en las líneas de los campeones de tu clan)

Con solo tus partidas hay pocas por cruce. `muestras` descarga con tu key partidas de jugadores Diamante+ y las convierte en filas de línea (fuente `diamante`) en la misma carpeta, de modo que `lineas` y el panel las usan igual que las tuyas.

```bash
python -m lolan muestras                                 # sigue hasta Ctrl+C
python -m lolan muestras --horas 6 --regiones kr,euw     # 6 horas en Corea y Europa Oeste
python -m lolan muestras --max-partidas 30 --solo-pool   # prueba corta, solo jugadores de tus campeones
python -m lolan muestras --estado                        # resumen de data/lineas/descarga.json
```

- **A quién muestrea**: primero, por cada campeón del *pool* del clan (los que tú y cada miembro del clan juegan en una línea con 3 partidas o más), los ~10 mejores jugadores Master+ de ese campeón en cada región según la lista pública de OP.GG, un campeón tras otro para que todos tengan cobertura. Después, para tener variedad, jugadores al azar de Challenger, Grandmaster, Master y Diamante I a IV (ligas de Riot). `--solo-pool` se queda solo con lo primero. De cada jugador se toman sus últimas 20 partidas de solitario/dúo de los 3 parches más recientes; las ya procesadas no se vuelven a bajar.
- **Regiones**: por defecto `kr` (Corea) y `euw` (Europa Oeste). Riot cuenta el límite de la key por servidor, así que estas regiones **no gastan la cuota** que usan la sincronización y Rivales en tu región (LA1 / americas). Se puede cambiar con `--regiones` (por ejemplo `na,br`). Ritmo medido: unas 1 500 partidas por hora y región con el límite de una key de desarrollo (100 peticiones cada 2 minutos; cada partida son 2 peticiones).
- **Se pausa sola cuando juegas**: antes de cada paso mira el cliente de League; en selección de campeones o en partida no hace ninguna petición y espera (avisa una vez y reanuda al terminar). Sin cliente abierto, sigue.
- **La key**: usa la `RIOT_API_KEY` de `.env`. Una key de desarrollo **expira a las 24 h**: si pasa en medio, el comando se detiene con un mensaje claro y deja todo guardado (filas, partidas procesadas y la cola en `descarga.json`); renueva la key y vuelve a ejecutar el mismo comando para continuar donde se quedó. Para dejarlo corriendo muchas horas conviene pedir una *Personal API Key* (ver [Cómo obtener la API key](#cómo-obtener-la-api-key)), que no expira.
- **Privacidad**: no se guarda el JSON de las partidas ni el puuid o nombre de ningún desconocido en las filas; los jugadores de la cola solo aparecen en `descarga.json` hasta que se procesan. Los puuid que da OP.GG están cifrados para su propia key y no se usan: cada Riot ID se resuelve con la API de Riot.
- Cada ~200 partidas nuevas (y al terminar) se recalculan los agregados de `duelos/` leyendo solo los 3 parches más recientes, así que sigue siendo rápido con decenas de miles de filas. Si quieres ver el avance mientras corre, cada tanda imprime partidas por hora, filas y los campeones del pool con menos cobertura.

## En vivo (tu partida mientras juegas)

La página **En vivo** del menú muestra la partida que estás jugando, pensada sobre todo para **compartir la ventana con tu clan en Discord**. El punto rojo junto a «En vivo» en el menú indica que hay una partida en curso.

**Cómo funciona.** El cliente del juego, mientras hay una partida, responde en tu propio equipo en `https://127.0.0.1:2999/liveclientdata/` (la «Live Client Data API» de Riot). El panel la lee desde ahí, en el mismo PC: **no se envía nada a ningún lado y esa lectura no usa tu key de Riot** (tampoco la de Gemini). Solo los datos de [Rivales](#rivales-quién-está-en-tu-partida) se piden a Riot con tu key. Sin partida, el panel solo comprueba cada 5 segundos si el juego responde; cuando alguien tiene la página abierta y hay partida, lee cada segundo. Funciona también en la Herramienta de práctica y en partidas personalizadas. Al terminar la partida se queda el resumen final hasta que empiece otra; en unos minutos puedes sincronizarla en **Sincronizar** para ver el análisis completo.

**Qué muestra** (solo lo que ya ves en pantalla con TAB, en el registro de eventos y en tus estadísticas):

- Reloj, modo, marcador (asesinatos), torres, inhibidores, dragones (con su tipo), heraldos y barones de cada equipo.
- Los 10 jugadores: campeón, nivel, KDA, CS, puntuación de visión, objetos (íconos de Data Dragon), y si están muertos con su cuenta atrás de reaparición, igual que en TAB.
- **Valor en objetos** de cada jugador: el oro ya gastado en sus objetos (suma del precio de cada uno, por unidad en pociones y wards), con la diferencia frente a su rival de línea (misma posición) y el total de cada equipo. No es el oro actual: Riot solo entrega el oro actual del jugador que está jugando (el tuyo, que se muestra tal cual). Los precios salen de Data Dragon (`data/ddragon/`); sin precios guardados y sin conexión se ve «—».
- **Oro total estimado («≈ Oro total») y «sin gastar»**: un modelo lineal (mínimos cuadrados, en Python puro) con el minuto, el CS, los kills y las asistencias del jugador, calibrado con tus partidas guardadas con timeline (todos los jugadores de cada partida, desde el minuto 2). Se ajusta con el 80 % de las partidas y su error típico por fase se mide con el 20 % restante, y es el «±» que se muestra. Los coeficientes y los errores quedan en `data/gold_model.json`, que se vuelve a calcular solo, en segundo plano, cuando faltan o cuando hay 20 partidas guardadas más que la última vez; mientras tanto se usan valores de referencia. «Sin gastar» es el estimado menos el valor en objetos (redondeado a 100) y es solo aproximado: no cuenta pociones o wards consumidos, objetos vendidos, tropas ni recompensas por objetivos. Para ti se muestra tu oro real.
- **Tú contra tu historial**: tu CS actual junto a tu promedio con ese campeón a esta altura de la partida (interpolado entre tus promedios a los 7, 14 y 21 minutos; con menos de 3 partidas con ese campeón se usa tu rol y luego todas tus partidas), tu CS a los 7, 14 y 21 minutos de esta partida (si el panel estaba abierto) y tu dif. de oro a los 14 de promedio. Además, las metas de tu **Plan de mejora** que se pueden contar en vivo con los eventos de la partida (muertes antes del minuto 14, muertes en el minuto siguiente a una kill tuya, muertes contra 3 o más rivales, muertes antes del 14 con el jungla enemigo), como un dato: «2 · meta: máximo 1». Las demás metas se marcan como «no se puede contar en vivo». Todo se calcula con tu jugador principal (el Riot ID de Configuración).

**Qué NO muestra, a propósito.** La política de Riot para productos con datos del juego prohíbe mostrar información que no está en el cliente y da ventaja competitiva (por ejemplo, las esperas de las definitivas enemigas) y no aprueba apps que dicten las decisiones del jugador. Por eso esta página **no** muestra las esperas de hechizos de invocador ni de definitivas enemigas, **ni** cuenta atrás de reaparición de objetivos o inhibidores, **ni** da consejos o alertas del tipo «vuelve a base» o «pelea ahora». Los datos de tu historial se muestran como números neutros.

**Modo stream.** El botón «Modo stream (para compartir en Discord)» abre `/en-vivo?stream=1`: sin menú, pie ni avisos, siempre oscuro, con letra grande y un diseño que cabe en 1280×720 sin desplazarse. Comparte esa ventana del navegador en Discord (o ábrela en un segundo monitor y compártelo) y ponla en pantalla completa con F11.

**Certificado.** El juego usa un certificado propio. Se comprueba su cadena contra el certificado raíz de Riot incluido en `lolan/certs/riotgames.pem` (descargado de la documentación de Riot, ver `lolan/certs/LEEME.txt`) y solo para `127.0.0.1`; el nombre del servidor no se comprueba porque el certificado no está emitido para esa dirección. Nunca se desactiva la verificación en general. Algunos antivirus (Norton) reemplazan incluso los certificados locales: si Riot no valida, el panel prueba con el almacén de certificados de Windows (que sí confía en tu antivirus), también solo para `127.0.0.1`. Si ninguno valida, la página lo avisa. Para pruebas automáticas se puede cambiar la dirección con la variable `LOLAN_TEST_LIVE_URL` (solo acepta direcciones locales; no la uses normalmente).

### Selección de campeones (grabación)

Mientras el panel está abierto, también escucha al **cliente de League** (no al juego): cuando entras a la selección de campeones guarda lo que el cliente expone en ese momento (baneos, picks, hechizos, tiempos, la sala y el estado de la partida) en `data/capturas/seleccion-AAAAMMDD-HHMMSS.jsonl`, una línea JSON por cambio y un archivo por selección (se conservan los 30 más recientes). Sirve para probar la página **Partida** (más abajo) con datos reales en lugar de adivinar los nombres de los campos. Todo se queda en tu PC: no usa tu key ni la API web de Riot, solo el cliente local (`lockfile` en `C:\Riot Games\League of Legends`; si instalaste el juego en otra carpeta, escribe `LOL_DIR=D:\Juegos\League of Legends` en `.env`). En Solo/Dúo clasificatoria el cliente anonimiza a aliados y enemigos, y **nada de esto se usa jamás para averiguar quién es un jugador anónimo**.

- Se enciende solo con el panel; para apagarlo define `LOLAN_GRABAR_SELECCION=off`.
- Sin el panel: `python -m lolan grabar-seleccion` lo corre en la terminal (muestra cada cambio de fase; Ctrl+C para detenerlo).
- Al terminar de grabar una selección avisa «Selección de campeones grabada: data/capturas/…». Si el cliente está cerrado no hace nada (revisa cada 5 segundos).
- Usa el mismo certificado y las mismas reglas que el juego: solo `127.0.0.1`. Para pruebas, `LOLAN_LCU_LOCKFILE` apunta a otro `lockfile`.

### Partida (la selección de campeones en vivo)

La página **Partida** (`/partida`, en el menú) sigue al cliente de League y se actualiza sola. Usa el mismo sondeo del cliente que la grabación de arriba (no abre una conexión aparte) y funciona aunque apagues la grabación con `LOLAN_GRABAR_SELECCION=off`.

- **Sin selección** (cliente cerrado, menú, sala, cola, partida encontrada): «Esperando la selección de campeones» y el estado en español.
- **Selección de campeones**: tu equipo (línea, persona del clan o «Aleatorio», campeón, «eligiendo…» si solo lo tiene en pantalla o «elegido» si ya lo bloqueó; tú resaltado), el equipo rival (los campeones que el cliente ya reveló), los bans y la fase con su cuenta regresiva. En ARAM y otros modos con banca también la banca y los rerolls.
- **Recomendación**: solo en la **Grieta del Invocador** (mapa 11, sin banca). Es el mismo draft de Composiciones con la alineación rellenada desde el cliente y recalculada con cada cambio (cada pick que se pasa por encima o se bloquea); lo que solo está en pantalla se usa como supuesto. Tu línea va primero. En otros modos (ARAM y cualquier modo con banca u otro mapa) se muestra el tablero y la nota «Composiciones solo aplica a la Grieta del Invocador (picks por línea)».
- **Tu línea**: si el cliente no la dice (selección a ciegas, Herramienta de práctica), se usa la que más juegas; la cambias en el selector de la página (se guarda solo en la dirección, `?rol=`, y no se escribe en ningún lado). Con otra línea que no sea ADC, tus recomendaciones salen de tu historial en esa línea y el ADC se trata como cualquier otra posición (las reglas ADC×soporte solo aplican cuando tú eres el ADC).
- **Quién es quién**: solo los miembros del clan registrados (prefijo o cuentas registradas) aparecen con su nombre; los demás son «Aleatorio». De un jugador que el cliente oculta (rival, o aliado anonimizado en Solo/Dúo) no se guarda ningún dato que lo identifique, solo el campeón.
- **Al empezar la partida**: si Partida está abierta, te lleva sola a **En vivo** (si la abres con la partida ya en curso solo ofrece el enlace «Ir a En vivo»). Al terminar muestra «Partida terminada» y el enlace a tus partidas.

Todavía no se ha visto con un draft real de la Grieta (solo Herramienta de práctica y ARAM): los valores de la línea asignada, cuándo se revelan los campeones rivales y el contenido de los bans se leen con cuidado y se confirmarán en la primera partida normal o flex.

### Rivales: quién está en tu partida

Dentro de **En vivo** (y también en el modo stream) hay una sección **Rivales y aliados** que se llena desde la **pantalla de carga**, con datos de Riot que se piden con tu key (API Spectator, league, champion-mastery y match-v5):

- **Cuándo se busca.** Mientras la página En vivo está abierta y el juego no está en partida (sin partida, cargando o con el resumen final de la anterior), el panel pregunta a Riot cada 30 segundos si tu cuenta está en una partida (1 petición cada vez). Cuando aparece una, hace el escaneo una sola vez en segundo plano y lo empuja a la página; el resultado se queda toda la partida y en «terminada». El botón **«Buscar partida ahora»** lo hace al momento, y el selector de jugador permite ver la partida de cualquier jugador registrado de tu clan. También por terminal: `python -m lolan rivales [--jugador "Nombre#TAG"]`.
- **Qué muestra de cada jugador.** Equipo aliado y enemigo (relativo al jugador buscado), bans, campeón, hechizos de invocador, runa clave y árbol secundario (nombres e íconos de Data Dragon en español), rango de Solo/Dúo y Flex con PL y winrate de la temporada, maestría en ese campeón (puntos, nivel y «hace N días» desde la última vez que lo jugó) con la etiqueta «poca experiencia con este campeón» (menos de 10 000 puntos o nunca lo jugó) o «main» (más de 100 000), racha de victorias, las últimas 10 partidas clasificatorias (de los enemigos, hasta 3 con detalle: V/D y KDA, y cuáles fueron con ese campeón) y el **historial local**: «Antes: 2V-1D contra ti», calculado gratis con tus partidas guardadas, comparando por Riot ID (`Nombre#TAG`, sin distinguir mayúsculas), nunca por puuid. Los jugadores de tu clan (prefijo, alias o registrados) llevan la insignia «de tu clan».
- **Jugadores ocultos.** Si Riot entrega a un jugador sin puuid ni Riot ID (modo oculto, streamer), la página solo muestra su campeón y sus hechizos, con la etiqueta «Jugador oculto»: nunca se intenta identificarlo ni se consulta nada de él. Los bots se marcan como «Bot».
- **Cuántas peticiones gasta.** Máximo **45 por escaneo** (típico en frío: unas 35; con todo en caché, solo la pregunta a Spectator). Orden: rango y maestría de todos (enemigos primero, luego aliados, tú al final; 2 por jugador), después las últimas partidas clasificatorias (1 petición por jugador; de los enemigos, hasta 3 detalles de partida que no estén ya guardados). Si se agotan las 45 o Riot pediría esperar más de 60 segundos por el límite de tu key (20 cada segundo y 100 cada 2 minutos por servidor), el escaneo se detiene y muestra lo que alcanzó a obtener y qué quedó pendiente. Cada servidor de Riot (por ejemplo `americas` y `la1`) tiene su propio cupo: el panel mantiene un limitador por servidor compartido por la sincronización, el escaneo y las búsquedas de cuentas.
- **Caché.** Lo consultado de cada (jugador, campeón) se guarda 6 horas en la tabla `scout_cache` de `data/lol.db` (se borra a las 24 horas), así que volver a buscar la misma sala, o a las mismas personas más tarde, no gasta peticiones. Los puuid se resuelven cada vez por Riot ID con tu key actual (y se recuerdan en memoria por Riot ID y huella de la key), no se toman del registro ni de las partidas guardadas, para que siga funcionando justo después de cambiar de key.
- **No corre junto a una sincronización.** Comparten el mismo cupo de peticiones: el botón responde con un mensaje amable, y la búsqueda automática espera a que termine la sincronización.
- **Política de Riot.** Solo se muestra lo que la API de Riot expone sobre la cuenta de cada jugador (rango, maestría, historial de partidas) y su Riot ID público; nada de esperas de hechizos en vivo, ni «dodge» ni consejos sobre qué hacer. Si algún día Riot cambia sus reglas, esta sección se puede quitar sin afectar al resto.

#### Duelos por línea y probabilidad estimada (OP.GG)

Dentro de **Rivales y aliados**, en partidas de la Grieta con 5 contra 5, el panel añade (en segundo plano, después del escaneo y sin retrasarlo) el bloque **Duelos por línea** y una **probabilidad estimada** de que gane tu equipo:

- **De dónde salen los datos.** Del servidor MCP público y oficial de OP.GG (`https://mcp-api.op.gg/mcp`, sin key). Para cada línea se pide la tasa de victorias de tu campeón contra el campeón enemigo (`lol_get_lane_matchup_guide`; el % es el de **tu** campeón) y, para el dúo tirador + soporte de cada equipo, su tasa conjunta (`lol_get_champion_synergies`; OP.GG solo lista los ~10 soportes más jugados de cada tirador, así que si el dúo no está ahí se busca en la lista de tiradores del soporte). OP.GG no deja filtrar por rango estos duelos: son datos de **todos los rangos**, y la pantalla lo dice. Con menos de 200 partidas en el duelo no se muestra el % («Pocos datos»). Verde = favorable (52 % o más), rojo = desfavorable (48 % o menos), gris azulado = parejo. Junto a tu línea aparece **tu propio historial** contra ese campeón (de tus partidas guardadas) si tienes 3 o más.
- **Las líneas se deducen.** Spectator no dice quién juega dónde: se asigna con Aplastar (jungla), los porcentajes de línea de cada campeón en OP.GG y, para los jugadores registrados, la línea que más juegan en tu base de datos (se prueban las 120 combinaciones por equipo). Si una línea es dudosa aparece «posición dudosa».
- **Es una estimación.** `logit(P) = w0 + 0,5 · suma de logit(tasa de cada línea) + 0,5 · (dúo propio − dúo enemigo) + 0,1 · (rango medio propio − enemigo, en ligas de 400 puntos cada una)`, con cada tasa acercada al 50 % según su muestra y el resultado limitado a 25–75 %, porque las cinco líneas no son independientes. Un dúo con menos de 30 partidas no cuenta; si solo cuenta uno de los dos, se compara contra un 50 % neutro. Con menos de 3 líneas con datos no se muestra nada. Dice «aún sin calibrar» hasta que se compruebe.
- **Calibración con el tiempo.** Cada estimación mostrada al empezar la partida se guarda en `data/lol.db` (tabla `predictions`); cuando sincronizas esa partida se anota si ganó tu equipo (las de menos de 15 minutos, remakes o rendiciones tempranas con alguien ausente, no cuentan: no dicen nada de la selección). Con 20 resultados aparece «Acierta el X % (N partidas, Brier B)». Con 100 o más, el panel prueba a reajustar los pesos (regresión logística con penalización hacia los pesos por defecto, en Python puro, dejando fuera de la prueba el 25 % más reciente) y **solo si ganan a los pesos por defecto** en esas partidas aparte los guarda en `data/winprob_model.json` (se puede borrar para volver a los pesos por defecto).
- **Cuántas peticiones.** Unas 9 a OP.GG por partida en frío, hasta 11 si algún dúo hay que buscarlo por el soporte (5 duelos, 2 a 4 listas de dúos, lista de campeones y porcentajes de línea); todo se guarda 24 horas en la tabla `opgg_cache` y se invalida al cambiar de parche. Si OP.GG no responde, la sección dice «Sin datos de OP.GG» y el resto del escaneo sigue igual. Para desactivarlo del todo, define la variable de entorno `LOLAN_OPGG=off`.
- **Política.** Solo datos: nada de consejos sobre qué elegir ni sobre abandonar la partida.

Para pruebas automáticas, la variable `LOLAN_TEST_RIOT_URL` cambia la dirección de la API de Riot (solo acepta direcciones locales; no la uses normalmente).

## Dónde queda todo

| Ruta | Contenido |
|---|---|
| `data/lol.db` | Base de datos SQLite con el JSON crudo de cada partida y timeline (y la caché de 6 horas del escaneo de Rivales, tabla `scout_cache`) |
| `data/winprob_model.json` | Pesos reajustados de la probabilidad estimada de En vivo (solo existe tras 100 partidas con resultado; se puede borrar) |
| `data/gold_model.json` | Coeficientes y errores del modelo de oro estimado de la página En vivo (se recalcula solo; se puede borrar) |
| `data/capturas/` | Grabaciones de la selección de campeones del cliente de League (`seleccion-*.jsonl`, las 30 más recientes; se pueden borrar) |
| `data/ddragon/` | Mapa, íconos y nombres (habilidades, hechizos de invocador, runas) en español descargados de Data Dragon (se pueden borrar: se vuelven a bajar) |
| `data/lineas/` | Muestras de líneas en JSON (filas, agregados por campeón y rival; ver [Líneas](#líneas-cómo-va-cada-cruce)); tus filas se regeneran con `lineas --reconstruir`; las `diamante` y `descarga.json` vienen de `muestras` (ver [Muestra Diamante+](#muestra-diamante-qué-pasa-en-las-líneas-de-los-campeones-de-tu-clan)) |
| `data/games.csv` | Una fila por partida analizable con todas las métricas |
| `reports/resumen.md` | Resumen general, tendencias y lo que decide tus partidas por campeón |
| `reports/campeones/<Campeón>.md` | Promedios y qué separa tus victorias de tus derrotas con ese campeón |
| `reports/partidas/<matchId>.md` | Boleta de cada una de tus últimas 30 partidas |
| `reports/<cola>/…` y `data/games-<cola>.csv` | Lo mismo, pero solo de una cola (`analyze --cola solo` → `reports/solo/`, `data/games-solo.csv`) |
| `reports/jugadores/<nombre>/…` y `data/games-<nombre>[-<cola>].csv` | Lo mismo para cada jugador del clan (`analyze --jugador "Nombre#TAG"` o `analyze --clan`); `<nombre>/<cola>/` si usas `--cola` |

`data/` y `reports/` están en `.gitignore`.

## Cómo se calculan algunas métricas

- **Rival de línea**: el jugador del equipo contrario con el mismo `teamPosition`. Si no existe, las comparaciones con el rival quedan vacías.
- **Minutos 7, 14, 21 y 28**: CS, oro y experiencia tuyos y diferencia contra el rival de línea, solo si la partida llegó a ese minuto.
- **Fases**: 0-7, 7-14, 14-21, 21-28 y 28+ minutos. Se cuentan kills, muertes, asistencias, wards colocadas, objetivos épicos (dragones, heraldo, barón, larvas del vacío) y estructuras por equipo.
- **Muerte en solitario**: te mató un campeón y nadie recibió asistencia en esa kill.
- **Muerte en la mitad enemiga**: el equipo azul (100) es dueño de la esquina inferior izquierda; una posición es "mitad enemiga" para azul si `x + y > 15000` y para rojo si `x + y < 15000`.
- **Debilidades marcadas** en la boleta: percentil propio de 25 o menos, 2 o más muertes antes del minuto 14, 2 o más muertes en solitario, muchas muertes en la mitad enemiga, diferencia de oro al minuto 14 menor a -500, entre otras. **Fortalezas**: percentil propio de 75 o más o ventaja clara sobre el rival.

## Scripts de Windows

| Archivo | Para qué |
|---|---|
| `instalar.bat` | Doble clic. Busca Python 3.10+ (si falta, lo instala con `winget`), crea `.venv`, instala `requirements.txt`, instala ffmpeg si falta (para el análisis de video; si falla no pasa nada), crea el acceso directo «LoL Analyzer» en el escritorio y abre el panel. Se puede repetir sin problema. Opciones: `/sin-acceso-directo`, `/sin-abrir` |
| `abrir-panel.bat` | Abre el panel (`python -m lolan web`); acepta las mismas opciones, por ejemplo `--no-abrir` |
| `actualizar.bat` | Descarga la última versión de `main`, copia los archivos nuevos sin borrar nada y sin tocar `.env`, `data\`, `reports\` ni `.venv\`, y revisa las librerías. Se copia a `%TEMP%` antes de actualizar porque `cmd` lee los `.bat` poco a poco y se reemplaza a sí mismo; se niega a correr si el panel está abierto o si la carpeta es un repositorio git. Para pruebas, `LOLAN_ZIP_URL` apunta a otro zip (dirección o ruta local) y `LOLAN_PUERTO` al puerto que revisa |

Los `.bat` usan solo caracteres ASCII (cmd se confunde con acentos en algunos Windows) y terminan en CRLF (ver `.gitattributes`).

## Pruebas

```bash
.venv/Scripts/python -m pytest -q
```

Las pruebas usan partidas y timelines sintéticos; no necesitan red ni API key. Las del script de la página En vivo (sección Rivales) usan Node.js si está instalado (si no, se omiten).

## Notas sobre la base de datos

Cada partida se guarda una sola vez, junto con la lista de los 10 jugadores que participaron (tabla `match_players`); así, una partida que jugaron dos del clan cuenta para ambos y cada análisis solo lee las partidas de su jugador. La tabla `account` es el registro de jugadores. Si tienes una base de datos creada con una versión anterior (un solo jugador), se actualiza sola la primera vez que se abre (tarda menos de un segundo para cientos de partidas) y tu cuenta queda como jugador principal.

## Aviso legal

lol-analyzer isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.

## Licencia

[MIT](LICENSE). Copyright (c) 2026 EpsilonOsiris.
