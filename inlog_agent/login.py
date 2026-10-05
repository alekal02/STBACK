from __future__ import annotations

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from .config import Settings


class LoginError(RuntimeError):
    pass


def _mensagem_erro(page: Page) -> str:
    for selector in (".erroLogin", ".validation-summary-errors", "#erro", ".alert-danger"):
        loc = page.locator(selector)
        if loc.count() and loc.first.is_visible():
            texto = loc.first.inner_text().strip()
            if texto:
                return texto
    return ""


def esta_logado(page: Page, settings: Settings) -> bool:
    url = page.url.lower()
    if "account/login" in url:
        return False
    if "autenticador/account" in url:
        return False
    return settings.base_url.split("?")[0].rstrip("/").lower() in url or (
        "/rastreamento/apresentacao/" in url and "login" not in url
    )


def fazer_login(page: Page, settings: Settings) -> None:
    page.goto(settings.login_url, wait_until="domcontentloaded")
    page.wait_for_selector('input[name="UserName"], form#formGeral', timeout=settings.timeout_ms)

    if esta_logado(page, settings):
        return

    page.fill('input[name="UserName"]', settings.username)
    page.fill('input[name="Password"], #senha', settings.password)

    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=settings.timeout_ms):
            page.click('input#entrar, input[name="entrar"], button[type="submit"]')
    except PlaywrightTimeout:
        pass

    try:
        page.wait_for_function(
            """() => !window.location.href.toLowerCase().includes('account/login')""",
            timeout=settings.timeout_ms,
        )
    except PlaywrightTimeout as exc:
        erro = _mensagem_erro(page)
        raise LoginError(
            erro or "Login não concluiu. Confira usuário/senha no .env."
        ) from exc

    if "account/login" in page.url.lower():
        raise LoginError(_mensagem_erro(page) or "O portal devolveu a tela de login.")

    try:
        page.goto(settings.base_url, wait_until="domcontentloaded")
    except PlaywrightTimeout:
        pass
