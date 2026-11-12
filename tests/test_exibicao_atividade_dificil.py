"""O polígono de difícil acesso só aparece com atividade comprovada do basculante."""

from datetime import datetime, timedelta

from shapely.geometry import LineString, Polygon, box

from inlog_agent.cobertura import TO_UTM, TO_WGS
from inlog_agent.dificil_acesso import atividade_comprovada
from inlog_agent.mapa_oficial import eh_area_dificil

_X, _Y = TO_UTM.transform(-49.25, -16.82)


def _rua(dx0, dy0, dx1, dy1) -> LineString:
    return LineString([(_X + dx0, _Y + dy0), (_X + dx1, _Y + dy1)])


def _geom(dx0, dy0, dx1, dy1):
    return box(_X + dx0, _Y + dy0, _X + dx1, _Y + dy1)


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


def _serie(pontos: list[dict]) -> list[tuple[str, float, float]]:
    return [(ponto["timestamp"], ponto["latitude"], ponto["longitude"]) for ponto in pontos]


LARGO = _geom(-40, -200, 840, 200)
CENTRO = _rua(40, 0, 760, 0)
BORDA = _rua(40, 190, 760, 190)
MALHA_LARGA = [CENTRO, BORDA]


def test_atividade_interna_comprovada():
    pontos = _pontos(CENTRO, datetime(2026, 10, 19, 8, 0))
    assert len(pontos) > 8
    assert atividade_comprovada(MALHA_LARGA, LARGO, pontos) is True


def test_setor_estreito_com_progressao_na_via_oficial_conta():
    estreito = _geom(-20, -15, 420, 15)
    rua = _rua(10, 0, 390, 0)
    assert atividade_comprovada([rua], estreito, _pontos(rua, datetime(2026, 10, 19, 8, 0))) is True


def test_presenca_proximidade_contorno_ponto_e_parado_nao_ativam():
    coleta = _pontos(CENTRO, datetime(2026, 10, 19, 8, 0))
    assert atividade_comprovada(MALHA_LARGA, LARGO, coleta[:1]) is False
    parado = []
    momento = datetime(2026, 10, 19, 8, 0)
    for _ in range(8):
        parado.append({**coleta[4], "timestamp": momento.strftime("%Y-%m-%dT%H:%M:%S")})
        momento += timedelta(seconds=20)
    assert atividade_comprovada(MALHA_LARGA, LARGO, parado) is False
    canto = _pontos(_rua(40, -170, 760, -170), datetime(2026, 10, 19, 9, 0))
    assert atividade_comprovada(MALHA_LARGA, LARGO, canto) is False
    perto = []
    base = datetime(2026, 10, 19, 10, 0)
    for indice in range(6):
        ponto = CENTRO.interpolate(80)
        lon, lat = TO_WGS.transform(ponto.x, ponto.y + 20)
        perto.append(
            {
                "latitude": lat,
                "longitude": lon,
                "timestamp": (base + timedelta(seconds=15 * indice)).strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
    assert atividade_comprovada(MALHA_LARGA, LARGO, perto) is False
    assert atividade_comprovada(MALHA_LARGA, LARGO, _pontos(BORDA, datetime(2026, 10, 19, 11, 0))) is False
    cruzamento = _pontos(_rua(400, -220, 400, 220), datetime(2026, 10, 19, 12, 0), vel_kmh=36.0, passo_s=8)
    assert atividade_comprovada(MALHA_LARGA, LARGO, cruzamento) is False


def _area(geom, ruas):
    anel = []
    for x, y in list(geom.exterior.coords):
        lon, lat = TO_WGS.transform(x, y)
        anel.append([lat, lon])
    area = {
        "id": "SETOR|atividade",
        "nome": "SETOR ATIVIDADE",
        "codigo": "DIFICIL ACESSO",
        "frequencia": "SEG/SEX - DIURNO",
        "completo": "DIFICIL ACESSO - SETOR ATIVIDADE",
        "poligonos": [anel],
    }
    return area, {area["id"]: {"geom": geom, "ruas": ruas}}


def _preparar(monkeypatch, tmp_path, area, malha):
    banco = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    import inlog_agent.dificil_acesso as dificil
    import inlog_agent.live_mapa as mapa

    dificil._COBERTURA_AREA.clear()
    dificil._ATIVIDADE_AREA.clear()
    mapa._DIFICIL_OK.clear()
    mapa._AREAS = None
    monkeypatch.setattr(mapa, "areas_dificil", lambda: [area])
    monkeypatch.setattr("inlog_agent.mapa_oficial.malha_areas_dificil", lambda: malha)
    return mapa


def _inserir(mapa, data, turno, placa, pontos):
    from inlog_agent.live_db import conectar, inserir_pontos

    con = conectar()
    inserir_pontos(con, data, turno, placa, pontos)
    con.commit()
    con.close()


def test_api_dois_basculantes_no_mesmo_setor_e_troca_de_data(monkeypatch, tmp_path):
    area, malha = _area(LARGO, MALHA_LARGA)
    mapa = _preparar(monkeypatch, tmp_path, area, malha)
    pontos_a = _pontos(CENTRO, datetime(2026, 10, 12, 8, 0))
    pontos_b = _pontos(CENTRO, datetime(2026, 10, 12, 9, 0))
    _inserir(mapa, "2026-10-12", "diurno", "SCN-4B49", pontos_a)
    _inserir(mapa, "2026-10-12", "diurno", "SCN-1G89", pontos_b)
    from inlog_agent.dificil_acesso import cobertura_dos_basculantes

    um = cobertura_dos_basculantes(MALHA_LARGA, LARGO, [{"placa": "SCN4B49", "veiculo": "CBT018", "pontos": pontos_a}])
    dois = cobertura_dos_basculantes(
        MALHA_LARGA,
        LARGO,
        [
            {"placa": "SCN4B49", "veiculo": "CBT018", "pontos": pontos_a},
            {"placa": "SCN1G89", "veiculo": "CBT012", "pontos": pontos_b},
        ],
    )
    assert dois["percentual"] == um["percentual"]
    assert dois["percentual"] <= 100

    pacote = mapa.dificil_acesso("2026-10-12", "diurno")
    setor = pacote["areas"][0]
    assert setor["programada"] is True
    assert setor["atividade"] is True
    assert setor["fora_programacao"] is False
    assert setor["percentual"] == dois["percentual"]
    cbt018 = next(item for item in pacote["veiculos"] if item["codigo"] == "CBT018")
    cbt012 = next(item for item in pacote["veiculos"] if item["codigo"] == "CBT012")
    assert area["id"] in cbt018["trechos_por_area"]
    assert area["id"] in cbt012["trechos_por_area"]
    assert {item["codigo"] for item in pacote["veiculos"]} == {"CBT018", "CBT012"}

    outro_turno = mapa.dificil_acesso("2026-10-12", "noturno")
    assert outro_turno["areas"][0]["atividade"] is False
    assert outro_turno["areas"][0]["percentual"] is None
    outro_dia = mapa.dificil_acesso("2026-10-13", "diurno")
    assert outro_dia["areas"][0]["atividade"] is False
    assert outro_dia["areas"][0]["programada"] is False
    assert all(not item["trechos_por_area"] for item in outro_dia["veiculos"])


def test_area_programada_sem_atividade_fica_oculta_e_fora_da_programacao_aparece(monkeypatch, tmp_path):
    area, malha = _area(LARGO, MALHA_LARGA)
    mapa = _preparar(monkeypatch, tmp_path, area, malha)
    from inlog_agent.live_db import conectar

    con = conectar()
    con.execute(
        """
        INSERT INTO rota (data, turno, codigo, percentual, status, grupo)
        VALUES ('2026-10-12', 'diurno', '01AD', 41.0, 'Parcial', 'parcial')
        """
    )
    con.commit()
    con.close()
    _inserir(mapa, "2026-10-12", "diurno", "ABC1D23", _pontos(CENTRO, datetime(2026, 10, 12, 8, 0)))

    sem_basculante = mapa.dificil_acesso("2026-10-12", "diurno")
    assert sem_basculante["areas"][0]["programada"] is True
    assert sem_basculante["areas"][0]["atividade"] is False
    assert sem_basculante["areas"][0]["percentual"] == 0
    assert all(not item["trechos_internos"] for item in sem_basculante["veiculos"])

    fora = _pontos(CENTRO, datetime(2026, 10, 13, 8, 0))
    _inserir(mapa, "2026-10-13", "diurno", "SCN-4B49", fora)
    pacote = mapa.dificil_acesso("2026-10-13", "diurno")
    setor = pacote["areas"][0]
    assert setor["programada"] is False
    assert setor["atividade"] is True
    assert setor["fora_programacao"] is True
    assert setor["percentual"] is None
    cbt018 = next(item for item in pacote["veiculos"] if item["codigo"] == "CBT018")
    assert area["id"] in cbt018["trechos_por_area"]
    assert "ABC1D23" not in {item["placa"] for item in pacote["veiculos"]}

    trilhas = mapa.trilhas("2026-10-12", "diurno")
    placas = {item["placa"] for item in trilhas["veiculos"]}
    assert "ABC1D23" in placas
    assert not any(placa.startswith("SCN") for placa in placas)
    body, _etag = mapa.geometria_bytes("2026-10-12", "diurno")
    assert "DIFICIL" not in body.decode("utf-8").upper()
    assert not any(eh_area_dificil(codigo) for codigo in ("01AD",))
    con = conectar()
    gravada = con.execute(
        "SELECT percentual FROM rota WHERE data = '2026-10-12' AND turno = 'diurno' AND codigo = '01AD'"
    ).fetchone()
    con.close()
    assert gravada["percentual"] == 41.0


def test_falha_temporaria_da_fulltrack_preserva_a_leitura(monkeypatch, tmp_path):
    area, malha = _area(LARGO, MALHA_LARGA)
    mapa = _preparar(monkeypatch, tmp_path, area, malha)
    _inserir(mapa, "2026-10-12", "diurno", "SCN-4B49", _pontos(CENTRO, datetime(2026, 10, 12, 8, 0)))
    primeiro = mapa.dificil_acesso("2026-10-12", "diurno")
    assert primeiro["areas"][0]["atividade"] is True
    assert "gps_indisponivel" not in primeiro

    from inlog_agent.live_db import conectar

    con = conectar()
    con.execute("DELETE FROM ponto WHERE data = '2026-10-12' AND turno = 'diurno'")
    con.execute(
        """
        INSERT INTO placa (data, turno, placa, veiculo, consulta_falha_em)
        VALUES ('2026-10-12', 'diurno', 'SCN-4B49', 'CBT018', '2026-10-12T10:00:00')
        """
    )
    con.commit()
    con.close()
    preservado = mapa.dificil_acesso("2026-10-12", "diurno")
    assert preservado["gps_indisponivel"] is True
    assert preservado["falha"] is True
    assert preservado["areas"][0]["atividade"] is True
    assert preservado["areas"][0]["percentual"] == primeiro["areas"][0]["percentual"]

    def falha(*_args, **_kwargs):
        raise ConnectionError("Fulltrack temporariamente indisponivel")

    monkeypatch.setattr(mapa, "_ler_pontos_basculante", falha)
    de_novo = mapa.dificil_acesso("2026-10-12", "diurno")
    assert de_novo["gps_indisponivel"] is True
    assert de_novo["areas"][0]["atividade"] is True
    assert de_novo is not primeiro
