"""Cobertura do setor de difícil acesso, separada das rotas dos compactadores."""

from datetime import date, datetime, timedelta

from shapely.geometry import LineString, Point, Polygon, box

from inlog_agent.cobertura import TO_UTM, TO_WGS
from inlog_agent.dificil_acesso import anotar_programacao, cobertura_dos_basculantes
from inlog_agent.mapa_oficial import carregar_areas_dificil, eh_area_dificil, malha_areas_dificil

_X, _Y = TO_UTM.transform(-49.25, -16.82)


def _rua(dx0, dy0, dx1, dy1) -> LineString:
    return LineString([(_X + dx0, _Y + dy0), (_X + dx1, _Y + dy1)])


def _pontos(linha: LineString, inicio: datetime, vel_kmh: float = 12.0, passo_s: int = 12) -> list[dict]:
    passo = max(8.0, vel_kmh / 3.6 * passo_s)
    saida = []
    dist = 0.0
    momento = inicio
    while dist <= linha.length:
        ponto = linha.interpolate(dist)
        lon, lat = TO_WGS.transform(ponto.x, ponto.y)
        saida.append(
            {
                "latitude": lat,
                "longitude": lon,
                "timestamp": momento.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        dist += passo
        momento += timedelta(seconds=passo_s)
    return saida


def _serie(placa: str, codigo: str, pontos: list[dict]) -> dict:
    return {"placa": placa, "veiculo": codigo, "pontos": pontos}


def test_dois_basculantes_na_mesma_via_nao_duplicam():
    rua = _rua(0, 0, 800, 0)
    geom = rua.buffer(60)
    pontos = _pontos(rua, datetime(2026, 10, 19, 8, 0))
    um = cobertura_dos_basculantes([rua], geom, [_serie("SCN4B49", "CBT018", pontos)])
    dois = cobertura_dos_basculantes(
        [rua],
        geom,
        [
            _serie("SCN4B49", "CBT018", pontos),
            _serie("SCN1G89", "CBT012", pontos),
        ],
    )
    assert um["calculado"] is True
    assert um["percentual"] >= 95
    assert dois["percentual"] == um["percentual"]
    assert dois["percentual"] <= 100


def test_ruas_diferentes_entram_na_uniao_e_nao_passam_de_cem():
    primeira = _rua(0, 0, 800, 0)
    segunda = _rua(0, 40, 800, 40)
    geom = box(_X - 40, _Y - 40, _X + 840, _Y + 80)
    so_um = cobertura_dos_basculantes(
        [primeira, segunda],
        geom,
        [_serie("SCN4B49", "CBT018", _pontos(primeira, datetime(2026, 10, 19, 8, 0)))],
    )
    uniao = cobertura_dos_basculantes(
        [primeira, segunda],
        geom,
        [
            _serie("SCN4B49", "CBT018", _pontos(primeira, datetime(2026, 10, 19, 8, 0))),
            _serie("SCN1G89", "CBT012", _pontos(segunda, datetime(2026, 10, 19, 9, 0))),
        ],
    )
    assert uniao["percentual"] > so_um["percentual"]
    assert uniao["percentual"] <= 100


def test_gps_fora_do_poligono_nao_entra_no_percentual():
    rua = _rua(0, 0, 600, 0)
    geom = rua.buffer(40)
    fora = _rua(0, 400, 600, 400)
    resultado = cobertura_dos_basculantes(
        [rua],
        geom,
        [_serie("SCN4B49", "CBT018", _pontos(fora, datetime(2026, 10, 19, 8, 0)))],
    )
    assert resultado["percentual"] == 0


def test_sem_gps_a_cobertura_nao_e_acrescentada():
    rua = _rua(0, 0, 400, 0)
    vazio = cobertura_dos_basculantes([rua], rua.buffer(40), [])
    assert vazio["percentual"] == 0
    sem_via = cobertura_dos_basculantes([], rua.buffer(40), [_serie("SCN4B49", "CBT018", _pontos(rua, datetime(2026, 10, 19, 8, 0)))])
    assert sem_via["calculado"] is False
    assert sem_via["percentual"] is None


def test_malha_oficial_fica_dentro_do_poligono_e_fora_da_rota():
    import inlog_agent.mapa_oficial as mapa

    mapa._MALHA_AREAS = None
    malha = malha_areas_dificil()
    areas = carregar_areas_dificil()
    assert len(malha) == 11
    assert set(malha) == {area["id"] for area in areas}
    for area in areas:
        assert eh_area_dificil(area["codigo"])
        info = malha[area["id"]]
        geom = info["geom"]
        assert not geom.is_empty
        for rua in info["ruas"]:
            resto = rua.difference(geom.buffer(0.5))
            assert resto.is_empty or resto.length < 1
    assert mapa._MALHA_AREAS is malha


def test_terra_do_sol_existe_e_quarta_nao_e_dia_programado():
    areas = carregar_areas_dificil()
    terra = [area for area in areas if area["nome"] == "TERRA DO SOL"]
    assert len(terra) == 1
    area = terra[0]
    assert area["id"].startswith("TERRA DO SOL|")
    assert area["frequencia"] == "QUI"
    anel = area["poligonos"][0]
    assert len(anel) >= 4
    assert anel[0] == anel[-1]
    quarta = anotar_programacao(areas, date(2026, 10, 21), "diurno", [])
    marcada = next(item for item in quarta if item["nome"] == "TERRA DO SOL")
    assert marcada["programada"] is False
    assert marcada["poligonos"] == area["poligonos"]
    assert len(quarta) == 11


def test_api_isola_data_turno_e_nao_grava_a_rota(monkeypatch, tmp_path):
    banco = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    import inlog_agent.dificil_acesso as dificil
    import inlog_agent.live_mapa as mapa
    from inlog_agent.live_db import conectar, inserir_pontos

    dificil._COBERTURA_AREA.clear()
    oeste, sul, leste, norte = -49.300, -16.810, -49.290, -16.800
    anel = [
        [norte, oeste],
        [norte, leste],
        [sul, leste],
        [sul, oeste],
        [norte, oeste],
    ]
    xs, ys = TO_UTM.transform([oeste, leste, leste, oeste], [norte, norte, sul, sul])
    geom = Polygon(list(zip(xs, ys)))
    rua = LineString([(min(xs) + 40, (min(ys) + max(ys)) / 2), (max(xs) - 40, (min(ys) + max(ys)) / 2)])
    area = {
        "id": "SETOR|teste",
        "nome": "SETOR TESTE",
        "codigo": "DIFICIL ACESSO",
        "frequencia": "SEG/SEX - DIURNO",
        "completo": "DIFICIL ACESSO - SETOR TESTE",
        "poligonos": [anel],
    }
    mapa._AREAS = None
    monkeypatch.setattr(mapa, "areas_dificil", lambda: [area])
    monkeypatch.setattr(
        "inlog_agent.mapa_oficial.malha_areas_dificil",
        lambda: {area["id"]: {"geom": geom, "ruas": [rua]}},
    )
    pontos = _pontos(rua, datetime(2026, 10, 12, 8, 0))
    con = conectar()
    inserir_pontos(con, "2026-10-12", "diurno", "SCN-4B49", pontos)
    inserir_pontos(con, "2026-10-12", "noturno", "SCN-4B49", pontos)
    inserir_pontos(con, "2026-10-13", "diurno", "SCN-4B49", pontos)
    con.execute(
        """
        INSERT INTO rota (data, turno, codigo, percentual, status, grupo)
        VALUES ('2026-10-11', 'diurno', '01AD', 33.3, 'Parcial', 'parcial')
        """
    )
    con.commit()
    con.close()

    segunda = mapa.dificil_acesso("2026-10-12", "diurno")
    assert segunda["areas"][0]["programada"] is True
    assert segunda["areas"][0]["percentual"] >= 95
    cbt = next(item for item in segunda["veiculos"] if item["codigo"] == "CBT018")
    linhas = cbt["trechos_por_area"][area["id"]]
    anel_oficial = Polygon([(par[1], par[0]) for par in anel])
    assert linhas
    for linha in linhas:
        for lat, lon in linha:
            ponto = Point(lon, lat)
            assert anel_oficial.covers(ponto) or anel_oficial.distance(ponto) < 1e-8

    sexta = mapa.dificil_acesso("2026-10-16", "diurno")
    assert sexta["areas"][0]["programada"] is True
    assert sexta["areas"][0]["percentual"] == 0

    noturno = mapa.dificil_acesso("2026-10-12", "noturno")
    assert noturno["areas"][0]["percentual"] is None
    outro_dia = mapa.dificil_acesso("2026-10-13", "diurno")
    assert outro_dia["areas"][0]["programada"] is False
    assert outro_dia["areas"][0]["percentual"] is None
    assert not outro_dia["areas"][0].get("fora_programacao")
    fora = next(item for item in outro_dia["veiculos"] if item["codigo"] == "CBT018")
    assert area["id"] not in fora["trechos_por_area"]
    assert fora["trechos_internos"] == []

    con = conectar()
    gravada = con.execute(
        "SELECT percentual FROM rota WHERE data = '2026-10-11' AND turno = 'diurno' AND codigo = '01AD'"
    ).fetchone()
    outras = con.execute("SELECT COUNT(*) FROM rota").fetchone()[0]
    gps_fora = con.execute(
        "SELECT COUNT(*) FROM ponto WHERE data = '2026-10-13' AND turno = 'diurno'"
    ).fetchone()[0]
    con.close()
    assert gravada["percentual"] == 33.3
    assert outras == 1
    assert gps_fora == len(pontos)
