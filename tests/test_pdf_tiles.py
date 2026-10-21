"""Fundo do PDF: enquadramento real e volta ao mapa anterior se o tile falhar."""

import unittest
from io import BytesIO

from PIL import Image

from inlog_agent import live_pdf, pdf_tiles
from inlog_agent.mapa_oficial import caminho_gpkg


def _setor():
    from inlog_agent.live_mapa import _poligonos

    return next(item for item in _poligonos() if item.get("aneis"))


@unittest.skipUnless(caminho_gpkg(), "GPKG oficial ausente")
class PdfTiles(unittest.TestCase):
    def test_rota_oficial_ocupa_o_quadro(self):
        setor = _setor()
        anel = [(float(par[1]), float(par[0])) for par in setor["aneis"][0]]
        vista = pdf_tiles.enquadrar(anel, 1600, 1120)
        self.assertIsNotNone(vista)
        xs, ys = zip(*(pdf_tiles.projetar(vista, lon, lat) for lon, lat in anel))
        altura_util = 1120 - 46
        largura = (max(xs) - min(xs)) / 1600
        altura = (max(ys) - min(ys)) / altura_util
        self.assertGreaterEqual(max(largura, altura), 0.7)
        self.assertLessEqual(len(vista["tiles"]), 36)

    def test_falha_de_tile_reusa_o_mapa_anterior(self):
        setor = _setor()
        anel = [(float(par[1]), float(par[0])) for par in setor["aneis"][0]]
        original = live_pdf.obter_tiles
        live_pdf.obter_tiles = lambda _chaves: None
        try:
            antes = live_pdf.mapas_fallback
            blob, itens = live_pdf._mapa(
                {setor["codigo"]: [anel]},
                [],
                [{"codigo": setor["codigo"], "grupo": "concluida"}],
                largura=480,
                altura_px=360,
                individual=True,
            )
        finally:
            live_pdf.obter_tiles = original
        simples = live_pdf._mapa_simples(
            {setor["codigo"]: [anel]},
            [],
            [{"codigo": setor["codigo"], "grupo": "concluida"}],
            largura=480,
            altura_px=360,
            individual=True,
        )
        self.assertEqual(blob, simples)
        self.assertEqual(live_pdf.mapas_fallback, antes + 1)
        self.assertNotIn(("area", "Área oficial"), itens)
        self.assertNotIn(("gps", "GPS"), itens)
        self.assertEqual(itens, [])
        self.assertTrue(blob.startswith(b"\xff\xd8"))
        Image.open(BytesIO(blob)).verify()

    def test_cache_de_ram_tem_teto(self):
        try:
            for indice in range(50):
                pdf_tiles._ram_por((1, indice, 0), b"x" * 32)
            self.assertLessEqual(pdf_tiles.ram_tiles(), 32)
        finally:
            with pdf_tiles._TRAVA:
                pdf_tiles._RAM.clear()
