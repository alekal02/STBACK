from __future__ import annotations

from playwright.sync_api import Frame, Page, TimeoutError as PlaywrightTimeout

MENU_ALIASES = (
    "sinótico de veículos",
    "sinotico de veiculos",
    "sinótico de veiculo",
    "sinótico veículos",
    "sinotico veiculos",
    "quadro sinótico",
    "quadro sinotico",
    "sinótico",
    "sinotico",
)

HISTORICO_ALIASES = (
    "histórico",
    "historico",
    "histórico de posições",
    "historico de posicoes",
    "buscar histórico",
    "buscar historico",
)


def visible_texts(page: Page) -> list[str]:
    textos: list[str] = []
    script = """() => Array.from(document.querySelectorAll('a, button, span, li, td, th, label, div'))
            .map(el => (el.innerText || '').trim())
            .filter(t => t && t.length < 120)
        """
    for frame in all_frames(page):
        try:
            textos.extend(frame.evaluate(script))
        except Exception:
            continue
    return textos


def all_frames(page: Page) -> list[Frame]:
    return page.frames


def click_by_text(page: Page, aliases: tuple[str, ...], timeout: int = 8000) -> bool:
    lowered = [alias.lower() for alias in aliases]
    locators = [
        page.get_by_role("link", name=alias, exact=False)
        for alias in aliases
    ] + [
        page.get_by_role("button", name=alias, exact=False)
        for alias in aliases
    ] + [
        page.get_by_text(alias, exact=False)
        for alias in aliases
    ]

    for locator in locators:
        try:
            target = locator.first
            if target.count() == 0:
                continue
            target.scroll_into_view_if_needed(timeout=timeout)
            target.click(timeout=timeout)
            page.wait_for_timeout(1200)
            return True
        except PlaywrightTimeout:
            continue
        except Exception:
            continue

    for frame in all_frames(page):
        try:
            clicked = frame.evaluate(
                """(aliases) => {
                    const nodes = Array.from(document.querySelectorAll('a, button, span, li, td, div'));
                    for (const el of nodes) {
                        const text = (el.innerText || '').trim().toLowerCase();
                        if (!text || text.length > 80) continue;
                        if (aliases.some(alias => text.includes(alias))) {
                            el.click();
                            return text;
                        }
                    }
                    return '';
                }""",
                lowered,
            )
        except Exception:
            continue
        if clicked:
            page.wait_for_timeout(1200)
            return True
    return False


def fill_filter(page: Page, texto: str) -> bool:
    selectors = (
        "input[type='search']",
        "input[placeholder*='Pesquis' i]",
        "input[placeholder*='Busc' i]",
        "input[placeholder*='Filt' i]",
        "input[placeholder*='Veíc' i]",
        "input[placeholder*='Veic' i]",
        "input[name*='filtro' i]",
        "input[name*='pesquisa' i]",
        "input[id*='filtro' i]",
        "input[id*='search' i]",
    )
    for frame in all_frames(page):
        for selector in selectors:
            loc = frame.locator(selector)
            if loc.count() == 0:
                continue
            try:
                campo = loc.first
                if not campo.is_visible():
                    continue
                campo.fill(texto)
                campo.press("Enter")
                page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    return False


def collect_menu_snapshot(page: Page) -> dict:
    frames = []
    for frame in all_frames(page):
        try:
            frames.append(
                frame.evaluate(
                    """() => ({
                        url: location.href,
                        title: document.title,
                        links: Array.from(document.querySelectorAll('a'))
                            .map(a => ({ text: (a.innerText || '').trim(), href: a.href }))
                            .filter(a => a.text || a.href)
                            .slice(0, 400),
                        buttons: Array.from(document.querySelectorAll('button, input[type=button], input[type=submit]'))
                            .map(b => (b.innerText || b.value || '').trim())
                            .filter(Boolean)
                            .slice(0, 200)
                    })"""
                )
            )
        except Exception:
            continue
    principal = frames[0] if frames else {"url": page.url, "title": "", "links": [], "buttons": []}
    return {
        **principal,
        "frames": frames,
    }
