"""O admin rebusca o GPS do turno na API e recalcula a conclusão."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar

from inlog_agent.acesso import criar_primeiro_admin
from inlog_agent.server import Handler, SalaHTTPServer


DIA = "2026-11-10"
AGORA = datetime(2026, 11, 10, 15, 0)


def _pontos():
    return {
        "SCH-9B28": [
            {"timestamp": "2026-11-10T10:00:00", "latitude": -16.82, "longitude": -49.25},
            {"timestamp": "2026-11-10T02:00:00", "latitude": -16.82, "longitude": -49.25},
            {"timestamp": "2026-11-10T19:30:00", "latitude": -16.82, "longitude": -49.25},
        ],
        "SDI-4197": [
            {"timestamp": "2026-11-10T11:00:00", "latitude": -16.821, "longitude": -49.251},
            {"timestamp": "2026-11-10T11:05:00", "latitude": -16.822, "longitude": -49.252},
        ],
    }


def test_filtro_da_unidade_pega_vck_sem_modelo_nem_prefixo():
    from inlog_agent.fulltrack_api import _escolher_veiculos, _norm_placa
    from inlog_agent.live_sync import _placas_unidade_aprovada

    placas = _placas_unidade_aprovada()
    assert "VCK0D86" in placas
    assert "TTV9G70" not in placas
    veiculos = [
        {
            "ras_vei_placa": "VCK-0D86",
            "ras_vei_modelo": "VW 26.260",
            "ras_vei_veiculo": "VW 26.260",
            "ras_vei_tipo": 3,
            "ras_vei_id": "vck",
            "ras_vei_equipamento": "eq-vck",
        },
        {
            "ras_vei_placa": "XYZ-1A23",
            "ras_vei_modelo": "VW 26.260",
            "ras_vei_veiculo": "VW 26.260",
            "ras_vei_tipo": 3,
            "ras_vei_id": "xyz",
            "ras_vei_equipamento": "eq-xyz",
        },
    ]
    eventos = [{"ras_eve_latitude": -16.82, "ras_eve_longitude": -49.25, "ras_ras_id": "eq-vck"}]
    com_unidade = {
        _norm_placa(str(item.get("ras_vei_placa") or ""))
        for item in _escolher_veiculos(veiculos, eventos, placas, None)
    }
    sem_lista = {
        _norm_placa(str(item.get("ras_vei_placa") or ""))
        for item in _escolher_veiculos(veiculos, eventos, (), None)
    }
    assert com_unidade == {"VCK0D86"}
    assert "VCK0D86" not in sem_lista


def test_rebusca_grava_so_a_janela_e_recalcula(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent.live_db import conectar
    from inlog_agent.live_sync import rebuscar_historico_turno

    monkeypatch.setattr(
        "inlog_agent.live_sync._limites",
        lambda dia, turno: [{"codigo": "ALTO", "minx": -1e9, "miny": -1e9, "maxx": 1e9, "maxy": 1e9}],
    )
    serie = _pontos()

    def buscar(placa, inicio, fim):
        assert inicio == datetime(2026, 11, 10, 6, 0)
        assert fim == AGORA
        return serie[placa]

    def recalcular(con, output_dir, dia, turno, codigos):
        assert codigos
        return [
            {
                "codigo": "ALTO",
                "rota": "JARDIM ALTO PARAÍSO",
                "percentual": 72.5,
                "placa": "SCH-9B28",
                "_geo": True,
            }
        ]

    resultado = rebuscar_historico_turno(
        tmp_path,
        DIA,
        "diurno",
        ["sch9b28", "SDI-4197"],
        agora=AGORA,
        buscar_historico=buscar,
        recalcular=recalcular,
    )
    por_placa = {item["placa"]: item for item in resultado["placas"]}
    assert resultado["ok"] is True
    assert resultado["turno"] == "diurno"
    assert por_placa["SCH-9B28"]["pontos"] == 1
    assert por_placa["SCH-9B28"]["novos"] == 1
    assert por_placa["SCH-9B28"]["fora_da_janela"] == 2
    assert por_placa["SDI-4197"]["pontos"] == 2
    assert resultado["rotas"] == [
        {"codigo": "ALTO", "rota": "JARDIM ALTO PARAÍSO", "percentual": 72.5, "placa": "SCH-9B28"}
    ]
    con = conectar()
    try:
        gravados = {
            row["placa"]: row["qtd"]
            for row in con.execute("SELECT placa, COUNT(*) AS qtd FROM ponto GROUP BY placa")
        }
    finally:
        con.close()
    assert gravados == {"SCH-9B28": 1, "SDI-4197": 2}

    de_novo = rebuscar_historico_turno(
        tmp_path,
        DIA,
        "diurno",
        ["SCH-9B28", "SDI-4197"],
        agora=AGORA,
        buscar_historico=buscar,
        recalcular=recalcular,
    )
    repetido = {item["placa"]: item["novos"] for item in de_novo["placas"]}
    assert repetido == {"SCH-9B28": 0, "SDI-4197": 0}
    assert de_novo["rotas"][0]["percentual"] == 72.5


def test_recusa_placa_vazia(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent.live_sync import rebuscar_historico_turno

    resultado = rebuscar_historico_turno(tmp_path, DIA, "diurno", [], agora=AGORA)
    assert resultado["ok"] is False
    assert "placa" in resultado["erro"]


def _chamar(opener, url, dados=None, headers=None):
    corpo = None if dados is None else json.dumps(dados).encode("utf-8")
    pedido = urllib.request.Request(url, data=corpo, method="POST" if corpo is not None else "GET")
    pedido.add_header("Content-Type", "application/json")
    for chave, valor in (headers or {}).items():
        pedido.add_header(chave, valor)
    try:
        with opener.open(pedido, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def test_endpoint_admin_rebusca_o_turno(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    monkeypatch.setenv("SAGTRACK_AUTH_DB", str(tmp_path / "acesso.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    chamadas = []

    def falso(output_dir, data, turno, placas, **kwargs):
        chamadas.append((data, turno, placas))
        return {
            "ok": True,
            "data": "2026-11-10",
            "turno": "noturno",
            "placas": [{"placa": "SCH-9B28", "pontos": 40, "novos": 40}],
            "rotas": [
                {"codigo": "ALTO", "rota": "JARDIM ALTO PARAÍSO", "percentual": 81.2, "placa": "SCH-9B28"}
            ],
        }

    monkeypatch.setattr("inlog_agent.live_sync.rebuscar_historico_turno", falso)
    criar_primeiro_admin("admin.historico", "Admin-Historico-1", "Admin")
    httpd = SalaHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        anon = urllib.request.build_opener()
        status, payload = _chamar(
            anon,
            base + "/api/admin/rebuscar-historico",
            {"data": DIA, "turno": "noturno", "placas": ["SCH-9B28"]},
        )
        assert status == 401
        assert payload["ok"] is False

        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        status, sessao = _chamar(opener, base + "/api/auth/login", {"login": "admin.historico", "senha": "Admin-Historico-1"})
        assert status == 200
        status, payload = _chamar(
            opener,
            base + "/api/admin/rebuscar-historico",
            {"data": DIA, "turno": "noturno", "placa": "SCH-9B28, SDI-4197"},
            {"X-CSRF-Token": sessao["csrf"]},
        )
        assert status == 200, payload
        assert payload["rotas"][0]["percentual"] == 81.2
        assert chamadas == [(DIA, "noturno", "SCH-9B28, SDI-4197")]
    finally:
        httpd.shutdown()
