# Arquetipos por línea: clases oficiales, criterio y datos

Fecha de los datos: 23-oct-2026 (Data Dragon 16.21.1). Archivo con todo el detalle por campeón:
`lolan/data/champion_classes.json` (173 campeones, todos con clase oficial).

**Estado: aplicado.** `lolan/comps.py` ya no tiene listas escritas a mano: construye las tablas de tipos
(ADC, soporte, top, jungla, mid) desde ese JSON cuando se importa. Los ADC y soportes del video de
coaching se conservan tal cual y ganan al JSON. Los casos dudosos quedan marcados «pendiente de datos» hasta
que `python -m lolan arquetipos --decidir` los decida con la muestra Diamante+.

## 1. Fuentes

- **Clase y subclases oficiales de Riot**: League of Legends Wiki, módulo `Module:ChampionData/data`
  (campo `role`, la fuente de la página "List of champions"). Verificado contra la tabla de
  <https://wiki.leagueoflegends.com/en-us/List_of_champions> (173 de 173 coinciden). 19 campeones tienen dos
  subclases (se guardan todas).
- **Tags de Data Dragon** (Fighter/Tank/Mage/Assassin/Marksman/Support), versión 16.21.1: se guardan, pero
  son el sistema viejo y no se usan para decidir.
- **Roles de OP.GG** por línea (`lol_get_champion_analysis`, partidas ranked de todos los rangos): cómo se
  *construye* el campeón en esa línea, no su diseño (Gwen top sale 99% MAGE). Sirve para desempatar.
- **El video de coaching** (un soporte Challenger): solo ADC y soportes.
- **Muestra Diamante+** (`data/lineas/`, fuente `diamante`): decide los casos dudosos (sección 5).

## 2. De dónde sale cada tipo (el «origen»)

Cada entrada de las tablas lleva un origen, que el panel y los reportes muestran en español:

| Origen (`source`) | Se muestra como | Qué es |
|---|---|---|
| `video` | del video | ADC y soportes que nombra el video. Ganan siempre al JSON. |
| `oficial` | clase oficial de Riot | Regla aplicada a la subclase oficial (`basis: regla` en el JSON). |
| `criterio` | criterio | La subclase no decide y hay un juicio manual (`basis: manual`), o se mantiene lo que había (`basis: actual`). |
| `datos` | por datos | Decidido por `arquetipos --decidir` (`basis: datos`, con la evidencia y la fecha en el JSON). |
| `pendiente` | pendiente de datos | Caso dudoso de `comps.PENDING_DATA`: conserva el valor de la propuesta hasta que los datos decidan. |

Los 4 campeones que estaban en las tablas viejas pero no en la propuesta (Rammus y Skarner top, Rell jungla,
Corki mid) se añadieron al JSON con `basis: actual`. Fiddlesticks lleva el alias `FiddleSticks`, que es como lo
escribe la API de partidas: las tablas usan esa ortografía.

Cobertura de las tablas: top 88, jungla 75, mid 74, ADC 44 (17 del video, 19 magos), soporte 60 campeones.
Quien no aparece figura «Sin clasificar» (picks fuera de rol).

## 3. Cómo se traduce subclase -> arquetipo

Las subclases oficiales son: Controller (Enchanter, Catcher), Fighter (Juggernaut, Diver), Mage (Burst,
Battlemage, Artillery), Marksman, Slayer (Assassin, Skirmisher), Tank (Vanguard, Warden) y Specialist.

| Línea | Regla |
|---|---|
| **Top** | Vanguard / Warden -> `tanque`. Juggernaut -> `bruiser` (Dr. Mundo, con OP.GG TANK >= 90%, `tanque`). Diver -> `bruiser`. Skirmisher / Assassin -> `split`. Burst / Battlemage / Artillery -> **`mago`**. Specialist: a mano (criterio). |
| **Jungla** | Vanguard / Warden -> `tanque`. Diver / Assassin -> `early`. Skirmisher / Juggernaut / Marksman / magos -> `farm`. Specialist: a mano. |
| **Mid** | Assassin -> `asesino`. Burst / Battlemage / Artillery -> `control`. Skirmisher / Diver / Juggernaut / Vanguard / Warden -> `melee`. Assassin + Skirmisher (Yone) -> `melee`. Ahri, Zoe y Twisted Fate (Burst) se quedan `asesino` a mano. |
| **ADC** | followup / kiting / flexible del video, intactos. Magos oficiales sin tipo -> **`mago`**. Los demás ADC que ya estaban (Jhin, Yunara, Miss Fortune, Kalista, Nilah, Xayah, Smolder, Corki) mantienen su tipo con origen «criterio». |
| **Soporte** | Vanguard -> `engage`. Catcher -> `engage` (si OP.GG lo marca MAGE >= 60%, `poke`). Enchanter -> `peel`. Warden -> `peel`. Burst / Artillery / Battlemage -> `poke`. Thresh y Bard -> `mixto` a mano. |

**Cambios más visibles respecto a las tablas anteriores** (el detalle por campeón está en el JSON, campo
`proposal[línea].current`): Vladimir, Rumble, Jayce, Kennen, Heimerdinger y otros top -> `mago`; Zaahen top
`tanque` -> `split`; Volibear top `tanque` -> `bruiser`; Diana, Ekko y Hecarim jungla `farm` -> `early`; Zaahen
jungla `tanque` -> `farm`; Galio mid `asesino` -> `control`; Neeko mid `asesino` -> `control`; Locke mid `melee`
-> `asesino`; Poppy soporte `engage` -> `peel` (sigue en `HARD_ENGAGE`); Zilean soporte `poke` -> `peel`; Mel ADC
`flexible` -> `mago`; unos 18 magos que eran «Sin clasificar» en el ADC (Ziggs, Hwei, Viktor, Lux...).

## 4. El tipo `mago`

Existe en **top** y en **ADC** (en soporte y mid los magos ya caben en `poke` y `control`). En top: Vladimir,
Rumble, Jayce, Kennen, Heimerdinger, Ryze, Swain, Viktor, Syndra... En ADC unos 19 (8% de las apariciones en la
línea). Qué cambia en el código:

- Etiqueta «Mago», orden de los tipos, fila en las tablas por línea, matriz ADC x soporte y ADC x engage.
- `team_plan`: un top mago **no** cuenta como `split`; un mago de artillería en el ADC (Ziggs, Hwei, Xerath, Lux,
  Vel'Koz, Mel) cuenta como fuente de `poke`, igual que esos campeones en mid.
- Draft: top y ADC ofrecen el tipo «Mago» (representantes neutros: Vladimir y Syndra); un ADC mago puntúa por la
  tabla `FIT`.
- **Números propios, sin calibrar** (están comentados como tales en `comps.py`): `FIT["mago"]` = engage 0.1,
  mixto 0.3, peel 0.3, poke 0.0; `ENGAGE_FIT["mago"]` = 0 / 0 / 0.1 / 0.2 según haya 0, 1, 2 o 3+ engages;
  soporte desconocido 0.0; regla del top mago = +0.1 neutra; un ADC mago suma a «el resto aguanta el 4v4» en la
  regla del split. Se calibrarán con datos.

## 5. Casos dudosos y cómo los decide la muestra

`comps.PENDING_DATA` lista, por línea, los campeones cuyo tipo es un juicio y no una regla:

- **Top (18)**: los Diver con presencia real en la línea (Ambessa, Olaf, Pantheon, Renekton, Warwick, Wukong),
  Camille, Irelia, Volibear, Zaahen, Singed, y los casos manuales (Cho'Gath, Gangplank, Gnar, Kayle, Quinn,
  Teemo, Vayne).
- **Jungla (10)**: Diana, Ekko, Hecarim y los casos manuales early/farm (Fiddlesticks, Graves, Nidalee, Quinn,
  Teemo, Trundle, Zyra).
- **Mid (5)**: Locke, Ahri, Zoe, Twisted Fate y Sylas (asesino o melee).
- Siguen siendo «criterio» (no los decide ningún dato por ahora): los magos de top que no son `mago` por regla
  (Jayce, Kennen, Heimerdinger), Galio y Azir en mid, Bard y Thresh en soporte, e Ivern, Cho'Gath y Volibear
  en jungla.

`python -m lolan arquetipos [--decidir] [--linea X]` (módulo `lolan/archetypes_data.py`) usa las filas `diamante`
de los **3 parches más recientes** y calcula, por campeón y línea, **puntajes z contra el promedio de la línea**
(media y desviación de los campeones de esa línea con al menos 30 partidas; 0 = un campeón típico):

| Evidencia | De qué se compone |
|---|---|
| **early-ness** | oro+exp ganado por minuto contra el rival en los niveles 1-5; `diff14`; win rate en partidas de menos de 25 min menos el de partidas de 35+ min (si hay 8+ de cada una) |
| **tank-ness** | parte del daño recibido + automitigado del equipo; segundos de CC por minuto |
| **split-ness** | daño a edificios por minuto; placas de torre; y una participación en kills **menor** |
| **carry** | parte del daño a campeones del equipo (se muestra, no decide) |

Reglas (M = 0.25 de margen; hace falta un mínimo de **30 partidas** del campeón en esa línea y los puntajes que
la regla usa, si no el campeón sigue «pendiente»):

- **Jungla**: `early` si early-ness >= +M, `farm` si <= -M, si no, evidencia ambigua (pendiente).
- **Top**: `tanque` si tank >= +M y tank - split >= M; `split` si split >= +M y split - tank >= M; `bruiser` en
  cualquier otro caso (es el punto medio entre los dos).
- **Mid**: se mide `lean = tank - 0.5 * early` (los duelistas melee reciben daño, los asesinos explotan temprano):
  `melee` si lean >= +M, `asesino` si <= -M, si no, pendiente.

Sin `--decidir` solo imprime la tabla de evidencia y un **reporte de sanidad**: campeones con 30+ partidas cuya
evidencia contradice con fuerza (|z| >= 1) su tipo actual. No cambia nada. Con `--decidir` escribe en el JSON
`proposal[línea].type`, `basis: "datos"`, `evidence` (los puntajes y las medidas), `n` (partidas), `patches`,
`date` y `previous` (el tipo anterior). Repetirlo con datos nuevos vuelve a decidir; si los datos ya no alcanzan,
la decisión anterior se conserva. Hay que reiniciar el panel para que cargue las tablas nuevas.

**Limitaciones**: las medidas de línea (oro+exp, diff14) están contra el rival, así que mezclan «es temprano» con
«es fuerte»; el win rate corto menos largo ayuda a separarlos. Los campos de estilo (daño a edificios, daño
recibido, CC, participación en kills, placas) se guardan desde las filas nuevas; las anteriores no los tienen.

### Estado con los datos del 23-oct-2026

Las filas Diamante+ ya descargadas (parches 16.19-16.21) no traen los campos de estilo (el descargador arrancó
antes del cambio). Hoy solo se puede decidir la **jungla** (usa solo medidas que ya existen): de 10 casos, 8 tienen
decisión y 2 (Ekko, Trundle) quedan ambiguos. Top y mid quedan pendientes hasta que el descargador, reiniciado
con el código nuevo, acumule partidas con los campos nuevos (30 por campeón y línea).

## 6. `HARD_ENGAGE`

La lista coincide casi del todo con las subclases: están todos los Vanguard (falta solo Nunu), los Catcher de
engage y varios Diver. Los que **no** son Vanguard/Catcher/Warden/Diver son Kennen (Specialist), Nocturne
(Assassin) y Sett (Juggernaut): son excepciones por habilidad. Los Warden Braum, Shen, Tahm Kench y Taric no
están, y eso es coherente con ser peel. No se cambió la lista.

## 7. Cómo usar el JSON

Cada campeón (clave = id de Data Dragon; Fiddlesticks lleva el alias `FiddleSticks`) tiene `riot_class`,
`riot_classes`, `riot_subclasses`, `dd_tags`, `opgg_roles` por línea (`role`, `rate`), `lanes` (parte de sus
partidas en cada línea según OP.GG, solo >= 5%), `sources` y `proposal` por línea con `type`, `basis` (`regla`,
`manual`, `actual` o `datos`), `current` y `note`; las entradas decididas por datos añaden `evidence`, `n`,
`patches`, `date` y `previous`. `proposal` incluye las líneas con >= 5% de uso más las que aparecen en las
partidas del clan. Un tipo `otro` significa «sin clasificar».
