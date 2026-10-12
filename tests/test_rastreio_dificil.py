"""Chegada fictícia do GPS dos basculantes. O banco de produção não é aberto."""

from datetime import datetime

from inlog_agent.dificil_acesso import (
    SEM_RASTREAMENTO,
    SINAL_DESATUALIZADO,
    eventos_nas_areas,
    montar_trajetoria,
    preparar_areas,
)
from inlog_agent.mapa_oficial import carregar_areas_dificil


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
        "poligonos": [anel],
    }


SUL, NORTE, OESTE, LESTE = -16.90, -16.80, -49.30, -49.20
AREA = _quadrado("BAIRRO A", SUL, NORTE, OESTE, LESTE)
PRONTOS = preparar_areas([AREA])


def _passo(indice, lat, lon, segundo):
    return {
        "timestamp": f"2026-10-12T08:00:{segundo:02d}",
        "latitude": lat,
        "longitude": lon,
    }


def _presenca(pontos, prontos=None, codigo="CBT018", placa="SCN4B49", data="2026-10-12", turno="diurno"):
    return eventos_nas_areas(
        pontos,
        PRONTOS if prontos is None else prontos,
        codigo=codigo,
        placa=placa,
        data=data,
        turno=turno,
    )


def _coords(pontos):
    return {(round(lat, 5), round(lon, 5)) for _ts, lat, lon in pontos}


def test_entrada_saida_e_permanencia_usam_so_o_gps():
    lats = [-16.797, -16.799, -16.801, -16.803, -16.805, -16.803, -16.801, -16.799, -16.797]
    pontos = [(f"2026-10-12T08:{(i * 10) // 60:02d}:{(i * 10) % 60:02d}", lat, -49.25) for i, lat in enumerate(lats)]
    presenca = _presenca(pontos)
    tipos = [ev["tipo"] for ev in presenca["eventos"]]
    assert tipos == ["entrada", "saida"]
    entrada, saida = presenca["eventos"]
    assert entrada["veiculo"] == "CBT018"
    assert entrada["placa"] == "SCN4B49"
    assert entrada["area"] == "BAIRRO A"
    assert entrada["area_id"] == "BAIRRO A|teste"
    assert entrada["data"] == "2026-10-12"
    assert entrada["turno"] == "diurno"
    assert saida["area_id"] == entrada["area_id"]
    assert saida["turno"] == "diurno"
    vistos = _coords(pontos)
    assert all((ev["lat"], ev["lon"]) in vistos for ev in presenca["eventos"])
    assert presenca["area"] == ""
    fechada = presenca["permanencias"][0]
    assert fechada["aberta"] is False
    assert fechada["saida"] == saida["ts"]
    assert fechada["veiculo"] == "CBT018"
    assert fechada["segundos"] >= 0
    desenho = montar_trajetoria(pontos, lambda *_args: False)
    assert desenho["trajeto"]
    assert desenho["poligonos"] == []


def test_gap_quebra_a_linha_e_nao_inventa_saida():
    pontos = [
        ("2026-10-12T08:00:00", -16.85, -49.25),
        ("2026-10-12T08:11:00", -16.8502, -49.2502),
    ]
    presenca = _presenca(pontos)
    assert [ev["tipo"] for ev in presenca["eventos"]] == ["entrada", "entrada"]
    assert all(item["saida"] == "" for item in presenca["permanencias"])
    assert presenca["permanencias"][0]["aberta"] is False
    assert presenca["permanencias"][1]["aberta"] is True
    assert presenca["permanencias"][0]["fim"] == pontos[0][0]
    assert presenca["permanencias"][1]["entrada"] == pontos[1][0]
    desenho = montar_trajetoria(pontos, lambda *_args: False)
    assert desenho["trajeto"] == []
    assert desenho["deslocamento"] == []
    assert desenho["dificil"] == []
    vistos = _coords(pontos)
    assert all((ev["lat"], ev["lon"]) in vistos for ev in presenca["eventos"])


def test_um_ponto_dentro_e_permanencia_aberta():
    pontos = [("2026-10-12T08:00:00", -16.85, -49.25)]
    presenca = _presenca(pontos, codigo="CBT012", placa="SCN1G89")
    assert len(presenca["eventos"]) == 1
    assert presenca["eventos"][0]["tipo"] == "entrada"
    assert presenca["eventos"][0]["veiculo"] == "CBT012"
    assert presenca["permanencias"][0]["aberta"] is True
    assert presenca["area"] == "BAIRRO A"
    assert montar_trajetoria(pontos, lambda *_args: False)["trajeto"] == []


def test_cada_poligono_oficial_reconhece_o_proprio_interior():
    from shapely.geometry import Polygon

    areas = carregar_areas_dificil()
    assert len(areas) == 11
    assert len({area["id"] for area in areas}) == 11
    prontos = preparar_areas(areas)
    for area in areas:
        anel = area["poligonos"][0]
        poly = Polygon([(float(par[1]), float(par[0])) for par in anel])
        dentro = poly.representative_point()
        presenca = eventos_nas_areas(
            [("2026-10-12T08:00:00", dentro.y, dentro.x)],
            prontos,
            codigo="CBT018",
            placa="SCN4B49",
            data="2026-10-12",
            turno="diurno",
        )
        assert presenca["area_id"] == area["id"]
        assert presenca["eventos"][0]["tipo"] == "entrada"
        assert presenca["eventos"][0]["area"] == area["nome"]
        assert presenca["permanencias"][0]["aberta"] is True
        assert all(ev["tipo"] != "saida" for ev in presenca["eventos"])


def _banco(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    monkeypatch.setattr(
        "inlog_agent.live_sync.agora_brt",
        lambda: datetime(2026, 10, 12, 10, 0, 0),
    )


def test_sync_reconhece_os_dois_quando_cada_um_aparece(monkeypatch, tmp_path):
    _banco(monkeypatch, tmp_path)
    import inlog_agent.live_mapa as mapa
    from inlog_agent.dificil_acesso import presentes_no_cadastro
    from inlog_agent.live_sync import _limpar_cache_frota, ciclo_gps

    outra = _quadrado("BAIRRO B", -16.90, -16.80, -49.50, -49.40)
    mapa._AREAS = None
    monkeypatch.setattr(mapa, "areas_dificil", lambda: [AREA, outra])
    brutos = []
    fase = {"n": 0}
    chamadas = []
    rotas = []
    lats = [-16.797, -16.799, -16.801, -16.803, -16.805, -16.807]
    lons = [-49.394, -49.396, -49.398, -49.400, -49.402, -49.404]
    serie18 = [_passo(i, lats[i], -49.25, i * 10) for i in range(6)]
    serie12 = [_passo(i, -16.85, lons[i], i * 10) for i in range(6)]

    def descobrir():
        return [
            {"placa": "SCH-9B28", "veiculo": "VW/18.260", "vehicle_id": "1"},
            *presentes_no_cadastro(brutos),
        ]

    def historico(placa, _vid, _inicio, _fim):
        chave = str(placa or "").upper().replace("-", "")
        chamadas.append((fase["n"], chave))
        if chave == "SCN4B49":
            return list(serie18)
        if chave == "SCN1G89":
            return list(serie12)
        if chave == "SCH9B28":
            return [{"timestamp": "2026-10-12T08:00:00", "latitude": -16.70, "longitude": -49.25}]
        return []

    def recalcular(*_args, **_kwargs):
        rotas.append("cobertura")
        return []

    def ciclo():
        fase["n"] += 1
        _limpar_cache_frota()
        return ciclo_gps(
            tmp_path,
            "2026-10-12",
            "diurno",
            agora=datetime(2026, 10, 12, 10, 0, 0),
            buscar_historico=historico,
            descobrir=descobrir,
            recalcular=recalcular,
            limites=[],
        )

    ciclo()
    assert "SCN4B49" not in {chave for _fase, chave in chamadas}
    assert "SCN1G89" not in {chave for _fase, chave in chamadas}
    pacote = mapa.dificil_acesso("2026-10-12", "diurno")
    assert [item["sinal"] for item in pacote["veiculos"]] == ["sem_rastreamento", "sem_rastreamento"]
    assert pacote["eventos"] == []
    assert len(pacote["areas"]) == 2

    brutos.append({"ras_vei_placa": "SCN4B49", "ras_vei_id": "9", "ras_vei_veiculo": "IVECO"})
    ciclo()
    assert sum(1 for n, chave in chamadas if n == 2 and chave == "SCN4B49") == 1
    cbt018 = next(item for item in mapa.dificil_acesso("2026-10-12", "diurno")["veiculos"] if item["codigo"] == "CBT018")
    cbt012 = next(item for item in mapa.dificil_acesso("2026-10-12", "diurno")["veiculos"] if item["codigo"] == "CBT012")
    assert cbt018["pulsos"] == 6
    assert cbt018["eventos"][0]["tipo"] == "entrada"
    assert cbt018["eventos"][0]["area"] == "BAIRRO A"
    assert cbt018["dentro"] is True
    assert cbt018["aviso"] == SINAL_DESATUALIZADO
    assert cbt018["trajeto"]
    assert cbt012["pulsos"] == 0
    assert cbt012["rotulo"] == SEM_RASTREAMENTO
    assert cbt012["eventos"] == []

    brutos.append({"ras_vei_placa": "SCN1G89", "ras_vei_id": "12", "ras_vei_veiculo": "CBT012"})
    ciclo()
    assert sum(1 for n, chave in chamadas if n == 3 and chave == "SCN1G89") == 1
    assert sum(1 for n, chave in chamadas if n == 3 and chave == "SCN4B49") == 1
    pacote = mapa.dificil_acesso("2026-10-12", "diurno")
    por_codigo = {item["codigo"]: item for item in pacote["veiculos"]}
    assert por_codigo["CBT012"]["eventos"][0]["tipo"] == "entrada"
    assert por_codigo["CBT012"]["eventos"][0]["area"] == "BAIRRO B"
    assert por_codigo["CBT012"]["eventos"][0]["veiculo"] == "CBT012"
    assert por_codigo["CBT012"]["eventos"][0]["data"] == "2026-10-12"
    assert por_codigo["CBT012"]["eventos"][0]["turno"] == "diurno"
    assert {ev["veiculo"] for ev in pacote["eventos"]} == {"CBT018", "CBT012"}
    assert rotas == []
    _limpar_cache_frota()


def test_historico_sobrevive_reinicio_e_troca_de_turno(monkeypatch, tmp_path):
    _banco(monkeypatch, tmp_path)
    import inlog_agent.live_mapa as mapa
    from inlog_agent.live_db import conectar, gravar_rotas, inserir_pontos

    mapa._AREAS = None
    monkeypatch.setattr(mapa, "areas_dificil", lambda: [AREA])
    con = conectar()
    gravar_rotas(
        con,
        "2026-10-12",
        "diurno",
        [
            {
                "codigo": "66DN",
                "rota": "66DN",
                "percentual": 42,
                "status": "Concluída parcialmente",
                "grupo": "parcial",
                "placa": "SCH-9B28",
            }
        ],
    )
    tabelas = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    inserir_pontos(
        con,
        "2026-10-12",
        "diurno",
        "SCN-4B49",
        [
            {"timestamp": "2026-10-12T09:50:00", "latitude": -16.85, "longitude": -49.25},
            {"timestamp": "2026-10-12T09:50:20", "latitude": -16.8504, "longitude": -49.2501},
        ],
    )
    inserir_pontos(
        con,
        "2026-10-12",
        "diurno",
        "SCN-1G89",
        [{"timestamp": "2026-10-12T09:00:00", "latitude": -16.85, "longitude": -49.25}],
    )
    con.commit()
    antes = tuple(con.execute("SELECT percentual, status, grupo FROM rota WHERE codigo = '66DN'").fetchone())
    pontos = con.execute("SELECT COUNT(*) FROM ponto").fetchone()[0]
    con.close()

    def ler(data, turno):
        return mapa.dificil_acesso(data, turno)

    vivo = ler("2026-10-12", "diurno")
    por = {item["codigo"]: item for item in vivo["veiculos"]}
    assert por["CBT018"]["sinal"] == "atualizado"
    assert por["CBT018"]["aviso"] == ""
    assert por["CBT018"]["rotulo"] == "Permanência em BAIRRO A"
    assert por["CBT018"]["trajeto"]
    assert por["CBT018"]["eventos"][0]["tipo"] == "entrada"
    assert all(ev["tipo"] != "saida" for ev in por["CBT018"]["eventos"])
    assert por["CBT012"]["sinal"] == "desatualizado"
    assert por["CBT012"]["aviso"] == SINAL_DESATUALIZADO
    assert "BAIRRO A" in por["CBT012"]["rotulo"]
    assert por["CBT012"]["eventos"][0]["tipo"] == "entrada"
    assert len(vivo["areas"]) == 1

    con = conectar()
    assert con.execute("SELECT COUNT(*) FROM ponto").fetchone()[0] == pontos
    con.close()
    de_novo = ler("2026-10-12", "diurno")
    assert [ev["ts"] for ev in de_novo["eventos"]] == [ev["ts"] for ev in vivo["eventos"]]

    noturno = ler("2026-10-12", "noturno")
    assert noturno["eventos"] == []
    assert all(item["pulsos"] == 0 and item["rotulo"] == SEM_RASTREAMENTO for item in noturno["veiculos"])
    assert len(noturno["areas"]) == 1
    depois_do_noturno = ler("2026-10-12", "diurno")
    assert [ev["ts"] for ev in depois_do_noturno["eventos"]] == [ev["ts"] for ev in vivo["eventos"]]

    antigo = ler("2026-10-06", "diurno")
    assert antigo["eventos"] == []
    con = conectar()
    inserir_pontos(
        con,
        "2026-10-06",
        "diurno",
        "SCN-4B49",
        [{"timestamp": "2026-10-06T08:00:00", "latitude": -16.85, "longitude": -49.25}],
    )
    con.commit()
    con.close()
    historico = ler("2026-10-06", "diurno")
    velho = next(item for item in historico["veiculos"] if item["codigo"] == "CBT018")
    assert velho["sinal"] == "historico"
    assert velho["aviso"] == ""
    assert velho["rotulo"] == "Permanência em BAIRRO A"
    assert velho["eventos"][0]["data"] == "2026-10-06"
    assert velho["eventos"][0]["turno"] == "diurno"
    seguia = ler("2026-10-12", "diurno")
    assert [ev["ts"] for ev in seguia["eventos"]] == [ev["ts"] for ev in vivo["eventos"]]

    con = conectar()
    depois = tuple(con.execute("SELECT percentual, status, grupo FROM rota WHERE codigo = '66DN'").fetchone())
    tabelas_fim = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert antes == depois == (42.0, "Concluída parcialmente", "parcial")
    assert tabelas_fim == tabelas
    assert "area_dificil" not in tabelas_fim
