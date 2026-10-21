"""Cobertura de uma rota por vários caminhões. Não fala com a Fulltrack e não grava histórico encerrado."""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from shapely.geometry import LineString

from inlog_agent.cobertura import (
    SEM_APOIO,
    TO_UTM,
    TO_WGS,
    cobertura_compartilhada,
    ler_executores_json,
)
from inlog_agent.mapa_oficial import caminho_gpkg, carregar_setores

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
                "tz": "BRT",
            }
        )
        dist += passo
        momento += timedelta(seconds=passo_s)
    return saida


def _veiculo(placa: str, pontos: list[dict]) -> dict:
    return {"placa": placa, "veiculo": placa, "pontos": pontos}


def _placas(resultado: dict) -> list[str]:
    return [item["placa"] for item in resultado["executores"]]


def test_um_caminhao_coincide_com_a_cobertura_homologada():
    from inlog_agent.cobertura import avaliar_cobertura

    ruas = [_rua(0, 0, 1200, 0), _rua(0, 150, 1200, 150)]
    pontos = _pontos(ruas[0], datetime(2026, 10, 19, 8, 0)) + _pontos(ruas[1], datetime(2026, 10, 19, 9, 0))
    novo = cobertura_compartilhada(ruas, [_veiculo("AAA1A11", pontos)])
    antigo = avaliar_cobertura([], pontos, ruas=ruas)
    assert abs(float(novo["km_feito"]) - float(antigo["km_coleta"])) < 0.05
    assert novo["executores"][0]["papel"] == "principal"


def test_um_caminhao_cobre_a_rota_e_nao_inventa_apoio():
    ruas = [_rua(0, 0, 1200, 0), _rua(0, 150, 1200, 150)]
    pontos = _pontos(ruas[0], datetime(2026, 10, 19, 8, 0)) + _pontos(ruas[1], datetime(2026, 10, 19, 9, 0))
    resultado = cobertura_compartilhada(ruas, [_veiculo("AAA1A11", pontos)])
    assert resultado["percentual"] >= 95
    assert _placas(resultado) == ["AAA1A11"]
    assert resultado["executores"][0]["papel"] == "principal"
    assert resultado["apoio"] == SEM_APOIO
    assert "AAA1A11" in resultado["resumo_veiculos"]
    assert SEM_APOIO in resultado["resumo_veiculos"]


def test_dois_ou_mais_caminhoes_somam_sem_duplicar():
    ruas = [_rua(0, 0, 1200, 0), _rua(0, 150, 1200, 150), _rua(0, 300, 1000, 300)]
    a = _veiculo("AAA1A11", _pontos(ruas[0], datetime(2026, 10, 19, 8, 0)))
    b = _veiculo("BBB2B22", _pontos(ruas[1], datetime(2026, 10, 19, 8, 10)))
    c = _veiculo("CCC3C33", _pontos(ruas[2], datetime(2026, 10, 19, 8, 20)))
    so_a = cobertura_compartilhada(ruas, [a])
    todos = cobertura_compartilhada(ruas, [a, b, c])
    assert todos["percentual"] > so_a["percentual"] + 20
    assert todos["percentual"] <= 100
    assert set(_placas(todos)) == {"AAA1A11", "BBB2B22", "CCC3C33"}
    apoios = [item for item in todos["executores"] if item["papel"] == "apoio"]
    assert len(apoios) == 2
    assert all(item["contribuicao_exclusiva"] > 10 for item in apoios)
    assert todos["apoio"] != SEM_APOIO
    assert "BBB2B22" in todos["apoio"] and "CCC3C33" in todos["apoio"]


def test_principal_com_apoio_parcial():
    ruas = [_rua(0, 0, 1600, 0), _rua(0, 150, 500, 150)]
    principal = _veiculo("AAA1A11", _pontos(ruas[0], datetime(2026, 10, 19, 8, 0)))
    apoio = _veiculo("BBB2B22", _pontos(ruas[1], datetime(2026, 10, 19, 9, 0)))
    resultado = cobertura_compartilhada(ruas, [principal, apoio])
    papeis = {item["placa"]: item["papel"] for item in resultado["executores"]}
    assert papeis["AAA1A11"] == "principal"
    assert papeis["BBB2B22"] == "apoio"
    individual = {item["placa"]: item["percentual"] for item in resultado["executores"]}
    assert individual["AAA1A11"] > individual["BBB2B22"]
    exclusivo = next(item["contribuicao_exclusiva"] for item in resultado["executores"] if item["placa"] == "BBB2B22")
    assert exclusivo > 5
    assert resultado["percentual"] > individual["AAA1A11"]


def test_caminhao_executa_uma_rota_e_apoia_outra():
    rota_a = [_rua(0, 0, 1400, 0)]
    rota_b = [_rua(0, 400, 1400, 400), _rua(0, 550, 700, 550)]
    frota = _veiculo("AAA1A11", _pontos(rota_a[0], datetime(2026, 10, 19, 8, 0)))
    apoio = _veiculo(
        "AAA1A11",
        _pontos(rota_b[1], datetime(2026, 10, 19, 10, 0)),
    )
    dono_b = _veiculo("BBB2B22", _pontos(rota_b[0], datetime(2026, 10, 19, 8, 0)))
    na_sua = cobertura_compartilhada(rota_a, [frota])
    na_outra = cobertura_compartilhada(rota_b, [dono_b, apoio])
    assert na_sua["executores"][0]["papel"] == "principal"
    assert na_sua["apoio"] == SEM_APOIO
    papeis = {item["placa"]: item["papel"] for item in na_outra["executores"]}
    assert papeis["BBB2B22"] == "principal"
    assert papeis["AAA1A11"] == "apoio"


def test_mesma_rua_nao_duplica_percentual():
    ruas = [_rua(0, 0, 1200, 0), _rua(0, 180, 1200, 180)]
    a = _veiculo("AAA1A11", _pontos(ruas[0], datetime(2026, 10, 19, 8, 0)))
    b = _veiculo("BBB2B22", _pontos(ruas[0], datetime(2026, 10, 19, 8, 5)))
    um = cobertura_compartilhada(ruas, [a])
    dois = cobertura_compartilhada(ruas, [a, b])
    assert abs(dois["percentual"] - um["percentual"]) < 1.5
    assert set(_placas(dois)) == {"AAA1A11", "BBB2B22"}
    segundo = next(item for item in dois["executores"] if item["placa"] == "BBB2B22")
    assert segundo["contribuicao_exclusiva"] < 2
    if segundo["contribuicao_exclusiva"] == 0:
        assert segundo["papel"] == "sobreposta"
        assert dois["apoio"] == SEM_APOIO


def test_cobertura_sobreposta_permanece_no_detalhe_sem_mudar_o_total():
    ruas = [_rua(0, 0, 1200, 0), _rua(0, 180, 1200, 180)]
    pontos = _pontos(ruas[0], datetime(2026, 10, 19, 8, 0))
    a = _veiculo("AAA1A11", pontos)
    b = _veiculo("BBB2B22", list(pontos))
    um = cobertura_compartilhada(ruas, [a])
    dois = cobertura_compartilhada(ruas, [a, b])
    invertido = cobertura_compartilhada(ruas, [b, a])
    assert dois["percentual"] == um["percentual"] == invertido["percentual"]
    assert dois["apoio"] == SEM_APOIO
    assert dois["sobrepostos"] == ["BBB2B22"]
    assert invertido["placa_principal"] == dois["placa_principal"]
    assert set(invertido["sobrepostos"]) == set(dois["sobrepostos"])
    segundo = next(item for item in dois["executores"] if item["placa"] == "BBB2B22")
    assert segundo["papel"] == "sobreposta"
    assert segundo["classificacao"] == "Cobertura sobreposta"
    assert segundo["contribuicao_exclusiva"] == 0
    assert segundo["contribuicao_compartilhada"] > 0
    assert segundo["trechos"]
    assert "BBB2B22" not in dois["resumo_veiculos"]
    assert "Cobertura sobreposta" not in dois["apoio"]


def test_trecho_curto_com_progressao_continua_e_apoio():
    longa = _rua(0, 0, 1200, 0)
    curta = _rua(0, 200, 38, 200)
    principal = _veiculo("AAA1A11", _pontos(longa, datetime(2026, 10, 19, 8, 0)))
    apoio = _veiculo("BBB2B22", _pontos(curta, datetime(2026, 10, 19, 9, 0), vel_kmh=12, passo_s=8))
    um = cobertura_compartilhada([longa, curta], [principal])
    dois = cobertura_compartilhada([longa, curta], [principal, apoio])
    invertido = cobertura_compartilhada([longa, curta], [apoio, principal])
    assert dois["percentual"] > um["percentual"]
    assert dois["percentual"] == invertido["percentual"]
    assert set(dois["apoio"].split(", ")) == set(invertido["apoio"].split(", "))
    segundo = next(item for item in dois["executores"] if item["placa"] == "BBB2B22")
    assert segundo["papel"] == "apoio"
    assert segundo["contribuicao_exclusiva"] > 0
    assert len(segundo["trechos"]) >= 1


def test_aproximacao_nao_cria_apoio_e_preserva_a_uniao():
    longa = _rua(0, 0, 1200, 0)
    outra = _rua(0, 220, 1200, 220)
    ponta = _rua(400, 50, 400, 90)
    base = _pontos(longa, datetime(2026, 10, 19, 8, 0))
    desvio = _pontos(LineString([(_X + 400, _Y + 12), (_X + 430, _Y + 12)]), datetime(2026, 10, 19, 8, 20), vel_kmh=26, passo_s=4)
    principal = _veiculo("AAA1A11", base + _pontos(outra, datetime(2026, 10, 19, 9, 0)))
    encosta = _veiculo("BBB2B22", base + desvio)
    ruas = [longa, outra, ponta]
    um = cobertura_compartilhada(ruas, [principal])
    dois = cobertura_compartilhada(ruas, [encosta, principal])
    assert abs(dois["percentual"] - um["percentual"]) < 1.5
    segundo = next(item for item in dois["executores"] if item["placa"] == "BBB2B22")
    assert segundo["papel"] == "sobreposta"
    assert segundo["contribuicao_exclusiva"] == 0
    assert "BBB2B22" not in dois["apoio"]
    assert "BBB2B22" in dois["cobertura_sobreposta"]
    assert dois["placa_principal"] == "AAA1A11"
    ficha = next(item for item in dois["auditoria"] if item["placa"] == "BBB2B22")
    assert "já cobertos" in ficha["motivo"]
    assert segundo["trechos"]
    lido = ler_executores_json(__import__("json").dumps(dois["persistencia"]))
    assert lido["apoio"] == dois["apoio"]
    assert lido["cobertura_sobreposta"] == dois["cobertura_sobreposta"]
    assert "BBB2B22" not in lido["resumo_veiculos"]
    assert lido["passagens"] == dois["passagens"]
    diag = dois["diagnostico"]
    assert diag["cobertura_geografica_pct"] == dois["percentual"]
    assert diag["evidencia_operacional_m"] <= diag["cobertura_geografica_m"]
    assert ficha["evidencia_operacional_m"] <= ficha["cobertura_geografica_m"]
    assert lido["diagnostico"]["cobertura_geografica_pct"] == dois["percentual"]


def test_atravessar_o_setor_nao_conta_como_servico():
    ruas = [_rua(0, 0, 1200, 0)]
    cruzamento = LineString([(_X + 400, _Y - 250), (_X + 400, _Y + 250)])
    travessia = _veiculo("ZZZ9Z99", _pontos(cruzamento, datetime(2026, 10, 19, 8, 0), vel_kmh=55, passo_s=2))
    sozinha = cobertura_compartilhada(ruas, [travessia])
    assert sozinha["executores"] == []
    assert sozinha["percentual"] == 0
    assert "ZZZ9Z99" in sozinha["passagens"]
    assert sozinha["apoio"] == SEM_APOIO
    ficha = next(item for item in sozinha["auditoria"] if item["placa"] == "ZZZ9Z99")
    assert ficha["entra_na_uniao"] is False
    assert ficha["evidencia_operacional_m"] == 0
    coleta = _veiculo("AAA1A11", _pontos(ruas[0], datetime(2026, 10, 19, 8, 0)))
    com_coleta = cobertura_compartilhada(ruas, [coleta, travessia])
    assert _placas(com_coleta) == ["AAA1A11"]
    assert com_coleta["percentual"] >= 95


def test_gps_com_salto_e_ponto_isolado_nao_inventa_trecho():
    ruas = [_rua(0, 0, 800, 0), _rua(0, 500, 800, 500)]
    validos = _pontos(ruas[0], datetime(2026, 10, 19, 8, 0))
    salto = {
        "latitude": validos[-1]["latitude"] + 0.02,
        "longitude": validos[-1]["longitude"],
        "timestamp": "2026-10-19T08:40:00",
        "tz": "BRT",
    }
    isolados = []
    for i, dist in enumerate((0, 250, 500, 750)):
        ponto = ruas[0].interpolate(dist)
        lon, lat = TO_WGS.transform(ponto.x, ponto.y)
        isolados.append(
            {
                "latitude": lat,
                "longitude": lon,
                "timestamp": f"2026-10-19T07:{10 + i:02d}:00",
                "tz": "BRT",
            }
        )
    com_falha = cobertura_compartilhada(ruas, [_veiculo("AAA1A11", validos + [salto])])
    assert com_falha["percentual"] < 80
    assert com_falha["executores"]
    vazio = cobertura_compartilhada(ruas, [_veiculo("ZZZ9Z99", isolados)])
    assert vazio["percentual"] == 0
    assert vazio["executores"] == []
    assert "ZZZ9Z99" in vazio["nao_confirmados"]
    ficha = next(item for item in vazio["auditoria"] if item["placa"] == "ZZZ9Z99")
    assert ficha["entra_na_uniao"] is False


def test_rotas_proximas_nao_herdam_passagem():
    propria = [_rua(0, 0, 1200, 0)]
    vizinha = [_rua(0, 120, 1200, 120)]
    coleta = _pontos(propria[0], datetime(2026, 10, 19, 8, 0))
    raspao = _pontos(LineString([(_X, _Y + 120), (_X + 90, _Y + 120)]), datetime(2026, 10, 19, 11, 0), vel_kmh=50, passo_s=2)
    na_vizinha = cobertura_compartilhada(vizinha, [_veiculo("AAA1A11", coleta + raspao)])
    assert na_vizinha["executores"] == []
    na_propria = cobertura_compartilhada(propria, [_veiculo("AAA1A11", coleta)])
    assert na_propria["percentual"] >= 95


def test_turno_diurno_e_noturno_guardam_a_janela():
    rua = [_rua(0, 0, 1200, 0)]
    dia = cobertura_compartilhada(
        rua,
        [_veiculo("AAA1A11", _pontos(rua[0], datetime(2026, 10, 19, 8, 5)))],
    )
    noite = cobertura_compartilhada(
        rua,
        [_veiculo("BBB2B22", _pontos(rua[0], datetime(2026, 10, 19, 23, 10)))],
    )
    assert dia["executores"][0]["hora_inicio"] == "08:05"
    assert noite["executores"][0]["hora_inicio"] == "23:10"
    assert noite["executores"][0]["horario"].startswith("23:10")


def test_reinicio_recalcula_do_gps_persistido_sem_memoria():
    ruas = [_rua(0, 0, 1200, 0), _rua(0, 160, 1200, 160)]
    primeiro = _veiculo("AAA1A11", _pontos(ruas[0], datetime(2026, 10, 19, 8, 0)))
    antes = cobertura_compartilhada(ruas, [primeiro])
    segundo = _veiculo("BBB2B22", _pontos(ruas[1], datetime(2026, 10, 19, 9, 0)))
    depois = cobertura_compartilhada(ruas, [primeiro, segundo])
    de_novo = cobertura_compartilhada(ruas, [primeiro, segundo])
    assert depois["percentual"] > antes["percentual"] + 20
    assert depois["percentual"] == de_novo["percentual"]
    assert _placas(depois) == _placas(de_novo)
    assert ler_executores_json(__import__("json").dumps(depois["persistencia"]))["resumo_veiculos"] == depois["resumo_veiculos"]


def test_mapa_api_tabela_e_pdf_leem_o_mesmo_snapshot():
    anterior = os.environ.get("SAGTRACK_LIVE_DB")
    pasta = Path(tempfile.mkdtemp())
    os.environ["SAGTRACK_LIVE_DB"] = str(pasta / "live.db")
    os.environ["SAGTRACK_LIVE_SYNC"] = "0"
    try:
        ruas = [_rua(0, 0, 1200, 0), _rua(0, 160, 900, 160)]
        a = _veiculo("AAA1A11", _pontos(ruas[0], datetime(2026, 10, 19, 8, 0)))
        b = _veiculo("BBB2B22", _pontos(ruas[1], datetime(2026, 10, 19, 9, 30)))
        c = _veiculo("CCC3C33", list(a["pontos"]))
        conta = cobertura_compartilhada(ruas, [a, b, c])
        from inlog_agent.live_db import conectar, gravar_rotas, gravar_turno, inserir_pontos
        from inlog_agent.live_mapa import detalhe_rota
        from inlog_agent.live_pdf import _rota_lida
        from inlog_agent.live_sync import _anexar_geometria_visual

        setor = {
            "codigo": "TST1",
            "_ruas": ruas,
            "_geom": None,
            "_cobertura": conta,
            "caminhao": {},
            "candidatos": [],
            "status": "PARCIAL",
        }
        rota = {"codigo": "TST1", "rota": "TST1", "frequencia": "TESTE", "na_grade": True, "grupo": "parcial", "percentual": 0}
        _anexar_geometria_visual([setor], [], [rota])
        assert rota["percentual"] == conta["percentual"]
        assert rota["placa"] == conta["placa_principal"]
        con = conectar()
        gravar_turno(con, {"data": "2026-10-19", "turno": "diurno", "aberto": 1, "periodo": "teste"})
        gravar_rotas(con, "2026-10-19", "diurno", [rota])
        for veiculo in (a, b, c):
            inserir_pontos(con, "2026-10-19", "diurno", veiculo["placa"], veiculo["pontos"])
        con.commit()
        con.close()
        con = conectar()
        from inlog_agent.live_db import ler_view

        painel = ler_view(con, "2026-10-19", "diurno")
        linha = con.execute("SELECT * FROM rota WHERE codigo = 'TST1'").fetchone()
        con.close()
        tabela = next(item for item in painel["rotas"] if item["codigo"] == "TST1")
        pdf = _rota_lida(linha)
        detalhe = detalhe_rota("2026-10-19", "diurno", "TST1")
        assert tabela["percentual"] == conta["percentual"]
        assert pdf["percentual"] == conta["percentual"]
        assert detalhe["percentual"] == conta["percentual"]
        assert tabela["resumo_veiculos"] == pdf["resumo_veiculos"] == detalhe["resumo_veiculos"]
        assert tabela["apoio"] == pdf["apoio"] == detalhe["apoio"]
        assert set(tabela["placas_apoio"]) == set(pdf["placas_apoio"]) == set(detalhe["placas_apoio"])
        assert "BBB2B22" in tabela["apoio"]
        assert "CCC3C33" not in tabela["apoio"]
        assert tabela["cobertura_sobreposta"] == pdf["cobertura_sobreposta"] == detalhe["cobertura_sobreposta"]
        assert "CCC3C33" in detalhe["cobertura_sobreposta"]
        assert "CCC3C33" not in tabela["resumo_veiculos"]
        cores = {item["placa"]: item["cor"] for item in detalhe["executores"]}
        assert len(set(cores.values())) == 3
        placas_mapa = {item["placa"] for item in detalhe["veiculos"]}
        assert placas_mapa == set(_placas(conta))
        assert any(item["linhas"] for item in detalhe["veiculos"])
        assert detalhe["trechos_feitos"]["features"]
        assert detalhe["trechos_faltando"]["type"] == "FeatureCollection"
    finally:
        if anterior is None:
            os.environ.pop("SAGTRACK_LIVE_DB", None)
        else:
            os.environ["SAGTRACK_LIVE_DB"] = anterior


def test_previa_de_turno_encerrado_nao_altera_o_gravado():
    anterior = os.environ.get("SAGTRACK_LIVE_DB")
    pasta = Path(tempfile.mkdtemp())
    os.environ["SAGTRACK_LIVE_DB"] = str(pasta / "live.db")
    os.environ["SAGTRACK_LIVE_SYNC"] = "0"
    try:
        from inlog_agent.live_db import conectar, gravar_rotas, gravar_turno
        from inlog_agent.live_sync import auditar_turno_leitura

        con = conectar()
        gravar_turno(con, {"data": "2026-10-19", "turno": "noturno", "aberto": 0, "periodo": "teste"})
        gravar_rotas(
            con,
            "2026-10-19",
            "noturno",
            [{"codigo": "65DN", "placa": "SDE5I26", "percentual": 60.3, "grupo": "parcial", "status": "Parcial", "na_grade": True, "_geo": True}],
        )
        con.commit()
        con.close()
        previa = auditar_turno_leitura("2026-10-19", "noturno")
        assert previa["intacto"] is True
        assert previa["rotas"][0]["gravado"] == 60.3
        assert previa["rotas"][0]["auditado"] is None
        con = conectar()
        row = con.execute("SELECT percentual, placa, executores_json FROM rota WHERE codigo = '65DN'").fetchone()
        con.close()
        assert row["percentual"] == 60.3
        assert row["placa"] == "SDE5I26"
        assert not str(row["executores_json"] or "").strip()
    finally:
        if anterior is None:
            os.environ.pop("SAGTRACK_LIVE_DB", None)
        else:
            os.environ["SAGTRACK_LIVE_DB"] = anterior


def test_turno_encerrado_nao_e_regravado():
    anterior = os.environ.get("SAGTRACK_LIVE_DB")
    pasta = Path(tempfile.mkdtemp())
    os.environ["SAGTRACK_LIVE_DB"] = str(pasta / "live.db")
    os.environ["SAGTRACK_LIVE_SYNC"] = "0"
    try:
        from inlog_agent.live_db import conectar, gravar_rotas, gravar_turno
        from inlog_agent.live_sync import ciclo_gps

        con = conectar()
        gravar_turno(con, {"data": "2026-10-01", "turno": "noturno", "aberto": 0, "periodo": "teste"})
        con.execute(
            "UPDATE turno SET situacao = 'ENCERRADO', aberto = 0 WHERE data = ? AND turno = ?",
            ("2026-10-01", "noturno"),
        )
        gravar_rotas(
            con,
            "2026-10-01",
            "noturno",
            [{"codigo": "65DN", "placa": "HIST1", "percentual": 37.5, "grupo": "parcial", "status": "Parcial", "na_grade": True, "_geo": True}],
        )
        con.commit()
        con.close()

        def proibido(*_args, **_kwargs):
            raise AssertionError("turno encerrado não baixa GPS")

        resultado = ciclo_gps(
            pasta,
            "2026-10-01",
            "noturno",
            agora=datetime(2026, 10, 20, 20, 0, 0),
            buscar_historico=proibido,
            descobrir=proibido,
        )
        assert resultado.get("fechado") is True
        con = conectar()
        row = con.execute("SELECT percentual, placa, executores_json FROM rota WHERE codigo = '65DN'").fetchone()
        con.close()
        assert row["percentual"] == 37.5
        assert row["placa"] == "HIST1"
        assert not str(row["executores_json"] or "").strip()
    finally:
        if anterior is None:
            os.environ.pop("SAGTRACK_LIVE_DB", None)
        else:
            os.environ["SAGTRACK_LIVE_DB"] = anterior


def _setor(codigo: str) -> dict:
    for setor in carregar_setores(None):
        if str(setor.get("codigo") or "").upper() == codigo:
            return setor
    raise AssertionError(f"rota oficial {codigo} ausente")


def _dividir(ruas: list) -> tuple[list, list]:
    marcas = sorted((linha.centroid.x, linha) for linha in ruas)
    meio = max(1, len(marcas) // 2)
    return [linha for _x, linha in marcas[:meio]], [linha for _x, linha in marcas[meio:]]


def test_65dn_e_outra_rota_oficial_reconhecem_dois_caminhoes():
    if caminho_gpkg() is None:
        return
    for codigo in ("65DN", "64DN"):
        setor = _setor(codigo)
        ruas = list(setor.get("ruas") or [])
        geom = setor.get("geom")
        esquerda, direita = _dividir(ruas)
        inicio = datetime(2026, 10, 19, 20, 0)
        pontos_a = []
        pontos_b = []
        for indice, rua in enumerate(esquerda):
            if rua.length < 80:
                continue
            pontos_a.extend(_pontos(rua, inicio + timedelta(minutes=indice)))
        for indice, rua in enumerate(direita):
            if rua.length < 80:
                continue
            pontos_b.extend(_pontos(rua, inicio + timedelta(minutes=indice)))
        a = _veiculo("PRI0A00", pontos_a)
        b = _veiculo("APO0B00", pontos_b)
        antes = max(
            cobertura_compartilhada(ruas, [a], geom)["percentual"],
            cobertura_compartilhada(ruas, [b], geom)["percentual"],
        )
        depois = cobertura_compartilhada(ruas, [a, b], geom)
        assert depois["percentual"] > antes + 3, codigo
        assert set(_placas(depois)) == {"PRI0A00", "APO0B00"}
        assert depois["apoio"] != SEM_APOIO
        assert codigo not in depois["resumo_veiculos"]


def test_via_paralela_dentro_do_raio_nao_e_creditada():
    propria = _rua(0, 0, 1200, 0)
    paralela = _rua(0, 30, 1200, 30)
    resultado = cobertura_compartilhada(
        [propria, paralela],
        [_veiculo("AAA1A11", _pontos(propria, datetime(2026, 10, 19, 8, 0)))],
    )
    assert 40 <= resultado["percentual"] <= 60
    assert resultado["executores"][0]["papel"] == "principal"


def test_contorno_percorrido_entra_no_percentual_sem_virar_apoio():
    from shapely.geometry import Polygon

    interna = _rua(0, 200, 1200, 200)
    contorno = _rua(0, 5, 800, 5)
    geom = Polygon([(_X - 30, _Y), (_X + 1400, _Y), (_X + 1400, _Y + 500), (_X - 30, _Y + 500)])
    principal = _veiculo("AAA1A11", _pontos(interna, datetime(2026, 10, 19, 8, 0)))
    beira = _veiculo("BBB2B22", _pontos(contorno, datetime(2026, 10, 19, 9, 0)))
    so_interna = cobertura_compartilhada([interna, contorno], [principal], geom)
    ambos = cobertura_compartilhada([interna, contorno], [principal, beira], geom)
    assert ambos["percentual"] > so_interna["percentual"] + 15
    papeis = {item["placa"]: item["papel"] for item in ambos["executores"]}
    assert papeis["AAA1A11"] == "principal"
    assert papeis["BBB2B22"] == "sobreposta"
    assert ambos["apoio"] == SEM_APOIO
    ficha = next(item for item in ambos["auditoria"] if item["placa"] == "BBB2B22")
    assert "contorno" in ficha["motivo"]


def test_exclusiva_interna_curta_continua_apoio():
    from shapely.geometry import Polygon

    longa = _rua(0, 200, 1200, 200)
    curta = _rua(80, 260, 160, 260)
    geom = Polygon([(_X - 30, _Y), (_X + 1400, _Y), (_X + 1400, _Y + 500), (_X - 30, _Y + 500)])
    principal = _veiculo("AAA1A11", _pontos(longa, datetime(2026, 10, 19, 8, 0)))
    apoio = _veiculo("BBB2B22", _pontos(curta, datetime(2026, 10, 19, 9, 0), vel_kmh=12, passo_s=8))
    resultado = cobertura_compartilhada([longa, curta], [principal, apoio], geom)
    segundo = next(item for item in resultado["executores"] if item["placa"] == "BBB2B22")
    assert segundo["papel"] == "apoio"
    assert segundo["contribuicao_exclusiva"] > 0


def test_vias_indistinguiveis_ficam_nao_confirmadas():
    esquerda = _rua(0, 0, 800, 0)
    direita = _rua(0, 10, 800, 10)
    meio = LineString([(_X, _Y + 5), (_X + 800, _Y + 5)])
    resultado = cobertura_compartilhada(
        [esquerda, direita],
        [_veiculo("AAA1A11", _pontos(meio, datetime(2026, 10, 19, 8, 0)))],
    )
    assert resultado["percentual"] == 0
    assert resultado["executores"] == []
    assert "AAA1A11" in resultado["nao_confirmados"]


def test_amostragem_espaçada_cobre_a_via_percorrida():
    propria = _rua(0, 0, 800, 0)
    paralela = _rua(0, 30, 800, 30)
    pontos = _pontos(propria, datetime(2026, 10, 19, 8, 0), vel_kmh=18, passo_s=20)
    resultado = cobertura_compartilhada([propria, paralela], [_veiculo("AAA1A11", pontos)])
    assert resultado["percentual"] >= 45
    assert resultado["percentual"] <= 60


def test_passo_isolado_nao_cria_apoio_e_o_trecho_entra_na_uniao():
    from shapely.geometry import Polygon

    longa = _rua(0, 200, 1200, 200)
    beco = _rua(400, 260, 440, 260)
    geom = Polygon([(_X - 30, _Y), (_X + 1400, _Y), (_X + 1400, _Y + 500), (_X - 30, _Y + 500)])
    principal = _veiculo("AAA1A11", _pontos(longa, datetime(2026, 10, 19, 8, 0)))
    isolado = _veiculo(
        "BBB2B22",
        _pontos(longa, datetime(2026, 10, 19, 8, 0))[:8] + _pontos(beco, datetime(2026, 10, 19, 9, 0)),
    )
    so = cobertura_compartilhada([longa, beco], [principal], geom)
    ambos = cobertura_compartilhada([longa, beco], [principal, isolado], geom)
    assert ambos["percentual"] > so["percentual"]
    papeis = {item["placa"]: item["papel"] for item in ambos["executores"]}
    assert papeis["AAA1A11"] == "principal"
    assert papeis["BBB2B22"] == "nao_confirmado"
    assert ambos["apoio"] == SEM_APOIO
    assert "BBB2B22" not in ambos["cobertura_sobreposta"]
    assert "BBB2B22" in ambos["nao_confirmados"]
    ficha = next(item for item in ambos["auditoria"] if item["placa"] == "BBB2B22")
    assert "único passo" in ficha["motivo"]
    assert "não confirmada" in ficha["motivo"]


def test_circulacao_em_um_sentido_nao_vira_apoio():
    longa = _rua(0, 0, 1200, 0)
    reta = _rua(0, 200, 120, 200)
    principal = _veiculo("AAA1A11", _pontos(longa, datetime(2026, 10, 19, 8, 0)))
    passagem = _veiculo(
        "BBB2B22",
        _pontos(longa, datetime(2026, 10, 19, 8, 0))[:8]
        + _pontos(reta, datetime(2026, 10, 19, 10, 0), vel_kmh=24, passo_s=6),
    )
    so = cobertura_compartilhada([longa, reta], [principal])
    ambos = cobertura_compartilhada([longa, reta], [principal, passagem])
    assert ambos["percentual"] > so["percentual"]
    papeis = {item["placa"]: item["papel"] for item in ambos["executores"]}
    assert papeis["AAA1A11"] == "principal"
    assert papeis["BBB2B22"] == "nao_confirmado"
    assert "BBB2B22" not in ambos["apoio"]
    assert "BBB2B22" not in ambos["cobertura_sobreposta"]
    ficha = next(item for item in ambos["auditoria"] if item["placa"] == "BBB2B22")
    assert "circulação" in ficha["motivo"]


def test_empate_curto_nao_troca_o_principal_por_ruido():
    from shapely.geometry import Polygon

    base = _rua(0, 200, 1200, 200)
    lasca = _rua(0, 260, 30, 260)
    geom = Polygon([(_X - 30, _Y), (_X + 1400, _Y), (_X + 1400, _Y + 500), (_X - 30, _Y + 500)])
    inicio = datetime(2026, 10, 19, 8, 0)
    comum = _pontos(base, inicio)
    com_lasca = _veiculo("ZZZ9Z99", comum + _pontos(lasca, inicio + timedelta(hours=2)))
    estavel = _veiculo("AAA1A11", list(comum))
    resultado = cobertura_compartilhada([base, lasca], [com_lasca, estavel], geom)
    assert resultado["placa_principal"] == "AAA1A11"


def test_dois_percursos_nao_somam_o_mesmo_segmento():
    ruas = [_rua(0, 0, 1000, 0), _rua(0, 180, 1000, 180)]
    pontos = _pontos(ruas[0], datetime(2026, 10, 19, 8, 0))
    um = cobertura_compartilhada(ruas, [_veiculo("AAA1A11", pontos)])
    dois = cobertura_compartilhada(
        ruas,
        [_veiculo("AAA1A11", pontos), _veiculo("BBB2B22", list(pontos))],
    )
    assert dois["percentual"] == um["percentual"]
    assert dois["km_feito"] == um["km_feito"]
