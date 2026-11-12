"""Pruebas de Wyss Sync. Correr: QT_QPA_PLATFORM=offscreen python3 -m unittest -v test_wyss_sync"""
import json
import os
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from wyss_sync import core, ui  # noqa: E402

ME, OTHER = "AAAAAAA-111", "BBBBBBB-222"


class FakeApi:
    """Responde como la API de Syncthing; `routes` mapea path -> valor o función(params)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, path, **params):
        self.calls.append((path, params))
        v = self.routes[path]
        if isinstance(v, Exception):
            raise v
        return v(params) if callable(v) else v


def routes(connected=True, folder_paused=False, status=None, completion=None):
    status = status or {"state": "idle", "globalBytes": 1000, "needBytes": 0, "needTotalItems": 0}
    return {
        "/rest/system/status": {"myID": ME},
        "/rest/config/devices": [{"deviceID": ME, "name": "pc-a"}, {"deviceID": OTHER, "name": "pc-b"}],
        "/rest/config/folders": [
            {"id": "home", "label": "Home", "path": "/home/u", "paused": folder_paused,
             "devices": [{"deviceID": ME}, {"deviceID": OTHER}]},
            {"id": "solo-local", "label": "Local", "path": "/x", "devices": [{"deviceID": ME}]},
        ],
        "/rest/system/connections": {"connections": {OTHER: {
            "connected": connected, "paused": False,
            "address": "tcp://192.0.2.10:22000" if connected else ""}}},
        "/rest/db/status": status,
        "/rest/db/completion": completion or {"completion": 100},
    }


LOCAL = {"host": "pc-a", "os": "CachyOS Linux", "chassis": 3, "folders": {"home": "/home/u"}}
REMOTE = {"host": "pc-b", "os": "CachyOS Linux", "chassis": 10, "folders": {"home": "/home/otro"}}


class FolderStateTests(unittest.TestCase):
    def st(self, **kw):
        base = dict(paused=False, status={"state": "idle"}, completion=None,
                    remote_connected=True, remote_paused=False)
        base.update(kw)
        return core.folder_state(**base)

    def test_synced(self):
        self.assertEqual(self.st(completion={"completion": 100}), ("synced", None))

    def test_syncing_con_porcentaje_del_remoto(self):
        self.assertEqual(self.st(completion={"completion": 42.0}), ("syncing", 42.0))

    def test_nunca_muestra_100_mientras_sincroniza(self):
        s, pct = self.st(status={"state": "syncing"}, completion={"completion": 100})
        self.assertEqual(s, "syncing")
        self.assertLess(pct, 100)

    def test_offline_gana_a_synced(self):
        self.assertEqual(self.st(remote_connected=False)[0], "offline")

    def test_pausada_y_error(self):
        self.assertEqual(self.st(paused=True)[0], "paused")
        self.assertEqual(self.st(status={"state": "error"})[0], "error")
        self.assertEqual(self.st(status={"state": "idle", "pullErrors": 2})[0], "error")

    def test_scanning(self):
        self.assertEqual(self.st(status={"state": "scanning"})[0], "scanning")

    def test_pendientes_locales_sin_remoto_medido(self):
        s = {"state": "idle", "needTotalItems": 3, "globalBytes": 100, "needBytes": 50}
        self.assertEqual(self.st(status=s)[0], "syncing")


class CollectorTests(unittest.TestCase):
    def make(self, r, remote=REMOTE, **kw):
        probes = []

        def remote_probe(host):
            probes.append(host)
            return remote

        c = core.Collector(FakeApi(r), local_probe=lambda: LOCAL, remote_probe=remote_probe, **kw)
        return c, probes

    def test_modelo_completo(self):
        c, probes = self.make(routes())
        m = c.collect()
        self.assertEqual(probes, ["pc-b"])  # primero el alias de ssh, que es el nombre del dispositivo
        self.assertEqual([f["id"] for f in m["folders"]], ["home"])  # la carpeta no compartida no aparece
        f = m["folders"][0]
        self.assertEqual((f["local_path"], f["remote_path"], f["state"]), ("/home/u", "/home/otro", "synced"))
        self.assertEqual((m["local"]["kind"], m["remote"]["kind"]), ("desktop", "laptop"))
        self.assertTrue(m["remote"]["online"])

    def test_si_el_alias_no_responde_prueba_la_ip_de_la_conexion(self):
        probes = []

        def remote_probe(host):
            probes.append(host)
            return REMOTE if host == "192.0.2.10" else None

        c = core.Collector(FakeApi(routes()), local_probe=lambda: LOCAL, remote_probe=remote_probe)
        m = c.collect()
        self.assertEqual(probes, ["pc-b", "192.0.2.10"])
        self.assertTrue(m["remote"]["ssh"])

    def test_sonda_remota_se_cachea(self):
        now = [0.0]
        c, probes = self.make(routes(), clock=lambda: now[0], ttl=60)
        c.collect()
        now[0] = 30
        c.collect()
        self.assertEqual(len(probes), 1)
        now[0] = 61
        c.collect()
        self.assertEqual(len(probes), 2)

    def test_sin_ssh_no_inventa_la_ruta(self):
        c, _ = self.make(routes(), remote=None)
        m = c.collect()
        self.assertIsNone(m["folders"][0]["remote_path"])
        self.assertFalse(m["remote"]["ssh"])
        self.assertTrue(m["remote"]["online"])

    def test_otra_pc_apagada_conserva_la_ultima_sonda(self):
        c, _ = self.make(routes(connected=True))
        c.collect()
        c._api.routes = routes(connected=False)
        m = c.collect()
        self.assertEqual(m["folders"][0]["state"], "offline")
        self.assertEqual(m["folders"][0]["remote_path"], "/home/otro")
        self.assertFalse(m["remote"]["online"])

    def test_syncthing_caido_levanta_ApiError(self):
        r = routes()
        r["/rest/system/status"] = core.ApiError("x")
        c, _ = self.make(r)
        with self.assertRaises(core.ApiError):
            c.collect()


class ProbeTests(unittest.TestCase):
    def test_probe_remote_envia_probe_por_stdin_y_parsea(self):
        seen = {}

        class R:
            returncode, stdout = 0, json.dumps(REMOTE)

        def runner(cmd, **kw):
            seen["cmd"], seen["input"] = cmd, kw["input"]
            return R()

        self.assertEqual(core.probe_remote("192.0.2.10", runner=runner), REMOTE)
        self.assertEqual(seen["cmd"][-2:], ["python3", "-"])
        self.assertIn("BatchMode=yes", seen["cmd"])
        self.assertIn("def probe()", seen["input"])
        self.assertTrue(seen["input"].rstrip().endswith("print(json.dumps(probe()))"))   # solo ejecuta probe()
        self.assertNotIn("read_config()", seen["input"].rsplit("def probe_remote", 1)[1])  # nunca lee la clave

    def test_probe_remote_falla_en_silencio(self):
        def runner(*a, **k):
            raise OSError("sin ssh")
        self.assertIsNone(core.probe_remote("x", runner=runner))

    def test_probe_local_corre(self):
        p = core.probe()
        self.assertEqual(set(p), {"host", "os", "chassis", "folders", "disks"})

    def test_read_config_no_expone_la_clave_en_el_error(self):
        with self.assertRaises(core.ApiError) as cm:
            core.read_config(Path("/no/existe.xml"))
        self.assertNotIn("apikey", str(cm.exception).lower())


def lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = c
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def over(fg, alpha, bg):
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


class ContrastTests(unittest.TestCase):
    """Peor caso: escritorio blanco y escritorio negro detrás del vidrio."""

    def panel(self, backdrop):
        top = over(ui.PANEL_TOP[:3], ui.PANEL_TOP[3] / 255, backdrop)
        card = over((255, 255, 255), ui.CARD_FILL[3] / 255, top)
        return top, card

    def test_texto_sobre_vidrio_cumple_AA(self):
        for backdrop in ((0, 0, 0), (255, 255, 255)):
            for surface in self.panel(backdrop):
                for name, fg in (("TEXT", ui.TEXT), ("MUTED", ui.MUTED)):
                    self.assertGreaterEqual(contrast(hexrgb(fg), surface), 4.5, (name, backdrop, surface))

    def test_texto_de_cada_estado_cumple_AA(self):
        for backdrop in ((0, 0, 0), (255, 255, 255)):
            top, _ = self.panel(backdrop)
            for key, (_, color, text) in ui.STATES.items():
                pill = over(hexrgb(color), 46 / 255, top)
                self.assertGreaterEqual(contrast(hexrgb(text), pill), 4.5, (key, backdrop))


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def model(self, state="synced", pct=None, online=True, n=2):
        folders = [{"id": f"f{i}", "label": f"Carpeta {i}", "type": "sendreceive",
                    "local_path": f"/home/u/c{i}", "remote_path": f"/home/otro/c{i}",
                    "state": state, "percent": pct, "need_items": 0} for i in range(n)]
        return {"local": {"name": "pc-a", "os": "CachyOS Linux", "kind": "desktop", "online": True},
                "remote": {"name": "pc-b", "os": "CachyOS Linux", "kind": "laptop", "online": online,
                           "address": "192.0.2.10", "ssh": True},
                "folders": folders, "fetched_at": 0}

    def test_render_crea_una_fila_por_carpeta_y_actualiza_en_sitio(self):
        w = ui.Window(None)
        w.render(self.model(n=3))
        self.assertEqual(len(w._rows), 3)
        first = w._rows["f0"][1]
        w.render(self.model(state="syncing", pct=42, n=3))
        self.assertIs(w._rows["f0"][1], first)  # no se reconstruye si no cambian los ids
        self.assertEqual(first.label(), "Sincronizando 42 %")

    def test_rutas_en_ambos_lados(self):
        w = ui.Window(None)
        w.render(self.model(n=1))
        left, _, right = w._rows["f0"]
        self.assertTrue(left.path._full.endswith("/home/otro/c0"))
        self.assertTrue(right.path._full.startswith("/home/otro/c0"))

    def test_estados_sin_conexion_y_error_de_syncthing(self):
        w = ui.Window(None)
        w.render(self.model(state="offline", online=False))
        self.assertIn("sin conexión", w.conn.text())
        w.render(core.ApiError("/rest/system/status: URLError"))
        self.assertIn("Syncthing no responde", w.status.text())

    def test_el_porcentaje_nunca_muestra_100_si_falta_algo(self):
        pill = ui.LinkPill()
        pill.set_state("syncing", 99.96)
        self.assertEqual(pill.label(), "Sincronizando 99 %")
        pill.set_state("syncing", 42.7)
        self.assertEqual(pill.label(), "Sincronizando 42 %")

    def test_timer_de_giro_solo_corre_sincronizando(self):
        w = ui.Window(None)
        w.render(self.model(state="syncing", pct=10, n=1))
        pill = w._rows["f0"][1]
        self.assertTrue(pill._timer.isActive())
        w.render(self.model(state="synced", n=1))
        self.assertFalse(pill._timer.isActive())

    def test_la_pildora_no_recorta_ninguna_etiqueta(self):
        from PyQt6.QtGui import QFontMetrics
        pill = ui.LinkPill()
        pill.resize(ui.LinkPill().minimumWidth(), 56)  # ancho mínimo = peor caso
        fm = QFontMetrics(pill.pill_font())
        for key, (text, _, _) in ui.STATES.items():
            for label in (text, f"{text} 100 %"):
                self.assertGreaterEqual(pill.pill_width() - 46, fm.horizontalAdvance(label), (key, label))

    def test_sin_carpetas_muestra_vacio(self):
        w = ui.Window(None)
        w.render(self.model(n=0))
        self.assertIn("no tiene carpetas", w.empty.text())


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- fase 1: subcarpetas e ignorados
import base64  # noqa: E402
import tempfile  # noqa: E402

from wyss_sync import manage  # noqa: E402


class IgnoreApi:
    """Syncthing simulado: guarda las reglas y decide `ignored` con el matcher del panel,
    salvo `liar`, que simula una instalación que no refleja el cambio."""

    def __init__(self, lines, liar=False):
        self.lines, self.posts, self.liar = list(lines), [], liar

    def get(self, path, **p):
        if path == "/rest/db/ignores":
            return {"ignore": list(self.lines)}
        if path == "/rest/db/file":
            if self.liar:
                return {"local": {"sequence": 1, "ignored": False}}
            if p["file"] in getattr(self, "not_found", ()):
                raise core.ApiError("/rest/db/file: HTTPError")        # 404: Syncthing nunca la indexó
            if p["file"] in getattr(self, "never_indexed", ()):
                return {"local": {"sequence": 0, "ignored": False}}   # el objeto vacío que devuelve Syncthing
            return {"local": {"sequence": 1, "ignored": manage.match_rule(self.lines, p["file"])[1]}}
        if path == "/rest/config/folders":
            return [{"id": "home", "path": self.root}]
        raise AssertionError(path)

    def post(self, path, body=None, **p):
        if path == "/rest/db/ignores":
            self.lines = list(body["ignore"])
            self.posts.append(list(body["ignore"]))
        return {}


class RuleTests(unittest.TestCase):
    def test_valid_rel_rechaza_entrada_peligrosa(self):
        for bad in ("", "/", "../x", "a/../b", "a//b", "a\nb", "a\rb", "a\0b", "~/x", ".stignore",
                    "a/.stignore", ".stfolder", ".", "a/./b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                manage.valid_rel(bad)
        self.assertEqual(manage.valid_rel("/Docs/Fotos/"), "Docs/Fotos")

    def test_regla_escapa_comodines_y_queda_anclada(self):
        self.assertEqual(manage.rule_for("Docs"), "/Docs")
        self.assertEqual(manage.rule_for("a*b"), "/a\\*b")
        self.assertEqual(manage.rule_for("x[1]"), "/x\\[1\\]")
        self.assertEqual(manage.rule_for("!raro"), "/!raro")   # el '/' inicial neutraliza la negación

    def test_una_regla_escapada_solo_coincide_con_ese_nombre(self):
        lines = [manage.rule_for("a*b")]
        self.assertTrue(manage.match_rule(lines, "a*b")[1])
        self.assertFalse(manage.match_rule(lines, "axxb")[1])

    def test_matcher_semantica_de_syncthing(self):
        L = ["// comentario", "", "/anclada", "suelta", "(?i)Mayus", "dir/**/cache", "*.tmp", "!/anclada/excepcion", "/anclada/**"]
        m = manage.match_rule
        self.assertTrue(m(L, "anclada")[1])
        self.assertFalse(m(L, "otra/anclada")[1])
        self.assertTrue(m(L, "a/b/suelta")[1])         # sin '/' inicial coincide en cualquier nivel
        self.assertTrue(m(L, "x/MAYUS")[1])            # (?i)
        self.assertTrue(m(L, "dir/a/b/cache")[1])      # **
        self.assertTrue(m(L, "algo.tmp")[1])
        self.assertTrue(m(L, "anclada/hijo")[1])       # lo cubre la carpeta padre ignorada

    def test_primera_coincidencia_manda_y_la_negacion_gana_si_va_antes(self):
        self.assertTrue(manage.match_rule(["/x", "!/x"], "x")[1])
        self.assertFalse(manage.match_rule(["!/x", "/x"], "x")[1])
        self.assertEqual(manage.match_rule(["/x"], "x")[0], "/x")
        self.assertEqual(manage.match_rule(["/x"], "y"), (None, False))

    def test_bloque_gestionado_va_arriba_y_conserva_el_resto(self):
        base = ["// mío", ".cache", "/Data"]
        n = manage.plan_change(base, "Fotos", "ignore")
        self.assertEqual(n, [manage.MARK_BEGIN, "/Fotos", manage.MARK_END, *base])
        n2 = manage.plan_change(n, "Docs", "ignore")
        self.assertEqual(manage.split_managed(n2)[0], ["/Fotos", "/Docs"])
        self.assertEqual(manage.split_managed(n2)[1], base)         # lo ajeno no se toca
        self.assertEqual(manage.plan_change(n2, "Fotos", "unignore"),
                         [manage.MARK_BEGIN, "/Docs", manage.MARK_END, *base])
        self.assertEqual(manage.plan_change(manage.plan_change(base, "F", "ignore"), "F", "unignore"), base)

    def test_negacion_solo_cuando_se_pide(self):
        n = manage.plan_change([".cache"], ".cache", "unignore", negate=True)
        self.assertEqual(manage.split_managed(n)[0], ["!/.cache"])
        self.assertTrue(manage.match_rule(n, ".cache")[1] is False)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def run_apply(self, api, rel, action):
        return manage.apply_change(api, "home", rel, action, state_dir=self.tmp, sleep=lambda s: None)

    def test_ignorar_escribe_backup_log_y_verifica(self):
        api = IgnoreApi(["// mío", ".cache"])
        r = self.run_apply(api, "Fotos", "ignore")
        self.assertTrue(r.ok and r.verified)
        self.assertEqual(r.changed, [manage.MARK_BEGIN, "/Fotos", manage.MARK_END])
        self.assertIn("/Fotos", api.lines)
        self.assertEqual(Path(r.backup).read_text().splitlines(), ["// mío", ".cache"])   # el original, intacto
        self.assertEqual(oct(Path(r.backup).stat().st_mode & 0o777), "0o600")
        log = [json.loads(x) for x in next(self.tmp.glob("cambios-*.jsonl")).read_text().splitlines()]
        self.assertEqual((log[0]["action"], log[0]["rel"], log[0]["folder"]), ("ignore", "Fotos", "home"))
        self.assertNotIn("apikey", json.dumps(log).lower())

    def test_dejar_de_ignorar_una_regla_del_panel_la_quita(self):
        api = IgnoreApi(["a"])
        self.run_apply(api, "Fotos", "ignore")
        r = self.run_apply(api, "Fotos", "unignore")
        self.assertTrue(r.verified)
        self.assertEqual(api.lines, ["a"])

    def test_dejar_de_ignorar_una_regla_ajena_agrega_negacion_arriba(self):
        api = IgnoreApi(["// mío", ".cache", "/Data"])
        r = self.run_apply(api, ".cache", "unignore")
        self.assertTrue(r.verified and r.ignored_after is False)
        self.assertEqual(manage.split_managed(api.lines)[0], ["!/.cache"])
        self.assertEqual(manage.split_managed(api.lines)[1], ["// mío", ".cache", "/Data"])  # su regla sigue ahí
        self.assertEqual(len(api.posts), 2)   # primero quitó la propia (no alcanzó), después negó

    def test_si_syncthing_no_refleja_el_cambio_no_afirma_exito(self):
        r = self.run_apply(IgnoreApi([], liar=True), "Fotos", "ignore")
        self.assertFalse(r.ok)
        self.assertIs(r.verified, False)

    def test_ruta_invalida_no_llega_a_la_api(self):
        api = IgnoreApi(["a"])
        with self.assertRaises(ValueError):
            self.run_apply(api, "../etc", "ignore")
        with self.assertRaises(ValueError):
            self.run_apply(api, "ok\n!/**", "ignore")   # intento de inyectar una regla por salto de línea
        self.assertEqual(api.posts, [])

    def test_ruta_nunca_indexada_se_verifica_con_la_regla(self):
        api = IgnoreApi([])
        api.never_indexed = {"Fotos"}
        r = self.run_apply(api, "Fotos", "ignore")
        self.assertTrue(r.verified and r.ignored_after is True)

    def test_ruta_que_la_api_responde_404_se_trata_como_sin_entrada(self):
        api = IgnoreApi([])
        api.not_found = {"Fotos"}
        r1 = self.run_apply(api, "Fotos", "ignore")
        self.assertTrue(r1.verified and r1.ignored_after is True)
        r2 = self.run_apply(api, "Fotos", "unignore")       # el caso que rompió en la prueba en vivo
        self.assertTrue(r2.verified and r2.ignored_after is False)
        self.assertEqual(api.lines, [])

    def test_dejar_de_ignorar_algo_que_solo_ignora_la_regla_no_dice_no_estaba(self):
        api = IgnoreApi([".cache"])
        api.never_indexed = {".cache"}     # como en la Acer: sin entrada, ignorada por la regla
        r = self.run_apply(api, ".cache", "unignore")
        self.assertNotIn("no estaba", r.note)
        self.assertEqual(manage.split_managed(api.lines)[0], ["!/.cache"])

    def test_dos_cambios_en_el_mismo_segundo_no_pisan_el_backup(self):
        api = IgnoreApi([])
        a = self.run_apply(api, "A", "ignore")
        b = self.run_apply(api, "B", "ignore")
        self.assertNotEqual(a.backup, b.backup)
        self.assertEqual(Path(a.backup).read_text().strip(), "")
        self.assertIn("/A", Path(b.backup).read_text())

    def test_ignorar_dos_veces_no_duplica(self):
        api = IgnoreApi([])
        self.run_apply(api, "Fotos", "ignore")
        r = self.run_apply(api, "Fotos", "ignore")
        self.assertEqual(manage.split_managed(api.lines)[0], ["/Fotos"])
        self.assertIn("ya estaba", r.note)

    def test_restore_solo_acepta_backups_del_panel(self):
        api = IgnoreApi(["a", "b"])
        r = self.run_apply(api, "Fotos", "ignore")
        n = manage.restore_backup(api, "home", r.backup, state_dir=self.tmp, clock=lambda: 1e9)
        self.assertEqual((n, api.lines), (2, ["a", "b"]))
        evil = self.tmp / "otro.stignore"
        evil.write_text("/**\n")
        with self.assertRaises(ValueError):
            manage.restore_backup(api, "home", str(evil), state_dir=self.tmp)
        with self.assertRaises(ValueError):
            manage.restore_backup(api, "home", str(self.tmp / "backups" / ".." / "otro.stignore"), state_dir=self.tmp)


class ChildrenTests(unittest.TestCase):
    def test_lista_subcarpetas_con_estado_y_regla(self):
        root = Path(tempfile.mkdtemp())
        for d in ("Docs", ".cache", "Fotos"):
            (root / d).mkdir()
        (root / "suelto.txt").write_text("x")
        (root / "enlace").symlink_to(root / "Docs")
        api = IgnoreApi([".cache"])
        api.root = str(root)
        got = {c.name: c for c in manage.children(api, "home")}
        self.assertEqual(set(got), {"Docs", ".cache", "Fotos", "enlace"})   # los archivos sueltos no
        self.assertTrue(got[".cache"].ignored)
        self.assertEqual(got[".cache"].rule, ".cache")
        self.assertFalse(got["Docs"].ignored)
        self.assertEqual(got["enlace"].kind, "link")

    def test_entrada_vacia_de_la_api_no_cuenta_como_indexada(self):
        self.assertIsNone(manage.entry_ignored({"local": {"sequence": 0, "ignored": False}}))
        self.assertIsNone(manage.entry_ignored({}))
        self.assertIs(manage.entry_ignored({"local": {"sequence": 5, "ignored": True}}), True)

    def test_sin_entrada_decide_la_regla_y_la_contradiccion_se_marca(self):
        root = Path(tempfile.mkdtemp())
        for d in (".cache", "Nueva", "Vieja"):
            (root / d).mkdir()
        api = IgnoreApi([".cache", "Vieja"])
        api.root = str(root)
        api.never_indexed = {".cache", "Nueva"}
        api.get_orig = api.get

        def get(path, **p):
            if path == "/rest/db/file" and p["file"] == "Vieja":   # indexada antes de la regla
                return {"local": {"sequence": 9, "ignored": False}}
            return api.get_orig(path, **p)
        api.get = get
        got = {c.name: c for c in manage.children(api, "home")}
        self.assertIs(got[".cache"].ignored, True)      # nunca indexada pero la regla la cubre
        self.assertIsNone(got["Nueva"].ignored)
        self.assertTrue(got["Vieja"].stale)
        self.assertEqual({r.name: r.here for r in manage.merge_rows(list(got.values()), None)},
                         {".cache": "ignored", "Nueva": "unindexed", "Vieja": "stale"})

    def test_dir_size_respeta_presupuesto_y_no_sigue_enlaces(self):
        root = Path(tempfile.mkdtemp())
        (root / "a").mkdir()
        (root / "a" / "f").write_bytes(b"x" * 100)
        (root / "l").symlink_to(root / "a")
        self.assertEqual(manage.dir_size(str(root)), (100, 1, True))
        t = iter(range(1000))
        self.assertFalse(manage.dir_size(str(root), budget=0, clock=lambda: next(t))[2])


class SizeSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_tamanos_de_mas_de_2gb_no_se_desbordan(self):
        from wyss_sync import detail
        got = []
        real_sizes = manage.dir_sizes
        manage.dir_sizes = lambda path, rel, rules, budget=4.0: {
            "disk": 6_000_000_000, "synced": 5_000_000_000, "files": 7, "synced_files": 5, "complete": True}
        try:
            t = detail.SizeTask([("grande", "/x")], manage.Rules([]))
            t.one.connect(lambda *a: got.append(a))
            t.run()    # en este hilo: la señal se entrega directa
        finally:
            manage.dir_sizes = real_sizes
        self.assertEqual(got, [("grande", 6_000_000_000, 5_000_000_000, 7, 5, True)])
        self.assertEqual(manage.human_size(6_000_000_000), "5,6 GB")
        self.assertEqual(manage.human_size(10, complete=False), "≥ 10 B")


class MergeTests(unittest.TestCase):
    def test_une_ambos_lados_y_marca_lo_que_falta(self):
        local = [manage.Child("A", "A", "dir", 1.0, False), manage.Child("B", "B", "dir", 2.0, True, "/B"),
                 manage.Child("C", "C", "dir", 3.0, None)]
        remote = [{"name": "A", "rel": "A", "ignored": True, "rule": "A"}, {"name": "Z", "rel": "Z", "ignored": False}]
        rows = {r.name: r for r in manage.merge_rows(local, remote)}
        self.assertEqual((rows["A"].here, rows["A"].there), ("sync", "ignored"))
        self.assertEqual((rows["B"].here, rows["B"].there), ("ignored", "missing"))
        self.assertEqual(rows["C"].here, "unindexed")
        self.assertEqual((rows["Z"].here, rows["Z"].there), ("missing", "sync"))   # solo existe allá
        self.assertEqual({r.there for r in manage.merge_rows(local, None)}, {"unknown"})


class CliTests(unittest.TestCase):
    def payload(self, **kw):
        return base64.urlsafe_b64encode(json.dumps(kw).encode()).decode()

    def test_cli_apply_devuelve_json_sin_secretos(self):
        out = []
        api = IgnoreApi([])
        with tempfile.TemporaryDirectory() as d:
            old = manage.STATE_DIR
            manage.STATE_DIR = Path(d)
            try:
                orig = manage.apply_change
                manage.apply_change = lambda *a, **k: orig(*a, state_dir=Path(d), sleep=lambda s: None, **k)
                rc = manage.cli(["apply", self.payload(folder="home", rel="Fotos", action="ignore")], api=api, out=out.append)
            finally:
                manage.apply_change, manage.STATE_DIR = orig, old
        self.assertEqual(rc, 0)
        data = json.loads(out[0])
        self.assertTrue(data["ok"] and data["result"]["verified"])

    def test_cli_error_es_json_y_codigo_distinto_de_cero(self):
        out = []
        rc = manage.cli(["apply", self.payload(folder="home", rel="../x", action="ignore")], api=IgnoreApi([]), out=out.append)
        self.assertNotEqual(rc, 0)
        self.assertFalse(json.loads(out[0])["ok"])

    def test_remote_cli_manda_payload_base64_sin_shell(self):
        seen = {}

        class R:
            returncode, stdout = 0, '{"ok": true}'

        def runner(cmd, **k):
            seen["cmd"] = cmd
            return R()

        manage.remote_cli(["pc-b"], "apply", {"folder": "home", "rel": "a b;rm -rf ~", "action": "ignore"}, runner=runner)
        arg = seen["cmd"][-1]
        self.assertRegex(arg, r"^[A-Za-z0-9_=-]+$")           # nada que un shell pueda interpretar
        self.assertEqual(seen["cmd"][-4:-1], [manage.REMOTE_ENTRY, "--cli", "apply"])
        self.assertEqual(json.loads(base64.urlsafe_b64decode(arg))["rel"], "a b;rm -rf ~")


class DiskTests(unittest.TestCase):
    MOUNTINFO = (
        "25 1 259:2 /@home /home rw,relatime shared:1 - btrfs /dev/nvme0n1p2 rw,ssd\n"
        "30 1 0:40 / /mnt/hdd rw,relatime shared:5 - autofs systemd-1 rw\n"
        "31 30 8:2 / /mnt/hdd rw,relatime shared:6 - ntfs3 /dev/sda2 rw\n"
        "20 1 259:3 / / rw,relatime - btrfs /dev/nvme0n1p3 rw\n")

    def table(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "mountinfo"
        p.write_text(self.MOUNTINFO)
        return core.mount_table(str(p))

    def test_mount_table_lee_montajes_y_fs(self):
        t = self.table()
        self.assertEqual([(m["mount"], m["fstype"], m["source"]) for m in t][:3],
                         [("/home", "btrfs", "/dev/nvme0n1p2"), ("/mnt/hdd", "autofs", "systemd-1"),
                          ("/mnt/hdd", "ntfs3", "/dev/sda2")])

    def test_el_montaje_mas_especifico_gana_y_autofs_no_cuenta(self):
        t = self.table()
        real_stat, real_rp = os.stat, os.path.realpath
        try:
            os.path.realpath = lambda p: p
            os.stat = lambda p, *a, **k: real_stat("/")
            d = core.disk_info("/mnt/hdd/Data/x", t)
            self.assertEqual((d["mount"], d["fstype"], d["source"]), ("/mnt/hdd", "ntfs3", "/dev/sda2"))
            h = core.disk_info("/home/u/Documentos", t)
            self.assertEqual((h["mount"], h["fstype"]), ("/home", "btrfs"))
            r = core.disk_info("/etc/x", t)
            self.assertEqual(r["mount"], "/")
        finally:
            os.stat, os.path.realpath = real_stat, real_rp

    def test_un_enlace_se_resuelve_al_disco_de_destino(self):
        root = Path(tempfile.mkdtemp())
        (root / "real").mkdir()
        (root / "enlace").symlink_to(root / "real")
        d = core.disk_info(str(root / "enlace"))
        self.assertEqual(d["real"], os.path.realpath(root / "real"))
        self.assertIn("total", d)

    def test_disk_label(self):
        self.assertEqual(manage.disk_label({"kind": "NVMe", "device": "nvme0n1", "fstype": "btrfs"}), "NVMe nvme0n1 · btrfs")
        self.assertEqual(manage.disk_label({"kind": "", "device": "", "fstype": "tmpfs"}), "Disco · tmpfs")
        self.assertEqual(manage.disk_label(None), "")

    def test_physical_disk_de_algo_que_no_es_un_dispositivo(self):
        self.assertEqual(core.physical_disk("systemd-1"), {})
        self.assertEqual(core.physical_disk("//srv/share"), {})


# ---------------------------------------------------------------- tanda 2: historial, diferencias, conflictos, versiones, ajustes
class EventApi:
    def __init__(self, events, start="t0"):
        self.events, self.start, self.seen_since = events, start, []

    def get(self, path, _timeout=None, **p):
        if path == "/rest/system/status":
            return {"startTime": self.start}
        if path == "/rest/events":
            self.seen_since.append(p["since"])
            return [e for e in self.events if e["id"] > p["since"]]
        raise AssertionError(path)


def ev(i, t, **data):
    return {"id": i, "type": t, "time": f"2026-10-10T21:00:{i:02d}-03:00", "data": data}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_eventos_a_filas(self):
        r = manage.event_to_record(ev(1, "ItemFinished", action="update", folder="f", item="a/b.txt", type="file", error=None), "pc")
        self.assertEqual((r["what"], r["path"], r["error"]), ("recibido aquí", "a/b.txt", ""))
        r = manage.event_to_record(ev(2, "ItemFinished", action="update", folder="f", item="x", type="file", error="sin permiso"), "pc")
        self.assertIn("falló", r["what"])
        self.assertEqual(r["error"], "sin permiso")
        r = manage.event_to_record(ev(3, "LocalChangeDetected", action="deleted", folderID="f", path="z", type="file"), "pc")
        self.assertEqual(r["what"], "borrado en esta PC")
        r = manage.event_to_record(ev(4, "RemoteChangeDetected", action="modified", folderID="f", path="z", type="file"), "pc")
        self.assertEqual(r["what"], "modificado en la otra PC")
        self.assertIsNone(manage.event_to_record(ev(5, "StateChanged"), "pc"))

    def test_no_registra_los_archivos_del_propio_panel(self):
        for t, key in (("LocalChangeDetected", "path"), ("RemoteChangeDetected", "path"), ("ItemFinished", "item")):
            d = {key: ".local/state/wyss-sync/historial-pc.jsonl", "folderID": "f", "folder": "f", "action": "update"}
            self.assertIsNone(manage.event_to_record(ev(1, t, **d), "pc"))
        ok = manage.event_to_record(ev(2, "LocalChangeDetected", path=".local/state/wyss-syncX/y", folderID="f", action="modified"), "pc")
        self.assertIsNone(ok)    # el prefijo es de la carpeta entera; un vecino con nombre parecido también se salta
        self.assertIsNotNone(manage.event_to_record(ev(3, "LocalChangeDetected", path=".local/state/otra/y", folderID="f", action="modified"), "pc"))

    def test_harvest_es_incremental_y_sobrevive_al_reinicio(self):
        api = EventApi([ev(1, "ItemFinished", action="update", folder="f", item="a", type="file", error=None),
                        ev(2, "LocalChangeDetected", action="modified", folderID="f", path="b", type="file")])
        self.assertEqual(manage.harvest(api, self.tmp, host="pc"), 2)
        self.assertEqual(manage.harvest(api, self.tmp, host="pc"), 0)        # ya estaban
        api.events.append(ev(3, "ItemFinished", action="delete", folder="f", item="c", type="file", error=None))
        self.assertEqual(manage.harvest(api, self.tmp, host="pc"), 1)
        self.assertEqual(api.seen_since, [0, 2, 2])
        api.start, api.events = "t1", [ev(1, "ItemFinished", action="update", folder="g", item="n", type="file", error=None)]
        self.assertEqual(manage.harvest(api, self.tmp, host="pc"), 1)        # ids vuelven a 1: no se pierde
        rows = manage.read_history(self.tmp, "pc")
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["folder"], "g")                              # el más nuevo primero

    def test_sin_eventos_nuevos_no_reescribe_el_estado(self):
        api = EventApi([ev(1, "ItemFinished", action="update", folder="f", item="a", type="file", error=None)])
        manage.harvest(api, self.tmp, host="pc")
        sp = self.tmp / "historial-estado-pc.json"
        before = (sp.stat().st_mtime_ns, sp.read_text())
        import time as _t
        _t.sleep(0.01)
        self.assertEqual(manage.harvest(api, self.tmp, host="pc"), 0)
        self.assertEqual((sp.stat().st_mtime_ns, sp.read_text()), before)   # ni contenido ni fecha: Syncthing no ve nada

    def test_filtros_de_lectura(self):
        api = EventApi([ev(1, "ItemFinished", action="update", folder="f", item="Docs/uno.txt", type="file", error=None),
                        ev(2, "ItemFinished", action="update", folder="g", item="otro.bin", type="file", error="x")])
        manage.harvest(api, self.tmp, host="pc")
        self.assertEqual(len(manage.read_history(self.tmp, "pc", folder="f")), 1)
        self.assertEqual(len(manage.read_history(self.tmp, "pc", text="uno")), 1)
        self.assertEqual(len(manage.read_history(self.tmp, "pc", only_errors=True)), 1)
        self.assertEqual(manage.read_history(self.tmp, "nadie"), [])

    def test_la_lectura_oculta_registros_viejos_del_propio_panel(self):
        (self.tmp / "historial-pc.jsonl").write_text(
            '{"ts":"a","what":"x","path":".local/state/wyss-sync/historial-estado-x.json"}\n'
            '{"ts":"b","what":"y","path":"Docs/a.txt"}\n')
        self.assertEqual([r["path"] for r in manage.read_history(self.tmp, "pc")], ["Docs/a.txt"])

    def test_archivo_corrupto_no_rompe_la_lectura(self):
        (self.tmp / "historial-pc.jsonl").write_text('{"ts":"a","what":"x","path":"p"}\nBASURA\n')
        self.assertEqual(len(manage.read_history(self.tmp, "pc")), 1)


class DiffApi:
    def get(self, path, _timeout=None, **p):
        if path == "/rest/db/status":
            return {"needTotalItems": 2, "needBytes": 30, "state": "idle", "globalFiles": 9, "globalBytes": 900}
        if path == "/rest/db/completion":
            return {"completion": 80.0, "needItems": 1, "needBytes": 5}
        if path == "/rest/db/need":
            return {"progress": [{"name": "a", "size": 10, "type": "FILE_INFO_TYPE_FILE"}],
                    "queued": [{"name": "b", "size": 20, "type": "FILE_INFO_TYPE_FILE", "deleted": True}], "rest": []}
        if path == "/rest/db/remoteneed":
            return {"files": [{"name": "c", "size": 5, "type": "FILE_INFO_TYPE_DIRECTORY"}]}
        raise AssertionError(path)


class DiffTests(unittest.TestCase):
    def test_folder_diff_junta_los_dos_lados(self):
        d = manage.folder_diff(DiffApi(), "f", "REM")
        self.assertEqual((d["here"]["items"], d["here"]["bytes"], d["there"]["items"]), (2, 30, 1))
        self.assertEqual([x["name"] for x in d["here"]["list"]], ["a", "b"])
        self.assertTrue(d["here"]["list"][1]["deleted"])
        self.assertEqual(d["there"]["list"][0]["type"], "directory")
        self.assertEqual(d["completion"], 80.0)

    def test_sin_pendientes_no_pide_las_listas(self):
        class Quiet(DiffApi):
            def get(self, path, _timeout=None, **p):
                if path in ("/rest/db/need", "/rest/db/remoteneed"):
                    raise AssertionError("no debía pedir " + path)
                if path == "/rest/db/status":
                    return {"needTotalItems": 0, "needBytes": 0}
                return {"completion": 100, "needItems": 0, "needBytes": 0}
        d = manage.folder_diff(Quiet(), "f", "REM")
        self.assertEqual((d["here"]["list"], d["there"]["list"]), ([], []))


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.state = Path(tempfile.mkdtemp())

    def mk(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_parse_nombres(self):
        self.assertEqual(manage.parse_conflict_name("nota.sync-conflict-20260918-213613-ABCDEFG.md"),
                         ("nota.md", "2026-09-18 21:36:13", "ABCDEFG"))
        self.assertEqual(manage.parse_conflict_name("sin_ext.sync-conflict-20260918-213613-ABCDEFG")[0], "sin_ext")
        self.assertEqual(manage.parse_conflict_name("a.tar.sync-conflict-20260918-213613-ABCDEFG.gz")[0], "a.tar.gz")
        self.assertIsNone(manage.parse_conflict_name("normal.txt"))

    def test_find_respeta_skip_y_dirs_pesados(self):
        self.mk("Docs/n.sync-conflict-20260918-213613-ABCDEFG.md")
        self.mk("Docs/n.md")
        self.mk(".cache/x.sync-conflict-20260918-213613-ABCDEFG.md")
        self.mk("Ignorada/y.sync-conflict-20260918-213613-ABCDEFG.md")
        found, done = manage.find_conflicts(str(self.root), "f", skip={"Ignorada"})
        self.assertTrue(done)
        self.assertEqual([c.rel for c in found], ["Docs/n.sync-conflict-20260918-213613-ABCDEFG.md"])
        self.assertTrue(found[0].orig_exists)

    def test_find_no_entra_a_la_papelera_del_panel(self):
        self.mk("descartados/x.sync-conflict-20260918-213613-ABCDEFG.md")
        self.mk("Docs/n.sync-conflict-20260918-213613-ABCDEFG.md")
        found, _ = manage.find_conflicts(str(self.root), "f")
        self.assertEqual([c.rel for c in found], ["Docs/n.sync-conflict-20260918-213613-ABCDEFG.md"])

    def test_find_corta_por_presupuesto(self):
        self.mk("a/n.sync-conflict-20260918-213613-ABCDEFG.md")
        t = iter(range(100))
        found, done = manage.find_conflicts(str(self.root), "f", budget=0, clock=lambda: next(t))
        self.assertFalse(done)

    def test_diff_texto_binario_e_igual(self):
        a, b = self.mk("a.txt", "uno\ndos\ntres\n"), self.mk("b.txt", "uno\nDOS\ntres\n")
        d = manage.diff_files(str(a), str(b))
        self.assertEqual((d["kind"], d["added"], d["removed"]), ("text", 1, 1))
        self.assertIn("+DOS", d["lines"])
        self.assertEqual(manage.diff_files(str(a), str(a))["kind"], "same")
        bin1, bin2 = self.root / "1.bin", self.root / "2.bin"
        bin1.write_bytes(b"\0\1\2")
        bin2.write_bytes(b"\0\1\3")
        self.assertEqual(manage.diff_files(str(bin1), str(bin2))["kind"], "binary")
        self.assertEqual(manage.diff_files(str(a), str(self.root / "no"))["kind"], "missing")

    def conflict(self, orig_text="orig", conf_text="conf"):
        o = self.mk("Docs/n.md", orig_text)
        c = self.mk("Docs/n.sync-conflict-20260918-213613-ABCDEFG.md", conf_text)
        return manage.Conflict("f", str(c), str(o), "Docs/" + c.name, "", "ABCDEFG", 4, True)

    def test_descartar_guarda_antes_de_quitar(self):
        c = self.conflict()
        r = manage.resolve_conflict(c, "discard", state_dir=self.state, clock=lambda: 1.0)
        self.assertFalse(Path(c.path).exists())
        self.assertEqual(Path(r["saved"][0]).read_text(), "conf")
        self.assertEqual(Path(c.original).read_text(), "orig")

    def test_usar_la_copia_guarda_el_original(self):
        c = self.conflict()
        r = manage.resolve_conflict(c, "use", state_dir=self.state, clock=lambda: 2.0)
        self.assertEqual(Path(c.original).read_text(), "conf")
        self.assertFalse(Path(c.path).exists())
        self.assertEqual(Path(r["saved"][0]).read_text(), "orig")

    def test_rechaza_rutas_que_no_son_conflictos(self):
        o = self.mk("Docs/n.md")
        bad = manage.Conflict("f", str(o), str(o), "n.md", "", "", 1, True)
        with self.assertRaises(ValueError):
            manage.resolve_conflict(bad, "discard", state_dir=self.state)
        c = self.conflict()
        c.original = str(self.root / "Otro" / "n.md")      # original en otra carpeta: inconsistente
        with self.assertRaises(ValueError):
            manage.resolve_conflict(c, "use", state_dir=self.state)
        with self.assertRaises(ValueError):
            manage.resolve_conflict(self.conflict(), "borrar", state_dir=self.state)


class VersionApi:
    def __init__(self):
        self.posts = []

    def get(self, path, _timeout=None, **p):
        return {"Docs/a.md": [{"versionTime": "2026-09-01T10:00:00-03:00", "size": 5},
                              {"versionTime": "2026-09-18T21:36:13-03:00", "size": 7}],
                "b.txt": [{"versionTime": "2026-08-01T08:00:00-03:00", "size": 1}]}

    def post(self, path, body=None, **p):
        self.posts.append((path, body, p))
        return {}


class VersionTests(unittest.TestCase):
    def test_lista_ordenada_y_filtrada(self):
        rows = manage.list_versions(VersionApi(), "f")
        self.assertEqual([r["path"] for r in rows], ["Docs/a.md", "b.txt"])
        self.assertEqual(rows[0]["versions"][0]["versionTime"], "2026-09-18T21:36:13-03:00")
        self.assertEqual([r["path"] for r in manage.list_versions(VersionApi(), "f", text="DOCS")], ["Docs/a.md"])

    def test_ruta_del_archivo_versionado(self):
        self.assertEqual(manage.version_file("/r", "Docs/a.md", "2026-09-18T21:36:13-03:00"),
                         "/r/.stversions/Docs/a~20260918-213613.md")
        self.assertEqual(manage.version_file("/r", "b", "2026-09-18T21:36:13Z"), "/r/.stversions/b~20260918-213613")

    def test_restaurar_valida_y_registra(self):
        api, tmp = VersionApi(), Path(tempfile.mkdtemp())
        r = manage.restore_version(api, "f", "Docs/a.md", "2026-09-18T21:36:13-03:00", state_dir=tmp)
        self.assertTrue(r["ok"])
        self.assertEqual(api.posts[0][1], {"Docs/a.md": "2026-09-18T21:36:13-03:00"})
        for bad_rel, bad_t in (("../x", "2026-09-18T21:36:13-03:00"), ("a", "ayer")):
            with self.assertRaises(ValueError):
                manage.restore_version(api, "f", bad_rel, bad_t, state_dir=tmp)
        self.assertEqual(len(api.posts), 1)


class CfgApi:
    def __init__(self):
        self.cfg = {"id": "f", "label": "F", "path": "/p", "type": "sendreceive", "paused": False,
                    "rescanIntervalS": 3600, "fsWatcherEnabled": True, "ignorePerms": False, "maxConflicts": 10,
                    "versioning": {"type": "", "params": {}, "cleanupIntervalS": 3600, "fsPath": "", "fsType": "basic"},
                    "devices": [{"deviceID": "X"}]}
        self.puts = []

    def get(self, path, _timeout=None, **p):
        return json.loads(json.dumps(self.cfg))

    def put(self, path, body, **p):
        self.puts.append(path)
        self.cfg = body


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_validacion_rechaza_lo_que_no_esta_en_la_lista(self):
        for bad in ({"path": "/etc"}, {"devices": []}, {"type": "otro"}, {"rescanIntervalS": -1},
                    {"rescanIntervalS": True}, {"paused": "si"}, {"versioning": "magia"},
                    {"versioning_params": {"keep": "diez"}}, {"maxConflicts": 1000}):
            with self.assertRaises(ValueError, msg=str(bad)):
                manage.validate_changes(bad)
        self.assertEqual(manage.validate_changes({"paused": True, "rescanIntervalS": 600}),
                         {"paused": True, "rescanIntervalS": 600})

    def test_aplicar_cambia_solo_lo_pedido_y_deja_backup(self):
        api = CfgApi()
        r = manage.apply_settings(api, "f", {"paused": True, "rescanIntervalS": 600}, state_dir=self.tmp)
        self.assertTrue(r["ok"])
        self.assertTrue(api.cfg["paused"])
        self.assertEqual(api.cfg["devices"], [{"deviceID": "X"}])      # lo demás, intacto
        self.assertEqual(api.cfg["path"], "/p")
        self.assertEqual(json.loads(Path(r["backup"]).read_text())["paused"], False)
        self.assertEqual(sorted(x.split(":")[0] for x in r["changed"]), ["Escaneo cada (s)", "Pausada"])

    def test_versionado_staggered_con_parametros(self):
        api = CfgApi()
        manage.apply_settings(api, "f", {"versioning": "staggered", "versioning_params": {"maxAge": "86400", "x": "1"}},
                              state_dir=self.tmp)
        v = api.cfg["versioning"]
        self.assertEqual((v["type"], v["params"], v["cleanupIntervalS"]), ("staggered", {"maxAge": "86400"}, 3600))
        manage.apply_settings(api, "f", {"versioning": ""}, state_dir=self.tmp, clock=lambda: 5.0)
        self.assertEqual(api.cfg["versioning"]["type"], "")

    def test_sin_cambios_no_escribe(self):
        api = CfgApi()
        r = manage.apply_settings(api, "f", {"paused": False}, state_dir=self.tmp)
        self.assertEqual((r["note"], api.puts), ("sin cambios", []))

    def test_deshacer_restaura_y_valida_el_backup(self):
        api = CfgApi()
        r = manage.apply_settings(api, "f", {"type": "receiveonly"}, state_dir=self.tmp)
        self.assertEqual(api.cfg["type"], "receiveonly")
        self.assertTrue(manage.restore_settings(api, "f", r["backup"], state_dir=self.tmp))
        self.assertEqual(api.cfg["type"], "sendreceive")
        evil = self.tmp / "config-f-x.json"
        evil.write_text('{"id": "f", "path": "/"}')
        with self.assertRaises(ValueError):
            manage.restore_settings(api, "f", str(evil), state_dir=self.tmp)
        with self.assertRaises(ValueError):
            manage.restore_settings(api, "otra", r["backup"], state_dir=self.tmp)

    def test_id_de_carpeta_no_puede_escapar_de_la_ruta(self):
        with self.assertRaises(ValueError):
            manage.apply_settings(CfgApi(), "../x", {"paused": True}, state_dir=self.tmp)


# ---------------------------------------------------------------- pestañas (humo, sin red)
class PagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def ctx(self):
        from wyss_sync.pages import Context
        c = Context(object())
        c.update({"local": {"name": "pc-a"}, "remote": {"name": "pc-b", "online": True, "id": "R",
                                                               "address": "1.2.3.4"},
                  "folders": [{"id": "f", "label": "Home", "local_path": "/tmp"}]})
        return c

    def test_historial_pinta_filas_y_marca_errores(self):
        from wyss_sync import pages
        p = pages.HistoryPage(self.ctx())
        rows = [{"ts": "2026-10-10T21:00:05-03:00", "host": "pc-a", "folder": "f", "path": "a/b", "what": "recibido aquí", "error": ""},
                {"ts": "2026-10-10T21:00:01-03:00", "host": "pc-b", "folder": "f", "path": "c", "what": "borrado (falló)", "error": "x"}]
        p._loaded((rows, True))
        self.assertEqual(p.table.topLevelItemCount(), 2)
        self.assertEqual(p.table.topLevelItem(0).text(2), "Home")          # el id se muestra como etiqueta
        self.assertEqual(p.table.topLevelItem(0).text(0), "2026-10-10 21:00:05")
        p._loaded(RuntimeError("boom"))
        self.assertIn("boom", p.info.text())

    def test_diferencias_resume_y_lista(self):
        from wyss_sync import pages
        p = pages.DiffsPage(self.ctx())
        d = {"here": {"items": 2, "bytes": 30, "list": [{"name": "a", "size": 10, "type": "file", "deleted": False}]},
             "there": {"items": 0, "bytes": 0, "list": []}, "completion": 80.0, "global_items": 9,
             "global_bytes": 900, "local_changed": 0, "state": "idle", "folder": "f"}
        p._loaded({"f": d})
        self.assertEqual(p.summary.topLevelItem(0).text(1), "2 · 30 B")
        self.assertEqual(p.summary.topLevelItem(0).text(4), "con pendientes")
        self.assertEqual(p.left.topLevelItemCount(), 1)
        p._loaded({"f": {"error": "x"}})
        self.assertEqual(p.summary.topLevelItem(0).text(4), "sin datos")

    def test_conflictos_y_versiones_se_listan(self):
        from wyss_sync import pages
        p = pages.ConflictsPage(self.ctx())
        c = manage.Conflict("f", "/r/Docs/n.sync-conflict-20260918-213613-ABCDEFG.md", "/r/Docs/n.md",
                            "Docs/n.sync-conflict-20260918-213613-ABCDEFG.md", "2026-09-18 21:36:13", "ABCDEFG", 12, True)
        p._conflicts_found(([c], True))
        self.assertEqual(p.list.topLevelItemCount(), 1)
        self.assertIn("1 conflictos", p.info.text())
        p._conflicts_found(([c], False))
        self.assertIn("incompleta", p.info.text())
        f = {"id": "f", "label": "Home", "local_path": "/r"}
        p._versions_found(([(f, {"path": "Docs/a.md", "versions": [{"versionTime": "2026-09-18T21:36:13-03:00", "modTime": "", "size": 7}]})], ["Otra"]))
        top = p.list.topLevelItem(0)
        self.assertEqual((top.text(0), top.childCount()), ("Docs/a.md", 1))
        self.assertIn("sin versionado: Otra", p.info.text())

    def test_diff_viewer_para_texto_binario_e_igual(self):
        from wyss_sync import pages
        p = pages.ConflictsPage(self.ctx())
        p._show_diff({"kind": "text", "lines": ["--- a", "+++ b", "@@ -1 +1 @@", "-uno", "+<b>dos</b>"], "added": 1, "removed": 1, "meta": {}})
        self.assertIn("&lt;b&gt;dos", p.viewer.toHtml().replace("&amp;", "&"))     # el contenido se escapa
        p._show_diff({"kind": "same", "meta": {}})
        self.assertIn("idénticos", p.viewer.toPlainText())
        p._show_diff({"kind": "binary", "meta": {"a": {"size": 1, "mtime": 0, "sha256": "aa"}, "b": None}})
        self.assertIn("no existe", p.viewer.toPlainText())

    def test_ajustes_tabla_y_cambios_del_dialogo(self):
        from wyss_sync import pages
        cur = manage.folder_settings({"type": "sendreceive", "paused": False, "rescanIntervalS": 86400,
                                      "fsWatcherEnabled": True, "ignorePerms": False, "maxConflicts": 10,
                                      "versioning": {"type": "staggered", "params": {"maxAge": "31536000"}}})
        p = pages.SettingsPage(self.ctx())
        p._loaded({"f": {"label": "Home", "local": cur, "remote": dict(cur, paused=True)}})
        top = p.table.topLevelItem(0)
        self.assertEqual(top.childCount(), 6)
        self.assertEqual([top.child(0).text(2), top.child(0).text(3)], ["Activa", "Pausada"])
        dlg = pages.SettingsDialog(None, "Home", cur, "A", "B")
        self.assertEqual(dlg.changes(), {})                       # sin tocar nada no cambia nada
        dlg.rescan.setValue(1)
        dlg.paused.setChecked(True)
        self.assertEqual(dlg.changes(), {"paused": True, "rescanIntervalS": 3600})
        i = dlg.ver.findData("simple")
        dlg.ver.setCurrentIndex(i)
        dlg.vparam.setValue(7)
        ch = dlg.changes()
        self.assertEqual((ch["versioning"], ch["versioning_params"]), ("simple", {"keep": "7"}))
        self.assertEqual(manage.validate_changes(ch)["versioning"], "simple")    # lo que arma el diálogo siempre valida

    def test_confirm_construye_con_y_sin_otra_pc(self):
        from wyss_sync import pages
        d = pages.Confirm(None, "Probar", ["x"], ["ojo"], "A", "B")
        self.assertTrue(d.both_pcs())
        d2 = pages.Confirm(None, "Probar", ["x"], [], "A", None)
        self.assertFalse(d2.both_pcs())

    def test_ventana_trae_las_seis_pestanas(self):
        w = ui.Window(None)
        self.assertEqual([w.tabs.tabText(i) for i in range(w.tabs.count())],
                         ["Carpetas", "Vínculos", "Historial", "Diferencias", "Conflictos y versiones", "Ajustes"])


class HistorianTests(unittest.TestCase):
    def test_reintenta_con_espera_creciente_y_se_recupera(self):
        tmp = Path(tempfile.mkdtemp())
        calls, sleeps = {"n": 0}, []

        class Flaky:
            def get(self, path, _timeout=None, **p):
                calls["n"] += 1
                if calls["n"] <= 3:
                    raise core.ApiError("caído")
                if path == "/rest/system/status":
                    return {"startTime": "t"}
                return []

        made = {"n": 0}

        def make_api():
            made["n"] += 1
            return Flaky()

        stop = {"v": False}

        def sleeper(s):
            sleeps.append(s)

        def stopper():
            return calls["n"] >= 6

        manage.historian(make_api, tmp, wait=0, stop=stopper, sleep=sleeper)
        self.assertEqual(sleeps, [5.0, 10.0, 20.0])          # espera creciente
        self.assertGreaterEqual(made["n"], 4)                # vuelve a leer la configuración tras cada falla
        self.assertTrue((tmp / "historial-estado-{}.json".format(manage.host_name())).exists())

    def test_unidad_systemd_apunta_al_panel_y_no_pide_privilegios(self):
        txt = (Path(__file__).parent / "wyss-sync-historian.service").read_text()
        self.assertIn("--historian", txt)
        self.assertIn("WantedBy=default.target", txt)
        self.assertNotIn("sudo", txt.lower())


class SyncedSizeTests(unittest.TestCase):
    LINES = ["// c", ".cache", "/Data", "*.tmp", "/Docs/privado", "!/Docs/privado/ok", "(?i)Basura", "a/b"]

    def test_rules_equivale_a_match_rule(self):
        r = manage.Rules(self.LINES)

        def full(rel):    # como recorre dir_sizes: si un padre está ignorado, todo lo de abajo también
            parts = rel.split("/")
            return any(r.ignored("/".join(parts[:i])) for i in range(1, len(parts) + 1))

        for rel in (".cache", "x/.cache", "Data", "Docs/privado", "Docs/otro", "Docs/privado/ok", "n.tmp", "z/n.tmp",
                    "BASURA", "x/basura", "a/b", "q/a/b", "Docs", "nada", "Data2", "Data/hijo/n.txt"):
            self.assertEqual(full(rel), manage.match_rule(self.LINES, rel)[1], rel)

    def test_primera_coincidencia_en_reglas_mezcladas(self):
        r = manage.Rules(["!/x/y", "/x/*", "(?i)z"])      # la negación literal va antes que el comodín
        self.assertFalse(r.ignored("x/y"))
        self.assertTrue(r.ignored("x/otro"))
        self.assertTrue(r.ignored("Z"))

    def test_dir_sizes_separa_disco_y_sincroniza(self):
        root = Path(tempfile.mkdtemp())
        (root / "Docs" / "privado").mkdir(parents=True)
        (root / "Docs" / "pub").mkdir()
        (root / "Docs" / "pub" / "a.txt").write_bytes(b"x" * 100)
        (root / "Docs" / "pub" / "b.tmp").write_bytes(b"x" * 40)
        (root / "Docs" / "privado" / "c.txt").write_bytes(b"x" * 1000)
        (root / "Docs" / "enlace").symlink_to(root / "Docs" / "pub")
        r = manage.Rules(["/Docs/privado", "*.tmp"])
        d = manage.dir_sizes(str(root / "Docs"), "Docs", r)
        self.assertEqual((d["disk"], d["synced"], d["files"], d["synced_files"], d["complete"]), (1140, 100, 3, 1, True))
        ig = manage.dir_sizes(str(root / "Docs"), "Docs", manage.Rules(["/Docs"]))
        self.assertEqual((ig["disk"], ig["synced"]), (1140, 0))        # carpeta ignorada: nada entra
        t = iter(range(100))
        self.assertFalse(manage.dir_sizes(str(root), "", r, budget=0, clock=lambda: next(t))["complete"])

    def test_velocidad_de_la_conexion(self):
        now = [100.0]
        calls = {"n": 0}

        class Net(FakeApi):
            pass

        r = routes()
        totals = [(1000, 5000), (3000, 5000 + 10_000)]

        def conns(_p):
            i, o = totals[min(calls["n"], 1)]
            calls["n"] += 1
            return {"connections": {OTHER: {"connected": True, "paused": False, "address": "tcp://1.2.3.4:22000",
                                            "inBytesTotal": i, "outBytesTotal": o}}}
        r["/rest/system/connections"] = conns
        c = core.Collector(FakeApi(r), local_probe=lambda: LOCAL, remote_probe=lambda h: REMOTE, clock=lambda: now[0])
        m1 = c.collect()
        self.assertEqual((m1["remote"]["rate_in"], m1["remote"]["rate_out"]), (0.0, 0.0))   # sin muestra previa
        now[0] = 102.0
        m2 = c.collect()
        self.assertEqual((m2["remote"]["rate_in"], m2["remote"]["rate_out"]), (1000.0, 5000.0))
        self.assertEqual(manage.human_rate(1536), "1,5 KB/s")

    def test_el_modelo_trae_los_bytes_que_sincroniza(self):
        c = core.Collector(FakeApi(routes(status={"state": "idle", "globalBytes": 6_300_000_000, "globalFiles": 10449,
                                                   "needBytes": 70, "needTotalItems": 0})),
                           local_probe=lambda: LOCAL, remote_probe=lambda h: REMOTE)
        f = c.collect()["folders"][0]
        self.assertEqual((f["sync_bytes"], f["sync_files"], f["need_bytes"]), (6_300_000_000, 10449, 70))


# ---------------------------------------------------------------- revisión externa: carreras, verificación y backups
import threading  # noqa: E402


class SlowApi(IgnoreApi):
    """El POST tarda: abre la ventana entre leer y escribir donde dos escritores se pisan."""

    def post(self, path, body=None, **p):
        import time as _t
        _t.sleep(0.15)
        return super().post(path, body, **p)


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_dos_escritores_no_se_pisan_las_reglas(self):
        api = SlowApi([])
        res = {}

        def go(rel, delay):
            import time as _t
            _t.sleep(delay)
            res[rel] = manage.apply_change(api, "home", rel, "ignore", state_dir=self.tmp, sleep=lambda s: None)

        a, b = threading.Thread(target=go, args=("A", 0)), threading.Thread(target=go, args=("B", 0.03))
        a.start(); b.start(); a.join(); b.join()
        self.assertEqual(sorted(manage.split_managed(api.lines)[0]), ["/A", "/B"])    # ninguna se perdió
        self.assertTrue(res["A"].ok and res["B"].ok)

    def test_si_otro_proceso_cambia_el_archivo_en_el_medio_no_se_afirma_exito(self):
        class Meddler(IgnoreApi):
            def post(self, path, body=None, **p):
                super().post(path, body, **p)
                self.lines = self.lines + ["//otro escritor"]       # alguien más escribe justo después

        r = manage.apply_change(Meddler([]), "home", "A", "ignore", state_dir=self.tmp, sleep=lambda s: None)
        self.assertFalse(r.ok)
        self.assertIn("otro proceso", r.note)

    def test_sin_confirmacion_de_syncthing_no_es_exito(self):
        class Mute(IgnoreApi):
            """Acepta el POST y después deja de responder: no hay con qué verificar."""
            muted = False

            def get(self, path, **p):
                if self.muted:
                    raise core.ApiError(path)
                return super().get(path, **p)

            def post(self, path, body=None, **p):
                r = super().post(path, body, **p)
                self.muted = True
                return r

        r = manage.apply_change(Mute([]), "home", "A", "ignore", state_dir=self.tmp, sleep=lambda s: None)
        self.assertIsNone(r.verified)
        self.assertFalse(r.ok)                       # antes: ok=True y la UI decía «verificado»
        self.assertIn("no pude confirmar", r.note)

    def test_indice_viejo_no_justifica_una_excepcion_de_mas(self):
        class StaleIndex(IgnoreApi):
            """Syncthing todavía dice «ignorada» aunque la regla ya no está (índice sin reescanear)."""

            def get(self, path, **p):
                if path == "/rest/db/file":
                    return {"local": {"sequence": 5, "ignored": True}}
                return super().get(path, **p)

        api = StaleIndex([])
        manage.apply_change(api, "home", "X", "ignore", state_dir=self.tmp, sleep=lambda s: None)
        r = manage.apply_change(api, "home", "X", "unignore", state_dir=self.tmp, sleep=lambda s: None)
        self.assertEqual(api.lines, [])                           # sin «!/X» sobrante
        self.assertFalse(r.ok)                                    # y tampoco se afirma que Syncthing ya lo reflejó
        self.assertIs(r.verified, False)

    def test_dejar_de_ignorar_limpia_una_excepcion_vieja_del_panel(self):
        api = IgnoreApi([manage.MARK_BEGIN, "!/X", manage.MARK_END, "algo"])
        r = manage.apply_change(api, "home", "X", "unignore", state_dir=self.tmp, sleep=lambda s: None)
        self.assertEqual(api.lines, ["algo"])                     # el bloque queda vacío y se va
        self.assertTrue(r.ok)

    def test_backup_con_el_nombre_correcto_pero_que_el_panel_no_escribio(self):
        api = IgnoreApi(["a"])
        manage.apply_change(api, "home", "A", "ignore", state_dir=self.tmp, sleep=lambda s: None)
        forged = self.tmp / "backups" / "home-pc-a-19990101-000000.stignore"
        forged.write_text("/**\n")
        with self.assertRaises(ValueError) as cm:
            manage.restore_backup(api, "home", str(forged), state_dir=self.tmp)
        self.assertIn("registro", str(cm.exception))

    def test_backup_legitimo_modificado_despues_se_rechaza(self):
        api = IgnoreApi(["a"])
        r = manage.apply_change(api, "home", "A", "ignore", state_dir=self.tmp, sleep=lambda s: None)
        Path(r.backup).write_text("/**\n")           # alguien tocó el contenido
        with self.assertRaises(ValueError) as cm:
            manage.restore_backup(api, "home", r.backup, state_dir=self.tmp)
        self.assertIn("cambió", str(cm.exception))

    def test_restaurar_registra_el_estado_previo_y_sigue_funcionando(self):
        api = IgnoreApi(["a"])
        r = manage.apply_change(api, "home", "A", "ignore", state_dir=self.tmp, sleep=lambda s: None)
        n = manage.restore_backup(api, "home", r.backup, state_dir=self.tmp, clock=lambda: 1e9)
        self.assertEqual((n, api.lines), (1, ["a"]))
        pre = [e for e in (json.loads(x) for x in next(self.tmp.glob("cambios-*.jsonl")).read_text().splitlines())
               if e.get("action") == "pre-restore"]
        self.assertEqual(len(pre), 1)                  # lo previo a deshacer también es restaurable
        self.assertTrue(Path(pre[0]["backup"]).exists())

    def test_ajustes_backup_falsificado_se_rechaza(self):
        api = CfgApi()
        manage.apply_settings(api, "f", {"paused": True}, state_dir=self.tmp)
        forged = self.tmp / "backups" / "config-f-pc-a-19990101-000000.json"
        forged.write_text('{"id": "f", "path": "/"}')
        with self.assertRaises(ValueError):
            manage.restore_settings(api, "f", str(forged), state_dir=self.tmp)


# ---------------------------------------------------------------- fase 2: vínculos X -> Y
from wyss_sync import links  # noqa: E402


class LinkApi:
    """Syncthing simulado con carpetas, reglas por carpeta, defaults y alta/baja de carpetas."""

    def __init__(self, me="ME", folders=None):
        self.scans = []
        self.me = me
        self.folders = folders or [{"id": "home-saintwyss", "label": "Home", "path": "/h", "devices": []},
                                   {"id": "mnt-hdd-data", "label": "HDD Data", "path": "/d", "devices": []}]
        self.ign = {f["id"]: [] for f in self.folders}
        self.deleted, self.fail_post_folder = [], False

    def get(self, path, _timeout=None, **p):
        if path == "/rest/system/status":
            return {"myID": self.me}
        if path == "/rest/config/folders":
            return json.loads(json.dumps(self.folders))
        if path == "/rest/config/defaults/folder":
            return {"type": "sendreceive", "versioning": {"type": ""}, "paused": False}
        if path == "/rest/db/ignores":
            return {"ignore": list(self.ign[p["folder"]])}
        if path == "/rest/db/file":
            return {"local": {"sequence": 1, "ignored": manage.match_rule(self.ign[p["folder"]], p["file"])[1]}}
        raise AssertionError(path)

    def post(self, path, body=None, **p):
        if path == "/rest/db/ignores":
            self.ign[p["folder"]] = list(body["ignore"])
        elif path == "/rest/db/scan":
            self.scans.append((p.get("folder"), p.get("sub")))
        elif path == "/rest/config/folders":
            if self.fail_post_folder:
                raise core.ApiError("/rest/config/folders: HTTPError")
            self.folders.append(body)
            self.ign[body["id"]] = []
        return {}

    def _call(self, path, params, data, timeout=None, method=None):
        if method == "DELETE" and path.startswith("/rest/config/folders/"):
            fid = urllib_unquote(path.rsplit("/", 1)[-1])
            self.folders = [f for f in self.folders if f["id"] != fid]
            self.deleted.append(fid)
            return {}
        raise AssertionError((path, method))


def urllib_unquote(s):
    import urllib.parse
    return urllib.parse.unquote(s)


class LinkPathTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = str(self.root / "home")
        (self.root / "home" / "Proyectos" / "app").mkdir(parents=True)
        self.folders = [{"id": "home-saintwyss", "label": "Home", "path": self.home}]

    def v(self, p, **kw):
        return links.validate_path(p, self.folders, self.home, must_exist=kw.get("must_exist", True))

    def test_ruta_valida(self):
        self.assertEqual(self.v(self.home + "/Proyectos/app"), [])

    def test_rechaza_lo_peligroso(self):
        cases = {"": "falta", "relativa/x": "absoluta", self.home + "/a/../b": "normalizada",
                 "/etc/passwd": "solo se admiten", self.home: "raíz entera", self.home + "/.stfolder/x": "reservada",
                 self.home + "/a\nb": "control"}
        for path, frag in cases.items():
            out = self.v(path, must_exist=False)
            self.assertTrue(any(frag in b for b in out), (path, out))

    def test_enlace_simbolico_y_carpetas_de_syncthing(self):
        (self.root / "home" / "enlace").symlink_to(self.root / "home" / "Proyectos")
        self.assertTrue(any("enlace simbólico" in b for b in self.v(self.home + "/enlace")))
        # contiene a una carpeta de Syncthing / es su raíz
        folders = self.folders + [{"id": "otra", "label": "Otra", "path": self.home + "/Proyectos/app"}]
        out = links.validate_path(self.home + "/Proyectos", folders, self.home, must_exist=True)
        self.assertTrue(any("contiene a la carpeta" in b for b in out))
        out = links.validate_path(self.home + "/Proyectos/app", folders, self.home, must_exist=True)
        self.assertTrue(any("raíz de la carpeta" in b for b in out))
        # dentro de un vínculo existente
        folders = self.folders + [{"id": "wyss-app-1", "label": "app", "path": self.home + "/Proyectos/app"}]
        (self.root / "home" / "Proyectos" / "app" / "sub").mkdir()
        out = links.validate_path(self.home + "/Proyectos/app/sub", folders, self.home, must_exist=True)
        self.assertTrue(any("dentro del vínculo" in b for b in out))

    def test_containing_folder_elige_la_mas_especifica(self):
        fs = [{"id": "home", "path": "/home/u"}, {"id": "data", "path": "/home/u/Data"}]
        self.assertEqual(links.containing_folder("/home/u/Data/x/y", fs), ("data", "x/y"))
        self.assertEqual(links.containing_folder("/home/u/Docs", fs), ("home", "Docs"))
        self.assertIsNone(links.containing_folder("/mnt/otro", fs))

    def test_expand_y_ids(self):
        self.assertEqual(links.expand("~/x/y", "/home/u"), "/home/u/x/y")
        self.assertEqual(links.expand("/abs", "/home/u"), "/abs")
        self.assertTrue(links.new_id("/home/u/Mi App", "/r/y").startswith("wyss-mi-app-"))
        self.assertEqual(links.new_id("/a", "/b"), links.new_id("/a", "/b"))
        for bad in ("home-saintwyss", "mnt-hdd-data", "wyss-", "wyss-A", "wyss-../x", "wyss-" + "a" * 70, None):
            with self.assertRaises(ValueError):
                links.check_fid(bad)
        self.assertEqual(links.check_fid("wyss-app-0a1b2c"), "wyss-app-0a1b2c")


class LinkDiffTests(unittest.TestCase):
    def test_diff_cuenta_y_tolera_2_segundos(self):
        here = {"a": [10, 100], "b": [5, 100], "c": [7, 100], "d": [1, 50]}
        there = {"a": [10, 102], "b": [5, 200], "x": [3, 1], "d": [2, 50]}
        d = links.diff_indexes(here, there)
        self.assertEqual((d["same"]["n"], d["different"]["n"]), (1, 2))          # a igual (±2 s); b y d distintos
        self.assertEqual((d["only_here"]["n"], d["only_here"]["examples"]), (1, ["c"]))
        self.assertEqual((d["only_there"]["n"], d["only_there"]["bytes"]), (1, 3))
        self.assertEqual(d["different"]["same_size_other_time"], 1)

    def test_tree_index_no_sigue_enlaces_ni_entra_a_reservadas(self):
        r = Path(tempfile.mkdtemp())
        (r / "a").mkdir(); (r / ".stversions").mkdir()
        (r / "a" / "f.txt").write_bytes(b"xx")
        (r / ".stversions" / "v").write_bytes(b"y")
        (r / "enlace").symlink_to(r / "a")
        idx, trunc = links.tree_index(str(r))
        self.assertEqual((sorted(idx), trunc), (["a/f.txt", "enlace"], False))
        self.assertEqual(idx["a/f.txt"][0], 2)
        t = iter(range(100))
        self.assertTrue(links.tree_index(str(r), budget=0, clock=lambda: next(t))[1])


class LinkFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        (self.home / "Proyectos" / "app").mkdir(parents=True)
        (self.home / "Proyectos" / "app" / "a.txt").write_text("hola")
        self.api = LinkApi(folders=[{"id": "home-saintwyss", "label": "Home", "path": str(self.home), "devices": []},
                                    {"id": "mnt-hdd-data", "label": "HDD Data", "path": "/d", "devices": []}])
        self.local_folders = [{"id": f["id"], "path": f["path"], "label": f["label"]} for f in self.api.folders]

    def remote_info(self, exists=False, files=0, folder="home-saintwyss", rel="Respaldos/app", blockers=None):
        return {"path": "/home/u/Respaldos/app", "home": "/home/u", "blockers": blockers or [], "exists": exists,
                "folder": folder, "rel": rel, "files": files, "bytes": files * 10, "truncated": False}

    def plan(self, **kw):
        return links.build_plan(str(self.home / "Proyectos" / "app"), "~/Respaldos/app", self.local_folders,
                                str(self.home), self.remote_info(**kw))

    def test_plan_saca_origen_y_destino_de_sus_carpetas(self):
        p = self.plan()
        self.assertEqual(p.blockers, [])
        self.assertEqual([(r["folder"], r["rel"]) for r in p.rules],
                         [("home-saintwyss", "Proyectos/app"), ("home-saintwyss", "Respaldos/app")])
        self.assertFalse(p.y_has_data)
        self.assertTrue(any("se va a crear" in w for w in p.warnings))
        self.assertTrue(p.fid.startswith("wyss-app-"))

    def test_plan_con_destino_con_datos_pide_la_diferencia(self):
        info = self.remote_info(exists=True, files=3)
        info["index"] = {"a.txt": [4, 0], "otro.txt": [2, 0]}
        p = links.build_plan(str(self.home / "Proyectos" / "app"), "~/Respaldos/app", self.local_folders,
                             str(self.home), info)
        self.assertTrue(p.y_has_data)
        self.assertEqual(p.diff["only_there"]["n"], 1)
        self.assertEqual(p.diff["different"]["n"], 1)            # a.txt: 4 bytes acá, 4 allá pero mtime distinto
        self.assertNotIn("index", p.x)

    def test_misma_ruta_en_las_dos_pc_es_un_bloqueo(self):
        x = str(self.home / "Proyectos" / "app")
        info = self.remote_info()
        info["path"] = x
        p = links.build_plan(x, x, self.local_folders, str(self.home), info)
        self.assertTrue(any("misma ruta" in b for b in p.blockers))

    def test_plan_propaga_bloqueos_y_no_crea_con_ellos(self):
        p = self.plan(blockers=["es la raíz de la carpeta de Syncthing «Home»"])
        self.assertTrue(any(b.startswith("destino:") for b in p.blockers))
        with self.assertRaises(ValueError):
            links.create_link(self.api, p, self.local_folders, lambda s, d: {"ok": True}, "PEER", state_dir=self.tmp)
        self.assertEqual(len(self.api.folders), 2)               # no tocó nada

    def test_apply_side_aplica_en_orden_y_crea_la_carpeta(self):
        dest = self.home / "Respaldos" / "app"
        out = links.apply_side(self.api, fid="wyss-app-1", label="app", path=str(dest), peer="PEER",
                               rules=[{"folder": "home-saintwyss", "rel": "Respaldos/app"}], create_dir=True,
                               folders=self.local_folders, home=str(self.home))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["steps"], ["ignore", "mkdir", "folder"])
        self.assertTrue(dest.is_dir())
        new = self.api.folders[-1]
        self.assertEqual((new["id"], new["path"], new["type"]), ("wyss-app-1", str(dest), "sendreceive"))
        self.assertEqual([d["deviceID"] for d in new["devices"]], ["ME", "PEER"])
        self.assertIn("/Respaldos/app", manage.split_managed(self.api.ign["home-saintwyss"])[0])

    def test_si_falla_el_alta_se_deshace_todo_lo_anterior(self):
        dest = self.home / "Respaldos" / "app"
        self.api.fail_post_folder = True
        out = links.apply_side(self.api, fid="wyss-app-1", label="app", path=str(dest), peer="PEER",
                               rules=[{"folder": "home-saintwyss", "rel": "Respaldos/app"}], create_dir=True,
                               folders=self.local_folders, home=str(self.home))
        self.assertFalse(out["ok"])
        self.assertEqual(out["undone"], ["mkdir", "ignore"])
        self.assertFalse(dest.exists())                           # la carpeta que se creó, se quitó (vacía)
        self.assertEqual(manage.split_managed(self.api.ign["home-saintwyss"])[0], [])
        self.assertEqual(len(self.api.folders), 2)

    def test_deshacer_nunca_borra_una_carpeta_con_contenido(self):
        dest = self.home / "Respaldos" / "app"
        (dest).mkdir(parents=True)
        (dest / "mio.txt").write_text("importante")
        links.undo_steps(self.api, [("mkdir", str(dest))])
        self.assertTrue((dest / "mio.txt").exists())              # rmdir falla en silencio si no está vacía

    def test_crear_vinculo_completo_y_quitarlo(self):
        calls = []

        def remote(sub, payload):
            calls.append((sub, payload.get("fid")))
            return {"ok": True, "result": {"ok": True, "steps": ["ignore", "mkdir", "folder"]}}

        p = self.plan()
        res = links.create_link(self.api, p, self.local_folders, remote, "PEER", state_dir=self.tmp, home=str(self.home))
        self.assertTrue(res["ok"], res)
        self.assertEqual(calls[0][0], "link-apply")               # primero la otra PC
        self.assertIn(p.fid, links.load_links(self.tmp))
        self.assertEqual(self.api.folders[-1]["path"], str(self.home / "Proyectos" / "app"))
        out = links.drop_link(self.api, p.fid, remote, state_dir=self.tmp)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.api.deleted, [p.fid])
        self.assertEqual(manage.split_managed(self.api.ign["home-saintwyss"])[0], [])   # reglas deshechas
        self.assertNotIn(p.fid, links.load_links(self.tmp))
        self.assertTrue((self.home / "Proyectos" / "app" / "a.txt").exists())          # los archivos, intactos

    def test_si_falla_el_lado_local_la_otra_pc_vuelve_atras(self):
        calls = []

        def remote(sub, payload):
            calls.append(sub)
            return {"ok": True, "result": {"ok": True}}

        self.api.fail_post_folder = True
        res = links.create_link(self.api, self.plan(), self.local_folders, remote, "PEER", state_dir=self.tmp,
                                home=str(self.home))
        self.assertFalse(res["ok"])
        self.assertEqual((res["stage"], calls), ("local", ["link-apply", "link-remove"]))
        self.assertEqual(links.load_links(self.tmp), {})

    def test_si_falla_la_otra_pc_no_se_toca_esta(self):
        res = links.create_link(self.api, self.plan(), self.local_folders,
                                lambda s, d: {"ok": True, "result": {"ok": False, "error": "sin permiso"}}, "PEER",
                                state_dir=self.tmp, home=str(self.home))
        self.assertEqual((res["ok"], res["stage"]), (False, "remoto"))
        self.assertEqual(len(self.api.folders), 2)
        self.assertEqual(manage.split_managed(self.api.ign["home-saintwyss"])[0], [])

    def test_quitar_solo_acepta_ids_de_vinculo_y_conocidos(self):
        for bad in ("home-saintwyss", "mnt-hdd-data"):
            with self.assertRaises(ValueError):
                links.remove_side(self.api, fid=bad, rules=[])
        with self.assertRaises(ValueError):
            links.drop_link(self.api, "wyss-no-existe", lambda s, d: None, state_dir=self.tmp)
        self.assertEqual(self.api.deleted, [])
        out = []
        rc = manage.cli(["link-remove", manage.b64({"fid": "mnt-hdd-data", "rules": []})], api=self.api, out=out.append)
        self.assertNotEqual(rc, 0)                                # la CLI remota tampoco puede borrarla
        self.assertFalse(json.loads(out[0])["ok"])
        self.assertEqual(self.api.deleted, [])


class LinkUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def ctx(self):
        from wyss_sync.pages import Context
        c = Context(object())
        c.update({"local": {"name": "A"}, "remote": {"name": "B", "online": True, "id": "R", "address": "1.2.3.4"},
                  "folders": []})
        return c

    def mkplan(self, **kw):
        base = links.Plan("wyss-app-1", "app", {"path": "/h/app", "folder": "home", "files": 3, "bytes": 30},
                          {"path": "/r/app", "exists": True, "files": 2, "bytes": 20, "folder": "home", "rel": "app"})
        base.rules = [{"folder": "home", "rel": "app", "why": "origen"}]
        for k, v in kw.items():
            setattr(base, k, v)
        return base

    def test_resumen_muestra_pasos_y_diferencia_sin_inventar(self):
        d = {"only_here": {"n": 1, "bytes": 5, "examples": ["a"]}, "only_there": {"n": 0, "bytes": 0, "examples": []},
             "same": {"n": 2, "bytes": 9}, "different": {"n": 1, "bytes": 3, "examples": ["<b>x</b>"], "same_size_other_time": 0}}
        html_ = links_plan_html(self.mkplan(diff=d, y_has_data=True))
        for frag in ("Se va a hacer", "bidireccional", "solo acá", "&lt;b&gt;x&lt;/b&gt;", "No se borra ningún archivo"):
            self.assertIn(frag, html_)
        self.assertNotIn("<b>x</b>", html_)                       # los nombres de archivo se escapan

    def test_con_bloqueos_no_hay_pasos_y_se_ven_los_motivos(self):
        html_ = links_plan_html(self.mkplan(blockers=["destino: es la raíz de la carpeta de Syncthing «Home»"]))
        self.assertIn("No se puede crear", html_)
        self.assertNotIn("Se va a hacer", html_)

    def test_crear_exige_confirmar_la_fusion_si_el_destino_tiene_datos(self):
        from wyss_sync import pages
        d = pages.LinkDialog(None, self.ctx())
        d.plan = self.mkplan(y_has_data=True)
        d._gate()
        self.assertFalse(d.create.isEnabled())
        d.ack.setChecked(True)
        self.assertTrue(d.create.isEnabled())
        d.plan = self.mkplan(y_has_data=False)
        d.ack.setChecked(False)
        d._gate()
        self.assertTrue(d.create.isEnabled())                     # destino vacío o nuevo: no hace falta la casilla
        d.plan = self.mkplan(blockers=["x"], y_has_data=False)
        d._gate()
        self.assertFalse(d.create.isEnabled())

    def test_cambiar_una_ruta_invalida_el_plan(self):
        from wyss_sync import pages
        d = pages.LinkDialog(None, self.ctx())
        d.x.setText("/h/app"); d.y.setText("~/r")
        d.plan, d._for = self.mkplan(), ("/h/app", "~/r")
        d.create.setEnabled(True)
        d.y.setText("~/otra")
        self.assertIsNone(d.plan)
        self.assertFalse(d.create.isEnabled())

    def test_la_pagina_lista_los_vinculos_registrados(self):
        from wyss_sync import pages
        tmp = Path(tempfile.mkdtemp())
        links.save_link({"id": "wyss-app-1", "label": "app", "x": "/h/app", "y": "/r/app", "rules": []}, tmp)
        real = links.load_links
        links.load_links = lambda state_dir=tmp: real(tmp)
        try:
            ctx = self.ctx()
            ctx.update({**ctx.model, "folders": [{"id": "wyss-app-1", "state": "syncing", "percent": 42.5,
                                                  "sync_bytes": 2 * 1024 ** 3}]})
            p = pages.LinksPage(ctx)
            p.load()
        finally:
            links.load_links = real
        it = p.table.topLevelItem(0)
        self.assertEqual((it.text(0), it.text(3), it.text(4)), ("app", "sincronizando 42 %", "2,0 GB"))


def links_plan_html(plan):
    from wyss_sync import pages
    return pages.plan_html(plan)


class Decisiones7Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "descartados"
        self.d.mkdir()

    def mk(self, name, size=10, age_days=0):
        p = self.d / name
        p.write_bytes(b"x" * size)
        return p

    def stamp(self, days_ago):
        return time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - days_ago * 86400))

    def test_rutas_criticas(self):
        for rel in (".config", ".ssh", ".claude", "Data", ".local/share", "Documentos/"):
            self.assertTrue(manage.is_critical(rel), rel)
        for rel in (".config/VirtualBox", ".cache", "Proyectos", ".local/share/rtk", "Data/sub"):
            self.assertFalse(manage.is_critical(rel), rel)

    def test_purga_solo_lo_viejo_y_solo_lo_del_panel(self):
        viejo = self.mk(f"{self.stamp(40)}-conflicto-a.md", 100)
        nuevo = self.mk(f"{self.stamp(5)}-conflicto-b.md", 50)
        ajeno = self.mk("notas-mias.txt", 70)                       # no lo escribió el panel: no se toca
        out = manage.purge_descartados(self.tmp, days=30)
        self.assertEqual((out["removed"], out["bytes"]), (1, 100))
        self.assertFalse(viejo.exists())
        self.assertTrue(nuevo.exists() and ajeno.exists())

    def test_vaciar_todo_borra_lo_del_panel_pero_nunca_nombres_ajenos(self):
        a = self.mk(f"{self.stamp(1)}-original-a.md")
        b = self.mk(f"{self.stamp(0)}-conflicto-b.md")
        c = self.mk("otra-cosa.bin")
        out = manage.purge_descartados(self.tmp, days=0)
        self.assertEqual(out["removed"], 2)
        self.assertTrue(c.exists() and not a.exists() and not b.exists())

    def test_la_purga_no_sigue_enlaces_ni_entra_a_subcarpetas(self):
        real = self.tmp / "importante.txt"
        real.write_text("no tocar")
        (self.d / f"{self.stamp(60)}-conflicto-enlace.md").symlink_to(real)
        sub = self.d / f"{self.stamp(60)}-conflicto-dir"
        sub.mkdir()
        (sub / "x").write_text("x")
        out = manage.purge_descartados(self.tmp, days=30)
        self.assertEqual(out["removed"], 0)
        self.assertTrue(real.exists() and (sub / "x").exists())

    def test_resumen_cuenta_lo_vencido(self):
        self.mk(f"{self.stamp(40)}-conflicto-a.md", 100)
        self.mk(f"{self.stamp(2)}-conflicto-b.md", 20)
        sm = manage.descartados_summary(self.tmp)
        self.assertEqual((sm["files"], sm["bytes"], sm["due_files"], sm["due_bytes"]), (2, 120, 1, 100))

    def test_el_historiador_purga_una_vez_por_dia(self):
        viejo = self.mk(f"{self.stamp(40)}-conflicto-a.md")

        class Quiet:
            def get(self, path, _timeout=None, **p):
                return {"startTime": "t"} if path == "/rest/system/status" else []

        n = {"i": 0}

        def stop():
            n["i"] += 1
            return n["i"] > 3

        manage.historian(lambda: Quiet(), self.tmp, wait=0, stop=stop, sleep=lambda s: None)
        self.assertFalse(viejo.exists())


class CriticalDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def row(self, rel):
        return manage.Row(rel.split("/")[-1], rel, "dir", 0.0, "sync", "sync")

    def test_ignorar_una_ruta_critica_exige_la_casilla(self):
        from wyss_sync import detail
        d = detail.ChangeDialog(None, "ignore", [self.row(".config")], "A", "B")
        self.assertFalse(d.ok_btn.isEnabled())
        d.ack.setChecked(True)
        self.assertTrue(d.ok_btn.isEnabled())

    def test_una_ruta_comun_o_dejar_de_ignorar_no_la_piden(self):
        from wyss_sync import detail
        d = detail.ChangeDialog(None, "ignore", [self.row(".config/VirtualBox")], "A", "B")
        self.assertTrue(d.ok_btn.isEnabled())
        self.assertIsNone(d.ack)
        d = detail.ChangeDialog(None, "unignore", [self.row(".config")], "A", "B")
        self.assertTrue(d.ok_btn.isEnabled())


# ---------------------------------------------------------------- segunda revisión externa
class StaleIgnoreApi(LinkApi):
    """Syncthing que nunca refleja el cambio de reglas: el índice dice siempre lo mismo."""

    def __init__(self, *a, ignored=False, **k):
        super().__init__(*a, **k)
        self.index_ignored = ignored

    def get(self, path, _timeout=None, **p):
        if path == "/rest/db/file":
            return {"local": {"sequence": 1, "ignored": self.index_ignored}}
        return super().get(path, _timeout, **p)


class SecondReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        (self.home / "Proyectos" / "app").mkdir(parents=True)
        self.folders_cfg = [{"id": "home-saintwyss", "label": "Home", "path": str(self.home), "devices": []},
                            {"id": "mnt-hdd-data", "label": "HDD Data", "path": "/d", "devices": []}]
        self.lf = [{"id": f["id"], "path": f["path"], "label": f["label"]} for f in self.folders_cfg]

    def api(self, cls=LinkApi, **kw):
        return cls(folders=json.loads(json.dumps(self.folders_cfg)), **kw)

    def plan(self):
        info = {"path": "/home/u/Respaldos/app", "home": "/home/u", "blockers": [], "exists": False,
                "folder": "home-saintwyss", "rel": "Respaldos/app", "files": 0, "bytes": 0, "truncated": False}
        return links.build_plan(str(self.home / "Proyectos" / "app"), "~/Respaldos/app", self.lf, str(self.home), info)

    # --- 1. purga a través de un enlace
    def test_purga_no_sigue_un_descartados_que_es_un_enlace(self):
        afuera = Path(tempfile.mkdtemp())
        old = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 90 * 86400))
        victima = afuera / f"{old}-conflicto-importante.txt"
        victima.write_text("no se toca")
        (self.tmp / "descartados").symlink_to(afuera)
        self.assertEqual(manage.purge_descartados(self.tmp, days=0)["removed"], 0)
        self.assertEqual(manage.purge_descartados(self.tmp, days=30)["removed"], 0)
        self.assertEqual(manage.descartados_summary(self.tmp)["files"], 0)
        self.assertTrue(victima.exists())

    # --- 2. ruta inexistente bajo un enlace
    def test_destino_inexistente_bajo_un_enlace_se_bloquea_y_no_se_crea(self):
        real = self.home / "real"
        real.mkdir()
        (self.home / "destino").symlink_to(real)
        target = str(self.home / "destino" / "nueva" / "carpeta")
        out = links.validate_path(target, self.lf, str(self.home), must_exist=False)
        self.assertTrue(any("enlace simbólico" in b for b in out), out)
        with self.assertRaises(ValueError):
            links.safe_makedirs(target, str(self.home))
        self.assertEqual(list(real.iterdir()), [])                # nada se creó detrás del enlace

    def test_safe_makedirs_crea_anidado_y_devuelve_lo_creado(self):
        dest = str(self.home / "a" / "b" / "c")
        created = links.safe_makedirs(dest, str(self.home))
        self.assertEqual([os.path.basename(c) for c in created], ["a", "b", "c"])
        self.assertEqual(links.safe_makedirs(dest, str(self.home)), [])     # ya existe: no crea ni toca nada

    # --- 3. el ignorado tiene que estar confirmado antes de dar de alta la carpeta
    def test_si_el_ignorado_no_se_confirma_no_se_da_de_alta_la_carpeta(self):
        api = self.api(StaleIgnoreApi, ignored=False)
        dest = self.home / "Respaldos" / "app"
        out = links.apply_side(api, fid="wyss-app-1", label="app", path=str(dest), peer="P",
                               rules=[{"folder": "home-saintwyss", "rel": "Respaldos/app"}], create_dir=True,
                               folders=self.lf, home=str(self.home), verify_tries=2, sleep=lambda s: None)
        self.assertFalse(out["ok"])
        self.assertIn("confirmar", out["error"])
        self.assertEqual(len(api.folders), 2)                                # no se agregó la carpeta
        self.assertEqual(manage.split_managed(api.ign["home-saintwyss"])[0], [])   # regla deshecha
        self.assertFalse(dest.exists())

    # --- 7. el rollback no toca lo que ya existía
    def test_el_rollback_no_quita_una_regla_que_ya_existia(self):
        api = self.api()
        api.ign["home-saintwyss"] = [manage.MARK_BEGIN, "/Respaldos/app", manage.MARK_END]
        api.fail_post_folder = True
        out = links.apply_side(api, fid="wyss-app-1", label="app", path=str(self.home / "Respaldos" / "app"),
                               peer="P", rules=[{"folder": "home-saintwyss", "rel": "Respaldos/app"}],
                               create_dir=True, folders=self.lf, home=str(self.home))
        self.assertFalse(out["ok"])
        self.assertIn("/Respaldos/app", manage.split_managed(api.ign["home-saintwyss"])[0])   # la previa sigue

    # --- 4. quitar: el resultado de cada regla cuenta, y el registro no se olvida si quedó algo
    def _created(self, api):
        remote = lambda s, d: {"ok": True, "result": {"ok": True, "added": d.get("rules", [])}}   # noqa: E731
        res = links.create_link(api, self.plan(), self.lf, remote, "P", state_dir=self.tmp, home=str(self.home))
        self.assertTrue(res["ok"], res)
        return res["id"], remote

    def test_quitar_con_una_regla_sin_confirmar_conserva_el_registro_y_se_puede_repetir(self):
        api = self.api()
        fid, remote = self._created(api)
        stale = self.api(StaleIgnoreApi, ignored=True)              # Syncthing sigue diciendo «ignorada»
        stale.folders, stale.ign = api.folders, api.ign
        out = links.drop_link(stale, fid, remote, state_dir=self.tmp, sleep=lambda s: None)
        self.assertFalse(out["ok"])
        self.assertIn(fid, links.load_links(self.tmp))              # no se olvidó
        self.assertEqual(links.load_links(self.tmp)[fid]["state"], "quitando-incompleto")
        out2 = links.drop_link(api, fid, remote, state_dir=self.tmp)        # ahora responde bien: se repite
        self.assertTrue(out2["ok"], out2)
        self.assertNotIn(fid, links.load_links(self.tmp))

    def test_quitar_con_errores_en_la_otra_pc_no_se_da_por_hecho(self):
        api = self.api()
        fid, _ = self._created(api)
        bad = lambda s, d: {"ok": True, "result": {"ok": False, "errors": ["x: no confirmado"]}}   # noqa: E731
        out = links.drop_link(api, fid, bad, state_dir=self.tmp)
        self.assertFalse(out["ok"])
        self.assertIn(fid, links.load_links(self.tmp))

    # --- 5. la intención queda registrada antes de cambiar nada
    def test_el_registro_existe_antes_de_tocar_la_otra_pc(self):
        api = self.api()
        seen = {}

        def remote(sub, payload):
            seen["state"] = links.load_links(self.tmp).get(payload["fid"], {}).get("state")
            return {"ok": True, "result": {"ok": True, "added": payload["rules"]}}

        res = links.create_link(api, self.plan(), self.lf, remote, "P", state_dir=self.tmp, home=str(self.home))
        self.assertTrue(res["ok"])
        self.assertEqual(seen["state"], "creando")
        self.assertEqual(links.load_links(self.tmp)[res["id"]]["state"], "activo")

    def test_si_no_se_puede_registrar_no_se_cambia_nada(self):
        api, calls = self.api(), []
        bad_dir = self.tmp / "es-un-archivo"
        bad_dir.write_text("x")                                     # state_dir imposible de crear
        with self.assertRaises(OSError):
            links.create_link(api, self.plan(), self.lf, lambda s, d: calls.append(s), "P", state_dir=bad_dir / "x",
                              home=str(self.home))
        self.assertEqual((calls, len(api.folders)), ([], 2))

    def test_una_excepcion_en_la_otra_pc_no_deja_registro_colgado(self):
        api = self.api()

        def boom(sub, payload):
            raise OSError("ssh murió")

        res = links.create_link(api, self.plan(), self.lf, boom, "P", state_dir=self.tmp, home=str(self.home))
        self.assertFalse(res["ok"])
        self.assertEqual(links.load_links(self.tmp), {})            # nada se aplicó: no hay qué quitar

    def test_un_id_forjado_no_pasa_por_create_link(self):
        p = self.plan()
        p.fid = "home-saintwyss"
        with self.assertRaises(ValueError):
            links.create_link(self.api(), p, self.lf, lambda s, d: None, "P", state_dir=self.tmp)

    # --- 6. la comparación y los enlaces simbólicos
    def test_dos_enlaces_con_distinto_destino_no_son_iguales(self):
        d = links.diff_indexes({"l": [0, 10, "a"], "m": [0, 10, "z"]}, {"l": [0, 10, "b"], "m": [0, 99, "z"]})
        self.assertEqual((d["same"]["n"], d["different"]["n"]), (1, 1))     # m: mismo destino = igual; l: no
        self.assertEqual(d["different"]["examples"], ["l"])                 # el viejo daba same=2 y different=0

    def test_el_indice_trae_el_destino_del_enlace(self):
        r = Path(tempfile.mkdtemp())
        (r / "a.txt").write_text("x")
        (r / "l").symlink_to("a.txt")
        idx, _ = links.tree_index(str(r))
        self.assertEqual(idx["l"][2], "a.txt")
        self.assertEqual(len(idx["a.txt"]), 2)


class VigilanteTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.api = LinkApi(folders=[{"id": "home-saintwyss", "label": "Home", "path": str(self.home), "devices": []}])
        self.lf = [{"id": "home-saintwyss", "path": str(self.home), "label": "Home"}]

    def test_el_vinculo_reescanea_seguido_y_al_crearse(self):
        out = links.apply_side(self.api, fid="wyss-app-1", label="app", path=str(self.home / "Respaldos" / "app"),
                               peer="P", rules=[{"folder": "home-saintwyss", "rel": "Respaldos/app"}],
                               create_dir=True, folders=self.lf, home=str(self.home), sleep=lambda s: None)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.api.folders[-1]["rescanIntervalS"], links.LINK_RESCAN_S)   # no 86400
        self.assertLessEqual(links.LINK_RESCAN_S, 900)
        self.assertIn(("wyss-app-1", None), self.api.scans)                               # escaneo inmediato

    def test_rescan_solo_acepta_ids_de_vinculo(self):
        with self.assertRaises(ValueError):
            links.rescan_link(self.api, "home-saintwyss", lambda s, d: None)
        r = links.rescan_link(self.api, "wyss-app-1", lambda s, d: {"ok": True, "result": {"ok": True}})
        self.assertEqual(r, {"local": True, "remote": True})
        out = []
        rc = manage.cli(["link-scan", manage.b64({"fid": "mnt-hdd-data"})], api=self.api, out=out.append)
        self.assertNotEqual(rc, 0)
