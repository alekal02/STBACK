"""Executor da rota oficial: via percorrida e permanência, não km de GPS no polígono.

Cenário de 18/10/2026, diurno, POLO INDUSTRIAL.
"""

from shapely.geometry import Point
from shapely.ops import unary_union

from inlog_agent.mapa_oficial import TO_WGS, carregar_setores
from inlog_agent.unificado import (
    _escolher_vencedor,
    _execucao_das_vias,
    _pode_executar,
    _rebaixar_passagens,
)


def _cand(
    placa: str,
    execucao: str,
    *,
    pct: float,
    metros: float | None,
    km: float,
    inicio: str = "07:00",
    fim: str = "11:30",
) -> dict:
    row = {
        "id": placa,
        "placa": placa,
        "veiculo": placa,
        "fonte_nome": "SEGTRACK",
        "execucao": execucao,
        "percentual": pct,
        "km_coleta": km,
        "pontos_no_setor": 400,
        "hora_inicio": inicio,
        "hora_fim": fim,
        "passagens": [{}, {}],
        "tortuosidade": 2.4,
    }
    if metros is not None:
        row["percentual_vias"] = pct
        row["metros_vias"] = metros
    return row


def _cenario_18_outubro() -> list[dict]:
    return [
        _cand("SDJ-6C47", "PARCIAL", pct=89.0, metros=23936.4, km=12.0),
        _cand("SDM4D87", "PASSAGEM", pct=18.2, metros=4913.8, km=30.0, inicio="09:00", fim="09:20"),
        _cand("SDE6B76", "PASSAGEM", pct=43.0, metros=11609.6, km=48.0, inicio="08:00", fim="08:25"),
        _cand("SDG1J76", "PASSAGEM", pct=13.5, metros=3644.9, km=22.0, inicio="10:00", fim="10:12"),
        _cand("SDG6D46", "PASSAGEM", pct=0.8, metros=216.0, km=8.0, inicio="11:00", fim="11:04"),
    ]


def test_89_porcento_vence_quem_tem_mais_km_no_poligono():
    fila = _cenario_18_outubro()
    fila.append(_cand("SDE6B76", "PARCIAL", pct=43.0, metros=11609.6, km=80.0))
    vencedor, _nota = _escolher_vencedor(fila)
    assert vencedor["placa"] == "SDJ-6C47"
    assert vencedor["execucao"] == "PARCIAL"
    assert _execucao_das_vias(89.0, 23936.4) == "PARCIAL"


def test_passagem_nao_assume_a_rota():
    vencedor, _nota = _escolher_vencedor(_cenario_18_outubro())
    assert vencedor["placa"] == "SDJ-6C47"
    so_passagem = [row for row in _cenario_18_outubro() if row["execucao"] == "PASSAGEM"]
    _recusado, nota = _escolher_vencedor(so_passagem)
    assert nota == "passagem, não coleta"
    assert max(so_passagem, key=lambda row: row["km_coleta"])["placa"] == "SDE6B76"


def test_secundario_continua_passagem_e_filtro_barra_a_execucao():
    fila = _cenario_18_outubro()
    sdj = fila[0]
    for row in fila[1:]:
        row["execucao"] = "PARCIAL"
    melhor = 23936.4
    assert _pode_executar(sdj, melhor, {"SDJ6C47"}) is True
    assert _pode_executar(fila[2], melhor, {"SDE6B76"}) is False
    assert _pode_executar(sdj, melhor, {"SDE6B76"}) is False
    assert _pode_executar(fila[1], melhor, None) is False
    _rebaixar_passagens(fila, sdj)
    assert sdj["execucao"] == "PARCIAL"
    assert {row["placa"]: row["execucao"] for row in fila[1:]} == {
        "SDM4D87": "PASSAGEM",
        "SDE6B76": "PASSAGEM",
        "SDG1J76": "PASSAGEM",
        "SDG6D46": "PASSAGEM",
    }


def test_apoio_no_vizinho_nao_vira_executor():
    from inlog_agent.unificado import aplicar_apoios

    caminhao = {
        "id": "SDJ6C47",
        "placa": "SDJ-6C47",
        "veiculo": "SDJ-6C47",
        "hora_inicio": "07:00",
        "hora_fim": "11:00",
        "horario": "07:00–11:00",
        "tortuosidade": 2.4,
    }
    principal = {
        "codigo": "01AD",
        "status": "PARCIAL",
        "percentual": 89,
        "frequencia": "SEG DIURNO",
        "grade_do_dia": True,
        "caminhao": dict(caminhao),
    }
    vizinho = {
        "codigo": "02AD",
        "status": "PASSAGEM",
        "percentual": 22,
        "frequencia": "SEG DIURNO",
        "grade_do_dia": True,
        "caminhao": {
            **caminhao,
            "hora_inicio": "11:10",
            "hora_fim": "11:45",
            "horario": "11:10–11:45",
        },
    }
    aplicar_apoios([principal, vizinho])
    assert principal["status"] == "PARCIAL"
    assert principal["caminhao"]["placa"] == "SDJ-6C47"
    assert vizinho["status"] == "APOIO"
    assert vizinho["rota_principal"] == "01AD"
    assert vizinho["caminhao"]["placa"] == "SDJ-6C47"


def test_empate_de_via_usa_permanencia_e_nao_o_km():
    curto = _cand("CURTO", "PARCIAL", pct=60.0, metros=None, km=40.0, inicio="08:00", fim="08:10")
    longo = _cand("LONGO", "PARCIAL", pct=60.0, metros=None, km=4.0, inicio="07:00", fim="12:00")
    vencedor, _nota = _escolher_vencedor([curto, longo])
    assert vencedor["placa"] == "LONGO"


def _pontos(coords, segundos: int) -> list[dict]:
    saida = []
    for indice, ponto in enumerate(coords):
        instante = 6 * 3600 + segundos + indice * 15
        hora, resto = divmod(instante, 3600)
        minuto, segundo = divmod(resto, 60)
        if hora >= 19:
            break
        lon, lat = TO_WGS.transform(ponto.x, ponto.y)
        saida.append(
            {
                "timestamp": f"2026-10-18T{hora:02d}:{minuto:02d}:{segundo:02d}",
                "latitude": lat,
                "longitude": lon,
            }
        )
    return saida


def _ao_longo(ruas, fracao: float) -> list:
    total = sum(rua.length for rua in ruas) or 1.0
    alvo = fracao * total
    saida = []
    andado = 0.0
    for rua in sorted(ruas, key=lambda item: item.length, reverse=True):
        if andado >= alvo:
            break
        dist = 0.0
        while dist <= rua.length and andado < alvo:
            saida.append(rua.interpolate(min(dist, rua.length)))
            dist += 40.0
            andado += 40.0
    return saida


def _fora_da_via(geom, ruas, limite: int = 180) -> list:
    livre = geom.difference(unary_union(ruas).buffer(60))
    if livre.is_empty:
        return []
    partes = list(getattr(livre, "geoms", [livre]))
    poly = max(partes, key=lambda item: item.area)
    minx, miny, maxx, maxy = poly.bounds
    saida = []
    y = miny
    direita = True
    while y <= maxy and len(saida) < limite:
        xs = []
        x = minx
        while x <= maxx:
            xs.append(x)
            x += 75.0
        if not direita:
            xs.reverse()
        direita = not direita
        for x in xs:
            ponto = Point(x, y)
            if poly.covers(ponto):
                saida.append(ponto)
                if len(saida) >= limite:
                    break
        y += 90.0
    return saida


def test_polo_industrial_atribui_sdj_e_respeita_o_filtro(monkeypatch, tmp_path):
    banco = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent.live_db import conectar, gravar_rotas, gravar_turno, inserir_pontos, marcar_placa
    from inlog_agent.live_sync import reprocessar_rotas_historicas

    polo = next(setor for setor in carregar_setores(None) if setor["codigo"].upper() == "POLO INDUSTRIAL")
    ruas = polo["ruas"]
    executor = _pontos(_ao_longo(ruas, 0.80), 0)
    passagem = _pontos(_ao_longo(ruas, 0.30) + _fora_da_via(polo["geom"], ruas) * 4, 30)
    assert len(executor) > 50
    assert len(passagem) > len(executor) * 0.4

    con = conectar()
    gravar_turno(
        con,
        {
            "data": "2026-10-18",
            "turno": "diurno",
            "aberto": 0,
            "periodo": "18/10/2026 diurno",
            "hora_inicio": "06:00",
            "hora_fim": "18:59",
            "atualizado_em": "2026-10-18T19:20:00",
        },
    )
    con.execute(
        """
        UPDATE turno SET situacao = 'ENCERRADO', aberto = 0, fechado_em = '2026-10-18T19:20:00',
            resumo_json = '{"nao_iniciadas":1}'
        WHERE data = '2026-10-18' AND turno = 'diurno'
        """
    )
    gravar_rotas(
        con,
        "2026-10-18",
        "diurno",
        [
            {
                "codigo": "POLO INDUSTRIAL",
                "rota": "POLO INDUSTRIAL",
                "frequencia": "DOM",
                "status": "Não iniciada",
                "grupo": "nao",
                "percentual": 0,
                "na_grade": True,
            }
        ],
    )
    inserir_pontos(con, "2026-10-18", "diurno", "SDJ-6C47", executor)
    inserir_pontos(con, "2026-10-18", "diurno", "SDE6B76", passagem)
    marcar_placa(con, "2026-10-18", "diurno", "SDJ-6C47", veiculo="SDJ-6C47")
    marcar_placa(con, "2026-10-18", "diurno", "SDE6B76", veiculo="SDE6B76")
    con.commit()
    con.close()

    previa = reprocessar_rotas_historicas(
        tmp_path,
        "2026-10-18",
        "diurno",
        ["POLO INDUSTRIAL"],
        placas=["SDJ-6C47", "SDE6B76"],
    )
    assert previa["aplicado"] is False
    assert previa["previa"] is True
    ficha = previa["rotas"]["POLO INDUSTRIAL"]
    assert ficha["placa"] == "SDJ-6C47"
    assert 80 <= ficha["percentual"] < 95
    assert ficha["metros_cobertos"] > 20000
    assert ficha["status"] == "Parcial"

    so_passagem = reprocessar_rotas_historicas(
        tmp_path,
        "2026-10-18",
        "diurno",
        ["POLO INDUSTRIAL"],
        placas=["SDE6B76"],
    )
    bloqueada = so_passagem["rotas"]["POLO INDUSTRIAL"]
    assert bloqueada["placa"] in {"", "—"}
    assert bloqueada["percentual"] == 0

    so_executor = reprocessar_rotas_historicas(
        tmp_path,
        "2026-10-18",
        "diurno",
        ["POLO INDUSTRIAL"],
        placas=["SDJ-6C47"],
    )
    assert so_executor["rotas"]["POLO INDUSTRIAL"]["placa"] == "SDJ-6C47"
    assert so_executor["rotas"]["POLO INDUSTRIAL"]["status"] == "Parcial"
    assert 80 <= so_executor["rotas"]["POLO INDUSTRIAL"]["percentual"] < 95

    con = conectar()
    intacta = con.execute(
        """
        SELECT percentual, placa, status FROM rota
        WHERE data = '2026-10-18' AND turno = 'diurno' AND codigo = 'POLO INDUSTRIAL'
        """
    ).fetchone()
    copias = con.execute(
        """
        SELECT COUNT(*) FROM rota
        WHERE data = '2026-10-18' AND turno = 'diurno' AND codigo = 'POLO INDUSTRIAL'
        """
    ).fetchone()[0]
    con.close()
    assert intacta["percentual"] == 0
    assert intacta["placa"] in {"", "—", None}
    assert intacta["status"] == "Não iniciada"
    assert copias == 1

    from inlog_agent.live_report import ler_estado

    painel = ler_estado(tmp_path, "2026-10-18", "diurno")
    tela = next(rota for rota in painel["rotas"] if rota["codigo"] == "POLO INDUSTRIAL")
    assert painel["situacao"] == "ENCERRADO"
    assert painel["historico"] is True
    assert tela["placa"] == "SDJ-6C47"
    assert tela["status"] == "Parcial"
    assert 80 <= tela["percentual"] < 95
    assert painel["resumo"]["percentual_geral"] == round(tela["percentual"])
    assert painel["resumo"]["parciais"] == 1
    assert painel["resumo"]["nao_iniciadas"] == 0

    con = conectar()
    depois = con.execute(
        """
        SELECT t.situacao, r.percentual, r.placa, r.status
        FROM turno t JOIN rota r ON r.data = t.data AND r.turno = t.turno
        WHERE t.data = '2026-10-18' AND t.turno = 'diurno' AND r.codigo = 'POLO INDUSTRIAL'
        """
    ).fetchone()
    con.close()
    assert depois["situacao"] == "ENCERRADO"
    assert depois["percentual"] == 0
    assert depois["placa"] in {"", "—", None}
    assert depois["status"] == "Não iniciada"


def test_execucao_geral_segue_as_rotas_exibidas():
    from inlog_agent.live_report import _exibir_classificacao

    estado = _exibir_classificacao(
        {
            "resumo": {"percentual_geral": 0, "nao_iniciadas": 3, "previstas": 3},
            "rotas": [
                {"codigo": "POLO INDUSTRIAL", "na_grade": True, "grupo": "nao", "percentual": 89, "status": "Não iniciada"},
                {"codigo": "01AD", "na_grade": True, "grupo": "nao", "percentual": 0, "status": "Não iniciada"},
                {"codigo": "02AD", "na_grade": True, "grupo": "apoio", "percentual": 30, "status": "Apoio"},
                {"codigo": "EXTRA", "na_grade": False, "grupo": "parcial", "percentual": 100, "status": "Parcial"},
            ],
        }
    )
    resumo = estado["resumo"]
    assert resumo["percentual_geral"] == round((89 + 0) / 2)
    assert resumo["parciais"] == 1
    assert resumo["nao_iniciadas"] == 1
    assert resumo["apoios"] == 1
    assert resumo["previstas"] == 2
    polo = next(rota for rota in estado["rotas"] if rota["codigo"] == "POLO INDUSTRIAL")
    assert polo["grupo"] == "parcial"
    assert polo["status"] == "Parcial"
    assert polo["percentual"] == 89
