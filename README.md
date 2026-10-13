# lol-analyzer

Descarga **todas** tus partidas de League of Legends desde la API de Riot, las guarda en una base de datos local (SQLite) y analiza qué haces bien y qué haces mal, partida por partida y en general.

Qué produce:

- Un reporte por partida ("boleta de calificaciones") que compara tus métricas con las de tu rival de línea y con tu propio historial (primero con ese campeón; si hay menos de 5 partidas, con tu rol; y si no, con todas tus partidas).
- Por campeón (con 5 partidas o más): qué métricas separan más tus victorias de tus derrotas (tamaño del efecto = diferencia de promedios / desviación estándar combinada).
- Un resumen general: winrate por cola, rol y campeón, y tendencias por mes, por día de la semana y por número de partida dentro de una sesión (una sesión es una racha de partidas con menos de 45 minutos de separación).
- Un CSV con una fila por partida y todas las métricas, para que lo explores con tu herramienta favorita.

Solo se analizan las colas de la Grieta del Invocador (400, 420, 430, 440 y 490) y se excluyen los remakes (menos de 5 minutos o rendición temprana). Las demás partidas se guardan en la base de datos, pero se omiten del análisis (puedes incluirlas con `--todas-las-colas`). Para analizar una sola cola, pasa `--cola` a `analyze`, `game` o `campeon` (por ejemplo, `analyze --cola solo`).

## Instalación

Requiere Python 3.10 o superior (probado con 3.13).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Linux / macOS
```

Copia el archivo de configuración y llénalo:

```bash
cp .env.example .env
```

```
RIOT_API_KEY=RGAPI-...        # tu API key
RIOT_ID=Nombre#TAG            # tu Riot ID completo
PLATFORM=la1                  # tu servidor (ver tabla abajo)
REGION=americas               # región de enrutamiento (ver tabla abajo)
```

### Cómo obtener la API key

1. Entra a <https://developer.riotgames.com> con tu cuenta de Riot.
2. Copia la **Development API Key** que aparece en el panel.
3. Pégala en `.env`. **Las keys de desarrollo expiran cada 24 horas**: si recibes el error "la API key es inválida o expiró", genera una nueva (botón "Regenerate API Key"), actualiza `.env` y vuelve a ejecutar el comando. Nada de lo ya descargado se pierde.

La key nunca se imprime en pantalla y `.env` está en `.gitignore`.

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
.venv/Scripts/python -m lolan sync [--max N] [--since AAAA-MM-DD] [--cola solo] [--no-timeline]
.venv/Scripts/python -m lolan analyze [--cola solo]
.venv/Scripts/python -m lolan game <matchId | ultima>
.venv/Scripts/python -m lolan campeon <Nombre>
```

- `sync` descarga las partidas nuevas (la partida y su timeline; cada partida son unas 2 peticiones). Respeta los límites de una key de desarrollo (20 peticiones por segundo y 100 cada 2 minutos), así que rinde unas 50 partidas cada 2 minutos. **Una sincronización completa de ~1000 partidas tarda alrededor de 40 minutos.** Guarda cada partida apenas la descarga: si se interrumpe (Ctrl+C, falla la red, expira la key), vuelve a ejecutar el mismo comando y continúa donde se quedó, sin repetir lo ya guardado.
  - `--max N`: descarga como máximo N partidas nuevas (útil para probar).
  - `--since AAAA-MM-DD`: solo partidas desde esa fecha.
  - `--cola COLA`: solo partidas de esa cola: `solo` (clasificatoria solo/dúo, 420), `flex` (440), `normal` (400), `blind` (430), `quickplay` (490) o el número de cola.
  - `--no-timeline`: no descarga el timeline. Es el doble de rápido, pero se pierden las métricas por minuto (diferencias a los minutos 7/14/21/28, muertes por fase, objetivos, etc.).
- `analyze` reconstruye `data/games.csv` y todos los reportes a partir de lo que ya está en la base de datos (no usa la red, así que puedes correrlo las veces que quieras).
- `game <matchId>` imprime la boleta de una partida; `game ultima` usa la más reciente.
- `campeon <Nombre>` imprime los promedios y lo que decide tus partidas con ese campeón (el nombre no distingue mayúsculas ni apóstrofes: `kaisa` funciona).

## Dónde queda todo

| Ruta | Contenido |
|---|---|
| `data/lol.db` | Base de datos SQLite con el JSON crudo de cada partida y timeline |
| `data/games.csv` | Una fila por partida analizable con todas las métricas |
| `reports/resumen.md` | Resumen general, tendencias y lo que decide tus partidas por campeón |
| `reports/campeones/<Campeón>.md` | Promedios y qué separa tus victorias de tus derrotas con ese campeón |
| `reports/partidas/<matchId>.md` | Boleta de cada una de tus últimas 30 partidas |
| `reports/<cola>/…` y `data/games-<cola>.csv` | Lo mismo, pero solo de una cola (`analyze --cola solo` → `reports/solo/`, `data/games-solo.csv`) |

`data/` y `reports/` están en `.gitignore`.

## Cómo se calculan algunas métricas

- **Rival de línea**: el jugador del equipo contrario con el mismo `teamPosition`. Si no existe, las comparaciones con el rival quedan vacías.
- **Minutos 7, 14, 21 y 28**: CS, oro y experiencia tuyos y diferencia contra el rival de línea, solo si la partida llegó a ese minuto.
- **Fases**: 0-7, 7-14, 14-21, 21-28 y 28+ minutos. Se cuentan kills, muertes, asistencias, wards colocadas, objetivos épicos (dragones, heraldo, barón, larvas del vacío) y estructuras por equipo.
- **Muerte en solitario**: te mató un campeón y nadie recibió asistencia en esa kill.
- **Muerte en la mitad enemiga**: el equipo azul (100) es dueño de la esquina inferior izquierda; una posición es "mitad enemiga" para azul si `x + y > 15000` y para rojo si `x + y < 15000`.
- **Debilidades marcadas** en la boleta: percentil propio de 25 o menos, 2 o más muertes antes del minuto 14, 2 o más muertes en solitario, muchas muertes en la mitad enemiga, diferencia de oro al minuto 14 menor a -500, entre otras. **Fortalezas**: percentil propio de 75 o más o ventaja clara sobre el rival.

## Pruebas

```bash
.venv/Scripts/python -m pytest -q
```

Las pruebas usan partidas y timelines sintéticos; no necesitan red ni API key.
