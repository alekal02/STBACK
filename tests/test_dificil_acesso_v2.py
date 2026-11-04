"""Camada aditiva V2: acessos, passagens e endpoint novo. Não mexe no V1."""

from inlog_agent.dificil_acesso_v2 import (
    GAP_PASSAGEM_S,
    calcular_acessos,
    montar_area_v2,
    passagens_do_veiculo,
    trechos_do_veiculo,
)
from inlog_agent.dificil_acesso_v2 import _geom_wgs


def _quadrado(nome, sul, norte, oeste, leste):
    anel = [
        [norte, oeste],
        [norte, leste],
        [sul, leste],
        [sul, oeste],
        [norte, oeste],
    ]
    return {
        "id": f"{nome}|teste",
        "nome": nome,
        "codigo": "DIFÍCIL ACESSO",
        "programada": True,
        "atividade": True,
        "percentual": 50,
        "poligonos": [anel],
    }


SUL, NORTE, OESTE, LESTE = -16.90, -16.80, -49.30, -49.20
AREA = _quadrado("BAIRRO A", SUL, NORTE, OESTE, LESTE)


def test_passagem_continua_e_quebra_por_gap_de_3_min():
    # Entra, fica, sai; depois gap > 3 min e nova entrada.
    pontos = [
        ("2026-10-12T08:00:00", -16.79, -49.25),  # fora
        ("2026-10-12T08:01:00", -16.85, -49.25),  # dentro
        ("2026-10-12T08:02:00", -16.86, -49.25),  # dentro
        ("2026-10-12T08:03:00", -16.79, -49.25),  # fora → fecha P1
        ("2026-10-12T08:10:00", -16.79, -49.26),  # fora (gap > 3 min)
        ("2026-10-12T08:11:00", -16.85, -49.26),  # dentro → P2
        ("2026-10-12T08:12:00", -16.86, -49.26),
    ]
    geom = _geom_wgs(AREA["poligonos"])
    acessos = [
        {"id": "acesso-1", "nome": "Acesso 1", "n": 1, "lat": -16.80, "lon": -49.25},
        {"id": "acesso-2", "nome": "Acesso 2", "n": 2, "lat": -16.90, "lon": -49.25},
    ]
    pas = passagens_do_veiculo(
        pontos,
        geom,
        acessos,
        codigo="CBT018",
        placa="SCN4B49",
        area_id=AREA["id"],
        area_nome=AREA["nome"],
    )
    assert len(pas) == 2
    assert pas[0]["marcador_entrada"] == "E1"
    assert pas[0]["marcador_saida"] == "S1"
    assert pas[0]["saida"] is not None
    assert pas[0]["entrada"]["acesso"]
    assert pas[1]["n"] == 2
    assert pas[1]["aberta"] is True
    assert pas[1]["saida"] is None
    assert GAP_PASSAGEM_S == 180.0


def test_trechos_marcam_dentro_e_fora():
    pontos = [
        ("2026-10-12T08:00:00", -16.79, -49.25),
        ("2026-10-12T08:01:00", -16.85, -49.25),
        ("2026-10-12T08:02:00", -16.86, -49.25),
        ("2026-10-12T08:03:00", -16.79, -49.25),
    ]
    geom = _geom_wgs(AREA["poligonos"])
    pas = passagens_do_veiculo(
        pontos,
        geom,
        [],
        codigo="CBT018",
        placa="SCN4B49",
        area_id=AREA["id"],
        area_nome=AREA["nome"],
    )
    trechos = trechos_do_veiculo(pontos, geom, pas)
    assert trechos
    assert any(t["dentro"] for t in trechos)
    assert any(not t["dentro"] for t in trechos)


def test_montar_area_v2_status_e_cor():
    series = {
        "CBT018": [
            ("2026-10-12T08:00:00", -16.79, -49.25),
            ("2026-10-12T08:01:00", -16.85, -49.25),
            ("2026-10-12T08:02:00", -16.86, -49.25),
            ("2026-10-12T08:03:00", -16.79, -49.25),
        ],
        "CBT012": [],
    }
    basc = (
        {"codigo": "CBT018", "placa": "SCN4B49"},
        {"codigo": "CBT012", "placa": "SCN1G89"},
    )
    item = montar_area_v2(AREA, series, basc, malha_info=None)
    assert item["grupo"] == "parcial"
    assert item["status"] == "Parcial"
    assert item["passagens"]
    assert item["passagens"][0]["cor"].startswith("#")
    assert isinstance(item["acessos"], list)


def test_calcular_acessos_nao_quebra_sem_malha():
    acessos = calcular_acessos(AREA, None)
    assert isinstance(acessos, list)
    # Fallback amostra a borda → pelo menos alguns pontos nomeados.
    assert all(a["nome"].startswith("Acesso ") for a in acessos)


def test_endpoint_v2_existe_e_v1_intacta(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent.live_db import conectar
    from inlog_agent import live_mapa

    con = conectar()
    con.close()
    # Sem GPS: V2 devolve pacote estruturado, sem exception.
    pacote = live_mapa.dificil_acesso_v2("2026-10-12", "diurno")
    assert pacote["versao"] == 2
    assert "areas" in pacote
    assert "veiculos" in pacote
    assert "placas" in pacote
    # V1 continua respondendo.
    v1 = live_mapa.dificil_acesso("2026-10-12", "diurno")
    assert v1.get("versao") != 2
    assert "areas" in v1
    assert "veiculos" in v1


def test_servidor_rota_v2():
    import inspect
    from inlog_agent import server as srv

    fonte = inspect.getsource(srv.Handler.do_GET)
    assert "/api/live-report/dificil-acesso/v2" in fonte
    assert "/api/live-report/areas-dificil-acesso/v2" in fonte
    assert "/api/live-report/dificil-acesso" in fonte
    mapa = inspect.getsource(srv.Handler._mapa_vivo)
    assert "dificil_acesso_v2" in mapa
