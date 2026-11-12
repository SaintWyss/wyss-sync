"""Pruebas de sync_status.py. Correr: python3 -B -m unittest test_sync_status"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import sync_status as st


class RulesDiffTests(unittest.TestCase):
    def test_ignora_comentarios_orden_y_vacias(self):
        a = "// c\n.cache\n\n/Data\n"
        b = ".cache\n// otro comentario\n/Data\n\n"
        self.assertEqual(st.rules_diff(a, b), ([], []))     # el md5 de estos dos archivos difiere; las reglas no

    def test_dice_de_que_lado_esta_cada_una(self):
        here, there = st.rules_diff(".a\n.b\n", ".b\n.c\n")
        self.assertEqual((here, there), ([".a"], [".c"]))


class ConflictTests(unittest.TestCase):
    def test_archivados_no_cuentan_como_sin_resolver(self):
        rels = ["Home/.config/a.sync-conflict-20260918-213613-ABCDEFG",
                "Home/.local/state/u7/state-sync-conflicts-20260924/x.sync-conflict-20260918-213613-ABCDEFG",
                "HDD Data/60_Ops/maintenance/sync-repair/conflicts-resolved/y.sync-conflict-20260921-143327-ABCDEFG.desktop"]
        b = st.conflict_breakdown(rels)
        self.assertEqual((b["active"], b["archived"]), (1, 2))
        self.assertEqual(b["sources"], [("Home/.config", 1)])

    def test_agrupa_por_origen_y_ordena(self):
        rels = [f"Home/.config/VirtualBox/f{i}.sync-conflict-20260918-213613-ABCDEFG.log" for i in range(3)] + \
               ["Home/.local/share/rtk/h.sync-conflict-20260918-213613-ABCDEFG.db"]
        b = st.conflict_breakdown(rels)
        self.assertEqual(b["sources"][0], ("Home/.config", 3))
        self.assertEqual(b["sources_total"], 2)

    def test_la_papelera_del_panel_no_cuenta(self):
        self.assertTrue(st.is_archived("Home/.local/state/wyss-sync/descartados/2026-conflicto-x.sync-conflict-20260918-213613-ABCDEFG.md"))

    def test_el_nombre_de_una_carpeta_no_es_un_conflicto(self):
        # el contador viejo contaba cualquier ruta con 'sync-conflict' en el nombre
        self.assertTrue(st.is_archived("a/state-sync-conflicts-20260924/x"))
        self.assertFalse(st.is_archived("a/b/c.sync-conflict-20260918-213613-ABCDEFG.md"))


class PackagesTests(unittest.TestCase):
    def test_filtra_con_apps_ignore(self):
        ign = [re.compile(r"^nvidia"), re.compile(r"-ucode$")]
        here, there = st.pkg_diff({"a", "nvidia-utils", "amd-ucode", "b"}, {"b", "c", "intel-ucode"}, ign)
        self.assertEqual((here, there), (["a"], ["c"]))

    def test_read_names_y_patterns(self):
        d = Path(tempfile.mkdtemp())
        (d / "w.txt").write_text("# comentario\nobsidian\nvesktop   # cliente discord\n\n")
        (d / "i.txt").write_text("# hardware\n^nvidia\n\n-ucode$\n")
        self.assertEqual(st.read_names(d / "w.txt"), ["obsidian", "vesktop"])
        self.assertEqual(len(st.read_patterns(d / "i.txt")), 2)
        self.assertEqual(st.read_names(d / "no-existe"), [])

    def test_peer_aliases_are_optional_and_reject_paths(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(st.configured_peers(), ())
        with patch.dict(os.environ, {"WYSS_SYNC_PEER": "pc-b", "WYSS_SYNC_PEER_FALLBACK": "192.0.2.10"}, clear=True):
            self.assertEqual(st.configured_peers(), ("pc-b", "192.0.2.10"))
        with patch.dict(os.environ, {"WYSS_SYNC_PEER": "../../otro"}, clear=True):
            with self.assertRaises(ValueError):
                st.configured_peers()


class RenderTests(unittest.TestCase):
    BASE = {"host": "pc-a", "other_name": "pc-b", "other_online": True,
            "apps": {"wishlist_total": 7, "missing": [], "only_here": ["x"], "only_there": [], "source": "en vivo"},
            "files": {"syncthing": True, "conflicts": 2, "conflicts_archived": 3, "conflict_sources": [["Home/.config", 2]],
                      "conflict_age_s": {"Home": 5, "HDD Data": None},
                      "folders": [{"id": "h", "label": "Home", "state": "syncing", "percent": 95.0, "need_items": 2,
                                   "need_bytes": 70000, "sync_bytes": 6_300_000_000, "sync_files": 10449}]},
            "settings": {"rules": 274, "stignore": "diff", "only_here": [".a"], "only_there": []}}

    def test_texto_muestra_lo_que_el_script_viejo_no_veia(self):
        out = st.render(self.BASE)
        for frag in ("Home: sincronizando 95 %", "sincroniza 5,9 GB", "faltan", "2 conflictos sin resolver", "+3 archivados",
                     "Home/.config", "HDD Data: sin recorrer", "solo acá:  .a", "solo acá (1):"):
            self.assertIn(frag, out)

    def test_json_es_serializable_y_conserva_las_claves_de_la_gui_vieja(self):
        data = json.loads(json.dumps(self.BASE))
        for k in ("host", "other_name", "other_online"):
            self.assertIn(k, data)
        self.assertEqual(set(data["apps"]) >= {"wishlist_total", "missing"}, True)
        self.assertEqual(set(data["files"]) >= {"syncthing", "conflicts"}, True)
        self.assertEqual(set(data["settings"]) >= {"rules", "stignore"}, True)


class ConflictCacheTests(unittest.TestCase):
    def test_home_se_recorre_y_data_sin_cache_queda_sin_afirmar(self):
        root_home, root_data = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (root_home / "d").mkdir()
        (root_home / "d" / "n.sync-conflict-20260918-213613-ABCDEFG.md").write_text("x")
        (root_data / "m.sync-conflict-20260918-213613-ABCDEFG.md").write_text("x")

        class Api:
            def get(self, path, _timeout=None, **p):
                return {"ignore": []}

        st.CACHE = Path(tempfile.mkdtemp()) / "c.json"
        real_home = st.HOME
        st.HOME = root_home
        try:
            folders = [{"id": "h", "label": "Home", "local_path": str(root_home)},
                       {"id": "d", "label": "HDD Data", "local_path": str(root_data)}]
            b, ages = st.conflicts(Api(), folders, deep=False)
            self.assertEqual(b["active"], 1)                       # solo el de Home
            self.assertIsNone(ages["HDD Data"])                    # nunca recorrido: no se dice "0 conflictos"
            b2, ages2 = st.conflicts(Api(), folders, deep=True)
            self.assertEqual(b2["active"], 2)
            self.assertIsNotNone(ages2["HDD Data"])
        finally:
            st.HOME = real_home


if __name__ == "__main__":
    unittest.main()
