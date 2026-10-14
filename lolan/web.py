"""Local web dashboard: python -m lolan web."""
from __future__ import annotations

import re
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import NotFound

from . import analyze as an
from .__main__ import QUEUE_ALIASES
from .config import ConfigError, load_config, project_root
from .players import load_roster, normalize_riot_id
from .riot import (InvalidApiKeyError, MissingApiKeyError, NotFoundError, RateLimiter,
                   RiotClient, RiotConnectionError, RiotError)
from .settings import (PLATFORM_CHOICES, KeyHealth, age_text, key_warning, mask_key, platform_region,
                       read_settings, update_env_file, valid_key)
from .store import Store
from .sync import sync_players

# cola slug -> queue id (None = every analyzable queue); the label is what the UI shows.
QUEUES = {"todas": None, "solo": QUEUE_ALIASES["solo"], "flex": QUEUE_ALIASES["flex"]}
QUEUE_LABELS = {"todas": "Todas", "solo": "Solo/Dúo", "flex": "Flex"}
DEFAULT_COLA = "solo"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
PAGE_SIZE = 50
MAX_LOG_LINES = 200

PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]")
TOTAL_RE = re.compile(r"Por descargar: (\d+)")
HEADER_RE = re.compile(r"^=== (.+) \((\d+)/(\d+)\) ===$")   # printed by sync_players per player
MISSING_KEY_MESSAGE = ("Falta tu key de Riot. Pégala en la página Configuración y vuelve a "
                       "intentarlo; no hace falta reiniciar el panel.")
INVALID_KEY_MESSAGE = ("Tu key de Riot es inválida o expiró (las keys de desarrollo duran 24 horas). "
                       "Consigue una nueva en developer.riotgames.com, pégala en la página "
                       "Configuración y vuelve a {again}; no hace falta reiniciar el panel.")
# Pages that stay reachable before the first setup (API calls are never redirected).
SETUP_ENDPOINTS = ("static", "config_page", "api_config_save", "api_config_test")


# ------------------------------------------------------------------- cache
@dataclass
class Data:
    games: list
    total: int          # matches stored for this player, analyzable or not


class GamesCache:
    """Each player's analyzable games, loaded once and reloaded when that player's stored match
    count changes (or on invalidate).

    Per-queue lists are built from shallow copies so session info is computed on the filtered
    list, matching `load_games(queue=...)` in the CLI.
    """

    def __init__(self, db_path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._entries: dict = {}     # puuid -> {"key": count, "all": [...], "by_queue": {...}}

    def invalidate(self) -> None:
        with self._lock:
            self._entries.clear()

    def forget(self, puuid: str) -> None:
        with self._lock:
            self._entries.pop(puuid, None)

    def get(self, puuid: str | None, queue: int | None = None) -> Data:
        if puuid is None:
            return Data([], 0)
        # sqlite connections are per-thread, so every call opens its own short-lived Store.
        store = Store(self._db_path)
        try:
            count = store.count_matches(puuid)
            with self._lock:
                entry = self._entries.get(puuid)
                if entry is None or entry["key"] != count:
                    games = an.load_games(store, puuid) if count else []
                    entry = {"key": count, "all": games, "by_queue": {}}
                    self._entries[puuid] = entry
                if queue is None:
                    games = entry["all"]
                else:
                    games = entry["by_queue"].get(queue)
                    if games is None:
                        games = [dict(x) for x in entry["all"] if x["queue_id"] == queue]
                        an.add_session_info(games)
                        entry["by_queue"][queue] = games
                return Data(games, count)
        finally:
            store.close()


# -------------------------------------------------------------------- sync
class SyncJob:
    """One background sync at a time; state is read by GET /api/sync."""

    def __init__(self, root, cache: GamesCache, health: KeyHealth | None = None):
        self._root = root
        self._cache = cache
        self.health = health or KeyHealth()
        self._lock = threading.Lock()
        # Shared by every sync this server runs: the key's 100 req / 2 min budget spans runs
        # (and the Riot ID lookups done when adding players).
        self.limiter = RateLimiter()
        self.thread: threading.Thread | None = None
        self._reset()

    def _reset(self) -> None:
        self.running = False
        self.lines: deque = deque(maxlen=MAX_LOG_LINES)
        self.progress = None
        self.current = None      # {"riot_id", "index", "count"} of the player being synced
        self.result = None
        self.error = None

    def start(self, queue, max_matches, with_timeline, target="yo") -> bool:
        """`target`: "yo" (the RIOT_ID in .env), "clan" (everyone registered) or a Riot ID."""
        with self._lock:
            if self.running:
                return False
            self._reset()
            self.running = True
            self.thread = threading.Thread(
                target=self._run, args=(queue, max_matches, with_timeline, target),
                daemon=True, name="lolan-sync")
            self.thread.start()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {"running": self.running, "lines": list(self.lines),
                    "progress": dict(self.progress) if self.progress else None,
                    "current": dict(self.current) if self.current else None,
                    "result": self.result, "error": self.error}

    def join(self, timeout=None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)

    def _out(self, line: str) -> None:
        with self._lock:
            self.lines.append(str(line))
            m = HEADER_RE.match(line)
            if m:
                self.current = {"riot_id": m.group(1), "index": int(m.group(2)),
                                "count": int(m.group(3))}
                self.progress = None        # the previous player's bar is over
                return
            m = PROGRESS_RE.match(line)
            if m:
                self.progress = {"done": int(m.group(1)), "total": int(m.group(2))}
                return
            m = TOTAL_RE.search(line)
            if m and int(m.group(1)) > 0:
                self.progress = {"done": 0, "total": int(m.group(1))}

    def _run(self, queue, max_matches, with_timeline, target) -> None:
        result = error = None
        key_used = ""
        try:
            # override=True: the dev API key is regenerated every 24 h and .env gets edited
            # while the server runs.
            cfg = load_config(self._root, override=True)
            key_used = cfg.api_key
            if not cfg.api_key:
                raise MissingApiKeyError(MISSING_KEY_MESSAGE)
            client = RiotClient(cfg.api_key, region=cfg.region, limiter=self.limiter)
            store = Store(cfg.db_path)
            try:
                if target == "clan":
                    # You first (even if not registered yet), then everyone else registered.
                    riot_ids = [cfg.riot_id] if cfg.riot_id else []
                    seen = {r.casefold() for r in riot_ids}
                    riot_ids += [p.riot_id for p in load_roster(store, cfg.riot_id).players
                                 if p.riot_id.casefold() not in seen]
                    if not riot_ids:
                        raise ConfigError("No hay jugadores que sincronizar: escribe tu Riot ID en "
                                          "Configuración o agrega jugadores en la página Jugadores.")
                elif target == "yo":
                    if not cfg.riot_id:
                        raise ConfigError("Falta tu Riot ID (formato Nombre#TAG): escríbelo en la "
                                          "página Configuración.")
                    riot_ids = [cfg.riot_id]
                else:
                    riot_ids = [target]
                outcomes = sync_players(client, store, riot_ids, main_riot_id=cfg.riot_id,
                                        max_matches=max_matches, with_timeline=with_timeline,
                                        queue=queue, out=self._out)
            finally:
                store.close()
            result = _sync_result(outcomes)
            self.health.mark_ok(cfg.api_key)
        except MissingApiKeyError:
            error = MISSING_KEY_MESSAGE
        except InvalidApiKeyError:
            self.health.mark_invalid(key_used)
            error = INVALID_KEY_MESSAGE.format(again="pulsar Sincronizar")
        except (ConfigError, RiotError, ValueError) as exc:
            error = str(exc)
        except Exception as exc:  # keep the thread's failure visible in the UI
            traceback.print_exc()
            error = f"Error inesperado: {type(exc).__name__}: {exc}"
        finally:
            self._cache.invalidate()
            with self._lock:
                self.result, self.error, self.running = result, error, False


def _sync_result(outcomes) -> dict:
    """Totals over the players that synced, plus one entry per player."""
    ok = [o.result for o in outcomes if o.result]
    return {
        "total_ids": sum(r.total_ids for r in ok),
        "already_stored": sum(r.already_stored for r in ok),
        "downloaded": sum(r.downloaded for r in ok),
        "missing_timelines": sum(r.missing_timelines for r in ok),
        "failed": sum(r.failed for r in ok),
        "jugadores": [
            {"riot_id": o.riot_id, "error": o.error,
             **({k: v for k, v in asdict(o.result).items() if k != "puuid"} if o.result else {})}
            for o in outcomes],
    }


# --------------------------------------------------------------------- app
def _bar_rows(rows, short=None, title=None):
    return [{"label": short(r) if short else r["label"],
             "title": f"{title(r) if title else r['label']}: {an.fmt_pct(r['winrate'])} "
                      f"en {r['games']} partidas",
             "winrate": r["winrate"], "games": r["games"]} for r in rows]


def create_app(root=None) -> Flask:
    root = Path(root) if root else project_root()
    app = Flask(__name__)
    # Same location as Config.db_path.
    db_path = root / "data" / "lol.db"
    cache = GamesCache(db_path)
    health = KeyHealth()
    job = SyncJob(root, cache, health)
    app.extensions["lolan"] = {"cache": cache, "sync": job, "root": root, "health": health}

    app.jinja_env.filters.update(
        val=lambda v, key: an.fmt_value(key, v), pct=an.fmt_pct, minutes=an.fmt_minutes,
        queue=an.queue_name, role=an.role_name,
        num=lambda v, digits=2: "-" if v is None else f"{v:.{digits}f}",
        when=lambda v: (v or "-").replace("T", " "),
    )

    def main_riot_id() -> str:
        # Re-read on every request: .env may be edited while the server runs.
        try:
            return load_config(root, override=True).riot_id
        except ConfigError:
            return ""

    def open_roster():
        store = Store(db_path)
        try:
            return load_roster(store, main_riot_id())
        finally:
            store.close()

    @app.before_request
    def _guard():
        # Reject foreign Host headers (DNS rebinding) even though we only bind to 127.0.0.1.
        host = request.host
        host = host[:host.index("]") + 1] if host.startswith("[") else host.rsplit(":", 1)[0]
        if host.lower() not in LOCAL_HOSTS:
            return "Host no permitido.", 400
        if request.endpoint == "static":
            return None
        # .env is re-read on every request (it changes when the user saves Configuración).
        g.settings = read_settings(root)
        if (not g.settings.ready and request.endpoint not in SETUP_ENDPOINTS
                and not request.path.startswith("/api/")):
            return redirect(url_for("config_page"))
        cola = (request.args.get("cola") or DEFAULT_COLA).lower()
        g.cola = cola if cola in QUEUES else DEFAULT_COLA
        g.roster = open_roster()
        # ?jugador=<slug>; missing or unknown -> the main player (None if nobody is registered).
        g.player = g.roster.by_slug(request.args.get("jugador")) or g.roster.main
        return None

    @app.context_processor
    def _context():
        roster = g.get("roster")
        main_slug = roster.main_slug if roster else None
        current = g.get("player")
        # The main player is the default, so its links stay without ?jugador=.
        param = roster.slug(current) if (roster and current) else None
        if param == main_slug:
            param = None

        def u(endpoint, **values):
            values.setdefault("jugador", param)
            if values["jugador"] == main_slug:
                values["jugador"] = None
            return url_for(endpoint, cola=g.cola, **values)

        def switch(cola):
            if request.endpoint in (None, "static"):
                return url_for("index", cola=cola)
            return url_for(request.endpoint, cola=cola, jugador=param, **(request.view_args or {}))

        # The player picker is a GET form to the current page (a match page has no equivalent
        # for another player, so it goes to the games list).
        picker_action = (url_for("games_list") if request.endpoint == "match" else request.path)
        st = g.get("settings")
        banner = key_warning(st, health) if st else None
        return {"cola": g.get("cola", DEFAULT_COLA), "queue_labels": QUEUE_LABELS, "u": u,
                "key_banner": banner,
                "key_age": age_text(st.riot_key_saved_at) if banner == "vieja" else "",
                "switch": switch, "endpoint": request.endpoint, "roster": roster,
                "jugador": current, "picker_action": picker_action,
                "show_picker": bool(roster) and len(roster) > 1
                and request.endpoint not in ("clan_page", "players_page"),
                "min_champion_games": an.MIN_CHAMPION_GAMES, "session_gap": an.SESSION_GAP_MIN}

    def data() -> Data:
        return cache.get(g.player.puuid if g.player else None, QUEUES[g.cola])

    def empty_page(d: Data):
        """Friendly page when there is nothing to show; None when there is data."""
        if d.games:
            return None
        if d.total == 0:
            reason = "db"
        elif g.cola != "todas":
            reason = "cola"
        else:
            reason = "analizables"
        return render_template("vacio.html", reason=reason)

    # ------------------------------------------------------------ pages
    @app.get("/")
    def index():
        d = data()
        if (page := empty_page(d)) is not None:
            return page
        games = d.games
        s = an.summarize(games)
        sess = an.trend_by_session_game(games)
        insights = []
        for row in s["by_champion"]:
            if row["games"] < an.MIN_CHAMPION_GAMES:
                continue
            ins = an.champion_insights(games, row["key"])
            if ins["enough"]:
                insights.append({"row": row, "ins": ins})
        return render_template(
            "resumen.html", s=s, o=s["overall"], first=games[0]["date"], last=games[-1]["date"],
            months=_bar_rows(an.trend_by_month(games), title=lambda r: r["label"]),
            weekdays=_bar_rows(an.trend_by_weekday(games), short=lambda r: r["label"][:3]),
            positions=_bar_rows(sess["by_position"],
                                short=lambda r: "5+" if r["key"] >= 5 else str(r["key"]),
                                title=lambda r: r["label"]),
            sess=sess, insights=insights)

    @app.get("/partidas")
    def games_list():
        d = data()
        if (page := empty_page(d)) is not None:
            return page
        newest_first = d.games[::-1]
        pages = max(1, -(-len(newest_first) // PAGE_SIZE))
        try:
            n = int(request.args.get("pagina", 1))
        except ValueError:
            n = 1
        n = min(max(n, 1), pages)
        chunk = newest_first[(n - 1) * PAGE_SIZE:n * PAGE_SIZE]
        return render_template("partidas.html", games=chunk, page=n, pages=pages,
                               total=len(newest_first), start=(n - 1) * PAGE_SIZE + 1,
                               end=(n - 1) * PAGE_SIZE + len(chunk))

    @app.get("/campeon/<nombre>")
    def champion(nombre):
        d = data()
        if d.total == 0:
            return empty_page(d)
        games = an.games_for_champion(d.games, nombre)
        if not games:
            everywhere = an.games_for_champion(
                cache.get(g.player.puuid, None).games, nombre)
            if not everywhere:
                abort(404, description=f"No hay partidas con el campeón «{nombre}».")
            return render_template("campeon.html", name=everywhere[0]["champion"], ins=None,
                                   games=[])
        ins = an.champion_insights(d.games, nombre)
        return render_template("campeon.html", name=ins["champion"], ins=ins, games=games[::-1])

    @app.get("/partida/<match_id>")
    def match(match_id):
        d = data()
        if d.total == 0:
            return empty_page(d)
        wanted = match_id.lower()

        def find(pool):
            return next((x for x in pool if str(x["match_id"]).lower() == wanted), None)

        pool, other_queue = d.games, False
        game = find(pool)
        if game is None:
            pool = cache.get(g.player.puuid, None).games
            game, other_queue = find(pool), True
        if game is None:
            owner = "tus partidas" if g.player.is_main else f"las partidas de {g.player.riot_id}"
            abort(404, description=f"No se encontró la partida {match_id} entre {owner} "
                                   "analizables (puede ser un remake o de una cola que no es de la Grieta).")
        card = an.game_report_card(game, pool)
        return render_template("partida.html", card=card, pool_size=len(pool),
                               pool_label="todas las colas" if other_queue or g.cola == "todas"
                               else QUEUE_LABELS[g.cola], other_queue=other_queue)

    @app.get("/clan")
    def clan_page():
        queue = QUEUES[g.cola]
        rows, games_by = [], {}
        for p in g.roster.players:
            d = cache.get(p.puuid, queue)
            games_by[p.puuid] = d.games
            rows.append({"player": p, "slug": g.roster.slug(p), "ov": an.player_overview(d.games),
                         "stored": d.total})
        pairs = []
        for pr in an.pair_stats(games_by):
            pairs.append({**pr, "a": g.roster.by_puuid(pr["a"]), "b": g.roster.by_puuid(pr["b"])})
        return render_template("clan.html", rows=rows, pairs=pairs,
                               min_pair_games=an.MIN_PAIR_GAMES)

    @app.get("/jugadores")
    def players_page():
        store = Store(db_path)
        try:
            counts = store.match_counts()
        finally:
            store.close()
        rows = [{"player": p, "slug": g.roster.slug(p), "stored": counts.get(p.puuid, 0)}
                for p in g.roster.players]
        return render_template("jugadores.html", rows=rows)

    @app.get("/sincronizar")
    def sync_page():
        return render_template("sincronizar.html", riot_id=main_riot_id())

    @app.get("/configuracion")
    def config_page():
        st = g.settings
        choices = list(PLATFORM_CHOICES)
        if st.platform not in dict(choices):         # a server typed by hand in .env
            choices.append((st.platform, st.platform))
        return render_template("configuracion.html", st=st, platforms=choices,
                               riot_fin=mask_key(st.riot_key), gemini_fin=mask_key(st.gemini_key),
                               saved_age=age_text(st.riot_key_saved_at), welcome=not st.ready)

    # -------------------------------------------------------------- API
    def api_guard():
        """An error response unless the X-Lolan header is present; None when the call may go on."""
        # A custom header + JSON body can't be sent by a cross-site HTML form.
        if request.headers.get("X-Lolan") != "1":
            return jsonify(error="Falta el encabezado X-Lolan."), 403
        return None

    @app.get("/api/sync")
    def api_sync_status():
        return jsonify(job.snapshot())

    @app.post("/api/sync")
    def api_sync_start():
        if (denied := api_guard()) is not None:
            return denied
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="El cuerpo debe ser JSON."), 400
        cola = str(body.get("cola", "solo")).lower()
        if cola not in QUEUES:
            return jsonify(error="La cola debe ser solo, flex o todas."), 400
        max_matches = body.get("max")
        if max_matches in ("", None):
            max_matches = None
        elif isinstance(max_matches, bool) or not isinstance(max_matches, (int, str)):
            return jsonify(error="«Máximo de partidas» debe ser un número entero mayor que 0."), 400
        else:
            try:
                max_matches = int(max_matches)
            except ValueError:
                max_matches = 0
            if max_matches < 1:
                return jsonify(error="«Máximo de partidas» debe ser un número entero mayor que 0."), 400
        with_timeline = bool(body.get("timeline", True))
        # "yo" (default) = the RIOT_ID in .env, "clan" = everyone registered, else one player.
        who = str(body.get("jugador") or "yo").strip()
        if who.lower() in ("yo", "clan"):
            target = who.lower()
        else:
            player = open_roster().by_ref(who)
            if player is None:
                return jsonify(error="Ese jugador no está registrado. Agrégalo en la página "
                                     "Jugadores."), 400
            target = player.riot_id
        if not job.start(QUEUES[cola], max_matches, with_timeline, target):
            return jsonify(error="Ya hay una sincronización en curso."), 409
        return jsonify(job.snapshot()), 202

    def _key_field(body, name):
        """(key, error) for an optional key field: blank means "keep the saved one"."""
        value = body.get(name)
        key = "" if value is None else str(value).strip()
        if key and not valid_key(key):
            return "", ("La key tiene caracteres que no son válidos (espacios, acentos o símbolos). "
                        "Cópiala de nuevo, completa y sin espacios.")
        return key, None

    @app.post("/api/configuracion")
    def api_config_save():
        if (denied := api_guard()) is not None:
            return denied
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="El cuerpo debe ser JSON."), 400
        st = read_settings(root)
        try:
            name, tag = normalize_riot_id(body.get("riot_id"))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        platform = str(body.get("plataforma") or st.platform).strip().lower()
        region = platform_region(platform)
        if region is None:
            return jsonify(error="Elige un servidor de la lista."), 400
        riot_key, error = _key_field(body, "riot_api_key")
        gemini_key, error2 = _key_field(body, "gemini_api_key")
        if error or error2:
            return jsonify(error=error or error2), 400

        want = {"RIOT_ID": f"{name}#{tag}", "PLATFORM": platform, "REGION": region}
        if riot_key and riot_key != st.riot_key:
            want["RIOT_API_KEY"] = riot_key
            want["RIOT_API_KEY_SAVED_AT"] = str(int(time.time()))
        if gemini_key and gemini_key != st.gemini_key:
            want["GEMINI_API_KEY"] = gemini_key
        if "key_permanente" in body:
            want["RIOT_API_KEY_PERMANENT"] = "1" if body["key_permanente"] is True else "0"
        updates = {k: v for k, v in want.items() if st.raw.get(k, "") != v}
        try:
            if updates:
                update_env_file(root / ".env", updates)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except OSError:
            return jsonify(error="No se pudo guardar el archivo .env. Revisa que la carpeta del "
                                 "programa no sea de solo lectura y que ningún otro programa lo "
                                 "tenga abierto."), 500
        new = read_settings(root)
        return jsonify(ok=True, listo=new.ready, riot_id=new.riot_id,
                       riot_key_fin=mask_key(new.riot_key), gemini_key_fin=mask_key(new.gemini_key),
                       mensaje="Listo, se guardó tu configuración.")

    @app.post("/api/configuracion/probar")
    def api_config_test():
        """One cheap request to Riot with the typed key (or the saved one); saves nothing."""
        if (denied := api_guard()) is not None:
            return denied
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="El cuerpo debe ser JSON."), 400
        st = read_settings(root)
        key, error = _key_field(body, "riot_api_key")
        if error:
            return jsonify(error=error), 400
        key = key or st.riot_key
        if not key:
            return jsonify(error="Primero pega tu key de Riot en el cuadro de arriba."), 400
        platform = str(body.get("plataforma") or st.platform).strip().lower()
        if platform_region(platform) is None:
            return jsonify(error="Elige un servidor de la lista."), 400
        try:
            RiotClient(key, region=platform, limiter=job.limiter, max_retries=1,
                       timeout=10).get_platform_status()
        except InvalidApiKeyError:
            health.mark_invalid(key)
            return jsonify(ok=False, estado="invalida",
                           mensaje="Riot no aceptó esa key: es inválida o ya expiró. Consigue una "
                                   "nueva en developer.riotgames.com y pégala aquí.")
        except RiotConnectionError:
            return jsonify(ok=False, estado="sin_conexion",
                           mensaje="Sin conexión: no se pudo llegar a Riot. Revisa tu internet "
                                   "(o tu antivirus) e inténtalo otra vez.")
        except RiotError as exc:
            return jsonify(ok=False, estado="error", mensaje=f"Riot respondió con un problema: {exc}")
        health.mark_ok(key)
        return jsonify(ok=True, estado="ok", mensaje="¡Tu key funciona! Riot la aceptó.")

    @app.post("/api/jugadores")
    def api_players_add():
        if (denied := api_guard()) is not None:
            return denied
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="El cuerpo debe ser JSON."), 400
        try:
            name, tag = normalize_riot_id(body.get("riot_id"))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        riot_id = f"{name}#{tag}"
        key_used = ""
        try:
            cfg = load_config(root, override=True)
            key_used = cfg.api_key
            if not cfg.api_key:
                raise MissingApiKeyError(MISSING_KEY_MESSAGE)
            puuid = RiotClient(cfg.api_key, region=cfg.region, limiter=job.limiter).get_puuid(name, tag)
        except MissingApiKeyError:
            return jsonify(error=MISSING_KEY_MESSAGE), 401
        except InvalidApiKeyError:
            health.mark_invalid(key_used)
            return jsonify(error=INVALID_KEY_MESSAGE.format(again="intentarlo")), 401
        except NotFoundError:
            return jsonify(error=f"No se encontró la cuenta {riot_id}. Revisa el nombre y el TAG."), 404
        except ConfigError as exc:
            return jsonify(error=str(exc)), 400
        except RiotError as exc:
            return jsonify(error=str(exc)), 502
        store = Store(db_path)
        try:
            known = store.get_player(puuid=puuid)
            existed = known is not None
            if known and known.riot_id.casefold() == riot_id.casefold():
                riot_id = known.riot_id        # same name typed in another case: keep how it was saved
            is_main = bool(cfg.riot_id) and riot_id.casefold() == cfg.riot_id.strip().casefold()
            store.add_player(puuid, riot_id, is_main=is_main)
            stored = store.count_matches(puuid)
        finally:
            store.close()
        cache.forget(puuid)
        message = (f"{riot_id} ya estaba registrado." if existed else f"Se agregó a {riot_id}.")
        return jsonify(riot_id=riot_id, partidas=stored, ya_registrado=existed,
                       mensaje=message), (200 if existed else 201)

    @app.delete("/api/jugadores/<puuid>")
    def api_players_remove(puuid):
        if (denied := api_guard()) is not None:
            return denied
        roster = open_roster()
        player = roster.by_puuid(puuid)
        if player is None:
            return jsonify(error="Ese jugador no está registrado."), 404
        if player.is_main:
            return jsonify(error="No se puede quitar al jugador principal (el Riot ID de Configuración)."), 400
        store = Store(db_path)
        try:
            store.remove_player(puuid)
        finally:
            store.close()
        cache.forget(puuid)
        return jsonify(riot_id=player.riot_id,
                       mensaje=f"Se quitó a {player.riot_id}. Sus partidas siguen guardadas."), 200

    # ----------------------------------------------------------- errors
    @app.errorhandler(404)
    def not_found(exc):
        message = exc.description if exc.description != NotFound.description else (
            "Esa página no existe.")
        return render_template("404.html", message=message), 404

    return app
