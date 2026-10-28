"""Importação do snapshot aprovado. Não chama a Fulltrack nem o Nominatim."""

from __future__ import annotations

import json
import sqlite3

import pytest

from inlog_agent.frota import (
    ARQUIVO_OPERACOES_APROVADO,
    _com_banco,
    _ler_snapshot,
    _sem_acento,
    _versao_snapshot,
    criar_operacao,
    fixar_placa,
    importar_na_subida,
    linha_import,
)
from inlog_agent.fulltrack_api import _norm_placa


NOMES = {
    "Aparecida de Goiânia",
    "Ariquemes - RO",
    "Belo Horizonte - MG",
    "Caldas Novas - GO",
    "Contagem - MG",
    "Ibiporã - PR",
    "Londrina - PR",
    "Pindamonhangaba - SP",
    "Porto Velho - RO",
    "Uberaba - MG",
    "Unaí - MG",
}
APARECIDA = {
    "nome": "Aparecida de Goiânia",
    "lat_min": -16.9,
    "lat_max": -16.7,
    "lon_min": -49.3,
    "lon_max": -49.1,
    "ativa": True,
    "padrao": True,
}


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent import frota

    frota._CACHE.update(chave="", corpo=None, expira=0.0)
    yield tmp_path
    frota._CACHE.update(chave="", corpo=None, expira=0.0)


def _estado():
    def ler(con):
        ops = []
        for row in con.execute("SELECT * FROM frota_operacao ORDER BY id"):
            placas = [
                item["placa"]
                for item in con.execute(
                    "SELECT placa FROM frota_operacao_placa WHERE operacao_id = ? ORDER BY placa",
                    (row["id"],),
                )
            ]
            ops.append(
                {
                    "id": int(row["id"]),
                    "nome": str(row["nome"]),
                    "lat_min": float(row["lat_min"]),
                    "lat_max": float(row["lat_max"]),
                    "lon_min": float(row["lon_min"]),
                    "lon_max": float(row["lon_max"]),
                    "ativa": int(row["ativa"]),
                    "padrao": int(row["padrao"]),
                    "placas": placas,
                }
            )
        versoes = [str(item["versao"]) for item in con.execute("SELECT versao FROM frota_import ORDER BY versao")]
        return ops, versoes

    return _com_banco(ler)


def _gravar(path, operacoes, placas):
    versao = _versao_snapshot(operacoes, placas)
    path.write_text(
        json.dumps({"operacoes": operacoes, "placas": placas, "versao": versao}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return versao


def _placas(ops):
    return [placa for op in ops for placa in op["placas"]]


def test_snapshot_aprovado_so_tem_operacao_e_placa():
    bruto = json.loads(ARQUIVO_OPERACOES_APROVADO.read_text(encoding="utf-8"))
    assert set(bruto) == {"operacoes", "placas", "versao"}
    assert len(bruto["operacoes"]) == 11
    assert {op["nome"] for op in bruto["operacoes"]} == NOMES
    assert sum(1 for op in bruto["operacoes"] if op["padrao"]) == 1
    assert len(bruto["placas"]) == 171
    texto = json.dumps(bruto, ensure_ascii=False).lower()
    for termo in ("password", "senha", "token", "api_key", "secret", "authorization"):
        assert termo not in texto
    _, _, versao = _ler_snapshot(ARQUIVO_OPERACOES_APROVADO)
    assert versao == bruto["versao"]


def test_import_em_banco_vazio(ambiente, capsys):
    _, _, versao = _ler_snapshot(ARQUIVO_OPERACOES_APROVADO)
    importar_na_subida()
    assert capsys.readouterr().out == linha_import({"criadas": 10, "fixadas": 171, "versao": versao}) + "\n"
    ops, versoes = _estado()
    assert {op["nome"] for op in ops} == NOMES
    assert len(ops) == 11
    assert len(_placas(ops)) == 171
    assert [op["nome"] for op in ops if op["padrao"]] == ["Aparecida de Goiânia"]
    ap = next(op for op in ops if op["nome"] == "Aparecida de Goiânia")
    assert ap["lat_min"] == -16.98
    assert ap["lat_max"] == -16.68
    assert ap["lon_min"] == -49.42
    assert ap["lon_max"] == -49.05
    assert versoes == [versao]


def test_import_repetido_nao_duplica(ambiente, capsys):
    importar_na_subida()
    capsys.readouterr()
    importar_na_subida()
    assert capsys.readouterr().out == ""
    ops, versoes = _estado()
    assert len(ops) == 11
    assert len(_placas(ops)) == 171
    assert len(versoes) == 1


def test_comando_frota_importar(ambiente, capsys):
    from inlog_agent.cli import build_parser, main

    args = build_parser().parse_args(["frota-importar", "--arquivo", "outro.json"])
    assert args.comando == "frota-importar"
    assert args.arquivo == "outro.json"
    _, _, versao = _ler_snapshot(ARQUIVO_OPERACOES_APROVADO)
    main(["frota-importar", "--arquivo", str(ARQUIVO_OPERACOES_APROVADO)])
    assert capsys.readouterr().out == linha_import({"criadas": 10, "fixadas": 171, "versao": versao}) + "\n"
    main(["frota-importar"])
    assert capsys.readouterr().out == ""
    ops, versoes = _estado()
    assert len(ops) == 11
    assert len(_placas(ops)) == 171
    assert len(versoes) == 1


def test_vinculo_manual_preservado(ambiente):
    contagem = criar_operacao(
        {
            "nome": "Contagem - MG",
            "lat_min": -19.95,
            "lat_max": -19.85,
            "lon_min": -44.1,
            "lon_max": -44.02,
        }
    )["operacao"]
    fixar_placa(contagem["id"], "MAN6F66")
    antes, _ = _estado()
    limites = next(op for op in antes if op["id"] == contagem["id"])
    arquivo = ambiente / "snap.json"
    _gravar(
        arquivo,
        [
            {
                "nome": "Aparecida de Goiânia",
                "lat_min": -16.98,
                "lat_max": -16.68,
                "lon_min": -49.42,
                "lon_max": -49.05,
                "ativa": True,
                "padrao": True,
            },
            {
                "nome": "Contagem - MG",
                "lat_min": -19.5,
                "lat_max": -19.2,
                "lon_min": -44.5,
                "lon_max": -44.2,
                "ativa": True,
                "padrao": False,
            },
        ],
        {"MAN6F66": "Aparecida de Goiânia", "NOV1D44": "Contagem - MG"},
    )
    importar_na_subida(arquivo)
    ops, _ = _estado()
    por_nome = {op["nome"]: op for op in ops}
    manual = {_norm_placa(placa) for placa in por_nome["Contagem - MG"]["placas"]}
    aparecida = {_norm_placa(placa) for placa in por_nome["Aparecida de Goiânia"]["placas"]}
    assert _norm_placa("MAN6F66") in manual
    assert _norm_placa("MAN6F66") not in aparecida
    assert _norm_placa("NOV1D44") in manual
    assert sum(1 for placa in _placas(ops) if _norm_placa(placa) == _norm_placa("MAN6F66")) == 1
    depois = por_nome["Contagem - MG"]
    assert depois["lat_min"] == limites["lat_min"]
    assert depois["lat_max"] == limites["lat_max"]
    assert depois["lon_min"] == limites["lon_min"]
    assert depois["lon_max"] == limites["lon_max"]
    assert [op["nome"] for op in ops if op["padrao"]] == ["Aparecida de Goiânia"]


def test_operacao_existente_mantem_os_limites(ambiente, capsys):
    criar_operacao(
        {
            "nome": "Ibipora - PR",
            "lat_min": -23.3,
            "lat_max": -23.1,
            "lon_min": -51.2,
            "lon_max": -51.0,
        }
    )
    antes, _ = _estado()
    ibi_antes = next(op for op in antes if _sem_acento(op["nome"]).startswith("ibipora"))
    ap_antes = next(op for op in antes if op["nome"] == "Aparecida de Goiânia")
    arquivo = ambiente / "snap.json"
    _gravar(
        arquivo,
        [
            APARECIDA,
            {
                "nome": "Ibiporã - PR",
                "lat_min": -23.5,
                "lat_max": -23.2,
                "lon_min": -51.4,
                "lon_max": -51.1,
                "ativa": True,
                "padrao": False,
            },
        ],
        {"IBI1A11": "Ibiporã - PR"},
    )
    importar_na_subida(arquivo)
    saida = capsys.readouterr().out
    assert "0 operações criadas, 1 placas fixadas" in saida
    ops, versoes = _estado()
    ibis = [op for op in ops if _sem_acento(op["nome"]).startswith("ibipora")]
    assert len(ibis) == 1
    ibi = ibis[0]
    assert ibi["nome"] == "Ibipora - PR"
    assert (ibi["lat_min"], ibi["lat_max"], ibi["lon_min"], ibi["lon_max"]) == (
        ibi_antes["lat_min"],
        ibi_antes["lat_max"],
        ibi_antes["lon_min"],
        ibi_antes["lon_max"],
    )
    assert (ibi["lat_min"], ibi["lat_max"]) != (-23.5, -23.2)
    assert {_norm_placa(placa) for placa in ibi["placas"]} == {_norm_placa("IBI1A11")}
    ap = next(op for op in ops if op["nome"] == "Aparecida de Goiânia")
    assert (ap["lat_min"], ap["lat_max"], ap["lon_min"], ap["lon_max"]) == (
        ap_antes["lat_min"],
        ap_antes["lat_max"],
        ap_antes["lon_min"],
        ap_antes["lon_max"],
    )
    assert ap["padrao"] == 1
    assert len(versoes) == 1


def test_arquivo_com_erro_nao_derruba_a_subida(ambiente, monkeypatch, capsys):
    ruim = ambiente / "ruim.json"
    ruim.write_text("{", encoding="utf-8")
    importar_na_subida(ruim)
    saida = capsys.readouterr().out
    assert saida.startswith("Frota import:")
    assert "Arquivo inválido" in saida
    ops, versoes = _estado()
    assert [op["nome"] for op in ops] == ["Aparecida de Goiânia"]
    assert _placas(ops) == []
    assert versoes == []

    falso = ambiente / "falso.json"
    _gravar(falso, [APARECIDA], {})
    texto = json.loads(falso.read_text(encoding="utf-8"))
    texto["versao"] = "0" * 64
    falso.write_text(json.dumps(texto), encoding="utf-8")
    importar_na_subida(falso)
    assert "não confere" in capsys.readouterr().out
    ops, versoes = _estado()
    assert len(ops) == 1
    assert versoes == []

    from inlog_agent import frota

    original = frota._aplicar_snapshot

    def explode(con, operacoes, placas):
        original(con, operacoes, placas)
        raise sqlite3.OperationalError("falha simulada")

    monkeypatch.setattr(frota, "_aplicar_snapshot", explode)
    importar_na_subida()
    assert "Frota import: falha simulada" in capsys.readouterr().out
    monkeypatch.setattr(frota, "_aplicar_snapshot", original)
    ops, versoes = _estado()
    assert [op["nome"] for op in ops] == ["Aparecida de Goiânia"]
    assert _placas(ops) == []
    assert versoes == []

    serviu = {}

    class FalsoServidor:
        def __init__(self, addr, handler):
            serviu["addr"] = addr

        def serve_forever(self):
            serviu["ok"] = True

        def server_close(self):
            serviu["fechou"] = True

    monkeypatch.setattr("inlog_agent.frota._caminho_snapshot", lambda arquivo=None: ruim)
    monkeypatch.setattr("inlog_agent.server._garantir_bibliotecas_pdf", lambda: None)
    monkeypatch.setattr("inlog_agent.server.iniciar_disparo_automatico", lambda: None)
    monkeypatch.setattr("inlog_agent.live_sync.iniciar_loop", lambda: None)
    monkeypatch.setattr("inlog_agent.server.SalaHTTPServer", FalsoServidor)
    from inlog_agent.server import servir

    servir(porta=9, host="127.0.0.1")
    assert serviu.get("ok") is True
    assert serviu.get("fechou") is True
    assert "Frota import:" in capsys.readouterr().out
    ops, versoes = _estado()
    assert [op["nome"] for op in ops] == ["Aparecida de Goiânia"]
    assert versoes == []
