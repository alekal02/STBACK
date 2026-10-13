"""Desenho do basculante só dentro da área oficial programada.

Não grava ponto, não apaga trajeto e não muda a partição das rotas convencionais.
"""

from datetime import date, datetime

from shapely.geometry import Point, Polygon

from inlog_agent.live_mapa import _partir_trilha, trechos_internos_dificil
from inlog_agent.live_pdf import _aplicar_recorte_basculante, linhas_do_mapa_geral


def _anel(oeste, sul, leste, norte):
    return [
        [norte, oeste],
        [norte, leste],
        [sul, leste],
        [sul, oeste],
        [norte, oeste],
    ]


def _area(nome, anel, frequencia="DIARIO - DIURNO"):
    return {
        "id": nome,
        "nome": nome,
        "codigo": "DIFICIL ACESSO",
        "frequencia": frequencia,
        "poligonos": [anel],
    }


def _poly(anel):
    return Polygon([(par[1], par[0]) for par in anel])


def _pulso(hora, lat, lon, segundo=0):
    return (f"2026-10-13T{hora:02d}:{segundo // 60:02d}:{segundo % 60:02d}", lat, lon)


CAIXA = _anel(-49.30, -16.90, -49.20, -16.80)
AREA = _area("ALTO", CAIXA)
POLY = _poly(CAIXA)


def _coberto(lat, lon, poly=POLY):
    return poly.covers(Point(lon, lat))


def _coords(linhas):
    return [par for linha in linhas for par in linha]


def test_fora_da_area_nao_desenha_linha():
    pontos = [
        _pulso(8, -16.70, -49.25, 0),
        _pulso(8, -16.71, -49.25, 20),
        _pulso(8, -16.72, -49.25, 40),
    ]
    assert trechos_internos_dificil(pontos, [AREA]) == []


def test_dentro_da_area_so_desenha_o_interno():
    pontos = [
        _pulso(8, -16.85, -49.25, 0),
        _pulso(8, -16.852, -49.252, 20),
        _pulso(8, -16.854, -49.254, 40),
    ]
    linhas = trechos_internos_dificil(pontos, [AREA])
    assert len(linhas) == 1
    assert len(linhas[0]) >= 2
    assert all(_coberto(lat, lon) for lat, lon in _coords(linhas))


def test_entrada_e_saida_param_na_borda():
    anel = _anel(-49.30, -16.803, -49.20, -16.800)
    area = _area("FAIXA", anel)
    poly = _poly(anel)
    pontos = [
        _pulso(8, -16.7985, -49.25, 0),
        _pulso(8, -16.8015, -49.25, 20),
        _pulso(8, -16.8045, -49.25, 40),
    ]
    linhas = trechos_internos_dificil(pontos, [area])
    assert len(linhas) == 1
    linha = linhas[0]
    assert abs(linha[0][0] + 16.800) < 1e-6
    assert abs(linha[-1][0] + 16.803) < 1e-6
    assert all(poly.covers(Point(lon, lat)) for lat, lon in linha)
    assert not any(abs(lat + 16.7985) < 1e-5 or abs(lat + 16.8045) < 1e-5 for lat, _lon in linha)


def test_duas_areas_nao_se_conectam():
    esquerda = _anel(-49.270, -16.90, -49.252, -16.80)
    direita = _anel(-49.244, -16.90, -49.226, -16.80)
    areas = [_area("ESQ", esquerda), _area("DIR", direita)]
    pontos = [
        _pulso(8, -16.85, -49.258, 0),
        _pulso(8, -16.85, -49.254, 20),
        _pulso(8, -16.85, -49.250, 40),
        _pulso(8, -16.85, -49.246, 60),
        _pulso(8, -16.85, -49.242, 80),
        _pulso(8, -16.85, -49.238, 100),
    ]
    linhas = trechos_internos_dificil(pontos, areas)
    assert len(linhas) == 2
    poly_esq = _poly(esquerda)
    poly_dir = _poly(direita)
    donos = []
    for linha in linhas:
        na_esq = all(poly_esq.covers(Point(lon, lat)) for lat, lon in linha)
        na_dir = all(poly_dir.covers(Point(lon, lat)) for lat, lon in linha)
        assert na_esq != na_dir
        donos.append("esq" if na_esq else "dir")
    assert sorted(donos) == ["dir", "esq"]
    assert not any(abs(lon + 49.20) < 1e-4 for _lat, lon in _coords(linhas))


def test_intervalo_acima_de_dez_minutos_quebra():
    pontos = [
        _pulso(8, -16.85, -49.25, 0),
        _pulso(8, -16.851, -49.251, 20),
        ("2026-10-13T08:12:00", -16.852, -49.252),
        ("2026-10-13T08:12:20", -16.853, -49.253),
    ]
    linhas = trechos_internos_dificil(pontos, [AREA])
    assert len(linhas) == 2
    assert linhas[0][-1][0] > -16.852
    assert linhas[1][0][0] < -16.851


def test_salto_acima_de_480_metros_quebra():
    pontos = [
        _pulso(8, -16.820, -49.25, 0),
        _pulso(8, -16.8202, -49.2501, 20),
        _pulso(8, -16.826, -49.25, 40),
        _pulso(8, -16.8262, -49.2501, 60),
    ]
    linhas = trechos_internos_dificil(pontos, [AREA])
    assert len(linhas) == 2
    assert abs(linhas[0][-1][0] + 16.8202) < 1e-4
    assert abs(linhas[1][0][0] + 16.826) < 1e-4


def test_rota_convencional_continua_partindo_dentro_e_fora():
    setores = [{"codigo": "01AD-TESTE", "aneis": [CAIXA]}]
    coords = [(-16.75, -49.25), (-16.85, -49.25), (-16.95, -49.25)]
    dentro, fora = _partir_trilha(coords, setores)
    assert dentro and fora
    assert any(abs(par[0] + 16.85) < 1e-6 for parte in dentro for par in parte)
    assert any(abs(par[0] + 16.75) < 1e-6 or abs(par[0] + 16.95) < 1e-6 for parte in fora for par in parte)


def test_pdf_geral_omite_o_externo_do_basculante_e_mantem_a_rota():
    fora = [-16.70, -49.25]
    convencional = [[-16.70, -49.25], [-16.71, -49.25]]
    veiculos = [
        {
            "placa": "SCN4B49",
            "linhas": [[[-16.798, -49.25], [-16.802, -49.25], fora]],
        },
        {"placa": "ABC1D23", "linhas": [convencional]},
    ]
    pontos = {
        "SCN4B49": [
            _pulso(8, -16.798, -49.25, 0),
            _pulso(8, -16.802, -49.25, 20),
            _pulso(8, -16.805, -49.25, 40),
        ]
    }
    import inlog_agent.live_mapa as mapa

    original = mapa.areas_dificil
    mapa.areas_dificil = lambda: [AREA]
    try:
        _aplicar_recorte_basculante(veiculos, pontos, date(2026, 10, 13), "diurno")
    finally:
        mapa.areas_dificil = original
    assert "trechos_internos" in veiculos[0]
    assert "trechos_internos" not in veiculos[1]
    assert any(abs(par[0] + 16.70) < 1e-6 for par in veiculos[0]["linhas"][0])
    geral = linhas_do_mapa_geral(veiculos)
    basculante = [linha for linha in geral if linha != convencional]
    assert basculante
    assert all(_coberto(lat, lon) for linha in basculante for lat, lon in linha)
    assert convencional in geral


def test_area_nao_programada_preserva_gps_eventos_e_pino(monkeypatch, tmp_path):
    banco = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    monkeypatch.setattr(
        "inlog_agent.live_sync.agora_brt",
        lambda: datetime(2026, 10, 14, 10, 0, 0),
    )
    import inlog_agent.live_mapa as mapa
    from inlog_agent.live_db import conectar, inserir_pontos

    area = _area("ALTO", CAIXA, "TER/SAB - DIURNO")
    mapa._AREAS = None
    monkeypatch.setattr(mapa, "areas_dificil", lambda: [area])
    con = conectar()
    inserir_pontos(
        con,
        "2026-10-14",
        "diurno",
        "SCN-4B49",
        [
            {"timestamp": "2026-10-14T09:00:00", "latitude": -16.85, "longitude": -49.25},
            {"timestamp": "2026-10-14T09:00:20", "latitude": -16.851, "longitude": -49.251},
            {"timestamp": "2026-10-14T09:00:40", "latitude": -16.70, "longitude": -49.25},
        ],
    )
    con.commit()
    antes = con.execute("SELECT COUNT(*) FROM ponto").fetchone()[0]
    con.close()

    pacote = mapa.dificil_acesso("2026-10-14", "diurno")
    caminhao = next(item for item in pacote["veiculos"] if item["codigo"] == "CBT018")
    assert pacote["areas"][0]["programada"] is False
    assert caminhao["trechos_internos"] == []
    assert caminhao["trajeto"]
    assert caminhao["eventos"]
    assert caminhao["lat"] == -16.7
    assert caminhao["lon"] == -49.25
    con = conectar()
    try:
        depois = con.execute("SELECT COUNT(*) FROM ponto").fetchone()[0]
    finally:
        con.close()
    assert depois == antes == 3
