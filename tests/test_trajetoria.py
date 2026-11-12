"""Motor único: corte de turno, limpeza e casamento na via."""

from datetime import date, datetime, timedelta

from shapely.geometry import LineString, Point

from inlog_agent.cobertura import TO_UTM, TO_WGS
from inlog_agent.trajetoria import (
    _ligar,
    _projetar,
    casar_pulsos,
    cortar_turno,
    entra_no_percentual,
    limpar_pulsos,
    percentual_da_linha,
    rede_oficial,
)


_X, _Y = TO_UTM.transform(-49.25, -16.82)


def _rua(dx0, dy0, dx1, dy1) -> LineString:
    return LineString([(_X + dx0, _Y + dy0), (_X + dx1, _Y + dy1)])


def _pulso(linha: LineString, dist: float, quando: datetime) -> tuple[str, float, float]:
    ponto = linha.interpolate(dist)
    lon, lat = TO_WGS.transform(ponto.x, ponto.y)
    return quando.strftime("%Y-%m-%dT%H:%M:%S"), float(lat), float(lon)


def test_corte_de_turno_deixa_so_o_relogio_do_diurno():
    dia = date(2026, 11, 11)
    pontos = [
        ("2026-11-11T05:30:00", -16.82, -49.25),
        ("2026-11-11T08:00:00", -16.82, -49.25),
        ("2026-11-11T20:00:00", -16.82, -49.25),
    ]
    diurno = cortar_turno(pontos, dia, "diurno")
    noturno = cortar_turno(pontos, dia, "noturno")
    assert [item[0] for item in diurno] == ["2026-11-11T08:00:00"]
    assert [item[0] for item in noturno] == ["2026-11-11T20:00:00"]


def test_limpeza_remove_teleporte():
    base = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [
        ("2026-11-11T08:00:00", -16.820, -49.250),
        ("2026-11-11T08:00:10", -16.820, -49.251),
        ("2026-11-11T08:00:20", -15.000, -48.000),
        ("2026-11-11T08:00:30", -16.820, -49.252),
    ]
    limpos = limpar_pulsos(pontos)
    assert all(item[1] < -16 for item in limpos)
    assert len(limpos) == 3
    assert base.year == 2026


def test_casamento_segue_a_esquina_e_nao_a_corda():
    leste = _rua(0, 0, 400, 0)
    norte = _rua(400, 0, 400, 300)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = []
    for i, dist in enumerate(range(0, 401, 40)):
        pontos.append(_pulso(leste, dist, inicio + timedelta(seconds=12 * i)))
    for i, dist in enumerate(range(40, 301, 40), start=len(pontos)):
        pontos.append(_pulso(norte, dist, inicio + timedelta(seconds=12 * i)))
    trajeto = casar_pulsos(pontos, [leste, norte], date(2026, 11, 11), "diurno")
    assert trajeto.linhas
    assert trajeto.geom is not None
    malha = leste.union(norte)
    partes = [trajeto.geom] if trajeto.geom.geom_type == "LineString" else list(trajeto.geom.geoms)
    for parte in partes:
        for x, y in parte.coords:
            assert malha.distance(Point(x, y)) < 15
    assert trajeto.geom.length > 600


def test_camadas_somadas_ligam_o_cruzamento_e_a_ponta_a_8m(monkeypatch):
    from inlog_agent import mapa_oficial, trajetoria

    horizontal = _rua(0, 0, 200, 0)
    vertical = _rua(80, -60, 80, 60)
    ponta = _rua(200, 7, 280, 7)
    monkeypatch.setattr(trajetoria, "_REDE", None)
    monkeypatch.setattr(trajetoria, "_linhas_osm", lambda: [horizontal])
    monkeypatch.setattr(mapa_oficial, "carregar_vias", lambda: [vertical, ponta])
    rede = rede_oficial()
    origem = _projetar(rede, horizontal.coords[0][0], horizontal.coords[0][1], None, None)
    alto = _projetar(rede, vertical.coords[-1][0], vertical.coords[-1][1], None, None)
    fim = _projetar(rede, ponta.coords[-1][0], ponta.coords[-1][1], None, None)
    assert origem is not None and alto is not None and fim is not None
    assert _ligar(rede, origem, alto, 400) is not None
    assert _ligar(rede, origem, fim, 400) is not None


def test_percentual_da_linha_casada_nao_casa_de_novo():
    rua = _rua(0, 0, 400, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [_pulso(rua, dist, inicio + timedelta(seconds=12 * i)) for i, dist in enumerate(range(0, 401, 40))]
    trajeto = casar_pulsos(pontos, [rua], date(2026, 11, 11), "diurno")
    valor = percentual_da_linha(trajeto.geom, [rua], rua.buffer(40))
    assert valor is not None and valor >= 95
    fora = _rua(0, 300, 400, 300)
    fora_traj = casar_pulsos(
        [_pulso(fora, dist, inicio + timedelta(seconds=12 * i)) for i, dist in enumerate(range(0, 401, 40))],
        [rua, fora],
        date(2026, 11, 11),
        "diurno",
    )
    assert percentual_da_linha(fora_traj.geom, [rua], rua.buffer(30)) == 0


def test_pista_dupla_conta_a_outra_faixa_e_nao_a_continuacao():
    sul = _rua(0, 0, 400, 0)
    norte = _rua(0, 15, 400, 15)
    longe = _rua(0, 55, 400, 55)
    frente = _rua(400, 0, 800, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [_pulso(sul, dist, inicio + timedelta(seconds=12 * i)) for i, dist in enumerate(range(0, 401, 40))]
    trajeto = casar_pulsos(pontos, [sul, norte], date(2026, 11, 11), "diurno")
    setor = Point(_X + 200, _Y).buffer(500)
    duas = percentual_da_linha(trajeto.geom, [sul, norte], setor, ["Avenida Brasil", "Avenida Brasil"])
    uma = percentual_da_linha(trajeto.geom, [sul, norte], setor, ["Avenida Brasil", "Rua das Flores"])
    distante = percentual_da_linha(trajeto.geom, [sul, longe], setor, ["Avenida Brasil", "Avenida Brasil"])
    seguida = percentual_da_linha(trajeto.geom, [sul, frente], setor, ["Avenida Brasil", "Avenida Brasil"])
    assert duas is not None and uma is not None
    assert duas > uma + 20
    assert distante is not None and distante < duas - 20
    assert seguida is not None and seguida < duas - 20


def test_diurno_nao_credita_rota_de_noturno(monkeypatch):
    from inlog_agent import trajetoria

    sul = _rua(0, 0, 200, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [_pulso(sul, dist, inicio + timedelta(seconds=12 * i)) for i, dist in enumerate(range(0, 201, 40))]
    trajeto = casar_pulsos(pontos, [sul], date(2026, 11, 11), "diurno")
    setor = {
        "codigo": "66DN",
        "frequencia": "DIARIO - NOTURNO",
        "geom": sul.buffer(80),
        "ruas": [sul],
        "nomes": ["Avenida Brasil"],
    }
    monkeypatch.setattr(trajetoria, "trajetos_do_turno", lambda *_args, **_kwargs: {"SCN-4B49": trajeto})
    monkeypatch.setattr("inlog_agent.mapa_oficial.carregar_setores", lambda *_args, **_kwargs: [setor])
    assert trajetoria.percentual_das_rotas(date(2026, 11, 11), "diurno", ["66DN", "70AN"]) == {}
    noturno = trajetoria.percentual_das_rotas(date(2026, 11, 11), "noturno", ["66DN"])
    assert noturno.get("66DN", 0) >= 90


def test_so_coleta_entra_no_percentual():
    assert not entra_no_percentual("PM-001", "PM-001")
    assert not entra_no_percentual("PM-34", "PM-34")
    assert not entra_no_percentual("TAR-6461", "Trator EX854 - 32316461")
    assert not entra_no_percentual("CGZ-0015", "TRATOR CORTADOR DE GRAMA GIRO ZERO HUSQVARNA Z560X")
    assert not entra_no_percentual("SCZ-4A29", "IVECO/DAILY 65CD")
    assert entra_no_percentual("SCH-9B28", "VW/18.260 CRM 4X2 4P")
    assert entra_no_percentual("VCK-0D86", "VW/26.260 CRM 6X2 4P")
    assert entra_no_percentual("SCN-4E19", "IVECO/TECTOR 15-210")
    assert entra_no_percentual("SCN-4B49", "IVECO/TECTOR 24-280")
    assert entra_no_percentual("SCN-1G89", "CBT012")


def test_vao_curto_na_rua_sem_saida_liga_e_o_longo_nao():
    beco = _rua(0, 0, 0, 40)
    oeste = _rua(0, 0, -250, 0)
    sobe = _rua(-250, 0, -250, 80)
    alto = _rua(-250, 80, 18, 80)
    desce = _rua(18, 80, 18, 0)
    ruas = [beco, oeste, sobe, alto, desce]
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    curto = [
        _pulso(beco, 38, inicio),
        _pulso(desce, 42, inicio + timedelta(seconds=60)),
    ]
    trajeto = casar_pulsos(curto, ruas, date(2026, 11, 11), "diurno")
    assert trajeto.geom is not None and trajeto.geom.length > 400
    assert trajeto.geom.distance(Point(_X, _Y + 38)) < 8
    longo = [
        _pulso(beco, 38, inicio),
        _pulso(desce, 42, inicio + timedelta(seconds=120)),
    ]
    assert not casar_pulsos(longo, ruas, date(2026, 11, 11), "diurno").linhas


def test_intervalo_acima_de_dez_minutos_nao_liga():
    rua = _rua(0, 0, 400, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [
        _pulso(rua, 0, inicio),
        _pulso(rua, 300, inicio + timedelta(minutes=11)),
    ]
    trajeto = casar_pulsos(pontos, [rua], date(2026, 11, 11), "diurno")
    assert not trajeto.linhas
    assert percentual_da_linha(trajeto.geom, [rua], rua.buffer(40)) == 0


def test_dez_minutos_com_velocidade_de_coleta_liga_caminho_unico():
    rua = _rua(0, 0, 800, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [
        _pulso(rua, 0, inicio),
        _pulso(rua, 600, inicio + timedelta(minutes=8)),
    ]
    trajeto = casar_pulsos(pontos, [rua], date(2026, 11, 11), "diurno")
    assert trajeto.geom is not None and trajeto.geom.length > 500


def test_dois_minutos_num_caminho_unico_seguem_a_rua():
    leste = _rua(0, 0, 200, 0)
    norte = _rua(200, 0, 200, 180)
    atalho = _rua(0, 0, 200, 180)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [
        _pulso(leste, 0, inicio),
        _pulso(norte, 180, inicio + timedelta(seconds=120)),
    ]
    trajeto = casar_pulsos(pontos, [leste, norte], date(2026, 11, 11), "diurno")
    assert trajeto.linhas
    malha = leste.union(norte)
    for x, y in trajeto.geom.coords:
        assert malha.distance(Point(x, y)) < 8
    assert trajeto.geom.distance(Point(_X + 200, _Y)) < 8
    assert trajeto.geom.distance(atalho.interpolate(0.5, normalized=True)) > 40
    oficial = percentual_da_linha(trajeto.geom, [leste, norte], Point(_X + 100, _Y + 40).buffer(250))
    assert oficial is not None and oficial >= 80


def test_dois_caminhos_creditam_so_o_trecho_comum():
    direto = _rua(0, 0, 340, 0)
    sobe = _rua(120, 0, 120, 80)
    alto = _rua(120, 80, 220, 80)
    desce = _rua(220, 80, 220, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [
        _pulso(direto, 0, inicio),
        _pulso(direto, 340, inicio + timedelta(seconds=90)),
    ]
    trajeto = casar_pulsos(pontos, [direto, sobe, alto, desce], date(2026, 11, 11), "diurno")
    assert trajeto.geom is not None
    assert trajeto.geom.distance(Point(_X + 60, _Y)) < 8
    assert trajeto.geom.distance(Point(_X + 280, _Y)) < 8
    assert trajeto.geom.distance(Point(_X + 170, _Y)) > 20


def test_dois_caminhos_parecidos_nao_creditam():
    oeste = _rua(0, 0, 0, 80)
    sul = _rua(0, 0, 200, 0)
    leste = _rua(200, 0, 200, 80)
    norte = _rua(0, 80, 200, 80)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [
        _pulso(oeste, 40, inicio),
        _pulso(leste, 40, inicio + timedelta(seconds=70)),
    ]
    ruas = [oeste, sul, leste, norte]
    trajeto = casar_pulsos(pontos, ruas, date(2026, 11, 11), "diurno")
    assert not trajeto.linhas
    assert percentual_da_linha(trajeto.geom, ruas, Point(_X + 100, _Y + 40).buffer(180)) == 0


def test_percentual_nao_conta_apoio(monkeypatch):
    from inlog_agent import trajetoria

    sul = _rua(0, 0, 200, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [_pulso(sul, dist, inicio + timedelta(seconds=12 * i)) for i, dist in enumerate(range(0, 201, 40))]
    trajeto = casar_pulsos(pontos, [sul], date(2026, 11, 11), "diurno")
    setor = {
        "codigo": "01AD",
        "frequencia": "SEG/QUA/SEX - DIURNO",
        "geom": sul.buffer(80),
        "ruas": [sul],
        "nomes": ["Avenida Brasil"],
    }
    monkeypatch.setattr(trajetoria, "trajetos_do_turno", lambda *_args, **_kwargs: {"PM-001": trajeto, "TAR-6461": trajeto})
    monkeypatch.setattr(trajetoria, "placas_do_percentual", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("inlog_agent.mapa_oficial.carregar_setores", lambda *_args, **_kwargs: [setor])
    assert trajetoria.percentual_das_rotas(date(2026, 11, 11), "diurno", ["01AD"]) == {}
