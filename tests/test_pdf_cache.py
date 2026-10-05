import json
import tempfile
import unittest
from pathlib import Path

from inlog_agent.pdf_cache import (
    concorrencia,
    digest,
    espelhar_turno,
    gravar_cache,
    pdf_inteiro_valido,
    resolver_trabalho,
)


class CachePdf(unittest.TestCase):
    def test_hash_muda_quando_o_tracado_muda(self):
        base = {"rota": "11AD", "trechos": [[-16.8, -49.2], [-16.81, -49.21]]}
        outro = {"rota": "11AD", "trechos": [[-16.8, -49.2], [-16.82, -49.22]]}
        self.assertEqual(digest(base), digest(json.loads(json.dumps(base))))
        self.assertNotEqual(digest(base), digest(outro))

    def test_noturno_nao_apaga_o_diurno(self):
        with tempfile.TemporaryDirectory() as tmp:
            dia = Path(tmp)
            self._escrever(dia, "diurno", "mapa-dia")
            espelhar_turno(dia, "diurno")
            self._escrever(dia, "noturno", "mapa-noite")
            espelhar_turno(dia, "noturno")
            diurno = resolver_trabalho(dia, "diurno")
            noturno = resolver_trabalho(dia, "noturno")
            self.assertIn("mapa-dia", (diurno / "mapa.html").read_text(encoding="utf-8"))
            self.assertIn("mapa-noite", (noturno / "mapa.html").read_text(encoding="utf-8"))
            self.assertNotEqual(diurno, noturno)

    def test_pdf_pronto_so_com_o_mesmo_carimbo(self):
        with tempfile.TemporaryDirectory() as tmp:
            pasta = Path(tmp)
            self._escrever(pasta, "diurno", "mapa")
            self.assertIsNone(pdf_inteiro_valido(pasta))
            (pasta / "relatorio-rotas.pdf").write_bytes(b"x" * 25000)
            gravar_cache(pasta, "abc", {})
            self.assertIsNotNone(pdf_inteiro_valido(pasta))
            (pasta / "rotas.json").write_text('{"rotas":[2]}', encoding="utf-8")
            self.assertIsNone(pdf_inteiro_valido(pasta))

    def test_concorrencia_limitada(self):
        self.assertGreaterEqual(concorrencia(), 1)
        self.assertLessEqual(concorrencia(), 4)

    def _escrever(self, pasta: Path, turno: str, mapa: str) -> None:
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / "mapa.html").write_text(mapa, encoding="utf-8")
        (pasta / "rotas.json").write_text('{"rotas":[]}', encoding="utf-8")
        (pasta / "relatorio-diario.json").write_text('{"linhas":[]}', encoding="utf-8")
        (pasta / "relatorio.json").write_text(json.dumps({"turno": turno}), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
