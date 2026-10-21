"""Auditoria administrativa: autorização, consistência e histórico sem recálculo."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from inlog_agent.acesso import criar_primeiro_admin
from inlog_agent.server import Handler, SalaHTTPServer


SENHA_ADMIN = "Admin-Rota-Vivo-1"
SENHA_OP = "Operador-Rota-2"
DIA = "2026-10-19"


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("SAGTRACK_AUTH_DB", str(tmp_path / "acesso.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    return tmp_path


def _chamar(opener, url, dados=None, headers=None, method=None):
    corpo = None if dados is None else json.dumps(dados).encode("utf-8")
    pedido = urllib.request.Request(url, data=corpo, method=method or ("POST" if corpo is not None else "GET"))
    pedido.add_header("Content-Type", "application/json")
    for chave, valor in (headers or {}).items():
        pedido.add_header(chave, valor)
    try:
        with opener.open(pedido, timeout=30) as resp:
            bruto = resp.read()
            payload = json.loads(bruto.decode("utf-8") or "{}") if bruto else {}
            return resp.status, payload, resp.headers
    except urllib.error.HTTPError as exc:
        bruto = exc.read()
        try:
            payload = json.loads(bruto.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        return exc.code, payload, exc.headers


def _bruto(opener, url):
    pedido = urllib.request.Request(url)
    try:
        with opener.open(pedido, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def _cliente():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _servidor():
    httpd = SalaHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _entrar(base, login, senha):
    opener = _cliente()
    status, payload, _ = _chamar(opener, base + "/api/auth/login", {"login": login, "senha": senha}, method="POST")
    assert status == 200
    return opener, payload["csrf"]


def _gravar(rotas, *, aberto=0, situacao="ENCERRADO", sync="2026-10-19T18:40:00", falha=""):
    from inlog_agent.live_db import conectar, gravar_rotas, gravar_turno

    con = conectar()
    gravar_turno(
        con,
        {
            "data": DIA,
            "turno": "diurno",
            "aberto": aberto,
            "periodo": "teste",
            "ultima_sincronizacao": sync,
            "atualizado_em": sync,
            "falha": falha,
            "telemetria": "segredo-telemetria-xyz",
        },
    )
    gravar_rotas(con, DIA, "diurno", rotas)
    con.execute(
        "UPDATE turno SET situacao = ?, aberto = ?, falha = ?, telemetria = ? WHERE data = ? AND turno = ?",
        (situacao, aberto, falha, "segredo-telemetria-xyz", DIA, "diurno"),
    )
    con.commit()
    con.close()


def _rota(codigo, percentual, snapshot, placa=""):
    return {
        "codigo": codigo,
        "rota": codigo,
        "placa": placa,
        "veiculo": placa,
        "percentual": percentual,
        "grupo": "parcial",
        "status": "Parcial",
        "na_grade": True,
        "_geo": True,
        "executores_json": snapshot if isinstance(snapshot, str) else json.dumps(snapshot, ensure_ascii=False),
    }


def _foto():
    con = sqlite3.connect(Path(__import__("os").environ["SAGTRACK_LIVE_DB"]))
    try:
        rotas = con.execute("SELECT codigo, percentual, placa, executores_json FROM rota ORDER BY codigo").fetchall()
        pontos = con.execute("SELECT COUNT(*) FROM ponto").fetchone()[0]
    finally:
        con.close()
    return rotas, pontos


def test_consistencia_com_tabela_detalhe_pdf_e_snapshot(ambiente):
    from inlog_agent.auditoria_rotas import consultar
    from inlog_agent.cobertura import cobertura_compartilhada
    from inlog_agent.live_db import conectar
    from inlog_agent.live_mapa import detalhe_rota
    from inlog_agent.live_pdf import _rota_lida
    from tests.test_cobertura_compartilhada import _pontos, _rua, _veiculo

    ruas = [_rua(0, 0, 1200, 0), _rua(0, 160, 900, 160)]
    a = _veiculo("AAA1A11", _pontos(ruas[0], datetime(2026, 10, 19, 8, 0)))
    b = _veiculo("BBB2B22", _pontos(ruas[1], datetime(2026, 10, 19, 9, 30)))
    conta = cobertura_compartilhada(ruas, [a, b])
    _gravar([_rota("TST1", conta["percentual"], conta["persistencia"], conta["placa_principal"])])
    relatorio = consultar(DIA, "diurno", "TST1")
    ficha = relatorio["rota"]
    assert ficha["consistente"] is True
    assert ficha["divergente"] is False
    assert ficha["placa_principal"] == conta["placa_principal"]
    assert "BBB2B22" in ficha["placas_apoio"]
    assert ficha["passagens"] == []
    assert ficha["nao_confirmados"] == []
    fontes = {item["fonte"]: item["percentual"] for item in ficha["comparacao"]}
    assert fontes["Snapshot oficial"] == pytest.approx(conta["percentual"], abs=0.1)
    assert fontes["Tabela"] == fontes["Detalhe"] == fontes["PDF"] == pytest.approx(conta["percentual"], abs=0.1)
    individuais = {item["placa"]: item["percentual_individual"] for item in ficha["caminhoes"]}
    for executor in conta["executores"]:
        assert individuais[executor["placa"]] == pytest.approx(executor["percentual"], abs=0.1)
        exclusivo = next(item["cobertura_exclusiva"] for item in ficha["caminhoes"] if item["placa"] == executor["placa"])
        sobreposta = next(item["cobertura_sobreposta"] for item in ficha["caminhoes"] if item["placa"] == executor["placa"])
        assert exclusivo == pytest.approx(executor["contribuicao_exclusiva"], abs=0.1)
        assert sobreposta == pytest.approx(executor["contribuicao_compartilhada"], abs=0.1)

    con = conectar()
    from inlog_agent.live_db import ler_view

    tabela = next(item for item in ler_view(con, DIA, "diurno")["rotas"] if item["codigo"] == "TST1")
    linha = con.execute("SELECT * FROM rota WHERE codigo = 'TST1'").fetchone()
    con.close()
    detalhe = detalhe_rota(DIA, "diurno", "TST1")
    pdf = _rota_lida(linha)
    assert fontes["Tabela"] == pytest.approx(float(tabela["percentual"]), abs=0.1)
    assert fontes["Detalhe"] == pytest.approx(float(detalhe["percentual"]), abs=0.1)
    assert fontes["PDF"] == pytest.approx(float(pdf["percentual"]), abs=0.1)
    assert "segredo-telemetria-xyz" not in json.dumps(relatorio)
    assert "senha" not in json.dumps(relatorio).lower()


def test_separa_passagem_e_nao_confirmado_com_justificativa(ambiente):
    from inlog_agent.auditoria_rotas import consultar

    snapshot = {
        "calculado": True,
        "percentual": 70,
        "placa_principal": "AAA1A11",
        "veiculo_principal": "AAA1A11",
        "apoio": "BBB2B22",
        "passagens": ["CCC3C33"],
        "nao_confirmados": ["DDD4D44"],
        "auditoria": [
            {"placa": "CCC3C33", "classe": "passagem", "motivo": "o GPS cruza as vias oficiais sem progredir"},
            {"placa": "DDD4D44", "classe": "nao_confirmado", "motivo": "há GPS na via, sem direção suficiente"},
            {"placa": "AAA1A11", "classe": "principal", "motivo": "principal da rota"},
        ],
        "executores": [
            {
                "placa": "AAA1A11",
                "papel": "principal",
                "classificacao": "Principal",
                "percentual": 70,
                "contribuicao_exclusiva": 50,
                "contribuicao_compartilhada": 20,
            },
            {
                "placa": "BBB2B22",
                "papel": "apoio",
                "classificacao": "Apoio efetivo",
                "percentual": 30,
                "contribuicao_exclusiva": 20,
                "contribuicao_compartilhada": 10,
            },
        ],
    }
    _gravar([_rota("TST1", 70, snapshot, "AAA1A11")])
    ficha = consultar(DIA, "diurno", "TST1")["rota"]
    assert ficha["consistente"] is True
    assert [item["placa"] for item in ficha["passagens"]] == ["CCC3C33"]
    assert "sem progredir" in ficha["passagens"][0]["justificativa"]
    assert [item["placa"] for item in ficha["nao_confirmados"]] == ["DDD4D44"]
    assert "sem direção" in ficha["nao_confirmados"][0]["justificativa"]
    assert "CCC3C33" not in ficha["placas_apoio"]
    assert "DDD4D44" not in ficha["placas_apoio"]


def test_historico_sem_snapshot_nao_recalcula(ambiente, monkeypatch):
    from inlog_agent.auditoria_rotas import consultar, gerar_pdf

    def _proibido(*_args, **_kwargs):
        raise AssertionError("histórico encerrado não pode ser recalculado")

    legado = {
        "percentual": 55,
        "placa_principal": "OLD1A11",
        "apoio": "",
        "executores": [{"placa": "OLD1A11", "papel": "principal", "classificacao": "Principal", "percentual": 55}],
    }
    _gravar(
        [
            _rota("65DN", 60.3, "", "SDE5I26"),
            _rota("OLD1", 55, legado, "OLD1A11"),
        ]
    )
    monkeypatch.setattr("inlog_agent.cobertura.cobertura_compartilhada", _proibido)
    monkeypatch.setattr("inlog_agent.live_sync.auditar_turno_leitura", _proibido)
    monkeypatch.setattr("inlog_agent.live_sync._exibir_cobertura_oficial", _proibido)
    monkeypatch.setattr("inlog_agent.live_db.conectar", _proibido)
    antes = _foto()
    relatorio = consultar(DIA, "diurno")
    pdf, nome = gerar_pdf(DIA, "diurno", "65DN")
    assert _foto() == antes
    por_codigo = {item["codigo"]: item for item in relatorio["rotas"]}
    antigo = por_codigo["65DN"]
    assert antigo["incompleto"] is True
    assert antigo["compativel_historico"] is True
    assert antigo["divergente"] is False
    assert antigo["consistente"] is False
    assert any("sem recálculo" in texto for texto in antigo["alertas"])
    legado_lido = por_codigo["OLD1"]
    assert legado_lido["compativel_historico"] is True
    assert legado_lido["consistente"] is True
    assert legado_lido["legado"] is True
    assert "contribuicao_exclusiva" in legado_lido["campos_ausentes"]
    assert pdf.startswith(b"%PDF")
    assert b"65DN" in pdf
    assert b"segredo-telemetria-xyz" not in pdf
    assert nome.startswith("SEGTRACK_Auditoria_2026-10-19_Diurno_65DN")
    assert relatorio["historico_encerrado"] is True
    assert relatorio["ultima_sincronizacao"] == "2026-10-19T18:40:00"


def test_divergencia_nao_grava_de_volta(ambiente, monkeypatch):
    from inlog_agent.auditoria_rotas import consultar

    monkeypatch.setattr(
        "inlog_agent.cobertura.cobertura_compartilhada",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("não recalcular")),
    )
    snapshot = {
        "calculado": True,
        "percentual": 80,
        "placa_principal": "AAA1A11",
        "apoio": "sem apoio efetivo",
        "executores": [
            {
                "placa": "AAA1A11",
                "papel": "principal",
                "classificacao": "Principal",
                "percentual": 80,
                "contribuicao_exclusiva": 80,
                "contribuicao_compartilhada": 0,
            }
        ],
    }
    _gravar([_rota("TST1", 40, snapshot, "AAA1A11")], aberto=1, situacao="ABERTO", sync="2026-10-19T08:00:00")
    antes = _foto()
    ficha = consultar(DIA, "diurno", "TST1")["rota"]
    assert _foto() == antes
    assert ficha["divergente"] is True
    assert ficha["consistente"] is False
    fontes = {item["fonte"]: item for item in ficha["comparacao"]}
    assert fontes["Tabela"]["percentual"] == 40
    assert fontes["Detalhe"]["percentual"] == 40
    assert fontes["PDF"]["percentual"] == 40
    assert fontes["Snapshot oficial"]["percentual"] == 80
    assert fontes["Tabela"]["confere"] is False
    assert fontes["Snapshot oficial"]["confere"] is False


def test_sincronizacao_antiga_e_falha_sem_expor_detalhe(ambiente, monkeypatch):
    from inlog_agent.auditoria_rotas import consultar

    monkeypatch.setattr("inlog_agent.live_sync.agora_brt", lambda: datetime(2026, 10, 19, 12, 0, 0))
    snapshot = {
        "calculado": True,
        "percentual": 10,
        "placa_principal": "AAA1A11",
        "executores": [
            {
                "placa": "AAA1A11",
                "papel": "principal",
                "percentual": 10,
                "contribuicao_exclusiva": 10,
                "contribuicao_compartilhada": 0,
            }
        ],
    }
    _gravar(
        [_rota("TST1", 10, snapshot, "AAA1A11")],
        aberto=1,
        situacao="ABERTO",
        sync="2026-10-19T10:00:00",
        falha="token=super-secreto",
    )
    relatorio = consultar(DIA, "diurno", "TST1")
    texto = json.dumps(relatorio)
    assert relatorio["sincronizacao_antiga"] is True
    assert relatorio["falha_sincronizacao"] is True
    assert relatorio["incompleto"] is False
    assert "token=super-secreto" not in texto
    assert "segredo-telemetria-xyz" not in texto
    assert any("falha" in item for item in relatorio["alertas"])
    assert any("15 minutos" in item for item in relatorio["alertas"])


def test_autorizacao_http(ambiente):
    httpd = _servidor()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    url = base + "/api/admin/auditoria-rotas?data=2026-10-19&turno=diurno"
    pdf_url = base + "/api/admin/auditoria-rotas.pdf?data=2026-10-19&turno=diurno"
    try:
        anon = _cliente()
        status, payload, _ = _chamar(anon, url)
        assert status == 401
        assert "senha" not in json.dumps(payload).lower()
        status, bruto, _ = _bruto(anon, pdf_url)
        assert status == 401

        criar_primeiro_admin("admin@rota", SENHA_ADMIN, "Administrador")
        admin, csrf = _entrar(base, "admin@rota", SENHA_ADMIN)
        status, payload, _ = _chamar(
            admin,
            base + "/api/auth/usuarios",
            {"nome": "Operador", "login": "operador@rota", "senha": SENHA_OP, "perfil": "operador"},
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 200
        operador, _ = _entrar(base, "operador@rota", SENHA_OP)
        status, payload, _ = _chamar(operador, url)
        assert status == 403
        assert payload["erro"] == "Somente administradores podem auditar rotas."
        status, _, _ = _bruto(operador, pdf_url)
        assert status == 403
        status, payload, _ = _chamar(operador, url, {}, method="POST")
        assert status == 403
        status, payload, _ = _chamar(
            admin,
            url,
            {},
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 405
        assert payload["erro"] == "A auditoria de rotas é somente leitura."

        status, payload, _ = _chamar(admin, url)
        assert status == 200
        assert payload["ok"] is True
        assert payload["leitura"] == "somente leitura do snapshot gravado"
        assert payload["rotas"] == []
        status, payload, _ = _chamar(admin, base + "/api/admin/auditoria-rotas?data=2026-10-19&turno=tarde")
        assert status == 400
        status, conteudo, headers = _bruto(admin, pdf_url)
        assert status == 200
        assert conteudo.startswith(b"%PDF")
        assert "pdf" in (headers.get("Content-Type") or "")
        assert "segredo" not in conteudo.decode("latin-1", errors="ignore")
    finally:
        httpd.shutdown()


def test_painel_esconde_auditoria_ate_o_administrador():
    html = Path("inlog_agent/web/index.html").read_text(encoding="utf-8")
    assert 'id="tab-auditoria"' in html
    assert "hidden" in html.split('id="tab-auditoria"', 1)[0][-80:] or 'id="tab-auditoria" class="nav-item" hidden' in html
    assert "/api/admin/auditoria-rotas" in html
    assert "cobertura_compartilhada" not in html
