# Cómo instalar LoL Analyzer (guía para quien no programa)

LoL Analyzer descarga tus partidas de League of Legends, las analiza y te muestra qué haces bien y qué puedes mejorar, en una página que se abre en tu navegador. También puedes ver a los demás miembros de tu clan.

No necesitas saber de computación: solo seguir estos pasos en orden. Tarda unos 10 minutos.

## Lo que necesitas

- Un computador con **Windows 10 u 11**.
- Más o menos **1 GB libre** en el disco.
- Conexión a internet.
- Tu cuenta de Riot (la misma con la que juegas).

## Paso 1. Descargar el programa

1. Descarga este archivo: [lol-analyzer.zip](https://github.com/EpsilonOsiris/lol-analyzer/archive/refs/heads/main.zip).
2. Búscalo en tu carpeta de **Descargas**. Haz **clic derecho** sobre el archivo y elige **«Extraer todo...»**.
3. En la ventana que aparece, cambia la ubicación a tu carpeta **Documentos** y pulsa **«Extraer»**.

> **Importante:** no abras el programa desde dentro del zip. Tiene que estar extraído en una carpeta normal, por ejemplo `Documentos\lol-analyzer-main`. Después de instalar, no la muevas de lugar.

## Paso 2. Instalar

1. Abre la carpeta que acabas de extraer.
2. Haz **doble clic** en el archivo **`instalar.bat`**.
3. Es probable que Windows muestre una pantalla azul que dice **«Windows protegió su PC»**. Es normal: Windows lo dice con cualquier programa pequeño y gratuito que no es muy conocido. Para continuar, pulsa **«Más información»** y luego **«Ejecutar de todas formas»**.
4. Se abrirá una ventana negra con mensajes. **No la cierres** hasta que diga **«Instalación terminada»**. Puede tardar varios minutos, sobre todo la primera vez.

Esto es lo que hace `instalar.bat` por ti:

- Revisa si tienes **Python** (un programa que hace falta para que funcione). Si no lo tienes, lo instala solo. Si Windows pide permiso, acepta.
- Descarga las **librerías** del programa (piezas auxiliares). Necesita internet.
- Instala **ffmpeg** si no lo tienes. Servirá para el análisis de video, que llegará pronto. Si falla, no pasa nada.
- Crea un **acceso directo llamado «LoL Analyzer» en tu escritorio**.
- Abre el panel en tu navegador.

Si algo falla, el programa te lo explica en la ventana y espera a que la leas antes de cerrarse. Mira la sección «Problemas comunes» al final.

## Paso 3. Conseguir tu key de Riot

La **key** es como una contraseña gratuita que Riot da a los programas para que puedan pedirle tus partidas. Es personal.

1. Entra a <https://developer.riotgames.com> e inicia sesión con tu cuenta de Riot.
2. Acepta los términos si te los piden.
3. En la página principal verás un recuadro llamado **«Development API Key»**. Pulsa el botón de copiar (o selecciona el texto que empieza con `RGAPI-` y cópialo).

**Esta key dura 24 horas.** Mañana tendrás que repetir este paso (mira «Renovar la key cada día»).

**Si no quieres renovarla todos los días:** en esa misma página pide una **Personal API Key**. Es gratis y no expira. Busca la opción «Register Product» y elige «Personal API Key». Llena el formulario contando que es una herramienta personal para analizar tus propias partidas. Riot la revisa y puede tardar unos días; mientras tanto usa la de 24 horas.

## Paso 4. Configurar tu cuenta en el panel

La primera vez, el panel te muestra la página **Configuración** con el mensaje «Bienvenido, configura tu cuenta para empezar».

1. **Tu Riot ID:** escribe tu nombre del juego con el `#` y lo que va después. Por ejemplo, `Jugador#LAN`.
2. **Servidor:** elige donde juegas. Si juegas en Latinoamérica Norte, elige **LAN**.
3. **Key de Riot:** pega la que copiaste en el paso anterior.
4. Pulsa **«Probar key de Riot»**. Debe decir que tu key funciona. Si no, revisa que la copiaste completa.
5. **Key de Gemini:** déjala vacía por ahora. Servirá para el análisis de video, que llegará más adelante (si ya quieres tenerla, en la misma página hay instrucciones).
6. Pulsa **«Guardar»**.

## Paso 5. Descargar tus partidas

1. Pulsa el botón **«Siguiente: descargar mis partidas»** (o entra a **Sincronizar** en el menú de arriba).
2. Deja las opciones como están y pulsa **«Sincronizar»**.
3. Verás una barra de progreso. Descargar todas tus partidas tarda un rato (más o menos 40 minutos por cada 1000 partidas), porque Riot limita cuántas se pueden pedir. Puedes dejarlo trabajando. Si lo interrumpes, la próxima vez continúa donde se quedó.

## Cómo leer el panel

El menú de arriba tiene estas secciones:

- **Resumen:** tus números generales (partidas, porcentaje de victorias, KDA, farmeo) y cómo te va según el mes, el día de la semana y el campeón.
- **Partidas:** la lista de todas tus partidas. Pulsa una para ver su «boleta»: tus puntos fuertes y débiles comparados con tu rival de línea y con tu propio historial.
- **Clan:** una tabla para comparar a todos los jugadores registrados y cuánto ganan cuando juegan juntos.
- **Jugadores:** aquí agregas o quitas a los miembros de tu clan.
- **En vivo:** tu partida mientras juegas (ver «Ver tu partida en vivo y compartirla en Discord»).
- **Sincronizar:** descarga partidas nuevas.
- **Configuración:** tu Riot ID, servidor y keys.

Arriba a la derecha puedes elegir la cola: **Solo/Dúo**, **Flex** o **Todas**.

## Ver tu partida en vivo y compartirla en Discord

1. Deja el panel abierto (la ventana negra de `abrir-panel.bat`) y entra a **En vivo** en el menú.
2. Empieza una partida (también sirve la Herramienta de práctica). La página dice «Esperando partida…» y **se llena sola** cuando termina la pantalla de carga. Junto a «En vivo» en el menú aparece un punto rojo mientras juegas.
3. Verás el marcador, a los 10 jugadores (como en TAB), el oro estimado, tus datos y cómo vas frente a tu propio historial. Esto no necesita ninguna key y no se envía nada a internet: el panel solo lee lo que el juego muestra en tu mismo PC.
4. **Para mostrarlo en Discord:** pulsa **«Modo stream (para compartir en Discord)»**, comparte **esa ventana del navegador** en Discord (o ábrela en un segundo monitor y compártelo) y ponla en pantalla completa con **F11**.

**Quién está en tu partida («Rivales y aliados»).** En la pantalla de carga, esa misma página muestra a los 10 jugadores con su rango, cuánto dominan el campeón que van a jugar y si ya jugaron contigo o contra ti. Esta parte **sí usa tu key** (hasta 45 peticiones por partida; si vuelves a abrirla o buscas la misma sala, sale de la memoria de 6 horas) y se hace sola mientras la página está abierta, o con el botón **«Buscar partida ahora»**. No funciona a la vez que una sincronización (espera a que termine). Si un jugador tiene el perfil oculto, solo verás su campeón y sus hechizos.

El oro de los demás jugadores es una **estimación**: Riot solo da el oro real de quien está jugando. Cuando termina la partida se queda el resumen; en unos minutos puedes sincronizarla en **Sincronizar** para ver el análisis completo. Esta página no muestra tiempos de habilidades ni de objetivos del rival ni te da órdenes, porque las reglas de Riot no lo permiten.

## Agregar a tu clan

1. Entra a **Jugadores**.
2. En «Agregar un jugador» escribe el Riot ID de tu compañero (`Nombre#TAG`) y pulsa **«Agregar»**.
3. Entra a **Sincronizar**, elige a ese jugador (o **«Todo el clan»**) y pulsa **«Sincronizar»**.

Solo necesitas **tu propia key**: sirve para ver a todo el clan. **No le pidas la key a nadie** ni compartas la tuya. Una partida que jugaron varios del clan se descarga una sola vez.

## Renovar la key cada día

Si usas la key de 24 horas, el panel te avisa con un recuadro amarillo: **«Tu key de Riot probablemente expiró»**. Cuando lo veas:

1. Entra a <https://developer.riotgames.com> y pulsa **«Regenerate API Key»**.
2. Copia la key nueva.
3. En el panel entra a **Configuración**, pégala en «Key de Riot» y pulsa **«Guardar»**.

No hace falta cerrar nada ni reiniciar. Todo lo ya descargado se conserva.

## Usar el programa otros días

Haz doble clic en el acceso directo **«LoL Analyzer»** del escritorio. Se abre una ventana negra y, en unos segundos, el panel en tu navegador. **No cierres la ventana negra mientras uses el panel.** Cuando termines, ciérrala para apagar el programa.

## Actualizar

Cuando haya una versión nueva:

1. **Cierra el panel** (cierra su ventana negra).
2. Haz doble clic en **`actualizar.bat`**, en la carpeta del programa.
3. Espera a que diga «LoL Analyzer está actualizado».

Tu configuración y tus partidas descargadas no se tocan.

## Desinstalar

1. Cierra el panel.
2. Borra la carpeta del programa (por ejemplo, `Documentos\lol-analyzer-main`). Ahí está todo, incluidas tus partidas y tu key.
3. Borra el acceso directo «LoL Analyzer» del escritorio.

El programa no instala nada más en Windows, salvo Python y ffmpeg si los instaló por ti; si quieres, los puedes quitar desde «Aplicaciones instaladas».

## Privacidad

- **Todo se queda en tu computador.** Tus partidas y tus keys se guardan en la carpeta del programa. El panel solo se puede ver desde tu propio equipo, no desde internet ni desde tu red.
- **Tus keys son personales.** No las compartas, no las pegues en chats y no publiques capturas de pantalla donde se vean. Si crees que alguien vio tu key, genera otra en developer.riotgames.com.
- Para ver a todo el clan solo necesitas tu propia key.

## Problemas comunes

**«Tu key de Riot probablemente expiró» o «la key es inválida o expiró»**
Las keys normales duran 24 horas. Consigue una nueva (ver «Renovar la key cada día»).

**La prueba de la key dice «Sin conexión» o el panel «no se puede conectar»**
Revisa tu internet. Si la tienes, prueba apagar un momento el antivirus o la VPN: algunos bloquean estos programas pequeños. Vuelve a intentarlo después.

**«Esperando partida…» no cambia aunque ya estoy jugando**
Espera a que termine la pantalla de carga: el juego solo responde cuando la partida ya empezó. Si el panel dice que el certificado del juego no coincide, tu antivirus puede estar cambiando los certificados locales: añade una excepción para el juego o el panel, o avísale a quien te ayuda con el programa.

**El panel no se abre en el navegador**
Mira que la ventana negra siga abierta. Luego abre el navegador y escribe esto en la barra de direcciones: `http://127.0.0.1:8765`

**Dice que el puerto está ocupado**
Casi siempre es porque el panel ya está abierto en otra ventana negra. Ciérrala, o usa esa misma. Si no encuentras ninguna ventana, reinicia el computador y vuelve a abrirlo.

**Cerré la ventana negra sin querer**
No pasa nada: el panel se apagó. Haz doble clic en el acceso directo «LoL Analyzer» para volver a abrirlo. Lo ya descargado sigue ahí.

**La descarga de partidas se interrumpió**
Vuelve a pulsar «Sincronizar». Continúa donde se quedó.

**Windows no me deja ejecutar `instalar.bat`**
Mira el paso 2 (pantalla «Windows protegió su PC»). Si hiciste doble clic dentro del zip, extráelo primero (paso 1).

**El instalador dice que no tengo `winget` o no puede instalar Python**
Instálalo a mano: entra a <https://www.python.org/downloads/>, descarga Python 3.13, ábrelo y **marca la casilla «Add python.exe to PATH»** antes de pulsar «Install Now». Después vuelve a hacer doble clic en `instalar.bat`.

Si nada de esto te ayuda, avísale a quien te pasó el programa y copia el mensaje que sale en la ventana (sin incluir tu key).

---

*lol-analyzer isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.*
