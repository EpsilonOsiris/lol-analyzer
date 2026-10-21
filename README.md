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
.venv/Scripts/python -m lolan composiciones [--cola solo] [--todos-los-adc]
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
- `composiciones` analiza tus partidas junto a tu clan (no usa la red): pools de cada amigo, dúo bot por soporte, matriz de arquetipos de ADC x soporte, alineaciones, itemización (penetración de armadura) y Purificar; imprime un resumen con las recomendaciones de ADC para la alineación más común y escribe `reports/composiciones.md`. Un jugador cuenta como del clan si su nombre empieza con `CLAN_PREFIX` (por defecto `Epsilon`) o es un jugador registrado; `CLAN_ALIASES` (`Cuenta=Persona;Otra=Persona`) junta varias cuentas de una misma persona. Ambos se editan en la página Configuración. La página **Composiciones** del panel muestra lo mismo y un formulario «Armar draft». También calcula, desde tu asiento, el winrate de tu equipo por tipo de aliado en cada línea (y por persona del clan), las duplas de tipos (jungla × mid, jungla × top, soporte × ADC) y el «plan de pelea» de cada equipo (engage, proteger al carry, poke, pick, split o mixto) con su cruce contra el plan rival; el plan de ambos equipos aparece en la página de cada partida y en `data/games.csv` (`team_plan`, `enemy_plan`). El formulario recomienda los 3 mejores picks para cada línea sin campeón (los campeones de la persona elegida, o solo tipos si dejas «Aleatorio») y el plan resultante. Las clasificaciones de ADC y soportes salen de un video de coaching, salvo las marcadas como «propia»; las de top, jungla y mid y el plan de pelea son nuestras (guías como EarlyGame, LoLTheory, WeCoach y Mobalytics), no datos oficiales de Riot.
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
| Composiciones (`/composiciones`) | Desde tu asiento y con el clan: pools por persona, dúo bot (por soporte, campeón y arquetipo, con dif. de oro @14 y muertes), matriz ADC x soporte, ADC x engage del equipo, alineaciones y parejas persona+campeón, penetración de armadura contra tanques y Purificar por terciles de CC enemigo; tipos por línea, duplas de tipos, plan de pelea (tuyo y del rival) y el formulario «Armar draft» que recomienda ADC de tu pool y picks para las demás líneas |
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

## Dónde queda todo

| Ruta | Contenido |
|---|---|
| `data/lol.db` | Base de datos SQLite con el JSON crudo de cada partida y timeline |
| `data/ddragon/` | Mapa, íconos y nombres de habilidades en español descargados de Data Dragon (se pueden borrar: se vuelven a bajar) |
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

Las pruebas usan partidas y timelines sintéticos; no necesitan red ni API key.

## Notas sobre la base de datos

Cada partida se guarda una sola vez, junto con la lista de los 10 jugadores que participaron (tabla `match_players`); así, una partida que jugaron dos del clan cuenta para ambos y cada análisis solo lee las partidas de su jugador. La tabla `account` es el registro de jugadores. Si tienes una base de datos creada con una versión anterior (un solo jugador), se actualiza sola la primera vez que se abre (tarda menos de un segundo para cientos de partidas) y tu cuenta queda como jugador principal.

## Aviso legal

lol-analyzer isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.

## Licencia

[MIT](LICENSE). Copyright (c) 2026 EpsilonOsiris.
