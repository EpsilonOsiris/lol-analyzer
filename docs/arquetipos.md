# Arquetipos por línea: propuesta basada en clases oficiales

Fecha de los datos: 23-oct-2026 (Data Dragon 16.21.1). Archivo con todo el detalle por campeón:
`lolan/data/champion_classes.json` (173 campeones, todos con clase oficial; ninguno quedó "sin clase oficial").

Esto es una **propuesta para revisar**. No se cambió `comps.py`.

## 1. Fuentes

- **Clase y subclases oficiales de Riot**: League of Legends Wiki, módulo `Module:ChampionData/data`
  (campo `role`, la fuente de la página "List of champions"). Verificado contra la tabla de
  <https://wiki.leagueoflegends.com/en-us/List_of_champions> (173 de 173 coinciden) y contra las páginas
  individuales de Singed, Ambessa, K'Sante, Zaahen, Bard, Nunu, Mel, Yunara, Locke y Dr. Mundo.
  Los campeones nuevos (Zaahen, Yunara, Mel, Locke, Ambessa, Aurora, Hwei...) ya traen clase en la wiki.
  La espejo de Fandom no hizo falta. 19 campeones tienen dos subclases (se guardan todas).
- **Tags de Data Dragon** (Fighter/Tank/Mage/Assassin/Marksman/Support): versión 16.21.1, `en_US`.
  Se guardan, pero son el sistema viejo y no se usan para decidir.
- **Roles de OP.GG** por línea: herramienta `lol_get_champion_analysis` del MCP de OP.GG (partidas ranked,
  todos los rangos). Devuelve, por línea, el reparto de `MAGE / FIGHTER / TANK / SLAYER / MARKSMAN /
  CONTROLLER`. Ojo: es **cómo se construye** el campeón en esa línea (por los objetos), no su diseño.
  Por eso Gwen top sale 99% MAGE y Ezreal 96% FIGHTER. Sirve para desempatar, no para clasificar solo.
- **Tus partidas**: `data/lol.db`, 551 partidas de Grieta, 1102 apariciones por línea (los 10 jugadores).
  Las tablas van ordenadas por esa frecuencia ("apar.").

## 2. Cobertura (% de apariciones en tus partidas con tipo asignado)

| Línea | Hoy | Con la propuesta |
|---|---|---|
| Top | 91.9% | 99.1% |
| Jungla | 93.2% | 99.9% |
| Mid | 93.4% | 97.7% |
| ADC (bot) | 89.0% | 97.5% |
| Soporte | 92.8% | 99.1% |

## 3. Cómo se traduce subclase -> arquetipo

Las subclases oficiales son: Controller (Enchanter, Catcher), Fighter (Juggernaut, Diver), Mage (Burst,
Battlemage, Artillery), Marksman, Slayer (Assassin, Skirmisher), Tank (Vanguard, Warden) y Specialist.
Una columna "criterio" significa que la subclase no decide y puse un juicio mío (se explica en la nota).

| Línea | Regla de la propuesta |
|---|---|
| **Top** | Vanguard / Warden -> `tanque`. Juggernaut -> `bruiser` (si OP.GG lo marca TANK >= 90%, `tanque`: Dr. Mundo). Diver -> `bruiser`. Skirmisher / Assassin -> `split`. Burst / Battlemage / Artillery -> **`mago` (tipo nuevo)**. Specialist: a mano. |
| **Jungla** | Vanguard / Warden -> `tanque`. Diver / Assassin -> `early`. Skirmisher / Juggernaut / Marksman / magos -> `farm`. Specialist: a mano. |
| **Mid** | Assassin (y Burst/Assassin) -> `asesino`. Burst / Battlemage / Artillery -> `control`. Skirmisher / Diver / Juggernaut / Vanguard / Warden -> `melee`. Assassin + Skirmisher (Yone) -> `melee`. |
| **ADC** | Se **mantienen** followup / kiting / flexible tal cual (vienen del video). Magos oficiales sin tipo -> **`mago` (tipo nuevo)**. |
| **Soporte** | Vanguard -> `engage`. Catcher -> `engage` (si OP.GG lo marca MAGE >= 60%, `poke`). Enchanter -> `peel`. Warden -> `peel`. Burst / Artillery / Battlemage -> `poke`. Thresh y Bard -> `mixto` a mano. |

Sobre los tipos de la propuesta:

- **Soporte**: los cuatro tipos actuales (engage, peel, mixto, poke) encajan bien con Vanguard+Catcher / Warden+Enchanter
  / mixto / magos. No propongo separar Catcher de Vanguard: para el ADC follow-up ambos son "alguien que abre pelea".
  Lo que sí cambia es que **Warden -> peel** (Braum, Taric y Tahm Kench ya eran peel en el video).
- **Top y ADC necesitan un tipo `mago`**: hoy Vladimir, Rumble, Jayce, Kennen y compañía figuran como "split pusher" o
  "tanque", y eso contamina `team_plan` (la señal `split` exige un top de presión de carril y poco engage). En ADC, unos 19
  magos (Ziggs, Hwei, Viktor, Veigar, Syndra, Swain, Lux, Seraphine, Brand...) suman 8% de las apariciones y no
  encajan en followup/kiting/flexible. Si se acepta `mago`, hay que añadirle fila en la tabla `FIT` (qué soporte le sirve).
  Alternativa sin tipo nuevo: top `mago` -> `split` (como hoy) y ADC mago -> `kiting` (piden peel, son frágiles).
- **Jungla y mid**: los tres tipos actuales sirven; solo cambian algunos campeones.

**Los tipos de ADC vienen de un video de coaching** (followup, kiting, flexible). Los cambios que propongo ahí son solo
añadir el tipo `mago` para los que no tenían y mover a Mel (que era "propia"). No toco ninguno del video.


## 4.1 Top

### Cambios sobre lo que hay hoy (ordenados por apariciones)

| Campeón | Apar. | Subclase oficial | OP.GG en la línea | Hoy -> propuesta | Por qué |
|---|---|---|---|---|---|
| Volibear | 25 | Juggernaut | Mage 39%, Tank 38% | tanque -> **bruiser** (regla) | Juggernaut. |
| Jayce | 20 | Artillery | Slayer 52%, Fighter 47% | split -> **mago** (criterio) | Artillery oficial (OP.GG lo reparte SLAYER/FIGHTER por sus builds). Dudoso: hoy es split. |
| Zaahen | 15 | Skirmisher | Fighter 98% | tanque -> **split** (criterio) | Skirmisher oficial; hoy figura como tanque y no lo parece. Dudoso: podria ser bruiser. |
| Kennen | 8 | Specialist | Mage 98% | tanque -> **mago** (criterio) | Specialist con OP.GG MAGE 98%: mago a distancia. Su entrada sigue en HARD_ENGAGE. Dudoso: hoy es 'tanque'. |
| Vladimir | 8 | Battlemage | Mage 100% | split -> **mago** (regla) | Mago oficial (Battlemage). |
| Rumble | 5 | Battlemage | Mage 100% | split -> **mago** (regla) | Mago oficial (Battlemage). |

(Hay 1 cambio(s) más en campeones que no salieron en tus partidas ni son habituales de la línea: están en el JSON.)

### Hoy sin clasificar -> propuesta (solo los vistos en tus partidas, más frecuentes primero)

| Campeón | Apar. | Subclase oficial | Propuesta | Por qué |
|---|---|---|---|---|
| Singed | 17 | Specialist | **tanque** (criterio) | Specialist: la subclase no decide; la wiki lo tuvo como Tank y OP.GG lo reparte MAGE 74% / TANK 26%. Dudoso (podria ser split/proxy). |
| Varus | 6 | Marksman/Artillery | **mago** (regla) | Mago oficial (Marksman/Artillery). |
| Heimerdinger | 5 | Specialist | **mago** (criterio) | Specialist con OP.GG MAGE 100%. |
| Zed | 4 | Assassin | **split** (regla) | Subclase Assassin. |
| Gragas | 4 | Vanguard | **tanque** (regla) | Subclase Vanguard. |
| Swain | 3 | Battlemage | **mago** (regla) | Mago oficial (Battlemage). |
| Master Yi | 3 | Skirmisher | **split** (regla) | Subclase Skirmisher. |
| Ryze | 3 | Battlemage | **mago** (regla) | Mago oficial (Battlemage). |
| Zac | 3 | Vanguard | **tanque** (regla) | Subclase Vanguard. |
| Kayn | 3 | Skirmisher | **split** (regla) | Subclase Skirmisher. |
| Anivia | 2 | Battlemage | **mago** (regla) | Mago oficial (Battlemage). |
| Twisted Fate | 2 | Burst | **mago** (regla) | Mago oficial (Burst). |
| Locke | 2 | Assassin | **split** (regla) | Subclase Assassin. |
| Viktor | 2 | Battlemage | **mago** (regla) | Mago oficial (Battlemage). |
| Ahri | 2 | Burst | **mago** (regla) | Mago oficial (Burst). |
| Rengar | 2 | Assassin/Diver | **bruiser** (regla) | Diver (la subclase no decide entre bruiser y split). |
| Vi | 2 | Diver | **bruiser** (regla) | Diver (la subclase no decide entre bruiser y split). |

Con 1 aparición: Katarina (split), Veigar (mago), Cassiopeia (mago), Lissandra (mago), Aurelion Sol (mago), Shyvana (bruiser), Briar (bruiser), Malzahar (mago), Alistar (tanque), Brand (mago), Syndra (mago), Xin Zhao (bruiser), Diana (bruiser), Rek'Sai (bruiser).

Siguen sin tipo (picks fuera de rol, sin encaje): Corki (2), Zilean (2), Graves (1), Smolder (1), Janna (1), Azir (1), Jhin (1), Senna (1).

### Preguntas abiertas

- ¿Quieres el tipo nuevo `mago` en top (Vladimir, Rumble, Jayce, Kennen, Heimerdinger, Ryze...)? Si no, ellos se quedan en `split`.
- Diver top (Renekton, Olaf, Pantheon, Wukong, Ambessa, Warwick) -> `bruiser` por defecto. Camille e Irelia los dejé en `split`. ¿Estás de acuerdo?
- Volibear es Juggernaut oficial (propuesta `bruiser`), pero se juega de frontline y está en HARD_ENGAGE. ¿Lo dejamos `tanque`?
- Zaahen: Skirmisher oficial (propuesta `split`). No sé si es duelista de split o bruiser; hoy figura como `tanque`.
- Singed: la subclase (Specialist) no decide. Propuse `tanque`; también podría ser `split` (proxy).

## 4.2 Jungla

### Cambios sobre lo que hay hoy (ordenados por apariciones)

| Campeón | Apar. | Subclase oficial | OP.GG en la línea | Hoy -> propuesta | Por qué |
|---|---|---|---|---|---|
| Diana | 24 | Assassin/Diver | Mage 99% | farm -> **early** (regla) | Subclase Assassin/Diver. |
| Ekko | 17 | Assassin | Mage 100% | farm -> **early** (regla) | Subclase Assassin. |
| Hecarim | 13 | Diver | Fighter 89%, Slayer 10% | farm -> **early** (regla) | Subclase Diver. |
| Zaahen | 12 | Skirmisher | - | tanque -> **farm** (regla) | Subclase Skirmisher. |

### Hoy sin clasificar -> propuesta (solo los vistos en tus partidas, más frecuentes primero)

| Campeón | Apar. | Subclase oficial | Propuesta | Por qué |
|---|---|---|---|---|
| Ambessa | 18 | Diver/Skirmisher | **early** (regla) | Subclase Diver/Skirmisher. |
| Shaco | 11 | Assassin | **early** (regla) | Subclase Assassin. |
| Malphite | 9 | Vanguard | **tanque** (regla) | Subclase Vanguard. |
| Zed | 8 | Assassin | **early** (regla) | Subclase Assassin. |
| Jax | 7 | Skirmisher | **farm** (regla) | Subclase Skirmisher. |
| Locke | 4 | Assassin | **early** (regla) | Subclase Assassin. |
| Gragas | 3 | Vanguard | **tanque** (regla) | Subclase Vanguard. |
| Taliyah | 2 | Battlemage | **farm** (regla) | Subclase Battlemage. |
| Zyra | 2 | Catcher | **farm** (criterio) | Catcher oficial pero OP.GG MAGE 100% en jungla (muy pocas partidas). |

Con 1 aparición: Camille (early), Tryndamere (farm), Draven (farm), Sion (tanque), Yorick (farm), Twitch (farm), Olaf (early), Fizz (early), Mordekaiser (farm), Brand (farm).

Siguen sin tipo (picks fuera de rol, sin encaje): Morgana (1).

### Preguntas abiertas

- Diana, Ekko y Hecarim son `farm` hoy, pero Assassin/Diver oficial los manda a `early`. ¿Mantenemos `farm` para ellos (la subclase no distingue early de farm)?
- Dr. Mundo sigue `tanque` solo por la excepción de OP.GG (TANK 98%); su subclase oficial (Juggernaut) diría `farm`.
- Ivern (Catcher oficial) se queda `tanque` porque protege a un carry; es un caso aparte.
- Zaahen en jungla: Skirmisher -> `farm` (hoy `tanque`).

## 4.3 Mid

### Cambios sobre lo que hay hoy (ordenados por apariciones)

| Campeón | Apar. | Subclase oficial | OP.GG en la línea | Hoy -> propuesta | Por qué |
|---|---|---|---|---|---|
| Galio | 29 | Warden | Mage 64%, Tank 24% | asesino -> **control** (criterio) | Warden oficial, pero en mid OP.GG lo marca MAGE 64%: waveclear y control. Hoy es 'asesino'. Dudoso. |
| Locke | 22 | Assassin | Mage 100% | melee -> **asesino** (regla) | Subclase Assassin. |
| Neeko | 3 | Burst/Catcher | - | asesino -> **control** (regla) | Mago oficial (Burst/Catcher). |

### Hoy sin clasificar -> propuesta (solo los vistos en tus partidas, más frecuentes primero)

| Campeón | Apar. | Subclase oficial | Propuesta | Por qué |
|---|---|---|---|---|
| Annie | 16 | Burst | **control** (regla) | Mago oficial (Burst). |
| Nasus | 3 | Juggernaut | **melee** (regla) | Subclase Juggernaut. |
| Camille | 3 | Diver | **melee** (regla) | Subclase Diver. |
| Riven | 3 | Skirmisher | **melee** (regla) | Subclase Skirmisher. |
| Kayle | 3 | Specialist | **control** (criterio) | Specialist con OP.GG MAGE 97%. |
| Mordekaiser | 3 | Juggernaut | **melee** (regla) | Subclase Juggernaut. |
| Brand | 2 | Burst | **control** (regla) | Mago oficial (Burst). |
| Gragas | 2 | Vanguard | **melee** (regla) | Subclase Vanguard. |
| Kog'Maw | 2 | Marksman | **control** (criterio) | Marksman oficial pero OP.GG MAGE 98% en mid (muy pocas partidas). |
| Ambessa | 2 | Diver/Skirmisher | **melee** (regla) | Subclase Diver/Skirmisher. |

Con 1 aparición: Dr. Mundo (melee), Illaoi (melee), Zaahen (melee), Jax (melee), Xin Zhao (melee), Aatrox (melee), Tryndamere (melee), Rumble (control), Seraphine (control).

Siguen sin tipo (picks fuera de rol, sin encaje): Tristana (4), Jayce (4), Kennen (3), Quinn (2), Smolder (2), Singed (1), Rakan (1), Draven (1), Lucian (1), Gangplank (1).

### Preguntas abiertas

- Locke (22 apariciones): hoy `melee`, pero Riot lo clasifica Assassin (propuesta `asesino`). Es un campeón nuevo y OP.GG lo marca 100% MAGE; ¿lo juegas/ves como asesino o como duelista?
- Ahri, Zoe y Twisted Fate son Burst oficial (la regla diría `control`) pero se juegan de pick/roam: los dejé `asesino` a mano.
- Galio (Warden oficial) pasa de `asesino` a `control` por su waveclear de mago. Su rol de engage sigue en HARD_ENGAGE.

## 4.4 ADC (bot)

### Cambios sobre lo que hay hoy (ordenados por apariciones)

| Campeón | Apar. | Subclase oficial | OP.GG en la línea | Hoy -> propuesta | Por qué |
|---|---|---|---|---|---|
| Mel | 8 | Artillery | Mage 100% | flexible -> **mago** (regla) | Mago oficial (Artillery) clasificado a mano como flexible; propuesta: tipo propio 'mago'. |

### Hoy sin clasificar -> propuesta (solo los vistos en tus partidas, más frecuentes primero)

| Campeón | Apar. | Subclase oficial | Propuesta | Por qué |
|---|---|---|---|---|
| Ziggs | 16 | Artillery | **mago** (regla) | Mago oficial (Artillery) sin tipo hoy. |
| Viktor | 9 | Battlemage | **mago** (regla) | Mago oficial (Battlemage) sin tipo hoy. |
| Hwei | 9 | Artillery | **mago** (regla) | Mago oficial (Artillery) sin tipo hoy. |
| Lux | 7 | Burst/Artillery | **mago** (regla) | Mago oficial (Burst/Artillery) sin tipo hoy. |
| Veigar | 7 | Burst | **mago** (regla) | Mago oficial (Burst) sin tipo hoy. |
| Swain | 7 | Battlemage | **mago** (regla) | Mago oficial (Battlemage) sin tipo hoy. |
| Seraphine | 7 | Burst/Enchanter | **mago** (regla) | Mago oficial (Burst/Enchanter) sin tipo hoy. |
| Syndra | 7 | Burst | **mago** (regla) | Mago oficial (Burst) sin tipo hoy. |
| Brand | 5 | Burst | **mago** (regla) | Mago oficial (Burst) sin tipo hoy. |
| Vel'Koz | 4 | Artillery | **mago** (regla) | Mago oficial (Artillery) sin tipo hoy. |
| Xerath | 4 | Artillery | **mago** (regla) | Mago oficial (Artillery) sin tipo hoy. |
| Karthus | 3 | Battlemage | **mago** (regla) | Mago oficial (Battlemage) sin tipo hoy. |
| Cassiopeia | 3 | Battlemage | **mago** (regla) | Mago oficial (Battlemage) sin tipo hoy. |
| Vladimir | 2 | Battlemage | **mago** (regla) | Mago oficial (Battlemage) sin tipo hoy. |

Con 1 aparición: LeBlanc (mago), Aurelion Sol (mago), Aurora (mago), Malzahar (mago).

Siguen sin tipo (picks fuera de rol, sin encaje): Yasuo (7), Soraka (3), Locke (3), Kindred (2), Katarina (2), Yone (1), Quinn (1), Fizz (1), Viego (1), Morgana (1).

### Preguntas abiertas

- ¿Aceptas el tipo `mago` para ADC? Si no, ¿van a `kiting`?
- Mel pasa de `flexible` (propia) a `mago`; Corki sigue `flexible`.
- Yasuo, Katarina y similares (Skirmisher/Assassin en bot) quedan sin tipo: son picks fuera de rol.

## 4.5 Soporte

### Cambios sobre lo que hay hoy (ordenados por apariciones)

| Campeón | Apar. | Subclase oficial | OP.GG en la línea | Hoy -> propuesta | Por qué |
|---|---|---|---|---|---|
| Zilean | 8 | Specialist | Controller 82%, Mage 15% | poke -> **peel** (regla) | Specialist; OP.GG lo marca CONTROLLER 82%. |
| Poppy | 8 | Warden | Tank 60%, Controller 34% | engage -> **peel** (regla) | Warden. |

### Hoy sin clasificar -> propuesta (solo los vistos en tus partidas, más frecuentes primero)

| Campeón | Apar. | Subclase oficial | Propuesta | Por qué |
|---|---|---|---|---|
| Bard | 13 | Catcher | **mixto** (criterio) | Catcher oficial; hace picks pero tambien cura/protege. Dudoso: engage o mixto. |
| Mel | 8 | Artillery | **poke** (regla) | Mago oficial (Artillery). |
| Camille | 8 | Diver | **engage** (regla) | Diver (entra a pelear; pick fuera de rol). |
| Shen | 6 | Warden | **peel** (regla) | Warden. |
| Pantheon | 5 | Diver | **engage** (regla) | Diver (entra a pelear; pick fuera de rol). |
| Sylas | 3 | Burst/Skirmisher | **poke** (regla) | Mago oficial (Burst/Skirmisher). |
| Fiddlesticks | 3 | Specialist | **poke** (regla) | Specialist; OP.GG lo marca MAGE. |
| Zoe | 2 | Burst | **poke** (regla) | Mago oficial (Burst). |
| Ziggs | 2 | Artillery | **poke** (regla) | Mago oficial (Artillery). |
| Viktor | 2 | Battlemage | **poke** (regla) | Mago oficial (Battlemage). |
| Galio | 2 | Warden | **peel** (regla) | Warden. |
| Anivia | 2 | Battlemage | **poke** (regla) | Mago oficial (Battlemage). |
| Malphite | 2 | Vanguard | **engage** (regla) | Vanguard. |

Con 1 aparición: Malzahar (poke), Ornn (engage), Singed (poke), Ahri (poke), Jayce (poke), Elise (poke), Veigar (poke), Nunu & Willump (engage), Ivern (engage), Sion (engage), Rammus (engage).

Siguen sin tipo (picks fuera de rol, sin encaje): Shaco (4), Miss Fortune (2), Dr. Mundo (1), Darius (1), Qiyana (1), Ashe (1).

### Preguntas abiertas

- Bard (13 apariciones, Catcher oficial): propongo `mixto` (picks + curas). La regla pura diría `engage`.
- Poppy pasa de `engage` a `peel` (Warden oficial). Sigue en HARD_ENGAGE, así que cuenta como engage para el plan de equipo.
- Zilean pasa de `poke` a `peel`: la subclase (Specialist) no decide; OP.GG lo marca CONTROLLER 82%.
- Senna sigue `poke` aunque es Enchanter oficial: OP.GG casi no la construye como controller (5%).
- Rakan (Catcher) queda `engage`. Podría ser `mixto` (su escudo protege), pero no lo cambié.

## 5. Aparte: `HARD_ENGAGE`

La lista actual coincide casi del todo con las subclases: están todos los Vanguard (falta solo Nunu), los Catcher de
engage y varios Diver. Los que **no** son Vanguard/Catcher/Warden/Diver son Kennen (Specialist), Nocturne (Assassin)
y Sett (Juggernaut): son tus excepciones por habilidad. Los Warden Braum, Shen, Tahm Kench y Taric no están,
y eso es coherente con ser peel. No propongo cambiar la lista salvo evaluar a Nunu.

## 6. Cómo usar el JSON

Cada campeón (clave = id de Data Dragon; Fiddlesticks lleva el alias `FiddleSticks`, que es como lo escribe la API de partidas)
tiene `riot_class`, `riot_classes`, `riot_subclasses`, `dd_tags`, `opgg_roles` por línea (`role`, `rate`),
`lanes` (parte de sus partidas en cada línea según OP.GG, solo >= 5%), `sources` y `proposal` por línea con
`type`, `basis` (`regla`, `manual` o `actual` = se mantiene lo que hay), `current` y `note`.
`proposal` incluye las líneas con >= 5% de uso más las que aparecen en tus partidas.

