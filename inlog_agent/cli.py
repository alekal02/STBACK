from __future__ import annotations

import argparse

from .config import aplicar_periodo, load_settings, rotulo_periodo
from .server import servir


def cmd_rotas(
    headed: bool | None,
    data: str | None,
    veiculos: str | None,
    hora_inicio: str | None = None,
    hora_fim: str | None = None,
    data_fim: str | None = None,
) -> None:
    settings = aplicar_periodo(load_settings(), data=data, hora_inicio=hora_inicio, hora_fim=hora_fim, data_fim=data_fim)
    if veiculos:
        settings = type(settings)(**{
            **settings.__dict__,
            "filtro_veiculos": tuple(item.strip() for item in veiculos.split(",") if item.strip()),
        })

    from .rotas import executar_rotas

    print(
        f"Extraindo rotas de {rotulo_periodo(settings.data_rota, settings.hora_inicio, settings.hora_fim, settings.data_fim)}...",
        flush=True,
    )
    relatorio = executar_rotas(settings, headed=headed)
    print("Coleta de rotas concluída.", flush=True)
    print(f"Data: {relatorio.get('data')}", flush=True)
    print(f"Veículos: {relatorio.get('total_veiculos')}", flush=True)
    print(f"Pontos GPS: {relatorio.get('total_pontos')}", flush=True)
    if relatorio.get("percentual_medio") is not None:
        print(f"% médio concluído: {relatorio.get('percentual_medio')}")
    arquivos = relatorio.get("arquivos") or {}
    if arquivos.get("mapa"):
        print(f"Mapa: {arquivos['mapa']}")
    if arquivos.get("csv"):
        print(f"CSV: {arquivos['csv']}")
    if arquivos.get("resumo"):
        print(f"JSON: {arquivos['resumo']}")
    if arquivos.get("pdf"):
        print(f"PDF: {arquivos['pdf']}")
    if relatorio["total_pontos"] == 0:
        print("Nenhum ponto GPS nesta sessão. Confira o .env e a internet.")


def cmd_segtrack_rotas(
    headed: bool | None,
    data: str | None,
    hora_inicio: str | None = None,
    hora_fim: str | None = None,
    data_fim: str | None = None,
) -> None:
    from .segtrack_rotas import executar_segtrack

    settings = aplicar_periodo(load_settings(), data=data, hora_inicio=hora_inicio, hora_fim=hora_fim, data_fim=data_fim)
    print(
        f"Extraindo Segtrack de {rotulo_periodo(settings.data_rota, settings.hora_inicio, settings.hora_fim, settings.data_fim)}...",
        flush=True,
    )
    relatorio = executar_segtrack(settings, headed=headed)
    print("Coleta Segtrack concluída.", flush=True)
    print(f"Obra: {relatorio.get('obra')}", flush=True)
    print(f"Veículos: {relatorio.get('total_veiculos')}", flush=True)
    print(f"Pontos GPS: {relatorio.get('total_pontos')}", flush=True)
    arquivos = relatorio.get("arquivos") or {}
    if arquivos.get("relatorio"):
        print(f"Relatório: {arquivos['relatorio']}")
    if arquivos.get("mapa"):
        print(f"Mapa: {arquivos['mapa']}")


def cmd_argos_rotas(
    headed: bool | None,
    data: str | None,
    hora_inicio: str | None = None,
    hora_fim: str | None = None,
    data_fim: str | None = None,
) -> None:
    from .argos_rotas import executar_argos

    settings = aplicar_periodo(load_settings(), data=data, hora_inicio=hora_inicio, hora_fim=hora_fim, data_fim=data_fim)
    print(
        f"Extraindo Argos de {rotulo_periodo(settings.data_rota, settings.hora_inicio, settings.hora_fim, settings.data_fim)}...",
        flush=True,
    )
    relatorio = executar_argos(settings, headed=headed)
    print("Coleta Argos concluída.", flush=True)
    print(f"Obra: {relatorio.get('obra')}", flush=True)
    print(f"Veículos: {relatorio.get('total_veiculos')}", flush=True)
    print(f"Pontos GPS: {relatorio.get('total_pontos')}", flush=True)
    print(f"Casados com Inlog: {relatorio.get('cruzados_inlog')}", flush=True)
    arquivos = relatorio.get("arquivos") or {}
    if arquivos.get("relatorio"):
        print(f"Relatório: {arquivos['relatorio']}")
    if arquivos.get("mapa"):
        print(f"Mapa: {arquivos['mapa']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inlog_agent",
        description="Agente que entra no rastreamento Inlog e exporta rotas dos caminhões.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Abre o Chromium na tela (recomendado na primeira execução).",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    def add_periodo(p):
        p.add_argument("--hora-inicio", default=None, help="Hora inicial HH:MM (padrão 00:00)")
        p.add_argument("--hora-fim", default=None, help="Hora final HH:MM (padrão 23:59)")
        p.add_argument("--data-fim", default=None, help="Data final YYYY-MM-DD se o ciclo atravessa o dia")

    rotas = sub.add_parser("rotas", help="Extrai a rota do dia dos caminhões de coleta")
    rotas.add_argument("--data", help="Data no formato YYYY-MM-DD (padrão: hoje)")
    rotas.add_argument(
        "--veiculos",
        help="Prefixos/placas separados por vírgula. Ex.: 101,102,ABC1D23",
    )
    add_periodo(rotas)

    web = sub.add_parser("servir", help="Sobe o painel web local")
    web.add_argument("--porta", type=int, help="Porta HTTP (padrão: 5889)")
    web.add_argument("--host", default=None, help="Host de bind (padrão: HOST do .env ou 127.0.0.1)")

    pdf = sub.add_parser("pdf", help="Gera PDF do dia com rotas feitas e mapa desenhado")
    pdf.add_argument("--data", required=True, help="Data no formato YYYY-MM-DD")
    pdf.add_argument("--argos", action="store_true", help="Gera o PDF do relatório Argos")
    pdf.add_argument("--segtrack", action="store_true", help="Gera o PDF do relatório Segtrack")
    pdf.add_argument("--unificado", action="store_true", help="Gera o PDF da frota e rotas do dia (3 empresas)")
    pdf.add_argument("--turno", choices=["diurno", "noturno"], default=None)

    argos = sub.add_parser("argos-rotas", help="Extrai a frota e o rastreio do Argos")
    argos.add_argument("--data", help="Data no formato YYYY-MM-DD (padrão: hoje)")
    add_periodo(argos)
    gpkg = sub.add_parser("argos-gpkg", help="Exporta o percurso Argos do dia em GeoPackage")
    gpkg.add_argument("--data", required=True, help="Data no formato YYYY-MM-DD")
    gpkg_inlog = sub.add_parser("gpkg", help="Exporta as rotas Inlog do dia em GeoPackage")
    gpkg_inlog.add_argument("--data", required=True, help="Data no formato YYYY-MM-DD")
    seg = sub.add_parser("segtrack-rotas", help="Extrai compactadores Segtrack em Aparecida")
    seg.add_argument("--data", help="Data no formato YYYY-MM-DD (padrão: hoje)")
    add_periodo(seg)
    gpkg_seg = sub.add_parser("segtrack-gpkg", help="Exporta o percurso Segtrack do dia em GeoPackage")
    gpkg_seg.add_argument("--data", required=True, help="Data no formato YYYY-MM-DD")
    uni = sub.add_parser("unificado", help="Coleta do dia: login nas 3 empresas, mapa e relatório")
    uni.add_argument("--data", help="Data no formato YYYY-MM-DD (padrão: hoje)")
    add_periodo(uni)
    uni.add_argument("--extrair", action="store_true", help="Faz login e puxa o GPS das 3 empresas antes do relatório")
    uni.add_argument("--so-relatorio", action="store_true", help="Só monta o relatório com o GPS já extraído")
    uni.add_argument(
        "--turno",
        choices=["diurno", "noturno"],
        help="Diurno 06:00–18:59 ou noturno a partir das 19:00, só com os polígonos desse turno",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    headed = True if args.headed else None

    if args.comando == "rotas":
        cmd_rotas(
            headed,
            getattr(args, "data", None),
            getattr(args, "veiculos", None),
            getattr(args, "hora_inicio", None),
            getattr(args, "hora_fim", None),
            getattr(args, "data_fim", None),
        )
        return
    if args.comando == "servir":
        servir(porta=args.porta, host=args.host)
        return
    if args.comando == "pdf":
        from .pdf import gerar_pdf_dia

        settings = load_settings()
        if args.argos:
            pasta = settings.output_dir / "argos" / args.data
        elif args.segtrack:
            pasta = settings.output_dir / "segtrack" / args.data
        elif args.unificado:
            from .pdf_cache import resolver_trabalho

            pasta = resolver_trabalho(settings.output_dir / "unificado" / args.data, getattr(args, "turno", None))
        else:
            pasta = settings.output_dir / "rotas" / args.data
        caminho = gerar_pdf_dia(pasta)
        print(f"PDF: {caminho}", flush=True)
        return
    if args.comando == "argos-rotas":
        cmd_argos_rotas(
            headed,
            getattr(args, "data", None),
            getattr(args, "hora_inicio", None),
            getattr(args, "hora_fim", None),
            getattr(args, "data_fim", None),
        )
        return
    if args.comando == "argos-gpkg":
        from .export_gpkg import gerar_gpkg_argos

        settings = load_settings()
        caminho = gerar_gpkg_argos(settings.output_dir / "argos" / args.data)
        print(f"GPKG: {caminho}", flush=True)
        return
    if args.comando == "gpkg":
        from .export_gpkg import gerar_gpkg_inlog

        settings = load_settings()
        caminho = gerar_gpkg_inlog(settings.output_dir / "rotas" / args.data)
        print(f"GPKG: {caminho}", flush=True)
        return
    if args.comando == "segtrack-rotas":
        cmd_segtrack_rotas(
            headed,
            getattr(args, "data", None),
            getattr(args, "hora_inicio", None),
            getattr(args, "hora_fim", None),
            getattr(args, "data_fim", None),
        )
        return
    if args.comando == "segtrack-gpkg":
        from .export_gpkg import gerar_gpkg_segtrack

        settings = load_settings()
        caminho = gerar_gpkg_segtrack(settings.output_dir / "segtrack" / args.data)
        print(f"GPKG: {caminho}", flush=True)
        return
    if args.comando == "unificado":
        settings = aplicar_periodo(
            load_settings(),
            data=args.data,
            hora_inicio=getattr(args, "hora_inicio", None),
            hora_fim=getattr(args, "hora_fim", None),
            data_fim=getattr(args, "data_fim", None),
        )
        from .unificado import executar_coleta_do_dia

        extrair = bool(args.extrair) and not bool(args.so_relatorio)
        relatorio = executar_coleta_do_dia(
            settings,
            extrair=extrair,
            turno=getattr(args, "turno", None),
        )
        print(f"Feitas: {relatorio.get('feitas')}", flush=True)
        print(f"Parciais: {relatorio.get('parciais')}", flush=True)
        print(f"Apoios: {relatorio.get('apoios')}", flush=True)
        print(f"Sem coleta: {relatorio.get('nao_feitas')}", flush=True)
        arquivos = relatorio.get("arquivos") or {}
        if arquivos.get("relatorio"):
            print(f"Relatório: {arquivos['relatorio']}", flush=True)
        if arquivos.get("mapa"):
            print(f"Mapa: {arquivos['mapa']}", flush=True)
        if arquivos.get("pdf"):
            print(f"PDF: {arquivos['pdf']}", flush=True)
        falhas = relatorio.get("falhas") or {}
        if falhas:
            print("Falhas: " + " · ".join(f"{k}: {v}" for k, v in falhas.items()), flush=True)
        return
    parser.error("Comando inválido")
