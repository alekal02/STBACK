"""POLO INDUSTRIAL é a rota de domingo e o código não segue o padrão 00XX."""

from datetime import date, datetime

from inlog_agent.live_report import _codigo_da_linha, _rota
from inlog_agent.live_sync import _via_percorrida
from inlog_agent.unificado import _linha, obter_rotas_previstas


def test_domingo_diurno_so_programa_polo_industrial():
    domingo = date(2026, 10, 18)
    diurno = obter_rotas_previstas(domingo, "diurno")
    assert [rota["codigo"] for rota in diurno] == ["POLO INDUSTRIAL"]
    assert diurno[0]["frequencia"] == "DOM"
    segunda = obter_rotas_previstas(date(2026, 10, 19), "diurno")
    assert "POLO INDUSTRIAL" not in {rota["codigo"] for rota in segunda}


def test_codigo_polo_industrial_nao_vira_vazio():
    linha = _linha(
        {
            "codigo": "POLO INDUSTRIAL",
            "setor": "POLO INDUSTRIAL",
            "frequencia": "DOM",
            "percentual": 0,
            "status": "NÃO FEITA",
            "grade_do_dia": True,
        }
    )
    rota = _rota(linha)
    assert linha["Codigo"] == "POLO INDUSTRIAL"
    assert rota["codigo"] == "POLO INDUSTRIAL"
    assert _codigo_da_linha({"Setor": "08AD - POLO-MARIVANIA"}) == "08AD"
    assert _codigo_da_linha({"Setor": "POLO INDUSTRIAL"}) == "POLO INDUSTRIAL"


def test_entrada_no_poligono_sem_via_nao_executa():
    assert _via_percorrida({"percentual": 80, "trechos_feitos": "[]"}) is False
    assert _via_percorrida({"percentual": 0, "trechos_feitos": "[[[1,2],[3,4]]]"}) is False
    assert _via_percorrida({"percentual": 12.5, "trechos_feitos": "[[[1,2],[3,4]]]"}) is True


def test_reprocessa_so_a_via_percorrida_e_preserva_o_resto(monkeypatch, tmp_path):
    banco = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent.live_db import conectar, gravar_rotas, gravar_turno, inserir_pontos, marcar_placa
    from inlog_agent.live_sync import ciclo_gps, reprocessar_rotas_historicas
    from inlog_agent.mapa_oficial import TO_WGS, carregar_setores

    polo = next(setor for setor in carregar_setores(None) if setor["codigo"].upper() == "POLO INDUSTRIAL")
    linha = max(polo["ruas"], key=lambda rua: rua.length)
    via = []
    distancia = 0.0
    indice = 0
    while distancia <= linha.length and indice < 40:
        ponto = linha.interpolate(distancia)
        lon, lat = TO_WGS.transform(ponto.x, ponto.y)
        minuto = 8 * 60 + indice
        via.append(
            {
                "timestamp": f"2026-10-18T{minuto // 60:02d}:{minuto % 60:02d}:00",
                "latitude": lat,
                "longitude": lon,
            }
        )
        distancia += 40
        indice += 1
    centro = polo["geom"].representative_point()
    lon, lat = TO_WGS.transform(centro.x, centro.y)
    so_poligono = [
        {
            "timestamp": f"2026-10-18T10:{indice:02d}:00",
            "latitude": lat + indice * 0.00001,
            "longitude": lon,
        }
        for indice in range(10)
    ]

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
            },
            {
                "codigo": "01AD",
                "rota": "01AD - OUTRA",
                "frequencia": "SEG",
                "placa": "AAA0A00",
                "veiculo": "OUTRO",
                "status": "Parcial",
                "grupo": "parcial",
                "percentual": 37,
                "na_grade": True,
            },
        ],
    )
    gravar_turno(
        con,
        {
            "data": "2026-10-19",
            "turno": "diurno",
            "aberto": 1,
            "periodo": "19/10/2026 diurno",
            "hora_inicio": "06:00",
            "hora_fim": "18:59",
            "atualizado_em": "2026-10-19T08:00:00",
        },
    )
    gravar_rotas(
        con,
        "2026-10-19",
        "diurno",
        [
            {
                "codigo": "01AD",
                "rota": "01AD - SEGUNDA",
                "percentual": 11,
                "placa": "BBB1B11",
                "status": "Parcial",
                "grupo": "parcial",
                "na_grade": True,
            }
        ],
    )
    inserir_pontos(con, "2026-10-18", "diurno", "SEM4VIA", so_poligono)
    marcar_placa(con, "2026-10-18", "diurno", "SEM4VIA", veiculo="SEM4VIA")
    con.commit()
    con.close()

    sem_via = reprocessar_rotas_historicas(tmp_path, "2026-10-18", "diurno", ["POLO INDUSTRIAL"])
    assert sem_via["aplicado"] is False
    assert sem_via["situacao"] == "ENCERRADO"
    assert sem_via["rotas"]["POLO INDUSTRIAL"]["percentual"] == 0
    assert sem_via["rotas"]["POLO INDUSTRIAL"]["placa"] in {"", "—"}

    con = conectar()
    inserir_pontos(con, "2026-10-18", "diurno", "SDM4D87", via)
    marcar_placa(con, "2026-10-18", "diurno", "SDM4D87", veiculo="SDM4D87")
    total = con.execute(
        "SELECT COUNT(*) FROM ponto WHERE data = '2026-10-18' AND turno = 'diurno'"
    ).fetchone()[0]
    con.commit()
    con.close()

    def repetir(_placa, _vid, _inicio, _fim):
        return list(via)

    previa = reprocessar_rotas_historicas(
        tmp_path,
        "2026-10-18",
        "diurno",
        ["POLO INDUSTRIAL"],
        buscar_historico=repetir,
        placas=["SDM4D87"],
    )
    assert previa["previa"] is True
    assert previa["aplicado"] is False
    assert previa["situacao"] == "ENCERRADO"
    ficha = previa["rotas"]["POLO INDUSTRIAL"]
    assert ficha["placa"] == "SDM4D87"
    assert ficha["pontos_placa"] == len(via)
    assert ficha["metros_cobertos"] > 0
    assert ficha["percentual"] > 0
    assert ficha["percentual_anterior"] == 0

    con = conectar()
    intacta = con.execute(
        "SELECT percentual, placa, situacao FROM rota r JOIN turno t ON t.data = r.data AND t.turno = r.turno WHERE r.data = '2026-10-18' AND r.turno = 'diurno' AND r.codigo = 'POLO INDUSTRIAL'"
    ).fetchone()
    con.close()
    assert intacta["percentual"] == 0
    assert intacta["placa"] in {"", "—"}
    assert intacta["situacao"] == "ENCERRADO"

    corrigido = reprocessar_rotas_historicas(
        tmp_path,
        "2026-10-18",
        "diurno",
        ["POLO INDUSTRIAL"],
        buscar_historico=repetir,
        placas=["SDM4D87"],
        aplicar=True,
    )
    assert corrigido["aplicado"] is True
    assert corrigido["pontos_novos"] == 0
    assert corrigido["pontos_gps"] == total
    assert corrigido["rotas"]["POLO INDUSTRIAL"]["percentual_anterior"] == 0
    assert corrigido["rotas"]["POLO INDUSTRIAL"]["percentual"] > 0
    assert corrigido["rotas"]["POLO INDUSTRIAL"]["placa"] == "SDM4D87"
    assert corrigido["atualizadas"] == ["POLO INDUSTRIAL"]

    de_novo = reprocessar_rotas_historicas(
        tmp_path,
        "2026-10-18",
        "diurno",
        ["POLO INDUSTRIAL"],
        buscar_historico=repetir,
        placas=["SDM4D87"],
        aplicar=True,
    )
    assert de_novo["pontos_novos"] == 0
    assert de_novo["pontos_gps"] == total
    assert de_novo["rotas"]["POLO INDUSTRIAL"]["percentual"] == corrigido["rotas"]["POLO INDUSTRIAL"]["percentual"]

    fechado = ciclo_gps(
        tmp_path,
        "2026-10-18",
        "diurno",
        agora=datetime(2026, 10, 19, 9, 0, 0),
        buscar_historico=lambda *_args, **_kwargs: [],
        descobrir=lambda: [],
    )
    assert fechado.get("fechado") is True

    con = conectar()
    outra = con.execute(
        "SELECT percentual, placa FROM rota WHERE data = '2026-10-18' AND turno = 'diurno' AND codigo = '01AD'"
    ).fetchone()
    segunda = con.execute(
        "SELECT percentual, placa FROM rota WHERE data = '2026-10-19' AND turno = 'diurno' AND codigo = '01AD'"
    ).fetchone()
    turno = con.execute(
        "SELECT situacao, fechado_em, resumo_json FROM turno WHERE data = '2026-10-18' AND turno = 'diurno'"
    ).fetchone()
    copias = con.execute(
        "SELECT COUNT(*) FROM rota WHERE data = '2026-10-18' AND turno = 'diurno' AND codigo = 'POLO INDUSTRIAL'"
    ).fetchone()[0]
    pontos = con.execute(
        "SELECT COUNT(*) FROM ponto WHERE data = '2026-10-18' AND turno = 'diurno' AND placa = 'SDM4D87'"
    ).fetchone()[0]
    con.close()
    assert outra["percentual"] == 37 and outra["placa"] == "AAA0A00"
    assert segunda["percentual"] == 11 and segunda["placa"] == "BBB1B11"
    assert turno["situacao"] == "ENCERRADO"
    assert turno["fechado_em"] == "2026-10-18T19:20:00"
    assert turno["resumo_json"] == '{"nao_iniciadas":1}'
    assert copias == 1
    assert pontos == len(via)
