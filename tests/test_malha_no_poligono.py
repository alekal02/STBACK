"""Malha recortada no polígono: rua de fora não entra no total de metros."""

from datetime import date, timedelta

from inlog_agent.mapa_oficial import FOLGA_RECORTE_M, carregar_setores


def test_setores_de_19_a_25_nao_trazem_rua_fora_da_folga():
    inicio = date(2026, 10, 19)
    vistos = 0
    for n in range(7):
        for setor in carregar_setores(inicio + timedelta(days=n)):
            vistos += 1
            geom = setor["geom"]
            zona = geom.buffer(FOLGA_RECORTE_M + 0.5)
            for rua in setor.get("ruas") or []:
                sobra = rua.difference(zona)
                assert sobra.is_empty or sobra.length < 0.05, setor["codigo"]
    assert vistos > 0


def test_malha_62dnc_fica_ate_15_porcento_acima_do_km_oficial():
    setor = next(
        item
        for item in carregar_setores(date(2026, 10, 20))
        if str(item.get("codigo") or "").upper() == "62DNC"
    )
    oficial_m = float(setor["km_oficial"]) * 1000.0
    malha_m = sum(rua.length for rua in setor["ruas"])
    assert oficial_m > 0
    assert malha_m <= oficial_m * 1.15
