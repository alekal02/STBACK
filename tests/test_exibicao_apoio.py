"""Seleção da rota mostra apoio comprovado sem trocar o percentual histórico."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest

from tests.test_cobertura_compartilhada import _pontos, _setor


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent import live_mapa

    live_mapa._LEITURA.clear()
    return tmp_path


def _gravar(codigo: str, percentual: float, placa: str, snapshot: str = "") -> None:
    from inlog_agent.live_db import conectar, gravar_rotas, gravar_turno

    con = conectar()
    gravar_turno(con, {"data": "2026-10-19", "turno": "noturno", "aberto": 0, "periodo": "teste"})
    gravar_rotas(
        con,
        "2026-10-19",
        "noturno",
        [
            {
                "codigo": codigo,
                "rota": codigo,
                "placa": placa,
                "veiculo": placa,
                "percentual": percentual,
                "grupo": "parcial",
                "status": "Parcial",
                "na_grade": True,
                "_geo": True,
                "executores_json": snapshot,
            }
        ],
    )
    con.execute(
        "UPDATE turno SET situacao = 'ENCERRADO', aberto = 0 WHERE data = ? AND turno = ?",
        ("2026-10-19", "noturno"),
    )
    con.commit()
    con.close()


def _inserir(placa: str, pontos: list[dict]) -> None:
    from inlog_agent.live_db import conectar, inserir_pontos

    con = conectar()
    inserir_pontos(con, "2026-10-19", "noturno", placa, pontos)
    con.commit()
    con.close()


def _ruas_longas(codigo: str) -> list:
    return [rua for rua in (_setor(codigo).get("ruas") or []) if rua.length >= 80]


def _serie(ruas: list, inicio: datetime) -> list[dict]:
    pontos = []
    for indice, rua in enumerate(ruas):
        pontos.extend(_pontos(rua, inicio + timedelta(minutes=indice)))
    return pontos


def _foto(codigo: str):
    import sqlite3
    from pathlib import Path

    con = sqlite3.connect(Path(os.environ["SAGTRACK_LIVE_DB"]))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT percentual, placa, executores_json FROM rota WHERE codigo = ?",
            (codigo,),
        ).fetchone()
    finally:
        con.close()
    return row


def test_hifen_nao_apaga_trajetoria_do_apoio_gravado(ambiente):
    from inlog_agent.mapa_oficial import caminho_gpkg

    if caminho_gpkg() is None:
        pytest.skip("sem geometria oficial")
    from inlog_agent.live_mapa import detalhe_rota

    setor = _setor("65DN")
    geom = setor["geom"]
    internas = [
        rua
        for rua in _ruas_longas("65DN")
        if geom.covers(rua.interpolate(0.5, normalized=True))
    ]
    rua = internas[0]
    inicio = datetime(2026, 10, 19, 20, 0)
    snapshot = {
        "calculado": True,
        "percentual": 70,
        "placa_principal": "SDE-5I26",
        "apoio": "RBM-2G21",
        "executores": [
            {"placa": "SDE-5I26", "papel": "principal", "classificacao": "Principal", "percentual": 40, "cor": "#111111"},
            {"placa": "RBM-2G21", "papel": "apoio", "classificacao": "Apoio efetivo", "percentual": 30, "cor": "#222222"},
        ],
    }
    _gravar("65DN", 70, "SDE-5I26", json.dumps(snapshot))
    _inserir("SDE5I26", _pontos(rua, inicio)[:6])
    _inserir("RBM2G21", _pontos(rua, inicio + timedelta(hours=1))[:6])
    detalhe = detalhe_rota("2026-10-19", "noturno", "65DN")
    assert detalhe["percentual"] == 70
    assert detalhe["placas_apoio"] == ["RBM-2G21"]
    assert detalhe["comparacao_leitura"]["ausente_no_snapshot"] == []
    desenhos = {item["placa"]: item for item in detalhe["veiculos"] if item.get("papel") in {"principal", "apoio"}}
    assert set(desenhos) == {"SDE-5I26", "RBM-2G21"}
    assert desenhos["RBM-2G21"]["linhas"]
    assert desenhos["RBM-2G21"]["oficial"] is True


def test_65dn_encerrada_mostra_apoio_sem_trocar_o_historico(ambiente):
    if os.environ.get("SAGTRACK_SEM_GPKG") == "1":
        pytest.skip("sem geometria oficial")
    from inlog_agent.mapa_oficial import caminho_gpkg

    if caminho_gpkg() is None:
        pytest.skip("sem geometria oficial")
    from inlog_agent.live_mapa import _norm_placa, _placas_executoras, detalhe_rota

    ruas = _ruas_longas("65DN")
    ordenadas = sorted(ruas, key=lambda rua: rua.centroid.x)
    corte = sum(rua.length for rua in ordenadas) * 0.62
    acumulado = 0.0
    do_principal, do_apoio = [], []
    for rua in ordenadas:
        if acumulado < corte:
            do_principal.append(rua)
            acumulado += rua.length
        else:
            do_apoio.append(rua)
    inicio = datetime(2026, 10, 19, 20, 0)
    pontos_principal = _serie(do_principal, inicio)
    _gravar("65DN", 60.3, "SDE-5I26")
    _inserir("SDE-5I26", pontos_principal)
    _inserir("RBM-2G21", _serie(do_apoio, inicio + timedelta(hours=3)))
    _inserir("ZZZ-9Z99", pontos_principal)
    _inserir(
        "PAS-9Z99",
        [
            {"timestamp": "2026-10-19T22:00:00", "latitude": 0.0, "longitude": 0.0, "tz": "BRT"},
            {"timestamp": "2026-10-19T22:00:30", "latitude": 0.001, "longitude": 0.001, "tz": "BRT"},
        ],
    )
    antes = _foto("65DN")
    detalhe = detalhe_rota("2026-10-19", "noturno", "65DN")
    depois = _foto("65DN")
    assert detalhe["percentual"] == 60.3
    assert detalhe["placas_apoio"] == []
    assert detalhe["executores"] == []
    assert depois["percentual"] == antes["percentual"] == 60.3
    assert depois["placa"] == "SDE-5I26"
    assert not str(depois["executores_json"] or "").strip()
    leitura = detalhe["comparacao_leitura"]
    assert leitura["oficial"] is False
    assert leitura["placa_principal"] == "SDE-5I26"
    assert leitura["placas_apoio"] == ["RBM-2G21"]
    assert leitura["ausente_no_snapshot"] == ["RBM-2G21"]
    assert "ZZZ-9Z99" not in leitura["placas_apoio"]
    assert "PAS-9Z99" not in leitura["placas_apoio"]
    assert "ZZZ-9Z99" in leitura["sobrepostos"]
    assert leitura["percentual_leitura"] != 60.3
    assert "não altera o percentual histórico" in leitura["aviso"]
    papeis = {item["placa"]: item for item in detalhe["veiculos"]}
    assert set(papeis) == {"SDE-5I26", "RBM-2G21"}
    assert papeis["SDE-5I26"]["papel"] == "principal"
    assert papeis["RBM-2G21"]["papel"] == "apoio"
    assert papeis["RBM-2G21"]["oficial"] is False
    assert papeis["RBM-2G21"]["origem"] == "leitura"
    assert papeis["SDE-5I26"]["linhas"] and papeis["RBM-2G21"]["linhas"]
    permitidas = _placas_executoras("2026-10-19", "noturno", "65DN")
    assert _norm_placa("SDE-5I26") in permitidas
    assert _norm_placa("RBM-2G21") in permitidas
    assert _norm_placa("ZZZ-9Z99") not in permitidas
    assert _norm_placa("PAS-9Z99") not in permitidas


def test_64dn_com_dois_apoios_entra_no_mapa_sem_regravar(ambiente):
    from inlog_agent.mapa_oficial import caminho_gpkg

    if caminho_gpkg() is None:
        pytest.skip("sem geometria oficial")
    from inlog_agent.live_mapa import detalhe_rota

    marcas = sorted(
        ((rua.centroid.x, rua) for rua in _ruas_longas("64DN")),
        key=lambda item: item[0],
    )
    n = len(marcas)
    fatias = [marcas[: n // 3], marcas[n // 3 : 2 * n // 3], marcas[2 * n // 3 :]]
    fatias.sort(key=lambda fatia: sum(rua.length for _x, rua in fatia), reverse=True)
    placas = ["SDM-4D87", "SCH-9B28", "SDO-2J06"]
    _gravar("64DN", 50.0, placas[0])
    inicio = datetime(2026, 10, 19, 20, 0)
    for indice, (placa, fatia) in enumerate(zip(placas, fatias)):
        _inserir(placa, _serie([rua for _x, rua in fatia], inicio + timedelta(hours=indice)))
    antes = _foto("64DN")
    detalhe = detalhe_rota("2026-10-19", "noturno", "64DN")
    depois = _foto("64DN")
    assert detalhe["percentual"] == 50.0
    assert detalhe["placas_apoio"] == []
    assert depois["percentual"] == antes["percentual"] == 50.0
    assert not str(depois["executores_json"] or "").strip()
    leitura = detalhe["comparacao_leitura"]
    assert leitura["oficial"] is False
    assert leitura["placa_principal"] == "SDM-4D87"
    assert set(leitura["placas_apoio"]) == {"SCH-9B28", "SDO-2J06"}
    assert set(leitura["ausente_no_snapshot"]) == {"SCH-9B28", "SDO-2J06"}
    assert not leitura["passagens"]
    papeis = {item["placa"]: item["papel"] for item in detalhe["veiculos"]}
    assert papeis == {"SDM-4D87": "principal", "SCH-9B28": "apoio", "SDO-2J06": "apoio"}
    assert all(item["linhas"] for item in detalhe["veiculos"])
