"""A interface pede a mesma janela que o backend usa para classificar o turno."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

from inlog_agent.live_turno import ciclo_operacional, periodo_coleta

HTML = Path("inlog_agent/web/index.html")
SABADO = "2026-10-24"
SEXTA = "2026-10-23"
QUARTA = "2026-10-21"


def _trecho(html: str, inicio: str, fim: str) -> str:
    pos = html.find(inicio)
    assert pos >= 0, inicio
    final = html.find(fim, pos)
    assert final > pos, fim
    return html[pos:final]


def test_o_painel_nao_guarda_a_janela_antiga_como_unica_regra():
    html = HTML.read_text(encoding="utf-8")
    janela = _trecho(html, "function janelaTurno", "function atualizarRotulosTurno")
    assert html.count("function janelaTurno") == 1
    assert 'hora_inicio: "05:00", hora_fim: "14:59"' in janela
    assert 'hora_inicio: "15:00", hora_fim: "03:00"' in janela
    assert 'hora_inicio: "19:00", hora_fim: "04:59"' in janela
    assert 'hora_inicio: "06:00", hora_fim: "18:59"' in janela
    assert 'hora_inicio: "19:00", hora_fim: "05:59"' in janela
    assert "aplicarPeriodo(cicloDoDia(" in html
    assert 'hora_inicio: "06:00",\n            hora_fim: "05:59"' not in html
    envio = _trecho(html, "async function enviarChatbot", "async function carregarChatbot")
    assert "cicloDoDia(diaEnvio)" in envio
    assert 'hora_inicio: "06:00"' not in envio


def test_parametros_da_interface_coincidem_com_o_backend():
    node = shutil.which("node")
    assert node, "Node não está disponível para executar a janela da interface."
    html = HTML.read_text(encoding="utf-8")
    funcoes = "\n".join(
        (
            _trecho(html, "function isoDia", "function hojeIso"),
            _trecho(html, "function diaDaSemana", "function atualizarRotulosTurno"),
        )
    )
    pedido = """
const dias = ["2026-10-21", "2026-10-23", "2026-10-24", "2026-10-25"];
const saida = {};
for (const data of dias) {
  saida[data] = {
    ciclo: cicloDoDia(data),
    diurno: janelaTurno("diurno", data),
    noturno: janelaTurno("noturno", data),
  };
}
process.stdout.write(JSON.stringify(saida));
"""
    resultado = subprocess.run(
        [node, "--check"],
        input=funcoes,
        text=True,
        capture_output=True,
        check=False,
    )
    assert resultado.returncode == 0, resultado.stderr
    rodou = subprocess.run(
        [node, "-e", funcoes + pedido],
        text=True,
        capture_output=True,
        check=False,
    )
    assert rodou.returncode == 0, rodou.stderr
    obtido = json.loads(rodou.stdout)
    for iso in (QUARTA, SEXTA, SABADO, "2026-10-25"):
        dia = date.fromisoformat(iso)
        inicio, fim, data_fim = ciclo_operacional(dia)
        assert obtido[iso]["ciclo"] == {
            "data": iso,
            "data_fim": data_fim.isoformat(),
            "hora_inicio": inicio,
            "hora_fim": fim,
        }
        for turno in ("diurno", "noturno"):
            hora_ini, hora_fim, fim_data = periodo_coleta(dia, turno)
            assert obtido[iso][turno] == {
                "data": iso,
                "data_fim": fim_data.isoformat(),
                "hora_inicio": hora_ini,
                "hora_fim": hora_fim,
                "turno": turno,
            }
